from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from jobs.services.catalogue import import_companies


class Command(BaseCommand):
    help = "Idempotently import name,website[,career_url] CSV; websites remain unverified."

    def add_arguments(self, parser):
        parser.add_argument("csv", type=Path)

    def handle(self, *args, **options):
        try:
            with options["csv"].open(encoding="utf-8-sig", newline="") as stream:
                created, existing = import_companies(stream)
        except (OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            f"{created} created, {existing} existing; review official websites in admin."
        )
