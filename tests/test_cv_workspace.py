import pytest
from django.core.exceptions import ValidationError
from django.test import Client

from jobs.models import Document, PreparedResume
from jobs.services import cv_workspace, resumes
from jobs.services.documents import base_documents, current_base, discard_generated_document
from tests.test_batches import document  # noqa: F401

pytestmark = pytest.mark.django_db


def base_profile(user, document):  # noqa: F811
    return resumes.confirm(
        user,
        document,  # noqa: F811
        "Example Candidate",
        "candidate@example.com | Israel",
        {
            "Skills": "Python, Linux",
            "Experience": "Engineer | Acme | 2020 - 2025\n• Built Python services.",
        },
        details={"basics": {"email": "candidate@example.com"}},
    )


def test_base_list_hides_generated_and_old_uploads(user, document, job):  # noqa: F811
    p = base_profile(user, document)
    derived, _, _ = resumes.tailor(
        p, {"title": job.title, "company": job.company, "description": job.description}
    )
    assert list(base_documents(user)) == [document]
    assert current_base(user) == document and derived.pk != document.pk


def test_regeneration_replaces_only_current_draft_and_cleans_old_output(
    user,
    document,  # noqa: F811
    application,
    connection,  # noqa: F811
):  # noqa: F811
    base_profile(user, document)
    first = cv_workspace.generate(user, application.pk).document
    # An owner edit creates a changed final draft, not another selectable base CV.
    initial = cv_workspace.editor_initial(user, first)
    initial["summary"] = "Python engineer focused on reliable services."
    application.refresh_from_db()
    edited = cv_workspace.save_editor(user, first, initial, application=application)
    assert not Document.objects.filter(pk=first.pk).exists()
    assert current_base(user) == document
    assert "reliable services" in resumes.extract(edited)
    regenerated = cv_workspace.generate(user, application.pk)
    assert regenerated.document_id != edited.pk
    assert not Document.objects.filter(pk=edited.pk).exists()
    assert regenerated.status == "draft" and not regenerated.approval_digest
    assert PreparedResume.objects.filter(owner=user).count() == 1


def test_base_edit_changes_future_base_not_application_attachment(
    user,
    document,  # noqa: F811
    application,
    connection,  # noqa: F811
):  # noqa: F811
    base_profile(user, document)
    application = cv_workspace.generate(user, application.pk)
    sent_bytes = resumes.extract(application.document)
    initial = cv_workspace.editor_initial(user, document)
    initial["skills"] = "Python, Linux, Go"
    updated = cv_workspace.save_editor(user, document, initial)
    assert current_base(user) == updated and updated.pk != document.pk
    application.refresh_from_db()
    assert resumes.extract(application.document) == sent_bytes
    assert "Go" in resumes.extract(updated)


@pytest.mark.parametrize("status", ["submitting", "confirmed", "unknown"])
def test_locked_attachment_is_preserved(user, document, application, connection, status):  # noqa: F811
    base_profile(user, document)
    application = cv_workspace.generate(user, application.pk)
    application.status = status
    application.save()
    initial = cv_workspace.editor_initial(user, application.document)
    with pytest.raises(ValidationError):
        cv_workspace.save_editor(user, application.document, initial, application=application)
    assert not discard_generated_document(user, application.document_id)


def test_cv_ui_has_one_base_and_no_profile_picker(
    authenticated,
    user,
    document,  # noqa: F811
    application,
    connection,  # noqa: F811
):  # noqa: F811
    base_profile(user, document)
    cv_workspace.generate(user, application.pk)
    response = authenticated.get("/documents/")
    assert b"Your base CV" in response.content and b"Uploaded files" not in response.content
    assert response.content.count(b"Edit base CV") == 1
    response = authenticated.get(f"/applications/{application.pk}/")
    assert (
        b"Edit application CV" in response.content
        and b"Reviewed factual profile:" not in response.content
    )
    assert (
        connection.email.encode()
        in authenticated.get(f"/applications/{application.pk}/cv/edit/").content
    )


def test_application_editor_owner_csrf_and_sent_state(
    authenticated,
    user,
    other,
    document,  # noqa: F811
    application,
    connection,  # noqa: F811
):  # noqa: F811
    base_profile(user, document)
    app = cv_workspace.generate(user, application.pk)
    url = f"/applications/{application.pk}/cv/edit/"
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(user)
    assert csrf.post(url, {}).status_code == 403
    app.owner = other
    app.save(update_fields=["owner"])
    assert authenticated.get(url).status_code == 404
    app.owner = user
    app.status = "confirmed"
    app.save()
    assert authenticated.get(url).status_code == 409


def test_simple_base_editor_saves_editable_pdf(authenticated, user, document, connection):  # noqa: F811
    base_profile(user, document)
    response = authenticated.get("/resume/edit/")
    assert response.status_code == 200
    assert (
        b"experience-TOTAL_FORMS" not in response.content
        and b"Every save creates" not in response.content
    )
    values = cv_workspace.editor_initial(user, document)
    values["skills"] = "Python, Linux, Go"
    values["simple_cv"] = "1"
    assert authenticated.post("/resume/edit/", values).status_code == 302
    base = current_base(user)
    assert "Python, Linux, Go" in resumes.extract(base)
    assert len(list(base_documents(user))) == 1


def test_referenced_cv_is_never_garbage_collected(user, document, application, connection):  # noqa: F811
    base_profile(user, document)
    app = cv_workspace.generate(user, application.pk)
    assert not discard_generated_document(user, app.document_id)
    assert Document.objects.filter(pk=app.document_id).exists()


@pytest.mark.browser
@pytest.mark.django_db(transaction=True)
def test_simple_base_and_application_editors_in_browser(
    page,
    live_server,
    settings,
    user,
    document,  # noqa: F811
    application,
    connection,  # noqa: F811
):
    from playwright.sync_api import expect

    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    base_profile(user, document)
    cv_workspace.generate(user, application.pk)
    page.goto(live_server.url + "/account/login/")
    page.get_by_label("Username").fill(user.username)
    page.get_by_label("Password").fill("a-long-test-password-123")
    page.get_by_role("button", name="Next", exact=True).click()
    page.goto(live_server.url + "/documents/")
    expect(page.get_by_role("heading", name="Your base CV", exact=True)).to_be_visible()
    expect(page.get_by_text("Uploaded files", exact=True)).to_have_count(0)
    page.get_by_role("link", name="Edit base CV", exact=True).click()
    page.get_by_label("Skills:", exact=True).fill("Python, Linux, Go")
    page.get_by_role("button", name="Save CV", exact=True).click()
    expect(page.get_by_role("heading", name="Your base CV", exact=True)).to_be_visible()
    page.goto(live_server.url + f"/applications/{application.pk}/")
    expect(page.locator('select[name="profile"]')).to_have_count(0)
    page.get_by_role("link", name="Edit application CV", exact=True).click()
    page.get_by_label("Summary:", exact=True).fill("Python engineer focused on reliability.")
    page.get_by_role("button", name="Save CV", exact=True).click()
    expect(
        page.get_by_text("CV saved. Review the updated PDF before sending.", exact=True)
    ).to_be_visible()
    application.refresh_from_db()
    assert application.status == "draft" and application.send_attempts == 0
    assert "reliability" in resumes.extract(application.document)
    assert "reliability" not in resumes.extract(current_base(user))
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
