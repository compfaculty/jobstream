# Cato Networks — application integration awaiting user validation

Official careers: https://www.catonetworks.com/careers/
Official linked ATS board: https://job-boards.greenhouse.io/catonetworks
Public inventory: https://boards-api.greenhouse.io/v1/boards/catonetworks/jobs?content=true

Collection uses the existing leased Greenhouse snapshot adapter and deterministic
normalization. A complete inventory imported 106 postings, including 32 with Israel
locations. The verified company owns its authoritative ATS endpoint; automatic closure
remains in shadow mode. The legacy JSON-LD source remains disabled. Secret Tel Aviv
reposts remain separate historical/source records.

The user-selected vacancy is **Automation Engineer**, posting `4937879101`:
https://www.catonetworks.com/careers/careers-post/4937879101/

The employer wrapper presents an Imperva challenge to automation. The official hosted
application works without registration:
https://job-boards.greenhouse.io/embed/job_app?for=catonetworks&token=4937879101

## Application flow

Open the imported vacancy in JobStream, prepare an application, select an owned PDF CV,
and enter first/last name, email, international phone and its ISO country code, and
current city/country. LinkedIn is optional unless the current vacancy requires it.
Saved equivalent contact answers prefill only when the owner explicitly enabled reuse
in My preferences. Referral source and demographic answers are left blank. Notes remain
private; this form does not send a cover letter.

Review the draft, attachment and answers, then approve. Approval binds their digest and
the live public question schema. The dispatcher sends Cato applications to the dedicated
`browser-delivery` queue. The Python Playwright worker drives the normal hosted UI,
resolves a unique city/country suggestion, waits for successful PDF upload, clicks Submit
once initially, and requires both a successful application response and the matching official
confirmation route. It does not use the employer-authenticated API POST.

Changed schemas, additional required questions/consents, ambiguous cities and visible
human challenges stop for review. Explicit 429 responses schedule bounded retries with
shared Cato cooldowns; Secret Tel Aviv has an independent provider bucket. A timeout
or interruption after a possible application POST is UNKNOWN and never replayed.
Cato has no implemented authenticated read-only receipt lookup; check employer email
or the employer portal before deciding what to do with UNKNOWN applications.

Greenhouse can explicitly reject the initial POST with HTTP 428 and request an email
security code. The local assisted-browser hook permits up to three code continuations
only after a recognized rejection for the approved recipient and with unchanged
application content. Codes are entered in the provider UI and never logged. An
uncertain response cannot authorize a continuation. The headless worker stops for
human verification; a cloud browser handoff is not implemented.

## Runtime and diagnostics

```sh
docker compose build web browser-worker
docker compose up -d web worker ingestion-worker preparation-worker delivery-worker browser-worker beat
docker compose run --rm --no-deps browser-worker python manage.py check_cato_application 4937879101 --browser
```

The diagnostic uses public GETs only, blocks application POSTs/uploads, and reports
schema support, browser controls and actual dropdown interactivity without applicant data. The browser worker is
non-root, uses a namespace/seccomp sandbox, a read-only filesystem and ephemeral
contexts, and allows only pinned public provider origins. No traces, screenshots,
sessions or applicant HTML are persisted. Chromium/system libraries live in the separate
browser image. Playwright is a Python dependency with its bundled driver; there is no
Node.js application service or npm build step.

## Verification boundary

Deterministic tests cover schema changes, owner-bound approval/content integrity,
provider cooldown isolation, browser dispatch, saved-answer opt-in, actual Chromium UI
fixtures, upload/confirmation, 429, rejection, uncertain POST outcomes, city ambiguity
and challenges. The live read-only check on 2026-11-01 confirmed the selected vacancy's
schema and browser controls, with no interactive challenge at inspection time.

An owner-authorized live CV upload and initial application POST on 2026-11-01 reached
the email-code verification rejection. No successful Cato receipt has been established.
Leave the company tranche pending validation until a successful response and matching
official confirmation route are observed.

Primary references: https://docs.greenhouse.io/job-board.html and
https://playwright.dev/python/docs/docker#crawling-and-scraping.

The first user-triggered attempt failed before an application POST because the SSR
form was visible before JavaScript handlers were attached. The corrected transport
checks dropdown interactivity before filling contact details. Read-only live testing
confirmed the corrected control responds; a new user-approved send is still pending.

## Deferred follow-up — 2026-11-01

Paused at the owner’s request; do not resume live sends as part of another company.
Application 3 is FAILED with `cato_human_verification_required`: the last attempt
ended after an explicit HTTP 428 email-code rejection, without a confirmed receipt.
The local browser session has expired. Camunda application 4 is confirmed and must
not be resent.

When returning to Cato, validate the real email-code continuation with the same
reviewed CV/answers in a fresh assisted session. Investigate any
`cato_reviewed_content_changed` field-name diagnostics without logging values or codes;
do not loosen the content/attachment guard blindly. Require a successful provider
response and matching official confirmation route before marking the integration
complete. Secure cloud human-verification handoff remains separate unfinished work.

Cato application 3 reached an explicit HTTP 428 email-verification rejection.
The transport now permits bounded code continuations only after a recognized rejection
for the approved email, while retaining the same application content. Unknown outcomes
never permit another POST. Equivalent phone punctuation/email casing and provider-owned
attribution metadata do not count as content changes. Diagnostics retain only changed
field names, not answer values or verification codes. A provider receipt remains the
criterion for completion; an email-code prompt alone is not a successful application.


## Pipeline recheck — 2026-11-06

Read-only runtime inspection found 115 active Cato postings, including Automation
Engineer (`greenhouse:catonetworks:4937879101`, local job 21110). The public
`check_cato_application 4937879101` diagnostic returned `schema_supported=true`.
This establishes current schema compatibility only; no upload, submission, receipt
or interactive browser compatibility was established by that check.

`test_company_inventory_to_approved_adapter` now exercises the discovery-to-delivery
seams with a fake official-page response, public inventory, schema and transport:
verified company -> authoritative Greenhouse source -> complete inventory ->
source-backed deterministic sections -> owner-bound UI draft -> explicit approval ->
fake confirmed or unknown delivery. It verifies that drafts and foreign owners cannot
send/approve and that confirmed/unknown outcomes cannot replay. Both cases passed.
The broader catalogue/discovery/adapter/submission/network suite passed 132 tests
(with 12 existing Django URLField deprecation warnings).

Live Cato delivery remains pending explicit owner review/approval. The prior deferred
application is not resumed by this verification, and the Camunda receipt must not be
resent. The AI quality benchmark and rollout flags remain separate gates.
