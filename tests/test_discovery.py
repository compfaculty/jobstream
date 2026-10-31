import pytest
from django.core.exceptions import ValidationError

from jobs.models import Job, Source
from jobs.services.discovery import ats_endpoint, from_html, propose
from jobs.services.ingestion import ingest

pytestmark = pytest.mark.django_db


def test_known_links_and_host_boundaries():
    assert ats_endpoint("https://job-boards.greenhouse.io/acme/jobs/123?utm_source=board") == (
        "greenhouse",
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true",
    )
    assert (
        ats_endpoint(
            "https://jobs.ashbyhq.com/acme/12345678-1234-1234-1234-123456789abc/application"
        )[0]
        == "ashby"
    )
    assert ats_endpoint("https://jobs.ashbyhq.com.attacker.example/acme") is None
    assert ats_endpoint("http://jobs.ashbyhq.com/acme") is None
    assert ats_endpoint("https://jobs.ashbyhq.com/acme/../../other") is None


def test_proposal_is_disabled_idempotent_and_preserves_review():
    board = Source.objects.create(name="Board", adapter="rss", url="https://example.com/feed")
    source = propose(
        "https://boards.greenhouse.io/acme/jobs/123", "Acme", "https://example.com/job", board
    )
    assert source.discovery_pending and not source.enabled and not source.authoritative
    assert source.discovery_url == "https://example.com/job"
    assert source.discovered_from == board
    source.discovery_pending = False
    source.authoritative = source.enabled = True
    source.save()
    same = propose(
        "https://boards.greenhouse.io/acme", "Wrong name", "https://example.com/another", board
    )
    assert same.pk == source.pk
    assert same.employer_name == "Acme" and same.authoritative and same.enabled
    assert Source.objects.count() == 2


def test_html_proposal_does_not_execute_or_fetch():
    proposals = from_html(
        b'<iframe src="https://jobs.ashbyhq.com/acme"></iframe>'
        b'<a href="https://jobs.ashbyhq.com/acme">Jobs</a>'
        b'<script>fetch("http://127.0.0.1/private")</script>',
        "https://acme.example/careers",
        "Acme",
    )
    assert len(proposals) == 1
    assert proposals[0].adapter == "ashby"
    assert not proposals[0].enabled


def test_pending_discovery_cannot_ingest_even_if_enabled(monkeypatch):
    source = Source.objects.create(
        name="Pending",
        adapter="rss",
        url="https://example.com/feed",
        enabled=True,
        discovery_pending=True,
    )
    with pytest.raises(ValidationError):
        source.full_clean()

    def forbidden(url):
        raise AssertionError("pending discovery cannot fetch")

    monkeypatch.setattr("jobs.services.ingestion.fetch_public", forbidden)
    ingest(source.pk)
    assert not source.ingestionrun_set.exists()


def test_board_cannot_overwrite_employer_content(monkeypatch):
    employer = Source.objects.create(
        name="Employer", adapter="greenhouse", url="https://example.com/api"
    )
    job = Job.objects.create(
        canonical_url="https://job-boards.greenhouse.io/acme/jobs/123",
        provider_key="greenhouse:acme:123",
        title="Verified title",
        company="Acme",
        description="Employer requirements",
        employer_source=employer,
        verification="open",
    )
    board = Source.objects.create(
        name="Board", adapter="rss", url="https://example.com/feed", enabled=True
    )
    feed = (
        b"<rss><channel><item><title>Board marketing</title>"
        b"<link>https://boards.greenhouse.io/acme/jobs/123</link>"
        b"<description>Old copy</description></item></channel></rss>"
    )
    monkeypatch.setattr("jobs.services.ingestion.fetch_public", lambda url: feed)
    ingest(board.pk)
    job.refresh_from_db()
    assert job.title == "Verified title" and job.description == "Employer requirements"
    assert job.origins.filter(source=board).exists()
