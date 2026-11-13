import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.core.exceptions import ValidationError

from jobs.models import Application, Company, Document, Job, Source
from jobs.services import catalogue, comeet, ingestion, submissions
from jobs.services.ats import provider_identity
from jobs.services.discovery import ats_endpoint
from jobs.services.inventories import collect
from jobs.services.sources import normalize

FIXTURE = Path(__file__).parent / "fixtures" / "akeyless" / "positions.json"


def rows():
    return json.loads(FIXTURE.read_text())


def board_page(values):
    # Match the provider's JSON-in-JavaScript escaping, including script endings.
    value = json.dumps(values).replace("<", "\\u003c")
    return (
        f"<script>var COMPANY_POSITIONS_DATA; COMPANY_POSITIONS_DATA = {value};</script>".encode()
    )


def source_config(**overrides):
    return SimpleNamespace(
        **{
            "adapter": "akeyless",
            "url": comeet.BOARD,
            "max_items": 100000,
            "max_response_bytes": 2 * 1024 * 1024,
            **overrides,
        }
    )


def test_public_inventory_normalization():
    result, _ = collect(source_config(), {}, fetch=lambda url: board_page(rows()))
    assert len(result.postings) == 2
    assert result.scope == "comeet:27.006:"
    assert not result.complete and result.expected_total is None and not result.cursor
    posting = result.postings[0]
    assert posting.provider_key == "comeet:27.006:EC.A6C"
    assert provider_identity(posting.url) == posting.provider_key
    assert posting.apply_url == posting.url
    values = normalize(posting)
    normalized = values["normalized"]
    assert "At least 5 years' experience as an Automation Engineer." in normalized["requirements"]
    assert "Experience with performance and load testing." in normalized["preferred"]
    assert any("Playwright" in line for line in normalized["responsibilities"])
    assert "Docker" in posting.description and "Kubernetes" in posting.description
    assert "applynow.io" not in posting.description


def test_synthetic_alternatives_and_negation_preserve_evidence():
    value = rows()[:1]
    value[0]["custom_fields"]["details"] = [
        {"name": "Requirements", "value": "<p>3 years with Python, Node.js, or Go.</p>"},
        {"name": "Advantages", "value": "<p>Rust is not required.</p>"},
    ]
    result, _ = collect(source_config(), {}, fetch=lambda url: board_page(value))
    normalized = normalize(result.postings[0])["normalized"]
    assert normalized["requirements"] == ["3 years with Python, Node.js, or Go."]
    assert normalized["preferred"] == ["Rust is not required."]


@pytest.mark.parametrize(
    "change", ["duplicate", "foreign", "uid", "details", "location", "internal"]
)
def test_rejects_malformed_inventory(change):
    value = rows()
    if change == "duplicate":
        value.append(copy.deepcopy(value[0]))
    elif change == "foreign":
        value[0]["url_active_page"] = value[0]["url_active_page"].replace("akeyless", "other")
    elif change == "uid":
        value[0]["uid"] = "ZZ.000"
    elif change == "details":
        value[0]["custom_fields"]["details"] = [None]
    elif change == "location":
        value[0]["location"] = []
    else:
        value[0]["is_internal"] = True
    with pytest.raises(ValueError):
        collect(source_config(), {}, fetch=lambda url: board_page(value))


@pytest.mark.parametrize(
    "data",
    [
        b"<html>challenge</html>",
        b"<script>COMPANY_POSITIONS_DATA=[",
        board_page({}),
        board_page([]) + board_page([]),
        b"<script>COMPANY_POSITIONS_DATA=[] + hostile();</script>",
    ],
)
def test_changed_truncated_or_ambiguous_markup(data):
    with pytest.raises(ValueError):
        collect(source_config(), {}, fetch=lambda url: data)


def test_empty_and_over_budget_are_never_complete():
    result, _ = collect(source_config(), {}, fetch=lambda url: board_page([]))
    assert not result.complete and not result.postings
    with pytest.raises(ValueError, match="over-budget"):
        collect(source_config(max_items=1), {}, fetch=lambda url: board_page(rows()))


def test_snapshot_resumption_and_changed_inventory():
    values = []
    for index in range(251):
        row = copy.deepcopy(rows()[0])
        row["uid"] = f"EC.{index:X}"
        row["url_active_page"] = comeet.BOARD + "/engineer/" + row["uid"]
        values.append(row)
    data = board_page(values)
    first, _ = collect(source_config(), {}, fetch=lambda url: data)
    assert len(first.postings) == 250 and first.cursor and not first.complete
    second, _ = collect(
        source_config(), first.cursor, fetch=lambda url: data + b"<script>volatile = 123;</script>"
    )
    assert len(second.postings) == 1 and not second.cursor and not second.complete
    with pytest.raises(ValueError, match="changed"):
        collect(source_config(), first.cursor, fetch=lambda url: board_page(values[:-1]))
    with pytest.raises(ValueError, match="offset"):
        collect(source_config(), {"offset": True}, fetch=lambda url: data)


@pytest.mark.parametrize(
    "url",
    [
        comeet.BOARD + "?x=1",
        comeet.BOARD.replace("www.comeet.com", "evil.example"),
        "http://www.comeet.com/jobs/akeyless/27.006",
        comeet.BOARD + "/role/EC.A6C?token=x",
    ],
)
def test_untrusted_urls(url):
    with pytest.raises(ValueError):
        comeet.identity(url)


@pytest.fixture
def company(db):
    return Company.objects.create(
        name="Akeyless",
        website="https://www.akeyless.io/careers/",
        website_verified=True,
        official_domain="akeyless.io",
    )


def activate_candidate(company, monkeypatch):
    monkeypatch.setattr(
        catalogue,
        "fetch_public_document",
        lambda url: (
            f'<a href="{rows()[0]["url_active_page"]}">Automation Engineer</a>'.encode(),
            url,
        ),
    )
    [source] = catalogue.discover_company(company)
    assert source.adapter == "akeyless" and source.url == comeet.BOARD
    assert not source.enabled and not source.authoritative and source.discovery_pending
    # Fixture-only operator review; never change the real enabled-source cohort.
    source.enabled = source.authoritative = True
    source.discovery_pending = False
    source.employer_name = "Akeyless"
    source.save()
    return source


def test_pipeline_manual_handoff(authenticated, user, other, company, monkeypatch, settings):
    settings.JOB_NORMALIZATION_BATCH_ENABLED = True
    source = activate_candidate(company, monkeypatch)
    monkeypatch.setattr(ingestion, "fetch_public", lambda *a, **kw: board_page(rows()))
    ingestion.ingest(source.pk)
    job = Job.objects.get(provider_key="comeet:27.006:EC.A6C")
    assert job.employer_source_id == source.pk and job.company_profile_id == company.pk
    assert job.normalized["requirements"]
    run = source.ingestionrun_set.latest("pk")
    assert not run.complete and run.imported == 2 and not run.rejected
    assert comeet.manual_url(job) == rows()[0]["url_active_page"]
    document = Document.objects.create(
        owner=user, name="Synthetic CV", file="synthetic.pdf", size=0
    )
    values = {"subject": "Application", "body": "Private note", "document": document.pk}
    without_cv = authenticated.post(
        f"/jobs/{job.pk}/apply/", {"subject": "Application", "body": "Private note"}
    )
    assert without_cv.status_code == 200 and not Application.objects.filter(job=job).exists()
    response = authenticated.post(f"/jobs/{job.pk}/apply/", values)
    assert response.status_code == 302
    application = Application.objects.get(job=job, owner=user)
    assert application.channel == "portal" and application.portal_url == comeet.manual_url(job)
    authenticated.force_login(other)
    assert authenticated.get(f"/applications/{application.pk}/").status_code == 404
    authenticated.force_login(user)
    detail = authenticated.get(f"/applications/{application.pk}/")
    assert detail.status_code == 200
    assert b"Akeyless / Comeet requires manual application" in detail.content
    assert b"Approve and send" not in detail.content
    with pytest.raises(Application.DoesNotExist):
        submissions.approve(other, application.pk)
    with pytest.raises(ValidationError, match="employer portal"):
        submissions.approve(user, application.pk)
    send = Mock()
    monkeypatch.setattr("jobs.services.gmail.send", send)
    submissions.submit(application.pk)
    submissions.submit(application.pk)
    send.assert_not_called()
    application.refresh_from_db()
    assert application.status == "draft" and not application.provider_id
    foreign = Document.objects.create(owner=other, name="Other CV", file="other.pdf", size=0)
    application.delete()
    response = authenticated.post(f"/jobs/{job.pk}/apply/", {**values, "document": foreign.pk})
    assert response.status_code == 200 and not Application.objects.filter(job=job).exists()


@pytest.mark.parametrize(
    "change", ["unverified", "foreign_source", "board", "key", "destination", "pending"]
)
def test_manual_route_requires_attested_identity(company, job, monkeypatch, change):
    source = activate_candidate(company, monkeypatch)
    job.company_profile = company
    job.employer_source = source
    job.apply_url = rows()[0]["url_active_page"]
    job.provider_key = "comeet:27.006:EC.A6C"
    assert comeet.manual_url(job)
    if change == "unverified":
        company.website_verified = False
    elif change == "foreign_source":
        source.company_id = None
    elif change == "board":
        source.url = "https://www.comeet.com/jobs/other/00.111"
    elif change == "key":
        job.provider_key = "comeet:27.006:ZZ.123"
    elif change == "destination":
        job.apply_url = job.apply_url.replace("www.comeet.com", "evil.example")
    else:
        source.discovery_pending = True
    assert not comeet.manual_url(job)


def test_discovery_is_narrow_and_preserves_disabled(company, monkeypatch):
    source = activate_candidate(company, monkeypatch)
    source.enabled = False
    source.review_reason = "Operator disabled automatic activation"
    source.save()
    catalogue.candidate(company, comeet.BOARD, evidence=company.website, trusted=True)
    source.refresh_from_db()
    assert not source.enabled and not source.closure_enabled
    assert ats_endpoint(comeet.BOARD.replace("akeyless", "other")) is None


def test_rate_limit_keeps_checkpoint_and_changed_inventory_discards_it(company, monkeypatch):
    from django.utils import timezone

    from jobs.services.network import HTTPStatusError

    source = activate_candidate(company, monkeypatch)
    values = []
    prototype = rows()[0]
    for index in range(251):
        value = copy.deepcopy(prototype)
        value["uid"] = f"EC.{index:X}"
        value["url_active_page"] = comeet.BOARD + "/engineer/" + value["uid"]
        values.append(value)
    data = board_page(values)
    fetch = Mock(return_value=data)
    monkeypatch.setattr(ingestion, "fetch_public", fetch)
    ingestion.ingest(source.pk)
    source.refresh_from_db()
    checkpoint = source.inventory_cursor.copy()
    assert checkpoint["offset"] == 250
    first = Job.objects.get(provider_key="comeet:27.006:EC.0")
    # Repeated task before its next due time is a no-op.
    fetch.reset_mock()
    ingestion.ingest(source.pk)
    fetch.assert_not_called()
    Source.objects.filter(pk=source.pk).update(next_run_at=timezone.now())
    fetch.side_effect = HTTPStatusError(429, "60")
    ingestion.ingest(source.pk)
    source.refresh_from_db()
    assert source.inventory_cursor == checkpoint
    assert not source.ingestionrun_set.latest("pk").complete
    # A restarted worker resumes the durable snapshot cursor.
    fetch.side_effect = None
    Source.objects.filter(pk=source.pk).update(next_run_at=timezone.now())
    ingestion.ingest(source.pk)
    source.refresh_from_db()
    assert not source.inventory_cursor
    assert Job.objects.filter(employer_source=source).count() == 251
    assert not source.last_complete_at and not source.closure_enabled
    assert Job.objects.get(pk=first.pk).active
    # A later cycle interrupted at the same checkpoint cannot resume a changed payload.
    Source.objects.filter(pk=source.pk).update(next_run_at=timezone.now())
    ingestion.ingest(source.pk)
    source.refresh_from_db()
    assert source.inventory_cursor
    fetch.return_value = board_page(values[:-1])
    Source.objects.filter(pk=source.pk).update(next_run_at=timezone.now())
    ingestion.ingest(source.pk)
    source.refresh_from_db()
    assert not source.inventory_cursor and source.last_error == "ValueError"
    assert Job.objects.get(pk=first.pk).active


def test_preparation_batch_manual_only_and_cancellation(
    company, user, other, monkeypatch, settings, tmp_path
):
    import hashlib
    import uuid

    from django.core.files.base import ContentFile

    from jobs.services import batches

    settings.MEDIA_ROOT = tmp_path
    source = activate_candidate(company, monkeypatch)
    monkeypatch.setattr(ingestion, "fetch_public", lambda *a, **kw: board_page(rows()))
    ingestion.ingest(source.pk)
    job = Job.objects.get(provider_key="comeet:27.006:EC.A6C")
    data = b"%PDF-1.7\nSynthetic fixture only\n%%EOF"
    document = Document(
        owner=user, name="Synthetic CV", size=len(data), sha256=hashlib.sha256(data).hexdigest()
    )
    document.file.save("cv.pdf", ContentFile(data))
    batch = batches.create(user, document.pk, [job.pk], uuid.uuid4())
    item = batch.items.get()
    batches.prepare(item.pk)
    item.refresh_from_db()
    assert item.state == "needs_action" and item.error_code == "manual_portal_required"
    application = item.application
    assert application.channel == "portal" and application.portal_url == comeet.manual_url(job)
    batches.prepare(item.pk)
    assert Application.objects.filter(job=job, owner=user).count() == 1
    with pytest.raises(ValidationError):
        batches.approve_items(user, batch.pk, [item.pk])
    with pytest.raises(ValidationError):
        batches.create(other, document.pk, [job.pk], uuid.uuid4())
    batches.cancel(user, batch.pk)
    with pytest.raises(ValidationError):
        batches.approve_items(user, batch.pk, [item.pk])
    batches.prepare(item.pk)
    application.refresh_from_db()
    assert application.status == "draft" and not application.approval_digest


@pytest.mark.browser
@pytest.mark.django_db(transaction=True)
def test_chromium_manual_handoff(
    page, live_server, authenticated, user, company, monkeypatch, settings
):
    from playwright.sync_api import expect

    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    source = activate_candidate(company, monkeypatch)
    monkeypatch.setattr(ingestion, "fetch_public", lambda *a, **kw: page_bytes)
    page_bytes = board_page(rows())
    ingestion.ingest(source.pk)
    job = Job.objects.get(provider_key="comeet:27.006:EC.A6C")
    document = Document.objects.create(
        owner=user, name="Synthetic CV", file="synthetic.pdf", size=0
    )
    page.context.add_cookies(
        [
            {
                "name": settings.SESSION_COOKIE_NAME,
                "value": authenticated.cookies[settings.SESSION_COOKIE_NAME].value,
                "url": live_server.url,
            }
        ]
    )
    external_requests = []

    def intercept(route):
        if route.request.url.startswith(live_server.url + "/"):
            route.continue_()
        else:
            external_requests.append(route.request.url)
            route.abort()

    page.context.route("**/*", intercept)
    page.goto(f"{live_server.url}/jobs/{job.pk}/apply/")
    expect(page.locator('input[name="recipient"]')).to_have_attribute("type", "hidden")
    page.get_by_label("Subject").fill("Application")
    page.get_by_label("Private note (not sent to Akeyless)").fill("Synthetic private note")
    expect(page.locator('input[type="hidden"][name="document"]')).to_have_value(str(document.pk))
    page.get_by_role("button", name="Prepare CV and application").click()
    expect(
        page.get_by_text("Akeyless / Comeet requires manual application", exact=False)
    ).to_be_visible()
    expect(page.get_by_role("button", name="Approve and send this application")).to_have_count(0)
    expect(page.get_by_role("link", name="Open employer application")).to_have_attribute(
        "href", comeet.manual_url(job)
    )
    assert not external_requests


def test_public_shortlink_and_empty_optional_section():
    value = json.loads((FIXTURE.parent / "general-position.json").read_text())
    assert value["url_active_page"].startswith("https://bit.ly/")
    result, _ = collect(source_config(), {}, fetch=lambda url: board_page([value]))
    posting = result.postings[0]
    assert posting.url == value["url_comeet_hosted_page"]
    assert posting.provider_key == "comeet:27.006:D7.A26"
    assert "Submit your resume" in posting.description
    assert normalize(posting)["normalized"]["requirements"] == []
    del value["url_comeet_hosted_page"]
    with pytest.raises(ValueError):
        collect(source_config(), {}, fetch=lambda url: board_page([value]))
