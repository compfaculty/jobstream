"""Explicit search orchestration; browsing remains read-only and sends nothing."""

import pytest
from django.test import Client
from django.utils import timezone

from jobs.models import AgentExecution, Application, ApplicationPreferences, Job, RecommendationRun
from jobs.services import recommendation_runs as runs

pytestmark = pytest.mark.django_db


@pytest.fixture
def source(evaluation_profile, agent_settings):
    return evaluation_profile


def test_post_starts_durable_run_and_get_does_not(authenticated, user, source, job):
    url = "/recommendations/update/"
    assert authenticated.get(url).status_code == 405
    response = authenticated.post(url, {"cv": f"profile:{source.pk}"})
    assert response.status_code == 302
    run = RecommendationRun.objects.get(owner=user)
    assert run.profile == source
    assert run.status == "queued"
    assert not AgentExecution.objects.exists()
    assert not Application.objects.exists()
    response = authenticated.get(response.url)
    assert response.context["recommendation_poll"]
    assert b"Search queued" in response.content
    assert b"every 5s" in response.content


def test_auth_csrf_and_owned_source(authenticated, user, other, source):
    url = "/recommendations/update/"
    assert Client().post(url, {"cv": f"profile:{source.pk}"}).status_code == 302
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(user)
    assert csrf_client.post(url, {"cv": f"profile:{source.pk}"}).status_code == 403
    other_client = Client()
    other_client.force_login(other)
    for value in (f"profile:{source.pk}", f"document:{source.document_id}", "profile:bad", ""):
        other_client.post(url, {"cv": value})
    assert not RecommendationRun.objects.exists()


def test_duplicate_click_reuses_active_run(user, source):
    one = runs.start(user, f"profile:{source.pk}")
    two = runs.start(user, f"document:{source.document_id}")
    assert one.pk == two.pk
    assert RecommendationRun.objects.count() == 1


def test_shortlist_hard_preferences_and_cv_priority(user, source, job):
    ApplicationPreferences.objects.create(owner=user, excluded_technologies=["Java"])
    Job.objects.create(
        canonical_url="https://example.com/java", title="Java engineer", description="Java"
    )
    Job.objects.create(canonical_url="https://example.com/closed", title="Python", active=False)
    Job.objects.create(
        canonical_url="https://example.com/unrelated", title="Designer", description="Figma"
    )
    run = runs.start(user, f"profile:{source.pk}")
    ids, scanned = runs.shortlist(run)
    assert scanned == 3
    assert ids[0] == job.pk
    assert len(ids) == 2


def test_coordinator_completion_recovery_and_cache(user, source, job):
    run = runs.start(user, f"profile:{source.pk}")
    runs.process(run.pk)
    run.refresh_from_db()
    assert run.status == "evaluating"
    assert run.executions.count() == 1
    assert runs.progress(run)["pending"] == 1
    runs.process(run.pk)  # Duplicate task does not create duplicate agents.
    assert AgentExecution.objects.count() == 1
    execution = run.executions.get()
    execution.status = "completed"
    execution.result = {"match_score": 90}
    execution.save()
    runs.process(run.pk)
    run.refresh_from_db()
    assert run.status == "completed"
    assert run.lease_until is None
    assert runs.progress(run)["completed"] == 1
    again = runs.start(user, f"profile:{source.pk}")
    runs.process(again.pk)
    assert AgentExecution.objects.count() == 1
    assert again.executions.get().pk == execution.pk
    assert not Application.objects.exists()


def test_limits_errors_and_no_candidates(user, source, job, monkeypatch):
    monkeypatch.setattr(runs, "EVALUATION_LIMIT", 1)
    monkeypatch.setattr(runs, "SCAN_LIMIT", 2)
    for n in range(3):
        Job.objects.create(canonical_url=f"https://example.com/{n}", title="Python")
    run = runs.start(user, f"profile:{source.pk}")
    ids, scanned = runs.shortlist(run)
    assert scanned == 2 and len(ids) == 1
    Job.objects.all().update(active=False)
    runs.process(run.pk)
    run.refresh_from_db()
    assert run.status == "completed"
    assert run.job_ids == []


def test_failed_agent_visible_and_private(authenticated, user, other, source, job):
    run = runs.start(user, f"profile:{source.pk}")
    runs.process(run.pk)
    run.executions.update(status="failed", error_code="ai_monthly_budget_exceeded")
    runs.process(run.pk)
    response = authenticated.get("/", {"mode": "recommended", "cv": f"profile:{source.pk}"})
    assert response.context["recommendation_progress"]["failed"] == 1
    assert not response.context["recommendation_poll"]
    assert runs.latest(other, source)["recommendation_run"] is None


def test_lease_and_interrupted_setup_recovery(user, source, job, monkeypatch):
    run = runs.start(user, f"profile:{source.pk}")
    RecommendationRun.objects.filter(pk=run.pk).update(
        lease_until=timezone.now() + timezone.timedelta(minutes=1)
    )
    runs.process(run.pk)
    assert not AgentExecution.objects.exists()
    RecommendationRun.objects.filter(pk=run.pk).update(lease_until=timezone.now())
    original = runs.shortlist
    monkeypatch.setattr(runs, "shortlist", lambda run: (_ for _ in ()).throw(RuntimeError("fake")))
    with pytest.raises(RuntimeError, match="fake"):
        runs.process(run.pk)
    run.refresh_from_db()
    assert run.lease_until is None
    monkeypatch.setattr(runs, "shortlist", original)
    runs.process(run.pk)
    assert run.executions.count() == 1


def test_configuration_failure_and_rejected_jobs(user, source, job, settings, monkeypatch):
    from django.core.exceptions import ValidationError

    run = runs.start(user, f"profile:{source.pk}")
    monkeypatch.setattr(
        runs.evaluations, "start", lambda *a, **kw: (_ for _ in ()).throw(ValidationError("fake"))
    )
    runs.process(run.pk)
    run.refresh_from_db()
    assert run.rejected_ids == [job.pk]
    assert run.status == "completed"
    settings.AGENT_BACKEND = "disabled"
    with pytest.raises(ValidationError):
        runs.start(user, f"profile:{source.pk}")


def test_pdf_source_and_storage_failure(user, source, job, monkeypatch):
    monkeypatch.setattr("jobs.services.resumes.extract", lambda doc: "Python and Linux")
    run = runs.start(user, f"document:{source.document_id}")
    runs.process(run.pk)
    execution = run.executions.get()
    assert execution.document_id == source.document_id
    assert execution.profile_id is None
    assert execution.snapshot["input_source"]["kind"] == "uploaded_pdf"
    run.executions.update(status="completed")
    runs.process(run.pk)
    failed = runs.start(user, f"document:{source.document_id}")

    def broken(doc):
        raise OSError("private storage detail must not be exposed")

    monkeypatch.setattr("jobs.services.resumes.extract", broken)
    runs.process(failed.pk)
    failed.refresh_from_db()
    assert failed.status == "failed"
    assert failed.error_code == "cv_or_configuration_unavailable"


def test_stale_coordinator_cannot_publish_or_overwrite(user, source, job, monkeypatch):
    run = runs.start(user, f"profile:{source.pk}")
    original = runs.shortlist
    replacement_lease = timezone.now() + timezone.timedelta(hours=1)

    def replaced(current):
        result = original(current)
        RecommendationRun.objects.filter(pk=current.pk).update(lease_until=replacement_lease)
        return result

    monkeypatch.setattr(runs, "shortlist", replaced)
    runs.process(run.pk)
    run.refresh_from_db()
    assert run.lease_until == replacement_lease
    assert run.status == "preparing"
    assert not AgentExecution.objects.exists()


def test_dispatch_and_worker_contract(user, source, job, monkeypatch):
    from jobs import tasks

    run = runs.start(user, f"profile:{source.pk}")
    published = []
    monkeypatch.setattr(tasks.coordinate_recommendations, "delay", published.append)
    tasks.dispatch_recommendation_runs()
    assert published == [str(run.pk)]
    tasks.coordinate_recommendations(str(run.pk))
    assert run.executions.count() == 1


@pytest.mark.django_db(transaction=True)
def test_simultaneous_start_is_one_owner_run(user, source):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections

    barrier = Barrier(2)

    def click():
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            return runs.start(user, f"profile:{source.pk}").pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: click(), range(2)))
    assert results[0] == results[1]
    assert RecommendationRun.objects.filter(owner=user).count() == 1
