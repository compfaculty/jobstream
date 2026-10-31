from django.core.management.base import BaseCommand

from jobs.models import Source
from jobs.services.network import canonical_url
from jobs.source_catalog import definitions


def identity(url: str) -> str:
    try:
        return canonical_url(url)
    except ValueError:
        return url


class Command(BaseCommand):
    help = "Add disabled JF source references without changing existing source settings."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", help="List additions without writing."
        )

    def handle(self, *args, **options):
        existing = list(Source.objects.values_list("name", "url"))
        names = {name for name, url in existing}
        urls = {identity(url) for name, url in existing}
        added = 0
        skipped = 0
        for definition in definitions():
            url = identity(definition.url)
            if definition.name in names or url in urls:
                skipped += 1
                continue
            if not options["dry_run"]:
                Source.objects.get_or_create(
                    name=definition.name,
                    defaults={
                        "adapter": definition.adapter,
                        "url": definition.url,
                        "notes": definition.notes,
                        "enabled": False,
                        "max_items": definition.max_items,
                        "employer_name": definition.employer_name,
                        "authoritative": definition.authoritative,
                        "max_response_bytes": definition.max_response_bytes,
                    },
                )
            self.stdout.write(
                f"{'Would add' if options['dry_run'] else 'Added'}: "
                f"{definition.name} ({definition.adapter})"
            )
            names.add(definition.name)
            urls.add(url)
            added += 1
        self.stdout.write(
            f"{'Planned' if options['dry_run'] else 'Added'} {added}; "
            f"preserved {skipped} existing sources. New sources are disabled."
        )
