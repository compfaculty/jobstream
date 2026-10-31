# Batch applications

Select jobs, click Apply, choose an owned PDF and an optional reviewed factual
profile, then prepare. Review the generated message, recipient and downloadable CV
before approving selected items together. Preparation never sends. Existing
applications are linked without modification.

## Factual profile and tailoring

From Documents, open **Review facts for tailoring**. PDF text is extracted as an
editable starting point. Review it, organize paragraphs into sections, and confirm
accuracy. Keep employers, roles, dates and achievements together in each paragraph.
Add real experience omitted from the original PDF, including AI work. Each save
creates an immutable profile version; older batches keep their original version.

A profile must belong to the selected base document and user. Without a profile,
preparation uses the original PDF. With a profile, each vacancy gets a generated PDF
and professional message quoting confirmed experience. Deterministic tailoring
reorders paragraphs within sections by keyword relevance. All confirmed paragraphs
are retained without changing their factual wording. Job snapshots, profile IDs,
selected fact IDs and generated-document hashes provide provenance.

Optional OpenAI assistance ranks confirmed fact IDs semantically, chooses message
highlights and flags unmatched requirements quoted from the vacancy. AI cannot
insert a new factual claim into the PDF. Review is still required. Configure
OPENAI_API_KEY and OPENAI_MODEL locally; no default model is imposed. Check the AI
checkbox per batch to authorize sending career facts and vacancy text to OpenAI.
Name/contact fields and original PDF bytes are not sent separately; factual text can
contain personal information. Requests use Responses structured output and store=false.
See https://developers.openai.com/api/docs/guides/structured-outputs.

Owner-scoped caching avoids repeat calls for identical inputs, model and version.
Input characters, output tokens and conservative daily token reservations are bounded
by AI_MAX_INPUT_CHARACTERS, AI_MAX_OUTPUT_TOKENS and AI_DAILY_TOKEN_BUDGET. Reservations
are intentionally not refunded after failed calls; this is a resource ceiling, not
an exact billing estimate. Missing/invalid credentials or unsupported output require
action; temporary failures and rate limits schedule bounded preparation retries.
No live provider request or email was sent during implementation tests.

The default embedded font is Bitstream Vera. Configure CV_FONT_PATH with a licensed
Unicode TTF when needed. Unsupported glyphs fail visibly rather than producing an
unreadable PDF. Source PDFs are preserved. Generated PDFs are stored privately.

## Durable processing and delivery

PostgreSQL holds batch items, snapshots, attempts, leases and dispatch reservations.
Celery processes different items concurrently without holding row locks during PDF
or AI work. Interrupted preparation is recovered after its lease; three claims are
the preparation retry budget. Lost broker publications are recovered by Beat.

Jobs without an email channel still receive downloadable prepared materials and
need manual portal completion. Published recruiting contacts retain source evidence; only explicit application
instructions automatically select apply_email. Operator-set addresses are preserved. Cancellation stops pending preparation
and approved-but-unclaimed sends; already submitting/confirmed/unknown applications
retain their actual lifecycle. Editing content invalidates approval.

Gmail explicit 429 responses schedule a durable retry retaining exact content and
Message-ID. Delay respects numeric/HTTP-date Retry-After and exponential backoff with jitter; five
rejections exhaust the budget. A separate retry deadline blocks early duplicate
tasks. Database-coordinated spacing is shared across workers, with configurable
provider/account/domain intervals. A 429 delays other sends from the same account.
Ambiguous outcomes are reconciled without automatic resending.

## API

POST /api/v1/batches/ accepts document, jobs, request_key (UUID), optional profile and
use_ai (default false). Reuse request_key for idempotent creation. GET /items/ for
progress; POST /approve/ with items; POST /cancel/ to stop pending work. All endpoints
are owner-scoped. A batch accepts at most 100 active jobs. Existing individual
application endpoints remain available.

## Review, reuse and portal handoff

Batch progress polls every five seconds while work is active, preserving ready-item
checkbox selections and keyboard focus. Application/batch lists and a private activity
history are available from Applications. History includes CV uploads, profile
confirmation, saved jobs, application preparation/approval/delivery, tracking changes
and note-update indicators. Notes themselves are not copied into audit events.

Identical owner/profile/job/rendering inputs reuse a private PreparedResume, including
its PDF, factual provenance and message. Rendering keys include profile content, model,
font and renderer versions. Cached materials remain available after batch cancellation;
future identical work can reuse them. Already-tracked applications skip preparation.

Secret Tel Aviv portal records support native multipart delivery without Gmail or a
browser. Enter applicant details and review terms either once per batch or in an
individual draft. Preparation stores a form fingerprint; approval binds it to the
answers, destination and CV. The worker rechecks and submits once, following receipt
redirects with GET. The form has no cover-letter field, so the generated message is
kept as a private note. Shared rate-limit cooldowns apply across users and stages.

Other portals remain manual handoffs with sign-in/registration hints. Their materials
can be edited/downloaded without an email recipient, and external completion can be
recorded through tracking. See docs/portals.md for supported delivery semantics and
the limited live test results.

Migrations 0008–0026 are required before updated web/worker/Beat processes start.
