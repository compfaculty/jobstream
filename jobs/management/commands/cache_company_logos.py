"""Copy surviving legacy logo files into the database without network enrichment."""

from botocore.exceptions import ClientError
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand

from jobs.models import Company, CompanyLogoBlob
from jobs.services.companies import ENRICHMENT_ERRORS, normalize_logo


class Command(BaseCommand):
    help = "Copy existing company logos into DB storage; missing files require re-enrichment."

    def handle(self, *args, **options):
        copied = failed = 0
        names = Company.objects.exclude(logo="").values_list("logo", flat=True).distinct()
        for name in names.iterator(chunk_size=100):
            if CompanyLogoBlob.objects.filter(pk=name).exists():
                continue
            try:
                with default_storage.open(name, "rb") as source:
                    data = normalize_logo(source.read(2 * 1024 * 1024 + 1))
            except (*ENRICHMENT_ERRORS, ClientError) as exc:
                failed += 1
                self.stderr.write(f"Legacy logo unavailable: {type(exc).__name__}")
                continue
            # Do not replace a concurrent upload/cache operation.
            _, created = CompanyLogoBlob.objects.get_or_create(name=name, defaults={"data": data})
            copied += int(created)
        self.stdout.write(f"{copied} copied, {failed} unavailable (re-enrich to restore).")
