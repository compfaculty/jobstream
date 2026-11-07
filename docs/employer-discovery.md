# Employer discovery worker

For broad company imports before hiring verification, use the new
[free company catalogue workflow](company-discovery-sources.md). The evidence-gated
workflow below remains available for the original Brave and Pituchim campaigns.

The worker finds **companies for operator review**, independently of job ingestion.
It uses Brave Search API or the no-key Pituchim public HTML directory, checks bounded public employer pages,
and promotes companies only when a technology vacancy and employer relationship
are evidenced. It never creates Jobs or Applications or activates career endpoints.

## Setup and operation

1. Apply migrations 0048–0051 through the normal deployment workflow. They add
   discovery tables and seed **Regional technology employers** (Brave) and
   **Pituchim hiring companies** (no API key), both disabled. No existing
   companies, sources, jobs, or enabled source cohort are changed. Take the usual
   database backup before migrations. No migration has been applied to the application
   database as part of development verification; pytest uses an isolated database.
2. For the **Brave** provider only, set `BRAVE_SEARCH_API_KEY` in the server/worker environment.
   For **Pituchim**, leave that key blank and use the separate Pituchim campaign. Obtain a Brave Search
   API subscription separately and check its terms, retention rights and billing cap
   before enabling. Query/result metadata is persisted, so select a plan permitting
   the intended storage. The integration does not create accounts or subscriptions.
3. Restart the web application, control workers, ingestion workers, and Beat together.
   Existing default and `ingestion` queues are sufficient; no separate service is needed.
4. In Django admin, inspect the campaign queries, technology title phrases, and limits.
   Use only public queries: no CV facts, personal search profiles, or applicant details.
   Enable the campaign when ready. Beat checks every minute. **Request a bounded run**
   requests a manual run for an enabled campaign without bypassing daily limits.
5. Review **Employer discovery runs** and **Employer discovery leads**. Confirmed leads
   link to pending Companies. Use the normal company website attestation and
   company-by-company adapter workflow before activating collection.

Campaigns run daily, from their `next_run_at`, then at UTC midnight. Defaults cover
Israel, Ukraine, and all 27 EU member states. The UK, Switzerland and other non-member
states are excluded. Headquarters need not be in the target region. Queries rotate
across countries between searches and days; per-query result-page offsets are saved.
Editable title phrases are deterministic whole-phrase matches, not an AI classifier.
Default queries use English; coverage of local-language vacancies will be incomplete.

## Evidence and review

Search URLs are leads, never hiring evidence by themselves. Each check fetches at
most five documents, up to 2 MiB each, with the existing public HTTPS, DNS pinning,
redirect, concurrency and host-cooldown protections. Pages are parsed as data; no
JavaScript execution, authentication, challenge bypass, or instructions from pages.

Employer identity comes from structured Organization/Corporation or
JobPosting.hiringOrganization metadata containing a name and website URL. A claim
about another site requires visiting that site; external job boards additionally
require a link from the employer website to the board. Redirecting the employer
website to a different domain requires review. This is evidence for a pending
catalogue entry, **not automatic attestation of an official domain**.

Accepted evidence is either:

- Employer-site JobPosting JSON-LD with title, description, datePosted and explicit
  eligible location. Future/invalid posting dates, expired validThrough, explicit
  closed states and recognized page closure messages do not qualify. Remote jobs
  require explicit applicantLocationRequirements for a target region.
- An employer-linked Greenhouse, Ashby or Workday public collector response with an
  open, matching technology title and explicit target location. Discovery inspects
  only the bounded response/sample, not a complete inventory. Unsupported and
  unrecognized systems remain unresolved; collection and delivery adapters are later work.

Country names/codes in structured data are supported. Free-text ATS locations accept
full target country names, explicit EU wording and selected unambiguous Israeli and
Ukrainian city names. Ambiguous city-only/remote locations remain unresolved.
Current evidence means observed public hiring evidence; stale employer markup cannot
be ruled out, and the worker does not guarantee an exhaustive list of hiring employers.

Leads retain public query/offset, candidate URL, employer/career URLs, vacancy
identity/location, relationship URL, observation time, expiry when provided, kind
of evidence and bounded diagnostics. Page bodies and snippets are not persisted.
Repeated discoveries update evidence without overwriting curated company fields.
Existing catalogue name/domain identity and manual attestation rules apply.

Staff with lead-change permission can reject or edit a lead's evidence. To manually
promote an unresolved lead, record all employer, vacancy and relationship fields,
a recent observation time, and `manual` as evidence kind after inspecting those URLs.
Promotion requires lead-change plus company-add and source-add permissions, checks
that evidence is at most seven days old and unexpired, and validates regional title/
location and URL relationships again. For an unsupported hosted system, an operator may attest a vacancy under its
recorded career URL by choosing `manual`; the resulting generic endpoint stays
disabled until its adapter is implemented and reviewed. Rejected leads are not automatically
revived; rejecting a lead does not remove an existing Company.

All admin actions use Django's existing staff/MFA, model permissions and POST/CSRF
handling. Runs are read-only. Reviewers are trusted catalogue operators; there is no
per-applicant discovery dataset exposed through public endpoints.

## Budgets, retries and recovery

Daily reservations are PostgreSQL records keyed by campaign and **UTC day**. Limits
can be lowered per campaign, with hard shared ceilings across all campaigns of
**30 discovery request attempts and 100 candidate checks/day**. Manual runs, duplicate deliveries,
retries and failed attempts cannot bypass those ceilings. A reservation can be spent
without making a request if a worker is interrupted or a shared cooldown defers it.
These conservative ceilings bound requests, not a promised monetary cost.

Each task performs one search page, one robots check, one directory page, or one candidate check. A ten-minute lease with a
unique fencing token prevents duplicate work from committing stale results; the
existing five-minute hard task limit is unchanged. Query offsets and pending lead
IDs are committed between tasks. Beat republishes lost queued work after five
minutes and resumes expired leases. Unchecked candidates carry into the next run
when a budget is exhausted.

Transient network failures, 429 and selected 5xx responses retry at most three times
with exponential delay. Retry-After and Brave exhausted-window reset headers extend
shared cooldowns. Permanent failures and exhausted retries remain visible by bounded
error code, without response bodies or exception payloads. Missing Brave credentials fail
before spending budget; Pituchim never checks or uses a Brave credential. Fix configuration and request a fresh bounded run if needed.

Disable a campaign to stop further dispatch and fence pending in-flight results.
No provider operation already in flight can be recalled. Leads/evidence remain for
review; existing company/source records are preserved. Worker concurrency and budgets
are deliberately conservative and no throughput claim has been benchmarked.

## Verification

All provider traffic is mocked; no paid search, real application, notification or
email is required to test this feature. Local PostgreSQL is required for reservation,
lease and concurrent promotion tests:

```sh
DEBUG=1 uv run pytest tests/test_pituchim.py tests/test_employer_discovery.py tests/test_catalogue.py \
  tests/test_discovery.py tests/test_tasks.py tests/test_network_sources.py -q --reuse-db
```

Run `make check` and review migrations before rollout. Reversing the schema migrations
removes discovery records, so disable the campaign to roll back behavior without
losing evidence; do not reverse migrations against valuable data without approval.


## Pituchim: no-key company source

Source: [Pituchim startup directory](https://pituchim.com/startups), using its public
server-rendered HTML only. Tracking parameters are removed. Its observed robots.txt
allows `/startups` and disallows `/api/`; the worker checks robots.txt before each
page and refuses a disallow or failed rules request (404 means no robots file).
No private endpoints, embedded API credentials or browser sessions are used.

**Hiring now** is implemented by filtering `.company-hiring` badges on the HTML cards.
Inspection of the site's public JavaScript found that the UI control is client-side
and does not encode the filter in a server-rendered URL. Consequently the worker
still walks the ordinary `?page=N` pages but only queues cards marked Hiring.
Pages with zero hiring badges are valid and advance the durable cursor. A hiring
badge is a third-party claim, not proof of a current regional technology vacancy.

Only card names and public website links are extracted. Descriptions, contact
information, images and full directory datasets are not copied. The directory URL
is retained in each lead's query/provenance field, providing Pituchim attribution.
Company names/websites are identity hints for inspecting the linked site; they never
attest an official domain or activate a career endpoint. Unresolved checks retain
those hints so an operator can work through the companies later.

Each directory page requires **two** separately checkpointed request attempts
(robots.txt, then the HTML page), including failed attempts, against the same shared
30/day ceiling. The initial campaign normally checks up to 15 directory pages/day,
fewer when Brave also runs or candidate budget/backpressure intervenes. Robots
approval is scoped to the pending page and expires after one hour. Separate tasks
allow host-spacing cooldowns to defer the HTML request without fetching robots.txt
again on each retry. A five-second host cooldown follows each successful
page. Pages accept at most 60 company cards (currently observed: 48); pagination
must remain on `/startups`, advance exactly one page, and match the displayed page
count. Unexpected markup, redirects, challenges or missing pagination fail visibly;
the worker does not infer the end of the directory from a parser failure.

The final page is checkpointed before pending candidates are processed; after those
checks, the run completes and the next daily cycle restarts from page one. Pending
checks survive a daily budget stop. Campaign provider is read-only after its first
run to avoid mixing cursors from different providers; create a separate campaign
when switching providers. Existing Brave configuration and its disabled state are
preserved.

Live read-only inspection on 2026-11-07 confirmed robots rules and captured pages 1
and 2. The parser found 48 usable company links and **zero Hiring badges on each of
those two pages**; that is not evidence that later pages or the UI's filtered view
have no hiring companies. No application database writes, paid requests, employer
crawl or rollout were part of that inspection.
