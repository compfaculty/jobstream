# Company-first discovery

See [the clean data model](data-model.md) for the current entity relationships and
fresh development bootstrap. Companies are independently discovered employers;
career sources belong to Companies, while job boards have no owning Company.
Board employer names stay on jobs as text and never create/link Company records.
The legacy `Company.source` relationship and historical backfill chain are removed.

The intended pipeline is: independently identify an employer → verify its official
website → discover its own or hosted ATS career endpoint → collect, normalize and
store jobs → prepare an application through a separately supported delivery adapter
→ preview and approve the exact application and CV before delivery. Optional AI
tailoring may emphasize verified CV facts and generate a PDF; it must not invent
qualifications or bypass approval.

Company entry supports manual/CSV input and Pituchim directory discovery.
Arbeitnow and Himalayas are job sources, not company-discovery providers. The [free catalogue workflow](company-discovery-sources.md)
imports pending companies before confirming hiring, including source-identified companies
with missing websites. Identity attestation and source activation remain operator decisions. See [Employer discovery](employer-discovery.md) for configuration,
evidence requirements, budgets and review. All seeded campaigns ship disabled.
Track fetch readiness and application readiness independently using the
[company integration criteria](company-integration-workflow.md). Fetching is the
minimum milestone; application support remains a separate high-priority milestone.
Collection support does not imply application submission support. Greenhouse/Ashby
delivery is integration-specific; Workday and Akeyless/Comeet are not generally
automated delivery routes. Unsupported routes require manual action. CV PDF
generation and optional AI tailoring exist in the batch preparation path; this is
not evidence that paid AI validation or rollout has been approved.

General RSS/Atom, Telfed and Real Work polling is disabled by migration 0028;
existing jobs, source provenance and applications remain available. Operators can
explicitly re-enable those feeds.

## Import and review

Use **Companies → Import CSV** in Django admin (company/source add permissions), or:

```sh
uv run python manage.py import_companies companies.csv
```

CSV headers are `name,website,career_url`; `career_url` is optional. URLs must be
public HTTPS. Imports are validated before writing, and repeat imports are
idempotent. Unverified identities include the normalized name and website domain;
different names are not automatically merged just because their imported URLs match.
Imported career URLs are disabled proposals, not evidence of employer authority.

Add/edit companies directly in admin. Independently verify the official website,
then select **Attest selected official websites and consolidate verified domains**.
This action consolidates only identical verified hostnames (normalizing `www.`),
retains aliases/legacy identities, and updates job/source company references without
changing job or application IDs. It does not merge merely similar names or unrelated
subdomains. Unresolved legacy profiles remain separate for review.

After attestation, **Schedule career endpoint discovery now** requests a bounded
homepage/careers-link scan; new verified companies are also picked up by Beat.
Discovery checks at most five official pages and proposes at most twenty endpoints.
Recognized Greenhouse, Ashby and Workday links from official pages establish
sufficient evidence for automatic activation. Ambiguous ownership, unsupported
external career systems, HTML/JSON-LD pages and JavaScript-only pages remain in the
source review queue. A previously operator-disabled endpoint stays disabled on
rediscovery. Changing a company's website invalidates verification and disables its
existing endpoints for review.

Generic HTML/JSON-LD sources can collect jobs after manual adapter review, but cannot
prove a complete inventory and therefore cannot infer closures. No browser rendering,
CAPTCHA bypass, authentication or application submission is part of discovery.

## Inventories and lifecycle

`CollectionResult` carries postings, scope, completion evidence, the next cursor,
errors, optional expected total and a retry time. Each posting has a stable provider
identity where supported and may carry an explicitly reported employer status.
Greenhouse/Ashby parse bounded full responses and persist up to 250 jobs per batch,
with a response hash guarding resumed inventory consistency. Workday uses read-only search POSTs
and detail GETs, with ten postings per page. Each batch is committed atomically;
cursors resume across worker invocations, including interruptions between details.
Rate limits retain checkpoints. Malformed pages, changed totals and configuration
changes discard the current cycle without inferring absence.

The configurable item ceiling is 100,000, with a hard 16 MiB limit per response.
Provider truncation, duplicate identities, or mismatched distinct-posting counts
prevent completion. A Workday cycle expires after two days so stale partial scans
restart. Completeness describes the provider's public inventory; hidden/internal
vacancies and undisclosed provider-side result caps cannot be inferred.

`JobOrigin` stores scope, observed status, cycle token, and consecutive missing count.
Only two complete successful inventories in the same endpoint scope establish
closure. Partial/failed runs never increment absence. A valid employer observation
reopens the vacancy and resets that endpoint's missing count. An overlapping
endpoint that still observes the job open keeps the shared vacancy open. A board
cannot reopen an employer-closed vacancy. Changing the configured endpoint scope
leaves the former scope's jobs for review, rather than closing them as absent.

Lifecycle states are `open`, `closed`, `filled`, and `archived`. Disappearance means
closed; filled requires an explicit employer observation or operator attestation.
Filled observations are preserved when a job subsequently disappears. The currently
supported public Workday payload does not establish a filled reason by itself.

Automatic closure defaults **off** (`Source.closure_enabled=False`). Shadow runs
still record absence and `would_close`; review those decisions for a small cohort
before enabling closure on its authoritative endpoints. Lifecycle events retain the
source, run, status, reason and time. Administrative status overrides control the
public `active` flag without stopping the underlying observation history.

## Admin operations

Jobs have permission-controlled CRUD, company/title/provider searches, lifecycle
filters, and read-only origins/events. Jobs referenced by applications or batches
cannot be hard-deleted; archive/close them instead. Edited content is stored as
curated overrides while the latest imported content remains separate. Use the
**restore latest imported content** action to clear content overrides, or **resume
automatic lifecycle management** to clear a status override.

Company admin shows aliases and endpoint health links. Source admin shows pending
reviews, lease/cursor state, last successful attempt and last complete inventory.
Manual source review can enable supported existing company-specific adapters as well.

## Scheduling and health

Endpoints default to daily checks with ±5% jitter. Beat dispatches up to 100 due
endpoints per minute using `skip_locked` database reservations. Source leases fence
concurrent workers for ten minutes; Celery's hard task limit is five minutes.
Expired running records become visible `WorkerInterrupted` failures. A lost queued
task can be republished after five minutes. Duplicate tasks are expected and harmless.

Request leases bound cross-worker concurrency globally and per host; 429/503
Retry-After cooldowns are shared per host. HTTPS redirects retain DNS pinning and
private-address rejection; search POST redirects are refused. Leases outlive the
hard worker limit, then expire after six minutes if a worker dies. No request slot
or source transaction is held while awaiting the next durable retry. Database
persistence is briefly serialized across inventory writers to avoid cross-source
job-lock ordering deadlocks; public network work remains concurrent.

Defaults can be configured through `CRAWL_GLOBAL_CONCURRENCY=8`,
`CRAWL_HOST_CONCURRENCY=2`, `CRAWL_HOST_INTERVAL=0`, and
`CRAWL_DISPATCH_BATCH=100`. Positive host spacing uses durable detail checkpoints.
Actual parallelism is also bounded by the ingestion worker count (Compose: two).
Company career-link rediscovery runs weekly; failures retry the following day.

```sh
uv run python manage.py crawl_report --hours 24
```

The JSON summary includes attempts, request counts, total/average duration, queue
delay, complete inventories, partial/deferred batches, failure rate, shadow closures
and pending reviews. Completion fraction is per batch, so a long paginated inventory
naturally has many incomplete batches. Queue delay measures time past the batch's
due time, not broker residence alone. Provider responses and applicant data are not
logged. Use Ingestion runs in admin for individual results.

## Rollout

1. Back up PostgreSQL before applying the fresh initial schema. Schema additions are
   compatible with existing application records. Data backfill consolidates verified
   domains and disables general boards; reversing migrations does not reconstruct
   merged company identities or restore old polling settings. Restore the backup if
   that data transformation must be undone.
2. Rebuild/restart web, control workers, ingestion workers and Beat together. Import a
   small reviewed company cohort; leave closure disabled and inspect source health.
3. Compare two or more complete inventories, shadow closure candidates and known
   employer vacancies. Resolve unsupported/ambiguous endpoints in admin.
4. Enable closure only for validated authoritative endpoints. Expand the catalogue
   while monitoring queue delay, failure rates and provider cooldowns. Increase
   worker counts only within measured provider and resource limits.

The synthetic 10,000-company test validates scheduling and a bounded simulated
collection sample, not a 10,000-company live crawl or a production throughput SLA.


## Job admin filters and appearance

The Jobs list uses Jazzmin with native dark mode and bundled local assets. It needs
no Node.js runtime, npm, build pipeline or Google Fonts connection. Staff MFA and
model permissions still apply. The application frontend keeps its existing design.

Combine **Location contains**, **Company contains**, **Technology**, and lifecycle
filters, then click **Search**. Location/company are case-insensitive text inputs,
so thousands of unique values do not generate giant dropdowns. The general search
box also includes location. Technology values include Python, Go/Golang, Rust,
Node.js/NodeJS and the other normalized technology labels. A GIN index supports
technology containment queries.

Technology defaults to mentions anywhere in the description. Choose **Required
qualifications only** under **Technology match** to restrict matches to recognized
required-qualification sections. Preferred qualifications are not counted as required;
missing or ambiguous sections remain unknown. Tags are deterministic evidence-based
text matching, not a judgment about the candidate or an inferred qualification.

After upgrading existing data, run `python manage.py refresh_job_technologies`
(or `--dry-run`). This refreshes technology tags, including curated normalized and
imported snapshots, without AI calls or replacing descriptions/curated sections.
The command locks bounded batches using the inventory writer's coordination lock.

Jazzmin 3.0.5 has a strict-manifest directory URL compatibility override limited to
`vendor/bootswatch`; missing asset files still raise errors. The script CSP allows
only same-origin scripts and the exact hash of Jazzmin's fixed theme bootstrap.
Filters initialize through an external local script. Inline styles are permitted on
admin routes for Bootstrap/Select2; public routes retain their existing strict policy.
Updating Jazzmin or its default mode requires checking the bootstrap hash; regression
tests validate rendered inline scripts and Chromium checks for CSP violations.

### Persistent company logos

Company logo uploads and enrichment now store normalized PNG bytes in PostgreSQL's
`CompanyLogoBlob` table, separately from company records so list queries do not load
image bytes. Existing file/S3 references still work through the authenticated logo
endpoint. Images retain the existing 2 MiB input / 4 million pixel limits, are resized
to at most 256×256, and have source metadata removed. Database backups include new logos.

After applying migrations, copy surviving legacy files without refetching websites:

```sh
uv run python manage.py cache_company_logos
```

The command is idempotent, retains original files, and reports unreadable/invalid
images. Already missing files cannot be recovered from their database paths; restore
storage from backup or run `enrich_companies --company ID --force` for an already
verified official website.

The local Compose LocalStack storage has no persistent data volume; recreating it
can lose legacy media. Database-backed logos no longer depend on that storage.
Other uploads still use the configured default storage. Do not reverse migration
0007 without exporting database logos first: reversing drops their bytes.
