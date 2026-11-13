"""Both feeds remain owner-scoped and never launch evaluations during browsing."""

import pytest
from bs4 import BeautifulSoup
from django.utils import timezone

from jobs.models import AgentExecution, ApplicationPreferences, Document, Job
from jobs.services import evaluations
from jobs.services.search import search_jobs

pytestmark = pytest.mark.django_db


def complete(owner, job, profile, score=85, *, pdf=False):
    record = (
        evaluations.start(owner, job, document_id=profile.document_id)
        if pdf
        else evaluations.start(owner, job, profile.pk)
    )
    record.status = "completed"
    record.result = {"match_score": score}
    record.save()
    return record


@pytest.fixture
def source(evaluation_profile, agent_settings, monkeypatch):
    monkeypatch.setattr("jobs.services.resumes.extract", lambda doc: "Python and Linux")
    return evaluation_profile


def recommended(client, source, **params):
    return client.get("/", {"mode": "recommended", "cv": f"profile:{source.pk}", **params})


def test_all_jobs_includes_closed_and_orders_by_added_date(authenticated, job):
    closed = Job.objects.create(
        canonical_url="https://example.com/closed-feed",
        title="Python Python Python",
        active=False,
        description="Python",
    )
    Job.objects.filter(pk=job.pk).update(discovered_at=timezone.now())
    response = authenticated.get("/", {"q": "Python"})
    assert list(response.context["page"]) == [job, closed]
    assert b"Closed" in response.content
    assert search_jobs().count() == 1  # Other callers retain their active-only contract.
    assert authenticated.get("/", {"mode": "invalid"}).context["job_mode"] == "all"


def test_threshold_current_cv_and_read_only(authenticated, user, job, source, monkeypatch):
    complete(user, job, source, 70, pdf=True)
    counts = AgentExecution.objects.count()
    monkeypatch.setattr(
        evaluations, "start", lambda *a, **kw: pytest.fail("browsing created an evaluation")
    )
    response = authenticated.get("/", {"mode": "recommended"})
    assert response.context["selected_cv"] == f"document:{source.document_id}"
    assert [j.pk for j in response.context["page"]] == [job.pk]
    assert response.context["page"][0].recommendation_score == 70
    soup = BeautifulSoup(response.content, "html.parser")
    assert soup.select_one('input[name="cv"]')["value"] == f"document:{source.document_id}"
    assert AgentExecution.objects.count() == counts
    assert b"Good for you \xc2\xb7 70% fit" in response.content


@pytest.mark.parametrize("score", [0, 15, 69, 70, 100])
def test_all_valid_scores_are_labeled(authenticated, user, job, source, score):
    complete(user, job, source, score)
    response = recommended(authenticated, source)
    assert response.context["page"][0].recommendation_score == score
    assert (b"Good for you" if score >= 70 else b"Bad for you") in response.content
    assert f"{score}% fit".encode() in response.content


@pytest.mark.parametrize("score", ["90", None, 101, True, -1])
def test_invalid_score_is_not_displayed(authenticated, user, job, source, score):
    complete(user, job, source, score)
    response = recommended(authenticated, source)
    assert response.context["page"][0].recommendation_score is None
    assert b"Not evaluated" in response.content


@pytest.mark.parametrize("change", ["description", "normalized", "location", "title", "company"])
def test_changed_job_invalidates_score(authenticated, user, job, source, change):
    complete(user, job, source)
    assert recommended(authenticated, source).context["page"].paginator.count == 1
    setattr(
        job, change, {"requirements": ["Rust"]} if change == "normalized" else "Changed content"
    )
    job.save()
    assert recommended(authenticated, source).context["page"][0].recommendation_score is None


def test_preferences_profile_and_closed_job_invalidations(authenticated, user, job, source):
    complete(user, job, source)
    preferences = ApplicationPreferences.objects.create(
        owner=user, excluded_technologies=["Python"]
    )
    assert recommended(authenticated, source).context["page"][0].recommendation_score is None
    preferences.delete()
    source.facts = [{"id": "new", "text": "Rust"}]
    source.save()
    assert recommended(authenticated, source).context["page"][0].recommendation_score is None
    source.refresh_from_db()
    complete(user, job, source)
    assert recommended(authenticated, source).context["page"].paginator.count == 1
    job.active = False
    job.save()
    assert recommended(authenticated, source).context["page"].paginator.count == 0


def test_current_pdf_hash_required(authenticated, user, job, source):
    complete(user, job, source, pdf=True)
    Document.objects.filter(pk=source.document_id).update(sha256="b" * 64)
    response = authenticated.get("/", {"mode": "recommended"})
    assert response.context["page"][0].recommendation_score is None


def test_other_users_scores_and_cv_selection_are_private(authenticated, user, other, job, source):
    complete(user, job, source)
    from jobs.models import ResumeProfile

    other_doc = Document.objects.create(
        owner=other, name="Private other CV", file="unused.pdf", sha256="c" * 64, size=1
    )
    other_profile = ResumeProfile.objects.create(
        owner=other, document=other_doc, name="Private other profile", facts=source.facts
    )
    complete(other, job, other_profile)
    for cv in (f"document:{other_doc.pk}", f"profile:{other_profile.pk}", "document:invalid"):
        response = authenticated.get("/", {"mode": "recommended", "cv": cv})
        assert response.context["page"].paginator.count == 0
        assert response.context["recommendation_source_invalid"]
        assert b"Private other" not in response.content
    AgentExecution.objects.filter(owner=user).delete()
    assert recommended(authenticated, source).context["page"][0].recommendation_score is None


def test_latest_current_evaluation_wins(authenticated, user, job, source):
    old = complete(user, job, source, 95)
    AgentExecution.objects.create(
        owner=user,
        job=job,
        profile=source,
        document=source.document,
        key="latest-different-attempt",
        snapshot=old.snapshot,
        model=old.model,
        version=old.version,
        status="completed",
        result={"match_score": 50},
    )
    assert recommended(authenticated, source).context["page"][0].recommendation_score == 50


def test_no_cv_and_no_scores_are_actionable(authenticated, user, job, agent_settings):
    response = authenticated.get("/", {"mode": "recommended"})
    assert b"Upload a CV to get started" in response.content
    assert AgentExecution.objects.count() == 0


def test_recommendation_filters_pagination_and_hx_preserve_mode(authenticated, user, job, source):
    for n in range(27):
        j = Job.objects.create(
            canonical_url=f"https://example.com/feed/{n}",
            title="Backend Engineer",
            description="Python",
            location="Tel Aviv",
        )
        complete(user, j, source, 70 + n)
    response = recommended(authenticated, source, category="backend", q="Python", page="2")
    assert response.context["page"].paginator.count == 27
    assert len(response.context["page"]) == 2
    assert [j.recommendation_score for j in response.context["page"]] == [71, 70]
    assert b"mode=recommended" in response.content
    partial = authenticated.get(
        "/",
        {"mode": "recommended", "cv": f"profile:{source.pk}", "category": "backend"},
        HTTP_HX_REQUEST="true",
    )
    assert b"Find your next job" not in partial.content
    assert b'name="mode" value="recommended" form="job-search"' in partial.content


@pytest.mark.parametrize("status", ["queued", "running", "failed", "cancelled"])
def test_only_completed_results_qualify(authenticated, user, job, source, status):
    record = complete(user, job, source)
    record.status = status
    record.save()
    assert recommended(authenticated, source).context["page"][0].recommendation_score is None


def test_hard_preferences_exclude_even_a_corrupted_high_score(authenticated, user, job, source):
    ApplicationPreferences.objects.create(owner=user, excluded_technologies=["Python"])
    complete(user, job, source, 99)
    assert recommended(authenticated, source).context["page"][0].recommendation_score is None


@pytest.mark.browser
@pytest.mark.django_db(transaction=True)
def test_feed_navigation_cv_filter_categories_and_mobile(
    page, live_server, client, user, source, settings
):
    from playwright.sync_api import expect

    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    high = Job.objects.create(
        canonical_url="https://example.com/feed-browser/high",
        title="Python Backend Strong",
        description="Python and Linux",
    )
    low = Job.objects.create(
        canonical_url="https://example.com/feed-browser/low",
        title="Python Backend Weak",
        description="Python",
    )
    closed = Job.objects.create(
        canonical_url="https://example.com/feed-browser/closed",
        title="Python Backend Closed",
        description="Python",
        active=False,
    )
    for job, score in ((high, 85), (low, 40), (closed, 99)):
        complete(user, job, source, score)
    initial = AgentExecution.objects.count()
    client.force_login(user)
    page.context.add_cookies(
        [
            {
                "name": settings.SESSION_COOKIE_NAME,
                "value": client.cookies[settings.SESSION_COOKIE_NAME].value,
                "url": live_server.url,
            }
        ]
    )
    page.goto(live_server.url + f"/?cv=profile:{source.pk}")
    tabs = page.get_by_role("navigation", name="Job views")
    expect(tabs.get_by_role("link", name="All jobs", exact=True)).to_have_attribute(
        "aria-current", "page"
    )
    expect(page.get_by_role("link", name=closed.title, exact=True)).to_be_visible()
    expect(page.get_by_label(f"Select {closed.title}", exact=True)).to_be_disabled()
    page.screenshot(path="/tmp/jobstream-all-feed.png", full_page=True)
    tabs.get_by_role("link", name="Recommended for you", exact=True).click()
    expect(page.locator('input[name="mode"]')).to_have_value("recommended")
    expect(page.locator('input[name="cv"][form="job-search"]')).to_have_value(
        f"profile:{source.pk}"
    )
    page.get_by_role("button", name="Update recommendations", exact=True).click()
    from jobs.models import RecommendationRun

    assert (
        RecommendationRun.objects.filter(owner=user, profile=source, status="queued").count() == 1
    )
    expect(page.get_by_text("Search queued", exact=False)).to_be_visible()
    expect(page.get_by_role("button", name="Update recommendations", exact=True)).to_be_disabled()
    from jobs.services.recommendation_runs import process

    AgentExecution.objects.filter(owner=user, job=low).update(status="running")
    process(RecommendationRun.objects.get(owner=user).pk)
    expect(page.get_by_text("Evaluation running", exact=True)).to_be_visible(timeout=10000)
    page.get_by_label(f"Select {low.title}", exact=True).check()
    AgentExecution.objects.filter(owner=user, job=low).update(status="completed")
    process(RecommendationRun.objects.get(owner=user).pk)
    expect(page.get_by_text("Search completed", exact=False)).to_be_visible(timeout=10000)
    expect(page.get_by_role("button", name="Update recommendations", exact=True)).to_be_enabled()
    expect(page.get_by_text("Good for you · 85% fit", exact=True)).to_be_visible()
    expect(
        page.locator(".job-list").get_by_role("link", name=low.title, exact=True)
    ).to_be_visible()
    expect(page.get_by_text("Bad for you · 40% fit", exact=True)).to_be_visible()
    expect(page.get_by_label(f"Select {low.title}", exact=True)).to_be_checked()
    expect(
        page.locator(".job-list article").first.get_by_role("link", name=low.title)
    ).to_be_visible()
    expect(page.get_by_role("link", name=closed.title, exact=True)).to_have_count(0)
    expect(page.locator('input[name="cv"][form="job-search"]')).to_have_value(
        f"profile:{source.pk}"
    )
    page.get_by_role("button", name="☰ Categories", exact=True).click()
    page.get_by_role("navigation", name="Job categories").get_by_role(
        "link", name="Backend", exact=True
    ).click()
    expect(page.locator('input[name="category"]')).to_have_value("backend")
    expect(page.get_by_text("Good for you · 85% fit", exact=True)).to_be_visible()
    page.get_by_role("button", name="Close category panel", exact=True).click()
    page.locator('input[name="q"]').fill("Missing keyword")
    page.locator("#job-search").get_by_role("button", name="Search", exact=True).click()
    expect(page.get_by_role("heading", name="No matching jobs yet", exact=True)).to_be_visible()
    page.get_by_role("link", name="Clear filters", exact=True).click()
    expect(page.get_by_text("Good for you · 85% fit", exact=True)).to_be_visible()
    expect(page.locator('input[name="mode"]')).to_have_value("recommended")
    page.screenshot(path="/tmp/jobstream-recommended-feed.png", full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.get_by_role("navigation", name="Job views").get_by_role(
        "link", name="All jobs", exact=True
    ).click()
    expect(page.locator('input[name="cv"][form="job-search"]')).to_have_value(
        f"profile:{source.pk}"
    )
    expect(page.get_by_role("link", name=closed.title, exact=True)).to_be_visible()
    assert AgentExecution.objects.count() == initial


def test_clear_all_filters_returns_to_root(authenticated, job):
    response = authenticated.get("/", {"q": "No matching phrase"})
    soup = BeautifulSoup(response.content, "html.parser")
    href = soup.find("a", string="Clear filters")["href"]
    cleared = authenticated.get(href if href.startswith("/") else "/" + href)
    assert cleared.context["q"] == ""
    assert [item.pk for item in cleared.context["page"]] == [job.pk]


@pytest.mark.parametrize("mode", ["all", "recommended"])
def test_evaluated_first_date_order_not_score(authenticated, user, source, job, mode):
    from datetime import timedelta

    newer = Job.objects.create(
        canonical_url="https://example.com/newer", title="Newer", description="Python"
    )
    pending = Job.objects.create(canonical_url="https://example.com/pending", title="Pending")
    complete(user, job, source, 100)
    complete(user, newer, source, 0)
    Job.objects.filter(pk=job.pk).update(discovered_at=timezone.now() - timedelta(days=1))
    response = authenticated.get("/", {"mode": mode, "cv": f"profile:{source.pk}"})
    assert [j.pk for j in response.context["page"]] == [newer.pk, job.pk, pending.pk]
    assert [j.recommendation_score for j in response.context["page"]] == [0, 100, None]


def test_polling_standalone_evaluation_refreshes_score(authenticated, user, source, job):
    execution = evaluations.start(user, job, source.pk)
    response = recommended(authenticated, source)
    assert response.context["recommendation_poll"]
    assert b"Evaluation queued" in response.content
    execution.status = "completed"
    execution.result = {"match_score": 15}
    execution.save()
    response = authenticated.get(
        "/", {"mode": "recommended", "cv": f"profile:{source.pk}"}, HTTP_HX_REQUEST="true"
    )
    assert not response.context["recommendation_poll"]
    assert b"Bad for you" in response.content
    assert b"15% fit" in response.content


def test_hard_blocked_zero_is_visible(authenticated, user, source, job):
    ApplicationPreferences.objects.create(owner=user, excluded_technologies=["Python"])
    complete(user, job, source, 0)
    response = recommended(authenticated, source)
    assert response.context["page"][0].recommendation_score == 0
    assert b"Bad for you" in response.content
