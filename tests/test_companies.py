from io import BytesIO, StringIO
from unittest.mock import Mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from PIL import Image

from jobs.models import Company, Job, JobOrigin, Source
from jobs.services import companies, ingestion
from jobs.services.search import search_jobs
from jobs.services.sources import Listing

pytestmark = pytest.mark.django_db


@pytest.fixture
def source(db):
    return Source.objects.create(
        name="Employer feed", adapter="rss", url="https://example.com/feed"
    )


@pytest.fixture
def company(source):
    company = Company.objects.create(name="Example")
    source.kind, source.company = Source.Kind.COMPANY, company
    source.save()
    return company


def png():
    data = BytesIO()
    Image.new("RGB", (500, 200), "red").save(data, format="PNG")
    return data.getvalue()


def test_secret_postings_do_not_create_catalogue_companies(source):
    source.adapter = Source.Adapter.SECRET
    source.save()
    # An identical catalogue name is not evidence of employer identity.
    Company.objects.create(name="Example")
    assert companies.resolve_company("Example", source) is None
    assert Company.objects.count() == 1


def test_secret_ingestion_stores_job_without_creating_company(monkeypatch, source):
    source.adapter = Source.Adapter.SECRET
    source.enabled = True
    source.save()
    listing = Listing("https://example.com/new-job", "Engineer", company="New employer")
    monkeypatch.setattr(ingestion, "fetch_public", lambda url: b"board")
    monkeypatch.setitem(
        ingestion.ADAPTERS, Source.Adapter.SECRET, Mock(discover=Mock(return_value=[listing]))
    )
    ingestion.ingest(source.pk)
    job = Job.objects.get(canonical_url=listing.url)
    assert job.company == "New employer"
    assert job.company_profile_id is None
    assert not Company.objects.exists()
    assert JobOrigin.objects.filter(job=job, source=source).exists()


def test_company_filter_pages_and_api(authenticated, client, company, job):
    job.company_profile = company
    job.save()
    other = Job.objects.create(title="Other", company="Other", canonical_url="https://other.com/j")
    assert list(search_jobs("Example", company=str(company.pk))) == [job]
    assert not search_jobs(company="invalid").exists()
    assert not search_jobs(company="9" * 50).exists()
    assert not search_jobs(company="999999999").exists()
    response = authenticated.get("/", {"company": company.pk})
    assert list(response.context["page"]) == [job]
    assert f"/companies/{company.pk}/".encode() in response.content
    assert list(authenticated.get(f"/companies/{company.pk}/").context["page"]) == [job]
    assert authenticated.get("/api/v1/jobs/", {"company": company.pk}).json()["count"] == 1
    assert authenticated.get("/", {"company": "bad"}).context["page"].paginator.count == 0
    assert authenticated.get("/companies/999999999/").status_code == 404
    job.active = False
    job.save()
    assert authenticated.get(f"/companies/{company.pk}/").context["page"].paginator.count == 0
    client.logout()
    assert client.get(f"/companies/{company.pk}/").status_code == 302
    assert other.company_profile_id is None


def test_ingestion_uses_explicit_source_company(monkeypatch, source, company):
    source.enabled = True
    source.save()
    monkeypatch.setattr(ingestion, "fetch_public", lambda url: b"feed")
    monkeypatch.setitem(
        ingestion.ADAPTERS,
        "rss",
        Mock(
            discover=Mock(
                return_value=[
                    Listing("https://example.com/a", "Engineer", company="Example"),
                    Listing("https://example.com/b", "Designer", company="Example"),
                ]
            )
        ),
    )
    ingestion.ingest(source.pk)
    assert Job.objects.filter(company_profile=company).count() == 2
    manual = Company.objects.create(name="Reviewed employer", identity_key="manual")
    Job.objects.filter(canonical_url="https://example.com/a").update(company_profile=manual)
    from django.utils import timezone

    Source.objects.filter(pk=source.pk).update(next_run_at=timezone.now())
    ingestion.ingest(source.pk)
    assert Job.objects.get(canonical_url="https://example.com/a").company_profile == company


@pytest.mark.parametrize("value", [b"<svg/>", b"<html>not an image</html>", b"\x89PNGbroken"])
def test_invalid_logos_rejected(value):
    with pytest.raises(companies.ENRICHMENT_ERRORS):
        companies.normalize_logo(value)


def test_logo_dimensions_and_size():
    data = companies.normalize_logo(png())
    with Image.open(BytesIO(data)) as image:
        assert image.size == (256, 102) and image.format == "PNG"
    with pytest.raises(ValueError):
        companies.normalize_logo(b"x" * (2 * 1024 * 1024 + 1))
    data = BytesIO()
    Image.new("1", (2001, 2000)).save(data, format="PNG")
    with pytest.raises(ValueError, match="pixel"):
        companies.normalize_logo(data.getvalue())


def test_metadata_graph_and_relative_icon():
    text, logo = companies.website_metadata(
        b"""<meta name="description" content="A company">
    <script type="application/ld+json">{"@graph":[{"@type":"Organization",
    "logo":{"url":"/logo.png"}}]}</script>""",
        "https://example.com/about",
    )
    assert text == "A company" and logo == ["https://example.com/logo.png"]
    assert companies.website_metadata(
        b'<link rel="icon" href="icon.png">', "https://example.com/about/"
    )[1] == ["https://example.com/about/icon.png"]
    with pytest.raises(ValueError):
        companies.website_metadata(
            b'<link rel="icon" href="http://127.0.0.1/a">', "https://example.com"
        )
    assert companies.website_metadata(
        b'<script type="application/ld+json">bad</script>', "https://example.com"
    ) == ("", [])


def test_enrichment_dry_run_storage_and_logo_access(
    monkeypatch, company, authenticated, settings, tmp_path
):
    settings.MEDIA_ROOT = tmp_path
    payload = b'<meta name="description" content="About Example"><link rel="icon" href="/logo.png">'
    fetch = Mock(side_effect=lambda url: (png() if url.endswith("logo.png") else payload, url))
    monkeypatch.setattr(companies, "fetch_public_document", fetch)
    output = StringIO()
    call_command(
        "enrich_companies",
        company=company.pk,
        website="https://example.com/",
        dry_run=True,
        stdout=output,
    )
    company.refresh_from_db()
    assert not company.website and not company.logo and not company.enriched_at
    assert not list(tmp_path.rglob("*.png"))
    call_command(
        "enrich_companies", company=company.pk, website="https://example.com/", stdout=output
    )
    company.refresh_from_db()
    assert company.website_verified and company.description == "About Example"
    assert company.logo and company.enriched_at
    response = authenticated.get(f"/companies/{company.pk}/logo/")
    assert response.status_code == 200 and response["Content-Type"] == "image/png"
    assert b"".join(response.streaming_content).startswith(b"\x89PNG")
    old = company.logo.name
    fetch.reset_mock()
    call_command("enrich_companies", stdout=output)
    fetch.assert_not_called()  # Fresh profiles are skipped.
    call_command("enrich_companies", company=company.pk, force=True, stdout=output)
    company.refresh_from_db()
    assert old != company.logo.name and not company.logo.storage.exists(old)
    assert authenticated.get("/companies/999999999/logo/").status_code == 404
    authenticated.logout()
    assert authenticated.get(f"/companies/{company.pk}/logo/").status_code == 302


def test_enrichment_failure_and_retry(monkeypatch, company):
    with pytest.raises(ValueError, match="verified"):
        companies.enrich_company(company)
    company.website = "https://example.com/"
    company.website_verified = True
    company.save()
    fetch = Mock(side_effect=[TimeoutError(), (b"<html>About</html>", company.website)])
    monkeypatch.setattr(companies, "fetch_public_document", fetch)
    companies.enrich_company(company)
    assert fetch.call_count == 2
    fetch.side_effect = ValueError("sensitive remote content")
    errors = StringIO()
    with pytest.raises(CommandError):
        call_command("enrich_companies", company=company.pk, force=True, stderr=errors)
    company.refresh_from_db()
    assert company.enrichment_error == "ValueError"
    assert "sensitive" not in errors.getvalue()
    assert company.enriched_at  # Last successful result remains available.


@pytest.mark.parametrize(
    "options",
    [
        {"limit": 0},
        {"retries": 4},
        {"stale_days": -1},
        {"website": "https://example.com"},
        {"company": 99999999},
    ],
)
def test_command_bounds(options):
    with pytest.raises(CommandError):
        call_command("enrich_companies", **options)


def test_stale_logo_falls_back_to_icon(monkeypatch, company, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    company.website = "https://example.com/"
    company.website_verified = True
    payload = b"""<script type="application/ld+json">
    {"@type":"Organization", "logo":"/missing.png"}</script>
    <link rel="icon" href="/icon.svg"><link rel="apple-touch-icon" href="/valid.png">"""

    def fetch(url):
        if url.endswith("missing.png"):
            raise ValueError("HTTP_404")
        if url.endswith("valid.png"):
            return png(), url
        assert url == company.website
        return payload, url

    monkeypatch.setattr(companies, "fetch_public_document", fetch)
    companies.enrich_company(company)
    assert company.logo_source_url == "https://example.com/valid.png"
    assert company.logo


def test_logo_drops_source_metadata():
    from PIL.PngImagePlugin import PngInfo

    original = BytesIO()
    metadata = PngInfo()
    metadata.add_text("Comment", "untrusted source metadata")
    exif = Image.Exif()
    exif[270] = "untrusted EXIF description"
    Image.new("RGB", (24, 24)).save(original, format="PNG", pnginfo=metadata, exif=exif)
    normalized = companies.normalize_logo(original.getvalue())
    with Image.open(BytesIO(normalized)) as image:
        assert not image.getexif()
        assert "Comment" not in image.info
