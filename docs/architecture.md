# Architecture and decisions

## Boundaries

One Django application and one PostgreSQL database, deployed as separate web, Celery
worker, and singleton Beat processes. Redis is a broker; PostgreSQL is the source of
truth for approved-but-unsent applications. The periodic dispatcher recovers work if
publishing a task fails. Five-minute dispatch reservations and bounded batches
(100 source endpoints, 20 application/preparation items) limit duplicate queue traffic; expired reservations recover lost task publication. Duplicate deliveries are expected and harmless before send.

Shared data: Company, CompanyAlias, Source, Job, JobOrigin, JobEvent, IngestionRun.
Private data: SavedJob, Document, GmailConnection, Application.
Security records: Invitation, AuditEvent; Django's admin log records operator changes.

Companies own multiple career endpoints. Verified official domains establish canonical
company identities; ambiguous legacy identities remain distinct. Provider requisition
identities deduplicate supported vacancies across hosted URLs; canonical HTTPS URL is
the fallback. Titles are never used to infer duplicate vacancies.

Per-endpoint observations and complete inventory evidence establish job lifecycle.
Two complete inventories must omit a job before it becomes a closure candidate;
partial and failed scans cannot establish absence. Closure defaults to shadow mode,
and overlapping employer observations keep shared vacancies open. Admin content and
status overrides remain separate from imported data. See [Company catalogue](company-catalogue.md)
for migration, pagination, concurrency and rollout details.

## Submission invariant

`draft → approved → submitting → confirmed | failed | unknown`

Edits to draft/approved/failed content clear approval. Submitting, confirmed and unknown
content is locked. Private notes and tracking remain editable independently.

Approval hashes recipient, subject, body, document ID/hash, and sender. The worker
locks the application row, checks the hash and attachment bytes, and commits the
submitting state before making a non-idempotent network request. A second worker
cannot claim the same row. There is no transaction spanning PostgreSQL and Gmail:
exactly-once delivery is not promised. Crashes after claim become unknown; they are
reconciled read-only and never blindly resent.

## Search

PostgreSQL English full-text search uses a stored generated search vector and GIN
index. PostgreSQL recomputes the vector on writes rather than every search. Location filtering is an additional substring predicate.
Pagination bounds response size. A separate search service is unnecessary until
measured ranking, language or throughput requirements justify it. Query cost and COUNT
latency must be measured against representative data.

## Deployment boundary

Local Compose is a development environment. Production provider, region, budget,
availability targets, RPO/RTO and data residency remain decisions before Terraform.
References to deployed pilot features in `NEXT_CHAT.md` are historical runtime
observations, not evidence that this production readiness boundary has been met.
The intended topology is managed containers, PostgreSQL, Redis and private object
storage behind Cloudflare, with restricted origin access. No Kubernetes or microservices.

JF informed adapters and application workflows. SQLite-on-EFS infrastructure and global
application flags were not copied. Source selectors require ongoing fixture maintenance.

## Optional evaluation runtime and portal delivery

The existing Rust Infinity service evaluates vacancies; it does not replace Django,
ingestion or application delivery. Django owns authorization, source-backed result
validation and budget accounting. Infinity uses a separate restricted PostgreSQL
role/schema and short-lived capabilities to read one active execution's context.
It must not gain access to application tables or delivery tools. See
[the runtime boundary and recovery contract](../services/infinity-agent/README.md).

Approved delivery also includes supported employer portal adapters and a separate
browser-delivery queue. Explicit approval and unknown-outcome constraints apply
to those paths as well as Gmail.
