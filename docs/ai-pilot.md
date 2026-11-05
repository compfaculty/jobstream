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
unsupported evidence. Hard-preference tests are a separate required automated check.
Do not treat a fixture self-check or mocked model as measured live semantic accuracy.

`tests/test_ai_pilot.py` exercises 5,000 unique ingested job events, duplicate enqueue,
bounded preparation, budget exhaustion, profile isolation, reset preservation and Batch
recovery. This verifies correctness/backpressure, not 24-hour production throughput or
provider rate capacity. Operations shows backlog age, CV states, cache reuse, workload
estimates, unsettled reservations and budget-blocked work. Logs exclude bodies and CVs.

Full operation at 1,000–5,000 new jobs/day is a separate rollout decision after measured
cost and quality. Neither the monetary cap nor the daily token guard is raised by reset.
