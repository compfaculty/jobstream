# Security model

## Trust boundaries

Job content and URLs are untrusted, including values entered by staff. Users cannot
access another user's notes, resumes, OAuth credentials or applications. Worker tasks
accept database IDs, not serialized credentials or arbitrary submission payloads.
Administrators can configure ingestion but cannot execute code via source configuration.

Controls implemented:

- Invite-only registration with expiring, one-use tokens; Django password validation.
- Django sessions, CSRF, HttpOnly cookies, HTTPS/secure cookies and HSTS outside local mode.
- Login throttling through django-axes; mandatory TOTP for staff across product/Admin/API.
- Ownership-scoped queries and forms; private downloads require authentication.
- Autoescaped templates; source HTML reduced to text; self-hosted HTMX and restrictive CSP.
- Bounded PDF uploads, randomized private object keys, download-only Content-Disposition.
- Encrypted OAuth tokens; approval integrity and audited state transitions.
- Public-only HTTPS source fetching with DNS pinning, redirect validation and byte limits.
- JSON logs with categories/IDs; no provider response bodies or application content in audit.

## Limits and production requirements

PDF signature checks are format checks, not malware scanning or full PDF validation.
Files are never rendered inline. Add an antivirus/quarantine pipeline before accepting
uploads from an untrusted public audience. Enforce request/body limits at the origin
and edge as well as Django; Django's memory limits alone are not ingress limits.

Database administrators can access private records and alter audit data; this is not
a tamper-proof audit ledger. Production needs least-privilege roles, encrypted storage,
backup access controls and external restricted audit retention. Disable DEBUG and use
a secrets manager. No secrets belong in Terraform variables committed to Git.

The local S3 emulator is disposable and not an authorization validation substitute for
production S3. Production must block public bucket access and restrict IAM to the
private bucket. No public media route exists in Django.

No proxy header is trusted by default. Configure HTTPS termination only at a verified
proxy, strip client-supplied forwarded headers, then explicitly configure secure-proxy
settings. Restrict origin access so Cloudflare protections cannot be bypassed.

Sentry exports category-only errors without request/user/breadcrumb/exception payloads.
OTel is opt-in via OTEL_EXPORTER_OTLP_ENDPOINT, samples 5%, and excludes invite/OAuth
callback URLs. Do not enable HTTP header capture or SQL parameter recording. Treat all
telemetry as sensitive and enforce retention/access controls. Check collector output
before production enablement.

Review dependencies, images, CI actions and OAuth consent before beta. Security scans
are automated checks, not proof that the application is vulnerability-free.

Run `uv run python scripts/scan_secrets.py` for a Docker-based Gitleaks scan of all
tracked and unignored source files. Untracked `.env` and `.venv` are excluded using
Git’s ignore rules. Tracked files are always included, even if also ignored.
