# Milestones and development tasks

Status: code-backed checkpoint, 2026-11-13. Tasks are not authorization for live
collection, paid inference, uploads, applications, deployment or messages. Keep test
results in PR/CI records, not dated transcripts here.

## Implemented baseline

Invite-only/staff-MFA accounts; company/source catalogue; guarded ingestion,
provenance/dedupe and complete-inventory lifecycle; private CV editing/factual tailoring;
preferences/recommendations; exact-content approval and durable Gmail/supported-portal
delivery; UNKNOWN reconciliation; local browser assistance; synthetic email sandbox;
optional Infinity evaluation/monitoring. Code is not proof of current provider
compatibility, rights or production readiness. See [architecture](ARCHITECTURE.md).

## M0 — simplify and establish a clean baseline

- [x] Reconcile the proposal with existing services; avoid duplicate models/frameworks.
- [x] Consolidate handoffs/plans/research into six maintained Markdown files.
- [x] Keep the initial Greenhouse contract offline: bounded synthetic inputs, fixture
  grants, stable IDs, complete totals and snapshot pagination/drift checks.
- [ ] Validate the combined local functionality and publish a sanitized PR; merge only
  with passing required checks. Record failures without exemptions/suppressions.

Acceptance: documentation links resolve, guidance matches code, private operational
records are absent, proposed work is labeled and each procedure has one home.

## M1 — source governance, then one original employer

1. Add Source-linked permission revisions: reviewer/evidence, scoped storage/display
   uses, expiry/revocation, attribution, retention/removal and request budgets. Existing
   sources start REVIEW_REQUIRED; enabled/authoritative never implies permission.
   Introduce shadow reporting before separately reviewed enforcement changes.
2. Gate discovery activation, scheduling, worker start, page resume and final write/
   closure against the same grant revision/lock. Revocation fences in-flight results.
   Reuse existing collectors, leases, provenance and lifecycle.
3. After a named employer/provider grant, validate one original Greenhouse board using
   an authorized contract fixture. Until then stay synthetic. Controlled live acceptance
   needs separate approval, bounded traffic and shadow closure; only then expand providers.

Acceptance: valid/empty/malformed responses, pagination drift/replay, 429/5xx/timeout,
expired/revoked/wrong-scope grants, duplicate identities, partial failure, one/two
complete absences, overlapping open origins, reopening and provenance. Test permission
races/locks with PostgreSQL. Separate source-updated, published and first-seen semantics.

Original employer agreements outrank catalogue scraping. Greenhouse public GET is not
blanket commercial reuse; application POST requires employer authorization. Ashby's
publication API is not generic submission. Comeet needs authorized UID/token. Lever/
Workable/Workday need provider/tenant-specific review. Do not systematically copy InstaSite
or scrape HireMe/Job Match AI for production. Competitor ideas become tasks below;
marketing counts are never performance/acceptance evidence.

## M2 — trustworthy eligibility and simple onboarding

Depends on M1 governance and existing confirmed profiles/preferences.

- Version verified facts; add missing deterministic legal eligibility, seniority,
  employer exclusions, salary unknowns, geography/commute, work-mode and language
  constraints. Unknown mandatory requirements need review.
- Refine existing HTMX onboarding: base CV → confirmed facts → essential preferences
  → explainable verified opportunities. Review Hebrew/English, accessibility and RTL.
- Preserve PDF-vs-profile input identity; invalidate stale evaluations/preparations.

Acceptance: impossible commute, ambiguous relocation, excluded employer, unknown salary,
seniority mismatch and unconfirmed claims cannot become send-ready. Foreign owner access
fails. A match score does not substitute for mandatory requirement verification.

## M3 — application evidence and supported handoff

Depends on M2. Extend Application/PreparedResume/outbox instead of rebuilding them.

- Add only verified gaps in immutable attempt/receipt evidence, frozen fact/evaluation
  revisions and cross-mirror dedupe. Preserve stored CONFIRMED/UNKNOWN values.
- Retain exact CV/hash/fact provenance; recheck sender, schema/answers and approval
  before effects. Separate confirmed transport from recruiter outcomes.
- Validate existing fake-provider/Mailpit paths. Unsupported Comeet/BambooHR/forms
  remain manual, not generic autosubmit.
- Hosted browser handoff needs owner-bound expiring sessions, exclusive control and
  a defined user-owned final-submit boundary. Local assistance is not hosted streaming.

Acceptance: duplicate workers/replay, cancellation races, content/attachment drift,
unsupported forms and post-send timeouts stop safely. Never blindly retry UNKNOWN.
Real applications require fresh vacancy/fit review and exact-content authorization.

## M4 — bounded standing authorization

Depends on M1–M3 and approved consent design. Add affirmative versioned grants scoped
to channel/employer/geography/profile fields, expiry, pause/revoke and atomic daily/
weekly/employer caps. Review Each remains default; batches/saved answers are not consent.
Custom/sensitive questions, low confidence and unsupported forms enter an exception inbox.

Acceptance: revoke between queue/execution, concurrent cap exhaustion, policy/fact changes
and expiry stop sends. Disclose in-flight limits; human challenges require permitted
human handling, never bypass. A live bounded pilot needs separate activation approval.

## M5 — optional HR response lifecycle

Depends on M3 and privacy/OAuth review. Add owner-scoped thread links, minimal retained
acknowledgement/question/interview/rejection evidence, deletion and incremental consent.
Reuse encrypted grants. Replies/follow-ups need separate approval; scheduling needs
independent calendar permission. Do not overwrite transport state with recruiter inference.

Acceptance: dedupe, multilingual mail, spoofed/forwarded senders, prompt injection,
revoked OAuth, false interview classification and scheduling collisions. No message bodies
in logs or automatic unsafe replies.

## M6 — measured outcomes and production gates

Measure qualified replies/interviews, candidate effort, cost per confirmed transport,
uncertainty, duplicates, factual accuracy, false closure and freshness with explicit
cohort denominators. Calibrate against consented outcomes. Benchmark representative
query plans/load before optimizing or adding infrastructure.

Before a public pilot choose provider/region/budget/SLO/RPO/RTO, verify TLS/proxy/IAM
and restore, review source rights/Google OAuth assessment, and implement erasure/retention
across CVs, derived data, runtime history and backups. Bound adversarial PDF extraction;
resolve AI quality/denominator evidence before paid rollout. Define immutable-audit vs
deletion policy. External alerts, privileged exporters and capacity promises remain
pending justification. Local Compose and synthetic deployment checks are not production proof.
