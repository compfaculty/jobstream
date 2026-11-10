"""Database storage for small company logos; legacy media remains readable."""

import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import Storage, default_storage
from django.utils.deconstruct import deconstructible


@deconstructible
class CompanyLogoStorage(Storage):
    def get_available_name(self, name, max_length=None):
        # Fresh names avoid overwriting shared/legacy references and storage probes.
        return f"companies/logos/{uuid.uuid4().hex}.png"

    def _save(self, name, content):
        from jobs.models import CompanyLogoBlob
        from jobs.services.companies import normalize_logo

        data = normalize_logo(content.read(2 * 1024 * 1024 + 1))
        CompanyLogoBlob.objects.create(name=name, data=data)
        return name

    def _open(self, name, mode="rb"):
        from jobs.models import CompanyLogoBlob

        blob = CompanyLogoBlob.objects.filter(pk=name).first()
        if blob is not None:
            return ContentFile(bytes(blob.data), name=name)
        return default_storage.open(name, mode)

    def exists(self, name):
        from jobs.models import CompanyLogoBlob

        return CompanyLogoBlob.objects.filter(pk=name).exists() or default_storage.exists(name)

    def delete(self, name):
        from jobs.models import CompanyLogoBlob

        deleted, _ = CompanyLogoBlob.objects.filter(pk=name).delete()
        if not deleted:
            default_storage.delete(name)

    def url(self, name):
        from django.urls import reverse

        from jobs.models import Company

        pk = Company.objects.filter(logo=name).values_list("pk", flat=True).first()
        if pk is None:
            raise ValueError("Logo is not attached to a company")
        return reverse("company-logo", args=[pk])

    def size(self, name):
        with self.open(name) as content:
            return content.size
