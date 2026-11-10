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

## Local assisted-browser delivery checkpoint — 2026-11-10

Current work also includes the collection adapters described in
`docs/public-career-adapters.md` and separate fetch/application acceptance criteria
in `docs/company-integration-workflow.md`. Preserve these uncommitted changes.

Local assisted approval/delivery now supports Cato and Camunda in addition to
GitLab. `assist_application` is the common command; the old GitLab command remains
an alias. See `docs/browser-assisted-applications.md` for the implemented scope,
usage, verification and remaining hosted-session work. No live applications,
emails, deployments, runtime source activation or container restarts were performed.

A new PostgreSQL concurrency regression exposed owner-quota row locks blocking
completion audit foreign-key references. Owner coordination in submission, Camunda
and batches now uses `FOR NO KEY UPDATE`, retaining serialization while allowing
those references. Do not revert this to `FOR UPDATE` without rechecking the race.
Existing nullable-company mypy errors and the earlier migration lint error remain
outside this change. No commits have been made.


## Authorized update, restart and commits — 2026-11-10

This checkpoint supersedes earlier statements that changes were uncommitted or
runtime rollout had not happened. The user requested container updates/restarts
and separate task commits. Collection adapters, assisted-browser delivery and
independent readiness criteria were committed separately, followed by rollout notes.

Both application images were rebuilt. All 16 previously running services were
restarted/recreated using `compose.yaml` + `compose.monitoring.yaml`, preserving
volumes and existing configuration. All are running; infrastructure health checks
are healthy. Existing workers and Beat are running again and normal schedules resume.
`jobs.0005_public_career_adapters` applied. Nine reviewed sources are configured but
remain disabled, non-authoritative and closure-disabled; only Flexor is still pending.
Repeated configuration preview reports zero updates. No manual crawl was triggered.

203 combined regression tests passed. Runtime Django checks and migration consistency
passed. Health/login/Grafana return 200, admin redirects to login, all ten Prometheus
targets are up, and sandboxed Chromium starts in the rebuilt browser container on a
synthetic page. There were zero application records at rollout; no real application
or email was sent. Nothing was pushed. Existing unrelated lint/type/query-budget
issues remain documented in the earlier verification notes.

The latest proposal for handing a prefilled page to the user for their own final
Submit is still a proposal. Current local assisted adapters own final submission;
there is no hosted browser stream or automatic tab handoff to a normal user browser.

## Recommendation workflow rollout — 2026-11-10

Implemented persistent recommendation runs over collected jobs: bounded 500-job
screening, up to 20 independent Infinity evaluations, existing AI budgets and
input-key reuse, fenced coordinator leases and owner-level active-run uniqueness.
Both job views now retain low scores and unevaluated jobs, display Good for you
(70–100%) / Bad for you (0–69%), and order evaluated jobs first, newest added first
within each group. Five-second progress polling preserves visible job selections
and unsaved CV choices. Ownership, current-input checks and send approval remain.

Verification: 78 focused backend tests and Chromium running-to-completed polling,
score badges, selection preservation, navigation and mobile checks passed. Changed
Python files passed Ruff; Django checks, migration consistency, JavaScript syntax
and diff whitespace checks passed. No dependencies were changed.

Both local application images were rebuilt. Migration `0006_recommendation_runs`
applied successfully. Web, all Python workers, browser-worker, Beat and the snapshot
exporter were recreated on the new images using the existing monitoring overlay;
all 16 services remain running and infrastructure health checks are healthy.
Health/login and a read-only authenticated recommendation render returned 200;
the running backend is Infinity and the coordinator routes to preparation.

A live run already present during verification screened 500 jobs and selected 20:
1 completed, 16 failed `ai_daily_budget_exceeded`, 1 failed `agent_unsupported_claim`,
and 2 were skipped. These are runtime limits/validation failures, not successful
comparisons. No limits were raised, no failed evaluation was retried, and no search,
application or email was manually triggered for verification. Existing schedules
continue running. See `docs/ai-pilot.md` for search limits, retries and rollback.
