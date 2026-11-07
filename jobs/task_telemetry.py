"""Celery lifecycle logging: identifiers only, never args, results or exceptions."""

import logging
import time
from uuid import UUID

from celery.signals import after_task_publish, task_postrun, task_prerun
from django.db import DatabaseError
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)
POLL_TASKS = frozenset(
    "jobs.tasks." + name
    for name in (
        "schedule_sources",
        "dispatch_submissions",
        "reconcile_submissions",
        "dispatch_batches",
        "schedule_company_discovery",
        "dispatch_evaluations",
        "collect_operations",
        "dispatch_cv_profiles",
        "submit_normalization_batch",
        "poll_normalization_batches",
        "schedule_employer_discovery",
        "schedule_catalogue_inspections",
    )
)


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
    if sender is not None:
        values = new_context(getattr(sender.request, "headers", None))
        values["task_id"] = task_id
        sender.request.telemetry_token = context.set(values)
    if sender and sender.name.startswith("jobs.tasks."):
        sender.request.operations_started = time.monotonic()
        sender.request.operations_correlation = correlation(sender, args, kwargs or {})
        if sender.name in POLL_TASKS:
            return
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
    if (
        sender
        and sender.name.startswith("jobs.tasks.")
        and (sender.name not in POLL_TASKS or state != "SUCCESS")
    ):
        started_at = getattr(sender.request, "operations_started", None)
        logger.log(
            logging.ERROR if state == "FAILURE" else logging.INFO,
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

    if sender is not None:
        token = getattr(sender.request, "telemetry_token", None)
        if token is not None:
            context.reset(token)
            sender.request.telemetry_token = None


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


# Correlation headers are data only; validate again at every task execution.
from celery.signals import (  # noqa: E402
    before_task_publish,
    setup_logging,
    worker_process_shutdown,
    worker_ready,
)

from jobs.observability.context import context, new_context  # noqa: E402
from jobs.observability.metrics import (  # noqa: E402
    QUEUES,
    RUNNING,
    TASK_TIME,
    TASKS,
    observe,
)


@setup_logging.connect
def safe_worker_logging(**kwargs):
    import logging.config

    from django.conf import settings

    logging.config.dictConfig(settings.LOGGING)


@before_task_publish.connect
def publish_context(headers=None, **kwargs):
    if headers is not None:
        # Celery retry reuses request headers: keep the original workflow identity.
        values = new_context(
            {
                **context.get(),
                **{key: headers[key] for key in ("workflow_id", "request_id") if key in headers},
            }
        )
        headers.update(values)


def task_labels(sender):
    from config.celery import app

    name = (
        sender.name
        if sender.name in app.tasks and sender.name.startswith("jobs.tasks.")
        else "other"
    )
    delivery = getattr(sender.request, "delivery_info", None) or {}
    queue = delivery.get("routing_key", "celery")
    return name, queue if queue in QUEUES else "other"


@task_prerun.connect
def metric_started(sender=None, task_id=None, **kwargs):
    if sender is None:
        return
    sender.request.telemetry_start = time.monotonic()
    task, queue = task_labels(sender)
    observe(lambda: TASKS.labels(task, queue, "started").inc())
    observe(lambda: RUNNING.labels(task, queue).inc())


@task_postrun.connect
def metric_finished(sender=None, state=None, **kwargs):
    if sender is None:
        return
    task, queue = task_labels(sender)
    state = state.lower() if state in {"SUCCESS", "FAILURE", "RETRY", "REVOKED"} else "other"
    observe(lambda: TASKS.labels(task, queue, state).inc())
    observe(lambda: RUNNING.labels(task, queue).dec())
    started = getattr(sender.request, "telemetry_start", None)
    if started is not None:
        observe(lambda: TASK_TIME.labels(task, queue).observe(time.monotonic() - started))


def mark_dead(pid):
    import os

    if os.getenv("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(pid)


@worker_process_shutdown.connect
def child_stopped(pid=None, **kwargs):
    if pid:
        mark_dead(pid)


@worker_ready.connect
def parent_child_cleanup(sender=None, **kwargs):
    # Chain the existing billiard callback, including abrupt child termination.
    pool = getattr(getattr(sender, "pool", None), "_pool", None)
    if pool is not None and hasattr(pool, "on_process_down"):
        previous = pool.on_process_down

        def process_down(worker):
            mark_dead(worker.pid)
            if previous:
                previous(worker)

        pool.on_process_down = process_down
