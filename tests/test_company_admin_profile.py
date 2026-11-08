from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from jobs.admin import CompanyAdmin, CompanyAdminForm


def test_profile_fields_are_editable_but_identity_is_protected():
    for field in ("logo", "logo_source_url", "description", "application_email"):
        assert field not in CompanyAdmin.readonly_fields
    for field in ("identity_key", "official_domain", "website_verified", "enrichment_error"):
        assert field in CompanyAdmin.readonly_fields


def test_logo_is_normalized_to_png():
    image = BytesIO()
    Image.new("RGB", (500, 200)).save(image, format="JPEG")
    form = CompanyAdminForm(files={"logo": SimpleUploadedFile("logo.jpg", image.getvalue())})
    form.is_valid()
    assert "logo" not in form.errors
    logo = form.cleaned_data["logo"]
    assert logo.name.endswith(".png")
    with Image.open(logo) as normalized:
        assert normalized.format == "PNG"
        assert normalized.size == (256, 102)


@pytest.mark.parametrize("data", [b"<svg></svg>", b"bad", b"x" * (2 * 1024 * 1024 + 1)])
def test_invalid_logo_is_rejected(data):
    form = CompanyAdminForm(files={"logo": SimpleUploadedFile("logo.png", data)})
    form.is_valid()
    assert "logo" in form.errors


def test_logo_url_is_metadata_and_requires_https():
    form = CompanyAdminForm(data={"logo_source_url": "http://example.com/logo.png"})
    form.is_valid()
    assert "logo_source_url" in form.errors
