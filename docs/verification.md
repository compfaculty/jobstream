# Local verification — 2026-10-31

Environment: Apple M5 MacBook Pro, 10 CPU cores, 32 GB RAM; Docker Desktop;
Python 3.14.10, Django 5.2.18, PostgreSQL 17 and Redis 7. No production deployment.

## Completed checks

- Pytest: **90 passed**, one third-party deprecation warning. Covers database, authorization, CSRF, malformed inputs, source adapters, OAuth state,
  Gmail lifecycle, upload integrity, duplicate delivery and real two-worker race tests.
- Chromium: login, actual dark stylesheet/HTMX loading, search/save/draft/approval failure,
  desktop visual inspection and mobile overflow check.
- Authorization, submission and Gmail services: 100% statement and branch coverage in
  the combined suite; enforced minimum is 90% each.
- Ruff checks/formatting, mypy on authorization/submission boundaries, Django system
  checks, migration drift and OpenAPI validation.
- Runtime dependency audit: no known vulnerabilities after upgrading cryptography.
- Final application image: no fixable HIGH/CRITICAL vulnerabilities in Trivy scan.
  Unused base-image pip/ensurepip tooling was removed to eliminate bundled findings.
- Source-only Gitleaks scan: no secrets found. Generated `.env` secrets are deliberately
  untracked and excluded from the image; full-directory scans correctly detect them.
- S3 emulator: put/get/delete smoke check and public-access-block configuration verified.
- Running non-root container: health/login/CSS/JS returned 200; Celery worker connected
  to Redis and Beat started. Root filesystem is read-only with bounded writable /tmp.

The GitHub workflow has been authored but has not run on GitHub: no remote, commit or
push was requested. Local equivalent checks were run. One pytest/Playwright dependency
emits a session-fixture deprecation warning; the tests still pass.

## Query/parser baseline

10,000 synthetic jobs, 100 repeated searches, two queries per paginated search:

- Before stored search vector: median 425.77 ms, p95 466.42 ms.
- After stored search vector: median 5.26 ms, p95 6.08 ms.
- Parser/normalizer: approximately 9,602 listings/s on a synthetic 100-entry feed.
- Process maximum RSS: approximately 127 MiB in the optimized benchmark run.

These are warm local query/parser measurements, not remote ingestion throughput or a
production SLA. The generated-vector write/index storage cost is paid during ingestion.

## HTTP baseline

Two Gunicorn workers, 10,000 synthetic jobs, Docker-local HTTP, 30 virtual users at
peak with one-second think time, two-minute ramp (k6 2.3.0):

- 1,760 authenticated searches, 100% successful, no HTTP failures.
- Median 19.50 ms, p95 29.26 ms, maximum 40.15 ms.
- Average throughput 14.58 requests/s across ramp-up and ramp-down.

The first attempted load run used a mismatched development signing key and measured
only authentication failures; it was discarded. The harness now requires the shared
environment and checks authenticated access before starting k6. The successful run
above is a local baseline, not a saturation test or a 50,000-DAU certification.
Temporary benchmark users/sessions and synthetic jobs were removed afterward.

## Not verified externally

Real Google consent, send and sent-mail reconciliation require OAuth credentials and
dedicated test accounts. Live job-source compatibility, browser portal submission,
managed backup recovery, Cloudflare policy, production alert delivery, hosted CI and
50,000-DAU capacity remain unverified. Read the documented gates before public launch.

## MFA QR contrast follow-up

Authenticator QR images now have an explicit white background and 16 px padding on
the dark UI. A focused Chromium setup test verified the image loads and the computed
background/padding are correct. The web container clock matched the host within one
second; authenticator enrollment data and TOTP validation settings were unchanged.

## MFA enrollment diagnosis

A full Chromium enrollment test calculated RFC 6238 SHA-1 / 30-second / six-digit
TOTP independently from the test account's displayed URI and completed enrollment.
An external HTTPS Date reference matched host time within one second. No real user
secrets were read. The upstream wizard resets its pending secret on a fresh GET;
setup now explains that refreshed/restarted enrollment must be scanned again.

The follow-up QR integration test also intercepted the disposable account's actual
QR encoder input, confirmed it equals the displayed enrollment URI, independently
calculated its code, and completed setup. All four targeted MFA/browser checks passed.
This rules out a general QR-payload/validator mismatch, but does not establish the
cause of a particular phone's rejected code. Account type and phone time need confirmation.


## Confirmed staff MFA bug and fix

A staff-specific regression reproduced the failure: requesting `/favicon.ico` during
enrollment followed the privileged-MFA redirect into SetupView. That GET reset the
wizard, so the code generated from the displayed QR no longer matched the pending key.
The earlier generic-user test did not exercise the staff middleware.

Favicon now returns 204 directly. Background asset/API and unsafe-method requests from
unverified staff return 403 rather than redirecting into setup. Protected document
navigation still requires MFA. A regression checks favicon, image and API requests
between QR generation and successful standard-code verification. No real account
secrets or validation tolerance were changed.

After the staff-MFA fix, the full suite passed: 75 tests, with 100% middleware
statement/branch coverage. Ruff, typing, Django checks and migration drift checks passed.


## Legacy source import — 2026-10-31

Imported 51 disabled configurations and preserved both existing enabled sources and
their settings, for 53 total. The catalog contains six RSS feeds, two boards, and
45 company pages; 40 company pages are JSON-LD reference configurations requiring
compatibility review. Seeder dry-run/idempotency/operator-preservation tests pass.

Bounded read-only checks returned 25 items per DOU category, 200 Jobicy entries and
122 Real Work From Anywhere entries. The latter feed has XML irregularities but
feedparser extracted its entries. Sampled AI21, AudioCodes and Real Work From Anywhere
details parsed, and Akeyless/Comeet embedded JSON yielded a 3,005-character description
with a location. Legacy 40Seas and Agrematch URLs returned HTTP 404. These are point-in-time
checks, not a guarantee of every listing or the remaining company sites.

Full regression suite: 90 passed. Added checks cover namespaced RSS fields, RSS/Atom
dates, company-link restrictions, synthetic inline-job identities, non-executable
Comeet JSON extraction, bounded detail requests and RSS fallback counters. Source
secret scanning found no leaks.

## Employer sourcing and batch applications follow-up

- Full suite: **161 passed**, including Chromium profile review, batch selection,
  automatic progress polling with checkbox retention, and a real two-worker
  preparation race. One existing third-party fixture deprecation warning remains.
- Required trust-boundary coverage gates, lint/format checks, mypy and OpenAPI
  validation passed. Source-only Gitleaks found no secrets.
- Runtime dependency audit initially flagged Pillow 12.3.0. Upgraded to 13.0.0,
  verified image/PDF behavior, and reran the audit: no known vulnerabilities found.
  Added an explicit regression check for dropping source image metadata.
- Applied migrations through 0024 to the local database. Verified four live Celery
  queue subscriptions (control, ingestion, preparation, delivery), Django checks,
  health endpoint and private PDF storage put/get/delete with a synthetic object.
- GitLab careers HTML references Greenhouse. A bounded read-only endpoint check
  returned 238 jobs and matching total metadata with full descriptions. Its source
  reference remains disabled. Source proposals discovered from links also remain
  disabled and pending review.
- Secret Tel Aviv public application form inspected read-only; authorized test URL
  still required for submission/upload/confirmation validation. No real application,
  email or live OpenAI request was sent during these checks.

- Final image scan after the Pillow update: zero fixable HIGH/CRITICAL findings.
- Retry-After handling accepts delta seconds and HTTP dates for Gmail and OpenAI;
  extreme provider pauses require review rather than overflowing datetime arithmetic
  or retrying before the requested deadline.

## Secret Tel Aviv read-only preflight follow-up

Added an operator preflight command and eight tests covering form recognition,
changed fields/actions, hostile destinations, tokenized URLs, consent defaults,
challenge indicators and missing/ambiguous forms. Live read-only inspection matched
the known public form and found no structural warnings; submission remained disabled.
Full suite now **169 passed**, with the same third-party deprecation warning. Required
coverage gates, lint/format, mypy and Django checks passed. No upload or submission
endpoint was called. The authorized test-vacancy URL and synthetic-applicant
constraints remain pending; the inspected public vacancy is not a submission target.

## Native Secret Tel Aviv submission follow-up

- Ported the old Go native multipart flow, with exact-content approval, owned CV
  integrity checks, ephemeral cookies and pinned public HTTPS connections.
- Full suite: **194 passed**, including browser applicant entry/review/approval
  without Gmail, multipart upload, native receipt redirects, duplicate protection,
  429 schedules and shared cooldowns, and uncertain-outcome handling. Required
  coverage, lint/format, mypy, Django checks and OpenAPI validation passed.
- The user authorized up to two live tests. Exactly two POSTs used a visibly labeled
  integration-test PDF, synthetic contact details and no headhunter forwarding.
  The first produced a redirect and was not repeated. The second produced native
  receipt 117950 and an explicit application-sent notice. The resulting empty-error
  placeholder and receipt path are now covered by regression tests. A later anonymous
  GET did not repeat the one-time notice; it was not treated as a fresh confirmation.
- No real candidate credentials, CV or qualifications were used in the live tests.
  No third submission was made. Other portal integrations remain manual.
