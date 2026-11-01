import json

from django.core.management.base import BaseCommand, CommandError

from jobs.models import Job
from jobs.services import camunda_ashby


class Command(BaseCommand):
    help = "Read-only Camunda Ashby schema and optional browser-interactivity check."

    def add_arguments(self, parser):
        parser.add_argument("posting_id")
        parser.add_argument("--browser", action="store_true")

    def handle(self, *args, **options):
        try:
            job = Job.objects.get(provider_key=f"ashby:camunda:{options['posting_id']}")
            schema = camunda_ashby.read_schema(job)
            result = {"schema_supported": True, "fingerprint": schema.fingerprint}
            if options["browser"]:
                from jobs.services.ashby_browser import probe

                result.update(probe(camunda_ashby.posting_id(job), schema))
        except Exception as exc:
            raise CommandError(f"Read-only check failed: {type(exc).__name__}") from None
        self.stdout.write(json.dumps(result, sort_keys=True))
