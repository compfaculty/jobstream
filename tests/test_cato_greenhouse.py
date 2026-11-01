import copy
import hashlib
import json
import uuid
from unittest.mock import Mock

import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile

from jobs.models import Application, Company, Document, Source
from jobs.services import cato_greenhouse as cato
from jobs.services import submissions
from jobs.services.secret_telaviv import PortalRateLimited, PortalRejected, PortalUnknown

IDENTITY = "4937879101"
ANSWERS = {
    "first_name": "Synthetic",
    "last_name": "Applicant",
    "email": "test@example.invalid",
    "phone": "+972502345678",
    "phone_country": "IL",
    "city": "Tel Aviv",
    "country": "Israel",
    "linkedin": "",
}
PDF = b"%PDF-1.7\nfixture only\n%%EOF"
PAYLOAD = {
    "id": int(IDENTITY),
    "questions": [
        {"label": name, "required": True, "fields": [{"name": name, "type": "input_text"}]}
        for name in ("first_name", "last_name", "email", "phone")
    ]
    + [
        {
            "label": "Resume",
            "required": True,
            "fields": [
                {"name": "resume", "type": "input_file"},
                {"name": "resume_text", "type": "textarea"},
            ],
        },
        {
            "label": "LinkedIn Profile",
            "required": False,
            "fields": [
                {"name": "question_123", "type": "input_text"},
            ],
        },
    ],
    "location_questions": [
        {
            "label": "Location",
            "required": True,
            "fields": [
                {"name": "location", "type": "input_text"},
            ],
        }
    ],
    "data_compliance": [{"type": "gdpr", "requires_consent": False}],
}


@pytest.fixture
def cato_job(job):
    company = Company.objects.create(
        name="Cato Networks",
        website="https://www.catonetworks.com/",
        website_verified=True,
        official_domain="catonetworks.com",
    )
    source = Source.objects.create(
        name="Cato official",
        company=company,
        adapter="greenhouse",
        authoritative=True,
        url="https://boards-api.greenhouse.io/v1/boards/catonetworks/jobs?content=true",
    )
    job.provider_key = f"greenhouse:catonetworks:{IDENTITY}"
    job.company_profile = company
    job.employer_source = source
    job.apply_url = f"https://www.catonetworks.com/careers/careers-post/{IDENTITY}/"
    job.save()
    return job


@pytest.fixture
def cato_application(user, cato_job, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    document = Document(
        owner=user, name="Fixture CV", size=len(PDF), sha256=hashlib.sha256(PDF).hexdigest()
    )
    document.file.save("fixture.pdf", ContentFile(PDF), save=False)
    document.save()
    return Application.objects.create(
        owner=user,
        job=cato_job,
        document=document,
        channel="portal",
        portal_url=cato.job_url(cato_job),
        portal_answers=ANSWERS,
        subject="Application",
        body="Private note",
    )


def test_schema_binding_and_optional_linkedin():
    schema = cato.inspect_schema(PAYLOAD, IDENTITY)
    assert schema.linkedin_field == "question_123"
    changed = copy.deepcopy(PAYLOAD)
    changed["questions"][-1]["label"] = "Portfolio URL"
    assert cato.inspect_schema(changed, IDENTITY).fingerprint != schema.fingerprint
    for payload in ({}, [], {**PAYLOAD, "id": 999}):
        with pytest.raises(PortalRejected, match="schema_changed"):
            cato.inspect_schema(payload, IDENTITY)


@pytest.mark.parametrize("change", ["question", "consent", "demographic"])
def test_unhandled_questions_pause(change):
    payload = copy.deepcopy(PAYLOAD)
    if change == "question":
        payload["questions"].append(
            {
                "label": "Legal sex",
                "required": True,
                "fields": [
                    {"name": "question_456", "type": "input_text"},
                ],
            }
        )
    elif change == "consent":
        payload["data_compliance"][0]["requires_processing_consent"] = True
    else:
        payload["demographic_questions"] = {"questions": ["gender"]}
    with pytest.raises(PortalRejected):
        cato.inspect_schema(payload, IDENTITY)


def test_answers_are_explicit_and_phone_country_consistent():
    cato.validate_answers(ANSWERS)
    for changes in (
        {"city": ""},
        {"phone_country": "US"},
        {"phone": "123"},
        {"gender_identity": "male"},
    ):
        with pytest.raises(ValidationError):
            cato.validate_answers({**ANSWERS, **changes})


def test_form_get_429_and_malformed(cato_job, monkeypatch):
    fetch = Mock(side_effect=cato.HTTPStatusError(429, "120"))
    monkeypatch.setattr(cato, "fetch_public", fetch)
    with pytest.raises(PortalRateLimited) as error:
        cato.read_schema(cato_job)
    assert error.value.retry_after == "120"
    fetch.side_effect = None
    fetch.return_value = b"not json"
    with pytest.raises(PortalRejected):
        cato.read_schema(cato_job)


@pytest.mark.django_db
def test_trust_requires_verified_company_and_source(cato_job):
    assert IDENTITY in cato.job_url(cato_job)
    cato_job.company_profile.website_verified = False
    with pytest.raises(ValueError):
        cato.job_url(cato_job)


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
def test_approval_dispatch_outcomes(cato_application, monkeypatch, outcome, state):
    from jobs.services import cato_browser

    monkeypatch.setattr(cato, "fetch_public", lambda url: json.dumps(PAYLOAD).encode())
    delivery = Mock(return_value=f"cato_greenhouse:{IDENTITY}", side_effect=outcome)
    monkeypatch.setattr(cato_browser, "deliver", delivery)
    submissions.approve(cato_application.owner, cato_application.pk)
    submissions.submit(cato_application.pk)
    cato_application.refresh_from_db()
    assert cato_application.status == state
    assert cato_application.tracking_status == ("applied" if state == "confirmed" else "interested")
    submissions.submit(cato_application.pk)
    submissions.reconcile(cato_application.pk)
    assert delivery.call_count == 1


@pytest.mark.django_db
def test_changed_answers_and_schema_do_not_send(cato_application, monkeypatch):
    from jobs.services import cato_browser

    monkeypatch.setattr(cato, "fetch_public", lambda url: json.dumps(PAYLOAD).encode())
    delivery = Mock()
    monkeypatch.setattr(cato_browser, "deliver", delivery)
    submissions.approve(cato_application.owner, cato_application.pk)
    cato_application.refresh_from_db()
    cato_application.portal_answers = {**ANSWERS, "city": "Haifa"}
    cato_application.save()
    submissions.submit(cato_application.pk)
    cato_application.refresh_from_db()
    assert cato_application.error_code == "preflight_failed"
    delivery.assert_not_called()
    cato_application.portal_fingerprint = "outdated"
    with pytest.raises(PortalRejected, match="form_changed"):
        cato.send(cato_application, PDF)


@pytest.mark.django_db
def test_create_form_and_owner_bound_cv(authenticated, cato_job, cato_application, other):
    cato_application.delete()
    url = f"/jobs/{cato_job.pk}/apply/"
    response = authenticated.get(url)
    assert b"cato_first_name" in response.content
    values = {f"cato_{key}": value for key, value in ANSWERS.items()}
    values.update(
        subject="Application",
        document=Document.objects.get(owner=authenticated.session["_auth_user_id"]).pk,
    )
    assert authenticated.post(url, values).status_code == 302
    app = Application.objects.get(job=cato_job)
    assert app.channel == "portal" and app.portal_answers == ANSWERS
    detail = authenticated.get(f"/applications/{app.pk}/")
    assert b"Cato Networks application" in detail.content
    assert b"Approve and send" in detail.content


@pytest.mark.django_db
def test_batch_prefills_only_opted_in_equivalent_answers(cato_application, settings, monkeypatch):
    from cryptography.fernet import Fernet

    from jobs.models import BatchItem
    from jobs.services import batches, saved_answers

    settings.TOKEN_ENCRYPTION_KEY = Fernet.generate_key().decode()
    owner, document, job = cato_application.owner, cato_application.document, cato_application.job
    cato_application.delete()
    saved_answers.save_answers(
        owner.pk, {**ANSWERS, "gender_identity": "prefer_not_to_answer"}, reuse_enabled=True
    )
    monkeypatch.setattr(cato, "fetch_public", lambda url: json.dumps(PAYLOAD).encode())
    batch = batches.create(owner, document.pk, [job.pk], uuid.uuid4())
    item = BatchItem.objects.get(batch=batch)
    batches.prepare(item.pk)
    item.refresh_from_db()
    assert item.state == "ready"
    assert item.application.portal_answers == {
        key: value for key, value in ANSWERS.items() if value
    }


@pytest.mark.django_db
def test_browser_queue_and_independent_provider_cooldown(cato_application, monkeypatch):
    from datetime import timedelta

    from django.utils import timezone

    from jobs import tasks
    from jobs.services import throttles

    cato_application.status = "approved"
    cato_application.save()
    browser_task, ordinary = Mock(), Mock()
    monkeypatch.setattr(tasks.send_browser_application, "delay", browser_task)
    monkeypatch.setattr(tasks.send_application, "delay", ordinary)
    tasks.dispatch_submissions()
    browser_task.assert_called_once_with(cato_application.pk)
    ordinary.assert_not_called()
    throttles.defer_delivery(cato_application, timezone.now() + timedelta(seconds=120))
    assert throttles.portal_pause_seconds("cato_greenhouse") > 100
    assert throttles.portal_pause_seconds() == 0


@pytest.mark.django_db
def test_preflight_requires_supported_portal_cv_and_current_preferences(cato_application):
    from jobs.models import ApplicationPreferences

    ApplicationPreferences.objects.create(
        owner=cato_application.owner, excluded_technologies=["Java"]
    )
    # Python vacancy satisfies this exclusion policy; the eligible branch stays usable.
    submissions.validate_content(cato_application)
    cato_application.document = None
    with pytest.raises(ValidationError, match="Choose a CV"):
        submissions.validate_content(cato_application)
    cato_application.job.provider_key = "greenhouse:unverified:4937879101"
    with pytest.raises(ValidationError, match="employer portal"):
        submissions.validate_content(cato_application)
