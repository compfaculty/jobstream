# Infinity migration experiment

## Audit and dependency map

Baseline: d41e07a, Django 5.2 / Python 3.14, PostgreSQL, Redis/Celery,
server-rendered templates and HTMX. No existing agent framework or conversation store.

- Ingestion: Celery `schedule_sources -> ingest_source -> ingestion.ingest` invokes
  public source adapters, normalization, company/contact enrichment and lifecycle rules.
  Optional `normalization.normalize_with_ai -> ai.request_json` selects exact source lines.
- Preparation: owner-scoped batches -> Celery `prepare_batch_item` -> `batches.prepare`
  -> `preference_matching.evaluate` / `matching.assess`, then `resumes.tailor`.
  Optional tailoring calls `ai.select_facts -> request_json` (OpenAI Responses).
- AI persistence: `AIResult` caches owner/purpose/model/input/schema hashes with leases;
  `AIQuota` locks a global daily conservative token reservation. No conversation history.
- Delivery: application approval hashes message, attachment and sender; Celery dispatch
  claims approved records before connector calls. Uncertain delivery is reconciled, not
  blindly repeated. Gmail and employer/browser connectors remain Python services.
- Security: session authentication, staff MFA middleware, owner checks for private data,
  CSRF, private documents and encrypted Gmail credentials. Jobs are shared catalogue data.
- Tests: pytest-django/PostgreSQL, httpx mocks, browser and integration markers;
  Makefile uses uv, Ruff, mypy and migration checks. Existing periodic dispatchers recover
  lost broker publications using database leases.

## Classification

PRESERVE: all application services, matching rules, ORM, ingestion, connectors,
approvals, quota and private-data boundaries, existing AI paths and APIs.
ADAPT: job detail adds owner-scoped evaluation, gateway contracts, execution ledger,
Celery dispatcher and one read-only tool endpoint.
REPLACE: no legacy orchestration exists; Infinity runs only the new evaluation.
REMOVE LATER: none.

## Integration

User POST + confirmed owned profile -> PostgreSQL execution/input snapshot -> Celery
preparation queue -> versioned authenticated Rust HTTP -> Infinity context tool ->
authenticated Django immutable context -> model proposal -> runtime status polling ->
Django schema/evidence validation -> execution result -> owner-only HTMX panel.

Python computes and retains FitResult. The model ranks confirmed fact IDs and quotes
unmatched requirements; it cannot change eligibility or produce application side effects.
No automatic ingestion fanout, callbacks, email tools, shell tools or child agents.

Official crates: infinity-agent-core =0.2.0, infinity-provider-protocol =0.2.0.
Local execution needs Tokio LocalSet. Built-in tools must be disabled explicitly.
Sources: https://infinity.hydro.run/docs/infinity-runtime/overview/ and
https://infinity.hydro.run/docs/infinity-runtime/low-level-api/ .

## Tool protocol choice

RAP supports HTTP discovery and asynchronous callbacks; MCP bridges support sessions,
discovery and tool invocation. Neither is needed for one immediate read. A native
Infinity tool calls a fixed Django HTTP endpoint with credentials outside model arguments.
No arbitrary ORM access or model-selected URL is exposed.
Reference: https://infinity.hydro.run/docs/infinity-runtime/quickstart/connecting-rap-and-mcp/ .

## Recovery design

Django is authoritative for status and accepted results, with attempt fencing and
bounded retries. Rust's separate runtime schema stores thread snapshots and terminal
runtime outcomes. An interrupted inference can repeat and incur additional cost;
exactly-once inference is not promised. Polling and duplicate task delivery must persist
one accepted business result. In-memory mode intentionally loses runtime state on restart.
The runtime is a singleton with two concurrent evaluations; horizontal scaling is outside
this experiment. See the service README for operational details and verified limitations.


## Measured comparison — 2026-11-03

There is no original agent orchestration implementation to benchmark. Existing deterministic
matching and the new advisory agent perform different work; these numbers are separate
baselines, not evidence of a speedup.

- Deterministic Python matching: `DEBUG=1 uv run python manage.py benchmark_evaluations
  --iterations 1000`, fixture `python-linux-v1`: median **0.248 ms**, maximum **14.94 ms**
  in one warmed run. No database queries or LLM calls are included. Tokens/cost are zero.
- Infinity fake-model end-to-end: `make infinity-smoke`, same Python/Linux facts plus an
  untrusted-description fixture: **60.35 ms** in one debug-build sample, including gateway
  polling and Django result persistence. It excludes user queuing/Beat delay and real inference.
- Post-evaluation idle runtime RSS: **16,496 KiB** (16.1 MiB), measured with `ps -o rss`.
  This is the runtime process only, not the Django/PostgreSQL/Redis stack.
- Model tokens and inference cost: fake fixture reports zero. Real-provider latency, token
  consumption and cost per successful evaluation are **unmeasured**; no paid call was made.
  Configured price estimates are supported but cannot account for unreported interrupted calls.
- New integration source: **589 Python lines** across gateway, execution service and
  evaluation views; **1662 Rust lines** across HTTP/runtime, provider and store adapters.
  Counts include comments, blanks and inline Rust tests, and exclude templates, models,
  Celery additions, standalone Python tests and the lockfile. Existing agent-framework LOC
  is not applicable; these additions increase implementation and operational complexity.
- Dependencies: no new Python packages; **17 direct Rust dependencies**, with
  **265 packages including the service** in the all-target Cargo lockfile.
- Failure recovery: verified real process kill during the second fake completion, restricted
  PostgreSQL snapshot restoration, duplicate polling and one accepted business result.
  Also verified reconstruction of a committed final turn whose terminal status was lost,
  using an injected crash-window database fixture, before serving requests after restart.
- Operations: one extra singleton local Rust process and one private schema in the existing
  PostgreSQL database. Redis/Celery remain unchanged. No AWS, queue service or Kubernetes.

Environment: macOS arm64, Python 3.14.10, Rust 1.99.1, PostgreSQL 17.11; runtime built
with Cargo's development profile. Measurements are illustrative single-host samples without
statistical confidence intervals. Repeat on the intended deployment before performance claims.

## Validation and limits

The existing full non-browser suite passed (369 tests at the full run), and focused
contracts/restart checks passed after subsequent recovery coverage was added (24 focused
tests passed, with the real-provider test skipped). Seven Rust
unit tests, Clippy with warnings denied, formatting, Django checks, migration consistency,
and the existing mypy targets passed. Authorization, submission and Gmail statement/branch
coverage remain at least 90%. Real-provider checks are explicitly opt-in and were skipped;
GitHub CI configuration was updated but has not been run remotely. Browser regression tests
were not run locally; the existing CI browser job remains in place.

The feature defaults to disabled. Existing AI normalization and CV selection remain on
Python/OpenAI; matching and approval decisions stay in Python. Explicit retries are bounded
to three attempts and honor the provider retry window. Service ambiguity reuses an attempt.
Durable storage recovers history and completed outcomes; in-memory mode intentionally
loses internal state. Exact-once inference, multi-runtime scaling, production remote transport,
and automated runtime-history retention are outside this experiment's guarantees.
