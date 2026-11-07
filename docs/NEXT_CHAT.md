# Current work: clean development reset

User explicitly authorized replacing the local development database and migration
history, then clarified that the existing superuser login and enrolled MFA must be
preserved. All other project container/volume data may be discarded. Do not reset
other Docker projects, source code or `.env`.

Current data model and bootstrap: `docs/data-model.md`.

- One fresh `jobs/0001_initial.py` replaces the former migration chain.
- Company.source is removed; Source.kind separates company careers from boards.
- Career jobs use their Source.company. Boards never create/link Companies.
- Arbeitnow/Himalayas are board ingestion providers, not company imports.
- `bootstrap_development` is idempotent and starts sources/discovery disabled.
- Retain existing submission approval, CV truthfulness, ownership and CAPTCHA boundaries.
- No live applications/emails, paid calls or production deployment are authorized.

Read Git status and actual runtime state before continuing; do not treat this note
as evidence that a reset, migration, restoration, rebuild or validation has completed.
Historical integration notes remain useful for provider mechanisms, not migration
instructions or claims about the new database's contents.

## Verified completion checkpoint — 2026-11-07

The local reset and authentication restore completed. All Compose services, including
Infinity, are running; db/redis/storage report healthy. Local /health/ and login return
200, and /admin/ routes to login. Runtime database has only jobs.0001_initial applied
for this app; one superuser and one confirmed TOTP device remain. Counts: 4 companies,
7 disabled sources (4 company career endpoints, 3 boards), 1 disabled company directory
campaign; 0 jobs/applications/documents/mail connections/ordinary users. Temporary
superuser/MFA export was removed after exact field verification.

The full non-browser suite passed 669 tests (9 skipped), and final source/view checks
passed 28 tests. make check and Gitleaks passed. Inspect Git status for any changes after this checkpoint.
No crawls, paid calls, applications or emails were triggered by bootstrap/reset.
