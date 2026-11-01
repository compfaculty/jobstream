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

## Company-first catalogue — 2026-10-31

- Full suite: **218 passed** in 40.51 seconds, including six browser tests. One existing
  pytest/Playwright fixture deprecation warning remains. A subsequently added admin
  HTTP check also passed: MFA-verified staff could render job list/add/edit, company
  list/add, source add and CSV import pages, and import an unverified company via upload.
  That check emits Django 5.2 URLField scheme deprecation warnings.
- Ruff check/format, trust-boundary mypy, Django checks, migration drift, OpenAPI
  validation with warnings treated as errors, and the existing >=90% statement/branch
  coverage gates passed. No dependencies changed.
- Deterministic tests cover unverified import separation, verified-domain merges,
  preserved application references, endpoint evidence and operator disables,
  two-check closure/shadow mode, endpoint overlap, explicit filled status,
  reopening, curated content, scope changes, failed/partial inventories,
  interrupted workers, concurrent source leases, request limits and shared 429 cooldowns.
- Greenhouse processing imported **501 jobs in three bounded batches**. Workday
  fixtures imported **501 jobs in 51 batches** using 51 read-only search POSTs and
  501 detail GETs. Detail checkpoints resumed after both capacity deferral and 429
  without replaying the search or previously completed detail requests.
- Synthetic PostgreSQL benchmark: **10,000 companies/endpoints**, all dispatched
  exactly once in **1.113 seconds** across 100 scheduler invocations; setup plus a
  100-run simulated collection sample took **4.279 seconds**. Average simulated run:
  **0.0133 seconds**. The sample had **100% completion**, **0% failure**, and **zero
  network requests**. Average recorded due-time delay was **3603.187 seconds** because
  the fixture intentionally made every endpoint one hour overdue. This measures
  scheduler/database behavior, not live crawling capacity. At the normal one-minute
  Beat cadence, 100 scheduler batches span approximately 100 minutes, not 1.113 seconds.
- Actual adapter smoke checks were read-only and did not store live postings:
  NVIDIA Workday returned ten postings, a continuation at offset ten / reported total
  2,000, using eleven requests in **10.49 seconds**; GitLab Greenhouse returned a
  complete inventory of **238 jobs** in **1.20 seconds**. No applications were submitted.
- Local rollout: private PostgreSQL backup taken with mode 0600 at
  `/tmp/jobstream-company-first-pre-rollout-20261031.dump`; migrations **0027–0031**
  applied; web, control, ingestion, preparation, delivery and Beat containers rebuilt
  or restarted. All nine Compose services are running. `/health/` returned 200;
  unauthenticated `/admin/jobs/job/` returned the expected 302 to authentication.
  The database has **111 companies**, **three verified domains**, **two applications**,
  **zero queued/submitting applications**, **zero enabled general boards**, **one
  enabled Secret Tel Aviv source**, and **zero closure-enabled sources**.

Collection metrics on pre-migration IngestionRun rows default to zero; historical
rows do not establish new request/duration/completeness measurements. Use newly
collected runs when evaluating the rollout cohort. Unsupported custom/JavaScript
sites remain for review. Closure must be enabled explicitly after shadow validation.

## Admin filters and Jazzmin — 2026-10-31

- Full suite: **226 passed** in 43.93 seconds, including Chromium interaction with
  Jazzmin's visible technology selector, combined location/company filtering, and
  the job edit page. Existing Django URLField and pytest fixture deprecations remain.
- Admin filters combine case-insensitive location/company text, normalized technology
  mentions and required-qualification-only technology tags. Node.js/NodeJS recognition
  and required/preferred separation are deterministic. Migration 0032 adds the JSON
  containment GIN index; 0033 corrects the Companies label.
- Installed **django-jazzmin 3.0.5** with native dark mode. No Node.js/npm/build tooling
  was added. Visual inspection used `/tmp/jobstream-admin-refresh.png`. Browser tests
  reported no JS errors or CSP violations. Theme assets/fonts are served locally.
- Strict manifest compatibility is restricted to Jazzmin's Bootswatch directory URL;
  a negative test confirms missing files still fail. Tests verify exact inline-script
  hashes, preserved MFA enforcement, admin permissions and CSV import pages.
- Ruff, formatting, trust-boundary mypy, Django checks, migration drift, OpenAPI and
  existing coverage gates passed. The locked production dependency audit reported
  **no known vulnerabilities** after adding Jazzmin.
- Rebuilt and restarted the local containers, applied migrations 0032–0033, and
  refreshed technology tags on **640 existing jobs** without AI calls. A private
  pre-rollout backup is at `/tmp/jobstream-admin-refresh-rollout-20261031.dump`.

## Matching and My preferences — 2026-11-01

- Checkpoint commit **05fccbf** recorded the preceding company-first/admin work
  after 226 passing tests. Cato's verified Greenhouse source then imported **106
  postings**, including **32 Israeli locations**, with no rejections. Application
  browser execution remains in progress; no Cato application or CV was submitted.
- Added per-user mandatory technology exclusions with explicit OR alternatives,
  preferred-only handling, one-overlap CV eligibility and explainable skips/pauses.
  Matching uses confirmed profile facts or extracted PDF text without inventing facts.
  New HTML batches enable fit checks by default; API clients opt in with `check_fit`.
- Expanded `/preferences/` into **My preferences**: locations, titles, wanted/excluded
  technologies, explicit-only limits, remote-only selection and saved application
  answers. Defaults are soft. Unknown remote eligibility pauses. Preference limits
  are rechecked during approval and queued-send preflight.
- Common answers use owner-bound Fernet encryption with the existing configured
  private-data key. Saving and reuse are explicit separate choices; records can be
  edited/deleted. Tests cover cross-owner ciphertext copying, missing keys, owner
  isolation and contact prefill without accepting terms or headhunter forwarding.
  Gender answers are optional, never inferred and not used for job matching or AI.
- Contact prefill currently targets Secret Tel Aviv. Notice-period, location,
  LinkedIn, authorization notes and demographic values await compatible per-portal
  mappings. Saved answers are not a blanket permission to submit applications.
- Final full suite: **258 passed** in **46.98 seconds**, including Chromium saving
  preferences, editing/deleting encrypted answers and checking the mobile layout.
  Existing Django/pytest deprecation warnings remain. Desktop/mobile screenshots:
  `/tmp/jobstream-my-preferences-desktop.png`, `/tmp/jobstream-my-preferences-mobile.png`.
  Ruff, formatting, mypy, Django/OpenAPI checks, migration drift and existing
  statement/branch coverage gates passed.
- Local application image rebuilt. Migrations 0034 and 0035 add matching preferences
  and encrypted saved answers. Private pre-rollout backups were taken in `/tmp` with
  mode 0600. No encryption key values or actual applicant data were printed.

The Cato tranche remains uncommitted until its application integration is complete;
accepted workflow and current gaps are recorded in `company-integration-workflow.md`
and `integrations/cato-networks.md`.

## Cato Networks hosted application delivery — 2026-11-01

- Added strict verified-Cato dispatch, public question-schema fingerprinting, contact/
  location validation, owner-bound draft review and approval, and opt-in equivalent
  saved-answer prefill. Unknown required questions/consents require review.
- Added Python Playwright UI delivery on its own queue/container, sandboxed Chromium,
  pinned public origins, default-deny requests, unique city matching, confirmed CV
  upload and one application POST. A successful response plus the matching official
  confirmation route is required; uncertain outcomes never replay automatically.
- Separate Cato provider spacing/cooldown preserves Secret Tel Aviv behavior.
- **282 tests passed** in 49.98 seconds, with 17 existing warnings. This includes
  intercepted Chromium success/429/rejection/uncertainty/challenge/city fixtures,
  approval drift, batch contact prefill and dedicated task routing.
- Ruff lint/format, trust-boundary mypy, Django system checks, OpenAPI validation,
  >=90% statement/branch coverage gates and source secret scanning passed.
- The read-only diagnostic for posting `4937879101` returned supported schema,
  expected controls and Submit button, and no visible interactive challenge. Chromium
  launched with its sandbox inside the non-root, read-only browser container.
- No live application or CV upload was made by the agent. User CV selection, answer
  review, explicit approval and employer receipt verification remain the live test.

### Cato first user-triggered attempt: hydration correction

The user's first approval did not yield a confirmed application: application 3 was
FAILED with `cato_network_or_form_changed`. The user confirmed no employer email was
received. Read-only reproduction found the country dropdown's first click occurred
before React attached its handlers; a later harmless click worked. The original
static-controls diagnostic did not test interactivity and missed this condition.

The transport now proves dropdown interactivity before entering contact details,
retries only that harmless readiness operation, and reports pre-submit failures by
stage. The read-only diagnostic checks interactivity too. A real Chromium regression
fixture delays event-handler attachment. No application was resent during debugging;
a fresh user review/approval and employer confirmation remain required.

## Camunda Ashby adapter — 2026-11-01

- Confirmed the official careers page links to Camunda's Ashby board. Existing source
  56 already collects the selected Senior Manager, QA Engineering posting as job 20370.
- Added dynamic draft questions from the public read-only job schema, reviewed public
  schema snapshots, owner/CV approval integrity and browser-queue routing. New shared
  Preferences fields remain deferred; production answers/consents have no fake defaults.
- Added isolated normal-browser answer saves, pinned upload-handle/file transport,
  successful resume-binding checks and exactly one final Submit. This posting uses
  `ApiSubmitMultipleFormsAction`; both application and consent-survey results must be
  explicit successes. Partial/unknown results never automatically replay.
- Tests cover semantic fingerprints across regenerated render IDs, changed consent,
  escaped provider help text, explicit yes/no choices, queued/uncertain/manual quota
  history, concurrent quota admission, upload origins, 429 pauses during answer saves
  and submission, malformed responses, provider blocks and partial survey failures.
- Thirty targeted schema/approval/browser checks passed after the final combined-form
  protocol correction. Prior full-suite checks passed 311 tests before that correction;
  final full-suite results are checked again for rollout.
- Live read-only browser check confirmed all 22 fields and city autocomplete, including
  `Tel Aviv, Tel Aviv District, Israel`, with schema agreement across HTTP/browser reads.
  All form writes, CV uploads and submissions were blocked during that check.
- Ruff, trust-boundary mypy, Django checks, OpenAPI validation and source secret scan
  passed. No live application was sent. User-selected CV/answers and explicit approval
  followed by an employer receipt remain the end-to-end validation boundary.

Camunda final verification: **313 full-suite tests passed** in 61.73 seconds with
17 existing warnings; statement/branch coverage gates passed. After adding clearing
of old answers/consent on schema changes, **43 focused adapter/browser/preferences
checks passed**. Lint/format, mypy, Django/OpenAPI checks and diff validation passed.
Live application submission remains untested; synthetic answers are confined to tests.

The final 43 focused checks also passed with regenerated survey storage IDs excluded
from semantic fingerprints; question paths/text/choices and consent remain bound.

### Authorized live verification — 2026-11-01

The owner authorized real submissions for applications 3 and 4. Camunda application
4 is now confirmed/applied: Ashby returned explicit application and consent-survey
success after human verification in visible local Chromium. It must not be resent.
Live fixes cover short-city matching, nested phone/SMS controls, and upload-handle
timing/canonicalization before final submission.

The final full suite passed **321 tests** with 17 existing warnings; coverage gates,
Ruff, trust-boundary mypy and source secret scanning passed. Hosted human-verification handoff remains
unimplemented; today's visible-browser hook is for local assisted debugging.
