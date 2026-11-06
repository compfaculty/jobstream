import pytest
from django.urls import reverse

from jobs.forms import ProfileExperienceForm, ResumeProfileForm
from jobs.models import ResumeProfile
from tests.test_batches import document  # noqa: F401


def payload():
    return {
        "name": "Example Engineer",
        "email": "engineer@example.com",
        "phone": "+972 50 123 4567",
        "headline": "Backend Engineer",
        "confirmed": "on",
        "experience-TOTAL_FORMS": "1",
        "experience-INITIAL_FORMS": "0",
        "experience-0-title": "Software Engineer",
        "experience-0-company": "Example Co",
        "experience-0-start": "2020-01",
        "experience-0-end": "2024-05",
        "experience-0-description": "Built Python services.",
        "education-TOTAL_FORMS": "0",
        "education-INITIAL_FORMS": "0",
        "projects-TOTAL_FORMS": "0",
        "projects-INITIAL_FORMS": "0",
    }


def test_contact_and_dates_validation():
    assert not ResumeProfileForm({"name": "Example", "email": "bad", "confirmed": "on"}).is_valid()
    for changes in ({"end": "2019-01"}, {"current": True}, {"start": "nonsense"}):
        data = {"title": "Engineer", "company": "Example", "start": "2020-01", "end": "2024-01"}
        data.update(changes)
        assert not ProfileExperienceForm(data).is_valid()


@pytest.mark.django_db
def test_structured_profile_roundtrip(authenticated, user, document):  # noqa: F811
    url = reverse("resume-profile", args=[document.pk])
    response = authenticated.get(url)
    assert response.status_code == 200
    assert b"Email address" in response.content
    assert b"Job title" in response.content
    data = payload()
    assert authenticated.post(url, data).status_code == 302
    first = ResumeProfile.objects.get(owner=user)
    assert first.details["basics"]["email"] == data["email"]
    assert first.details["experience"][0]["start"] == "2020-01"
    assert data["email"] in first.contact and data["phone"] in first.contact
    assert any("Example Co" in fact["text"] for fact in first.facts)
    response = authenticated.get(url)
    assert response.context["form"]["email"].value() == data["email"]
    entries = response.context["entry_groups"][0]["forms"]
    assert entries.forms[0]["company"].value() == "Example Co"
    data["experience-INITIAL_FORMS"] = "1"
    data["experience-0-company"] = "New Company"
    assert authenticated.post(url, data).status_code == 302
    first.refresh_from_db()
    assert first.details["experience"][0]["company"] == "Example Co"
    assert ResumeProfile.objects.count() == 2


@pytest.mark.django_db
def test_profile_errors_and_ownership(authenticated, other, document):  # noqa: F811
    url = reverse("resume-profile", args=[document.pk])
    data = payload()
    data["experience-0-end"] = "2019-01"
    response = authenticated.post(url, data)
    assert response.status_code == 200
    assert b"End date must be" in response.content
    assert not ResumeProfile.objects.exists()
    data = payload()
    del data["projects-TOTAL_FORMS"]
    assert authenticated.post(url, data).status_code == 200
    assert not ResumeProfile.objects.exists()
    document.owner = other
    document.save()
    assert authenticated.get(url).status_code == 404
    assert authenticated.post(url, payload()).status_code == 404


@pytest.mark.django_db
def test_legacy_profile_preserved(authenticated, user, document):  # noqa: F811
    from jobs.services.resumes import confirm

    confirm(user, document, "Example", "old@example.com", {"Experience": "Legacy role at Example."})
    response = authenticated.get(reverse("resume-profile", args=[document.pk]))
    assert response.context["form"]["experience"].value() == "Legacy role at Example."
    assert response.context["form"]["contact"].value() == "old@example.com"
    assert b"Legacy role at Example." in response.content


@pytest.mark.browser
@pytest.mark.django_db(transaction=True)
def test_profile_desktop_mobile_and_dynamic_entries(page, live_server, settings, user, document):  # noqa: F811
    from playwright.sync_api import expect

    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(live_server.url + "/account/login/")
    page.get_by_label("Username").fill(user.username)
    page.get_by_label("Password").fill("a-long-test-password-123")
    page.get_by_role("button", name="Next", exact=True).click()
    page.goto(live_server.url + reverse("resume-profile", args=[document.pk]))
    expect(page.get_by_label("Email address", exact=True)).to_be_visible()
    page.get_by_label("Full name", exact=True).fill("Example Engineer")
    page.get_by_label("Email address", exact=True).fill("engineer@example.com")
    page.get_by_label("Skills and technologies", exact=True).fill("Python, Linux")
    page.get_by_role("button", name="+ Add experience entry", exact=True).click()
    expect(page.locator("#id_experience-TOTAL_FORMS")).to_have_value("2")
    page.locator("#id_experience-1-title").fill("Engineer")
    page.locator("#id_experience-1-company").fill("Example Co")
    page.locator("#id_experience-1-start").fill("2020-01")
    page.locator("#id_experience-1-current").check()
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.screenshot(path="/tmp/jobstream-profile-desktop.png", full_page=True)
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path="/tmp/jobstream-profile-mobile.png", full_page=True)
    page.get_by_label("I confirm these facts", exact=False).check()
    page.get_by_role("button", name="Confirm and save profile", exact=True).click()
    expect(page).to_have_url(live_server.url + reverse("documents"))
    page.goto(live_server.url + reverse("resume-profile", args=[document.pk]))
    expect(page.get_by_label("Email address", exact=True)).to_have_value("engineer@example.com")
    expect(page.locator("#id_experience-0-company")).to_have_value("Example Co")
    expect(page.locator("#id_experience-0-start")).to_have_value("2020-01")
    assert not errors


@pytest.mark.django_db
def test_deleted_entries_and_contact_limit(authenticated, document):  # noqa: F811
    url = reverse("resume-profile", args=[document.pk])
    data = payload()
    data.update({"skills": "Linux", "experience-0-DELETE": "on"})
    assert authenticated.post(url, data).status_code == 302
    profile = ResumeProfile.objects.latest("pk")
    assert profile.details["experience"] == []
    assert not any("Example Co" in fact["text"] for fact in profile.facts)
    data["linkedin"] = "https://example.com/" + "x" * 250
    data["website"] = "https://example.com/" + "y" * 250
    response = authenticated.post(url, data)
    assert b"Combined contact details" in response.content
    assert ResumeProfile.objects.count() == 1


def test_role_description_keeps_employer_context():
    from jobs.services.profile_editor import confirmed_sections

    form = ResumeProfileForm({"name": "Example", "confirmed": True})
    assert form.is_valid()
    sections = confirmed_sections(
        form.cleaned_data,
        {
            "experience": [
                {
                    "company": "Example Co",
                    "description": "Built services.\n\nImproved tests.",
                    "current": True,
                }
            ],
            "education": [],
            "projects": [],
        },
    )
    assert "\n\n" not in sections["Experience"]
    assert "Status: Present" in sections["Experience"]
