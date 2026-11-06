# Next chat: Akeyless checkpoint and remaining validation

Repository: `/Users/alexg/projects/jobstream`.
Integration branch: `feat/akeyless-comeet-integration`, created from `4032da1` on
`master`, preserving its two local commits. Read AGENTS.md and inspect current Git
status/diff and commit history before continuing.

## Current tranche

Akeyless / Comeet collection and explicit manual application handoff are implemented.
See `docs/integrations/akeyless.md` for public evidence, configuration, limitations
and verification commands. Automated application delivery remains incomplete.

- Official careers links establish the Akeyless-to-Comeet board relationship.
  The direct official-site request returned 403; no bypass was attempted.
- The public hosted board returned nine embedded positions on 2026-11-06.
  Collection uses stable `comeet:27.006:<uid>` identities, deterministic normalization,
  bounded 250-job persistence batches and posting-content-hash checkpoints. It never claims
  a complete inventory or infers missing-job closure from HTML data.
- Comeet proposals remain disabled/pending even from verified-site discovery.
  Existing legacy Akeyless careers-page sources are preserved. No runtime source
  records or enabled cohort were changed; closure remains off.
- The reviewed hosted vacancy had reCAPTCHA enabled and a JavaScript form widget.
  There is no supported automated submit adapter. Drafts require an owned CV,
  show the manual route, and cannot be approved/sent through Jobstream. Batches
  require manual action; owner answers/consents are entered on the employer site.
- No real application, upload, email, paid AI call, rollout, push or deployment
  was performed. No provider receipt was established or fabricated.

## Verification checkpoint

The final combined PostgreSQL suite passed **210 tests**, including intercepted
Chromium. Focused Ruff lint/security rules, formatting, Django checks, migration-drift
checks and `git diff --check` passed. All nine captured public postings parsed and
normalized offline. See the integration note for commands, warnings and limitations.
No full-project security scan or production-settings check was run.

## Next work

Review the tranche and integration note. Keep automated delivery marked
incomplete unless a safe public applicant route and meaningful schema are established.
A challenge must lead to human action, never bypass. Any future live submission needs
explicit approval for the selected vacancy, owned CV, factual answers and destination.
Do not transfer employer API credentials or treat widget tokens as authorization.

Keep the four-source enabled cohort unchanged until an explicit rollout decision.
Do not enable closure for hosted Comeet HTML inventories. Fixture activation is
covered; live operator activation and ingestion have not been performed here.
Preserve the distinct preparation-fit and percentage-evaluation policies.

The prior Cato benchmark/accounting hardening is in `4032da1`; the prior profile-editor
work is separate. Do not resume Cato/GitLab/Camunda sends as part of this tranche.

## Deferred AI validation — do not lose or silently enable

The paid live benchmark did **not** run in the preceding chat. Automatic approval
review rejected it for missing explicit authorization for external fixture payloads
and paid calls. A question was presented, but no approval was received before this
handoff. Do not retry indirectly. Obtain explicit approval before sending the 100
public postings and 30 synthetic CV/job fixtures to the configured OpenAI API.
No personal CVs are needed for this benchmark.

Keep the $25/month, 200,000 tokens/UTC day and 20 paid comparison attempts/non-staff
user/day guards. Keep `AI_PILOT_ROLLOUT_APPROVED=0` and
`CV_REPRESENTATIONS_ENABLED=0`; Batch queueing remains on. No model escalation,
budget increase, reset, backup deletion or rollout is authorized by this handoff.

The existing Batch `6e0ea95e-8ef9-41b3-958c-c2ebbfe086fa` was observed completed and
settled on 2026-11-06: 598 input + 43 output tokens, estimated cost $0.00004065.
Do not create a replacement smoke Batch. The durable quality evidence in
`docs/ai-pilot-verification.json` still represents an incomplete benchmark, not a pass.
Acceptance remains all 100 postings and 30 comparisons completed, >=95% agreement
separately, no unsupported accepted evidence, and passing hard-preference tests.

Last allowlisted runtime read on 2026-11-06: monthly committed $0.29053235; no quota
row yet for that UTC day; 67 sources retained, four enabled; migration 0047 applied.
Cato had 115 active jobs; Automation Engineer posting 4937879101 was local job 21110.
Its public schema diagnostic returned supported. Read current state again rather
than relying on those observations. Web/workers had different image versions;
repository fixes were not deployed. Preserve secrets, MFA, backups and source reviews.

Cato still lacks a confirmed live receipt; its deferred send is not resumed here.
GitLab real delivery remains unverified. Camunda has a historical confirmed receipt
and must not be resent. Leave those live checks separate from the new company.
The profile-editor UX follow-up is also separate; see its prior commit/history.
