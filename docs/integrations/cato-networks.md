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
once, and requires both a successful application response and the matching official
confirmation route. It does not use the employer-authenticated API POST.

Changed schemas, additional required questions/consents, ambiguous cities and visible
human challenges stop for review. Explicit 429 responses schedule bounded retries with
shared Cato cooldowns; Secret Tel Aviv has an independent provider bucket. A timeout
or interruption after a possible application POST is UNKNOWN and never replayed.
Cato has no implemented authenticated read-only receipt lookup; check employer email
or the employer portal before deciding what to do with UNKNOWN applications.

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

**No live CV upload or Cato application was sent by the agent.** The remaining check is
the user's real selected CV/answers and explicit approval, followed by employer receipt
verification. Leave the company tranche pending validation before moving to the next.

Primary references: https://docs.greenhouse.io/job-board.html and
https://playwright.dev/python/docs/docker#crawling-and-scraping.

The first user-triggered attempt failed before an application POST because the SSR
form was visible before JavaScript handlers were attached. The corrected transport
checks dropdown interactivity before filling contact details. Read-only live testing
confirmed the corrected control responds; a new user-approved send is still pending.
