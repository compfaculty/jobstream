# Operations and production gates

## Local maintenance

```sh
docker compose ps
docker compose logs --tail=100 worker beat
uv run python manage.py check
uv run python manage.py makemigrations --check --dry-run
uv run python manage.py seed_sources
```

Use Admin ingestion histories and source last-success dates to detect stale sources.
Monitor approved outbox age, unknown submissions, explicit send rejections, worker
availability, queue length, database latency and storage errors. Never reset an unknown
application to approved just because it is old. Reconcile with Gmail or investigate it.

A source lease recovers after ten minutes; worker soft/hard limits are 240/300 seconds.
Tasks are acknowledged late and never blindly retry uncertain sends. Explicit 429
rejections use durable retry schedules. Keep one Beat scheduler.
Redis uses noeviction; broker failures delay work but do not erase approved database state.

## Backup/restore procedure to validate before launch

1. Take managed PostgreSQL point-in-time backups and encrypted object-storage versions.
2. Back up token encryption keys separately under restricted access.
3. Restore into an isolated environment with workers and Beat **disabled** and outbound
   Gmail blocked. Never start a restored outbox against real accounts automatically.
4. Verify row counts, ownership, document integrity and representative searches.
5. Reconcile any approved/submitting/unknown rows against the original environment and
   provider history before enabling workers. A restore can resurrect previously sent work.
6. Record measured recovery time and loss window against agreed RTO/RPO.

This procedure has not been exercised against a managed production platform yet.

## Hosting decision and Terraform milestone

Choose provider/region, budget ceiling, expected dataset/request mix, data residency,
availability and recovery targets first. Then implement reviewed Terraform for network,
managed containers, PostgreSQL, Redis, private object storage, secrets, Cloudflare,
monitoring and backups. Use encrypted remote state with locking and restricted access.

CI should authenticate through workload OIDC, build once, scan, and deploy by image digest.
Use an explicitly protected production environment, reviewed migration job, rolling health
checks and rollback to a prior image. Prefer additive compatible schema changes; an image
rollback does not reverse destructive migrations. Configure branch protection in the
hosting service when the repository remote is selected. No cloud resources or deploy
credentials are created by this local implementation.

## Worker queues

Compose runs independent control (`celery`), ingestion, preparation and delivery
workers. Concurrency defaults are 1/2/2/1 respectively. Queue routing is configured in
CELERY_TASK_ROUTES. Restart all application workers and Beat together after changing
routes; old workers listening only to the default queue cannot consume the new stage
queues. `make worker` consumes all four queues for lightweight local development.

```sh
docker compose build web
docker compose run --rm --no-deps web python manage.py migrate --noinput
docker compose up -d web worker ingestion-worker preparation-worker delivery-worker beat
docker compose logs --tail=100 preparation-worker delivery-worker
```

Explicit Gmail 429 rejections schedule bounded durable retries; ambiguous delivery
outcomes remain unknown. Do not manually enqueue an unknown send. See
`docs/batch-applications.md` for cooldowns, approvals, cancellation and AI budgets.
