"""Refresh derived job sections without changing originals or operator overrides."""

import json

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from jobs.models import Job
from jobs.services.ai import AIUnavailable
from jobs.services.normalization import normalize_description, normalize_with_ai


class Command(BaseCommand):
    help = "Refresh normalized job sections; dry-run and rules-only unless explicitly requested."

    def add_arguments(self, parser):
        parser.add_argument("--job", type=int)
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--ai", action="store_true")

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 10000:
            raise CommandError("Choose a limit between 1 and 10000.")
        if options["ai"] and not options["apply"]:
            raise CommandError("AI requests require --apply; dry-run never makes paid requests.")
        query = Job.objects.order_by("pk")
        if options["job"] is not None:
            query = query.filter(pk=options["job"])
        counts = {
            "updated": 0,
            "unchanged": 0,
            "overrides_skipped": 0,
            "ai_failed": 0,
            "changed_during_refresh": 0,
        }
        for row in query[: options["limit"]].iterator():
            if {"normalized", "description"} & set(row.content_overrides):
                counts["overrides_skipped"] += 1
                continue
            description = row.description
            normalized = normalize_description(description)
            if options["ai"]:
                try:
                    normalized = normalize_with_ai(description, normalized)
                except AIUnavailable:
                    counts["ai_failed"] += 1
                    continue
            if normalized == row.normalized:
                counts["unchanged"] += 1
                continue
            if options["apply"]:
                with transaction.atomic():
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT pg_advisory_xact_lock(720316003)")
                    current = Job.objects.select_for_update().get(pk=row.pk)
                    if current.description != description or {"normalized", "description"} & set(
                        current.content_overrides
                    ):
                        counts["changed_during_refresh"] += 1
                        continue
                    current.normalized = normalized
                    imported = dict(current.imported_content)
                    if imported.get("description") == description:
                        imported["normalized"] = normalized
                        current.imported_content = imported
                    current.save(update_fields=["normalized", "imported_content"])
            counts["updated"] += 1
        self.stdout.write(json.dumps({"applied": options["apply"], **counts}, sort_keys=True))
