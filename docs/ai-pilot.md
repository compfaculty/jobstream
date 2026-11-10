# Clean pilot and cost controls

The application reserves an estimated maximum cost before paid work. The default
shared ceiling is **$25 per UTC calendar month**, with the existing **200,000-token
UTC daily guard** and **20 admitted paid comparison attempts per non-staff user/day**.
Staff bypass only the per-user allowance. Cache hits, coalesced in-flight requests,
hard-preference blocks and completely deterministic comparisons do not consume slots.
Reservations and allowance updates are transactional. Unknown outcomes retain their
reservation. Costs use versioned configured uncached prices, including Batch's factor;
these are application estimates, not a provider invoice or provider account limit.
Historical token commitments are carried conservatively at the highest configured
rate when this ledger is first installed. The reset retains those aggregate totals.

## Controls and deployment

- `AI_MONTHLY_BUDGET_USD=25`
- `AI_USER_DAILY_COMPARISONS=20`
- `AI_MODEL_PRICES`: JSON mapping model IDs to input/output USD per million tokens.
  Unpriced models are blocked before inference. Update reviewed rates with model changes.
- `CV_REPRESENTATIONS_ENABLED=1`: automatically prepare reusable owner-scoped CV
  evidence. No mandatory manual review. Invalid AI evidence falls back to local PDF text.
- `JOB_NORMALIZATION_BATCH_ENABLED=1`: ingestion publishes free rule results immediately
  and queues incomplete extracts instead of making synchronous inference calls.
- `AI_PILOT_ROLLOUT_APPROVED=1`: permit scheduled paid Batch submissions only after
  the reviewed quality gate passes. Queueing remains available while this is off.
- `JOB_NORMALIZATION_BATCH_SIZE=50`: maximum requests prepared each hourly submission.
  Polling runs each minute. There is no automatic Standard-price fallback.

Keep feature flags off until their acceptance gates pass. The normalization cohort is
controlled by source enablement and AI-normalization opt-in/global configuration.
Only previously enabled sources resume after reset, staggered over time. Source reviews,
authority, verified employer relationships, URLs and adapter configuration are retained.

## Reset and recovery

1. Stop beat and application writers; drain/reconcile evaluations, submissions, source
   leases and provider batches. Do not kill an active submission and call it drained.
2. Inspect `python manage.py reset_pilot --admin USERNAME` (dry run).
3. On the Compose host run
   `python scripts/pilot_backup.py --admin-id ID --directory private-backups/UNIQUE_NAME`.
   This creates a mode-0700 directory, copies private files, restores the PostgreSQL dump
   into an isolated temporary database, verifies row counts and fingerprints identity,
   MFA and permissions. The temporary verification database is dropped afterward.
4. Clear all application queues, delayed/unacknowledged work and result caches while
   workers are stopped. This local deployment dedicates Redis DB 0 to JobStream.
5. Mount the verified backup into a one-off application container and run
   `python manage.py reset_pilot --admin USERNAME --apply --quiesced --backup-manifest /backup/manifest.json`.
6. Verify admin identity/MFA fingerprints, source configuration, deleted file absence,
   cleared application/runtime tables, health and migrations before resuming writers.

Sessions are cleared: sign in with the same password and MFA. Other accounts, CVs,
preferences and Gmail connections are removed. Authentication secrets remain in the
deployment. Company identity/verification rows required by sources are retained; logo
files and enrichment text are cleared. Database sequences are not reset, preventing old
integer task identifiers from referring to newly inserted jobs/documents.

If file deletion fails after the DB commit, remove the names recorded in the manifest
using Django's private storage backend before reopening access. For rollback, stop
writers, restore `database.dump` into an empty database with `pg_restore`, and restore
manifest files to their exact private storage keys; never publish the backup directory.
Backups contain sensitive personal data and authentication secrets. They are gitignored
and excluded from Docker build context. Delete them explicitly once rollback is no
longer needed; this implementation does not silently expire the only recovery copy.

## CV representation and matching

The identity includes owner, document SHA-256, parser/prompt/schema version and model.
Source-line IDs anchor every selected fact to locally extracted text. Negated skill
mentions are not treated as professional evidence. Python resolves only unambiguous
bare skill requirements against affirmative standalone skill lines; compound, seniority,
experience and alternative-language criteria remain model decisions. Infinity receives
unresolved criteria and evidence; Django merges deterministic decisions and calculates
the weighted score. Changing a CV, vacancy, preferences or policy invalidates reuse.
Dispatch and processing use separate states and expiring claims, so delayed worker delivery
does not prevent recovery. Profiles are removed with their documents. Optional confirmed profiles remain supported.

## Batch recovery and accounting

Public descriptions only are uploaded. Persisted content/model/prompt identity deduplicates
work across jobs and users. Operator overrides and changed descriptions fence late writes.
A lost Batch-create response becomes `unknown`; reconcile via provider metadata without
blind resubmission. Only 100 recent batches are searched per reconciliation, so older
unresolved submissions can require operator investigation. Reservations remain held.
Partial successes are applied once. Proven no-run failures may retry up to three attempts
with an hour delay. Missing/ambiguous usage remains reserved. Terminal state polling,
worker restarts and duplicate result files do not create extra paid submissions.
Provider-call metadata retains the existing 30-day policy. Financial accounting records
contain only allowlisted metadata; private profiles follow source-document lifetime.

## Quality and capacity gates

`python manage.py benchmark_pilot` evaluates the free parser and writes
`/tmp/jobstream-pilot-quality.json`. `--live` permits shared-budget API calls. Fixtures
include 100 real public postings across six adapters and 30 synthetic comparison cases.
Public-label candidates must be reviewed against source text; the harness refuses to pass an incomplete review.
The committed public labels check selected source-line category placements, not exhaustive
recall over every sentence. A passing placement score is not a hiring-accuracy guarantee. It reports extraction and comparison accuracy separately, unsupported
evidence, elapsed time, usage estimates and cache reuse. The gate requires all 100
postings reviewed, >=95% agreement in both tasks, all 30 comparisons run, and no
unsupported evidence. Every posting and comparison case must complete; meeting the
assertion-count or accuracy threshold on a partial run cannot pass. Reports include
completed case counts. Usage estimates include only new charges admitted within the
command execution context, excluding concurrent application work and cached charges.
Hard-preference tests are a separate required automated check.
Do not treat a fixture self-check or mocked model as measured live semantic accuracy.

`tests/test_ai_pilot.py` exercises 5,000 unique ingested job events, duplicate enqueue,
bounded preparation, budget exhaustion, profile isolation, reset preservation and Batch
recovery. This verifies correctness/backpressure, not 24-hour production throughput or
provider rate capacity. Operations shows backlog age, CV states, cache reuse, workload
estimates, unsettled reservations and budget-blocked work. Logs exclude bodies and CVs.

Full operation at 1,000–5,000 new jobs/day is a separate rollout decision after measured
cost and quality. Neither the monetary cap nor the daily token guard is raised by reset.

## Job views

The jobs page offers `/?mode=all` (the default) and `/?mode=recommended`.
All jobs includes stored closed/archived vacancies; the recommendations view
includes active jobs for the selected uploaded PDF or reviewed profile. Both views
show evaluated jobs first, then unevaluated jobs, ordered by `discovered_at` descending
within each group (PK breaks ties). Search, category, company, location, and saved
filters still apply. Unavailable jobs are labeled and cannot be selected for bulk
application. Other search/API callers retain their active-only contract.

Every valid current completed score from 0–100 is visible: **Good for you** at
70% or higher and **Bad for you** below 70%. Unevaluated jobs remain visible,
including queued/running/failed status where available. This replaces the former
70%-only, highest-score-first feed. Hard-preference-blocked evaluations may display
zero but never a positive score. Malformed scores and stale evaluations receive no
fit badge. CV hash/reviewed facts, preferences, job content, and policy version must
still match; owner and selected source scope all scores and statuses.

The newest uploaded PDF is initially selected. `cv=document:ID` or `cv=profile:ID`
preserves selection between views. While a search or selected-CV evaluation is active,
the list polls every five seconds and updates as individual evaluations finish.
Polling preserves visible job selections and unsaved CV choices; disabled jobs are
never reselected. GET browsing never extracts PDFs, starts work or consumes allowance.

### Explicit recommendation searches

**Update recommendations** now POSTs to `/recommendations/update/`. It starts an
owner-scoped `RecommendationRun`; ordinary GET browsing remains read-only. The
coordinator screens up to **500 recent active collected jobs**, applies hard
preferences, then prioritizes soft preferences and CV technology overlap. It
selects at most **20 vacancies** for independent Infinity evaluation agents.
Shortlisting is deterministic; these agents perform the existing evidence-backed
CV/vacancy comparisons, rather than an agent debate or unrestricted web search.
The existing runtime limits concurrent comparisons. This bounded shortlist does
not establish that every stored job was evaluated or that excluded candidates
are weaker fits. Repeated runs may select the same candidates and reuse results.

Celery Beat dispatches the coordinator every ten seconds to the preparation queue.
The existing evaluation dispatcher runs the selected comparisons. Durable run state,
expiring fenced leases, an owner-level active-run uniqueness constraint and existing
input-key evaluation deduplication recover interruptions and repeated clicks. No
transaction is held across CV storage reads. Results remain owner-scoped and must
satisfy the existing current-input checks. The page polls progress
and provides links to all selected evaluations, including low scores and failures.
A failed comparison is inspected/retried from its job page; failed cached executions
are not automatically retried by clicking Update again.

New comparisons retain the configured daily/monthly/token limits; a run does not
reserve or promise an allowance for all 20 candidates. No limits are raised and no
source is activated, crawled or discovered. The selected CV is read in the worker;
changes before/during a run can invalidate results under the existing freshness
checks. Application preparation and sending are separate, unchanged workflows.

Rollout requires migration `0006_recommendation_runs` and updating web, preparation
workers and Beat together. Keep workers/Beat on the previous version until the
migration has applied. To roll back, disable the new schedule and restore the old
button/view; retain the additive tables and existing evaluations. No live model call,
source activation or application submission is needed for migration or verification.
