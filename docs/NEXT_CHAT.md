# Next chat: finish JobStream AI validation and controlled rollout

I want a new, decision-complete implementation plan for the remaining JobStream work.
Inspect the repository and current runtime first, then split the plan into verification
milestones. This request is for planning; do not run paid benchmarks, change rollout
flags, delete data, or raise budgets during the planning pass.

## Project and completed work

Repository: `/Users/alexg/projects/jobstream`.
Current branch: `experiment/infinity-agent-runtime`. Review the recent task-based
commits and preserve any new uncommitted work. The stack remains Django/Celery,
PostgreSQL, Redis, private object storage and Rust Infinity in Docker Compose.

Already implemented and deployed:

- Staff/MFA-protected, read-only Operations with queue/worker/scheduler observations,
  paginated history, execution details, provider-call metadata and correlated logs.
- Uploaded PDF evaluation without mandatory manual profile setup. Luna classifies
  source-backed evidence; Python calculates the percentage and enforces preferences.
  Excluded technologies are hard blockers, including optional mentions/alternatives.
- Shared application-side monthly cost reservations, daily token guard and per-user
  paid-comparison allowance. Known usage settles once; unknown outcomes retain their
  reservation. Cached and preference-blocked results do not incur paid-call allowance.
- Source-grounded normalization, content caching, compact comparison context,
  versioned CV representations, bounded worker recovery and durable Batch processing.
- A default-dry-run reset command, checksummed private backups and isolated restoration
  verification. The reset was already executed successfully. Admin `compfaculty`, MFA,
  recovery tokens, permissions and all 67 sources were retained. Only the same four
  sources remain enabled. Ingestion has resumed with fresh jobs.
- Two job views: All jobs includes all stored statuses, newest added first;
  Recommended for you shows only active jobs with a current completed score >=70%
  for the selected CV, highest score first. Ownership, current CV/job/preferences,
  filters, pagination and HTMX/mobile behavior are covered. Browsing never starts AI.

Do not repeat the reset or rebuild these features from scratch. Existing deployment
secrets, account/MFA setup and current source review/enablement must be preserved.

## Limits and last verified runtime state

These are application guards, not provider invoice guarantees:

- Monthly ceiling: **$25** across normalization, CV parsing, comparison and tailoring.
- Daily guard: **200,000 tokens per UTC day**.
- Non-staff allowance: **20 admitted paid comparison attempts per UTC day**.
- Normalization model: `gpt-6-luna`, reasoning `none`.
- Comparison model: `gpt-6-luna`, reasoning `low`.
- `JOB_NORMALIZATION_BATCH_ENABLED=1`: queue incomplete extracts after immediate
  publication of free rule-based results.
- `AI_PILOT_ROLLOUT_APPROVED=0`: scheduled paid Batch submission is held.
- `CV_REPRESENTATIONS_ENABLED=0`: the new cached-profile flow is held. Direct PDF
  evaluation remains available through the existing path.

At the last runtime read on **2026-11-05 UTC**, the daily ledger held **196,104 tokens**.
The reset did not replenish token or monetary capacity; legacy commitments were carried
conservatively. Read current state rather than assuming these observations are still
current. Do not refund uncertain legacy usage or raise limits to make validation pass.

The one-item public-description Batch smoke request was accepted, but its last observed
state was `in_progress`. Internal batch ID:
`6e0ea95e-8ef9-41b3-958c-c2ebbfe086fa`.
Poll/reconcile the existing record; do not blindly create a replacement submission.

## Milestone 1 — Complete the actual quality and Batch gates

Plan how to:

1. Inspect the accepted Batch's current state and reconcile terminal results, safe
   source application and reported usage exactly once. Preserve reservations on
   ambiguity; handle failed/expired/cancelled work using existing bounded recovery.
2. Resume the benchmark when the daily guard has capacity. It is cached and uses the
   shared spending guards. Planned implementation command:

   ```sh
   docker compose exec -T web python manage.py benchmark_pilot --live --report /tmp/jobstream-pilot-quality.json
   ```

3. Diagnose and fix real failures, then rerun only affected cases. Distinguish budget
   blocks, transport failures, schema/evidence rejection and semantic disagreement.
4. Update the durable verification summary and document the result before changing flags.

Fixtures contain **100 public postings across six adapters** and **30 synthetic CV/job
cases**. The last run processed 67 postings: **103/104 reviewed line placements matched
(99.04%)**. The remaining **33 postings and all 30 comparisons were budget-blocked**.
That is incomplete validation, not a full pass and not a measured CV-match accuracy.
Public labels check selected source-line category placements, not exhaustive recall or
hiring success. Do not use an LLM judge as the sole accuracy reference.

Acceptance requires all cases executed, >=95% agreement separately for extraction and
comparison, no unsupported accepted evidence and passing hard-preference tests. A
terminal, correctly settled live Batch result is also required.

## Milestone 2 — Enable and verify the existing guarded features

Only after the gates pass, plan a small rollout of
`CV_REPRESENTATIONS_ENABLED=1` and `AI_PILOT_ROLLOUT_APPROVED=1` using the existing
Compose deployment. Keep the monetary/token/user limits and source cohort unchanged.

Verify with synthetic private documents and mocked failures where possible:

- Upload/first use -> processing -> ready or local-text fallback; unreadable PDFs get
  a clear status. No mandatory manual profile entry or invented experience.
- Owner isolation, immutable source evidence, CV/job/policy/version cache invalidation,
  explicit skill aliases and conservative handling of ambiguity, negation and years.
- Duplicate requests, concurrent allowance admissions, retries, cancellation and worker
  restart recovery do not double-charge or publish stale results.
- New jobs remain visible immediately. Batch enrichment stays bounded and cannot
  overwrite changed descriptions or operator overrides.
- Operations accurately reflects backlog age, profile states, reservations, workload
  usage, unavailable/stale dependencies and budget-blocked work without private content.
- Both job views continue to work; viewing recommendations consumes no AI allowance.

Provide rollback via the existing flags and retain known/unknown accounting correctly.
Do not introduce automatic applications or a more expensive model escalation.

## Milestone 3 — Review real capacity and operating budget

The scale target is **1,000–5,000 new unique vacancies/day**, not comparisons/day.
Comparison volume is currently small and should remain on demand, growing with users.

The mocked 5,000-event scenario passed; sustained real-provider throughput has not been
proven. Measure actual tokens, retries, cache reuse, queue age and cost on the small
source cohort. The default hourly Batch submission prepares at most 50 items (up to
1,200/day before other limits); configuration currently clamps a batch to 100 items.
Only incomplete extracts need enrichment, but do not claim the 5,000/day target is
supported without comparing their measured arrival rate with this service capacity.

Plan bounded cohort expansion and any cadence/batch-size adjustments from measurements.
The pilot's 200,000-token daily guard is not sized for full AI processing at the target
volume. Any budget increase is a later explicit decision, not part of passing tests.

## Sources of truth and verification

Read:

- `docs/ai-pilot.md` — configuration, reset/recovery, budget semantics and job views.
- `docs/ai-pipeline.md` — model policy, audit and scaling assumptions.
- `docs/operations.md` — metadata and monitoring interpretation.
- `docs/ai-pilot-verification.json` — incomplete benchmark evidence and verified checks.
- `jobs/services/ai_costs.py`, `cv_profiles.py`, `normalization_batch.py` and
  `recommendations.py`; `jobs/management/commands/benchmark_pilot.py`.

Current migrations run through `0047_resume_profile_details`. Historical verification
(rerun against the current checkout before claiming a current pass):
492 non-browser Python tests passed (9 opt-in checks skipped); browser navigation,
Operations refresh and fake-provider gateway checks passed (14 checks, 1 real-provider
opt-in skip in the combined run); 11 Rust unit tests and
Clippy with warnings denied passed. Keep results attributable to commands actually run.

Useful implementation-phase checks:

```sh
DEBUG=1 .venv/bin/pytest --reuse-db -m 'not browser' -q
DEBUG=1 INFINITY_INTEGRATION=1 .venv/bin/pytest --reuse-db tests/test_infinity_integration.py tests/test_operations_browser.py tests/test_recommendations.py::test_feed_navigation_cv_filter_categories_and_mobile tests/test_browser.py::test_job_category_sidebar -q
cargo test --offline --locked --manifest-path services/infinity-agent/Cargo.toml
cargo clippy --offline --locked --manifest-path services/infinity-agent/Cargo.toml -- -D warnings
```

Local PostgreSQL/Redis, browser and Docker checks may require sandbox escalation.
`.env` and `private-backups/` are intentionally untracked. Never print credentials, CV
bodies or backup contents. The verified private backup location is documented locally;
backups must stay out of Git and Docker build context. No new hosted service is wanted.

## Profile editor — future improvement needed

The structured profile editor is an initial iteration, not the final UX. It now has
separate contact fields and repeatable experience, education, and project entries,
with immutable saved versions and a reference view of the original PDF text.

Follow-up work:

- Add source-backed PDF prefilling with explicit user review; never infer missing
  qualifications, dates, or contact details.
- Reduce the length of the form with clearer section navigation and compact entry
  summaries; retain accessible mobile and keyboard behavior.
- Improve entry removal/reordering and the experience without JavaScript.
- Replace the combined 500-character contact limit with structured rendering in
  generated CVs, while preserving existing saved versions and downstream contracts.

Keep this follow-up separate from the AI rollout gates above. The profile change
adds migration `0047_resume_profile_details`; it does not change rollout flags,
provider budgets, or application-send approval requirements.
