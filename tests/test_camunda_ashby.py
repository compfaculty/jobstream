import copy
import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock

import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.utils import timezone

from jobs.models import Application, Company, Document, Source
from jobs.services import camunda_ashby as camunda
from jobs.services import submissions
from jobs.services.secret_telaviv import PortalRateLimited, PortalRejected, PortalUnknown

IDENTITY = "5d366525-a519-4ca9-8c13-7e461a31a1d4"
PAYLOAD = json.loads((Path(__file__).parent / "fixtures/camunda_application.json").read_text())
SCHEMA = camunda.inspect_schema(PAYLOAD, IDENTITY)
PDF = b"%PDF-1.7\nSYNTHETIC TEST ONLY\n%%EOF"


def fixture_answers():
    answers = {"_schema": copy.deepcopy(SCHEMA.payload)}
    for entry in SCHEMA.entries:
        f = entry["field"]
        if f["type"] == "File":
            continue
        if f["type"] == "MultiValueSelect":
            value = []
        elif f["type"] == "Boolean":
            value = "no"
        elif f["type"] == "ValueSelect":
            value = f["selectableValues"][0]["value"] if entry["isRequired"] else ""
        elif f["type"] == "Location":
            value = "Tel Aviv, Israel"
        elif f["type"] == "Email":
            value = "synthetic@example.invalid"
        elif f["title"] == "LinkedIn Profile":
            value = "https://www.linkedin.com/in/synthetic-test-only"
        else:
            value = "SYNTHETIC TEST ONLY" if entry["isRequired"] else ""
        answers[f["path"]] = value
    answers["sms_consent"] = "no"
    return answers


@pytest.fixture
def camunda_job(job):
    company = Company.objects.create(
        name="Camunda",
        website="https://camunda.com/",
        website_verified=True,
        official_domain="camunda.com",
    )
    source = Source.objects.create(
        name="Camunda official",
        company=company,
        adapter="ashby",
        authoritative=True,
        url="https://api.ashbyhq.com/posting-api/job-board/camunda",
    )
    job.company_profile, job.employer_source = company, source
    job.provider_key = f"ashby:camunda:{IDENTITY}"
    job.apply_url = camunda.job_url(job)
    job.save()
    return job


@pytest.fixture
def camunda_application(user, camunda_job, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    document = Document(
        owner=user,
        name="Synthetic fixture CV",
        size=len(PDF),
        sha256=hashlib.sha256(PDF).hexdigest(),
    )
    document.file.save("synthetic.pdf", ContentFile(PDF), save=False)
    document.save()
    return Application.objects.create(
        owner=user,
        job=camunda_job,
        channel="portal",
        portal_url=camunda.job_url(camunda_job),
        portal_answers=fixture_answers(),
        portal_fingerprint=SCHEMA.fingerprint,
        document=document,
        subject="Application",
        body="Private note",
    )


def test_schema_and_consent_are_bound_without_inferred_answers():
    camunda.validate_answers(fixture_answers())
    changed = copy.deepcopy(PAYLOAD)
    changed["surveyForms"][0]["sections"][0]["fieldEntries"][0]["descriptionHtml"] += (
        " changed consent"
    )
    assert camunda.inspect_schema(changed, IDENTITY).fingerprint != SCHEMA.fingerprint
    with pytest.raises(ValidationError, match="changed"):
        camunda.validate_schema_answers(
            camunda.inspect_schema(changed, IDENTITY), fixture_answers()
        )
    for changes in (
        {"isListed": False},
        {"id": "other"},
        {"applicationLimitCalloutHtml": "No limits"},
    ):
        with pytest.raises(PortalRejected):
            camunda.inspect_schema({**PAYLOAD, **changes}, IDENTITY)


def test_no_missing_or_arbitrary_yes_answers():
    answers = fixture_answers()
    for entry in SCHEMA.entries:
        f = entry["field"]
        if f["type"] == "Boolean":
            broken = {**answers, f["path"]: "good"}
            with pytest.raises(ValidationError):
                camunda.validate_answers(broken)
            break
    with pytest.raises(ValidationError):
        camunda.validate_answers({"_schema": SCHEMA.payload})


@pytest.mark.django_db
def test_public_query_only_and_rate_limit(camunda_job, monkeypatch):
    request = Mock(
        return_value=(json.dumps({"data": {"jobPosting": PAYLOAD}}).encode(), camunda.API)
    )
    monkeypatch.setattr(camunda, "request_public_document", request)
    assert camunda.read_schema(camunda_job).fingerprint == SCHEMA.fingerprint
    assert request.call_args.kwargs["payload"]["operationName"] == "ApiJobPosting"
    assert "mutation" not in request.call_args.kwargs["payload"]["query"]
    request.side_effect = camunda.HTTPStatusError(429, "120")
    with pytest.raises(PortalRateLimited):
        camunda.read_schema(camunda_job)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "outcome,state",
    [
        (None, "confirmed"),
        (PortalUnknown("uncertain"), "unknown"),
        (PortalRateLimited("120"), "approved"),
        (PortalRejected("changed"), "failed"),
    ],
)
def test_approval_delivery_and_no_replay(camunda_application, monkeypatch, outcome, state):
    from jobs.services import ashby_browser

    monkeypatch.setattr(camunda, "read_schema", lambda job: SCHEMA)
    send = Mock(side_effect=outcome, return_value=f"camunda_ashby:{IDENTITY}")
    monkeypatch.setattr(ashby_browser, "deliver", send)
    submissions.approve(camunda_application.owner, camunda_application.pk)
    submissions.submit(camunda_application.pk)
    camunda_application.refresh_from_db()
    assert camunda_application.status == state
    submissions.submit(camunda_application.pk)
    submissions.reconcile(camunda_application.pk)
    assert send.call_count == 1


@pytest.mark.django_db
def test_approval_rejects_new_consent_schema(camunda_application, monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr(camunda, "read_schema", lambda job: replace(SCHEMA, fingerprint="changed"))
    with pytest.raises(ValidationError, match="changed"):
        submissions.approve(camunda_application.owner, camunda_application.pk)


@pytest.mark.django_db
@pytest.mark.parametrize("manual", [False, True])
def test_company_limit_counts_queued_and_unknown(camunda_application, monkeypatch, manual):
    from jobs.models import Job

    monkeypatch.setattr(camunda, "read_schema", lambda job: SCHEMA)
    for i, state in enumerate(("approved", "unknown")):
        job = Job.objects.create(
            title="Other role",
            canonical_url=f"https://example.test/{i}",
            provider_key=f"ashby:camunda:fixture-{i}",
        )
        Application.objects.create(
            owner=camunda_application.owner,
            job=job,
            status="draft" if manual else state,
            tracking_status="applied" if manual else "interested",
            subject="Test",
            body="Test",
            approved_at=None if manual else timezone.now(),
        )
    with pytest.raises(ValidationError, match="two applications"):
        submissions.approve(camunda_application.owner, camunda_application.pk)


@pytest.mark.django_db
def test_dynamic_draft_review_and_schema_drift(authenticated, camunda_application, monkeypatch):
    monkeypatch.setattr(camunda, "read_schema", lambda job: SCHEMA)
    url = f"/applications/{camunda_application.pk}/edit/"
    page = authenticated.get(url)
    assert b"What are your salary expectations?" in page.content
    assert b"good" not in page.content
    values = {
        f"camunda_{key}": value for key, value in fixture_answers().items() if key != "_schema"
    }
    values.update(
        subject="Application",
        document=camunda_application.document_id,
        camunda_schema=SCHEMA.fingerprint,
    )
    assert authenticated.post(url, {**values, "camunda_schema": "outdated"}).status_code == 200
    assert authenticated.post(url, values).status_code == 302
    detail = authenticated.get(f"/applications/{camunda_application.pk}/")
    assert b"Camunda application" in detail.content
    assert b"SYNTHETIC TEST ONLY" in detail.content


@pytest.mark.django_db(transaction=True)
def test_concurrent_approvals_cannot_overbook_company_limit(camunda_application, monkeypatch):
    from django.db import close_old_connections, connections

    from jobs.models import Job

    applications = [camunda_application]
    schemas = {camunda_application.job_id: SCHEMA}
    for _ in range(2):
        identity = str(uuid.uuid4())
        job = Job.objects.create(
            title="Synthetic role",
            canonical_url=f"https://jobs.ashbyhq.com/camunda/{identity}",
            provider_key=f"ashby:camunda:{identity}",
            company_profile=camunda_application.job.company_profile,
            employer_source=camunda_application.job.employer_source,
        )
        schema = camunda.inspect_schema({**PAYLOAD, "id": identity}, identity)
        schemas[job.pk] = schema
        answers = fixture_answers()
        answers["_schema"] = schema.payload
        applications.append(
            Application.objects.create(
                owner=camunda_application.owner,
                job=job,
                document=camunda_application.document,
                channel="portal",
                portal_answers=answers,
                portal_fingerprint=schema.fingerprint,
                subject="Synthetic application",
                body="Test only",
            )
        )
    monkeypatch.setattr(camunda, "read_schema", lambda job: schemas[job.pk])
    barrier = Barrier(3)
    owner = camunda_application.owner

    def approve(pk):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                submissions.approve(owner, pk)
                return "approved"
            except ValidationError:
                return "limited"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(approve, [a.pk for a in applications]))
    assert sorted(results) == ["approved", "approved", "limited"]


def test_transient_render_ids_do_not_change_approval():
    payload = copy.deepcopy(PAYLOAD)
    for form in [payload["applicationForm"], *payload["surveyForms"]]:
        form["id"] = "another-render"
        for section in form["sections"]:
            for entry in section["fieldEntries"]:
                entry["id"] = "another-render_" + entry["field"]["path"]
                entry["field"]["id"] = str(uuid.uuid4())
    assert camunda.inspect_schema(payload, IDENTITY).fingerprint == SCHEMA.fingerprint


@pytest.mark.django_db
def test_provider_help_text_is_escaped(authenticated, camunda_application, monkeypatch):
    payload = copy.deepcopy(PAYLOAD)
    payload["applicationForm"]["sections"][0]["fieldEntries"][0]["descriptionHtml"] = (
        "&lt;img src=x onerror=alert(1)&gt;"
    )
    schema = camunda.inspect_schema(payload, IDENTITY)
    monkeypatch.setattr(camunda, "read_schema", lambda job: schema)
    response = authenticated.get(f"/applications/{camunda_application.pk}/edit/")
    assert b"<img src=x" not in response.content
    assert b"&lt;img src=x" in response.content
    assert response.context["form"].initial["camunda_sms_consent"] == ""
    assert response.context["form"].initial["camunda__systemfield_data_consent_ack"] == []
