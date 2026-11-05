"""Celery lifecycle logging: identifiers only, never args, results or exceptions."""

import logging
import time
from uuid import UUID

from celery.signals import after_task_publish, task_postrun, task_prerun
from django.db import DatabaseError
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


def correlation(task, args, kwargs):
    if task.name != "jobs.tasks.evaluate_vacancy":
        return {}
    from jobs.models import AgentExecution

    try:
        execution_id = UUID(str(kwargs.get("pk") if "pk" in kwargs else args[0]))
        attempt_id = (
            AgentExecution.objects.filter(pk=execution_id)
            .values_list("attempt_id", flat=True)
            .first()
        )
        return {"execution_id": str(execution_id), "attempt_id": str(attempt_id or "")}
    except ValueError, IndexError, TypeError, DatabaseError:
        return {}


@task_prerun.connect
def started(sender=None, task_id=None, args=(), kwargs=None, **extra):
    if sender and sender.name.startswith("jobs.tasks."):
        sender.request.operations_started = time.monotonic()
        sender.request.operations_correlation = correlation(sender, args, kwargs or {})
        logger.info(
            "task_started",
            extra={
                "task_id": task_id,
                "task_name": sender.name,
                **sender.request.operations_correlation,
            },
        )


@task_postrun.connect
def finished(sender=None, task_id=None, state=None, **extra):
    if sender and sender.name.startswith("jobs.tasks."):
        started_at = getattr(sender.request, "operations_started", None)
        logger.info(
            "task_finished",
            extra={
                "task_id": task_id,
                "task_name": sender.name,
                "state": state,
                "duration_seconds": (
                    time.monotonic() - started_at if started_at is not None else None
                ),
                **getattr(sender.request, "operations_correlation", {}),
            },
        )


@after_task_publish.connect
def scheduler_published(sender=None, **extra):
    if sender != "jobs.tasks.collect_operations":
        return
    from jobs.services.operations import SCHEDULER_KEY, broker_client

    redis = broker_client()
    try:
        redis.set(SCHEDULER_KEY, str(time.time()), ex=300)
    except RedisError:
        logger.warning("operations_scheduler_probe_unavailable")
    finally:
        redis.close()
