import logging
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
    if sources:
        logging.getLogger(__name__).info(
            "scheduler_dispatched", extra={"queue": "ingestion", "inspected": len(sources)}
        )


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


@shared_task
def evaluate_vacancy(pk):
    from jobs.services.evaluations import process

    process(pk)


@shared_task
def dispatch_evaluations():
    from jobs.models import AgentExecution
    from jobs.services.evaluations import enabled

    if not enabled():
        return
    now = timezone.now()
    # Reservations throttle publication; process uses its own execution lease.
    with transaction.atomic():
        records = list(
            AgentExecution.objects.select_for_update(skip_locked=True)
            .filter(
                next_dispatch_at__lte=now,
            )
            .filter(
                Q(status__in=["queued", "running"])
                | Q(status="cancelled", cancellation_delivered=False)
            )
            .filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
            .order_by("created_at")[:20]
        )
        for record in records:
            record.next_dispatch_at = now + timedelta(seconds=20)
        AgentExecution.objects.bulk_update(records, ["next_dispatch_at"])
    for record in records:
        # Let the task claim now; lost publication recovers after the reservation.
        evaluate_vacancy.delay(str(record.pk))


@shared_task
def collect_operations():
    from jobs.services.operations import collect_snapshot

    collect_snapshot()


@shared_task
def build_cv_profile(pk):
    from jobs.services.cv_profiles import build

    build(pk)


@shared_task
def dispatch_cv_profiles():
    from django.conf import settings

    from jobs.models import CVRepresentation

    if not settings.CV_REPRESENTATIONS_ENABLED:
        return
    with transaction.atomic():
        records = list(
            CVRepresentation.objects.select_for_update(skip_locked=True)
            .filter(state__in=["pending", "dispatching", "processing"])
            .filter(Q(lease_until__isnull=True) | Q(lease_until__lte=timezone.now()))
            .order_by("pk")[:10]
        )
        for record in records:
            record.state = "dispatching"
            record.lease_until = timezone.now() + timedelta(minutes=5)
            record.save(update_fields=["state", "lease_until"])
    for record in records:
        # Build takes its own processing lease. Expiring dispatch claims recover lost messages.
        build_cv_profile.delay(record.pk)


@shared_task
def submit_normalization_batch():
    from django.conf import settings

    from jobs.models import NormalizationBatch
    from jobs.services.normalization_batch import prepare, submit

    if settings.JOB_NORMALIZATION_BATCH_ENABLED and settings.AI_PILOT_ROLLOUT_APPROVED:
        pending = NormalizationBatch.objects.filter(state="prepared").first()
        batch = pending or prepare()
        if batch:
            submit(batch)


@shared_task
def poll_normalization_batches():
    from jobs.models import NormalizationBatch
    from jobs.services.normalization_batch import TERMINAL, poll

    for batch in NormalizationBatch.objects.exclude(state__in=TERMINAL | {"prepared"}).order_by(
        "created_at"
    )[:20]:
        poll(batch)


@shared_task
def discover_employers(run_id):
    from jobs.services.employer_discovery import step

    step(run_id)


@shared_task
def schedule_employer_discovery():
    from jobs.services.employer_discovery import dispatch

    dispatch()


@shared_task
def inspect_catalogue_company(provenance_id):
    from jobs.services.catalogue_inspections import step

    step(provenance_id)


@shared_task
def schedule_catalogue_inspections():
    from jobs.services.catalogue_inspections import dispatch

    dispatch()
