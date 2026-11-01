# Camunda — Ashby application adapter

Official careers: https://camunda.com/careers/all-jobs/
Official linked ATS: https://jobs.ashbyhq.com/camunda
Public inventory: https://api.ashbyhq.com/posting-api/job-board/camunda

The existing verified, authoritative Ashby source collects and normalizes vacancies
without a browser. The selected posting is `5d366525-a519-4ca9-8c13-7e461a31a1d4`,
**Senior Manager, QA Engineering**, imported locally as job 20370. Its public form is:
https://jobs.ashbyhq.com/camunda/5d366525-a519-4ca9-8c13-7e461a31a1d4/application

## Delivery

The adapter reads the same public, read-only `ApiJobPosting` question query used by
Ashby's frontend. It creates draft fields from the provider's types, labels, choices,
requiredness and consent descriptions. Contact, eligibility, salary, address and
consent answers must be supplied and reviewed; there are no synthetic production
defaults. Optional pronouns, referral and consent questions stay unanswered unless
selected. No preference-to-question inference or new saved Preferences fields are
included in this delivery. A future Preferences extension should add preferred/legal
name separately, country-specific work eligibility/status, salary range/currency,
LinkedIn, postal address and remote willingness. Employer-specific consent still
needs review against the actual wording and purpose.

The draft retains a public schema snapshot for review. Approval binds the answers,
PDF integrity and semantic schema. Ashby regenerates form-render IDs on each read;
these IDs and generated storage IDs are excluded from the fingerprint, while question paths, types, choices,
requiredness, descriptions and notices remain bound. The worker compares its current
schema and uses its current render IDs. Unknown types, conditional fields, extra file
uploads, changed consents or changed application limits require review.

Ashby saves form answers before final submission. Approval covers these intermediate
saves and the CV upload. The isolated browser worker drives the normal hosted UI,
waits for answer saves, uploads only the reviewed PDF and waits for its form binding,
then clicks Submit once. It requires the provider's explicit `FormSubmitSuccess`
response for both the application and every accompanying consent survey, without a candidate block message. A partial survey failure after a possibly accepted application is UNKNOWN. HTTP 200 alone is not confirmation.

429 responses use bounded retries and a shared Camunda cooldown. Ambiguous outcomes
after a submission POST remain UNKNOWN; no automatic resend or authenticated receipt
lookup is implemented. An explicit rejection permits a new review/approval. Do not
assume an application was sent from the approval notification alone.

The browser keeps an ephemeral context and allows only pinned public Ashby/CDN and
CAPTCHA origins. It relays the browser's upload-handle request through pinned HTTPS
so the endorsed upload URL is known before JavaScript starts uploading. The file is
sent only to that exact public AWS URL, with DNS pinned and no redirect replay.
Provider cookies are not forwarded to the upload bucket. No traces/screenshots or
candidate HTML are persisted. Public schema fixtures contain no applicant answers.

Camunda states a maximum of two applications across roles in 30 days and no reapply
to the same role within 60 days without an offer. JobStream serializes quota admission
per owner, counts queued/confirmed/uncertain sends and manually tracked applications, and rechecks at dispatch. Manual records without a submission timestamp count conservatively from their most recent update. Existing
one-application-per-owner/job and locked confirmed records are stricter than the
60-day minimum. Applications made outside JobStream cannot be counted locally and
remain subject to Camunda's own checks.

## Verification and use

```sh
docker compose exec -T browser-worker python manage.py check_camunda_application 5d366525-a519-4ca9-8c13-7e461a31a1d4 --browser
```

This check blocks all answer saves, uploads and submissions. Live read-only inspection
on 2026-11-01 confirmed the 22-field schema and interactive location suggestions.
For example, the provider returns `Tel Aviv, Tel Aviv District, Israel`; a reviewed
Location answer must exactly match a unique suggestion, rather than selecting the
first result blindly. Registration was not required at inspection time.

Deterministic tests use synthetic answers and intercepted browser responses for the
entire flow, including upload, successful submission, 429, rejection, malformed
responses, uncertain outcomes, schema drift, failed saves, provider block messages,
private/foreign upload origins and concurrent quota admission.

Live validation on 2026-11-01 confirmed application 4 using the owner's reviewed CV
and answers. Ashby returned explicit success for both the application and its consent
survey after the owner completed verification in a visible local Chromium session.
The application is confirmed/applied and must not be resent.

The ordinary headless worker stops with a human-verification-required error when a
challenge needs interaction. The visible local debugging callback is not a cloud
handoff: secure owner-only access to the same isolated browser session, session expiry,
and a resumable verification state remain to be implemented for hosted deployments.
