"""Read-only collection health summary; no URLs, payloads or applicant data."""

import json
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Avg, Count, Max, Sum
from django.utils import timezone

from jobs.models import Company, IngestionRun, Source


class Command(BaseCommand):
    help = "Report collection completeness, failures, requests, duration and queue delay."

    def add_arguments(self, parser):
        parser.add_argument("--hours", type=int, default=24)

    def handle(self, *args, **options):
        hours = options["hours"]
        if not 1 <= hours <= 8760:
            raise CommandError("Hours must be 1–8760")
        runs = IngestionRun.objects.filter(started_at__gte=timezone.now() - timedelta(hours=hours))
        metrics = runs.aggregate(
            runs=Count("pk"),
            requests=Sum("request_count"),
            total_duration_seconds=Sum("duration_seconds"),
            average_duration_seconds=Avg("duration_seconds"),
            average_queue_delay_seconds=Avg("queue_delay_seconds"),
            max_queue_delay_seconds=Max("queue_delay_seconds"),
            shadow_closures=Sum("would_close"),
        )
        total = metrics["runs"]
        failed = runs.filter(status="failed").count()
        complete = runs.filter(complete=True).count()
        metrics.update(
            hours=hours,
            completed_inventories=complete,
            complete_batch_fraction=complete / total if total else None,
            failures=failed,
            failure_rate=failed / total if total else None,
            partial_batches=runs.filter(status="partial").count(),
            deferred_batches=runs.filter(status="deferred").count(),
            enabled_endpoints=Source.objects.filter(enabled=True).count(),
            pending_endpoint_reviews=Source.objects.filter(discovery_pending=True).count(),
            pending_company_reviews=Company.objects.filter(review_required=True).count(),
        )
        self.stdout.write(json.dumps(metrics, sort_keys=True))
