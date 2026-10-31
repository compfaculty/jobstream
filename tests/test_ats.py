import json
from datetime import timedelta

import pytest
from django.utils import timezone

from jobs.models import Job, Source
from jobs.services.ats import AshbyAdapter, GreenhouseAdapter, provider_identity
from jobs.services.ingestion import ingest

URL = "https://boards-api.greenhouse.io/v1/boards/example/jobs?content=true"


def payload(jobs, total=None):
    return json.dumps(
        {"jobs": jobs, "meta": {"total": len(jobs) if total is None else total}}
    ).encode()


def posting(pk=123):
    return {
        "id": pk,
        "title": "Python Engineer",
        "absolute_url": f"https://job-boards.greenhouse.io/example/jobs/{pk}",
        "content": "<h2>Requirements</h2><p>Python and Linux</p>",
        "location": {"name": "Remote - US"},
    }


def test_ats_parse_and_completeness():
    adapter = GreenhouseAdapter()
    jobs = adapter.discover(payload([posting()]), URL, 100)
    assert jobs[0].provider_key == "greenhouse:example:123"
    assert jobs[0].location == "Remote - US"
    assert adapter.complete(payload([]), URL, 100)
    assert not adapter.complete(payload([posting()], total=2), URL, 100)
    assert not adapter.complete(payload([posting(), posting(124)]), URL, 1)
    assert provider_identity(jobs[0].url) == jobs[0].provider_key
    with pytest.raises(ValueError):
        adapter.discover(b"{}", URL, 100)
    with pytest.raises(ValueError):
        adapter.scope("https://example.com/jobs")


def test_ashby_parse():
    adapter = AshbyAdapter()
    url = "https://api.ashbyhq.com/posting-api/job-board/example"
    data = json.dumps(
        {
            "jobs": [
                {
                    "id": "abc",
                    "title": "Engineer",
                    "jobUrl": "https://jobs.ashbyhq.com/example/abc",
                    "applyUrl": "https://jobs.ashbyhq.com/example/abc/application",
                    "descriptionPlain": "Python",
                    "isListed": True,
                }
            ]
        }
    ).encode()
    assert adapter.discover(data, url, 100)[0].provider_key == "ashby:example:abc"
    assert adapter.complete(data, url, 100)


@pytest.mark.django_db
def test_authoritative_snapshot_closes_only_verified_scope(monkeypatch):
    source = Source.objects.create(
        name="Example direct",
        adapter="greenhouse",
        url=URL,
        authoritative=True,
        employer_name="Example",
        enabled=True,
    )
    monkeypatch.setattr("jobs.services.ingestion.fetch_public", lambda url: payload([posting()]))
    ingest(source.pk)
    job = Job.objects.get()
    assert job.verification == "open" and job.active
    assert job.normalized["requirements"] == ["Python and Linux"]
    unrelated = Job.objects.create(canonical_url="https://elsewhere.com/job", title="Other")
    Source.objects.filter(pk=source.pk).update(next_run_at=timezone.now() - timedelta(seconds=1))
    monkeypatch.setattr("jobs.services.ingestion.fetch_public", lambda url: payload([]))
    ingest(source.pk)
    job.refresh_from_db()
    unrelated.refresh_from_db()
    assert job.verification == "closed" and not job.active
    assert unrelated.active


@pytest.mark.django_db
def test_incomplete_or_failed_snapshot_cannot_close(monkeypatch):
    source = Source.objects.create(
        name="Example",
        adapter="greenhouse",
        url=URL,
        authoritative=True,
        employer_name="Example",
        enabled=True,
    )
    job = Job.objects.create(
        canonical_url="https://job-boards.greenhouse.io/example/jobs/123",
        title="Engineer",
        provider_key="greenhouse:example:123",
    )
    monkeypatch.setattr("jobs.services.ingestion.fetch_public", lambda url: payload([], total=2))
    ingest(source.pk)
    job.refresh_from_db()
    assert job.active and job.verification == "unverified"


@pytest.mark.django_db
def test_closed_vacancy_board_repost_does_not_reopen(monkeypatch):
    job = Job.objects.create(
        canonical_url="https://job-boards.greenhouse.io/example/jobs/123",
        title="Engineer",
        provider_key="greenhouse:example:123",
        verification="closed",
        active=False,
    )
    source = Source.objects.create(
        name="Board", adapter="rss", url="https://example.com/feed", enabled=True
    )
    data = b"<rss><channel><item><title>Engineer</title><link>https://boards.greenhouse.io/example/jobs/123</link></item></channel></rss>"
    monkeypatch.setattr("jobs.services.ingestion.fetch_public", lambda url: data)
    ingest(source.pk)
    job.refresh_from_db()
    assert Job.objects.count() == 1
    assert not job.active and job.verification == "closed"


@pytest.mark.django_db
def test_repost_counts_publication_changes_not_polling(monkeypatch):
    source = Source.objects.create(
        name="Board dates", adapter="rss", url="https://example.com/feed", enabled=True
    )

    def feed(day):
        return (
            f"<rss><channel><item><title>Engineer</title><link>https://example.com/job/123</link>"
            f"<pubDate>{day} Oct 2026 12:00:00 GMT</pubDate></item></channel></rss>"
        ).encode()

    for day in [20, 20, 25]:
        Source.objects.filter(pk=source.pk).update(
            next_run_at=timezone.now() - timedelta(seconds=1)
        )
        monkeypatch.setattr(
            "jobs.services.ingestion.fetch_public", lambda url, observed_day=day: feed(observed_day)
        )
        ingest(source.pk)
    assert Job.objects.get().origins.get().repost_count == 1
