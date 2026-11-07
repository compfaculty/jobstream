import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction

from jobs.models import Company, EmployerDiscoveryCampaign, Job, JobOrigin, Source
from jobs.services import ingestion, inventories
from jobs.services.companies import resolve_company
from jobs.services.sources import Listing

pytestmark = pytest.mark.django_db


def test_clean_bootstrap_is_idempotent(settings):
    settings.DEBUG = True
    call_command("bootstrap_development")
    call_command("bootstrap_development")
    assert Company.objects.count() == 4
    assert Source.objects.count() == 7
    assert Source.objects.filter(kind="board", company__isnull=True).count() == 3
    assert Source.objects.filter(kind="company", company__isnull=False).count() == 4
    assert not Source.objects.filter(enabled=True).exists()
    assert EmployerDiscoveryCampaign.objects.count() == 1
    assert not Job.objects.exists()
    assert not hasattr(Company, "source")


def test_board_cannot_own_company_or_authority():
    company = Company.objects.create(name="Acme")
    source = Source(
        kind="board", name="Board", url="https://board.example/", adapter="rss", company=company
    )
    with pytest.raises(ValidationError):
        source.full_clean()
    with pytest.raises(IntegrityError), transaction.atomic():
        source.save()


def test_company_source_cannot_be_enabled_without_company():
    source = Source(
        name="Careers",
        kind="company",
        url="https://employer.example/",
        adapter="jsonld",
        enabled=True,
    )
    with pytest.raises(ValidationError):
        source.full_clean()


def test_board_names_never_create_or_match_company(monkeypatch):
    company = Company.objects.create(name="Acme")
    source = Source.objects.create(
        name="Board", url="https://board.example/", adapter="rss", enabled=True
    )
    assert resolve_company("Acme", source) is None
    monkeypatch.setattr(
        ingestion,
        "collect",
        lambda *a, **k: (
            inventories.CollectionResult(
                [
                    Listing(
                        "https://board.example/jobs/1",
                        "Engineer",
                        company="Acme",
                        description="Python",
                    )
                ],
                "board",
            ),
            b"",
        ),
    )
    ingestion.ingest(source.pk)
    job = Job.objects.get()
    assert job.company == "Acme" and job.company_profile_id is None
    assert job.employer_source_id is None
    assert JobOrigin.objects.get(job=job).source_id == source.pk
    assert Company.objects.count() == 1 and company.name == "Acme"


def test_company_jobs_reuse_explicit_employer(monkeypatch):
    company = Company.objects.create(name="Acme")
    source = Source.objects.create(
        name="Acme careers",
        kind="company",
        company=company,
        employer_name="Acme",
        url="https://acme.example/jobs",
        adapter="jsonld",
        enabled=True,
    )
    monkeypatch.setattr(
        ingestion,
        "collect",
        lambda *a, **k: (
            inventories.CollectionResult(
                [
                    Listing(
                        "https://acme.example/jobs/1",
                        "Engineer",
                        company="Unreliable label",
                        description="Python",
                    )
                ],
                "company",
            ),
            b"",
        ),
    )
    ingestion.ingest(source.pk)
    job = Job.objects.get()
    assert job.company_profile_id == company.pk and job.company == "Acme"
    assert Company.objects.count() == 1


def test_job_board_endpoints_do_not_duplicate():
    Source.objects.create(name="Board", adapter="rss", url="https://board.example/")
    with pytest.raises(IntegrityError), transaction.atomic():
        Source.objects.create(name="Other label", adapter="rss", url="https://board.example/")


@pytest.mark.parametrize("provider", ["arbeitnow", "himalayas"])
def test_free_job_feeds_keep_board_identity_and_application_route(monkeypatch, provider):
    from jobs.services import company_feeds
    from jobs.services.company_feeds import CompanyRecord, FeedPage

    source = Source.objects.create(
        name=provider,
        kind="board",
        adapter=provider,
        url=company_feeds.ARBEITNOW if provider == "arbeitnow" else company_feeds.HIMALAYAS,
        enabled=True,
    )
    url = f"https://{provider}.example/jobs/1"
    application_url = url + "?utm_source=api"
    row = CompanyRecord(
        "company:acme",
        "job:1",
        "Acme",
        url,
        hiring=True,
        title="Engineer",
        location="Germany",
        job_url=url,
        application_url=application_url,
        description="Python and Linux",
    )
    monkeypatch.setattr(company_feeds, "fetch", lambda *a, **k: FeedPage([row], {}))
    ingestion.ingest(source.pk)
    job = Job.objects.get()
    assert job.company_profile_id is None and job.apply_url == application_url
    assert job.company == "Acme" and not Company.objects.exists()
    assert JobOrigin.objects.get(job=job).source_id == source.pk
