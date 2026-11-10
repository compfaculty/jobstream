# Company integration workflow

Track **fetching jobs** and **applying to jobs** independently for every company.
Fetching is the minimum integration milestone. Application support is a high-priority
second milestone; missing submission support must not prevent shipping a verified
fetch adapter. Reuse platform adapters wherever possible. Do not use a single
“company complete” label that hides which capability works.

## Fetch readiness

Record one of these states with evidence and any blocker:

- **Pending:** no implemented, tested collection route.
- **Implemented:** adapter and focused tests exist; runtime rollout or validation remains.
- **Ready:** configured in the target environment and a controlled collection has
  verified employer/ATS identity, job descriptions, locations where supplied,
  stable identities, normalization, deduplication and applicable pagination/budgets.
- **Blocked:** collection cannot currently work; record the specific cause and next action.

A complete inventory must not be inferred from truncation, challenges, failed
requests or partial HTML. Automatic vacancy closure is a separate capability:
a source can be fetch-ready while closure remains disabled. Regressions can move
an existing integration back to blocked; retain its last successful evidence.

## Application readiness

See the [browser-assisted application plan](browser-assisted-applications.md) for
CAPTCHA handoff, browser compatibility and provider rollout.

Record an independent state, route and blocker:

- **Unassessed:** application route has not been reviewed.
- **Pending:** route is identified, but integration work remains.
- **Implemented:** preparation/submission code and tests exist; operational
  validation remains. A recognized form alone does not qualify.
- **Ready:** the supported route can prepare the intended application, preview
  recipient/destination, content and attachments, enforce ownership and explicit
  approval, submit with duplicate protection, and distinguish confirmed success,
  failure and uncertain outcomes. Record the validation method; never imply a
  real submission was tested when only fixtures or a sandbox were used.
- **Assisted:** a verified handoff supports the user completing specified steps;
  state exactly what Jobstream prepares and what the user must do. An untested
  external apply link alone is not evidence of a working assisted integration.
- **Blocked:** the route cannot currently be completed; record the cause, such as
  CAPTCHA, access restrictions, unsupported questions or unavailable attachments.

Keep the route explicit (published application email, supported ATS submission,
or employer portal). Platform collection support does not establish submission
support for every employer on that platform. Application readiness may differ
between vacancies, locations or forms; assess each selected vacancy again before
preparation/approval/delivery. A company-level summary is not authorization.

Prioritize supported public submission mechanisms and verified published application
email where suitable, then employer-specific forms and assisted workflows. CAPTCHA
or anti-bot challenges should pause the affected application for user action where
a supported handoff exists; otherwise report a blocker. Do not automatically retry
a possibly successful submission, bypass challenges, or mark a handoff as sent
without delivery evidence. A challenge in applying does not downgrade working
fetching; a challenge in fetching does block fetch readiness.

For both tracks, record relevant tests, last validation and remaining limitations.
No live applications, emails or notifications are authorized by readiness work.
Read-only live inspection is allowed. Live submissions require the user's selected
vacancies, CV and explicit approval; never invent an applicant or silently send
test applications. Do not commit changes unless the user requests it.

These are project acceptance and reporting criteria. They do not introduce new
persisted model fields, admin badges or delivery state transitions. The existing
`Source.needs_adapter` flag tracks collection implementation backlog only; clearing
it does not establish runtime fetch readiness or any application capability.

Target workflow: the user selects jobs and a truthful base CV/profile, approves one
batch, and supported integrations prepare/send eligible applications concurrently.
Current delivery approval remains per prepared application; automatic batch consent is
future work, not implicitly enabled by the matching/preferences changes.

Application preparation fit-screen policy (existing behavior):

- Explicit per-user excluded technologies block only mandatory requirements. In
  “Python, Node.js, or Go”, excluding Node.js leaves Python/Go as alternatives.
- At least one allowed CV technology overlap is enough to consider applying; do not
  require the entire advertised stack. Zero overlap skips the job. This is an
  intentionally permissive technology screen, not a claim that every requirement is met.
- Preferred-only technologies do not block. Ambiguous requirements or unreadable CVs
  pause for review. CV facts remain unchanged; tailoring may reorder/emphasize facts.
- New HTML batches enable CV-fit screening by default. `check_fit` is an explicit
  batch API flag, defaulting off for compatibility with existing manual-review clients.
  Saved exclusions always apply, including approval and queued-send preflight.
- Skips retain reasons and matched/excluded labels. Rechecking fit never approves or
  sends an application. A new batch/profile is needed when changing immutable CV facts.

The percentage-evaluation/recommendation path uses a stricter exclusion policy:
any excluded technology mention, including optional mentions and alternatives, blocks
the evaluation before inference. See [AI pipeline](ai-pipeline.md). These are distinct
existing paths; passing the preparation fit screen does not establish a current
recommendation or override an evaluation block. Do not silently change either policy
while adding company adapters.

My preferences now stores common application answers in an owner-bound encrypted
record, only on explicit save. Reuse is separately opt-in. Secret Tel Aviv contact
fields and Cato contact/location/LinkedIn fields are connected; other provider/question mappings remain company-by-company
work. Reuse only when meaning, options and purpose match. Do not infer
sex/gender or other demographic facts. Keep answers owner-private and editable/deletable;
company-specific consent or changed questions must prompt again. This does not permit
transferring employer API credentials into JobStream or bypassing security challenges.


My preferences includes wanted locations/titles/technologies, mandatory technology
exclusions and explicit-only remote/location/title/technology limits. Defaults are
soft and recorded in match evidence; public search ordering is unchanged. Hard
limits are checked in preparation and again before approval/sending. Known Israeli
city aliases support country preferences; location matching is text/alias based,
not a worldwide geocoder. Unknown remote eligibility pauses rather than claiming
that an office-only job is remote.
