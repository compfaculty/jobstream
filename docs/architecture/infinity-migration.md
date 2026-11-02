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
