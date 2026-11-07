# Current work: observability planning

The next requested feature is self-hosted Prometheus and Grafana for Django,
Rust Infinity, outbound requests/traffic, and Celery, plus Loki/Alloy structured
logging and correlated investigation views. The implementation handoff,
scope, security controls, verification, and next-chat prompt are in
`docs/observability-plan.md`. This is a plan only; monitoring is not implemented.
Preserve current database/runtime data. Historical reset authorization below does
not authorize another reset.

## Previous work: clean development reset

User explicitly authorized replacing the local development database and migration
history, then clarified that the existing superuser login and enrolled MFA must be
preserved. All other project container/volume data may be discarded. Do not reset
other Docker projects, source code or `.env`.

Current data model and bootstrap: `docs/data-model.md`.

- One fresh `jobs/0001_initial.py` replaces the former migration chain.
- Company.source is removed; Source.kind separates company careers from boards.
- Career jobs use their Source.company. Boards never create/link Companies.
- Arbeitnow/Himalayas are board ingestion providers, not company imports.
- `bootstrap_development` is idempotent and starts sources/discovery disabled.
- Retain existing submission approval, CV truthfulness, ownership and CAPTCHA boundaries.
- No live applications/emails, paid calls or production deployment are authorized.

Read Git status and actual runtime state before continuing; do not treat this note
as evidence that a reset, migration, restoration, rebuild or validation has completed.
Historical integration notes remain useful for provider mechanisms, not migration
instructions or claims about the new database's contents.

## Verified completion checkpoint — 2026-11-07

The local reset and authentication restore completed. All Compose services, including
Infinity, are running; db/redis/storage report healthy. Local /health/ and login return
200, and /admin/ routes to login. Runtime database has only jobs.0001_initial applied
for this app; one superuser and one confirmed TOTP device remain. Counts: 4 companies,
7 disabled sources (4 company career endpoints, 3 boards), 1 disabled company directory
campaign; 0 jobs/applications/documents/mail connections/ordinary users. Temporary
superuser/MFA export was removed after exact field verification.

The full non-browser suite passed 669 tests (9 skipped), and final source/view checks
passed 28 tests. make check and Gitleaks passed. Inspect Git status for any changes after this checkpoint.
No crawls, paid calls, applications or emails were triggered by bootstrap/reset.

## Observability implementation checkpoint — 2026-11-07

The planning header above is historical. Monitoring implementation is now in the
working tree: `compose.monitoring.yaml`, `jobs/observability/`, Rust instrumentation,
and four provisioned dashboards under `deploy/monitoring/`. The runbook and detailed
verification evidence are appended to `docs/operations.md`. Preserve the existing
handoff/plan edits and all database/runtime data. No changes have been committed.

Grafana is localhost-only on port 3000; admin credentials are in the restricted,
ignored `.monitoring-secrets/` directory. Web and monitoring were enabled for safe
HTTP/log verification. Collection/delivery workers and Beat were left stopped;
`make monitoring-up` resumes normal scheduled work and must be used intentionally.
The existing live Infinity container was not rebuilt; its telemetry code is tested
using the fake provider and can be applied at a safe restart boundary. Privileged
infrastructure exporters and external alert delivery remain deferred.

One pre-existing full-suite failure was reproduced from clean HEAD:
`test_search_query_budget` expects two queries but the existing origins prefetch
performs three. Do not conceal that failure by weakening the query assertion.
Read the current runtime/Git state before resuming; this note is not permission to
reset data, crawl sources, send applications/emails, invoke paid APIs, or deploy.

Final verification: 708 non-browser tests passed, one opt-in real-provider test
skipped, and the single pre-existing query-budget failure remained. Security-sensitive
coverage gates passed; 14 Cargo tests, fmt/clippy, make check/check-deploy,
monitoring config/alert tests, locked dependency audit, and Gitleaks passed.
