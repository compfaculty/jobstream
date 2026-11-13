# Development

Status: repository commands and contracts reviewed 2026-11-13. Use the root README
for quick start, [architecture](ARCHITECTURE.md) for invariants, and
[operations](OPERATIONS.md) before any side-effectful command. Results belong in
PR/CI evidence; this guide does not accumulate dated test transcripts.

## Environment and quality gates

Python 3.14, uv and locked dependencies; PostgreSQL and Redis are required for
transaction/concurrency tests. Never point tests at production or a valuable runtime
server. Host settings must explicitly target isolated local services. Tests create a
separate database; pytest may need DB creation privileges. Do not substitute SQLite.

```sh
uv sync --frozen
uv run python manage.py collectstatic --noinput
make check
make check-deploy
uv run pytest -m 'not browser' --cov=jobs --cov-report=json --cov-report=term-missing
uv run python scripts/check_coverage.py
uv run playwright install chromium
uv run pytest -m browser
```

`make check` runs Ruff lint/format, the existing authorization/submission mypy scope,
Django checks and migration drift checks. Ruff includes security rules for production
Python. `make check-deploy` uses synthetic secrets/storage and the existing HSTS-preload
exception; it does not validate real infrastructure. CI runs on PR/push, including
Rust, browser, OpenAPI, pip-audit, pinned Gitleaks and image/Cargo Trivy checks.
Required branch protection is a hosting setting, not established by workflow YAML.

Authorization, submission and Gmail services have existing 90% statement/branch
coverage floors (`scripts/check_coverage.py`); keep them without substituting coverage
for meaningful assertions. Test cross-owner access, CSRF, malformed input, attachment/
approval drift, duplicate workers, cancellation, failure/retry/UNKNOWN and provenance.
Use PostgreSQL for leases, constraints, advisory locks and concurrent quotas.

The session-wide Python TCP guard permits literal loopback/Unix sockets only. It does
not sandbox native DB clients, UDP, browsers or Rust/subprocesses. Mock all remote
providers and intercept browser traffic; keep subprocesses on isolated services.
Routine tests must not perform real paid inference, uploads, sends or notifications.

## Synthetic email journey

Use the separate Mailpit stack, isolated PostgreSQL/Redis and installed Chromium:

```sh
make mail-sandbox-up
make mail-sandbox-test
make mail-sandbox-stop
```

Inbox: http://127.0.0.1:18025; SMTP: loopback port 11025. The stack has ephemeral
bounded storage and no relay/forwarding configuration. It does not seed/restart the
normal application stack. The browser test creates fake user/job/PDF data in the test
DB/storage, prepares/approves through the UI, invokes the worker body twice and checks
one receipt plus exact MIME/PDF bytes. It does not prove broker scheduling, Gmail OAuth
or real employer receipt. Captured synthetic mail remains until the sink is recreated.

A manual sandbox must use a separate disposable database/storage/broker. Its bootstrap
requires `DEBUG=1 SANDBOX_MAIL_ENABLED=1` and
`python manage.py bootstrap_mail_sandbox --set-password`; the command never approves,
sends or invokes AI. Only the marked non-staff synthetic account/job and reserved
`applicant@jobstream.invalid`/`recruiter@jobstream.invalid` addresses work. Startup
refuses sandbox mail outside DEBUG. Marked connections never fall back to Gmail.
Do not attach a real CV. SMTP acceptance without an exact verified API receipt is UNKNOWN.

## Rust, browser and monitoring tests

```sh
make infinity-check
make infinity-test
DEBUG=1 OBSERVABILITY_PROCESS_TESTS=1 uv run pytest tests/test_observability_processes.py
make monitoring-check
```

`make infinity-test` builds Rust and runs unit/contract/restart tests with a fake
provider. Process tests require the appropriate local DB permissions to create/remove
temporary restricted roles/schema. Routine subprocess tests are opt-in through
`INFINITY_INTEGRATION=1`; never enable the separate real-LLM paid-test opt-in here.
For a focused fake smoke flow use `make infinity-smoke`. Chromium tests must preserve
sandboxing; install its system dependencies in the browser image/CI as configured.
The vendored seccomp profile allows user-namespace creation/chroot, not privileged
container execution. Monitoring process tests use isolated queues and fake providers.
For a nondefault Redis port, also set `PROBE_BROKER` to an isolated Redis database;
the lifecycle probe does not inherit `REDIS_URL` as its broker setting.

## API and UI conventions

`config/urls.py` and `jobs/api.py` define session-authenticated `/api/v1/jobs/`,
`/applications/` and `/batches/`. `/api/v1/schema/` exposes generated OpenAPI;
`/health/` exposes process liveness only. Validate the schema with:

```sh
uv run python manage.py spectacular --file /tmp/jobstream-openapi.yml --validate --fail-on-warn
```

State changes require POST and Django CSRF protection, including HTMX. Foreign private
objects return 404; unauthenticated session APIs return 403; validation failures return
safe errors. Jobs support `q`, `location` and exact company-profile IDs with bounded
page-number pagination (25 records). Company import/CRUD is staff/admin, not a public
writable API. Draft editing/document upload use the existing HTML forms.

Batch creation accepts owned document, active jobs (maximum 100), UUID `request_key`,
optional profile and `use_ai` (false by default). Reuse request_key for idempotency.
Item polling, selected-item approval and cancellation are owner-scoped. HTML preparation
uses the current base CV; legacy profile/document interfaces remain compatible.
API clients opt into `check_fit`; hard exclusions still apply to older manual flows.
Portal details/consent remain private and require exact per-application review.
Do not invent a broad writable model API or treat batch preparation as send consent.

The current base/application CV editors preserve locked attachments and source facts.
Generation/regeneration clears approval and relevant portal terms review. Prepared
PDFs escape markup, reject unsupported glyphs, use descriptive safe filenames and
cache by owner/input/rendering/font versions. `CV_FONT_PATH` and `CV_BOLD_FONT_PATH`
may select licensed Unicode TTFs; changes must invalidate rendering caches.

Infinity's authenticated internal PUT/GET/DELETE execution interface is separate from
user session routes. Its context endpoint grants one active attempt read capability;
capabilities/credentials are transport data, never model arguments. Django validates
schema, IDs, evidence, current inputs and result bounds. See gateway/runtime code for
wire shapes instead of maintaining a duplicate schema document.

## Adding or changing an integration

Keep existing `Listing`/`CollectionResult`, normalization, provenance, source leases and
lifecycle boundaries. A provider fixture must contain only public permitted fields or
invented data: no widget credentials, applicant answers, tokens or private receipt IDs.
Bound bytes/items/pages, reject changed snapshots and uncertain completeness, preserve
stable provider/board/posting IDs and timestamp meanings. Never follow arbitrary
application/upload URLs or treat a parser success as a permission grant.

Acceptance cases: valid/empty/malformed data, pagination drift/replay, 429/5xx/timeout,
partial scan, duplicate ID, two complete absences, overlapping origins and reopening.
For new permission enforcement test revocation/expiry at queue, page and final commit.
For delivery test owner isolation, exact approval binding, attachment integrity,
unsupported forms, double-worker/retry and UNKNOWN non-replay. Keep network calls
outside long transactions and use persistent state for externally visible effects.

`fixture_sources.py` is an offline contract exercise, disabled in settings and absent
from live registries. Its only grant use is `read_fixture`; tests explicitly enable it.
Run `uv run pytest tests/test_fixture_sources.py` without networking or DB writes.
Production source permissions/standing policies remain roadmap work, not an extension
of the fixture grant. Check rights separately before any live collection or submit.

## Performance, security and documentation changes

Use representative isolated datasets and record hardware, workload, cache state,
query counts, latency distributions and memory before performance claims.
`python manage.py benchmark --jobs 10000 --iterations 100` creates synthetic records
in DEBUG; use a disposable database. `scripts/load_test.py` and `benchmarks/search.js`
are authorized-local/staging load tools, not production capacity evidence. Search
query tests include provenance rendering across page sizes to prevent N+1 behavior.

Run `python scripts/scan_secrets.py` with Docker for the pinned source secret scan.
Dependency/image scanners follow CI; do not weaken gates or hide existing findings.
Never copy session cookies, real CVs, private messages or credentials into fixtures,
logs, public issues or PRs. Uploaded document extraction resource limits and full
account-erasure/retention remain explicit roadmap tasks.

Maintain exactly the six Markdown files listed in the root README. Put actionable
requirements in milestones, current contracts in architecture, procedures in operations
and engineering/testing instructions here. Link to code/configuration instead of
copying implementation listings. Historical transcripts, private operational state and
competitor marketing do not belong in maintained documentation. No new Markdown
archive, implementation handoff or verification report is needed.
