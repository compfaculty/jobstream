# Browser-assisted applications and reliable site access

Implementation plan and checkpoint, 2026-11-10. The first local assisted milestone
is implemented below. Hosted browser streaming and universal site support remain
future work. This document does not authorize live submissions or deployment.

## Objective

Keep fetching independently ready while making application preparation and delivery
reliable on supported employer sites. Support human verification in the original
browser session. Do not promise that automation is indistinguishable from a person:
using a real browser does not guarantee acceptance by an employer's risk controls.

Use the right transport for each integration:

- Documented APIs, feeds, email/OAuth and internal services use their supported
  protocol clients. Browser impersonation would add cost and failure modes here.
- Public static career pages retain bounded HTTP fetching when it works.
- JavaScript-dependent pages and supported applicant forms use real browser pages
  and the site's normal form, upload and navigation behavior.
- A challenge pauses the affected operation for a supported human handoff or records
  a blocker. A 403 alone does not establish CAPTCHA; distinguish authentication,
  permission, rate limiting, unavailable content and security challenges.

## What exists

`network.py` provides public HTTPS fetching with an explicit Jobstream user agent,
byte/time/redirect limits and destination checks. Cato, GitLab and Camunda/Ashby
have provider-specific browser paths, isolated contexts and guarded requests.
GitLab already exposes visible local Chromium through `assist_gitlab_application`
and `on_human_verification`, tied to approved assisted delivery. Tests cover owner
checks, worker exclusion and duplicate prevention. This is evidence of implemented
support, not proof of acceptance by every live employer challenge.

## Handoff architecture

Prefer shared control of the same live browser page over copying a CAPTCHA div,
iframe or token into Jobstream. Origin restrictions, page callbacks and browser
state make copied fragments unreliable. Verification tokens are short-lived and
single-use; the original form should receive them through its normal integration.
The employer, not Jobstream, owns the CAPTCHA verification secret.

First deliver a local workflow: Jobstream opens an isolated visible Chromium window,
prepares supported fields and attachments, and yields control when needed. The
user completes the challenge directly on the employer page. Keep that context and
network path alive through completion; do not transfer the solution to another
browser, machine or HTTP session. Challenge disappearance is not a delivery receipt.

A later hosted workflow could stream the full isolated browser view and forward
user input to it. This is remote browser control, not TLS interception or an HTML
reverse proxy. It requires authenticated owner-bound access, short-lived connection
tickets, encryption in transit, session expiry, strict resource limits, and isolation
from other users, Jobstream credentials and internal networks. Never expose a raw
browser debugging endpoint. Prefer the local version until hosted access is needed
and its operational cost is justified.

## Implementation sequence

1. **Inventory and baseline.** Classify outbound integrations by API, static HTML,
   rendered page and form delivery. Record transport, provider, existing safeguards,
   supported application route and sanitized failure category. Measure challenge
   frequency, successful fetches, confirmed deliveries and human interventions.
   Record browser version and environment without capturing tokens or applicant data.
2. **Reuse the GitLab assisted contract.** Extract only the shared lifecycle needed
   by the next provider: exclusive session ownership, human-required notification,
   bounded waiting, cancellation and cleanup. Keep provider-specific schemas,
   destination rules, approval checks and receipts in their existing adapters.
   Extend Cato and Camunda/Ashby individually after checking their actual forms.
3. **Preserve approval and delivery invariants.** Distinguish preparing, waiting for
   the user, resuming, submitting and confirmed/failed/unknown outcomes. Treat these
   as proposed lifecycle concepts; design their mapping to existing Application and
   SubmissionAttempt states before changing schema. Reserve one attempt, prevent
   workers from claiming an assisted application, and prevent parallel submissions.
   Recheck owner, approved answers, attachment hash, destination and form schema on
   resume. Changes require review/reapproval. Solving CAPTCHA does not grant consent.
   After a submission may have started, uncertain outcomes require reconciliation,
   not automatic replay. Decide explicitly whether the user or adapter owns the
   final submit action; direct user clicks must also be observed by the attempt gate.
4. **Improve compatibility with evidence.** Keep browser versions supported, normal
   browser-generated requests, valid JavaScript/assets and a consistent session.
   Follow actual page readiness and Retry-After; bound concurrency per provider.
   Review request interception and service-worker blocking against provider needs.
   Do not disable Chromium sandboxing, TLS validation, SSRF checks or destination
   controls to make a site load. Where interception changes behavior, evaluate a
   restricted network boundary before adjusting it. API headers should follow API
   contracts; do not paste browser fingerprints or fabricated headers everywhere.
5. **Make human-required actionable.** Show which application needs attention, why,
   remaining session time, and a local-browser action. Show expiry/cancellation and
   whether any send may have happened. A terminal-only GitLab command remains the
   initial implementation; a general web UI and hosted streaming are separate work.
6. **Verify, then roll out per provider.** Use controlled forms and provider test
   challenge modes where available. Test human success/failure/expiry, repeated
   challenges, changed schema, changed approved payload, cross-user access,
   cancellation, worker races, browser crashes before/after POST, duplicate clicks,
   unknown outcomes, blocked destinations and cleanup. Fixtures must not submit to
   real employers. Live read-only inspection cannot establish delivery success;
   real submission needs explicit approval for a selected application.

Start with one existing assisted GitLab regression baseline, then Cato as the next
candidate after a read-only compatibility check. Do not launch a generic all-sites
browser agent or replace all HTTP integrations. Roll back each provider independently
to its existing manual/unsupported state if assisted delivery fails verification.

## Privacy, operations and acceptance

Cookies, session state, CVs, answers and page contents are sensitive. Do not log or
record browser sessions by default, share everyday personal browser profiles, or
persist session credentials unnecessarily. Specify retention and deletion before
adding resumable persisted browser profiles. Treat employer pages as untrusted;
never follow embedded instructions to access unrelated files, secrets or destinations.
Use a dedicated bounded session runner rather than leaving unlimited worker tasks
waiting for a human. Fit deadlines to the existing lease/recovery behavior before
extending session lifetimes; a database checkpoint cannot revive expired page tokens.

Acceptance is measured per provider: correct employer/job and approved payload,
successful human handoff where supported, no cross-user access, no duplicate send,
receipt-backed confirmation, explicit unknown/blocked states, and bounded session
cost. Some employers may still reject automated sessions after human verification;
retain a manual route and report that limitation rather than promising universal access.

## Provider references

- [Google reCAPTCHA verification](https://developers.google.com/recaptcha/docs/verify):
  response tokens expire after two minutes, are single-use and report the hostname.
- [Cloudflare Turnstile verification](https://developers.cloudflare.com/turnstile/get-started/server-side-validation/):
  tokens expire after five minutes and are single-use; validation can check hostname/action.
- [Playwright browser contexts](https://playwright.dev/python/docs/browser-contexts):
  isolated contexts separate cookies and storage. Preserve one context for an active
  handoff while isolating different users and attempts.


## Implemented local milestone — 2026-11-10

- Cato and Camunda now accept explicit assisted approval, alongside GitLab. Their
  delivery providers pass the human callback into visible isolated Chromium.
- Application pages offer local assisted reservation for those three providers.
  GitLab retains its checked default; Cato and Camunda retain ordinary delivery
  unless the user selects assisted mode. No other employer is newly supported.
- `assist_application` starts the provider-appropriate approved local attempt;
  `assist_gitlab_application` remains a compatibility alias. Workers cannot claim
  reserved applications. Owner checks, approval digests, CV validation, quotas,
  stale recovery and outbox confirmation remain in the existing submission service.
- Cato/Camunda verification waits use the original context and a ten-minute total
  session budget. Visible applicant controls are compared around pre-submission
  human handoff; changes fail review rather than silently altering an application.
  Verification tokens/codes are excluded, and snapshots are never logged. Logs
  record provider and successful human-wait duration only. Cato's existing post-send
  email verification gate continues to freeze the initial submission payload and
  permit only bounded explicit verification continuations.
- Expiry before final submission fails the attempt. A possibly transmitted send
  retains an unknown outcome when confirmation is unavailable. Browser closure or
  Ctrl-C is not a promise that nothing was sent; review the recorded outcome.
- No CAPTCHA fragments, tokens, everyday browser profiles, hosted browser service,
  stealth fingerprinting or API impersonation are introduced. Existing sandbox,
  origin/destination checks, TLS validation and network restrictions remain enabled.

### Using local assisted delivery

Review the application, CV and destination in Jobstream, select **Reserve for local
assisted delivery**, then approve that application. On the local desktop, using the
configured development environment, run:

```sh
uv run --env-file .env python manage.py assist_application APPLICATION_ID --owner-id OWNER_ID
```

Chromium must be installed and a graphical desktop available. Complete verification
in the opened employer page. Do not edit applicant fields or click Submit while the
adapter is preparing the application. The adapter owns the final submit action;
existing Cato/GitLab verification dialogs may require their explicit continuation.
Inspect Jobstream's final confirmed/failed/unknown state; the browser closing or a
challenge disappearing is not a delivery receipt. Do not rerun an uncertain send.

This milestone uses a dedicated local command process, not a Celery worker waiting
for human input. No new database migration is required for assisted delivery. The
separate collection-adapter migration remains unapplied to the runtime database.

Remaining work: provider-specific live compatibility checks without submission,
user-approved real delivery validation when requested, additional employer adapters,
web-visible live session progress/cancellation, and hosted remote browser control
if its cost and isolation requirements are accepted. No claim is made that assisted
Chromium will be accepted by every employer's anti-bot system.

### Verification evidence

The final targeted suite passed **173 tests**, including real Chromium with all
employer traffic intercepted, PostgreSQL race tests, approval/ownership failures,
changed CV/answers/mode, worker exclusion, human completion/expiry, challenges
before filling and after upload, duplicate attempts, and batch/outbox regressions.
The concurrent Camunda test initially reproduced a deadlock: an owner `FOR UPDATE`
quota lock blocked completion audit foreign-key references while another transaction
held the application lock. Owner coordination in submissions, Camunda and batches
now uses `FOR NO KEY UPDATE`. Quota decisions still serialize; references can
complete. The race and final suite passed after the fix.

Django `check`, migration consistency, changed-file Ruff security/lint rules,
full-project formatting, diff whitespace and the repository's pinned Gitleaks scan
passed. The scoped mypy command still reports the two earlier nullable-company
`application_email` errors in unchanged model code; full-repository Ruff also has
the earlier migration `0004` long line. No new dependencies were added. No live
employer submissions, anti-bot acceptance tests, dependency audit, deployment checks,
container restarts or production rollout were performed for this milestone.
