from datetime import timedelta

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Q
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
    from jobs.models import IngestionRun

    IngestionRun.objects.filter(
        status="running", finished_at__isnull=True, started_at__lt=now - timedelta(minutes=10)
    ).update(
        status="failed",
        error_code="WorkerInterrupted",
        finished_at=now,
    )
    with transaction.atomic():
        sources = list(
            Source.objects.select_for_update(skip_locked=True)
            .filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
            .filter(
                enabled=True,
                discovery_pending=False,
                next_run_at__lte=now,
                next_dispatch_at__lte=now,
            )
            .order_by("next_run_at", "pk")[: settings.CRAWL_DISPATCH_BATCH]
        )
        for source in sources:
            source.next_dispatch_at = now + timedelta(minutes=5)
        Source.objects.bulk_update(sources, ["next_dispatch_at"])
    for source in sources:
        ingest_source.delay(source.pk)


@shared_task
def send_application(pk):
    submit(pk)


@shared_task
def send_browser_application(pk):
    submit(pk)


@shared_task
def dispatch_submissions():
    now = timezone.now()
    with transaction.atomic():
        applications = list(
            Application.objects.select_for_update(skip_locked=True)
            .filter(
                status=Application.Status.APPROVED,
                next_dispatch_at__lte=now,
                assisted_delivery=False,
            )
            .order_by("next_dispatch_at")[:20]
        )
        for application in applications:
            application.next_dispatch_at = now + timedelta(minutes=5)
            application.save(update_fields=["next_dispatch_at"])
    for application in applications:
        from jobs.services.portal_delivery import browser_supported

        task = send_browser_application if browser_supported(application) else send_application
        task.delay(application.pk)


@shared_task
def reconcile_submissions():
    stale = Application.objects.filter(
        status=Application.Status.SUBMITTING,
    ).filter(
        Q(assisted_delivery=False, started_at__lt=timezone.now() - timedelta(minutes=10))
        | Q(assisted_delivery=True, started_at__lt=timezone.now() - timedelta(minutes=15))
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


@shared_task
def discover_company_careers(company_id):
    from jobs.models import Company
    from jobs.services.catalogue import discover_company
    from jobs.services.crawl import CrawlDeferred

    company = Company.objects.filter(
        pk=company_id, website_verified=True, discovery_enabled=True
    ).first()
    if company is None:
        return
    try:
        discover_company(company)
    except CrawlDeferred as exc:
        Company.objects.filter(pk=company_id).update(next_discovery_at=exc.until)
    except Exception as exc:
        Company.objects.filter(pk=company_id).update(
            discovery_error=type(exc).__name__[:80],
            next_discovery_at=timezone.now() + timedelta(days=1),
        )
    else:
        Company.objects.filter(pk=company_id).update(
            discovery_error="", next_discovery_at=timezone.now() + timedelta(days=7)
        )


@shared_task
def schedule_company_discovery():
    from jobs.models import Company

    now = timezone.now()
    with transaction.atomic():
        companies = list(
            Company.objects.select_for_update(skip_locked=True)
            .filter(
                website_verified=True,
                discovery_enabled=True,
                next_discovery_at__lte=now,
            )
            .order_by("next_discovery_at", "pk")[: settings.CRAWL_DISPATCH_BATCH]
        )
        for company in companies:
            company.next_discovery_at = now + timedelta(minutes=10)
            company.save(update_fields=["next_discovery_at"])
    for company in companies:
        discover_company_careers.delay(company.pk)
