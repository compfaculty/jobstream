"""Inspect the public Cato form without creating or sending an application."""

import json

from django.core.management.base import BaseCommand, CommandError

from jobs.models import Job
from jobs.services import cato_greenhouse


class Command(BaseCommand):
    help = "Read-only Cato schema check; --browser additionally checks sandboxed Chromium controls."

    def add_arguments(self, parser):
        parser.add_argument("posting_id")
        parser.add_argument("--browser", action="store_true")

    def handle(self, *args, **options):
        identity = options["posting_id"]
        try:
            job = Job.objects.get(provider_key=f"greenhouse:catonetworks:{identity}")
            schema = cato_greenhouse.read_schema(job)
            result = {
                "posting_id": identity,
                "schema_supported": True,
                "fingerprint": schema.fingerprint,
            }
            if options["browser"]:
                from jobs.services.cato_browser import probe

                result.update(probe(identity))
        except Exception as exc:
            raise CommandError(f"Read-only check failed: {type(exc).__name__}") from None
        self.stdout.write(json.dumps(result, sort_keys=True))
