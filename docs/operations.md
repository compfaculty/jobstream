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
