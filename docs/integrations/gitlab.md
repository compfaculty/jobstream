# GitLab — Greenhouse

## Task status: needs verification

Implementation is checkpointed; real end-to-end application delivery is still unchecked.
Resume with local job [20407](http://localhost:8000/jobs/20407/).

- [ ] Select the applicant's CV and enter factual role-specific answers in JobStream.
- [ ] Review and explicitly approve the application for local assisted delivery.
- [ ] Run the displayed assisted command and complete any human verification.
- [ ] Verify the employer confirmation, saved receipt URL and Applied tracking state.

No real GitLab application has been sent. Never resend a confirmed or unknown attempt.


Official careers: https://about.gitlab.com/jobs/all-jobs/
Official linked ATS: https://job-boards.greenhouse.io/gitlab
Public inventory: https://boards-api.greenhouse.io/v1/boards/gitlab/jobs?content=true

## Collection and normalization

The official careers page renders links to GitLab's Greenhouse postings. Its static
headline can say zero jobs even when populated departments render after JavaScript.
Use the public Greenhouse inventory through the existing HTTP collector; no browser
or AI is needed for collection. Stable identities are `greenhouse:gitlab:<posting>`.
Existing deterministic normalization, endpoint observations and complete-inventory
lifecycle handling apply unchanged. Automatic inferred closure stays disabled for
this cohort until shadow review. New seed entries stay disabled until authority is
verified; seeding never changes existing operator settings.

On 2026-11-02 the local verified company 18 and authoritative source 54 were already
linked and enabled, with 238 imported postings and a successful complete inventory.
The source covers all locations and roles on a daily schedule with jitter.

## Application flow

Use Prepare application, choose a CV, and fill the current role's questions in
JobStream. The adapter supports text, textarea and single-choice fields from the
public schema. Required answers have no defaults. In particular sponsorship,
employment restrictions, prior employment and residence are factual user answers,
not inferred from CV keywords. Optional cover letters and voluntary demographic
surveys remain unanswered. Additional file uploads, location schemas, required
compliance questions, processing/retention consent or unsupported field types stop
for review. These are explicit limits, not silent omission of required questions.

Approval binds the reviewed schema, answers and CV digest. The worker fetches the
current schema again, verifies the native browser form matches, fills the normal
hosted UI and uploads only the approved PDF. Notes remain private. The separate
sandboxed browser queue uses pinned public provider origins and an independent
GitLab spacing/cooldown bucket. Background delivery permits one final POST. Local
assisted delivery permits bounded verification follow-ups only after a recognized explicit rejection, with identical
applicant content and the same uploaded CV. Only a successful response plus the exact
official job confirmation route establishes confirmation.
An uncertain send is UNKNOWN and is never replayed automatically. Explicit 429
responses use the existing bounded retry machinery and shared provider cooldown.

Background delivery stops on a CAPTCHA or email-code rejection. For local assisted
delivery, leave **Reserve for local assisted delivery** checked when approving the
reviewed application. This reservation is bound into the approval digest and excludes
both the scheduler and already-queued background tasks. Editing clears it and approval.

Run the following on the local desktop (not inside the headless worker), with the same
exported `.env` settings described in README and Chromium installed:

```sh
uv run playwright install chromium
uv run python manage.py assist_gitlab_application APPLICATION_ID --owner-id OWNER_ID
```

Use the IDs shown on the approved application's page. Owner identity is a local
operator assertion, not a replacement for the authenticated UI approval. The command
requires trusted local access to JobStream's environment/database. It never approves
or invents applicant data. Complete CAPTCHA or email-code prompts directly in the
visible Chromium window; do not enter codes in the terminal. The same ephemeral
session remains open for up to ten minutes. There is no mailbox read, CAPTCHA bypass,
code guessing, persisted verification code, or cloud browser handoff.

Only the recognized provider verification-rejection protocol permits follow-up POSTs:
the recipient must match the approved email, applicant content and original uploaded
CV must remain identical, and at most three code attempts are allowed. Expired/locked
codes stop for review. Uncertain requests never replay. A 429 leaves the assisted
reservation intact; rerun the command after the provider cooldown. Closing Chromium
or interrupting the command requires outcome review; Ctrl-C conservatively records
UNKNOWN. Interrupted assisted sessions become UNKNOWN after fifteen minutes, allowing
cleanup time beyond the ten-minute browser deadline. Confirmation records the exact
receipt URL and marks previously interested applications applied.

Cato's deferred verification work remains untouched.

## Diagnostics and validation

```sh
docker compose exec -T browser-worker python manage.py check_gitlab_application 8704363002 --browser
```

This diagnostic blocks all form writes, uploads and submissions. The initial role
is Backend Engineer (Python), AI Engineering: Agent Foundations, local job 20407.
Its advertised locations are Canada, Poland and the UK; it is an adapter test target,
not a recommendation to submit from another location or invent eligibility.

Deterministic tests use synthetic applicants and fully intercepted HTTP responses.
The public schema fixture contains questions only, with no candidate information.
No real GitLab application has been sent during implementation. A real approved send
and employer receipt remain the live end-to-end verification boundary.
