"""Public-schema fixtures and synthetic applicant answers only."""

import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile

from jobs.models import Application, Company, Document, Source
from jobs.services import gitlab_greenhouse as gitlab
from jobs.services import submissions
from jobs.services.secret_telaviv import PortalRateLimited, PortalRejected, PortalUnknown

IDENTITY = "8704363002"
PAYLOAD = json.loads((Path(__file__).parent / "fixtures/gitlab_application.json").read_text())
SCHEMA = gitlab.inspect_schema(PAYLOAD, IDENTITY)
PDF = b"%PDF-1.7\nSYNTHETIC TEST ONLY\n%%EOF"


def fixture_answers():
    answers = {"_schema": copy.deepcopy(SCHEMA.payload), "phone_country": "IL"}
    for q in SCHEMA.entries:
        f = q["fields"][0]
        if f["name"] == "email":
            value = "synthetic@example.invalid"
        elif f["name"] == "phone":
            value = "+972502345678"
        elif f["type"] == "multi_value_single_select":
            value = f["values"][0]["value"] if q["required"] else ""
        else:
            value = "Synthetic" if q["required"] else ""
        answers[f["name"]] = value
    return answers


@pytest.fixture
def gitlab_job(job):
    company = Company.objects.create(
        name="GitLab",
        website="https://about.gitlab.com/",
        website_verified=True,
        official_domain="about.gitlab.com",
    )
    source = Source.objects.create(
        kind=Source.Kind.COMPANY,
        name="GitLab official",
        company=company,
        adapter="greenhouse",
        authoritative=True,
        url="https://boards-api.greenhouse.io/v1/boards/gitlab/jobs?content=true",
    )
    job.company_profile, job.employer_source = company, source
    job.provider_key = f"greenhouse:gitlab:{IDENTITY}"
    job.apply_url = gitlab.job_url(job)
    job.save()
    return job


@pytest.fixture
def gitlab_application(user, gitlab_job, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    doc = Document(
        owner=user, name="Synthetic CV", size=len(PDF), sha256=hashlib.sha256(PDF).hexdigest()
    )
    doc.file.save("synthetic.pdf", ContentFile(PDF), save=False)
    doc.save()
    return Application.objects.create(
        owner=user,
        job=gitlab_job,
        channel="portal",
        portal_url=gitlab.job_url(gitlab_job),
        portal_answers=fixture_answers(),
        portal_fingerprint=SCHEMA.fingerprint,
        document=doc,
        subject="Application",
        body="Private note",
    )


def test_public_schema_and_truthful_required_answers():
    answers = fixture_answers()
    gitlab.validate_answers(answers)
    assert len(SCHEMA.entries) == 13
    assert "gender" not in answers
    required_choice = next(
        q["fields"][0]["name"]
        for q in SCHEMA.entries
        if q["required"] and q["fields"][0]["type"] == "multi_value_single_select"
    )
    for value in ("", "invented"):
        answers[required_choice] = value
        with pytest.raises(ValidationError):
            gitlab.validate_answers(answers)
    answers = fixture_answers()
    answers["gender"] = "1"
    with pytest.raises(ValidationError):
        gitlab.validate_answers(answers)
    answers = fixture_answers()
    answers["phone_country"] = "US"
    with pytest.raises(ValidationError):
        gitlab.validate_answers(answers)


@pytest.mark.parametrize(
    "change",
    [
        "id",
        "required_file",
        "duplicate",
        "type",
        "consent",
        "demographic",
        "required_eeoc",
        "empty",
        "location",
    ],
)
def test_schema_fails_closed(change):
    p = copy.deepcopy(PAYLOAD)
    if change == "id":
        p["id"] = 1
    elif change == "required_file":
        p["questions"][5]["required"] = True
    elif change == "duplicate":
        p["questions"].append(p["questions"][0])
    elif change == "type":
        p["questions"][6]["fields"][0]["type"] = "unknown"
    elif change == "consent":
        p["data_compliance"][0]["requires_processing_consent"] = True
    elif change == "demographic":
        p["demographic_questions"] = {"id": 1}
    elif change == "required_eeoc":
        p["compliance"][0]["questions"][0]["required"] = True
    elif change == "empty":
        p["questions"] = []
    elif change == "location":
        p["location_questions"] = [{"required": True}]
    with pytest.raises(PortalRejected):
        gitlab.inspect_schema(p, IDENTITY)


def test_identity_requires_authoritative_verified_relationship(gitlab_job):
    assert gitlab.job_url(gitlab_job).endswith(IDENTITY)
    gitlab_job.company_profile.website_verified = False
    with pytest.raises(ValueError):
        gitlab.posting_id(gitlab_job)
    gitlab_job.company_profile.website_verified = True
    gitlab_job.employer_source.authoritative = False
    with pytest.raises(ValueError):
        gitlab.posting_id(gitlab_job)


def test_schema_drift_owner_approval_and_delivery(gitlab_application, other, monkeypatch):
    a = gitlab_application
    monkeypatch.setattr(gitlab, "read_schema", lambda job: SCHEMA)
    with pytest.raises(Application.DoesNotExist):
        gitlab.approve_portal(other, a.pk)
    submissions.approve(a.owner, a.pk)
    a.refresh_from_db()
    assert a.status == "approved" and a.approval_digest == submissions.digest(a, "")
    from jobs.services import gitlab_browser

    deliver = Mock(return_value=f"gitlab_greenhouse:{IDENTITY}")
    monkeypatch.setattr(gitlab_browser, "deliver", deliver)
    assert gitlab.send(a, PDF) == f"gitlab_greenhouse:{IDENTITY}"
    assert deliver.call_args.args[1] == a.portal_answers
    a.portal_fingerprint = "changed"
    with pytest.raises(PortalRejected, match="gitlab_form_changed"):
        gitlab.send(a, PDF)
    assert deliver.call_count == 1


def test_approval_requires_review_of_current_schema(gitlab_application, monkeypatch):
    p = copy.deepcopy(PAYLOAD)
    p["questions"][0]["label"] = "Legal first name"
    monkeypatch.setattr(gitlab, "read_schema", lambda job: gitlab.inspect_schema(p, IDENTITY))
    with pytest.raises(ValidationError, match="changed"):
        gitlab.approve_portal(gitlab_application.owner, gitlab_application.pk)


def test_owner_form_review_and_cross_provider_throttle(
    gitlab_application, other, monkeypatch, client
):
    from jobs.forms import ApplicationForm
    from jobs.services import throttles

    a = gitlab_application
    monkeypatch.setattr(gitlab, "read_schema", lambda job: SCHEMA)
    form = ApplicationForm(owner=a.owner, instance=a)
    data = {
        "subject": a.subject,
        "body": "Private",
        "document": str(a.document_id),
        "gitlab_schema": SCHEMA.fingerprint,
        **{"gitlab_" + k: v for k, v in fixture_answers().items() if k != "_schema"},
    }
    bound = ApplicationForm(data, owner=a.owner, instance=a)
    assert bound.is_valid(), bound.errors
    assert "gitlab_question_37761134002" in form.fields
    assert not ApplicationForm(data, owner=other, instance=a).is_valid()
    client.force_login(a.owner)
    response = client.get(f"/applications/{a.pk}/")
    assert response.status_code == 200
    assert b"GitLab application" in response.content
    assert b"Submit application" not in response.content
    assert (
        throttles.scopes(None, a)[0][0] == hashlib.sha256(b"provider:gitlab_greenhouse").hexdigest()
    )
    from datetime import timedelta

    from django.utils import timezone

    throttles.defer_delivery(a, timezone.now() + timedelta(seconds=30))
    assert throttles.portal_pause_seconds("gitlab_greenhouse") > 0
    assert throttles.portal_pause_seconds("cato_greenhouse") == 0


@pytest.mark.parametrize(
    "error,status",
    [
        (PortalRejected("gitlab_human_verification_required"), "failed"),
        (PortalUnknown("gitlab_submission_outcome_unknown"), "unknown"),
        (PortalRateLimited("60"), "approved"),
    ],
)
def test_submission_states_and_browser_dispatch(gitlab_application, monkeypatch, error, status):
    a = gitlab_application
    monkeypatch.setattr(gitlab, "read_schema", lambda job: SCHEMA)
    submissions.approve(a.owner, a.pk)
    monkeypatch.setattr(gitlab, "send", Mock(side_effect=error))
    from jobs.services.portal_delivery import browser_supported

    assert browser_supported(a)
    submissions.submit(a.pk)
    a.refresh_from_db()
    assert a.status == status
    if status == "unknown":
        gitlab.send.reset_mock()
        submissions.reconcile(a.pk)
        assert not gitlab.send.called


def test_snapshot_cannot_remove_a_required_question():
    answers = fixture_answers()
    answers["_schema"]["questions"][-1]["required"] = False
    answers[answers["_schema"]["questions"][-1]["fields"][0]["name"]] = ""
    with pytest.raises(ValidationError, match="changed"):
        gitlab.validate_schema_answers(SCHEMA, answers)


def test_batch_pauses_for_role_specific_answers_and_uses_browser_queue(
    gitlab_application, monkeypatch
):
    import uuid

    from jobs import tasks
    from jobs.models import BatchItem
    from jobs.services import batches

    a = gitlab_application
    owner, document, job = a.owner, a.document, a.job
    a.delete()
    monkeypatch.setattr(gitlab, "read_schema", lambda job: SCHEMA)
    batch = batches.create(owner, document.pk, [job.pk], uuid.uuid4())
    item = BatchItem.objects.get(batch=batch)
    batches.prepare(item.pk)
    item.refresh_from_db()
    assert item.state == "needs_action"
    assert item.error_code == "gitlab_details_required"
    assert item.application.portal_answers == {"_schema": SCHEMA.payload}
    assert item.application.portal_url == gitlab.job_url(job)
    a = item.application
    a.status = "approved"
    a.save()
    browser_task, ordinary = Mock(), Mock()
    monkeypatch.setattr(tasks.send_browser_application, "delay", browser_task)
    monkeypatch.setattr(tasks.send_application, "delay", ordinary)
    tasks.dispatch_submissions()
    browser_task.assert_called_once_with(a.pk)
    ordinary.assert_not_called()


@pytest.mark.parametrize("enabled", [True, False])
def test_new_form_prefills_only_opted_in_owned_contacts(
    gitlab_job, user, other, settings, monkeypatch, authenticated, enabled
):
    from cryptography.fernet import Fernet

    from jobs.services.saved_answers import save_answers

    settings.TOKEN_ENCRYPTION_KEY = Fernet.generate_key().decode()
    monkeypatch.setattr(gitlab, "read_schema", lambda job: SCHEMA)
    contacts = {
        "first_name": "Saved",
        "last_name": "Candidate",
        "email": "saved@example.invalid",
        "phone": "+972502345678",
        "phone_country": "IL",
        "linkedin": "https://linkedin.com/in/synthetic",
        "gender_identity": "male",
        "work_authorization_notes": "Do not infer sponsorship",
    }
    save_answers(user.pk, contacts, reuse_enabled=enabled)
    save_answers(other.pk, {"first_name": "Other owner"}, reuse_enabled=True)
    response = authenticated.get(f"/jobs/{gitlab_job.pk}/apply/")
    assert response.status_code == 200
    form = response.context["form"]
    for name in ("first_name", "last_name", "email", "phone", "phone_country"):
        assert form[f"gitlab_{name}"].value() == (contacts[name] if enabled else "")
    assert form["gitlab_question_37761129002"].value() == (contacts["linkedin"] if enabled else "")
    for question in SCHEMA.entries:
        name = question["fields"][0]["name"]
        if name.startswith("question_") and name != "question_37761129002":
            assert form[f"gitlab_{name}"].value() == ""
    assert not Application.objects.filter(owner=user, job=gitlab_job).exists()

    # Invalid POST must keep explicit edits and blanks, not resurrect saved data.
    response = authenticated.post(
        f"/jobs/{gitlab_job.pk}/apply/",
        {"subject": "Application", "gitlab_first_name": "Edited", "gitlab_email": ""},
    )
    assert response.context["form"]["gitlab_first_name"].value() == "Edited"
    assert response.context["form"]["gitlab_email"].value() == ""


def test_prefill_never_replaces_existing_draft_or_changed_schema(
    gitlab_application, settings, monkeypatch
):
    from cryptography.fernet import Fernet

    from jobs.forms import ApplicationForm
    from jobs.services.saved_answers import save_answers

    a = gitlab_application
    settings.TOKEN_ENCRYPTION_KEY = Fernet.generate_key().decode()
    save_answers(a.owner_id, {"first_name": "Saved"}, reuse_enabled=True)
    monkeypatch.setattr(gitlab, "read_schema", lambda job: SCHEMA)
    assert ApplicationForm(owner=a.owner, instance=a)["gitlab_first_name"].value() == "Synthetic"
    a.portal_fingerprint = "outdated"
    assert ApplicationForm(owner=a.owner, instance=a)["gitlab_first_name"].value() == ""
