"""Inspect correlation/status/usage without exposing private input or credentials."""

import json

from django.core.management.base import BaseCommand

from jobs.models import AgentExecution


class Command(BaseCommand):
    help = "Inspect recent agent execution metadata (never prompts or CV text)."

    def add_arguments(self, parser):
        parser.add_argument("--execution")

    def handle(self, *args, **options):
        records = AgentExecution.objects.order_by("-created_at")
        if options["execution"]:
            records = records.filter(pk=options["execution"])
        for record in records[:50]:
            self.stdout.write(
                json.dumps(
                    {
                        "execution_id": str(record.pk),
                        "attempt_id": str(record.attempt_id),
                        "status": record.status,
                        "attempt": record.attempt,
                        "error_code": record.error_code,
                        "usage": record.usage,
                        "elapsed_seconds": (record.updated_at - record.created_at).total_seconds(),
                    }
                )
            )
