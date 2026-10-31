# API v1

All endpoints except `/health/` require a Django session. State-changing requests
require a CSRF cookie/header. Staff additionally require a verified TOTP session.

```sh
# Use a dedicated local test session; do not paste real session cookies into shared logs.
curl -H "Cookie: sessionid=$SESSION_ID" \
  'http://localhost:8000/api/v1/jobs/?q=Python&location=Tel%20Aviv'
curl -H "Cookie: sessionid=$SESSION_ID" \
  'http://localhost:8000/api/v1/applications/'
curl -X POST -H "Cookie: sessionid=$SESSION_ID; csrftoken=$CSRF_TOKEN" \
  -H "X-CSRFToken: $CSRF_TOKEN" -H 'Referer: http://localhost:8000/' \
  'http://localhost:8000/api/v1/applications/42/approve/'
```

Read endpoints: jobs list/detail and private applications list/detail. Responses use
DRF page-number pagination (25 records). Job search accepts q and location. An
application belonging to another user returns 404. Approval failures return 400 with
safe validation messages. Unauthenticated API requests return 403 with session auth.

Draft creation/editing and document upload currently use HTML forms. Do not expose
unimplemented writable model APIs. The generated OpenAPI schema is available at
`/api/v1/schema/`:

```sh
uv run python manage.py spectacular --file /tmp/jobstream-openapi.yml --validate --fail-on-warn
```

Health endpoint is process liveness only. Dependency readiness should be monitored
separately; do not expose database or infrastructure details in public responses.

## Secret Tel Aviv batch details

`POST /api/v1/batches/` optionally accepts `portal_answers` alongside the document,
job IDs and request UUID. Fields are `first_name`, `last_name`, `email`, `phone`,
`agree_terms` (explicit true), `forward_headhunters` (boolean, default false), and
`terms_url` (`https://www.secrettelaviv.com/terms-and-conditions-2`). These values remain
private to the owner and are copied into the per-job draft. They never bypass review.
Missing details can be completed in the individual application edit form.

Normal batch or individual approval supports Secret Tel Aviv without Gmail. It binds
the applicant details, consent, target, current form fingerprint and owned attachment.
Preparation snapshots the form outside approval transactions. Worker submission
revalidates it and performs a single native multipart POST. Explicit 429 responses
schedule safe retries with a shared site cooldown. Uncertain POST outcomes are never
resent automatically; known receipt URLs are checked read-only when possible.

Application responses include channel, portal URL, private answers and an optional
receipt URL. Unsupported employer portals continue to use manual handoff.
