import hashlib
import uuid

import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile

from jobs.models import Application, ApplicationPreferences, Document
from jobs.services.batches import create, prepare, recheck_fit
from jobs.services.matching import assess
from jobs.services.normalization import normalize_description
from jobs.services.submissions import approve


def job_data(text):
    return {"description": text, "normalized": normalize_description(text)}


@pytest.mark.parametrize(
    "requirement,excluded,state",
    [
        ("Python, Node.js, or Go", ["Node.js"], "eligible"),
        ("Python or Node.js", ["Node.js"], "eligible"),
        ("Python/Node.js", ["Node.js"], "eligible"),
        ("Python and Node.js", ["Node.js"], "skipped"),
        ("Java or PHP", ["Java", "PHP"], "skipped"),
        ("Python, Node.js, or Go and TypeScript", ["Node.js", "TypeScript"], "skipped"),
        ("C++ and Python", ["C++"], "skipped"),
        ("Python or Node.js using Rust", ["Rust"], "needs_action"),
    ],
)
def test_mandatory_exclusions_and_alternatives(requirement, excluded, state):
    assert assess(job_data("Requirements\n" + requirement), excluded, "Python, Go").state == state


def test_preferences_override_cv_overlap_but_not_optional_skills():
    assert (
        assess(job_data("Requirements\nJava and Python"), ["Java"], "Python Java").state
        == "skipped"
    )
    result = assess(job_data("Requirements\nPython\nNice to have\nNode.js"), ["Node.js"], "Python")
    assert result.state == "eligible" and result.matched == ("Python",)
    assert (
        assess(job_data("Requirements\nPython or Node.js"), ["Node.js"], "Node.js").state
        == "skipped"
    )


def test_one_overlap_is_enough_and_zero_overlap_skips():
    result = assess(job_data("Requirements\nPython and AWS and Kubernetes"), [], "Python")
    assert result.state == "eligible" and result.matched == ("Python",)
    assert (
        assess(job_data("Requirements\nRust"), [], "Python").reason == "fit_no_technology_overlap"
    )
    assert assess(job_data(""), [], "Python").state == "needs_action"
    assert assess(job_data("Java developer"), ["Java"], "Python Java").state == "needs_action"


@pytest.fixture
def cv(user, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    from jobs.services.resumes import pdf_bytes

    data = pdf_bytes("Alice", "", [{"section": "Skills", "text": "Python Linux"}])
    document = Document(
        owner=user, name="CV", sha256=hashlib.sha256(data).hexdigest(), size=len(data)
    )
    document.file.save("cv.pdf", ContentFile(data), save=False)
    document.save()
    return document


@pytest.mark.django_db
def test_preparation_skips_before_tailoring_then_can_recheck(user, job, cv, monkeypatch):
    job.description = "Requirements\nJava and Python"
    job.normalized = normalize_description(job.description)
    job.apply_email = "jobs@example.com"
    job.save()
    prefs = ApplicationPreferences.objects.create(owner=user, excluded_technologies=["Java"])
    batch = create(user, cv.pk, [job.pk], uuid.uuid4(), check_fit=True)
    item = batch.items.get()

    def forbidden(*args, **kwargs):
        raise AssertionError("Skipped jobs must not call tailoring or send")

    monkeypatch.setattr("jobs.services.resumes.tailor", forbidden)
    prepare(item.pk)
    item.refresh_from_db()
    assert item.state == "skipped" and item.error_code == "fit_excluded_technology"
    assert not Application.objects.exists()
    prefs.excluded_technologies = []
    prefs.save()
    recheck_fit(user, batch.pk)
    prepare(item.pk)
    item.refresh_from_db()
    assert item.state == "ready" and item.provenance["fit"]["matched"] == ["Python"]
    assert item.application.status == "draft"


@pytest.mark.django_db
def test_no_fit_and_unreadable_cv_are_not_sent(user, job, cv):
    job.description = "Requirements\nRust"
    job.save()
    batch = create(user, cv.pk, [job.pk], uuid.uuid4(), check_fit=True)
    item = batch.items.get()
    prepare(item.pk)
    item.refresh_from_db()
    assert item.state == "skipped" and item.error_code == "fit_no_technology_overlap"
    assert not Application.objects.exists()


@pytest.mark.django_db
def test_saved_preferences_are_private_and_validated(authenticated, user, other):
    assert authenticated.get("/preferences/").status_code == 200
    assert not ApplicationPreferences.objects.exists()
    assert (
        authenticated.post(
            "/preferences/", {"excluded_technologies": ["Java", "Node.js"]}
        ).status_code
        == 302
    )
    assert ApplicationPreferences.objects.get(owner=user).excluded_technologies == [
        "Java",
        "Node.js",
    ]
    authenticated.force_login(other)
    assert b"checked" not in authenticated.get("/preferences/").content
    response = authenticated.post("/preferences/", {"excluded_technologies": ["invented"]})
    assert response.status_code == 200
    assert not ApplicationPreferences.objects.filter(owner=other).exists()
    for value in [["Java", "Java"], ["invented"], {"Java": True}, [None]]:
        with pytest.raises(ValidationError):
            ApplicationPreferences(owner=other, excluded_technologies=value).full_clean()


@pytest.mark.django_db
def test_updated_exclusions_block_approval(application, connection):
    application.job.description = "Requirements\nJava and Python"
    application.job.normalized = normalize_description(application.job.description)
    application.job.save()
    ApplicationPreferences.objects.create(owner=application.owner, excluded_technologies=["Java"])
    with pytest.raises(ValidationError, match="preferences"):
        approve(application.owner, application.pk)
    application.refresh_from_db()
    assert application.status == "draft"


def test_preferred_only_exclusion_is_not_mandatory():
    result = assess(job_data("Python developer\nNice to have\nNode.js"), ["Node.js"], "Python")
    assert result.state == "eligible"


@pytest.mark.django_db
def test_unreadable_cv_pauses_and_recheck_is_owner_scoped(user, other, job, cv):
    from jobs.models import ApplicationBatch

    broken = b"%PDF-1.7\nfixture\n%%EOF"
    cv.file.save("broken.pdf", ContentFile(broken), save=False)
    cv.sha256 = hashlib.sha256(broken).hexdigest()
    cv.size = len(broken)
    cv.save()
    batch = create(user, cv.pk, [job.pk], uuid.uuid4(), check_fit=True)
    item = batch.items.get()
    prepare(item.pk)
    item.refresh_from_db()
    assert item.state == "needs_action" and item.error_code == "fit_cv_needs_review"
    assert not item.application_id
    with pytest.raises(ApplicationBatch.DoesNotExist):
        recheck_fit(other, batch.pk)


@pytest.mark.django_db
def test_preference_change_blocks_queued_send(application, connection, monkeypatch):
    from unittest.mock import Mock

    from jobs.services import gmail, submissions

    application.job.description = "Requirements\nJava and Python"
    application.job.normalized = normalize_description(application.job.description)
    application.job.save()
    approve(application.owner, application.pk)
    ApplicationPreferences.objects.create(owner=application.owner, excluded_technologies=["Java"])
    send = Mock()
    monkeypatch.setattr(gmail, "send", send)
    submissions.submit(application.pk)
    send.assert_not_called()
    application.refresh_from_db()
    assert application.status == "failed" and application.error_code == "preflight_failed"


def test_inline_optional_skills_are_not_mandatory():
    result = assess(job_data("Requirements\nPython\nNode.js is optional"), ["Node.js"], "Python")
    assert result.state == "eligible"
    result = assess(
        job_data("Requirements\nPython\nNode.js is not required"), ["Node.js"], "Python"
    )
    assert result.state == "eligible"
