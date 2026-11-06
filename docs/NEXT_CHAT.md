# Next chat: integrate another company end to end

Continue Jobstream with a **different company**, not another Cato/GitLab/Camunda
validation session. The user explicitly deferred new-company work to the next chat.
Repository: `/Users/alexg/projects/jobstream`; branch at handoff: `master`.
Read AGENTS.md, check Git status/recent commits, and preserve subsequent work.

## Objective and selection

Complete one small company tranche:
**verified company -> get jobs -> normalize -> supported application adapter**.
Reuse existing platform adapters and approval machinery. Do not start a generic
redesign or expand all sources at once.

1. Inspect the current company/source catalogue and existing integration notes.
2. Propose a different company with a working public careers endpoint and a useful
   application route. Akeyless/Comeet is a candidate for a different platform, not an
   attested choice: verify its current official site, board relationship and form first.
   Prefer a suitable existing disabled catalogue entry over inventing an employer.
3. State the selected company and route. Ask only if selection or scope needs user
   input; otherwise continue with bounded read-only inspection and offline work.

## Implementation milestones

### 1. Establish identity and collection

- Read `docs/company-integration-workflow.md`, `docs/company-catalogue.md`,
  `docs/sources.md`, and the relevant adapter code/tests.
- Verify the official employer-to-ATS relationship using public read-only evidence.
  Do not transfer employer API credentials, bypass challenges or assume authority
  from a board listing alone.
- Reuse supported collectors; add the smallest adapter change only if necessary.
- Cover bounded/resumable collection, stable provider IDs, duplicates, malformed or
  truncated responses, rate limits, interrupted runs and changed inventories.
- Preserve operator overrides and existing jobs/application IDs. Leave automatic
  closure off pending shadow review. Keep the current four-source cohort unchanged
  until an explicit rollout decision; test candidate activation with fixtures first.

### 2. Normalize and connect application preparation

- Publish deterministic normalized jobs immediately. Preserve original descriptions,
  exact source evidence and curated overrides; AI enrichment must remain optional.
- Test mandatory/preferred sections, alternatives, negations, years and technology
  evidence against reviewed public fixtures.
- Trace the collected provider identity through the UI's adapter selection and draft
  preparation. Prefer shared platform code over employer-specific duplication.
- Preserve the existing distinction: application preparation's technology-fit screen
  differs from percentage evaluation's absolute excluded-technology blockers.
  Do not silently change either policy (see `docs/ai-pipeline.md`).

### 3. Implement the application route safely

- Inspect the public form/schema and supported submission route. Record manual
  handoff explicitly if automation is unsupported; do not call that a finished
  automated integration.
- Require factual owner-entered answers, an owned CV, and approval bound to the exact
  answers, attachment, destination and meaningful form schema. Changed questions or
  consents require review. Saved answers require opt-in and semantic equivalence.
- Cover cross-owner access, schema drift, challenges, 429s, duplicate tasks,
  cancellation, worker interruption and uncertain outcomes. Never replay UNKNOWN.
- Tests use synthetic applicants and fake/intercepted providers. No real upload,
  application, email, invitation or notification without explicit approval for it.

### 4. Verify, document and checkpoint

- Add a pipeline regression spanning company discovery, real collector parsing,
  normalization, UI draft, owner approval and fake confirmed/unknown delivery.
- Run focused Ruff, PostgreSQL tests and intercepted Chromium checks as appropriate;
  run broader checks if production changes warrant them. Review the final diff.
- Record public evidence, commands, results and remaining live-validation boundaries
  in a new `docs/integrations/<company>.md` note. Distinguish fake success from a real
  receipt. Follow the existing per-company commit convention; never push/deploy
  without authorization. This handoff does not authorize a future commit by itself.

## Work completed in the preceding chat

- `benchmark_pilot.py`: all postings and comparison cases must complete before a
  live gate passes; assertion counts alone cannot establish complete coverage.
  Reports include completed-case counts.
- `ai_costs.py`: execution-context charge observation attributes newly admitted
  benchmark costs without including unrelated concurrent application work. Budget
  admission, settlement, cache identity and limits remain unchanged.
- Added benchmark regressions and Cato pipeline tests; clarified current policies
  and documented Cato's read-only recheck.
- Executed checks: benchmark/accounting suite **27 passed**; adjacent budget,
  normalization/preferences suite **34 passed**; company/discovery/adapter/
  submission/network suite **132 passed** (12 Django URLField deprecation warnings);
  two new Cato pipeline cases **2 passed**; Cato Chromium fixtures **15 passed**.
  Ruff lint/format and `git diff --check` passed. These are historical results; rerun
  relevant checks after changes. No full-project test/security scan was run.

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
