from io import BytesIO, StringIO
from unittest.mock import Mock

import pytest
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from PIL import Image

from jobs.logo_storage import CompanyLogoStorage
from jobs.models import Company, CompanyLogoBlob
from jobs.services.companies import ENRICHMENT_ERRORS

pytestmark = pytest.mark.django_db


def image_bytes():
    output = BytesIO()
    Image.new("RGB", (32, 32), "red").save(output, "PNG")
    return output.getvalue()


def test_upload_is_in_database_without_media():
    company = Company.objects.create(name="Example")
    company.logo.save("upload.png", ContentFile(image_bytes()))
    name = company.logo.name
    assert company.logo.url == f"/companies/{company.pk}/logo/"
    company.refresh_from_db()
    blob = CompanyLogoBlob.objects.get(pk=name)
    assert bytes(blob.data).startswith(b"\x89PNG")
    with company.logo.open("rb") as content:
        assert content.read() == bytes(blob.data)
    assert company.logo.size == len(blob.data)
    company.logo.delete()
    assert not CompanyLogoBlob.objects.filter(pk=name).exists()


def test_database_logo_survives_storage_failure(authenticated, monkeypatch):
    company = Company.objects.create(name="Example")
    company.logo.save("logo.png", ContentFile(image_bytes()))
    monkeypatch.setattr(default_storage, "open", Mock(side_effect=AssertionError("media accessed")))
    response = authenticated.get(f"/companies/{company.pk}/logo/")
    assert response.status_code == 200
    assert b"".join(response.streaming_content).startswith(b"\x89PNG")
    authenticated.logout()
    assert authenticated.get(f"/companies/{company.pk}/logo/").status_code == 302


def test_legacy_backfill_is_idempotent_and_keeps_missing_files(settings, tmp_path, authenticated):
    settings.MEDIA_ROOT = tmp_path
    name = default_storage.save("companies/logos/legacy.png", ContentFile(image_bytes()))
    company = Company.objects.create(name="Legacy", logo=name)
    missing = Company.objects.create(name="Missing", logo="companies/logos/missing.png")
    output, errors = StringIO(), StringIO()
    call_command("cache_company_logos", stdout=output, stderr=errors)
    assert "1 copied, 1 unavailable" in output.getvalue()
    default_storage.delete(name)
    response = authenticated.get(f"/companies/{company.pk}/logo/")
    assert b"".join(response.streaming_content).startswith(b"\x89PNG")
    call_command("cache_company_logos", stdout=output, stderr=errors)
    assert CompanyLogoBlob.objects.count() == 1
    missing.refresh_from_db()
    assert missing.logo.name == "companies/logos/missing.png"


def test_storage_rejects_active_content():
    with pytest.raises(ENRICHMENT_ERRORS):
        CompanyLogoStorage().save("logo.svg", ContentFile(b"<svg><script/></svg>"))
    assert not CompanyLogoBlob.objects.exists()


def test_admin_widget_can_render_database_logo():
    from jobs.admin import CompanyAdminForm

    company = Company.objects.create(name="Admin logo")
    company.logo.save("logo.png", ContentFile(image_bytes()))
    assert f"/companies/{company.pk}/logo/" in str(CompanyAdminForm(instance=company)["logo"])
