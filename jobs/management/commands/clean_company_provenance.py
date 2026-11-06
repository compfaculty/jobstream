"""Remove aggregator provenance from employer catalogue records."""

from django.core.management.base import BaseCommand
from django.db import transaction

from jobs.models import Company, Source


class Command(BaseCommand):
    help = "Clear Secret Tel Aviv company provenance; dry run unless --apply is supplied."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        companies = Company.objects.filter(source__adapter=Source.Adapter.SECRET)
        if options["apply"]:
            count = companies.update(source=None)
            self.stdout.write(f"Cleared aggregator provenance on {count} companies.")
        else:
            self.stdout.write(
                f"Would clear aggregator provenance on {companies.count()} companies."
            )
