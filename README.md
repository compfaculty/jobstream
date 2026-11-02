# Jobstream

Invite-only job search and approved email applications. Django 5.2 LTS, Python 3.14,
PostgreSQL, Redis/Celery, templates + HTMX. Dark, deliberately simple UI.

## Local start

Prerequisites: Git, uv, Docker Desktop with Compose. No cloud account is required.

```sh
test -f .env || cp .env.example .env
# Review .env, then export its local settings into this shell:
set -a
. ./.env
set +a
make setup
make services
make migrate
uv run python manage.py collectstatic --noinput
uv run python manage.py createsuperuser
uv run python manage.py seed_sources
make run
```

Open http://localhost:8000. Log in with the superuser, enroll TOTP MFA, then open
`/admin/` to create invitations and review sources. Invitations are URLs such as
`http://localhost:8000/invite/<token>/`; deliver them privately yourself. There is no
public signup. Invitation email becomes the new account's email.

In two additional terminals, export the same environment and run `make worker` and
`make beat`. Keep only one Beat scheduler. Sources start **disabled**; enabling them
allows network requests. No real job source is contacted merely by installation.

To run the application itself in containers:

```sh
docker compose build
docker compose run --rm web python manage.py migrate
docker compose run --rm web python manage.py createsuperuser
docker compose up -d web worker ingestion-worker preparation-worker delivery-worker browser-worker beat
```

Local PostgreSQL, Redis, and S3 emulator bind to loopback only. Their example
credentials are public development values. The S3 emulator is not a production
storage service and its data is disposable; local host mode uses private filesystem
storage unless S3 variables are configured. Never expose this Compose stack publicly.

## What works

- [Company-first catalogue](docs/company-catalogue.md): CSV/admin import, verified
  official-site discovery, Workday/Greenhouse/Ashby inventories, and reviewed lifecycle closure.

- Invite-only accounts, sessions, login throttling, mandatory staff TOTP.
- Job search, location filters, saved jobs, private notes and application tracking.
- Source management in Django Admin, scheduled ingestion, provenance and deduplication.
- RSS/Atom, JobPosting JSON-LD, Comeet public JSON, and public HTML selectors ported
  from JF for Secret Tel Aviv and Telfed, plus five company profiles and Real Work
  From Anywhere enrichment. The 53-entry legacy catalog includes disabled company
  references awaiting compatibility review. See [source limitations](docs/sources.md).
- Private versioned PDF resumes, approval tied to message/attachment/sender content.
- Connected Gmail OAuth, approved submissions, durable queue, uncertain-send
  reconciliation without blind resend. Requires operator OAuth configuration.
- Session-authenticated read APIs and approval action, OpenAPI at `/api/v1/schema/`.

Browser form submission is intentionally gated. External employer links are available.
Supported Cato Networks applications use the official hosted Greenhouse form after
review and approval. Run the dedicated `browser-worker` for these applications;
Camunda applications use the same isolated worker with an Ashby adapter;
GitLab supports reviewed dynamic Greenhouse questions and reserved local assisted delivery;
other delivery workers do not need Chromium. See the [Cato integration](docs/integrations/cato-networks.md).
Production hosting and Terraform follow the budget/provider decision; this repository
has not provisioned cloud infrastructure or claimed production capacity.

## Tests and checks

With local services running and environment exported:

```sh
make check
make test
uv run playwright install chromium
uv run pytest -m browser
uv run python manage.py benchmark --jobs 10000 --iterations 100
```

Tests use a separate PostgreSQL database, real Redis, and mocked Gmail transports;
no test sends real email. See [testing](docs/testing.md) for S3, concurrency, coverage,
and performance details. See [verification](docs/verification.md) for actual results.

## Documentation

- [Architecture and decisions](docs/architecture.md)
- [Sources and adapter examples](docs/sources.md)
- [Gmail setup and submission guarantees](docs/gmail.md)
- [Security and threat model](docs/security.md)
- [API examples](docs/api.md)
- [Testing and benchmarks](docs/testing.md)
- [Operations, backup and production gates](docs/operations.md)
- [Portal assessment](docs/portals.md)

Documentation changes accompany behavior changes. Keep runnable examples, configuration,
and failure states accurate; CI validates schema drift, code and critical journeys.

### Company profiles and logos

Jobs retain the employer text received from their source and also link to a company
profile. Migration `0007` backfills existing jobs. Names are normalized within a
source, never globally merged by name; jobs without provenance receive separate
provisional profiles. The exact **Company** filter (searchable with JavaScript, a
native select without it) and company pages use stable profile IDs. Keywords still
search the original employer text. The jobs API accepts `?company=<profile-id>`.

Company website verification and curated descriptions are managed in Django Admin.
Verify that a website belongs to the actual employer before marking it verified;
a job-board or ATS domain is not automatically the employer's website. A profile
may appear separately across different sources until its identity is reviewed.

Run enrichment explicitly; it never runs during a user page request:

```sh
# Inspect one verified employer without changing the database or storing images.
# --website is an operator attestation that this is the official employer site.
docker compose exec web python manage.py enrich_companies --company 123 --website https://employer.example/ --dry-run
# Save its website, missing description, and validated logo.
docker compose exec web python manage.py enrich_companies --company 123 --website https://employer.example/
# Refresh up to 20 verified profiles whose last attempt was at least 30 days ago.
docker compose exec web python manage.py enrich_companies --limit 20 --stale-days 30
```

Use `--force` to retry recent profiles, and `--retries 0..3` for transient network
failures. Dry runs still fetch and validate remote content but perform no writes.
The command records attempt timestamps and error categories, preserves existing
logos on failed refreshes and curated descriptions, and exits nonzero on failures.
Official-site JSON-LD organization logos or site icons are downloaded through the
SSRF-protected HTTPS fetcher (validated and pinned public DNS, redirects checked,
15-second connection timeout, 2 MiB response cap). Pillow validates raster images,
limits input to 4 million pixels, and re-encodes a maximum 256×256 PNG without
remote metadata. SVG is deliberately unsupported. Logos use the configured private
file storage and authenticated same-origin image URLs; no browser hotlinking.
Profiles without a validated logo have no avatar placeholder.

Company-by-company implementation status and the accepted matching/answer-reuse policy:
[Integration workflow](docs/company-integration-workflow.md), starting with
[Cato Networks](docs/integrations/cato-networks.md).

Camunda form delivery and its verification boundary: [Camunda / Ashby](docs/integrations/camunda.md).

GitLab collection and reviewed form delivery: [GitLab / Greenhouse](docs/integrations/gitlab.md).
Status: **needs real end-to-end verification**; resume with job 20407 using the checklist in that guide.
