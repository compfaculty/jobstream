# Self-hosted Prometheus, Grafana, and logging — implementation handoff

Status: planned, not implemented. Prepared 2026-11-07.

## Goal and boundaries

Provide live and historical visibility into Django, the Rust Infinity runtime,
outbound collection/provider requests, and Celery. Keep `/operations/` for business
records, review actions, and execution details; add Grafana for rates, distributions,
trends, and infrastructure health.

Include centralized investigative logging with Loki and Grafana Alloy, as detailed
below. Metrics show when/where a problem occurs; correlated events explain the
sequence of decisions, retries, and outcomes.

Use free, self-hosted software. No paid services, registration, database reset,
production deployment, real applications, emails, or paid AI calls are authorized.
Preserve company/source/job semantics, existing data, authentication, MFA, and
application approval boundaries. No application schema migrations are expected.

## Verified starting points

- `compose.yaml`: two Gunicorn workers; separate prefork Celery services for control,
  ingestion, preparation, delivery, and browser delivery; separate Beat and optional
  `infinity` profile. App containers have read-only roots and bounded tmpfs.
- `jobs/services/operations.py`: periodic infrastructure snapshot; dashboard reads
  stored data rather than probing dependencies on every request.
- `jobs/task_telemetry.py`: task start/finish signals and scheduler publication
  marker; logs metadata, never task arguments or results.
- `static/operations.js`: ten-second full-page reload, paused while editing.
- `jobs/apps.py`: optional Django OpenTelemetry tracing already exists. Do not
  activate another tracing backend or duplicate instrumentation in this work.
- `services/infinity-agent/src/main.rs`: Axum execution/attempt routes and heartbeat.
- `services/infinity-agent/src/telemetry.rs`: provider call outcomes, latency, token
  usage, and metadata persistence; no prompts or response bodies.
- No Prometheus or Grafana service currently exists. Recheck Git and runtime state
  at implementation time; this document is not evidence of runtime health.

## 1. Monitoring deployment

Add an opt-in `monitoring` Compose profile with pinned Prometheus and Grafana images,
named persistent volumes, resource limits, health checks, least privileges, and
read-only provisioned configuration. Select and verify supported image versions
and digests during implementation; do not use `latest`.

Start with 15-second scrapes, 15-second dashboard refresh, seven-day retention and
a 2 GiB Prometheus retention-size cap. Set explicit container memory limits and
measure actual usage before finalizing them. Document that retention is bounded
by whichever limit is reached first and volumes still need disk headroom.

Grafana binds only `127.0.0.1:3000`. Prometheus and metric listeners have no published
host ports by default. Grafana uses its own local login; Django MFA does not
automatically protect it. Disable anonymous access and signup. Generate an initial
admin secret into an ignored, restricted local secret file, mount it using the
supported file-based setting, and document rotation without printing passwords.

Use a dedicated monitoring network. Restrict metric listeners to intended peers
and enforce a dedicated scrape credential where network isolation alone does not
exclude other application containers. Never reuse Infinity's service credential.
Keep metric routes outside public Django URL routing and existing user middleware;
use a separate internal listener for exposition.

Provision the datasource and dashboard JSON from versioned files under
`deploy/monitoring/`. Do not require manual dashboard setup or download arbitrary
community dashboards/plugins. Provide Make targets and documented start/stop
commands that leave application volumes intact.

## 2. Metric collection and multiprocess correctness

Use the official Python Prometheus client and a maintained Rust client compatible
with the existing Axum stack. Lock dependencies and document their purpose.

Python instrumentation must work across Gunicorn workers and Celery prefork
children. Use per-container multiprocess files on bounded tmpfs, configured before
Python starts, with one internal exposition process per instrumented container.
Never share that directory across containers. Initialize/clear it once per fresh
container startup, never from a child worker or HTTP request. Implement Gunicorn
and Celery child-death hooks and verify gauge cleanup and worker replacement.
Review official multiprocess restrictions before choosing metric types.

Keep custom snapshot/business collectors in a separate registry/listener from the
multiprocess registry. Add a lightweight internal exporter that reads the existing
snapshot and bounded aggregate queries on a short cache interval. Scrapes must not
invoke Celery inspection, external requests, unbounded queries, or new ingestion.
Expose snapshot timestamps and collection failure states so stale data is visible.
Do not make Redis increments or synchronous monitoring network calls part of every
application request. Instrumentation failures must not break business operations.

Rust metrics are process-local atomics/histograms, exposed through a separate
internal listener. HTTP and provider instrumentation must account for completion,
failure, timeout, and cancellation without holding locks across network calls.

## 3. Instrumentation contract

Use seconds and bytes as base units, `_total` counters, bounded histogram buckets,
and a `jobstream_` namespace. Declare labels centrally and initialize known series
where practical. Record the scope and reset semantics of each metric.

### Django HTTP

- Request counter by normalized route name, allowlisted HTTP method, and status.
- Duration histogram and in-progress gauge, including exception paths.
- Response payload bytes where observable; do not consume or buffer streaming
  responses to measure them. Mark unknown sizes instead of treating them as zero.
- Collapse unknown routes to a fixed label. Exclude monitoring scrapes from normal
  application traffic panels; show liveness probes separately.

### Outbound HTTP and collection

- Attempts, duration, status class, categorized failure, retry, and throttle counts.
- Actual observed body bytes at existing bounded fetch/read points; distinguish
  downloaded payload bytes from compressed wire bytes and unknown sizes.
- Fixed integration labels such as Brave, Pituchim, Arbeitnow, Himalayas, supported
  ATS adapters, Django-to-Infinity, and AI provider; all other hosts map to `other`.
- Instrument existing safe fetch/crawl and provider boundaries, including robots
  requests and failed attempts. Audit direct HTTP call sites to document coverage
  gaps. Preserve SSRF/DNS pinning, redirect checks, budgets, timeouts, and size caps.
- Count retry attempts separately from logical operations. Browser-managed traffic
  and SMTP are outside HTTP payload coverage unless explicitly instrumented later.

### Infinity

- Inbound route request counts, duration, status, and in-progress requests.
- Active evaluations, attempts started/completed/failed/cancelled, and recovery.
- Outbound AI attempts, latency, categorized errors, throttling, and known token
  totals using an allowlisted model label. Track unknown usage separately; it must
  not imply zero tokens. Do not invent token counts or monetary estimates.
- Preserve current metadata records for per-execution diagnosis; metric labels
  must never contain execution IDs, attempt IDs, or provider request IDs.

### Celery and business progress

- Task starts/completions/failures/retries, duration, and running tasks by fixed
  task name and queue. Verify retry semantics against actual Celery signal order.
- Queue depth, consumer availability, scheduler publication age, snapshot age,
  and probe failures from the existing operational snapshot.
- Bounded aggregate gauges for pending company reviews, pending source reviews,
  enabled sources, and ingestion outcomes/new jobs. Define these precisely before
  implementation: do not describe stock counts as import-rate counters.
- Keep job collection, normalization, evaluation, preparation, and delivery distinct.
  Reading dashboards must never trigger these workflows.

## 4. Dashboards and alerts

Provision three dashboards with stable UIDs and useful defaults:

1. **Overview:** target health, request/collection rates, errors, p95 latency, worker
   backlog, snapshot freshness, and company/job review backlog.
2. **Django and collection:** route rates/statuses, p50/p95/p99, in-flight requests,
   observed payload bytes, provider failures/retries/throttling, queue/task trends.
3. **Infinity:** active evaluations, attempt outcomes, inbound and provider latency,
   errors/cancellations, known tokens and unknown-usage counts.

Provide time-range/service/queue/provider selectors using bounded labels. Label
payload charts accurately; do not call them total network traffic. Show no-data,
stale, disabled-service, and unhealthy states separately; optional Infinity being
disabled should not raise an incident. Percentiles need sufficient observations;
empty panels must not show fabricated zero latency.

Add local Prometheus alert rules for sustained target loss, stale snapshots,
scheduler/consumer loss, sustained error rate with a minimum traffic threshold,
and backlog growth. Validate rules and test firing/recovery. No external alert
receivers or notifications are enabled in this scope.

Add a staff-visible configured Grafana link to `/operations/`, retaining the existing
staff/MFA protection. No iframe, public embedding, shared tokens in URLs, or Grafana
dependency for loading Django pages.

## 5. Optional infrastructure metrics

Deliver application metrics first. Then offer a separate infrastructure profile
for container CPU, memory, and network receive/transmit bytes, plus deeper
PostgreSQL/Redis statistics if useful.

Evaluate Docker Desktop compatibility before selecting cAdvisor or another
container exporter. Document host mounts, privileges, VM-versus-host scope, and
overhead. Do not mount the Docker socket or grant broad host privileges silently;
a read-only socket mount does not provide a read-only Docker API. If a privileged
exporter is needed, present that concrete configuration for approval and keep it
disabled until approved. Show unsupported metrics as unavailable.

Database exporters, if added, use dedicated least-privilege monitoring credentials,
not application/superuser credentials. No SQL text, query parameters, user data, or
per-object labels. Core delivery does not depend on these optional exporters.

## 5a. Structured logging and investigations

### Collection and storage

Add pinned single-instance Loki and Alloy services to the monitoring profile and
provision a Loki datasource in Grafana. Loki is internal only: it does not inherit
Grafana authentication. Protect ingestion/query endpoints with network isolation
and dedicated authentication where untrusted containers can reach them. Persist
Loki data and Alloy read positions; configure and test seven-day deletion through
the supported retention/compactor settings. Bound ingestion rate, line size, query
range/concurrency, memory, and local buffers. Retention is not a hard disk cap:
monitor disk headroom and document a storage ceiling and full-disk procedure.

Default to structured JSON on stdout/stderr, preserving `docker compose logs`.
Choose collection transport explicitly before implementation: Docker Desktop does
not expose Linux container log paths like a native Linux host. Prefer Alloy through
a project-scoped Docker Logs API proxy with tightly allowlisted read endpoints and
no container exec/create/update access. A proxy still has security-sensitive Docker
access: review the concrete policy, mounts, and isolation before enabling it, and
request approval if elevated access is required. Do not mount the Docker socket
directly into Alloy or treat a read-only socket mount as API authorization.

If a sufficiently constrained Docker transport is unavailable, use a bounded
application JSON-file sink on dedicated log volumes and Alloy file tailing without
host privileges. Keep stdout, implement rotation/retention and multiprocess-safe
writes, and document which infrastructure logs are not collected by that fallback.
Do not add synchronous Loki/Alloy calls to application paths. Collector outages
must not block jobs or requests; expose dropped events and buffer saturation.
Logs can be delayed, duplicated, or lost: they are not an exactly-once audit store.

### Common event schema and correlation

Standardize Django, Gunicorn, Celery, Beat, and Rust events around UTC timestamp,
severity, service, environment, logger/target, schema version, stable event name,
outcome, duration, and safe error category. Include a build revision when available.
Use one event per JSON line with bounded field and event sizes.

Add request IDs generated server-side; accept incoming correlation headers only
after strict validation, and never use them for authorization. Propagate context
through Celery publication/task headers and Django-to-Infinity calls. Preserve
logical workflow ID across retries, while distinguishing task ID, execution ID,
attempt ID, and provider-call ID. Restore/clear Python contextvars and Rust tracing
spans on every exit so concurrent requests/tasks cannot inherit another's context.
Use trace/span IDs when existing tracing supplies them; full distributed tracing
storage is deferred, and no fake trace coverage should be claimed.

Identifiers are searchable JSON fields or supported structured metadata, never
Loki index labels or Prometheus labels. Loki labels are limited to fixed service,
environment, and severity values. Internal source/run identifiers may be recorded
where needed for operator diagnosis; applicant/user IDs are excluded by default.
All logs remain restricted operational data, including correlation IDs.

### Events that explain system behavior

- HTTP request completion: route name, method, status, duration, request ID; no raw
  URL, query string, IP, session, cookie, Authorization header, or request body.
- Scheduler: dispatch/skip/defer with reason, queue, next retry time, lease/budget
  outcome. Coalesce routine empty polling rather than flooding INFO logs.
- Discovery/ingestion: run start/checkpoint/completion, adapter/provider, page and
  inspected/new/existing/rejected/failed counts, categorized rejection/failure,
  elapsed time, and retry/budget/robots outcomes. Do not log opaque cursors, scraped
  HTML, job descriptions, or arbitrary remote error text.
- Normalization/evaluation: phase start/end, execution/attempt/call correlation,
  model alias, safe outcome, duration and known token usage; no CVs or prompts.
- Delivery: approval/state-transition outcomes and idempotency/duplicate-prevention
  decisions with safe workflow correlation. Never recipients, email/CV contents,
  attachments, tokens, or SMTP/API response bodies. Preserve existing durable audit
  and approval records as the authority for security-sensitive state changes.
- Worker/runtime: task lifecycle, retries, cancellations, process startup/shutdown,
  dependency loss/recovery, stale leases, and recovered interrupted work.

Define severity rules: INFO for meaningful transitions and summaries, WARNING for
recoverable degradation or exhausted capacity, ERROR for failed operations needing
attention. Expected exclusions are counters/summaries rather than one warning per
record. DEBUG is opt-in by component, explicitly time-bounded, and subject to the
same privacy policy; never enable framework/provider wire debugging globally.

### Safe errors and existing logging gaps

Django already emits JSON, but `django.request` is currently CRITICAL-only.
Replace that diagnostic gap with a safe explicit exception event, not unrestricted
framework exception logging. Configure Rust tracing JSON output and reconcile
Celery/Gunicorn formatters/handlers so events are consistent and not duplicated.
Audit direct print/logging, HTTP libraries, Celery failure handlers, and panic paths.

Prefer allowlisted fields and safe categorical messages at source. Redaction is
defense in depth, not permission to log arbitrary objects. Keep exception class,
error category, code location and a bounded safe stack fingerprint/frame list;
exclude exception messages, locals, function arguments, source lines containing
sensitive literals, request dumps, and chained remote payloads. Treat third-party
JSON, malformed lines, newline injection and ANSI controls as untrusted. Collector
parsing failures must not forward unsafe raw lines into a fallback log stream.
Do not ingest Postgres statement/parameter logs or unreviewed third-party debug logs.

### Investigation experience and acceptance tests

Add a Grafana investigation dashboard with service/severity/event filters, errors
over time, recent failures, and a live log view. Link metric panels to logs for the
same service and time range; link safe execution/task details to exact correlation
queries using a fixed configured Grafana origin and proper escaping. Provide
documented LogQL recipes: why a source has no new jobs, where a task stalled, why
Infinity retried, and whether a run completed after an interruption. State clearly
when logs expired, were dropped, or collection was disabled.

Use a mocked end-to-end workflow to prove one correlation chain spans Django,
Celery, and Infinity, including retry and cancellation. Test context isolation under
concurrency, duplicate delivery, malicious correlation headers and log injection.
Seed fake secrets/PII into requests, exception messages, third-party failures and
task arguments; assert absence from captured stdout, collected data, and Grafana
queries, not merely formatter output. Test rotation, read-position recovery,
collector/Loki outages, disk pressure, line bounds, retention and transport access
controls. Measure event volume and overhead alongside the metrics baseline.

Full tracing/Tempo, external alert delivery, long-term archival and immutable audit
storage are deferred. No external log shipping or SaaS credentials are included.

## 6. Privacy, reliability, and performance acceptance

- Never export URLs, queries, hostnames from arbitrary providers, email addresses,
  user/company/job IDs, task arguments/results, CVs, prompts, or response bodies.
- Route templates/names only; fixed fallback labels for unrecognized values.
  Test malicious and randomized values cannot create unbounded series.
- Do not log credentials or expose them in dashboard JSON, examples, or screenshots.
- Use bounded exporter concurrency, scrape timeouts, aggregate query cost, disk,
  memory, and label counts. Histogram bucket selection must reflect actual timeout
  ranges without multiplying series unnecessarily.
- Compare a reproducible local workload before/after: throughput, p95 latency,
  container RSS/CPU, metric series count, and exporter query count. Record results;
  agree a regression limit from the measured baseline instead of inventing a claim.
- Prometheus/Grafana failure must not affect collection, application requests,
  approval checks, or sends. Counters may reset on restart; queries use `rate` or
  `increase` correctly. Metrics are operational telemetry, not a durable audit log.

## 7. Verification and rollout

Mock external providers and AI. Test instrumentation with deterministic fake
requests/tasks; no live crawl, paid API, application, or communication is needed.

- Django: successes, errors, streaming responses, unknown routes, label escaping,
  sensitive-path/query exclusion, and unchanged authentication/MFA/CSRF behavior.
- Multiprocess: two Gunicorn workers and Celery prefork children; combined counts,
  process exit/replacement, stale gauges, clean container restart, isolated volumes,
  and no duplicate collector registration. Test the real process lifecycle.
- Rust: inbound/outbound success/failure/timeout/cancellation, unknown usage, bounded
  labels, scrape authentication, and unchanged execution authorization.
- Exporter: fresh/stale/missing snapshots, dependency outage, query bounds, no
  database writes or external workflow side effects during scrapes.
- Compose: validate config/profile isolation, localhost-only Grafana, no published
  scrape ports, secret handling, persistence, and application operation when the
  monitoring profile is stopped.
- Prometheus: `promtool check config`, `check rules`, rule unit tests, valid scrape
  exposition and all expected targets up. Grafana: datasource health, provisioned
  dashboard loading, working PromQL, and visible sample data in the browser.
- Run focused Django/Infinity suites, `make check`, `make infinity-check`, Cargo
  tests, configured secret scanning, migration-drift check, and final diff review.
  Report actual results and any unavailable checks.

Roll out locally with synthetic traffic first, inspect data/series cardinality and
resource usage, then use normal authorized application activity. Preserve disabled
sources and campaigns. Document troubleshooting, secret rotation, volume backup,
retention, dashboard edits, and rollback in `docs/operations.md`.

Rollback: stop only monitoring-profile services and disable instrumentation through
configuration. Keep application data and monitoring volumes. Never use
`docker compose down --volumes` as a monitoring rollback instruction.

## Suggested implementation order

1. Define metric names/labels and baseline; add deployment/provisioning skeleton.
2. Implement and test Python multiprocess exposition and Django HTTP metrics.
3. Add safe outbound instrumentation, Celery, and snapshot/business exporter.
4. Add Rust HTTP/evaluation/provider metrics and integration tests.
5. Provision dashboards/rules; add the operations link; validate end to end.
6. Add safe event schema/correlation, Loki/Alloy transport, and investigation views;
   validate privacy and outage behavior before enabling collection.
7. Document measured overhead and rollout. Offer optional infrastructure exporters
   separately with their exact privilege requirements.

## Official references

- Python multiprocess semantics and restrictions:
  https://prometheus.github.io/client_python/multiprocess/
- Prometheus instrumentation and label guidance:
  https://prometheus.io/docs/practices/instrumentation/
- Grafana provisioning:
  https://grafana.com/docs/grafana/latest/administration/provisioning/
- Grafana Docker configuration and file-based secrets:
  https://grafana.com/docs/grafana/latest/setup-grafana/configure-docker/
- Loki labels and high-cardinality structured metadata:
  https://grafana.com/docs/loki/latest/get-started/labels/
- Alloy Docker collection (review privilege requirements before use):
  https://grafana.com/docs/alloy/latest/monitor/monitor-docker-containers/

## Prompt for the next chat

Implement `docs/observability-plan.md`. First read AGENTS.md, Git status, current
telemetry, and deployment configuration. Preserve existing data and behavior. Build
the core self-hosted Prometheus/Grafana integration with mocked verification; keep
privileged infrastructure exporters optional and disabled pending approval. Do not
reset the database, send applications/emails, invoke paid APIs, deploy production,
or commit unless separately requested. Report verified results and limitations.
