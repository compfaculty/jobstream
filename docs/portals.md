# Employer portal applications

## Secret Tel Aviv

The native multipart adapter is ported from the previous Go implementation:
https://github.com/compfaculty/jf/blob/master/internal/scrape/job-boards/secret_telaviv.go

It does not require a browser or Secret Tel Aviv account. JobStream GETs the vacancy
in a fresh cookie session, verifies the known form, preserves hidden action/job fields,
and POSTs applicant details and the selected PDF together using the `file` field.

For individual applications, open Prepare application, choose a CV, enter first/family
name, email and telephone, and review the terms. For batches, provide these details
once in the optional Secret Tel Aviv section; preparation snapshots the form in the
background. Missing details can be completed by editing each prepared application.
Headhunter forwarding is a separate optional choice, off by default. The portal has
no cover-letter field: generated messages remain private application notes.

Review and approve the application before sending. Approval binds the channel,
vacancy URL, form/job fingerprint, applicant details, consent choices and exact CV
hash. It does not require Gmail. Changes clear approval. The sender rechecks the form
and attachment before POSTing. Changed/unknown forms or terms require review.

## Delivery and retries

A worker claims each approved application once before network I/O. POST attempts use
one known file-field name; no fallback field guessing or automatic transport retries.
Connections use checked public DNS pinned to TLS connections, bounded responses,
HTTPS and destination restrictions. Cookies are ephemeral and are not logged.

A 429 rejection schedules a durable retry using Retry-After (seconds or HTTP date),
backoff and jitter. Its shared site cooldown pauses preparation, approval preflight
and other deliveries. SECRET_TLV_APPLY_INTERVAL defaults to ten seconds across users.
Five delivery rejections exhaust the retry budget. Other explicit validation
rejections fail for review. A POST timeout, unexpected redirect or ambiguous response
remains unknown and is never blindly resent.

Confirmation requires an application-specific success notice. Native redirects to
`/job/SLUG/applied/ID/` are followed with GET and recorded as receipt references.
Empty error placeholders do not invalidate a positive notice. A 200/302 response or
unrelated “thank you” copy alone does not establish success. A known receipt from an
uncertain outcome can be checked read-only. Success notices may be session/one-time
flash messages; if they cannot be recovered, the result remains unknown for manual
review rather than resending.

## Verification on 2026-10-31

The user explicitly authorized at most two live tests. Exactly two native multipart
POSTs were made with a PDF prominently labeled “integration test—not a candidate
application,” synthetic contact details, and headhunter forwarding off.

- The first returned a native redirect before confirmation support was corrected;
  it was not repeated and remains an uncertain test outcome.
- The second returned a native receipt URL and the explicit notice “Your application
  has been sent.” Its empty error placeholder exposed a parser issue now covered by
  regression tests. A later fresh-session GET did not repeat the one-time notice.

No further live submissions were made. Remaining tests use local fixtures and mocked
remote responses, including the full browser review/approval workflow, multipart
payload, consent integrity, shared 429 handling, duplicate tasks and unknown outcomes.

The read-only `assess_portal URL` command remains available. Its
`submission_enabled=false` output means that inspection itself grants no sending
permission; normal JobStream application approval is the sending authorization.

## Other portals

Other employer forms remain manual handoffs. Public forms, authentication and
registration hints are shown alongside prepared materials. Complete those steps on
the employer site and update private tracking. Additional automated adapters should
use the same exact-content approval and explicit-confirmation invariants.
