import io
import uuid

import pytest
from django.core.exceptions import ValidationError
from pypdf import PdfReader

from jobs.services.batches import create, prepare
from jobs.services.resumes import confirm, extract, pdf_bytes, ranked_facts
from tests.test_batches import document  # noqa: F401

pytestmark = pytest.mark.django_db


def test_confirmed_additions_and_ranking(user, document):  # noqa: F811
    profile = confirm(
        user,
        document,  # noqa: F811
        "Alice Engineer",
        "alice@example.com",
        {
            "Experience": (
                "Built Go services at Example (2020-2022).\n\n"
                "Built Python APIs at Acme (2022-present)."
            ),
            "Projects": "Evaluated local AI models in a personal project.",
        },
    )
    facts = ranked_facts(profile, {"title": "Python engineer", "description": "Python APIs"})
    assert "Acme" in facts[0]["text"]
    assert {fact["text"] for fact in facts} == {fact["text"] for fact in profile.facts}
    data = pdf_bytes(profile.name, profile.contact, facts)
    text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(data)).pages)
    assert "Alice Engineer" in text
    assert "local AI models" in text
    assert "2020-2022" in text
    assert "Kubernetes" not in text


def test_profile_owner_and_empty_facts(user, other, document):  # noqa: F811
    with pytest.raises(ValidationError):
        confirm(other, document, "Bob", "", {"Skills": "Python"})
    with pytest.raises(ValidationError):
        confirm(user, document, "Alice", "", {})


def test_tailored_batch_snapshot_and_provenance(user, document, job):  # noqa: F811
    profile = confirm(user, document, "Alice", "", {"Skills": "Python, Linux"})
    job.apply_email = "jobs@example.com"
    job.save()
    batch = create(user, document.pk, [job.pk], uuid.uuid4(), profile_id=profile.pk)
    job.title = "Changed title"
    job.save()
    item = batch.items.get()
    prepare(item.pk)
    item.refresh_from_db()
    assert item.state == "ready"
    assert item.application.subject == "Application: Python engineer"
    assert item.application.document_id != document.pk
    assert item.provenance["profile_id"] == profile.pk
    assert item.provenance["fact_ids"] == ["fact-1"]
    assert "Python, Linux" in item.application.body
    assert "Python, Linux" in extract(item.application.document)


def test_unsupported_glyphs_are_explicit():
    with pytest.raises(ValidationError, match="font"):
        pdf_bytes("Alice", "", [{"section": "Skills", "text": "Python 🐍"}])


def test_preparation_cache_reuses_pdf_and_is_profile_versioned(user, document, monkeypatch):  # noqa: F811
    from jobs.services.resumes import tailor

    profile = confirm(user, document, "Alice", "", {"Skills": "Python"})
    job = {"title": "Python engineer", "company": "Acme", "description": "Python"}
    first, provenance, body = tailor(profile, job)
    from unittest.mock import Mock

    renderer = Mock(wraps=pdf_bytes)
    monkeypatch.setattr("jobs.services.resumes.pdf_bytes", renderer)
    same, other_provenance, same_body = tailor(profile, job)
    assert first.pk == same.pk
    assert provenance == other_provenance
    assert body == same_body
    renderer.assert_not_called()
    profile.contact = "new@example.com"
    changed, _, _ = tailor(profile, job)
    assert changed.pk != first.pk
    renderer.assert_called_once()


def test_existing_application_skips_tailoring(user, document, job, application, monkeypatch):  # noqa: F811
    from unittest.mock import Mock

    renderer = Mock(side_effect=AssertionError("must not tailor an existing application"))
    monkeypatch.setattr("jobs.services.resumes.tailor", renderer)
    batch = create(user, document.pk, [job.pk], uuid.uuid4())
    prepare(batch.items.get().pk)
    item = batch.items.get()
    assert item.state == "existing" and item.application_id == application.pk
    renderer.assert_not_called()


def test_role_headings_are_bold_larger_and_dates_are_distinct():
    data = pdf_bytes(
        "Alice Engineer",
        "alice@example.com",
        [
            {
                "section": "Experience",
                "text": (
                    "Senior Network Engineer | Acme / Cloud | Dec 2023 - Jul 2025\n"
                    "• Built Python frameworks for Linux.\n"
                    "Team Lead | Storage Inc | 2018 - 2021\n"
                    "• Validated recovery & upgrades <safely>."
                ),
            }
        ],
        job={"title": "Automation Engineer", "company": "Example"},
    )
    reader = PdfReader(io.BytesIO(data))
    runs = []
    for page in reader.pages:
        page.extract_text(
            visitor_text=lambda text, cm, tm, font, size: runs.append(
                (text, str((font or {}).get("/BaseFont", "")), size)
            )
        )
    heading = next(run for run in runs if "Senior Network Engineer" in run[0])
    bullet = next(run for run in runs if "Built Python" in run[0])
    dates = next(run for run in runs if "Dec 2023" in run[0])
    assert "Bold" in heading[1] and heading[2] > bullet[2] > dates[2]
    text = "\n".join(run[0] for run in runs)
    assert "Automation Engineer | Example" in text
    assert "recovery & upgrades <safely>" in text
    assert "Team Lead | Storage Inc" in text


def test_contact_override_is_private_versioned_and_per_job(user, document):  # noqa: F811
    from jobs.services.resumes import tailor

    profile = confirm(
        user,
        document,  # noqa: F811
        "Alice",
        "alice@old.example",
        {
            "Skills": "Python, AWS",
            "Other": "alice@old.example",
        },
        details={"basics": {"email": "alice@old.example"}},
    )
    job = {"id": 1, "title": "Automation Engineer", "company": "Example", "description": "Python"}
    first, provenance, _ = tailor(profile, job, contact_email="alice@gmail.com")
    text = extract(first)
    assert "alice@gmail.com" in text and "alice@old.example" not in text
    assert provenance["contact_email"] == "alice@gmail.com"
    profile.refresh_from_db()
    assert profile.contact == "alice@old.example"
    assert profile.facts[-1]["text"] == "alice@old.example"
    second, _, _ = tailor(
        profile, {**job, "id": 2, "company": "Different"}, contact_email="alice@gmail.com"
    )
    assert second.pk != first.pk and second.sha256 != first.sha256
    assert "Different" in extract(second)


def test_single_application_generation_requires_review(
    authenticated,
    user,
    document,  # noqa: F811
    application,
    connection,  # noqa: F811
):  # noqa: F811
    profile = confirm(user, document, "Alice", "alice@old.example", {"Skills": "Python, Linux"})
    application.status = "approved"
    application.approval_digest = "old-approval"
    application.save()
    response = authenticated.post(
        f"/applications/{application.pk}/cv/",
        {
            "profile": profile.pk,
            "contact_email": "alice@example.com",
        },
    )
    assert response.status_code == 302
    application.refresh_from_db()
    assert application.status == "draft" and application.approval_digest == ""
    assert application.approved_at is None and application.send_attempts == 0
    assert application.document_id != document.pk
    assert "alice@example.com" in extract(application.document)
    assert authenticated.get(f"/applications/{application.pk}/cv/").status_code == 405


def test_single_generation_denies_foreign_profile_and_application(
    authenticated,
    user,
    other,
    document,  # noqa: F811
    application,  # noqa: F811
):  # noqa: F811
    profile = confirm(user, document, "Alice", "", {"Skills": "Python"})
    profile.owner = other
    profile.save(update_fields=["owner"])
    payload = {"profile": profile.pk, "contact_email": "alice@example.com"}
    assert authenticated.post(f"/applications/{application.pk}/cv/", payload).status_code == 400
    application.refresh_from_db()
    assert application.document_id is None
    application.owner = other
    application.save(update_fields=["owner"])
    assert authenticated.post(f"/applications/{application.pk}/cv/", payload).status_code == 404


@pytest.mark.parametrize("status", ["submitting", "confirmed", "unknown"])
def test_generation_cannot_change_locked_application(
    authenticated,
    user,
    document,  # noqa: F811
    application,
    status,  # noqa: F811
):  # noqa: F811
    profile = confirm(user, document, "Alice", "", {"Skills": "Python"})
    application.status = status
    application.save()
    response = authenticated.post(
        f"/applications/{application.pk}/cv/",
        {
            "profile": profile.pk,
            "contact_email": "alice@example.com",
        },
    )
    assert response.status_code == 400
    application.refresh_from_db()
    assert application.status == status and application.document_id is None


def test_generation_rejects_stale_draft(user, document, application, monkeypatch):  # noqa: F811
    from django.utils import timezone

    from jobs.services import resumes

    profile = confirm(user, document, "Alice", "", {"Skills": "Python"})
    original = resumes.tailor

    def concurrent_edit(*args, **kwargs):
        result = original(*args, **kwargs)
        type(application).objects.filter(pk=application.pk).update(
            body="New user edit", updated_at=timezone.now()
        )
        return result

    monkeypatch.setattr(resumes, "tailor", concurrent_edit)
    with pytest.raises(ValidationError, match="changed during generation"):
        resumes.prepare_application_cv(user, application.pk, profile.pk, "alice@example.com")
    application.refresh_from_db()
    assert application.body == "New user edit" and application.document_id is None


def test_cv_generation_requires_csrf(user, document, application):  # noqa: F811
    from django.test import Client

    profile = confirm(user, document, "Alice", "", {"Skills": "Python"})
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    assert (
        client.post(
            f"/applications/{application.pk}/cv/",
            {
                "profile": profile.pk,
                "contact_email": "alice@example.com",
            },
        ).status_code
        == 403
    )
    application.refresh_from_db()
    assert application.document_id is None


def test_render_failure_keeps_existing_draft(user, document, application, monkeypatch):  # noqa: F811
    from jobs.services import resumes

    profile = confirm(user, document, "Alice", "", {"Skills": "Python"})

    def fail(*args, **kwargs):
        raise ValidationError("The CV font does not support some characters.")

    monkeypatch.setattr(resumes, "pdf_bytes", fail)
    with pytest.raises(ValidationError, match="font"):
        resumes.prepare_application_cv(user, application.pk, profile.pk, "alice@example.com")
    application.refresh_from_db()
    assert application.status == "draft" and application.document_id is None
    assert application.body == "My application"


def test_pdf_keeps_roles_chronological_even_when_relevance_order_differs():
    facts = [
        {
            "section": "Experience",
            "text": (
                "Engineer | Old Co | 2018 - 2021\n• Built storage tests.\n"
                "APPLIED PRACTICE\n• Built a personal prototype."
            ),
        },
        {
            "section": "Experience",
            "text": "Network Engineer | Recent Co | 2023 - 2025\n• Validated Linux networking.",
        },
    ]
    data = pdf_bytes("Alice", "", facts)
    text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(data)).pages)
    assert text.index("Recent Co") < text.index("Old Co") < text.index("APPLIED PRACTICE")
    assert "Built storage tests." in text and "Built a personal prototype." in text


def test_pdf_deduplicates_only_contact_components_already_in_header():
    data = pdf_bytes(
        "Alice",
        "alice@example.com | https://github.com/alice",
        [
            {"section": "Other", "text": "alice@example.com | github.com/alice"},
            {"section": "Other", "text": "Languages: English | Hebrew"},
        ],
    )
    text = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(data)).pages)
    assert text.count("alice@example.com") == 1
    assert "Languages: English | Hebrew" in text


def test_generated_filename_identifies_person_company_and_role(authenticated, user, document):  # noqa: F811
    from jobs.services.resumes import tailor

    profile = confirm(user, document, "Example Candidate", "", {"Skills": "Python"})
    generated, _, _ = tailor(
        profile,
        {
            "title": "Automation Engineer",
            "company": "Earnix",
            "description": "Python",
        },
    )
    assert generated.name == "Example_Candidate_Earnix_Automation_Engineer_CV.pdf"
    response = authenticated.get(f"/documents/{generated.pk}/")
    assert response.status_code == 200
    assert f'filename="{generated.name}"' in response["Content-Disposition"]
    response.close()


@pytest.mark.parametrize("name", ["../bad/evil\r\nX-Header: value.pdf", '"quoted".pdf', ""])
def test_pdf_filenames_are_header_safe(name):
    import re

    from jobs.services.documents import pdf_filename

    result = pdf_filename(name)
    assert re.fullmatch(r"[A-Za-z0-9_]+\.pdf", result)
    assert len(result) <= 144


def test_gmail_attachment_uses_descriptive_document_name(application, document):  # noqa: F811
    import base64
    from email import message_from_bytes

    from jobs.services.gmail import build_message

    document.name = "Example_Candidate_Earnix_Automation_Engineer_CV.pdf"
    application.document = document
    message = message_from_bytes(
        base64.urlsafe_b64decode(build_message(application, "alice@example.com", b"%PDF-test"))
    )
    assert [part.get_filename() for part in message.walk() if part.get_filename()] == [
        document.name
    ]
