"""Bounded operational probes and metadata-only dashboard queries."""

import time
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from redis import Redis
from redis.exceptions import RedisError

from jobs.models import OperationalSnapshot

QUEUES = ("celery", "ingestion", "preparation", "delivery", "browser-delivery")
SCHEDULER_KEY = "jobstream:operations:beat-published"


def broker_client():
    return Redis.from_url(settings.CELERY_BROKER_URL, socket_connect_timeout=1, socket_timeout=1)


def collect_snapshot():
    """Run on the control queue; never probe dependencies from dashboard requests."""
    from config.celery import app

    started = time.monotonic()
    payload = {"database": "healthy", "redis": "unavailable", "workers": [], "queues": []}
    redis = broker_client()
    try:
        # Include Kombu's default priority buckets, not just the priority-zero list.
        with redis.pipeline(transaction=False) as pipe:
            pipe.ping()
            pipe.get(SCHEDULER_KEY)
            for queue in QUEUES:
                for priority in (0, 3, 6, 9):
                    pipe.llen(queue if priority == 0 else f"{queue}\x06\x16{priority}")
            results = pipe.execute()
        payload["redis"] = "healthy"
        published = float(results[1]) if results[1] else None
        payload["scheduler_published_at"] = published
        payload["queues"] = [
            {"name": queue, "depth": sum(results[2 + i * 4 : 6 + i * 4])}
            for i, queue in enumerate(QUEUES)
        ]
    except RedisError, ValueError, TypeError:
        pass  # The explicit unavailable state is persisted below.
    finally:
        redis.close()
    try:
        # Connection and socket limits also bound the inspect broadcast transport.
        with app.connection_for_read(
            connect_timeout=1,
            transport_options={
                "socket_connect_timeout": 1,
                "socket_timeout": 1,
                "max_retries": 0,
            },
        ) as connection:
            inspector = app.control.inspect(timeout=1.0, connection=connection)
            replies = inspector.active_queues() or {}
            active = inspector.active() or {}
        # Inspection responses include args and kwargs. Persist only task identities.
        payload["active_tasks"] = [
            {
                "worker": str(worker)[:200],
                "task_id": str(task.get("id", ""))[:64],
                "task_name": task["name"],
            }
            for worker, tasks in sorted(active.items())
            for task in tasks[:100]
            if isinstance(task.get("name"), str) and task["name"].startswith("jobs.tasks.")
        ]
        payload["workers"] = [
            {
                "name": str(name)[:200],
                "queues": [q["name"] for q in queues if q.get("name") in QUEUES],
            }
            for name, queues in sorted(replies.items())
        ]
        payload["worker_probe"] = "healthy" if replies else "unavailable"
    except Exception:
        # Broker transports raise several library-specific exceptions; never export them.
        payload["worker_probe"] = "unavailable"
    payload["probe_duration_seconds"] = round(time.monotonic() - started, 3)
    OperationalSnapshot.objects.update_or_create(
        key="infrastructure", defaults={"payload": payload, "observed_at": timezone.now()}
    )
    return payload


def snapshot_context():
    snapshot = OperationalSnapshot.objects.filter(key="infrastructure").first()
    if snapshot is None:
        return {
            "state": "unavailable",
            "observed_at": None,
            "payload": {},
            "scheduler": "unavailable",
        }
    now = timezone.now()
    stale = snapshot.observed_at < now - timedelta(seconds=90)
    payload = snapshot.payload
    published = payload.get("scheduler_published_at")
    scheduler = (
        "unavailable"
        if published is None
        else ("stale" if stale or time.time() - published > 90 else "healthy")
    )
    subscribed = {q for worker in payload.get("workers", []) for q in worker["queues"]}
    queues = [
        {
            **queue,
            "worker_state": "stale"
            if stale
            else ("healthy" if queue["name"] in subscribed else "unavailable"),
        }
        for queue in payload.get("queues", [])
    ]
    return {
        "state": "stale" if stale else "healthy",
        "observed_at": snapshot.observed_at,
        "payload": payload,
        "scheduler": scheduler,
        "queues": queues,
    }
