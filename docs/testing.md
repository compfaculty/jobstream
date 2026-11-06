# Testing and performance

Use real PostgreSQL/Redis locally and in CI. Tests create `test_jobstream`; never point
tests at a production server. Gmail provider calls are mocked and never send mail.

```sh
make services
uv run python manage.py collectstatic --noinput
uv run pytest -m 'not browser' --cov=jobs --cov-report=json --cov-report=term-missing
uv run python scripts/check_coverage.py
uv run playwright install chromium
uv run pytest -m browser
```

Statement **and branch** coverage must each reach 90% for authorization, submission
and Gmail transport services. Coverage is a floor, not a replacement for assertions.
A real PostgreSQL two-worker race test verifies one send; other tests cover user
isolation, CSRF, source failures, malicious URLs, attachment drift, OAuth failures and
unknown-state reconciliation. Browser tests exercise login, HTMX search, save, draft
creation and an approval failure, plus a mobile-width overflow check.

S3 smoke testing: start Compose, configure S3_BUCKET=jobstream-private,
S3_ENDPOINT_URL=http://localhost:4566, AWS_ACCESS_KEY_ID=local-storage,
AWS_SECRET_ACCESS_KEY=local-storage-password. Upload/download using an authenticated
account. Validate real production bucket/IAM policies separately.

## Benchmarks

```sh
uv run python manage.py benchmark --jobs 10000 --iterations 100
```

This adds synthetic example.invalid jobs in DEBUG mode, then reports p50/p95 search
latency, query count, parser throughput and process RSS. It does not benchmark remote
sources or prove production capacity. Remove synthetic rows when finished:

```sh
uv run python manage.py shell -c 'from jobs.models import Job; Job.objects.filter(canonical_url__startswith="https://benchmark.example.invalid/").delete()'
```

For HTTP concurrency, use k6 (native install or its container) against a dedicated local
or explicitly authorized staging environment. Provide BASE_URL and SESSION_ID via your
shell environment and run `k6 run benchmarks/search.js`. Do not put session IDs into Git.
With the same `.env` exported as the running Compose stack,
`uv run python scripts/load_test.py`
creates a temporary account/session, runs k6 in Docker, then deletes both. It never
prints the session cookie.

The script ramps to 30 virtual users, records HTTP percentiles/throughput/errors, and
checks authentication; it cannot certify 50,000 DAU. Record dataset, hardware, worker
counts, concurrency, payload sizes, source workload and warm/cold cache conditions.

Before production, run mixed search/detail/save workloads alongside ingestion, inspect
PostgreSQL plans and worker memory, and agree latency/RPO/RTO budgets. Set latency gates
from those measurements. Do not choose an arbitrary p95 as a capacity claim.

## Repository quality gates

`make check` runs Ruff lint/format, the existing two-service mypy scope, Django
checks and migration drift checks. Ruff's Bandit-derived `S` rules now cover `jobs`
and `config`; tests (synthetic credentials/assertions) and operational scripts are
outside this initial SAST scope. Script subprocess/SQL findings require a separate
review; they are not claimed clean. Inline suppressions in application code explain
the individual false positives (DEBUG-only key, public hostname, validated URL and
non-security retry jitter). Maintainers must review these explanations when changing
those lines. Existing 90% critical-service branch/statement gates remain unchanged.

`make check-deploy` runs Django deployment checks with DEBUG disabled and ephemeral
synthetic secrets/storage configuration. It makes no provider, database or storage
requests. Warnings fail except `security.W021`: HSTS preload requires a separate
operator/domain decision. This exception belongs to the synthetic check only;
production settings are not silenced. This does not verify proxy headers, TLS,
bucket IAM, secret rotation, origin access or backups.

CI scans the web, browser and Infinity images with the existing Trivy action and
separately scans Infinity's Cargo lockfile for high/critical advisories, including
unfixed advisories. Image scans retain their existing fixed-vulnerability policy.
The source secret scan runs before image/scanner tooling creates caches and uses
`scripts/scan_secrets.py` to include tracked and unignored project files. Generated
vulnerability databases are not project source and must not be scanned as such.
A configured workflow is not evidence that its current run passed. Repository
administrators must configure required status checks in branch protection separately.

The session-wide Python TCP guard permits literal loopback and Unix sockets only.
Use local PostgreSQL/Redis and fake providers. Direct hostname connections are denied;
normal clients resolving localhost to loopback continue to work. The guard also
covers the Django live-server thread. It does not sandbox native database clients,
UDP, browsers, Rust or other subprocesses. Those retain isolated test configuration;
real-provider subprocess tests remain explicit paid opt-ins, never routine CI.

The pilot benchmark defaults to a unique, owner-only temporary file and reports its
path as `report_path` in the summary JSON. This works with a read-only container
working directory and avoids a predictable shared `/tmp` file. Explicit `--report`
paths retain their existing behavior and must be private, trusted locations.

## Remaining adoption work

Do not equate these gates with complete production readiness. Maintainers still
need to review operational scripts under SAST, expand types incrementally, bound
adversarial PDF extraction resources, and define retention/account-erasure across
application storage, Infinity history, telemetry and backups. Actual deployment
IAM/proxy/restore verification requires an isolated environment. AI quality evidence
in `ai-pilot-verification.json` remains historical and incomplete; do not change
rollout flags or budgets to make validation pass. Fix report denominator semantics
before using that artifact as a rollout decision. None of these pending items is a
scanner exemption for new application vulnerabilities.
