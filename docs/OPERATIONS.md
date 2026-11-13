# Operations

Status: operating contracts reviewed against code, 2026-11-13. Commands are procedures,
not authorization to run them against production. [Development](DEVELOPMENT.md) covers
fake-provider tests; [milestones](MILESTONES.md) owns unfinished capabilities.

## Development and controlled upgrades

Use the root README with an isolated development database. Host commands need exported
settings; Compose loads `.env`. Private local files are default unless S3 is configured;
the local S3 emulator does not establish production IAM. Run `make worker` and one
`make beat` only when background processing is intended: existing work may resume.

For an approved container upgrade, build first, review migration compatibility and
back up valuable data before applying migrations/updating services:

```sh
docker compose build web browser-worker
docker compose run --rm --no-deps web python manage.py migrate --noinput
docker compose run --rm --no-deps web python manage.py collectstatic --noinput
docker compose up -d web worker ingestion-worker preparation-worker delivery-worker browser-worker beat
```

This resumes scheduled/approved work; it is not a harmless smoke test. Review actual
routes in settings/Compose. Control/ingestion/preparation/delivery/browser concurrency
is 1/2/2/1/1; `make worker` consumes all non-browser queues. Keep one Beat. Use actual
migration files, not historical migration numbers or `--fake-initial`. The replaced
initial schema cannot be applied to old databases without a reviewed migration path.
Never reset existing data as a setup shortcut.

Inspect `docker compose ps`, bounded logs, staff/MFA `/operations/`, Admin ingestion
history and `python manage.py crawl_report --hours 24`. `/health/` is liveness, not
readiness. Source leases expire after ten minutes; worker limits are normally 240/300
seconds. Redis uses noeviction; approved intent/retry deadlines remain in PostgreSQL.

## Integrations

Collection support and delivery support are separate. No current source flags,
counts or live vacancies are asserted here. Review identity, rights and compatibility.

- Greenhouse/Ashby/Workday inventory adapters use scoped completeness/checkpoints.
  Official-site discovery can activate recognized endpoints after identity review;
  this does not establish reuse permission.
- Comeet/BambooHR collection exists, not general automated delivery. Hosted Comeet
  lacks an independently established complete-total contract and cannot count absence
  for closure. Akeyless has a dedicated reviewed manual workflow, not generic Comeet
  handoff. Supplementary RSS/JSON-LD/HTML parser success is not inventory completeness.
- Boards do not establish catalogue company identity or authoritative closure.
  Arbeitnow/Himalayas supply jobs, not catalogue companies. Directory hiring badges
  are third-party claims. Review campaign origin/UTC-day budgets before activation.
- Cato's employer-specific Greenhouse browser route requires reviewed authority,
  exact posting identity, approved questions/answers/CV, successful provider response
  and matching confirmation route. HTTP 428 email-code continuations are bounded and
  only follow recognized rejection with unchanged content, never uncertain outcomes.
- Camunda's Ashby route requires exact schema/answers, verified upload destination,
  application and consent-survey success. Partial ambiguous success is UNKNOWN.
  Local limits currently enforce two roles/30 days and no automatic same-role reapply;
  review current employer terms. Applications outside Jobstream cannot be counted locally.
- GitLab supports reviewed dynamic questions and exclusively reserved local assistance.
  Drift, ambiguous choices and human verification require owner action. Current live
  compatibility/real delivery still need separate acceptance.
- Secret Tel Aviv uses session-bound validated CV upload before a native multipart
  submission. Approval binds current answers/terms/form fingerprint. Require a verified
  receipt, not HTTP 200 alone. There is no cover-letter field; generated message is private.

The browser worker uses non-root Playwright, namespace/seccomp sandboxing, a read-only
filesystem and ephemeral allowlisted contexts. `deploy/chromium-seccomp.json` derives
from Playwright's Apache-2.0 profile with a chroot allowance inside the user namespace.
No privileged mode, SYS_ADMIN, host IPC or disabled sandbox. Do not persist applicant
HTML, cookies, traces or screenshots. Stop for CAPTCHA/login/anti-bot barriers.
Secure hosted session streaming/resume remains unimplemented.

Inspect help for `check_cato_application`, `check_camunda_application`,
`check_gitlab_application`, `assess_portal`, `assist_application` and provider-specific
assistance before use. Diagnostics can contact real sites; assisted commands can upload/
submit and require exact approval. Never resume an application from a historical example.
UNKNOWN/CONFIRMED are never blind-retry candidates. Supported routes are employer-specific.

## Source permissions

Before enabling an endpoint record verified employer/board ownership, permission
reviewer/date/evidence, permitted metadata/description/cache/display uses, attribution,
expiry/revocation, retention/removal contact, request budget and secret handling.
No named live reuse grant was established in the audit. Public GET, robots allowance
and enabled/authoritative flags are not legal grants. Durable enforcement is pending.

Primary provider references and conditional endpoints:

- [Greenhouse](https://docs.greenhouse.io/job-board.html): public GET at
  `https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true`; application
  POST requires an employer-authorized API key. Public GET is not blanket reuse rights.
- [Ashby](https://developers.ashbyhq.com/docs/public-job-posting-api): publication at
  `https://api.ashbyhq.com/posting-api/job-board/{board}`; respect `isListed=false`.
  No generic applicant-submit grant is established by this API.
- [Comeet](https://developers.comeet.com/reference/careers-api-overview): authorized
  employer UID/token, company-scoped `/careers-api/2.0/company/{uid}/positions` and
  IP throttling. Never harvest embedded tokens or place real query credentials in
  Source URLs/logs. Design secret indirection before onboarding API credentials.
- [Lever](https://github.com/lever/postings-api): site-scoped posting pagination;
  application POST needs employer credentials. No dedicated adapter/grant here.
- Workday needs tenant/employer search-endpoint and terms review. Workable endpoint,
  rights and quotas remain unverified. Do not infer authorization from scraper guides.
- Do not systematically copy/mirror InstaSite or scrape HireMe/Job Match AI for
  production; use independent employer feeds or obtain express partnership permission.

No numeric GET quota was established from consulted provider docs. A future pilot
could propose two scans/day, one origin request in flight and five-second spacing,
subject to permission; this is not a provider entitlement. Honor Retry-After and
bounded retries. Keep closure shadowed until complete-inventory acceptance.

Company import/discovery/enrichment are staff operations. Preview `import_companies`,
`configure_career_adapters` and `enrich_companies` help; use bounded dry runs before
writes. Website ownership requires employer evidence, not merely an ATS domain.
Logo enrichment validates bounded raster images and re-encodes 256px PNG; SVG is
unsupported. Preserve curated content and old logos on failure. Discovery campaigns
default off, use UTC-day request/check budgets and fenced leases; disabling blocks
later commits, not requests already in flight. Robots approval does not establish rights.

## Gmail, AI and Infinity

Configure Gmail API, a Web OAuth client/consent screen and exact redirect URI
(`http://localhost:8000/gmail/callback/` locally, HTTPS publicly). Store client secrets
and Fernet `TOKEN_ENCRYPTION_KEY` privately. OAuth state is user/session-bound,
single-use, ten-minute-lived and uses PKCE. Key rotation needs re-encryption and a
separately secured backup. Current scopes are send + readonly; reconciliation searches
SENT by Message-ID without fetching mailbox bodies. Public release needs applicable
Google verification/assessment. Disconnect clears local grants; revoke Google-side
access separately. In-flight requests may still complete after cancellation/disconnect.

Changed sender/attachment clears approval. Explicit 429 rejections schedule bounded
unchanged-content retries; timeout/5xx/interruption after possible send is UNKNOWN.
A matching SENT record confirms transport; no match does not prove failure.

AI settings/prices are in settings and `.env.example`: shared defaults are $25/UTC-month,
200,000 tokens/UTC-day and 20 admitted paid comparisons/non-staff user/day. These are
estimated application ceilings, not provider invoice/account limits. Unpriced models
are blocked; deterministic hard blocks/cache hits do not consume paid comparison slots.
Inspect `AI_PILOT_ROLLOUT_APPROVED`, Batch/CV-representation flags and quality acceptance
before paid activation. `reconcile_agent_budgets` repairs attributable completed work,
not uncertain usage or unrelated quota totals. Inspect before valuable-data writes.

Infinity stays `AGENT_BACKEND=disabled` unless intended. Configure independent service
and context-signing secrets: only Django holds `INFINITY_TOOL_SECRET`; Rust receives
service token, provider/model/API key and optionally restricted `INFINITY_DATABASE_URL`.
Provision via `python -m scripts.provision_infinity` with authorized DB-admin access:
separate runtime schema/login, denial of Django tables, no document/auth access.
Use loopback host URLs/private Compose names; never publicly expose internal interfaces.
Remote transport/network protection is required outside trusted local development.

The `infinity` profile or `make infinity`/`make infinity-django` starts intended runtime
work; workers can resume existing attempts. Fake provider/model is an offline fixture,
not a valid assessment. Durable mode has a singleton schema lock, up to two concurrent
executions, bounded calls/tools and a 180-second deadline. In-memory mode loses outcomes;
partial inference may repeat/be billed twice. Disable backend and stop Rust at an
approved boundary for rollback; retain schema/history. Automated erasure synchronization
of private runtime history remains pending.

## Monitoring and investigation

`compose.monitoring.yaml` is opt-in Prometheus/Grafana/Loki/Alloy. Setup initializes
telemetry secrets/volumes; `make monitoring-check` validates config/rules.
`make monitoring-up` also starts business workers/Beat. For a web-only check explicitly
select web and monitoring services with `--no-deps` using the overlay. Stop with
`make monitoring-stop`, preserving every volume.

Grafana is loopback-only at http://localhost:3000 with separate authentication. The
ignored `.monitoring-secrets/grafana-admin` holds its initial password; never copy it
to chat/logs. Scrape credentials are independent of Infinity's. Loki has no HTTP auth
and stays isolated on the backend network. Alloy needs no Docker socket/host mounts.
Do not publish internal listeners. Enable the Infinity target only when intended.

Operations reads stored snapshots; missing means unavailable, older than 90 seconds
means stale. Consumer/scheduler publication indicators are point-in-time observations,
not an independent Beat heartbeat. Provider-call metadata excludes prompts/responses;
terminal call metadata expires after 30 days, not all runtime history.

Metrics count attempts/observed decoded bytes, not complete wire traffic or exactly-once
business effects. Browser uploads/submits, Gmail/SMTP and S3 SDK traffic are not fully
covered. Usage unknowns stay unknown. IDs are JSON fields, never unbounded index labels.
Bounded logs exclude credentials/bodies/unsafe exceptions/task payloads. File/collector
queues can drop/duplicate on outages; missing logs do not prove no send. Audit state
remains authoritative. Correlate Operations IDs with Grafana Investigations/Compose logs.
Alerts are local rules; external receivers remain pending.

Prometheus retains seven days or 2 GiB blocks, with extra WAL/head space needed.
Loki's seven-day compaction retention is not exact erasure or a disk quota. Use 5 GiB
as the local operating ceiling and retain 2 GiB filesystem headroom. Under pressure
stop Alloy/Loki, inspect telemetry storage and adjust capacity/retention; never delete
live Loki/database files or run `down --volumes`. Encrypt/restrict stopped-volume backups.

Edit `scripts/generate_dashboards.py`, regenerate JSON and validate with
`scripts/check_monitoring_runtime.py`; dashboard UI edits are disabled. Rotate only
intended credentials with `scripts/setup_monitoring.py --rotate grafana|scrape`;
scrape rotation needs coordinated exporter reloads. Monitoring rollback leaves business
work operational; disabling instrumentation requires controlled plain-Compose recreation,
not reverse migrations or volume deletion.

## Backup, recovery and production

Back up PostgreSQL, private objects and encryption keys separately under restricted,
encrypted access. Restore into isolation with workers/Beat/outbound sends disabled.
Verify row counts, ownership, document hashes and searches; reconcile approved,
submitting and unknown records against original/provider evidence before restarting.
A restored outbox can otherwise duplicate accepted external effects. Measure recovery
against agreed RPO/RTO; managed production recovery is not yet established.

`reset_pilot` is destructive: explicit authorization, quiesced writers, reconciled
in-flight effects and a verified private manifest from `scripts/pilot_backup.py` are
required. Inspect dry-run/help, never copy a historical `--apply`. Backups contain
personal/authentication data; restrict them and retire them explicitly after rollback
needs end. No reset is part of normal setup/upgrade.

Hosting/Terraform, region/residency, budget/capacity and retention need explicit choices.
Use protected environments, workload identity, scanned image digests, reviewed additive
migrations, rollback images, TLS/origin restrictions and tested restore. Synthetic
settings checks do not verify real IAM/proxy/bucket policy. No local proposal or
health result authorizes production deployment or infrastructure purchases.
