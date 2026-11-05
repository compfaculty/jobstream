from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.conf import settings
from django.utils import timezone
from redis import Redis

from jobs.models import Job, JobOrigin, Source
from jobs.services import ingestion
from jobs.services.search import search_jobs
from jobs.services.sources import Listing

pytestmark = pytest.mark.django_db


def test_idempotent_ingestion(monkeypatch):
    source = Source.objects.create(
        name="Example", adapter="rss", url="https://example.com", enabled=True
    )
    monkeypatch.setattr(ingestion, "fetch_public", lambda url: b"feed")
    monkeypatch.setitem(
        ingestion.ADAPTERS,
        "rss",
        Mock(
            discover=Mock(
                return_value=[
                    Listing("https://example.com/job?utm_source=x", "Engineer"),
                    Listing("http://127.0.0.1", "bad"),
                ]
            )
        ),
    )
    ingestion.ingest(source.pk)
    source.refresh_from_db()
    assert source.last_success_at
    assert source.ingestionrun_set.get().rejected == 1
    Source.objects.filter(pk=source.pk).update(next_run_at=timezone.now())
    ingestion.ingest(source.pk)
    assert Job.objects.count() == JobOrigin.objects.count() == 1
    assert source.ingestionrun_set.count() == 2
    ingestion.ingest(source.pk)
    assert source.ingestionrun_set.count() == 2


def test_ingestion_error_and_disabled(monkeypatch):
    source = Source.objects.create(
        name="Fail", adapter="rss", url="https://example.com", enabled=False
    )
    ingestion.ingest(source.pk)
    assert not source.ingestionrun_set.exists()
    source.enabled = True
    source.save()
    monkeypatch.setattr(
        ingestion, "fetch_public", Mock(side_effect=TimeoutError("sensitive content"))
    )
    ingestion.ingest(source.pk)
    source.refresh_from_db()
    assert source.failure_count == 1
    assert source.next_run_at > timezone.now() + timedelta(minutes=15)
    assert source.ingestionrun_set.get().error_code == "TimeoutError"


@pytest.mark.integration
def test_postgres_search_and_redis(job):
    assert search_jobs("Python").count() == 1
    assert search_jobs("missing").count() == 0
    assert Redis.from_url(settings.REDIS_URL).ping()


def test_search_vector_changes_on_update(job):
    job.title = "Rust developer"
    job.description = "Kernel engineering"
    job.save()
    assert search_jobs("Rust").filter(pk=job.pk).exists()
    assert not search_jobs("Python").filter(pk=job.pk).exists()


def test_search_query_budget(job, django_assert_num_queries):
    with django_assert_num_queries(2):
        results = search_jobs("Python", "Tel Aviv")
        assert results.count() == 1
        assert len(list(results[:25])) == 1


def test_realwork_detail_failure_keeps_feed_data(monkeypatch):
    source = Source.objects.create(
        name="Real Work",
        adapter="realwork",
        url="https://www.realworkfromanywhere.com/rss.xml",
        enabled=True,
        max_items=20,
    )
    items = [
        Listing(f"https://example.com/jobs/{i}", f"Engineer {i}", description="Feed description")
        for i in range(12)
    ]
    adapter = ingestion.ADAPTERS["realwork"]
    monkeypatch.setattr(adapter, "discover", Mock(return_value=items))
    monkeypatch.setattr(adapter, "metadata", Mock(side_effect=ValueError("HTTP_404")))
    monkeypatch.setattr(ingestion, "fetch_public", lambda url: b"data")
    monkeypatch.setattr(ingestion.time, "sleep", lambda seconds: None)
    ingestion.ingest(source.pk)
    run = source.ingestionrun_set.get()
    assert run.status == "success" and run.imported == 12
    assert run.enrichment_failed == 10 and run.rejected == 0
    assert Job.objects.filter(description="Feed description").count() == 12


def test_company_detail_requests_are_bounded(monkeypatch):
    source = Source.objects.create(
        name="AI21", adapter="ai21", url="https://www.ai21.com/careers/", enabled=True, max_items=20
    )
    adapter = ingestion.ADAPTERS["ai21"]
    items = [Listing(f"https://example.com/jobs/{i}", f"Engineer {i}") for i in range(12)]
    monkeypatch.setattr(adapter, "discover", Mock(return_value=items))
    metadata = Mock(side_effect=lambda listing, data: listing)
    monkeypatch.setattr(adapter, "metadata", metadata)
    monkeypatch.setattr(ingestion, "fetch_public", lambda url: b"data")
    monkeypatch.setattr(ingestion.time, "sleep", lambda seconds: None)
    ingestion.ingest(source.pk)
    assert source.ingestionrun_set.get().imported == 10
    assert metadata.call_count == 10


@pytest.mark.parametrize("provider_fails", [False, True])
def test_global_ai_assists_one_missing_section_and_falls_back(
    monkeypatch, settings, provider_fails
):
    from jobs.services.ai import AIUnavailable
    from jobs.services.normalization import normalize_description

    settings.JOB_AI_NORMALIZATION_ENABLED = True
    source = Source.objects.create(
        name="Weak sections",
        adapter="rss",
        url="https://example.com",
        enabled=True,
        ai_normalization=False,
    )
    description = "Requirements\nPython"
    listing = Listing(
        "https://example.com/weak-sections", "Python engineer", description=description
    )
    monkeypatch.setattr(ingestion, "fetch_public", lambda url: b"feed")
    monkeypatch.setitem(ingestion.ADAPTERS, "rss", Mock(discover=Mock(return_value=[listing])))
    normalized = normalize_description(description)
    normalized["version"] = "ai-line-sections-v3"
    provider = (
        Mock(side_effect=AIUnavailable("ai_timeout"))
        if provider_fails
        else Mock(return_value=normalized)
    )
    monkeypatch.setattr(ingestion, "normalize_with_ai", provider)
    ingestion.ingest(source.pk)
    provider.assert_called_once()
    job = Job.objects.get()
    run = source.ingestionrun_set.get()
    assert job.description == description
    assert run.imported == 1
    assert run.enrichment_failed == int(provider_fails)
    assert job.normalized["version"] == ("sections-v3" if provider_fails else "ai-line-sections-v3")
