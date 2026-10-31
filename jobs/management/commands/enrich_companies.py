from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from jobs.models import Company
from jobs.services.companies import ENRICHMENT_ERRORS, enrich_company
from jobs.services.network import canonical_url


class Command(BaseCommand):
    help = "Update company metadata and logos from operator-verified official websites."

    def add_arguments(self, parser):
        parser.add_argument("--company", type=int, help="Only this company ID")
        parser.add_argument(
            "--website", help="Set and attest the official HTTPS website (with --company)"
        )
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--stale-days", type=int, default=30)
        parser.add_argument("--retries", type=int, default=1)
        parser.add_argument("--force", action="store_true")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        if not 1 <= options["limit"] <= 100 or not 0 <= options["retries"] <= 3:
            raise CommandError("Limit must be 1–100; retries must be 0–3.")
        if not 1 <= options["stale_days"] <= 3650:
            raise CommandError("Stale days must be 1–3650.")
        if options["website"] and not options["company"]:
            raise CommandError("--website requires --company; verify the employer identity first.")
        companies = Company.objects.order_by("enrichment_attempted_at", "pk")
        if options["company"]:
            companies = companies.filter(pk=options["company"])
            if not companies.exists():
                raise CommandError("Company not found.")
        if options["website"]:
            try:
                website = canonical_url(options["website"])
                if len(website) > 2048:
                    raise ValueError("Website URL exceeds limit")
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
        else:
            website = None
            companies = companies.filter(website_verified=True).exclude(website="")
        if not options["force"] and not website:
            cutoff = timezone.now() - timedelta(days=options["stale_days"])
            companies = companies.filter(
                Q(enrichment_attempted_at__isnull=True) | Q(enrichment_attempted_at__lt=cutoff)
            )
        updated = failed = 0
        for company in companies[: options["limit"]]:
            if website:
                company.website = website
                company.website_verified = True
                if not options["dry_run"]:
                    company.save(update_fields=["website", "website_verified"])
                    from jobs.services.catalogue import verify_company

                    company = verify_company(company)
            try:
                result = enrich_company(
                    company, dry_run=options["dry_run"], retries=options["retries"]
                )
            except ENRICHMENT_ERRORS as exc:
                failed += 1
                if not options["dry_run"]:
                    Company.objects.filter(pk=company.pk).update(
                        enrichment_attempted_at=timezone.now(),
                        enrichment_error=type(exc).__name__[:80],
                    )
                self.stderr.write(f"Company {company.pk}: {type(exc).__name__}")
            else:
                updated += 1
                self.stdout.write(
                    f"Company {company.pk}: logo={'yes' if result['has_logo'] else 'no'}"
                )
        prefix = "Dry run: " if options["dry_run"] else ""
        self.stdout.write(f"{prefix}{updated} updated, {failed} failed.")
        if failed:
            raise CommandError("Some companies could not be enriched; see error categories above.")
