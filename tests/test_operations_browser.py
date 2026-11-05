"""Render synthetic operational data and verify refresh/pause behavior in Chromium."""

from urllib.parse import urlsplit

import pytest
from django.conf import settings as django_settings
from django.utils import timezone
from django_otp import DEVICE_ID_SESSION_KEY
from playwright.sync_api import expect

from jobs.models import OperationalSnapshot

pytestmark = [pytest.mark.browser, pytest.mark.django_db(transaction=True)]


def test_operations_render_and_refresh(page, live_server, client, user, settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    user.is_staff = True
    user.save()
    device = user.totpdevice_set.create(name="operations-browser", confirmed=True)
    client.force_login(user)
    session = client.session
    session[DEVICE_ID_SESSION_KEY] = device.persistent_id
    session.save()
    page.context.add_cookies(
        [
            {
                "name": django_settings.SESSION_COOKIE_NAME,
                "value": client.cookies[django_settings.SESSION_COOKIE_NAME].value,
                "domain": urlsplit(live_server.url).hostname,
                "path": "/",
            }
        ]
    )
    OperationalSnapshot.objects.create(
        key="infrastructure",
        payload={
            "database": "healthy",
            "redis": "healthy",
            "scheduler_published_at": timezone.now().timestamp(),
            "queues": [{"name": "ingestion", "depth": 3}],
            "workers": [{"name": "synthetic-worker", "queues": ["ingestion"]}],
            "active_tasks": [
                {
                    "worker": "synthetic-worker",
                    "task_id": "synthetic-task",
                    "task_name": "jobs.tasks.ingest_source",
                }
            ],
        },
    )
    requests = []
    errors = []
    page.on(
        "request",
        lambda request: requests.append(request.url) if request.is_navigation_request() else None,
    )
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.clock.install()
    page.goto(live_server.url + "/operations/")
    expect(page.get_by_role("heading", name="Operations", exact=True)).to_be_visible()
    expect(page.get_by_text("jobs.tasks.ingest_source", exact=False)).to_be_visible()
    page.screenshot(path="/tmp/jobstream-operations-desktop.png", full_page=True)
    initial = len(requests)
    with page.expect_navigation():
        page.clock.fast_forward(10001)
    assert len(requests) == initial + 1
    page.evaluate("Object.defineProperty(document, 'hidden', {value: true, configurable: true})")
    initial = len(requests)
    page.clock.fast_forward(20001)
    assert len(requests) == initial
    page.evaluate("Object.defineProperty(document, 'hidden', {value: false, configurable: true})")
    with page.expect_navigation():
        page.clock.fast_forward(10001)
    page.get_by_label("Window").select_option("1")
    initial = len(requests)
    page.clock.fast_forward(10001)
    assert len(requests) == initial
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert not errors


def test_job_evaluation_setup_is_visible_near_top(page, live_server, client, user, job, settings):
    settings.AGENT_BACKEND = "infinity"
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    job.description = "Long original job description.\n" * 300
    job.save()
    client.force_login(user)
    page.context.add_cookies(
        [
            {
                "name": django_settings.SESSION_COOKIE_NAME,
                "value": client.cookies[django_settings.SESSION_COOKIE_NAME].value,
                "domain": urlsplit(live_server.url).hostname,
                "path": "/",
            }
        ]
    )
    page.goto(live_server.url + f"/jobs/{job.pk}/")
    panel = page.locator("#vacancy-evaluation")
    expect(panel.get_by_role("heading", name="Evaluate vacancy")).to_be_in_viewport()
    expect(panel.get_by_role("button", name="Evaluate", exact=True)).to_be_disabled()
    expect(panel.get_by_role("link", name="Upload a CV")).to_be_visible()
    page.screenshot(path="/tmp/jobstream-evaluation-setup.png", full_page=False)


def test_evaluate_accepts_uploaded_pdf_without_profile(
    page, live_server, client, user, job, settings
):
    from jobs.models import AgentExecution, Document, ResumeProfile

    settings.AGENT_BACKEND = "infinity"
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    document = Document.objects.create(
        owner=user, name="Existing uploaded CV.pdf", file="unused.pdf", sha256="a" * 64, size=1
    )
    client.force_login(user)
    page.context.add_cookies(
        [
            {
                "name": django_settings.SESSION_COOKIE_NAME,
                "value": client.cookies[django_settings.SESSION_COOKIE_NAME].value,
                "domain": urlsplit(live_server.url).hostname,
                "path": "/",
            }
        ]
    )
    page.goto(live_server.url + f"/jobs/{job.pk}/")
    panel = page.locator("#vacancy-evaluation")
    expect(panel.get_by_role("button", name="Evaluate", exact=True)).to_be_enabled()
    expect(panel.get_by_label("CV to evaluate")).to_have_value(f"document:{document.pk}")
    assert not ResumeProfile.objects.exists()
    assert not AgentExecution.objects.exists()
    page.screenshot(path="/tmp/jobstream-direct-pdf-evaluation.png", full_page=False)


def test_zero_percent_preference_override_is_clear(page, live_server, client, user, job, settings):
    from jobs.models import AgentExecution

    settings.AGENT_BACKEND = "infinity"
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    AgentExecution.objects.create(
        owner=user,
        job=job,
        key="scored-browser-fixture",
        snapshot={},
        model="fake",
        status="completed",
        result={
            "match_score": 0,
            "cv_match_score": None,
            "assessment_skipped": True,
            "preference_blocks": ["This job mentions C++, which you excluded."],
            "summary": "Your saved preferences exclude this role.",
            "assessments": [],
        },
    )
    client.force_login(user)
    page.context.add_cookies(
        [
            {
                "name": django_settings.SESSION_COOKIE_NAME,
                "value": client.cookies[django_settings.SESSION_COOKIE_NAME].value,
                "domain": urlsplit(live_server.url).hostname,
                "path": "/",
            }
        ]
    )
    page.goto(live_server.url + f"/jobs/{job.pk}/")
    expect(page.get_by_text("0% fit for you", exact=True)).to_be_visible()
    expect(
        page.get_by_text("This job mentions C++, which you excluded.", exact=True)
    ).to_be_visible()
    page.get_by_text("How this score was calculated", exact=True).click()
    expect(page.get_by_text("No AI assessment was needed", exact=False)).to_be_visible()
    page.screenshot(path="/tmp/jobstream-preference-zero-score.png", full_page=False)
