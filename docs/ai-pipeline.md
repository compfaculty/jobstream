# Job AI pipeline and model policy

## What the audit found

On 2026-11-05, both runtime model settings were `gpt-4.1-mini`. None of the 67 sources
had AI normalization enabled; zero Python AI cache records contained results. Existing
jobs had rule-based `sections-v1`/`sections-v2` data or no normalized payload. The AI
normalizer existed but only ran for opted-in sources when both requirements and
responsibilities were absent. A single recognized section prevented AI assistance.
The rule parser also used opening company paragraphs as the role summary and did not
recognize the heading “Advantages” as optional qualifications.

## Pilot policy

- Job normalization: `JOB_NORMALIZATION_MODEL=gpt-6-luna`, reasoning `none`, output
  cap 800 tokens, timeout 20 seconds. Deterministic parsing runs first. Automatic AI
  assistance applies only when required qualifications or duties are missing and
  `JOB_AI_NORMALIZATION_ENABLED=1` or the source opts in. Provider errors, quota
  rejection, and invalid extractions preserve deterministic results.
- CV comparison: `INFINITY_MODEL=gpt-6-luna`, reasoning `low`. The model classifies
  evidence; Python calculates the percentage and enforces hard preferences. Blocked
  jobs return 0% without inference. Only explicitly requested comparisons are queued.
- Existing optional Python fact selection uses `OPENAI_MODEL` and
  `OPENAI_REASONING_EFFORT`, independently of the Rust evaluation path. The existing
  tailoring step ranks source facts; this is not a new unrestricted CV rewriter.
  No automatic Terra/Sol escalation or extra paid pass is enabled.

Normalization uses strict Structured Outputs to select exact source-line IDs for
role summary, product/platform, mandatory qualifications, optional qualifications,
and responsibilities. The server rejects unknown/duplicate IDs and conflicting
qualification categories, then derives technology tags and evidence from the source.
It retains years, negations and language alternatives. Company slogans, culture,
benefits, EEO, fundraising and generic marketing are excluded from selected sections.
Raw descriptions remain available and are never overwritten by normalization.

The normalized JSON keeps the established fields and adds optional `product` text
and AI model/version metadata. CUDA, Triton, vLLM, ONNX and TensorRT are recognized
alongside the existing technology catalog. The rule parser prioritizes role duties
for summaries and recognizes Advantages as preferred qualifications.

## Caching and budget controls

Public job extraction is cached across users by source content, purpose, model,
prompt, schema, reasoning and output limit. Unchanged postings do not cause another
paid request. Private CV-related calls remain owner-scoped. Successful Python AI
calls settle their reservation to reported usage; unknown failures retain their
conservative allowance. Lease fencing prevents late workers from overwriting a
newer cache lease. The shared pilot daily token limit is unchanged.

For new evaluations, the full original snapshot remains in Django for audit. Infinity
receives compact criteria, concise role/product context and whitespace-normalized
CV evidence, without duplicate raw description and normalized JSON. Historical
contexts and reservation calculations retain their original behavior. A context
version participates in cache identity so old full-context results are not silently
relabelled as compact runs.

PDF extraction itself is currently local and bounded; there is not yet an automatic,
persisted semantic CV skill profile. Before public matching at scale, add that
representation keyed by document hash and parser version, then use Python exact
skill/alias matching and invoke AI only for unresolved semantic relationships.

## Refresh existing data

Dry-run without paid calls:

```sh
python manage.py normalize_jobs --limit 1000
```

Apply free rule improvements, preserving operator overrides and raw descriptions:

```sh
python manage.py normalize_jobs --limit 1000 --apply
```

Explicitly run AI for one public job (the existing cache and daily budget still apply):

```sh
python manage.py normalize_jobs --job 20651 --apply --ai
```

The command checks the description again under the inventory writer's advisory lock
before saving. A concurrent import or operator override causes it to skip that row.

## Evidence for the model choice

`python -m scripts.benchmark_job_models --live` runs synthetic extraction and CV
comparison smoke checks, with a default estimated API cost cap of $0.05. It never
uses personal CVs. Cached cases are marked; they must not be treated as cold latency
measurements. Estimates use published uncached Standard prices, not actual invoices.

The initial five-case run (two extraction cases, three CV cases) gave Luna all eight
labelled CV classifications at both `none` and `low`, matching the baseline on these
cases. Luna `low` matched all ten labelled extraction fields; the baseline matched
eight. Luna `none` had one 90-second transport timeout; that case passed all five
field checks on a bounded retry in 1.1 seconds. The small smoke check supports a
pilot choice, not a claim of universal superiority or a production-quality accuracy
benchmark. Review 100–200 representative real postings before public rollout.

## 1,000–5,000 new unique jobs per day

The target is new job ingestion, not current comparison volume. Normalize each
unique posting once and share its public structured data. Avoid a cross-product of
all users and all raw descriptions. Compare on demand, cache by CV/job/policy/model
versions, and isolate per-user budgets before public use.

At an illustrative 3,000 input + 500 output tokens per job, Luna Standard costs
$16.50–$82.50 per 30-day month for 1,000–5,000 jobs/day. These are assumptions, not
measurements of current postings; they exclude comparison, tailoring, retries and
extra reasoning. A complete AI pass at that assumed volume needs 3.5M–17.5M daily
tokens before comparisons. The current 200,000-token pilot cap is not sized for that
volume and is deliberately not raised automatically. Rule extraction and content
caching can materially reduce paid normalization volume.

OpenAI Batch can halve eligible asynchronous token costs, with up to 24-hour
completion. Celery alone does not activate Batch pricing. This deployment uses
standard Responses requests. A future Batch stage needs a persistent submit/poll/
reconcile ledger, source-version checks, retry accounting and idempotent writes.

Sources verified 2026-11-05:
- https://developers.openai.com/api/docs/models/gpt-6-luna
- https://developers.openai.com/api/docs/guides/structured-outputs
- https://developers.openai.com/api/docs/guides/batch
- https://developers.openai.com/api/docs/deprecations
