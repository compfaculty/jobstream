import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.core.exceptions import ValidationError

from jobs.services import secret_telaviv as sta
from jobs.services import submissions
from jobs.services.portals import inspect_secret
from tests.test_portals import FORM, URL

ANSWERS = {
    "first_name": "Test",
    "last_name": "Applicant",
    "email": "test@example.invalid",
    "phone": "0000000000",
    "agree_terms": True,
    "forward_headhunters": False,
    "terms_url": sta.TERMS_URL,
}
HTML = FORM.replace(b"https://www.secrettelaviv.com/terms/", sta.TERMS_URL.encode()).replace(
    b'value="not-logged"', b'value="apply"'
)
PDF = b"%PDF-1.7\nsynthetic CV\n%%EOF"


def draft():
    return SimpleNamespace(
        portal_url=URL,
        portal_answers=ANSWERS.copy(),
        portal_fingerprint=inspect_secret(HTML, URL).fingerprint,
    )


def test_native_multipart_upload_and_explicit_confirmation():
    session = Mock()
    session.request.side_effect = [
        sta.Response(200, {}, HTML),
        sta.Response(
            200, {}, b'<div class="wpjb-flash-info">Your application has been sent.</div>'
        ),
    ]
    assert sta.send(draft(), PDF, session=session).startswith("secret_telaviv:")
    assert session.request.call_count == 2
    args = session.request.call_args.args
    assert args[0] == "POST"
    assert PDF in args[2] and b'name="file"; filename="resume.pdf"' in args[2]
    assert b'name="headhunters[]"' not in args[2]
    assert b'name="similar_employers_cv[]"' in args[2]


def test_timeout_never_tries_another_file_field():
    session = Mock()
    session.request.side_effect = [sta.Response(200, {}, HTML), TimeoutError()]
    with pytest.raises(TimeoutError):
        sta.send(draft(), PDF, session=session)
    assert session.request.call_count == 2


def test_no_confirmation_is_not_success():
    with pytest.raises(sta.PortalUnknown):
        sta.confirmation(sta.Response(200, {}, b"<p>Thank you for visiting. Applied jobs.</p>"))
    with pytest.raises(sta.PortalRejected):
        sta.confirmation(sta.Response(200, {}, b'<div class="wpjb-flash-error">CV required</div>'))


def test_form_change_stops_before_post():
    session = Mock()
    session.request.return_value = sta.Response(
        200, {}, HTML.replace(b'value="123"', b'value="456"')
    )
    with pytest.raises(sta.PortalRejected, match="form_changed"):
        sta.send(draft(), PDF, session=session)
    session.request.assert_called_once()


@pytest.mark.parametrize("status", ["get", "post"])
def test_explicit_429_carries_cooldown(status):
    session = Mock()
    limited = sta.Response(429, {"retry-after": "600"}, b"")
    session.request.side_effect = (
        [limited] if status == "get" else [sta.Response(200, {}, HTML), limited]
    )
    with pytest.raises(sta.PortalRateLimited) as error:
        sta.send(draft(), PDF, session=session)
    assert error.value.retry_after == "600"


@pytest.mark.django_db
def test_portal_approval_delivery_without_gmail(user, job, tmp_path, settings, monkeypatch):
    from django.core.files.base import ContentFile

    from jobs.models import Application, Document

    settings.MEDIA_ROOT = tmp_path
    job.apply_url = job.canonical_url = URL
    job.save()
    document = Document(
        owner=user, name="Test CV", size=len(PDF), sha256=hashlib.sha256(PDF).hexdigest()
    )
    document.file.save("cv.pdf", ContentFile(PDF), save=False)
    document.save()
    application = Application.objects.create(
        owner=user,
        job=job,
        channel="portal",
        portal_url=URL,
        portal_answers=ANSWERS,
        subject="Application",
        body="For review",
        document=document,
    )
    session = Mock()
    session.request.side_effect = [
        sta.Response(200, {}, HTML),
        sta.Response(200, {}, HTML),
        sta.Response(200, {}, b'<div class="wpjb-flash-info">Application sent</div>'),
    ]
    monkeypatch.setattr(sta, "SecretSession", lambda: session)
    submissions.approve(user, application.pk)
    submissions.submit(application.pk)
    submissions.submit(application.pk)
    application.refresh_from_db()
    assert application.status == "confirmed" and application.tracking_status == "applied"
    assert session.request.call_count == 3


@pytest.mark.django_db
def test_portal_answers_are_bound_to_approval(user, job, tmp_path, settings, monkeypatch):
    from django.core.files.base import ContentFile

    from jobs.models import Application, Document

    settings.MEDIA_ROOT = tmp_path
    job.apply_url = URL
    job.save()
    document = Document(
        owner=user, name="CV", size=len(PDF), sha256=hashlib.sha256(PDF).hexdigest()
    )
    document.file.save("cv.pdf", ContentFile(PDF), save=False)
    document.save()
    app = Application.objects.create(
        owner=user,
        job=job,
        channel="portal",
        portal_url=URL,
        portal_answers=ANSWERS,
        subject="Application",
        body="Review",
        document=document,
    )
    session = Mock()
    session.request.return_value = sta.Response(200, {}, HTML)
    monkeypatch.setattr(sta, "SecretSession", lambda: session)
    submissions.approve(user, app.pk)
    app.refresh_from_db()
    app.portal_answers = {**ANSWERS, "forward_headhunters": True}
    app.save()
    submissions.submit(app.pk)
    app.refresh_from_db()
    assert app.status == "failed"
    session.request.assert_called_once()


def test_contact_validation_requires_explicit_consent():
    with pytest.raises(ValidationError):
        sta.validate_answers({**ANSWERS, "agree_terms": False})
    with pytest.raises(ValidationError):
        sta.validate_answers({**ANSWERS, "forward_headhunters": "false"})


@pytest.mark.django_db
@pytest.mark.parametrize("outcome,expected", [("limited", "approved"), ("timeout", "unknown")])
def test_portal_retry_and_uncertain_outcomes(
    user, job, tmp_path, settings, monkeypatch, outcome, expected
):
    from django.core.files.base import ContentFile
    from django.utils import timezone

    from jobs.models import Application, Document

    settings.MEDIA_ROOT = tmp_path
    job.apply_url = URL
    job.save()
    document = Document(
        owner=user, name="CV", size=len(PDF), sha256=hashlib.sha256(PDF).hexdigest()
    )
    document.file.save("cv.pdf", ContentFile(PDF), save=False)
    document.save()
    app = Application.objects.create(
        owner=user,
        job=job,
        channel="portal",
        portal_url=URL,
        portal_answers=ANSWERS,
        subject="Application",
        body="Review",
        document=document,
    )
    session = Mock()
    final = (
        sta.Response(429, {"retry-after": "600"}, b"") if outcome == "limited" else TimeoutError()
    )
    session.request.side_effect = [sta.Response(200, {}, HTML), sta.Response(200, {}, HTML), final]
    monkeypatch.setattr(sta, "SecretSession", lambda: session)
    submissions.approve(user, app.pk)
    submissions.submit(app.pk)
    submissions.submit(app.pk)
    app.refresh_from_db()
    assert app.status == expected
    assert session.request.call_count == 3
    if outcome == "limited":
        assert app.retry_not_before > timezone.now()
        assert app.approval_digest
    else:
        with pytest.raises(ValidationError):
            submissions.approve(user, app.pk)
        submissions.reconcile(app.pk)
        assert session.request.call_count == 3


@pytest.mark.django_db
def test_single_job_portal_form_and_private_details(authenticated, user, job, tmp_path, settings):
    from django.core.files.base import ContentFile

    from jobs.models import Application, Document

    settings.MEDIA_ROOT = tmp_path
    job.apply_url = URL
    job.save()
    document = Document(
        owner=user, name="CV", size=len(PDF), sha256=hashlib.sha256(PDF).hexdigest()
    )
    document.file.save("cv.pdf", ContentFile(PDF), save=False)
    document.save()
    response = authenticated.post(
        f"/jobs/{job.pk}/apply/",
        {
            "subject": "Application",
            "body": "",
            "document": document.pk,
            "secret_first_name": "Test",
            "secret_last_name": "Applicant",
            "secret_email": "test@example.invalid",
            "secret_phone": "0000000000",
            "secret_agree_terms": "on",
        },
    )
    assert response.status_code == 302
    app = Application.objects.get(owner=user, job=job)
    assert app.channel == "portal"
    assert app.portal_answers["agree_terms"] is True
    assert app.portal_answers["forward_headhunters"] is False
    assert app.body
    assert b"Regenerate from base CV" in authenticated.get(f"/applications/{app.pk}/").content


def test_transport_keeps_cookies_and_pins_public_connections(monkeypatch):
    from email.message import Message

    headers = Message()
    headers.add_header("Set-Cookie", "STA_SESSION=test-session; Path=/; Secure; HttpOnly")
    response = Mock(status=200)
    response.info.return_value = headers
    response.getheaders.return_value = list(headers.items())
    response.read.return_value = HTML
    connection = Mock()
    connection.getresponse.return_value = response
    factory = Mock(return_value=connection)
    monkeypatch.setattr(sta, "PinnedHTTPSConnection", factory)
    monkeypatch.setattr(sta, "public_addresses", lambda host: ["93.184.216.34"])
    session = sta.SecretSession()
    session.request("GET", URL)
    session.request("POST", URL + "?form=apply", b"body", "multipart/form-data; boundary=test")
    assert factory.call_args.args == (sta.SECRET_HOST, "93.184.216.34")
    assert "STA_SESSION=test-session" in connection.request.call_args.kwargs["headers"]["Cookie"]
    with pytest.raises(ValueError):
        session.request("POST", "https://attacker.example/upload", b"private CV")


def test_live_receipt_markup_with_empty_error_placeholder():
    response = sta.Response(
        200,
        {},
        b'<div class="wpjb-flash-info">Your application has been sent.</div>'
        b'<span class="wpjb-error"></span>',
    )
    assert sta.confirmation(response, URL + "applied/117950/") == "secret_telaviv:117950"


def test_native_receipt_redirect_gets_confirmation_without_second_post():
    session = Mock()
    session.request.side_effect = [
        sta.Response(200, {}, HTML),
        sta.Response(302, {"location": URL + "applied/123/"}, b""),
        sta.Response(
            200, {}, b'<div class="wpjb-flash-info">Your application has been sent.</div>'
        ),
    ]
    assert sta.send(draft(), PDF, session=session) == "secret_telaviv:123"
    assert [call.args[0] for call in session.request.call_args_list] == ["GET", "POST", "GET"]


def test_receipt_timeout_preserves_url_for_read_only_reconciliation():
    session = Mock()
    receipt = URL + "applied/123/"
    session.request.side_effect = [
        sta.Response(200, {}, HTML),
        sta.Response(303, {"location": receipt}, b""),
        TimeoutError(),
    ]
    with pytest.raises(sta.PortalUnknown) as exc:
        sta.send(draft(), PDF, session=session)
    assert exc.value.receipt_url == receipt


@pytest.mark.django_db
def test_batch_propagates_applicant_details_and_preflights_in_worker(
    user, job, tmp_path, settings, monkeypatch
):
    import uuid

    from django.core.files.base import ContentFile

    from jobs.models import Document
    from jobs.services.batches import approve_items, create, prepare

    settings.MEDIA_ROOT = tmp_path
    job.apply_url = URL
    job.save()
    document = Document(
        owner=user, name="CV", size=len(PDF), sha256=hashlib.sha256(PDF).hexdigest()
    )
    document.file.save("cv.pdf", ContentFile(PDF), save=False)
    document.save()
    session = Mock()
    session.request.return_value = sta.Response(200, {}, HTML)
    monkeypatch.setattr(sta, "SecretSession", lambda: session)
    batch = create(user, document.pk, [job.pk], uuid.uuid4(), portal_answers=ANSWERS)
    prepare(batch.items.get().pk)
    item = batch.items.get()
    assert item.state == "ready" and item.application.portal_fingerprint
    assert item.application.portal_answers == ANSWERS
    approve_items(user, batch.pk, [item.pk])
    # Approval uses the prepared form snapshot without network calls under batch locks.
    session.request.assert_called_once()


def test_canonical_get_redirect_is_safe_but_another_vacancy_is_not():
    session = Mock()
    session.request.side_effect = [
        sta.Response(301, {"location": URL}, b""),
        sta.Response(200, {}, HTML),
    ]
    assert sta.read_form(session, URL.rstrip("/"))[0].fingerprint
    session.request.side_effect = [sta.Response(301, {"location": "/job/another/"}, b"")]
    with pytest.raises(sta.PortalRejected):
        sta.read_form(session, URL)


@pytest.mark.django_db
def test_rate_limit_pauses_other_preparation_without_fetch(
    user, job, tmp_path, settings, monkeypatch
):
    import uuid
    from datetime import timedelta

    from django.core.files.base import ContentFile
    from django.utils import timezone

    from jobs.models import Document
    from jobs.services.batches import create, prepare
    from jobs.services.throttles import defer_portal

    settings.MEDIA_ROOT = tmp_path
    job.apply_url = URL
    job.save()
    document = Document(
        owner=user, name="CV", size=len(PDF), sha256=hashlib.sha256(PDF).hexdigest()
    )
    document.file.save("cv.pdf", ContentFile(PDF), save=False)
    document.save()
    batch = create(user, document.pk, [job.pk], uuid.uuid4(), portal_answers=ANSWERS)
    defer_portal(timezone.now() + timedelta(minutes=10))
    session = Mock()
    monkeypatch.setattr(sta, "SecretSession", lambda: session)
    prepare(batch.items.get().pk)
    item = batch.items.get()
    assert item.state == "queued" and item.error_code == "portal_rate_limited"
    assert item.next_attempt_at > timezone.now() + timedelta(minutes=9)
    session.request.assert_not_called()


def test_malformed_form_is_a_safe_preflight_rejection():
    session = Mock()
    session.request.return_value = sta.Response(
        200, {}, HTML.replace(b"?form=apply#wpjb-scroll", b"http://attacker.example/upload")
    )
    with pytest.raises(sta.PortalRejected, match="portal_form_changed"):
        sta.read_form(session, URL)


@pytest.mark.parametrize(
    "notice",
    [
        "Your application has not been sent.",
        "Application failed to be submitted.",
        "You have not successfully applied.",
    ],
)
def test_negative_notice_cannot_confirm_delivery(notice):
    with pytest.raises(sta.PortalUnknown):
        sta.confirmation(
            sta.Response(200, {}, f'<div class="wpjb-flash-info">{notice}</div>'.encode())
        )


def test_job_description_cannot_forge_delivery_confirmation():
    with pytest.raises(sta.PortalUnknown):
        sta.confirmation(
            sta.Response(
                200,
                {},
                b'<div class="wpjb-text"><div class="wpjb-flash-info">Application sent</div></div>',
            )
        )


def ajax_html():
    import json

    from jobs.services.portals import UPLOAD_PARAMS, UPLOAD_URL

    config = {
        "url": UPLOAD_URL,
        "file_data_name": "file",
        "browse_button": "wpjb-upload-browse-file",
        "container": "wpjb-upload-container-file",
        "multipart": True,
        "multipart_params": UPLOAD_PARAMS,
    }
    return HTML + ("<script>WPJB.upload.init(" + json.dumps(config) + ");</script>").encode()


def test_session_upload_precedes_application_and_uses_descriptive_filename():
    from jobs.services.portals import UPLOAD_URL

    html = ajax_html()
    application = draft()
    application.portal_fingerprint = inspect_secret(html, URL).fingerprint
    application.document = SimpleNamespace(
        name="Example_Candidate_Earnix_Automation_Engineer_CV.pdf"
    )
    session = Mock()
    session.request.side_effect = [
        sta.Response(200, {}, html),
        sta.Response(200, {}, b'{"result":1,"name":"resume.pdf"}'),
        sta.Response(
            200, {}, b'<div class="wpjb-flash-info">Your application has been sent.</div>'
        ),
    ]
    assert sta.send(application, PDF, session=session).startswith("secret_telaviv:")
    calls = session.request.call_args_list
    assert len(calls) == 3 and calls[1].args[:2] == ("POST", UPLOAD_URL)
    assert PDF in calls[1].args[2]
    assert b'filename="Example_Candidate_Earnix_Automation_Engineer_CV.pdf"' in calls[1].args[2]
    assert b"wpjb_main_upload" in calls[1].args[2]
    assert calls[2].args[:2] == ("POST", URL + "?form=apply")
    assert PDF not in calls[2].args[2] and b"filename=" not in calls[2].args[2]
    assert b'name="email"' in calls[2].args[2]


@pytest.mark.parametrize(
    "response",
    [
        sta.Response(200, {}, b'{"result":0,"msg":"private reflected data"}'),
        sta.Response(200, {}, b"not json"),
        sta.Response(200, {}, b'{"result":true}'),
        sta.Response(302, {"location": "https://attacker.example/"}, b""),
        TimeoutError(),
    ],
)
def test_failed_or_uncertain_upload_never_submits_application(response):
    html = ajax_html()
    application = draft()
    application.portal_fingerprint = inspect_secret(html, URL).fingerprint
    session = Mock()
    session.request.side_effect = [sta.Response(200, {}, html), response]
    with pytest.raises(sta.PortalRejected) as exc:
        sta.send(application, PDF, session=session)
    assert session.request.call_count == 2
    assert "private" not in str(exc.value)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.replace(b"wpjb_main_upload", b"unknown_action"),
        lambda data: data.replace(b"jobs.secrettelaviv.com/wp-admin", b"attacker.example/wp-admin"),
        lambda data: data.replace(b'"field": "file"', b'"field": "other"'),
        lambda data: data + data[data.index(b"<script>") :],
    ],
)
def test_upload_configuration_changes_stop_before_any_post(mutate):
    html = mutate(ajax_html())
    session = Mock()
    session.request.return_value = sta.Response(200, {}, html)
    with pytest.raises(sta.PortalRejected, match="form_changed"):
        sta.send(draft(), PDF, session=session)
    session.request.assert_called_once()


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("CV file is required", "portal_cv_required"),
        ("Email is invalid: private@example.com", "portal_email_rejected"),
        ("Telephone is invalid: 12345678", "portal_phone_rejected"),
        ("You already applied for this job", "portal_duplicate_application"),
        ("Unknown error private@example.com", "portal_validation_rejected"),
    ],
)
def test_portal_diagnostics_are_allowlisted_not_reflected(text, code):
    response = sta.Response(200, {}, f'<div class="wpjb-flash-error">{text}</div>'.encode())
    with pytest.raises(sta.PortalRejected) as exc:
        sta.confirmation(response)
    assert str(exc.value) == code


@pytest.mark.parametrize(
    ("formatted", "expected"),
    [
        ("+972-52-123-4567", "972521234567"),
        ("052-123-4567", "0521234567"),
        ("+1 (212) 555-0100", "12125550100"),
    ],
)
def test_phone_transport_preserves_digits_and_leading_zeroes(formatted, expected):
    application = draft()
    application.portal_answers["phone"] = formatted
    session = Mock()
    session.request.side_effect = [
        sta.Response(200, {}, HTML),
        sta.Response(200, {}, b'<div class="wpjb-flash-info">Application sent</div>'),
    ]
    sta.send(application, PDF, session=session)
    assert expected.encode() in session.request.call_args.args[2]
    assert application.portal_answers["phone"] == formatted


@pytest.mark.parametrize(
    "phone", ["123", "+1234567890123456", "phone@example.com", "0521234567 ext 2"]
)
def test_invalid_phone_stops_before_network(phone):
    application = draft()
    application.portal_answers["phone"] = phone
    session = Mock()
    with pytest.raises(ValidationError):
        sta.send(application, PDF, session=session)
    session.request.assert_not_called()


def test_numeric_phone_validation_is_actionable():
    response = sta.Response(
        200, {}, b'<div class="wpjb-errors">Telephone number * Value is not a Float.</div>'
    )
    with pytest.raises(sta.PortalRejected, match="portal_phone_rejected"):
        sta.confirmation(response)
