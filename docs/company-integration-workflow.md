# Company integration workflow

Checkpoint: 05fccbf — company-first collection, lifecycle tracking, admin filters and
Jazzmin. Company-by-company work starts with Cato Networks; see its integration note.
Commit each completed company integration before moving to the next. Reuse platform
adapters wherever possible, rather than writing a separate scraper for every employer.

Completion requires verified employer/ATS identity, full bounded/resumable collection,
normalization with fixtures, an identified and implemented application route, retries
and uncertain-outcome handling, ownership/approval checks, and recorded test evidence.
Read-only live inspection is allowed. Live submissions require the user's selected
vacancies, CV and approval; never invent an applicant or silently send test applications.
If an application route still needs implementation, leave the company in progress.

Target workflow: the user selects jobs and a truthful base CV/profile, approves one
batch, and supported integrations prepare/send eligible applications concurrently.
Current delivery approval remains per prepared application; automatic batch consent is
future work, not implicitly enabled by the matching/preferences changes.

Accepted matching policy:

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
