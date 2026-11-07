"""Idempotent clean development defaults; never enable outbound applications."""

from io import StringIO

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from jobs.models import Company, EmployerDiscoveryCampaign, Source
from jobs.services.catalogue import import_companies
from jobs.services.employer_defaults import technology_roles


class Command(BaseCommand):
    help = (
        "Create clean, disabled development sources and company discovery. Does not send or crawl."
    )

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError("Development bootstrap requires DEBUG=1.")
        for name, url, adapter in (
            ("Secret Tel Aviv", "https://jobs.secrettelaviv.com/", "secret_telaviv"),
            ("Arbeitnow", "https://www.arbeitnow.com/api/job-board-api", "arbeitnow"),
            ("Himalayas", "https://himalayas.app/jobs/api", "himalayas"),
        ):
            Source.objects.get_or_create(
                url=url,
                defaults={
                    "name": name,
                    "adapter": adapter,
                    "kind": Source.Kind.BOARD,
                    "enabled": False,
                },
            )
        for name, website, career, adapter in (
            (
                "Cato Networks",
                "https://www.catonetworks.com/",
                "https://boards-api.greenhouse.io/v1/boards/catonetworks/jobs?content=true",
                "greenhouse",
            ),
            (
                "GitLab",
                "https://about.gitlab.com/",
                "https://boards-api.greenhouse.io/v1/boards/gitlab/jobs?content=true",
                "greenhouse",
            ),
            (
                "Camunda",
                "https://camunda.com/",
                "https://api.ashbyhq.com/posting-api/job-board/camunda",
                "ashby",
            ),
            (
                "Akeyless",
                "https://www.akeyless.io/",
                "https://www.comeet.com/jobs/akeyless/27.006",
                "akeyless",
            ),
        ):
            import_companies(StringIO(f"name,website\n{name},{website}\n"))
            company = Company.objects.get(name=name, website=website)
            Source.objects.get_or_create(
                url=career,
                defaults={
                    "name": f"{name} careers",
                    "adapter": adapter,
                    "company": company,
                    "kind": Source.Kind.COMPANY,
                    "employer_name": name,
                    "enabled": False,
                    "discovery_pending": True,
                },
            )
        EmployerDiscoveryCampaign.objects.get_or_create(
            name="Company directory discovery",
            defaults={
                "provider": "pituchim",
                "catalogue_mode": True,
                "hiring_only": False,
                "enabled": False,
                "technology_roles": technology_roles(),
                "daily_search_limit": 100,
            },
        )
        self.stdout.write(
            "Created clean defaults: 4 companies, 7 job sources, "
            "1 directory campaign; all disabled."
        )
