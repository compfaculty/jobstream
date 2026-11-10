# Public career adapter rollout — 2026-11-10

Readiness is tracked independently under the
[project integration criteria](company-integration-workflow.md):

- **Fetching:** nine sources are **implemented** and configured in the local runtime;
  controlled collection validation remains before being called **ready**.
- **Applying:** **unassessed** for these nine integrations in this change. No new
  automatic or assisted application workflow was implemented or verified.
- **Flexor fetching:** **blocked**, pending a verified accessible endpoint.
  Its application route is **unassessed**.

Nine of the ten sources marked `needs_adapter` have reviewed collection routes:

- proteanTecs: hosted Comeet board `proteantecs/D5.00E`.
- 4M Analytics: Ashby board `4M`; the employer page also advertises Comeet
  `B6.00F`, whose public board was empty during inspection. Ashby returned six jobs.
- Alice: hosted Comeet board `alice/D5.005`. The employer's legacy ActiveFence
  configuration uses the same company UID; current hosted postings identify Alice.
- Alviere: public BambooHR list and individual detail endpoints.
- EX.CO: hosted Comeet board `exco/F0.005`.
- Faye: hosted Comeet board `withfaye/87.00A`.
- Firebolt: hosted Comeet board `firebolt/26.004`.
- Foundational: employer-owned career links and description sections.
- G2 Risk Solutions (source still named EverC): Comeet `g2risksolutions/C9.003`.

Flexor remains pending: its employer site returned HTTP 403 and the short Comeet
link lacks a company UID and returns 404. Do not guess a board identity or clear
its backlog flag without verified evidence.

## Applying the configuration

Use the normal project environment. First ship the code and choice-only migration
`0005_public_career_adapters`; then preview and apply the mappings:

```sh
uv run --env-file .env python manage.py configure_career_adapters
uv run --env-file .env python manage.py configure_career_adapters --apply
```

The command changes only matching pending sources, runs atomically, rejects endpoint
collisions, and never enables collection, authority, or automatic closure. It retains
original career URLs as discovery evidence when absent. Repeated application is a no-op.
At the implementation checkpoint, the command had only been previewed. The later
authorized local rollout below applied the configuration without enabling collection.

For rollback, disable any subsequently enabled source before restoring its previous
URL/adapter and `needs_adapter=True`. Existing jobs and approval records are unaffected.

## Boundaries and limitations

These are collection integrations; application submission remains separate work.
No new submission handler, consent, or automatic sending is enabled. The hosted
Comeet and BambooHR inventories do not establish an atomic complete snapshot and
cannot trigger vacancy closure. BambooHR detail requests are capped at ten per
checkpoint; the list hash detects list changes between batches. Foundational reuses
the existing HTML adapter enrichment behavior (up to ten detail pages per run);
its inspected page contained one vacancy. Provider/page errors fail collection
rather than turning challenges or malformed inventories into authoritative emptiness.

Public fixtures contain only vacancy data and minimal HTML; provider widget tokens
and applicant data are excluded. Tests block external Python network access.

Validation: parser fixtures, malformed/foreign identities, compound Comeet IDs,
unknown locations, checkpoint changes, bounded pagination, endpoint restrictions,
configuration preview/application/idempotency/collision rollback, existing Akeyless,
discovery, readiness and ingestion tests. No authentication or delivery boundary changed.
Full-project Ruff currently reports an existing long line in migration
`0004_company_application_email.py`; changed-file lint passes.

## Verification results

- `tests/test_public_career_adapters.py`: 30 passed, including database ingestion.
- Focused existing adapter/discovery/readiness/ingestion suite with local AI feature
  flags disabled: 91 passed, one known failure (`test_search_query_budget`: three
  queries versus two expected). The subsequently added BambooHR ingestion test
  passed separately. Earlier runs with `.env` AI flags inherited also exposed
  configuration-dependent normalization failures; disabling both flags isolated
  those tests without changing application settings or weakening assertions.
- Django `check` and `makemigrations --check --dry-run`: passed.
- Changed-file Ruff lint, full-project format check, `git diff --check`: passed.
- `scripts/scan_secrets.py` (pinned Gitleaks container): no leaks found.
- Existing scoped mypy check reports two nullable `Company.application_email`
  errors in unchanged model code. Full-project lint reports the pre-existing
  migration long line noted above. These were left outside the adapter change.
- No dependency changes; no separate dependency audit or production deployment
  check was run for this collection-only change.


## Local rollout — 2026-11-10

At the user's explicit request, rebuilt the application and browser images and
restarted the existing 16-service local stack with its monitoring overlay. All
services are running; configured infrastructure health checks are healthy.
Migration `0005_public_career_adapters` applied successfully. The configuration
command updated nine sources, all still disabled, non-authoritative and with
closure disabled. A second preview reports zero updates. Only Flexor remains
`needs_adapter=True`. No new source was enabled or manually crawled.

The combined collection/assisted-delivery regression suite passed 203 tests.
Runtime Django checks and migration consistency passed. Web health and login
returned 200, admin redirected to login, Grafana health returned 200, all ten
Prometheus targets are up, and sandboxed Chromium launched in the rebuilt browser
container using a local synthetic page. There were zero applications before and
after configuration. Existing workers and Beat were restored to their prior running
state; their normal scheduling resumes. No real application or email was sent by
this rollout, and no database/storage volumes were reset or removed.
