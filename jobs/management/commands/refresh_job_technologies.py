"""Reindex deterministic technology fields without AI calls or changing job content."""

from django.core.management.base import BaseCommand
from django.db import connection, transaction

from jobs.models import Job
from jobs.services.normalization import normalize_description, technology_fields


def refresh_payload(description, payload):
    result = dict(payload) if isinstance(payload, dict) else {}
    requirements = result.get("requirements")
    if not isinstance(requirements, list) or any(
        not isinstance(line, str) for line in requirements
    ):
        requirements = normalize_description(description)["requirements"]
    result.update(technology_fields(description, requirements))
    return result


class Command(BaseCommand):
    help = "Refresh technology mentions and required technology tags on existing jobs; no AI calls."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        changed = last_pk = 0
        while True:
            with transaction.atomic():
                # Use the inventory writer's ordering lock before taking job row locks.
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(720316003)")
                rows = list(
                    Job.objects.select_for_update().filter(pk__gt=last_pk).order_by("pk")[:250]
                )
                if not rows:
                    break
                pending = []
                for job in rows:
                    normalized = refresh_payload(job.description, job.normalized)
                    overrides = dict(job.content_overrides)
                    imported = dict(job.imported_content)
                    if isinstance(overrides.get("normalized"), dict):
                        overrides["normalized"] = refresh_payload(
                            job.description, overrides["normalized"]
                        )
                    if isinstance(imported.get("normalized"), dict):
                        imported["normalized"] = refresh_payload(
                            imported.get("description", job.description), imported["normalized"]
                        )
                    if (normalized, overrides, imported) == (
                        job.normalized,
                        job.content_overrides,
                        job.imported_content,
                    ):
                        continue
                    job.normalized = normalized
                    job.content_overrides = overrides
                    job.imported_content = imported
                    changed += 1
                    pending.append(job)
                if pending and not options["dry_run"]:
                    Job.objects.bulk_update(
                        pending, ["normalized", "content_overrides", "imported_content"]
                    )
                last_pk = rows[-1].pk
        self.stdout.write(
            f"{changed} jobs {'would be refreshed' if options['dry_run'] else 'refreshed'}."
        )
