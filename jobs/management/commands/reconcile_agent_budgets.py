"""Repair attributable successful legacy reservations; never reset daily quota totals."""

import json

from django.core.management.base import BaseCommand

from jobs.services.agent_budget import reconcile_legacy_successes


class Command(BaseCommand):
    help = "Settle completed single-attempt legacy evaluations against reported token usage."

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(reconcile_legacy_successes(), sort_keys=True))
