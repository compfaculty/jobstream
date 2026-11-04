# Jobstream Infinity experiment

This service powers only the owner-triggered **Evaluate vacancy** action. It never
creates an application, changes eligibility, ingests jobs, or sends email. The audit
and boundary map are in [infinity-migration.md](../../docs/architecture/infinity-migration.md).

## Local startup

### Docker Compose

Set `AGENT_BACKEND=infinity`, `INFINITY_PROVIDER=openai`, `INFINITY_MODEL`,
`OPENAI_MODEL`, `OPENAI_API_KEY`, and the two independent Infinity secrets in `.env`.
Set `INFINITY_DATABASE_URL` to
`postgresql://jobstream_infinity:<random-url-safe-password>@db:5432/jobstream`.
Compose supplies private service addresses and passes only the runtime's configuration
to its container. The Rust port is not published to the host.

```sh
docker compose --profile infinity build
docker compose run --rm web python manage.py migrate
docker compose run --rm web python -m scripts.provision_infinity
docker compose --profile infinity up -d
```

Provisioning creates the runtime schema and separate login, grants only runtime table
access, and checks that it cannot read the application execution table. Rerunning it
updates the login password from `.env`. Start the profile on subsequent Compose updates
to include the Rust service. Use only the Compose Beat instance.

### Host processes

When switching from Compose to host processes, use loopback URLs for `INFINITY_URL`,
`INFINITY_DJANGO_URL`, and the database host instead of Compose service names.

Use the existing PostgreSQL/Redis services (`make services`) and install Python dependencies
with `make setup`. Export your normal development settings (`DEBUG=1`, database URL, etc.).
Docker Compose loads `.env` for Django and workers. `make infinity` also loads `.env`,
then passes only runtime credentials to Rust. Direct host Django commands still need their
environment exported.

1. Run `make migrate` to add the application execution ledger.
2. Generate two independent random secrets, e.g. `openssl rand -hex 32` twice. Export
   `INFINITY_SERVICE_TOKEN` in Django/workers and Rust; export `INFINITY_TOOL_SECRET`
   only in Django/workers. Never commit or log these values.
3. Export `INFINITY_MODEL` in Django/workers with your chosen tool-capable OpenAI model.
   Export `OPENAI_API_KEY` only in Rust for this workflow. Existing Python AI paths
   still require their own existing configuration if used.
4. Run `make infinity` in one terminal; it reads `.env` automatically. The default listener
   is `127.0.0.1:8091`.
5. Run `make infinity-django`, and run `AGENT_BACKEND=infinity make worker` and
   `AGENT_BACKEND=infinity make beat` in other terminals. Use one Beat process.
6. On a job detail page, select one of your confirmed CV profiles and choose Evaluate.
   Beat dispatches within ten seconds; the panel polls only while work is pending.

For an offline smoke run, set Rust's `INFINITY_PROVIDER=fake` and use `INFINITY_MODEL=fake`
in Django. The fake provider still runs the official Infinity runtime and authorized
context tool, but ranks facts in their original order and returns no gaps. It is a test
fixture, not a meaningful vacancy assessment.

For containers, configure the fixed `INFINITY_URL` and `INFINITY_DJANGO_URL` addresses
explicitly. Do not expose either internal interface publicly. Plain HTTP/PostgreSQL
connections are intended for trusted local development; remote deployment needs an
appropriate protected transport and network boundary.

## Configuration

- `AGENT_BACKEND`: `disabled` (default) or `infinity`; existing AI calls are unaffected.
- `INFINITY_URL`: Django's fixed runtime origin, default `http://127.0.0.1:8091`.
- `INFINITY_SERVICE_TOKEN`: runtime authentication secret, at least 32 characters.
- `INFINITY_TOOL_SECRET`: separate Django capability-signing secret, never sent to Rust.
- `INFINITY_MODEL`: model captured in each Django execution snapshot.
- `INFINITY_DJANGO_URL`: Rust's fixed Django origin, default `http://127.0.0.1:8000`.
- `INFINITY_BIND`: Rust listener, default `127.0.0.1:8091`.
- `INFINITY_PROVIDER`: `openai` (default) or `fake`; production credentials stay in Rust.
- `OPENAI_API_KEY`: required by Rust when using OpenAI; no key is needed by fake tests.
- `INFINITY_DATABASE_URL`: optional restricted local PostgreSQL connection; absent/blank
  selects volatile in-memory runtime history and outcomes.
- `AI_MAX_INPUT_CHARACTERS`, `AI_MAX_OUTPUT_TOKENS`, `AI_DAILY_TOKEN_BUDGET`: existing
  Python settings reused for conservative reservations. Runtime hard caps are 30,000
  context characters and 2,000 output tokens per completion; use values at or below these.
- `INFINITY_INPUT_COST_PER_MILLION`, `INFINITY_OUTPUT_COST_PER_MILLION`: optional USD
  prices used for estimates; blank means unmeasured. Reservations are not refunds or
  invoice reconciliation, and uncertain requests may consume tokens without a usage report.
- `RUST_LOG`: application metadata level (`info` by default, or `debug`/`trace` etc.).
  Upstream runtime logs are suppressed even at trace level to avoid private tool content.

## Durable runtime state

Reuse the application PostgreSQL server/database with a **separate schema and login role**.
As a database administrator, run `schema.sql`; provision a dedicated login using your
normal secret tooling, then apply its documented schema/table grants. Do not grant it
access to `public.jobs_*`, Django auth tables, or application documents. Set
`INFINITY_DATABASE_URL` to that role's connection string. No schema provisioning happens
in the service process. The integration test explicitly checks denial of application-table access.

`ConversationStore` and `StateStore` delegate semantics to the official in-memory stores,
restore their serializable snapshots, and await a PostgreSQL write on each mutation.
The service holds a session advisory lock: only one runtime can own the schema. Two
executions can run concurrently; each attempt allows four model calls and four read tools,
and expires at the Django deadline (180 seconds).

Django remains authoritative for accepted results. Runtime responses are polled; there
are no callbacks. Starting the same execution/attempt is idempotent, with semantic request
conflicts rejected. A pending read committed before a crash is repeated using the same
tool-call ID. A committed final answer is reused. New read-only continuation input may
be appended if the process died before a complete checkpoint; partial inference may
repeat and be billed twice. Exact-once inference and cross-store atomicity are not promised.
Usage is recorded when the provider returns it; interruption before that point is unknown.

In-process metadata is bounded to 512 entries (with at most two active); expired
in-memory outcomes are evicted on new admission. Durable terminal outcomes are fetched
lazily from PostgreSQL and survive restarts. In-memory mode loses runtime history
and outcomes on restart; Django's immutable snapshot and accepted result remain. A
redispatched unfinished attempt may restart inference within its existing deadline.
Database connection loss stops the service rather than serving an undurable final answer.

The experiment intentionally uses explicit Retry for retryable failures (up to three total
attempts). Transport ambiguity reuses the same attempt. Cancel revokes Django tool access
immediately, rejects late results, and retries cancellation delivery through the dispatcher.
The runtime can still incur usage already in flight at cancellation.

## Verification and inspection

- `make infinity-smoke`: build Rust, run one real Django/Rust flow with the fake model.
- `make infinity-test`: Rust unit tests, Django contract tests, and process restart tests.
- `make infinity-check`: formatting and Clippy.
- `make infinity-logs ARGS='--execution <uuid>'`: metadata and usage only.
- `make check` and `make test`: existing application checks and regression tests.

Integration tests launch isolated subprocesses and use Django's PostgreSQL test database;
the durable test needs a local database administrator able to create a temporary restricted
login role. It removes its runtime schema and temporary role afterward. Routine tests skip
these subprocess tests unless `INFINITY_INTEGRATION=1` is set. No real provider calls are
made by default. To opt into a paid provider check, explicitly set
`INFINITY_INTEGRATION=1 INFINITY_REAL_LLM=1 INFINITY_TEST_MODEL=<model>` and `OPENAI_API_KEY`,
then run `uv run pytest tests/test_infinity_integration.py::test_real_provider_opt_in`.

The provider uses OpenAI Responses with `store=false`, strict structured output, an explicit
single function allowlist, bounded bodies/timeouts and provider error classification.
Reference: https://developers.openai.com/api/docs/guides/function-calling .

## Rollback

Set `AGENT_BACKEND=disabled` in web, worker and Beat environments and restart those
processes, then stop Rust. Existing ingestion, matching, preparation and delivery continue
on their original paths. Retain the additive execution table and runtime schema for audit;
no reverse migration or destructive cleanup is necessary. In-flight evaluations stop gaining
tool access and cannot trigger application side effects.

## Interface boundary

Rust accepts authenticated `PUT`, `GET` and `DELETE` on
`/v1/executions/<execution UUID>/<attempt UUID>` for start, status and cancellation.
The Python gateway owns the version-1 request/response contract. Requests carry model,
input hash, frozen limits, deadline, correlation ID and a short-lived context capability.
Responses carry the same IDs, lifecycle status, selection proposal, token counts and a
bounded error/retry classification. Unknown fields and stale/mismatched IDs are rejected.
Credentials and capabilities are transport data, never model arguments.

Django's `/internal/agent/v1/context/<execution UUID>/<attempt UUID>/` is a capability-only
read of one active execution snapshot. User endpoints use existing session/CSRF protection;
status fragments and cancel/retry actions always filter by the signed-in owner.

Runtime snapshots contain private confirmed facts. Protect the runtime schema, backups,
and retention with the same care as private application documents. The experiment retains
history for diagnosis; automatic retention/account-erasure synchronization is not implemented.
