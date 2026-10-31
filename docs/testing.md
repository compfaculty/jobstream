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
