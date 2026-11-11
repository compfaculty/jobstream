import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse

from jobs.services.profile_prefill import from_text
from tests.test_batches import document  # noqa: F401

CV = """EXAMPLE CANDIDATE
SENIOR SALES MANAGER |
ENTERPRISE ACCOUNTS
Israel | +972-50-123-4567
candidate@example.com | github.com/example | linkedin.com/in/example
PROFESSIONAL SUMMARY
Leads enterprise sales teams.
TECHNICAL SKILLS
CRM, account planning
PROFESSIONAL EXPERIENCE
Sales Manager | Example Co | 2020–Present
Managed a team of eight.
EDUCATION
Example University, BA, 2010
LANGUAGES
English, Hebrew
CERTIFICATIONS
Example credential
SELECTED PROJECTS
Customer onboarding programme
"""


def test_prefill_contacts_sections_and_wrapped_headline():
    values = from_text(CV)
    assert values["name"] == "EXAMPLE CANDIDATE"
    assert values["headline"] == "SENIOR SALES MANAGER | ENTERPRISE ACCOUNTS"
    assert values["email"] == "candidate@example.com"
    assert values["phone"] == "+972-50-123-4567"
    assert values["location"] == "Israel"
    assert values["github"] == "https://github.com/example"
    assert values["linkedin"] == "https://linkedin.com/in/example"
    assert values["summary"] == "Leads enterprise sales teams."
    assert "CRM" in values["skills"]
    assert "2020–Present" in values["experience"]
    assert "Example University" in values["education"]
    assert values["languages"] == "English, Hebrew"
    assert values["certifications"] == "Example credential"
    assert "onboarding" in values["projects"]
    assert "confirmed" not in values


def test_unknown_layout_preserves_prose_and_bounds():
    text = "Example Candidate\nUnusual career narrative\nNo standard headings."
    values = from_text(text)
    assert "Unusual career narrative" in values["other"]
    assert "No standard headings." in values["other"]
    assert from_text("") == {}
    with pytest.raises(ValueError):
        from_text("x" * 60001)


@pytest.mark.django_db
def test_new_profile_is_prefilled_without_saving(authenticated, document, monkeypatch):  # noqa: F811
    monkeypatch.setattr("jobs.services.resumes.extract", lambda doc: CV)
    response = authenticated.get(reverse("resume-profile", args=[document.pk]))
    assert response.status_code == 200
    assert response.context["prefilled"]
    assert response.context["form"]["email"].value() == "candidate@example.com"
    assert "Managed a team" in response.context["form"]["experience"].value()
    assert not response.context["form"]["confirmed"].value()
    assert not document.profiles.exists()

    # Imported free text can be saved without manually creating structured entries.
    data = from_text(CV)
    data["confirmed"] = "on"
    for group in ("experience", "education", "projects"):
        data[f"{group}-TOTAL_FORMS"] = "1"
        data[f"{group}-INITIAL_FORMS"] = "0"
    assert (
        authenticated.post(reverse("resume-profile", args=[document.pk]), data).status_code == 302
    )
    profile = document.profiles.get()
    assert any("Managed a team of eight." in fact["text"] for fact in profile.facts)
    assert profile.details["basics"]["headline"] == from_text(CV)["headline"]


@pytest.mark.django_db
def test_saved_edits_and_invalid_posts_are_never_replaced(authenticated, document, monkeypatch):  # noqa: F811
    from jobs.services.resumes import confirm

    monkeypatch.setattr("jobs.services.resumes.extract", lambda doc: CV)
    url = reverse("resume-profile", args=[document.pk])
    response = authenticated.post(url, {"name": "My edited name", "email": "invalid"})
    assert response.context["form"]["name"].value() == "My edited name"
    assert response.context["form"]["email"].value() == "invalid"
    assert not response.context["prefilled"]
    confirm(document.owner, document, "Saved name", "", {"Skills": "Saved skills"})
    response = authenticated.get(url)
    assert response.context["form"]["name"].value() == "Saved name"
    assert response.context["form"]["skills"].value() == "Saved skills"
    assert not response.context["prefilled"]


@pytest.mark.django_db
def test_ownership_checked_before_extracting(authenticated, document, other, monkeypatch):  # noqa: F811
    document.owner = other
    document.save()
    monkeypatch.setattr("jobs.services.resumes.extract", lambda doc: pytest.fail("Read another CV"))
    assert authenticated.get(reverse("resume-profile", args=[document.pk])).status_code == 404


@pytest.mark.django_db
def test_extraction_failure_leaves_manual_form(authenticated, document, monkeypatch):  # noqa: F811
    def fail(doc):
        raise ValidationError("Unreadable PDF")

    monkeypatch.setattr("jobs.services.resumes.extract", fail)
    response = authenticated.get(reverse("resume-profile", args=[document.pk]))
    assert response.status_code == 200
    assert not response.context["prefilled"]
    assert b"Unreadable PDF" in response.content


@pytest.mark.django_db
def test_untrusted_cv_text_is_escaped(authenticated, document, monkeypatch):  # noqa: F811
    monkeypatch.setattr(
        "jobs.services.resumes.extract", lambda doc: "SUMMARY\n<script>alert(1)</script>"
    )
    response = authenticated.get(reverse("resume-profile", args=[document.pk]))
    assert b"<script>alert(1)</script>" not in response.content
    assert b"&lt;script&gt;" in response.content
