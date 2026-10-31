"""Inspect a public Secret Tel Aviv form without filling, uploading or submitting."""

import http.client
import json

from django.core.management.base import BaseCommand, CommandError

from jobs.services.portals import assess_secret


class Command(BaseCommand):
    help = "Read-only Secret Tel Aviv form preflight; never submits an application."

    def add_arguments(self, parser):
        parser.add_argument("url", help="Public Secret Tel Aviv vacancy URL")

    def handle(self, *args, **options):
        try:
            assessment = assess_secret(options["url"])
        except (ValueError, TypeError, OSError, http.client.HTTPException) as exc:
            raise CommandError(f"Portal preflight failed: {type(exc).__name__}") from exc
        self.stdout.write(json.dumps(assessment.as_dict(), indent=2))
