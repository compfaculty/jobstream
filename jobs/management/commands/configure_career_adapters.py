"""Apply reviewed public endpoint mappings without starting collection."""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from jobs.models import Source

# Employer pages inspected read-only on 2026-11-10. No provider tokens required.
ENDPOINTS = {
    "https://www.proteantecs.com/careers#openpositions": (
        "comeet",
        "https://www.comeet.com/jobs/proteantecs/D5.00E",
    ),
    "https://www.4manalytics.com/careers#open-roles": (
        "ashby",
        "https://api.ashbyhq.com/posting-api/job-board/4M",
    ),
    "https://alice.io/careers#open-positions": (
        "comeet",
        "https://www.comeet.com/jobs/alice/D5.005",
    ),
    "https://alviere.com/careers/": ("bamboohr", "https://alviere.bamboohr.com/careers/list"),
    "https://ex.co/careers/": ("comeet", "https://www.comeet.com/jobs/exco/F0.005"),
    "https://www.withfaye.com/careers/": ("comeet", "https://www.comeet.com/jobs/withfaye/87.00A"),
    "https://www.firebolt.io/careers": ("comeet", "https://www.comeet.com/jobs/firebolt/26.004"),
    "https://www.foundational.io/careers": ("foundational", "https://www.foundational.io/careers"),
    "https://g2risksolutions.com/careers": (
        "comeet",
        "https://www.comeet.com/jobs/g2risksolutions/C9.003",
    ),
}


class Command(BaseCommand):
    help = "Preview reviewed adapter mappings; --apply updates disabled pending sources only."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")

    @transaction.atomic
    def handle(self, *args, **options):
        count = 0
        for original, (adapter, endpoint) in ENDPOINTS.items():
            source = (
                Source.objects.select_for_update().filter(url=original, needs_adapter=True).first()
            )
            if source is None:
                continue
            if source.enabled or source.authoritative or source.closure_enabled:
                raise CommandError("Pending source has unsafe activation state")
            if Source.objects.filter(url=endpoint).exclude(pk=source.pk).exists():
                raise CommandError(f"Endpoint already configured; review source {source.pk}")
            self.stdout.write(f"{source.pk}: {source.name}: {adapter} -> {endpoint}")
            if options["apply"]:
                source.adapter, source.url = adapter, endpoint
                source.needs_adapter = False
                source.discovery_url = source.discovery_url or original
                source.notes = (
                    source.notes + "\nPublic collection adapter configured; "
                    "application submission requires separate support and approval."
                ).strip()
                source.full_clean()
                source.save(
                    update_fields=["adapter", "url", "needs_adapter", "discovery_url", "notes"]
                )
            count += 1
        self.stdout.write(
            f"{'Updated' if options['apply'] else 'Would update'} {count} disabled sources."
        )
