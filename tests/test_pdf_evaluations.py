"""Uploaded PDFs are sufficient input; manual factual profiles remain optional."""

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from jobs.models import AgentExecution, ResumeProfile
from jobs.services import evaluations, resumes
from jobs.services.documents import upload_document

pytestmark = pytest.mark.django_db


@pytest.fixture
def uploaded_cv(user, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.STORAGES = {
        **settings.STORAGES,
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    }
    data = resumes.pdf_bytes(
        "Synthetic Engineer",
        "",
        [
            {"section": "Experience", "text": "Built Python services and Linux automation."},
        ],
    )
    return upload_document(user, SimpleUploadedFile("synthetic.pdf", data, "application/pdf"))


def test_uploaded_pdf_without_manual_profile_is_immutable_and_deduplicated(
    user, job, uploaded_cv, agent_settings
):
    assert not ResumeProfile.objects.exists()
    record = evaluations.start(user, job, document_id=uploaded_cv.pk)
    assert record.profile_id is None
    assert record.document_id == uploaded_cv.pk
    assert record.snapshot["input_source"] == {
        "kind": "uploaded_pdf",
        "document_id": uploaded_cv.pk,
        "sha256": uploaded_cv.sha256,
    }
    assert (
        "".join(f["text"] for f in record.snapshot["facts"]) == resumes.extract(uploaded_cv).strip()
    )
    assert evaluations.start(user, job, document_id=uploaded_cv.pk).pk == record.pk
    assert not ResumeProfile.objects.exists()
    original = record.snapshot["job"]["description"]
    job.description = "Changed vacancy requiring Python and Linux"
    job.save()
    record.refresh_from_db()
    assert record.snapshot["job"]["description"] == original
    assert evaluations.start(user, job, document_id=uploaded_cv.pk).pk != record.pk


def test_document_ownership_checked_before_extraction(
    other, job, uploaded_cv, agent_settings, monkeypatch
):
    def forbidden(*args):
        raise AssertionError("Must not read another owner's document")

    monkeypatch.setattr(resumes, "extract", forbidden)
    with pytest.raises(ValidationError, match="your uploaded CVs"):
        evaluations.start(other, job, document_id=uploaded_cv.pk)
    assert not AgentExecution.objects.exists()


@pytest.mark.parametrize(
    "error", [ValidationError("private parser details"), OSError("private storage")]
)
def test_unreadable_pdf_reports_actionable_error(
    user, job, uploaded_cv, agent_settings, monkeypatch, error
):
    def unreadable(*args):
        raise error

    monkeypatch.setattr(resumes, "extract", unreadable)
    with pytest.raises(ValidationError, match="text-based PDF") as exc:
        evaluations.start(user, job, document_id=uploaded_cv.pk)
    assert "private" not in str(exc.value)
    assert not AgentExecution.objects.exists()
    assert not ResumeProfile.objects.exists()


def test_chunks_preserve_long_source_and_input_budget(
    user, job, uploaded_cv, agent_settings, monkeypatch, settings
):
    text = ("Python services\nLinux automation.\n\n" * 160).strip()
    monkeypatch.setattr(resumes, "extract", lambda *args: text)
    record = evaluations.start(user, job, document_id=uploaded_cv.pk)
    assert "".join(f["text"] for f in record.snapshot["facts"]) == text
    assert all(len(f["text"]) <= 4000 for f in record.snapshot["facts"])
    settings.AI_MAX_INPUT_CHARACTERS = 100
    with pytest.raises(ValidationError, match="input budget"):
        evaluations.start(user, job, document_id=uploaded_cv.pk)
    assert AgentExecution.objects.count() == 1


def test_document_ui_and_post_do_not_require_profile(
    authenticated, job, uploaded_cv, agent_settings
):
    from bs4 import BeautifulSoup

    response = authenticated.get(f"/jobs/{job.pk}/")
    panel = BeautifulSoup(response.content, "html.parser").select_one("#vacancy-evaluation")
    assert not panel.find("button", string="Evaluate").has_attr("disabled")
    assert panel.select_one('input[type="hidden"][name="cv"]')["value"] == (
        f"document:{uploaded_cv.pk}"
    )
    assert not panel.find("select")  # Current-base selection is automatic in the UI.
    response = authenticated.post(f"/jobs/{job.pk}/evaluate/", {"cv": f"document:{uploaded_cv.pk}"})
    assert response.status_code == 302
    assert AgentExecution.objects.get().document_id == uploaded_cv.pk
    assert not ResumeProfile.objects.exists()


def test_other_document_and_invalid_selection_cannot_enqueue(
    authenticated, other, job, uploaded_cv, agent_settings
):
    uploaded_cv.owner = other
    uploaded_cv.save(update_fields=["owner"])
    for selection in (f"document:{uploaded_cv.pk}", "document:invalid", "wrong:1", ""):
        response = authenticated.post(f"/jobs/{job.pk}/evaluate/", {"cv": selection})
        assert response.status_code == 302
    assert not AgentExecution.objects.exists()


def test_modified_pdf_cannot_be_evaluated(user, job, uploaded_cv, agent_settings):
    with uploaded_cv.file.open("wb") as stream:
        stream.write(b"%PDF-1.7\nchanged file\n%%EOF")
    with pytest.raises(ValidationError, match="could not be read"):
        evaluations.start(user, job, document_id=uploaded_cv.pk)
    assert not AgentExecution.objects.exists()
