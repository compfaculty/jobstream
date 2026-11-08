from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient

from jobs.models import Application, Invitation, SavedJob

pytestmark = pytest.mark.django_db


def test_auth_required(client, job):
    assert client.get("/").status_code == 302
    assert client.get("/api/v1/jobs/").status_code == 403
    assert client.get("/health/").json() == {"status": "ok"}


def test_search_and_escape(authenticated, job):
    job.title = "<script>alert(1)</script> Python"
    job.save()
    response = authenticated.get("/", {"q": "Python", "location": "Tel Aviv"})
    assert response.status_code == 200
    assert b"&lt;script&gt;" in response.content
    assert b"<script>alert" not in response.content
    assert authenticated.get("/", {"q": "missing"}).context["page"].paginator.count == 0
    assert authenticated.get("/", HTTP_HX_REQUEST="true").status_code == 200


def test_private_views(authenticated, other, application):
    application.owner = other
    application.save()
    for suffix in ("", "edit/"):
        assert authenticated.get(f"/applications/{application.pk}/{suffix}").status_code == 404
    assert authenticated.post(f"/applications/{application.pk}/approve/").status_code == 404
    assert authenticated.get("/api/v1/applications/").json()["count"] == 0
    assert authenticated.get(f"/api/v1/applications/{application.pk}/").status_code == 404


def test_csrf(user, application, connection):
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(user)
    assert client.post(f"/applications/{application.pk}/approve/").status_code == 403
    assert client.post(f"/api/v1/applications/{application.pk}/approve/").status_code == 403


def test_save_create_edit_track(authenticated, user, job, connection):
    url = f"/jobs/{job.pk}/"
    assert authenticated.get(url).status_code == 200
    authenticated.post(url + "save/")
    assert SavedJob.objects.filter(owner=user, job=job).exists()
    authenticated.post(url + "save/")
    assert not SavedJob.objects.filter(owner=user, job=job).exists()
    values = {"recipient": "hr@example.com", "subject": "Application", "body": "Hello"}
    assert authenticated.post(url + "apply/", values).status_code == 302
    app = Application.objects.get(owner=user, job=job)
    assert authenticated.get(f"/applications/{app.pk}/").status_code == 200
    assert authenticated.post(f"/applications/{app.pk}/approve/").status_code == 302
    assert (
        authenticated.post(
            f"/applications/{app.pk}/edit/", {**values, "body": "Updated"}
        ).status_code
        == 302
    )
    authenticated.post(
        f"/applications/{app.pk}/tracking/", {"tracking_status": "interview", "notes": "Private"}
    )
    app.refresh_from_db()
    assert app.status == "draft"
    assert app.tracking_status == "interview"


@pytest.mark.django_db(transaction=True)
def test_upload_is_private(authenticated, user, other, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    bad = SimpleUploadedFile("bad.pdf", b"not a pdf", content_type="application/pdf")
    assert b"valid PDF" in authenticated.post("/documents/", {"file": bad}).content
    pdf = SimpleUploadedFile(
        "resume.pdf", b"%PDF-1.4\nresume\n%%EOF", content_type="application/pdf"
    )
    assert authenticated.post("/documents/", {"file": pdf}).status_code == 302
    doc = user.document_set.get()
    response = authenticated.get(f"/documents/{doc.pk}/")
    assert response.status_code == 200
    assert "attachment" in response["Content-Disposition"]
    response.close()
    authenticated.force_login(other)
    assert authenticated.get(f"/documents/{doc.pk}/").status_code == 404


def test_invitation(client):
    invitation = Invitation.objects.create(
        email="invitee@example.com", expires_at=timezone.now() + timedelta(days=1)
    )
    url = f"/invite/{invitation.token}/"
    assert client.get(url).status_code == 200
    assert (
        client.post(
            url,
            {
                "username": "newperson",
                "password1": "Strong-test-password-993",
                "password2": "Strong-test-password-993",
            },
        ).status_code
        == 302
    )
    invitation.refresh_from_db()
    assert invitation.used_at
    assert client.get(url).status_code == 404


def test_staff_mfa_gate(authenticated, user):
    user.is_staff = True
    user.save()
    assert authenticated.get("/").url == "/account/two_factor/setup/"
    assert authenticated.get("/admin/").status_code == 302
    assert authenticated.get("/account/two_factor/setup/").status_code == 200


def test_oauth_state_and_disconnect(authenticated, connection):
    assert authenticated.get("/gmail/callback/?state=wrong&code=bad").status_code == 400
    assert authenticated.get("/gmail/").status_code == 200
    authenticated.post("/gmail/disconnect/")
    connection.refresh_from_db()
    assert connection.revoked and not connection.encrypted_credentials


def test_schema(authenticated):
    assert authenticated.get("/api/v1/schema/").status_code == 200


def test_oauth_start_and_callback(authenticated, user, settings, monkeypatch):
    from unittest.mock import Mock

    from jobs import views
    from jobs.models import GmailConnection

    settings.GOOGLE_CLIENT_ID = "test-client"
    settings.GOOGLE_CLIENT_SECRET = "test-client-secret"
    settings.TOKEN_ENCRYPTION_KEY = "test-key"
    flow = Mock(code_verifier="verifier")
    flow.authorization_url.return_value = ("https://accounts.google.com/test", "state-value")
    flow.credentials = Mock(refresh_token="test-refresh", granted_scopes=settings.GOOGLE_SCOPES)
    monkeypatch.setattr(views, "oauth_flow", Mock(return_value=flow))
    monkeypatch.setattr(views, "encrypt_credentials", lambda credentials: "encrypted-test-value")
    client = Mock()
    client.users.return_value.getProfile.return_value.execute.return_value = {
        "emailAddress": "alice@example.com"
    }
    monkeypatch.setattr(views, "gmail_client", lambda connection: client)
    assert authenticated.post("/gmail/connect/").url == "https://accounts.google.com/test"
    assert (
        authenticated.get(
            "/gmail/callback/", {"state": "state-value", "code": "one-time-code"}
        ).status_code
        == 302
    )
    assert GmailConnection.objects.get(owner=user).email == "alice@example.com"
    assert (
        authenticated.get(
            "/gmail/callback/", {"state": "state-value", "code": "one-time-code"}
        ).status_code
        == 400
    )


def test_api_approval_and_private_doc_selection(authenticated, application, connection, other):
    from jobs.models import Document

    doc = Document.objects.create(
        owner=other, name="Other CV", file="other.pdf", sha256="abc", size=3
    )
    values = {
        "recipient": "hr@example.com",
        "subject": "Application",
        "body": "Hello",
        "document": doc.pk,
    }
    response = authenticated.post(f"/applications/{application.pk}/edit/", values)
    assert response.status_code == 200
    application.refresh_from_db()
    assert application.document_id is None
    url = f"/api/v1/applications/{application.pk}/approve/"
    assert authenticated.post(url).status_code == 200
    assert authenticated.post(url).status_code == 400


def test_activity_and_batches_are_owner_scoped(authenticated, user, other, application):
    from jobs.models import AuditEvent

    AuditEvent.objects.create(actor=user, application=application, event="application.approved")
    AuditEvent.objects.create(actor=other, event="document.uploaded")
    response = authenticated.get("/activity/")
    assert response.status_code == 200
    assert b"Approved for sending" in response.content
    assert b"Base CV uploaded" not in response.content
    assert authenticated.get("/batches/").status_code == 200


def test_evaluation_controls_visible_before_description_without_profile(
    authenticated, job, settings
):
    from bs4 import BeautifulSoup

    settings.AGENT_BACKEND = "infinity"
    response = authenticated.get(f"/jobs/{job.pk}/")
    soup = BeautifulSoup(response.content, "html.parser")
    panel = soup.select_one("#vacancy-evaluation")
    assert panel.find("h2").text == "Evaluate vacancy"
    assert panel.find("button", string="Evaluate").has_attr("disabled")
    assert panel.find("a", string="Upload a CV")["href"] == "/documents/"
    html = response.content.decode()
    assert html.index('id="vacancy-evaluation"') < html.index("Original description")


def test_evaluation_controls_with_profile(authenticated, job, evaluation_profile, settings):
    from bs4 import BeautifulSoup

    settings.AGENT_BACKEND = "infinity"
    response = authenticated.get(f"/jobs/{job.pk}/")
    panel = BeautifulSoup(response.content, "html.parser").select_one("#vacancy-evaluation")
    assert not panel.find("button", string="Evaluate").has_attr("disabled")
    assert panel.find("option", value=f"profile:{evaluation_profile.pk}") is not None
    assert panel.find("form")["action"] == f"/jobs/{job.pk}/evaluate/"


def test_disabled_evaluation_is_explained(authenticated, job, settings):
    settings.AGENT_BACKEND = "disabled"
    response = authenticated.get(f"/jobs/{job.pk}/")
    assert b"Evaluate vacancy" in response.content
    assert b"Vacancy evaluation is currently unavailable" in response.content


def test_delete_resume(authenticated, user, settings, tmp_path, django_capture_on_commit_callbacks):
    from jobs.models import Document

    settings.MEDIA_ROOT = tmp_path
    document = Document.objects.create(
        owner=user,
        name="Resume",
        sha256="a" * 64,
        size=3,
        file=SimpleUploadedFile("resume.pdf", b"pdf"),
    )
    path = document.file.path
    url = f"/documents/{document.pk}/delete/"
    assert url.encode() in authenticated.get("/documents/").content
    assert authenticated.get(url).status_code == 200
    assert Document.objects.filter(pk=document.pk).exists()
    with django_capture_on_commit_callbacks(execute=True):
        assert authenticated.post(url).status_code == 302
    assert not Document.objects.filter(pk=document.pk).exists()
    assert not document.file.storage.exists(path)
    assert authenticated.post(url).status_code == 404


def test_delete_resume_permissions(authenticated, client, user, other, evaluation_profile):
    from django.test import Client

    document = evaluation_profile.document
    url = f"/documents/{document.pk}/delete/"
    assert client.post(url).status_code == 302
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(user)
    assert csrf_client.post(url).status_code == 403
    assert authenticated.put(url).status_code == 405
    authenticated.force_login(other)
    assert authenticated.get(url).status_code == 404
    assert authenticated.post(url).status_code == 404
    document.refresh_from_db()


@pytest.mark.parametrize("reference", ["profile", "application"])
def test_delete_referenced_resume(authenticated, evaluation_profile, application, reference):
    document = evaluation_profile.document
    if reference == "application":
        evaluation_profile.delete()
        application.document = document
        application.save()
    response = authenticated.post(f"/documents/{document.pk}/delete/", follow=True)
    assert response.status_code == 200
    assert b"cannot be deleted" in response.content
    document.refresh_from_db()


def test_job_cards_show_only_required_stack(authenticated, job):
    job.normalized = {
        "required_technologies": ["Python", "Django", "PostgreSQL"],
        "technologies": ["Python", "Django", "PostgreSQL", "OptionalTech"],
    }
    job.save()
    response = authenticated.get("/")
    assert b'aria-label="Required technologies"' in response.content
    assert b"<li>Python</li>" in response.content
    assert b"OptionalTech" not in response.content
    job.normalized = {}
    job.save()
    assert b'aria-label="Required technologies"' not in authenticated.get("/").content
