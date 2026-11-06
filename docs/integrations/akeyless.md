# Akeyless / Comeet — collection and manual application handoff

Checkpoint: `feat/akeyless-comeet-integration`, based on `4032da1`. No activation,
deployment or live application is part of this tranche. Automated
application delivery remains **incomplete**; the supported route is manual handoff.

## Public evidence — 2026-11-06

- Official careers: https://www.akeyless.io/careers/. Read-only web inspection linked
  employer vacancies to the Comeet board `akeyless/27.006`. A subsequent direct GET
  returned HTTP 403; no challenge bypass was attempted. The official domain must
  still be operator-attested in Jobstream before treating an imported job as verified.
- Hosted board: https://www.comeet.com/jobs/akeyless/27.006. A bounded public GET
  returned HTTP 200, 98,073 bytes and nine `COMPANY_POSITIONS_DATA` records.
- Reviewed vacancy: https://www.comeet.com/jobs/akeyless/27.006/automation-infrastructure-engineer/EC.A6C.
  A bounded public GET returned HTTP 200, 125,107 bytes. `POSITION_DATA.uid` was
  `EC.A6C`; the page had `RECAPTCHA_ENABLED = true` and a `comeet-applyform`
  JavaScript widget. Position flags `is_consent_needed` and
  `is_sms_consent_needed` were false. These flags do not establish the widget's
  current questions, required answers or complete consent schema.

No employer API credentials, widget tokens, applicant information or uploads were
used. No application POST was attempted. There is no confirmed live receipt, fake
provider receipt, or automated-delivery claim. A supported automated submission
contract and meaningful question schema have not been established. The current
widget, questions, consents and human challenge must be reviewed on the employer
site by the applicant. Do not infer answers from the posting or those consent flags.

## Collection and review

Official links to this exact Comeet board create an idempotent **disabled, pending**
`akeyless` source proposal. Unlike existing Greenhouse/Ashby/Workday discovery,
this route never automatically activates a new source. Review employer identity,
board relationship and source configuration before activation. No runtime catalogue
records were changed; the existing enabled cohort is untouched.

Configure a reviewed hosted source with adapter `akeyless` and board URL
`https://www.comeet.com/jobs/akeyless/27.006`. Existing legacy Akeyless careers-page
sources retain their previous parser; their URLs and operator settings are not
rewritten. The existing `comeet` JSON-array API adapter is also unchanged.

The hosted parser decodes embedded JSON without evaluating JavaScript. It validates
board/posting identities, required details and public destinations, rejects duplicate
IDs and malformed/over-budget inventories, and supplies stable keys such as
`comeet:27.006:EC.A6C`. Descriptions preserve text boundaries and provider section
labels for deterministic normalization. Provider application email aliases are not
selected as email recipients. AI enrichment is unnecessary and remains optional.

Collection uses existing HTTPS/DNS/redirect protections, response/item ceilings,
request leases and cooldowns. Persistence is bounded to 250 jobs per batch, with a
normalized posting-content hash fencing resumed snapshots (ignoring volatile widget HTML). HTTP 429/503 retains durable checkpoints;
changed/malformed inventories discard the cycle. Jobs, curated overrides, application
IDs and lifecycle history use the existing ingestion machinery.

The HTML has no independently verified total or complete-inventory contract. Every
hosted collection therefore reports `complete=False`, including empty inventories
and the final batch. A syntactically valid provider-side omission cannot be detected.
No absence-based closure is permitted from this route, even if an operator changes
`closure_enabled`. Keep that flag off; a future closure rollout needs stronger
inventory evidence. Parsing uses O(response bytes + postings) memory/time before
existing per-batch normalization/database work; no performance improvement is claimed.

## Application behavior and security boundaries

Verified provider keys are traced through the owned draft UI to the exact hosted
vacancy. Recognition requires an attested Akeyless official domain, a matching
company-owned authoritative reviewed source, the expected board, and agreement
between URL and provider key. Drafts require an owned CV and display a private-note
label and explicit manual-application notice. No applicant answers or consents are
prefilled or saved for Comeet.

Preparation batches retain `needs_action / manual_portal_required`. Approval fails
closed because Comeet is absent from the delivery adapter registry; duplicate worker
calls cannot dispatch it. The user opens the employer form, supplies factual answers,
reviews current questions/consents, uploads there, and updates private tracking after
submission. Tracking does not manufacture a provider-confirmed receipt. Existing
approval digests, UNKNOWN handling, fit policies and other delivery adapters are
unchanged.

## Verification

Commands use `DEBUG=1 UV_CACHE_DIR=/tmp/jobstream-uv` locally, without loading `.env`.
Database/browser tests require local PostgreSQL and were run with sandbox permission
for loopback access. All Python provider traffic is blocked by the test network guard;
the Chromium test blocks external requests too.

Final combined run (supersedes intermediate focused runs):

```sh
DEBUG=1 UV_CACHE_DIR=/tmp/jobstream-uv uv run pytest \
  tests/test_akeyless_comeet.py tests/test_catalogue.py tests/test_ingestion.py \
  tests/test_legacy_sources.py tests/test_normalization.py tests/test_submissions.py \
  tests/test_batches.py tests/test_ats.py tests/test_discovery.py \
  tests/test_network_sources.py tests/test_network_guard.py \
  tests/test_cato_greenhouse.py tests/test_gitlab_greenhouse.py \
  tests/test_camunda_ashby.py -q --tb=short --reuse-db --browser chromium
```

**210 passed**, including intercepted Chromium; 12 existing Django URLField warnings
and one pytest-playwright fixture deprecation warning.

Focused `ruff check` (including security rules) and `ruff format --check` passed for
all nine changed Python files. `DEBUG=1 uv run python manage.py check` reported no
issues. `DEBUG=1 uv run python manage.py makemigrations --check --dry-run` reported
no changes, including the rerun with local database access after the sandbox blocked
the initial history check. `git diff --check` passed. No migrations or dependencies
were introduced.

The focused regressions cover reviewed public descriptions, mandatory/preferred
sections, years and technology evidence; synthetic alternatives/negation cases;
malformed/truncated/ambiguous data; URL identity validation; bounded resumption;
429 retention and changed snapshots; disabled discovery; owner-only CV/draft access;
manual approval rejection; duplicate preparation/delivery calls; and cancellation.
Public fixtures retain only job fields needed by the parser, including a general-interest
position with an external active-page shortener and null optional Requirements. The
collector always prefers the verified hosted vacancy and never follows that shortener.
Fixtures omit widget tokens,
email aliases, street/arrival details and unrelated scripts.

The captured public board was also parsed and deterministically normalized offline:
all nine postings succeeded, with no database writes or AI calls.

No production
settings check, full-project test run, dependency audit, standalone SAST or secrets
scanner was run: this change adds no dependency, credential, deployment configuration
or automated submission surface. Focused Ruff security rules and network/authorization
regressions are the security checks for this tranche.
