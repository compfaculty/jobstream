# Operations and production gates

## Local maintenance

```sh
docker compose ps
docker compose logs --tail=100 worker beat
uv run python manage.py check
uv run python manage.py makemigrations --check --dry-run
uv run python manage.py seed_sources
```

Use Admin ingestion histories and source last-success dates to detect stale sources.
Monitor approved outbox age, unknown submissions, explicit send rejections, worker
availability, queue length, database latency and storage errors. Never reset an unknown
application to approved just because it is old. Reconcile with Gmail or investigate it.

A source lease recovers after ten minutes; worker soft/hard limits are 240/300 seconds.
Tasks are acknowledged late and never blindly retry uncertain sends. Explicit 429
rejections use durable retry schedules. Keep one Beat scheduler.
Redis uses noeviction; broker failures delay work but do not erase approved database state.

## Backup/restore procedure to validate before launch

1. Take managed PostgreSQL point-in-time backups and encrypted object-storage versions.
2. Back up token encryption keys separately under restricted access.
3. Restore into an isolated environment with workers and Beat **disabled** and outbound
   Gmail blocked. Never start a restored outbox against real accounts automatically.
4. Verify row counts, ownership, document integrity and representative searches.
5. Reconcile any approved/submitting/unknown rows against the original environment and
   provider history before enabling workers. A restore can resurrect previously sent work.
6. Record measured recovery time and loss window against agreed RTO/RPO.

This procedure has not been exercised against a managed production platform yet.

## Hosting decision and Terraform milestone

Choose provider/region, budget ceiling, expected dataset/request mix, data residency,
availability and recovery targets first. Then implement reviewed Terraform for network,
managed containers, PostgreSQL, Redis, private object storage, secrets, Cloudflare,
monitoring and backups. Use encrypted remote state with locking and restricted access.

CI should authenticate through workload OIDC, build once, scan, and deploy by image digest.
Use an explicitly protected production environment, reviewed migration job, rolling health
checks and rollback to a prior image. Prefer additive compatible schema changes; an image
rollback does not reverse destructive migrations. Configure branch protection in the
hosting service when the repository remote is selected. No cloud resources or deploy
credentials are created by this local implementation.

## Worker queues

Compose runs independent control (`celery`), ingestion, preparation and delivery
workers. Concurrency defaults are 1/2/2/1 respectively. Queue routing is configured in
CELERY_TASK_ROUTES. Restart all application workers and Beat together after changing
routes; old workers listening only to the default queue cannot consume the new stage
queues. `make worker` consumes all four queues for lightweight local development.

```sh
docker compose build web
docker compose run --rm --no-deps web python manage.py migrate --noinput
docker compose up -d web worker ingestion-worker preparation-worker delivery-worker beat
docker compose logs --tail=100 preparation-worker delivery-worker
```

Explicit Gmail 429 rejections schedule bounded durable retries; ambiguous delivery
outcomes remain unknown. Do not manually enqueue an unknown send. See
`docs/batch-applications.md` for cooldowns, approvals, cancellation and AI budgets.

## Company catalogue rollout

See [Company catalogue](company-catalogue.md) before migrations 0027–0031. Back up
PostgreSQL first: verified company identities are consolidated and general boards
are disabled. Keep automatic closure in shadow mode until a small cohort is checked.
`python manage.py crawl_report --hours 24` provides collection health without exposing
remote response bodies or applicant data.

## Staff Operations dashboard

Open `/operations/` (also linked in staff navigation). Staff must complete the same
MFA verification required by Admin. The dashboard is read-only and exposes global
operational metadata; ordinary users cannot access either it or execution details.

The page shows workflow state counts, recent ingestion/preparation/delivery/evaluation
history, overdue leases/deadlines, retry timing, execution token totals, and the latest
infrastructure snapshot. History supports 1-hour, 24-hour, and 7-day windows, status
filters, and 25-record pages. Workflow counts cover all retained records; the history
window uses record creation/start time, not completion time. Execution tokens are
counted once from Django's execution ledger, never added to provider-call totals.
Missing or partial usage is explicitly counted as unknown; no monetary cost is estimated.

Beat publishes `jobs.tasks.collect_operations` every 30 seconds to the control queue.
That task samples Redis queue depths (including default priority buckets), workers'
queue subscriptions, and active task identities. Probe connections and inspection
waits have short timeouts; dashboard requests do not contact the broker. No task
arguments, return values, or worker inspection payloads are retained. The snapshot is
a single replaceable database record, rather than an accumulating metrics history.

A snapshot older than 90 seconds is **stale**: its dependency observations are
historical, not evidence of current health. No snapshot means **unavailable**.
A missing queue consumer is unavailable even when another worker responds. Scheduler
freshness records publication of the monitoring task, separately from its execution;
a stalled control queue makes the snapshot stale. Manually publishing that same task
also updates its publication timestamp, so this is a dispatch-progress indicator,
not an independent Beat process heartbeat. Observed running tasks are point-in-time
samples and can miss short tasks. A successful snapshot write proves database access
at observation time; the UI cannot serve database-backed history during a database outage.

Pages refresh every 10 seconds while visible. Refresh pauses in hidden tabs, while a
filter has focus, or after an unsaved filter change. Use Filter to apply changes and
resume refresh. No monitoring action retries work, cancels evaluations, or sends applications.

### Infinity / OpenAI metadata

Execution details show the latest 100 provider invocations across attempts within
30 days: execution/attempt/call IDs, model, timestamps, latency, outcome, categorized
error, reported tokens, and an allowlisted provider request ID when available.
Invocations include local validation failures before an HTTP request; these do not
imply OpenAI accepted a request. A completed provider call also does not imply the
whole evaluation completed. Unreported tokens remain unknown, including interrupted calls.

The separate authenticated endpoint is
`GET /v1/operations/executions/{execution_id}/calls`; the execution-response v1
contract is unchanged. Django uses a bounded HTTP timeout and response size and
projects only metadata fields. If Infinity is unavailable or lacks persistence,
the Django history still renders with an explicit provider-history unavailable state.

Metadata persists in `infinity_runtime.provider_calls` using the restricted runtime
role. Prompts, CV content, responses, credentials, and raw exceptions are excluded.
The singleton runtime lock permits marking unfinished calls interrupted after a
restart. Cancellation and evaluation deadlines finalize in-flight call metadata
without changing existing recovery/retry semantics. Runtime cleanup runs at startup
and hourly, deleting terminal metadata older than 30 days. Existing private conversation
and execution retention is unchanged. Provider metadata begins at deployment; it is
not backfilled. Telemetry write failures emit a categorized warning and do not fail
otherwise valid evaluations.

Python lifecycle logs contain task name/ID, state, and duration. Evaluation tasks also
include execution/attempt IDs. Infinity emits startup/readiness messages and an idle heartbeat every 30 seconds,
including the count of active evaluations. Accepted evaluations and context-tool
start/end events also carry correlation IDs. Infinity provider logs use the same execution/attempt
IDs plus call IDs and safe outcome/usage metadata. Follow these IDs through Compose
logs; the dashboard does not expose arbitrary logs or raw runtime conversation data.

### Upgrade and verification

Build the updated application and Infinity images before rollout. Apply Django
migrations 0038–0039 and rerun the existing Infinity provisioning script with the
configured database owner and `INFINITY_DATABASE_URL` to add the metadata table,
indexes, and narrowly scoped DELETE permission for retention:

```sh
uv run --env-file .env python manage.py migrate
uv run --env-file .env python -m scripts.provision_infinity
uv run --env-file .env python manage.py collectstatic --noinput
```

Restart web, all workers, Beat, and Infinity with the updated code. The Infinity schema
upgrade is required before starting the updated durable runtime; without persistence,
evaluations continue to work but durable provider history is unavailable. Local host
commands need host-reachable database endpoints; in Compose, run the migration and
provisioning commands inside the web image instead. Provisioning does not print credentials.

Verify a fresh snapshot appears within roughly one minute, each expected queue has a
consumer, and a synthetic evaluation produces call records. Focused checks:

```sh
DEBUG=1 uv run pytest tests/test_operations.py tests/test_evaluations.py tests/test_tasks.py
cargo test --locked --manifest-path services/infinity-agent/Cargo.toml
INFINITY_INTEGRATION=1 DEBUG=1 uv run pytest tests/test_infinity_integration.py
DEBUG=1 uv run pytest tests/test_operations_browser.py
```

Integration tests use a deterministic fake provider and a separate test database.
Real OpenAI inference remains opt-in through the existing explicit integration-test gate.


## Evaluate directly from an uploaded CV

A job page lists the owner's uploaded PDFs as the default evaluation inputs. Clicking
Evaluate extracts PDF text automatically; no reviewed profile, name form, or manual
paragraph entry is required. Optional reviewed profiles remain selectable, and the
existing `profile` POST field remains supported. The UI submits `cv=document:<id>`
or `cv=profile:<id>` to the existing evaluation endpoint.

Django migration 0040 adds an optional protected document reference to AgentExecution
and makes its profile reference optional. Older executions retain their profile links.
PDF evaluations record document ID/checksum in the immutable snapshot; they do not
create or label an automatically extracted profile as user-confirmed. Parsing checks
file ownership and integrity and applies existing PDF size/page/text and AI input
limits. Image-only, encrypted, corrupt, or missing PDFs produce an actionable error
before any execution is queued. Uploaded text is split mechanically into bounded
source chunks without inventing qualifications. Choosing a reviewed profile preserves
the existing evaluation behavior.

The existing manual profile editor and tailoring workflow remain available. No
uploaded CV is evaluated or sent to OpenAI merely by opening a page or deploying this
change; evaluation still requires the owner's explicit Evaluate action.

## CV match percentages and hard preferences

New evaluations use version `vacancy-evaluation-v2`. The model assesses each supplied
requirement exactly once as demonstrated, partial, or not demonstrated in the CV,
with a short explanation and an exact CV evidence quote for demonstrated/partial
claims. PDF whitespace is normalized when validating quotes. Django rejects unknown
IDs, duplicate or omitted criteria, unsupported quotes, extra output fields, and
invalid classifications. The model does not choose the final percentage.

The app calculates weighted requirement coverage: demonstrated = 1, partial = 0.5,
not demonstrated = 0; required criteria weigh 3 and preferred criteria weigh 1.
A rounded percentage reflects documented CV evidence, not hiring probability or
proof that an undocumented skill is absent. The page shows the percentage and a short
summary, with requirement-level evidence collapsed under calculation details.

Saved preferences override the final score. Any excluded technology mentioned in the
job title or description forces 0%, including alternatives and optional mentions.
Java and JavaScript remain distinct. Enabled required location/title/technology and
remote-only limits also force 0% when unsatisfied or unverifiable; soft preferences
do not. When a hard preference blocks a queued evaluation, Django completes it with
an explicit explanation and zero token usage without invoking Infinity/OpenAI.
Preferences are snapshotted at evaluation time; rerun after changing them.

Existing execution results remain unchanged and display a prompt to evaluate again
for a percentage. Re-evaluation uses a new version/cache key. The gateway envelope
and capability signing salt stay compatible; the Rust runtime continues accepting
legacy result contracts during recovery. Deploy web/workers and Infinity together
for v2 scoring. No additional database migration is required for scoring.

## Agent budget reservation settlement

Migration 0041 records each current attempt's original quota day, reserved allowance,
known charged tokens, and settlement state. A successful terminal response with both
usage counters replaces its safety reservation with actual reported usage under
execution and quota row locks. Duplicate deliveries cannot settle twice; settlement
uses the original admission day even across UTC midnight. Unknown or failed outcomes
keep their conservative reservation. Retrying such attempts never refunds them.

After upgrading web and workers, run:

```sh
docker compose exec -T web python manage.py reconcile_agent_budgets
```

This idempotently repairs only attributable, completed, single-attempt legacy agent
executions with valid usage and evidence of prior admission. It never resets daily
quota totals, touches other AI work's reservations, or releases uncertain legacy
attempts. The shared quota counter includes known agent usage plus unspent/uncertain
reservations; it is not simply provider-reported consumption. Other existing Python
AI workflows continue using their conservative reservation policy.

A locally rejected admission sends no provider request and does not consume a retry
attempt. It can be retried immediately when capacity is available; provider cooldowns
are unchanged. If one evaluation's allowance exceeds the entire configured daily
budget, the UI explains that waiting alone cannot resolve it. The configured daily
limit is never raised automatically.

## Local monitoring and investigations

The opt-in stack is Prometheus, Grafana, Loki, and Alloy. Native dashboards are
provisioned from `deploy/monitoring/dashboards/`: **Mission control**, **Django &
collection**, **Infinity runtime**, and **Investigations**. No plugins, community
imports, signup, paid services, or external alert receivers are required.

The explicit Compose overlay enables instrumentation; a plain `docker compose up`
continues to use the original development configuration. Application data and
migration history are unchanged.

```sh
make monitoring-setup       # Builds the app and initializes only telemetry volumes/secrets.
make monitoring-check       # Compose, Prometheus rules/tests, Alloy, and Loki validation.
make monitoring-up          # Starts normal app workers/Beat too: normal scheduled work resumes.
make monitoring-stop        # Stops only monitoring services; retains every volume.
```

`monitoring-up` resumes the existing application queues, including enabled sources.
For a web-only validation that must not trigger scheduled/provider work, use:

```sh
docker compose -f compose.yaml -f compose.monitoring.yaml --profile monitoring \
  up -d --no-deps web prometheus grafana loki alloy snapshot-exporter
```

Grafana is at **http://localhost:3000**, username `admin`. The initial password is
in `.monitoring-secrets/grafana-admin`; retrieve it locally without copying it into
chat, logs, screenshots, shell history, or command arguments. Host credential files
are `0600` inside a `0700` ignored directory. Initialization transfers credentials
over stdin into separate read-only container volumes, owned by UID 10001 with
`0400` files. Application containers cannot read the Grafana credential volume.
Grafana has its own authentication; Django MFA does not protect it.

Only Grafana publishes a monitoring port, bound to `127.0.0.1`. Its dedicated
non-internal ingress bridge is needed for Docker Desktop localhost forwarding.
Prometheus, Loki, Alloy, and scrape endpoints have no published ports. Loki has no
HTTP authentication and is isolated on a backend network shared only by trusted
monitoring services. Application metrics require a dedicated bearer credential,
independent of Infinity's execution credential. Do not attach unrelated containers
to these networks or publish internal listeners.

### Optional Infinity and normal operation

Infinity remains opt-in. Enable its target only when that runtime is intended to
be running; otherwise the target is absent and no target-loss alert fires for it.

```sh
uv run python scripts/setup_monitoring.py --infinity
# At a safe runtime restart boundary, apply the same overlay to Infinity:
docker compose -f compose.yaml -f compose.monitoring.yaml \
  --profile monitoring --profile infinity up -d --build --no-deps infinity
```

Restarting Infinity can recover existing work; this is not a synthetic test command.
`--without-infinity` disables its target. Repeated setup preserves existing target
selection and passwords. Normal application processes use private per-container
multiprocess directories under bounded `/tmp`; no metric files are shared between
containers. The entrypoint clears them once before fresh startup, supervises the
app, and retries failed exposition three times without terminating business work.
Gunicorn and Celery parent/child hooks remove dead live-gauge files. Counters and
histograms retain dead-worker contributions until the container restarts. They are
operational counters, not durable accounting.

Use `GRAFANA_URL=http://localhost:3000` (set by the overlay) for staff-visible links
on `/operations/` and execution details. Only an HTTP(S) origin without credentials,
query, fragment, or path is accepted. Execution links contain a validated UUID and
an escaped correlation filter. Existing staff/MFA restrictions still apply, and
page rendering does not query Grafana.

### Metric meanings and coverage

- HTTP labels are installed route names, fixed methods, and status. Unknown routes
  collapse to `unknown`; paths and query strings are never exported. Liveness is
  shown separately. Durations include response lifetime; streaming close/exhaustion
  releases in-progress gauges without buffering the body. Streaming payload sizes
  remain unknown.
- Outbound attempts include pinned public HTTPS fetches/redirects/robots, Brave,
  Python AI Responses and Batch endpoints, and Django-to-Infinity calls. Bodies are
  counted only at existing bounded reads. These are **observed decoded payload
  bytes**, not compressed wire bytes, TLS overhead, or total network traffic.
  Failed/unread bodies increment unknown-size counters. Redirects are attempts,
  not invented retry counts; Celery retry outcomes count task attempts separately.
- Rust provider metrics use the aliases `fake`/`configured`, fixed error categories,
  known input/output tokens, and an explicit unknown-usage counter. A dropped
  provider future counts cancellation. Reading provider metadata never creates an
  inference metric. Existing execution/provider-call records remain authoritative
  for per-execution investigation.
- Celery start/outcome/duration/running metrics count execution attempts, including
  `RETRY`, rather than publications or exactly-once business outcomes. Periodic
  empty poll tasks keep metrics but suppress routine INFO lifecycle noise.
- The separate snapshot exporter reads three aggregate queries every 15 seconds in
  a read-only PostgreSQL transaction with statement and connection timeouts. It
  never inspects Celery, probes providers, publishes tasks, or writes records during
  scrapes. Company review backlog means `review_required=True`; source review
  backlog means `discovery_pending=True`; enabled sources are a current stock.
  Queue/consumer/scheduler gauges are the **stored snapshot**, with explicit
  timestamp/presence/success metrics. Stale snapshots are not fresh broker probes.
- Finalized ingestion outcome/new-job counters are process counters. They are not
  database inventory totals and do not backfill historical runs.
- Browser-managed traffic, portal submission/upload transports in
  `secret_telaviv.py`/`ashby_browser.py`, Google/Gmail SDK traffic, SMTP, and S3 SDK
  traffic are outside HTTP body coverage. Shared public fetches used by these
  adapters remain covered. No container-wide traffic claim is made.

Each enabled app container has one authenticated exporter on internal port 9100.
Scrapes are serial and socket-time-bounded; the business collector has its own
registry. There are no Redis increments or monitoring HTTP calls in request paths.
Optional tracing configuration is preserved; valid existing trace/span IDs are
included when available. No Tempo or new tracing exporter is enabled.

### Log privacy, retention, and loss

Application handlers emit bounded JSON with a versioned schema, UTC timestamp,
service/environment/severity, stable event, safe outcome/error metadata, and
validated correlation fields. Arbitrary exception messages, locals, arguments,
results, provider bodies, CVs, prompts, cookies, credentials, and raw URLs are
excluded. Python errors include safe code frames and a type fingerprint. Rust
startup errors/panics exclude payloads. Models are aliases. Existing durable audit
and approval records remain the authority for sends and state transitions.

Request/workflow IDs pass through Celery headers and the Django-to-Infinity
boundary. Retries retain workflow identity; task/execution/attempt/call IDs remain
separate fields. Context is restored after requests, stream iterations, and tasks.
Runtime recovery can be joined through the persistent execution/attempt ID even
when its earlier in-memory workflow span is unavailable after restart.

Stdout remains available through `docker compose logs`. Docker's local log driver
is non-blocking with a 1 MiB buffer and three 10 MiB files per container; driver
buffer loss is not measurable without Docker API access. Quiet Celery startup
suppresses broker banners; static process-manager shutdown text may still appear
on stdout, but is never ingested as a raw fallback log.

Each process also has a non-blocking 512-event file queue, 2 MiB rotating files,
and two backups on a dedicated service volume. Dead-process history is pruned to
64 MiB/24 hours; live process files add at most 6 MiB per configured process.
Pruning runs at startup and during event delivery, so idle stopped services need
not immediately remove expired files. A queue/disk failure drops file events and
increments a drop counter; stdout delivery continues. File buffers are best effort
and may lose recent events on abrupt termination.

Alloy has no Docker socket or host mounts. It tails these volumes read-only,
persists positions, rejects malformed/oversized/non-schema events, reconstructs an
allowlisted JSON document, and limits forwarding to 200 events/second with a
400-event burst. Its push batches are bounded to 256 KiB with five retries. There
is no infinite collector spool: outages can cause drops, rotation loss, delay, or
duplicates. Infrastructure container logs are deliberately not collected.

Loki index labels are only service/environment/severity. Automatic service-name
label discovery is disabled. IDs are searchable JSON fields, never index or metric
labels. Early local validation streams created before that setting may retain the
bounded automatic `service_name` label until normal retention removes them.

Prometheus retains at most seven days or 2 GiB of retained blocks, whichever wins;
WAL/head/compaction need additional disk space. Loki's compactor uses seven-day
retention, with a two-hour deletion delay and periodic compaction. Retention is not
an exact wall-clock erasure guarantee or a hard disk quota. Physical seven-day
expiry has not been time-accelerated in local tests.

Use **5 GiB as the local Loki operational ceiling**, keep at least 2 GiB free on
its backing filesystem, and inspect growth with `docker compose ... exec -T loki /health/busybox du
-sh /loki`. The snapshot exporter exposes available filesystem bytes on only the
three mounted monitoring storage volumes; on Docker Desktop this is shared Linux
VM filesystem headroom, not macOS host/container network metrics. A local alert
fires below 2 GiB for five minutes. Review ingestion volume before increasing
retention or capacity. The ceiling is an operator policy, not a filesystem quota.

If disk pressure occurs, stop Alloy and Loki with the monitoring overlay; leave
application services and PostgreSQL volumes alone. Inspect monitoring growth and
provide storage or reduce retention before restarting. Do not delete live Loki
files, reset the application database, or use `down --volumes`. Back up stopped
monitoring volumes separately with read-only mounts and UID 10001; restrict and
encrypt backups because logs and Grafana configuration are operational private data.

### Investigation recipes and local alerts

The Investigation dashboard supports service/severity/event filters and an optional
correlation UUID. Follow metric panel links for the same service/time range, or
execution links for an exact UUID query. Useful LogQL patterns:

```logql
# Why no new jobs? Use the internal source ID, not a URL or applicant identifier.
{service="ingestion-worker"} | json | event="ingestion_completed" | source_id="42"
# Where did a task stop? Paste its UUID.
{service=~".+"} | json | task_id="00000000-0000-0000-0000-000000000001"
# Follow an evaluation and its provider outcomes/recovery across services.
{service=~".+"} | json | execution_id="00000000-0000-0000-0000-000000000002"
# Follow one logical request/workflow chain across tasks and runtime calls.
{service=~".+"} | json | workflow_id="00000000-0000-0000-0000-000000000003"
```

Use the relevant time range before declaring an event missing. Check target health,
snapshot freshness, application file drops, collector drops, and enabled collection.
Logs may have expired or never arrived; absence does not prove work did not occur.
No-data percentiles remain gaps. Infinity disabled, an unhealthy enabled runtime,
and a stale snapshot are distinct states. Alerts are local Prometheus rules only;
there is no Alertmanager receiver or email/Slack notification integration.

### Credentials, edits, validation, and rollback

```sh
# Explicit rotations: values travel over stdin and remain outside command arguments.
uv run python scripts/setup_monitoring.py --rotate grafana
uv run python scripts/setup_monitoring.py --rotate scrape
```

Grafana rotation updates the existing admin password through its CLI, not just the
bootstrap setting. Scrape rotation requires a controlled restart of instrumented
app exporters, Infinity if enabled, and Prometheus; until those processes reload
credentials, affected targets may report unauthorized. Restart only at an approved
work boundary. Existing Django/OAuth/Infinity execution credentials are untouched.

Edit `scripts/generate_dashboards.py`, regenerate the committed dashboard JSON, and
let Grafana provisioning reload it. UI dashboard edits are intentionally disabled.
Run `uv run python scripts/check_monitoring_runtime.py` for datasource health,
provisioning, and every provisioned PromQL/LogQL query. It never prints credentials
or log bodies. The repository CI runs process tests on Linux and validates the
monitoring configuration/rules in the existing web-image job; remote CI execution
still requires the normal PR workflow.

Focused verification commands:

```sh
DEBUG=1 OBSERVABILITY_PROCESS_TESTS=1 uv run pytest tests/test_observability_processes.py
cargo build --locked --manifest-path services/infinity-agent/Cargo.toml
DEBUG=1 INFINITY_INTEGRATION=1 uv run pytest tests/test_infinity_integration.py
DEBUG=1 uv run pytest tests/test_observability.py tests/test_operations.py
DEBUG=1 make check
make infinity-check
make monitoring-check
```

Process checks use unique test queues/key prefixes, mocked inference, loopback HTTP,
and a test PostgreSQL database. They do not start business workers or send anything.
The Linux image can run `python scripts/observability_lifecycle.py` with
`PROBE_BROKER=redis://redis:6379/15` to exercise real prefork behavior without pytest.

A synthetic, warmed 10,000-request `/health/` comparison on local macOS measured
9,117.5 requests/s and 0.238 ms p95 from the unchanged HEAD snapshot, versus 6,400.6
requests/s and 0.283 ms p95 with metrics/JSON completion events. CPU was 1.097 versus
1.559 seconds; max RSS was about 131 MiB in both processes. This test used local
in-memory metrics and console output redirected to `/dev/null`; it excludes file
shipping, real endpoint/database/provider work, and multi-container contention.
It demonstrates measurable overhead, not a representative production acceptance
benchmark. No regression threshold has been agreed, and no negligible-overhead
claim is made. Repeat with representative authorized workloads before production.

Pinned releases were checked against official release metadata on 2026-11-07:
Prometheus 3.13.5 LTS, Grafana 13.3.0, Loki 3.7.9, Alloy 1.21.3. Manifest digests are
in the overlay and derived Loki Dockerfile. Review upstream patches regularly. References:
[Prometheus multiprocess limitations](https://prometheus.github.io/client_python/multiprocess/),
[Prometheus LTS support](https://prometheus.io/docs/introduction/release-cycle/),
[Grafana release support](https://grafana.com/docs/grafana/latest/upgrade-guide/when-to-upgrade/).

Rollback monitoring with `make monitoring-stop`; application instrumentation keeps
working without the backend. To disable instrumentation too, recreate desired app
services using only `compose.yaml` at a safe work boundary. This removes the
monitoring environment/listeners/file mounts while retaining all application and
monitoring volumes. No migration reversal or data deletion is needed.

Local verification checkpoint (2026-11-07): the final non-browser run with fake
Infinity and real process tests enabled reported **708 passed, 1 skipped**, with
one pre-existing failure: `test_search_query_budget` expects two queries but the
unchanged `search_jobs` prefetch performs three. The same failure was reproduced
from a temporary clean HEAD snapshot; its implementation/test were not changed.
`make check`, Rust fmt/clippy, 14 Cargo tests, locked dependency audit, and Gitleaks
passed. All seven alert rules have firing/recovery tests. Grafana provisioning,
datasource health, dashboard PromQL/LogQL, and native browser rendering were checked.
A canary request was absent from captured Docker stdout and queried Loki data;
correlated events were present. Actual Loki index labels were checked separately
from LogQL's temporary extracted fields. Stopping/restarting Alloy left Django
available and recovered the file-buffered event while retaining earlier logs.

An idle local sample measured Grafana 271 MiB, Prometheus 69 MiB, Loki 90 MiB,
Alloy 59 MiB, and the snapshot exporter 56 MiB, within their configured limits.
This is an idle sample, not a load-capacity guarantee. Physical disk exhaustion,
week-long retention expiry, external alert delivery, and production traffic were
not exercised. Browser-driven application workflows were not rerun for this
backend change. During safe validation, only Django web and monitoring were
started; existing collection/delivery workers and Beat were left stopped. Their
unavailable/stale indicators are expected until normal application work resumes.
Optional Infinity instrumentation is implemented and fake-provider-tested but its
existing live container was not rebuilt/restarted for this rollout.

`jobstream_ingestion_new_jobs_total` counts newly committed `Job` rows only.
`jobstream_ingestion_observations_total` counts accepted observations, including
existing jobs; the older `IngestionRun.imported` field retains that original meaning.
Repeat observations and rolled-back transactions do not inflate the new-job counter.
Pre-connection pinned-fetch policy/DNS/capacity rejections appear in run/task
outcomes rather than the HTTP attempt counter. The native collector scrape uses a
metric-family allowlist to exclude per-file/PID label families.

Phase `*_finished` events with `outcome=returned` mean the function returned, not
that a business operation succeeded. Explicit approval/delivery state events are
emitted after commit with robust callbacks; expected validation rejection uses
INFO. Durable audit records still determine the actual application state. Console
and file logging failures have safe handlers that do not print logging's original
message/argument fallback.


Loki's pinned minimal upstream image has no wget. The local derived image copies
only the static BusyBox binary from the already pinned Prometheus image to perform
an actual `/ready` HTTP probe; it does not install packages or add privileges.
The exporter uses the existing local application database role. Read-only
transactions constrain normal scrapes, not that role's database privileges; no
generic PostgreSQL statistics exporter or additional database role was introduced.
The actual collector boundary was also checked with a synthetic JSON event carrying
an unknown private field and a malformed line: required state survived, the private
field was stripped, and the malformed line was dropped before Loki. The temporary
probe file was removed. All four dashboards were validated with their actual All
filters; Loki selectors require non-empty matchers, unlike PromQL selectors.
