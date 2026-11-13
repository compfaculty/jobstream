# Jobstream

Invite-only job search, factual CV preparation, and explicitly approved applications.
Python 3.14, Django 5.2 LTS, PostgreSQL, Redis/Celery, server-rendered templates and HTMX.

## Local development

Requires uv and Docker Compose. Use an isolated development database; do not run
bootstrap or migration commands against an existing production deployment.

```sh
test -f .env || cp .env.example .env
# Review the development settings before exporting them.
set -a
. ./.env
set +a
make setup
make services
make migrate
uv run python manage.py collectstatic --noinput
uv run python manage.py createsuperuser
uv run python manage.py bootstrap_development
make run
```

Open http://localhost:8000, enroll staff TOTP, and create invitations through Admin.
There is no public signup. Keep invitation URLs private. Development service ports
bind to loopback; their example credentials must never be used for public hosting.
See [operations](docs/OPERATIONS.md) before starting workers or enabling integrations:
workers and Beat can resume previously authorized work. Sources initially default off.

## Project documentation

These are the six maintained Markdown files, including this README:

- [AGENTS.md](AGENTS.md): engineering rules and required approval boundaries.
- [Architecture](docs/ARCHITECTURE.md): what is implemented and its safety guarantees.
- [Milestones](docs/MILESTONES.md): development tasks, acceptance criteria and blockers.
- [Operations](docs/OPERATIONS.md): configuration, integrations, monitoring and recovery.
- [Development](docs/DEVELOPMENT.md): tests, API conventions and adapter development.

Current technical guidance is checked against code; runtime flags and live provider
compatibility require fresh verification. Planned work is labeled in milestones.
Documentation describes behavior; editing it does not deploy or activate that behavior.
Historical test logs and implementation discussions belong in Git/PR/CI records.
Competitor ideas belong in actionable milestones, not separate research documents.
Do not add another guide when a section in these files will suffice.

## Checks

```sh
make check
make check-deploy
make test
```

Tests require isolated PostgreSQL/Redis and fake external providers. Browser, Rust,
synthetic-mail and security checks are described in [development](docs/DEVELOPMENT.md).
No source collection, paid inference, application or email is authorized by setup.
