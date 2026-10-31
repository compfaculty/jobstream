# Architecture and decisions

## Boundaries

One Django application and one PostgreSQL database, deployed as separate web, Celery
worker, and singleton Beat processes. Redis is a broker; PostgreSQL is the source of
truth for approved-but-unsent applications. The periodic dispatcher recovers work if
publishing a task fails. Five-minute dispatch reservations and batches of twenty
bound duplicate queue traffic; expired reservations recover lost task publication. Duplicate deliveries are expected and harmless before send.

Shared data: Source, Job, JobOrigin, IngestionRun.
Private data: SavedJob, Document, GmailConnection, Application.
Security records: Invitation, AuditEvent; Django's admin log records source changes.

A job can appear in several sources. Canonical HTTPS URL is its identity, with common
tracking parameters removed. This is conservative URL deduplication, not semantic
cross-board matching. Different URLs may represent the same vacancy. Source observations
are preserved separately. Missing listings do not automatically expire jobs because
bounded runs are not complete snapshots; operators can archive jobs through a future
explicit lifecycle feature. Last-seen dates are shown to users.

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
The intended topology is managed containers, PostgreSQL, Redis and private object
storage behind Cloudflare, with restricted origin access. No Kubernetes or microservices.

JF informed adapters and application workflows. SQLite-on-EFS infrastructure and global
application flags were not copied. Source selectors require ongoing fixture maintenance.
