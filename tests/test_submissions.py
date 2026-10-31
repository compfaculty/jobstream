import hashlib
from unittest.mock import Mock

import httplib2
import pytest
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.http import Http404
from googleapiclient.errors import HttpError

from jobs.models import Application, AuditEvent, Document, GmailConnection
from jobs.services import gmail
from jobs.services import submissions as service
from jobs.services.authorization import owned_application, owned_document

pytestmark = pytest.mark.django_db


def test_ownership(user, other, application, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    assert owned_application(user, application.pk) == application
    with pytest.raises(Http404):
        owned_application(other, application.pk)
    document = Document.objects.create(
        owner=user, name="CV", file=ContentFile(b"pdf", name="cv.pdf"), sha256="abc", size=3
    )
    assert owned_document(user, document.pk) == document
    with pytest.raises(Http404):
        owned_document(other, document.pk)


def test_approve_requires_connection(user, application):
    with pytest.raises(ValidationError):
        service.approve(user, application.pk)


def test_approval_and_duplicate_task(user, application, connection, monkeypatch):
    service.approve(user, application.pk)
    application.refresh_from_db()
    assert application.approval_digest == service.digest(application, connection.email)
    send = Mock(return_value="gmail-123")
    monkeypatch.setattr(gmail, "send", send)
    service.submit(application.pk)
    service.submit(application.pk)
    application.refresh_from_db()
    assert application.status == "confirmed"
    assert application.provider_id == "gmail-123"
    assert send.call_count == 1
    assert AuditEvent.objects.filter(
        application=application, event="application.confirmed"
    ).exists()
    with pytest.raises(ValidationError):
        service.approve(user, application.pk)


def test_edit_invalidates_approval(user, application, connection):
    service.approve(user, application.pk)
    result = service.edit(user, application.pk, {"body": "Changed"})
    assert result.status == "draft"
    assert not result.approval_digest
    assert result.approved_at is None


@pytest.mark.parametrize("status", ["submitting", "unknown", "confirmed"])
def test_edit_locked(user, application, status):
    application.status = status
    application.save()
    with pytest.raises(ValidationError):
        service.edit(user, application.pk, {"body": "Changed"})


@pytest.mark.parametrize(
    "changes", [{"recipient": "bad"}, {"subject": "a\nb"}, {"subject": ""}, {"body": " "}]
)
def test_content_validation(user, application, connection, changes):
    for key, value in changes.items():
        setattr(application, key, value)
    with pytest.raises(ValidationError):
        service.validate_content(application)


def test_cross_user_attachment(user, other, application):
    doc = Document.objects.create(owner=other, name="CV", file="other.pdf", sha256="abc", size=3)
    application.document = doc
    with pytest.raises(ValidationError):
        service.validate_content(application)


@pytest.mark.parametrize("mutation", ["body", "sender", "disconnect"])
def test_preflight_drift(user, application, connection, mutation, monkeypatch):
    service.approve(user, application.pk)
    if mutation == "body":
        Application.objects.filter(pk=application.pk).update(body="Changed out of band")
    elif mutation == "sender":
        GmailConnection.objects.filter(pk=connection.pk).update(email="different@example.com")
    else:
        connection.delete()
    send = Mock()
    monkeypatch.setattr(gmail, "send", send)
    service.submit(application.pk)
    application.refresh_from_db()
    assert application.status == "failed"
    send.assert_not_called()


def test_attachment_integrity(user, application, connection, settings, tmp_path, monkeypatch):
    settings.MEDIA_ROOT = tmp_path
    content = b"%PDF-1.4\noriginal\n%%EOF"
    doc = Document.objects.create(
        owner=user,
        name="CV",
        file=ContentFile(content, name="cv.pdf"),
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
    )
    application.document = doc
    application.save()
    assert service.attachment_bytes(application) == content
    service.approve(user, application.pk)
    with doc.file.open("wb") as stream:
        stream.write(b"tampered")
    send = Mock()
    monkeypatch.setattr(gmail, "send", send)
    service.submit(application.pk)
    application.refresh_from_db()
    assert application.status == "failed"
    send.assert_not_called()


@pytest.mark.parametrize(
    "error,status",
    [
        (TimeoutError(), "unknown"),
        (gmail.ReconnectRequired(), "failed"),
        (HttpError(httplib2.Response({"status": "429"}), b"{}"), "approved"),
        (HttpError(httplib2.Response({"status": "503"}), b"{}"), "unknown"),
    ],
)
def test_transport_failures(user, application, connection, monkeypatch, error, status):
    service.approve(user, application.pk)
    send = Mock(side_effect=error)
    monkeypatch.setattr(gmail, "send", send)
    service.submit(application.pk)
    service.submit(application.pk)
    application.refresh_from_db()
    assert application.status == status
    assert send.call_count == 1
    if status == "unknown":
        with pytest.raises(ValidationError):
            service.approve(user, application.pk)


def test_reconcile(user, application, connection, monkeypatch):
    service.reconcile(application.pk)  # Drafts are never reconciled.
    application.status = "unknown"
    application.save()
    find = Mock(return_value=None)
    monkeypatch.setattr(gmail, "find_sent", find)
    service.reconcile(application.pk)
    application.refresh_from_db()
    assert application.status == "unknown"
    find.side_effect = TimeoutError()
    service.reconcile(application.pk)
    find.side_effect = None
    find.return_value = "found-id"
    service.reconcile(application.pk)
    application.refresh_from_db()
    assert application.status == "confirmed"
    service.finish(application.pk, "unknown")
    application.refresh_from_db()
    assert application.status == "confirmed"


def test_missing_attachment_fails_safely(application, connection, user, monkeypatch):
    doc = Document.objects.create(
        owner=user, name="missing", file="missing.pdf", sha256="abc", size=3
    )
    application.document = doc
    application.save()
    service.approve(user, application.pk)
    monkeypatch.setattr(service, "attachment_bytes", Mock(side_effect=OSError()))
    service.submit(application.pk)
    application.refresh_from_db()
    assert application.error_code == "preflight_failed"
