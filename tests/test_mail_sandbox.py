"""Synthetic-only transport and an opt-in real Mailpit capture journey."""

import os
from email import policy
from email.parser import BytesParser
from unittest.mock import Mock

import httpx
import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command

from jobs.models import Application, Document, GmailConnection, Job
from jobs.services import gmail, sandbox_mail, submissions
from jobs.tasks import send_application


@pytest.fixture
def sandbox(settings, tmp_path):
    settings.DEBUG = True
    settings.SANDBOX_MAIL_ENABLED = True
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    settings.MEDIA_ROOT = tmp_path
    call_command("bootstrap_mail_sandbox")
    owner = get_user_model().objects.get(username=sandbox_mail.USERNAME)
    return (
        owner,
        Job.objects.get(canonical_url=sandbox_mail.JOB_URL),
        Document.objects.get(owner=owner, name="SYNTHETIC_TEST_CV.pdf"),
    )


@pytest.mark.django_db
def test_bootstrap_is_opt_in_and_idempotent(settings, sandbox):
    owner, job, document = sandbox
    call_command("bootstrap_mail_sandbox")
    assert Document.objects.filter(owner=owner).count() == 1
    assert not owner.has_usable_password()
    assert not owner.is_staff
    assert not Application.objects.filter(owner=owner).exists()
    assert document.owner == owner
    assert job.apply_email == sandbox_mail.RECIPIENT
    settings.SANDBOX_MAIL_ENABLED = False
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("bootstrap_mail_sandbox")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "change", ["disabled", "production", "recipient", "job", "portal", "marker", "staff"]
)
def test_synthetic_delivery_fails_closed(settings, sandbox, monkeypatch, change):
    owner, job, document = sandbox
    connection = GmailConnection.objects.get(owner=owner)
    app = Application.objects.create(
        owner=owner,
        job=job,
        document=document,
        recipient=sandbox_mail.RECIPIENT,
        subject="Synthetic test",
        body="Synthetic application only.",
    )
    if change == "disabled":
        settings.SANDBOX_MAIL_ENABLED = False
    elif change == "production":
        settings.DEBUG = False
    elif change == "recipient":
        app.recipient = "real@example.com"
    elif change == "job":
        job.canonical_url = "https://example.com/real-job"
        job.save()
    elif change == "portal":
        app.channel = "portal"
    elif change == "marker":
        connection.encrypted_credentials = "corrupt"
        connection.save()
    else:
        owner.is_staff = True
        owner.save()
    app.save()
    monkeypatch.setattr(gmail, "gmail_client", lambda *a: pytest.fail("real Gmail called"))
    monkeypatch.setattr(sandbox_mail.smtplib, "SMTP", lambda *a, **k: pytest.fail("SMTP called"))
    with pytest.raises(ValidationError):
        submissions.approve(owner, app.pk)
    with pytest.raises(ValidationError):
        gmail.send(connection, app, None)


@pytest.mark.django_db(transaction=True)
def test_synthetic_approval_and_duplicate_worker(sandbox, monkeypatch, settings):
    owner, job, document = sandbox
    app = Application.objects.create(
        owner=owner,
        job=job,
        document=document,
        recipient=sandbox_mail.RECIPIENT,
        subject="Synthetic test",
        body="Synthetic application only.",
    )
    smtp = Mock()
    smtp.return_value.__enter__ = Mock(return_value=smtp)
    smtp.return_value.__exit__ = Mock(return_value=False)
    smtp.sendmail.return_value = {}
    monkeypatch.setattr(sandbox_mail.smtplib, "SMTP", smtp)
    monkeypatch.setattr(sandbox_mail, "find_sent", lambda *a: "mailpit:test-receipt")
    monkeypatch.setattr(gmail, "gmail_client", lambda *a: pytest.fail("real Gmail called"))
    send_application.run(app.pk)
    assert not smtp.called  # A draft cannot send.
    submissions.approve(owner, app.pk)
    send_application.run(app.pk)
    send_application.run(app.pk)
    app.refresh_from_db()
    assert app.status == Application.Status.CONFIRMED
    assert app.provider_id == "mailpit:test-receipt"
    assert app.send_attempts == 1
    smtp.sendmail.assert_called_once()
    raw = smtp.sendmail.call_args.args[2]
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    assert parsed["From"] == sandbox_mail.SENDER
    assert parsed["To"] == sandbox_mail.RECIPIENT
    assert list(parsed.iter_attachments())[0].get_payload(
        decode=True
    ) == submissions.attachment_bytes(app)


@pytest.mark.django_db(transaction=True)
def test_uncertain_sink_receipt_is_not_resent(sandbox, monkeypatch):
    owner, job, document = sandbox
    app = Application.objects.create(
        owner=owner,
        job=job,
        document=document,
        recipient=sandbox_mail.RECIPIENT,
        subject="Synthetic test",
        body="Synthetic application only.",
    )
    send = Mock(side_effect=TimeoutError)
    monkeypatch.setattr(sandbox_mail, "send", send)
    submissions.approve(owner, app.pk)
    send_application.run(app.pk)
    send_application.run(app.pk)
    app.refresh_from_db()
    assert app.status == Application.Status.UNKNOWN
    send.assert_called_once()


@pytest.mark.django_db
@pytest.mark.parametrize("problem", ["absent", "duplicate", "wrong_header", "unsafe_id"])
def test_receipt_requires_one_exact_message(sandbox, monkeypatch, problem):
    owner, _, _ = sandbox
    connection = GmailConnection.objects.get(owner=owner)
    message_id = "<synthetic@applications.jobstream.invalid>"
    identity = "safeReceipt123" if problem != "unsafe_id" else "../foreign"
    messages = [{"ID": identity}]
    if problem == "absent":
        messages = []
    elif problem == "duplicate":
        messages *= 2
    raw = (
        f"Message-ID: {'<other@invalid>' if problem == 'wrong_header' else message_id}\r\n"
        f"From: {sandbox_mail.SENDER}\r\nTo: {sandbox_mail.RECIPIENT}\r\n\r\nSynthetic."
    ).encode()
    monkeypatch.setattr(
        sandbox_mail,
        "api_get",
        lambda path, **kwargs: (
            httpx.Response(200, json={"messages": messages})
            if path == "search"
            else httpx.Response(200, content=raw)
        ),
    )
    if problem == "unsafe_id":
        with pytest.raises(ValueError, match="receipt ID"):
            sandbox_mail.find_sent(connection, message_id)
    else:
        assert sandbox_mail.find_sent(connection, message_id) is None


@pytest.mark.django_db(transaction=True)
def test_disabled_after_approval_cannot_send(sandbox, monkeypatch, settings):
    owner, job, document = sandbox
    app = Application.objects.create(
        owner=owner,
        job=job,
        document=document,
        recipient=sandbox_mail.RECIPIENT,
        subject="Synthetic test",
        body="Synthetic application only.",
    )
    submissions.approve(owner, app.pk)
    settings.SANDBOX_MAIL_ENABLED = False
    monkeypatch.setattr(gmail, "gmail_client", lambda *a: pytest.fail("real Gmail called"))
    monkeypatch.setattr(sandbox_mail.smtplib, "SMTP", lambda *a, **k: pytest.fail("SMTP called"))
    send_application.run(app.pk)
    app.refresh_from_db()
    assert app.status == Application.Status.FAILED
    assert app.error_code == "preflight_failed"


@pytest.mark.browser
@pytest.mark.integration
@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(os.getenv("MAILPIT_INTEGRATION") != "1", reason="Requires local Mailpit")
def test_browser_application_reaches_real_mailpit(
    page, live_server, sandbox, settings, confirmed_base_cv
):
    from playwright.sync_api import expect

    owner, job, document = sandbox
    confirmed_base_cv(owner, document)
    owner.set_password("synthetic-browser-test-8291")
    owner.save()
    page.goto(live_server.url + "/account/login/")
    page.get_by_label("Username").fill(owner.username)
    page.get_by_label("Password").fill("synthetic-browser-test-8291")
    page.get_by_role("button", name="Next").click()
    page.goto(live_server.url + f"/jobs/{job.pk}/")
    page.get_by_role("link", name="Prepare application").click()
    page.get_by_label("Recipient").fill(sandbox_mail.RECIPIENT)
    page.get_by_label("Message to employer").fill(
        "SYNTHETIC TEST ONLY — please inspect the attached fake CV."
    )
    page.get_by_role("button", name="Prepare CV and application").click()
    expect(page.get_by_text("Prepared", exact=True)).to_be_visible()
    app = Application.objects.get(owner=owner, job=job)
    page.get_by_role("button", name="Approve and send this application").click()
    app.refresh_from_db()
    assert app.status == Application.Status.APPROVED
    # Execute the actual worker task body synchronously: no live queue/scheduler needed.
    send_application.run(app.pk)
    send_application.run(app.pk)
    app.refresh_from_db()
    assert app.status == Application.Status.CONFIRMED
    assert app.send_attempts == 1
    receipt = sandbox_mail.find_sent(GmailConnection.objects.get(owner=owner), app.message_id)
    assert receipt == app.provider_id
    identity = receipt.removeprefix("mailpit:")
    parsed = BytesParser(policy=policy.default).parsebytes(
        sandbox_mail.api_get(f"message/{identity}/raw").content
    )
    assert parsed["Subject"] == app.subject
    assert (
        parsed.get_body(preferencelist=("plain",)).get_content().replace("\r\n", "\n").strip()
        == app.body
    )
    assert list(parsed.iter_attachments())[0].get_payload(
        decode=True
    ) == submissions.attachment_bytes(app)
    page.reload()
    expect(page.get_by_text("Confirmed sent", exact=True)).to_be_visible()
