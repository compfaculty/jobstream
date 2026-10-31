from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.utils import timezone

from jobs.models import Application, Source
from jobs.services.ingestion import ingest
from jobs.services.submissions import finish, reconcile, submit


@shared_task
def ingest_source(source_id):
    ingest(source_id)


@shared_task
def schedule_sources():
    now = timezone.now()
    with transaction.atomic():
        sources = list(
            Source.objects.select_for_update(skip_locked=True)
            .filter(
                enabled=True,
                discovery_pending=False,
                next_run_at__lte=now,
                next_dispatch_at__lte=now,
            )
            .order_by("next_dispatch_at")[:20]
        )
        for source in sources:
            source.next_dispatch_at = now + timedelta(minutes=5)
            source.save(update_fields=["next_dispatch_at"])
    for source in sources:
        ingest_source.delay(source.pk)


@shared_task
def send_application(pk):
    submit(pk)


@shared_task
def dispatch_submissions():
    now = timezone.now()
    with transaction.atomic():
        applications = list(
            Application.objects.select_for_update(skip_locked=True)
            .filter(status=Application.Status.APPROVED, next_dispatch_at__lte=now)
            .order_by("next_dispatch_at")[:20]
        )
        for application in applications:
            application.next_dispatch_at = now + timedelta(minutes=5)
            application.save(update_fields=["next_dispatch_at"])
    for application in applications:
        send_application.delay(application.pk)


@shared_task
def reconcile_submissions():
    stale = Application.objects.filter(
        status=Application.Status.SUBMITTING, started_at__lt=timezone.now() - timedelta(minutes=10)
    )
    for pk in stale.values_list("pk", flat=True)[:100]:
        finish(pk, Application.Status.UNKNOWN, error_code="worker_interrupted")
    due = Application.objects.filter(
        status=Application.Status.UNKNOWN, next_dispatch_at__lte=timezone.now()
    ).order_by("next_dispatch_at", "pk")
    for pk in due.values_list("pk", flat=True)[:100]:
        Application.objects.filter(pk=pk).update(
            next_dispatch_at=timezone.now() + timedelta(minutes=5)
        )
        reconcile(pk)


@shared_task
def prepare_batch_item(item_id):
    from jobs.services.batches import prepare

    prepare(item_id)


@shared_task
def dispatch_batches():
    from django.db.models import Q

    from jobs.models import BatchItem

    now = timezone.now()
    with transaction.atomic():
        items = list(
            BatchItem.objects.select_for_update(skip_locked=True)
            .filter(
                Q(state="queued") | Q(state="preparing", lease_until__lte=now),
                next_dispatch_at__lte=now,
                next_attempt_at__lte=now,
                batch__cancelled_at__isnull=True,
            )
            .order_by("next_dispatch_at")[:20]
        )
        for item in items:
            item.next_dispatch_at = now + timedelta(minutes=5)
            item.save(update_fields=["next_dispatch_at"])
    for item in items:
        prepare_batch_item.delay(item.pk)
