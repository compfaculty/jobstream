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
        document,
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
