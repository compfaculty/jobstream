# Adding and operating sources

Company-first catalogue import, discovery, lifecycle and rollout are documented in
[Company catalogue](company-catalogue.md). Prefer company-owned career endpoints;
Secret Tel Aviv remains a supplementary board.

## Admin example

Open `/admin/jobs/source/add/` after TOTP verification:

- Name: `Example careers`
- Adapter: `Company page (JobPosting JSON-LD)`
- URL: `https://careers.example.com/openings`
- Interval: `1440` minutes; maximum items: `100000`
- Enabled: leave off until configuration and source permissions are reviewed.

Enable the source and use **Schedule selected sources now**. Beat finds due sources
once per minute. View **Ingestion runs** for imported/rejected counts and a safe error
category. A successful run updates last-success and last-seen timestamps. Failures
back off exponentially, capped at one day. One source run holds a ten-minute lease;
worker time limits are shorter. Each source is sequential; shared request leases and worker concurrency
bound global and per-host ingestion work. Keep source access conservative and authorized.

## Supported adapters

- RSS / Atom: imports full feed descriptions, UTC publication dates and available
  company/location namespace fields; malformed entries are rejected.
- JSON-LD: imports JobPosting nodes embedded in the configured page, including @graph.
- Comeet: configure a public positions API endpoint returning a JSON array. No token
  is embedded in the example repository. Page URLs and API URLs are different inputs.
- Akeyless hosted Comeet board: the existing `akeyless` adapter also accepts
  `https://www.comeet.com/jobs/akeyless/27.006`. It decodes public embedded job data
  with bounded, hash-checked persistence batches, without proving inventory completeness.
  Discovery proposes a disabled source; applications use manual handoff. See
  [Akeyless integration](integrations/akeyless.md). Legacy careers-page settings remain intact.
- Secret Tel Aviv: public `wpjb-col-title` listings and job-detail selectors from JF.
- Telfed: public UAEL title links and job-detail selectors from JF.

`seed_sources` imports 54 disabled references from the old Go project: six RSS feeds,
two boards and 45 company pages plus GitLab’s verified Greenhouse reference. It preserves existing names, URLs, enabled flags,
schedules and operator notes; URL matching also avoids duplicating renamed sources.
Run `uv run python manage.py seed_sources --dry-run` to preview additions.

RSS references: DOU Python, AI/ML, Golang and DevOps; Jobicy; Real Work From Anywhere.
Dedicated company parsers: 40Seas, Agrematch, AI21, Akeyless/Comeet and AudioCodes.
The other 40 company references use the JSON-LD adapter and are explicitly marked in
Admin notes as needing compatibility review; a reference is not a guarantee of support.
The 40Seas/Agrematch legacy URLs returned 404 during the 2026-10-31 checks.

Real Work From Anywhere optionally enriches up to ten feed entries with page metadata
and employer application links. Failed enrichment keeps the original feed entry and
increments the ingestion run's **Enrichment failed** counter. Later feed entries can
still import without enrichment. No application is sent by ingestion.

Public HTML adapters import at most ten details per run, without pagination. They **do not bypass
Cloudflare, CAPTCHA or authentication**; a protected or changed page fails visibly.
No live source compatibility is claimed without an operator test. JS-only company
sites need another adapter; the generic JSON-LD adapter does not render JavaScript.

## New adapter example

Implement the SourceAdapter discovery contract in `jobs/services/sources.py`:

```python
class ExampleAdapter:
    def discover(self, data: bytes, base_url: str, limit: int) -> list[Listing]:
        # Parse bounded bytes; return at most limit listings.
        return [Listing(url="https://example.com/jobs/42", title="Engineer")]
```

Register it in ADAPTERS, add the Source.Adapter choice and migration, and add fixture
coverage for valid, empty, malformed and hostile inputs. Metadata enrichment is a
separate adapter capability (see BoardAdapter.metadata), using the same safe fetcher.
Never add eval, shell commands, dynamic Python or browser scripts to Admin fields.

Every fetch validates HTTPS, ports, credentials and DNS addresses. It pins a checked
IP while retaining hostname verification/SNI, revalidates redirects, limits response
bytes and sets socket timeouts. Production also needs network-level egress restrictions;
application validation is not a substitute for those controls.

## Legacy source provenance

Catalog references come from [JF aggregators](https://github.com/compfaculty/jf/blob/master/internal/aggregators/registry.go)
and [JF company pages](https://github.com/compfaculty/jf/blob/master/internal/repo/companies.go),
including the commented-out entries. Only Secret TLV and Telfed were enabled in that
checked-in registry. The new catalog deliberately leaves additions disabled.

Adding references does not probe every company. After operator review, enable one
source, schedule it, and examine its imported/rejected/enrichment-failure counts.
A zero-import JSON-LD run does not prove that a JS-only careers page is supported.


## Direct ATS sources and employer verification

Greenhouse: https://boards-api.greenhouse.io/v1/boards/BOARD/jobs?content=true
Ashby: https://api.ashbyhq.com/posting-api/job-board/BOARD
Workday: https://TENANT.wdN.myworkdayjobs.com/SITE

Set employer_name to the verified employer. Enable authoritative only after reviewing
the employer-to-board relationship. Two complete, successful ATS inventories can establish missing jobs
in the same endpoint scope. Closure requires the endpoint's `closure_enabled` rollout flag. Truncated, failed, malformed and
partially rejected snapshots cannot close jobs. Board observations do not reactivate
an employer-closed vacancy; a later authoritative observation can reopen it. Provider
identities deduplicate supported hosted URLs across sources without matching titles.

The GitLab seed reference is disabled by default; existing operator settings are
preserved. The local verified authoritative endpoint is enabled and has collected
238 jobs. Its 2026-11-02 integration uses the public inventory, a 100,000-item budget
and 8 MiB response limit. All fetches retain HTTPS/DNS/redirect validation and a
16 MiB hard ceiling; other sources retain their 2 MiB default. See
[GitLab integration](integrations/gitlab.md) for application support and limits.

Descriptions retain paragraph/heading boundaries and receive deterministic normalized
sections. The original text remains available. Technology mentions include evidence;
they do not automatically establish mandatory qualifications. Published email contacts
retain source URL, context and purpose. Only explicit CV/application instructions can
automatically select an email channel; generic HR contacts are shown for review.
Operator-set application emails are preserved. Ingestion never sends applications.


Optional source-level ai_normalization classifies exact source lines when deterministic
parsing finds neither requirements nor responsibilities. It uses the same configured
OpenAI provider, cache and daily resource ceiling as CV prioritization. Failure retains
the deterministic result and increments enrichment_failed. No invented requirements
are accepted. The setting defaults off.

Repost signals count forward changes in a known vacancy's published date per source.
Polling the same date does not count. Missing dates or distinct unidentified vacancies
do not establish reposting; no title-based merge or ghost-job verdict is made.

## Automatic employer-source proposals

Board observations recognize supported Greenhouse/Ashby/Workday application links and create
idempotent, disabled Source proposals. Verified-company website enrichment also
examines bounded anchor/iframe links. Proposal creation does not fetch or execute
embedded content, enable scheduling, or infer employer authority. Admin shows
`discovered_from`, the evidence URL, and a `discovery_pending` filter. Verify the
company-to-board relationship, clear pending, then explicitly enable the source and
mark authoritative if appropriate. Pending proposals cannot ingest even if enabled
through a direct database update. The company discovery task can activate recognized ATS links from a verified official
site, retaining evidence; CSV imports and board observations cannot. Explicit operator
disables are preserved on rediscovery.

Once an employer source has supplied a vacancy, board observations retain their own
origins/repost dates without replacing the employer's description, normalized fields,
company, location, application URL, or publication date.

Secret Tel Aviv now defaults to the engineering/programmer category at
`https://jobs.secrettelaviv.com/list/category/programmer/`, rather than the mixed
homepage. Its current collector still enriches only ten listings per run without
pagination, so this source is not a complete technology-job inventory. Existing
operator URLs are preserved by `seed_sources`; update the source explicitly in admin.
