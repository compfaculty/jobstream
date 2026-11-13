from unittest.mock import Mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from jobs import tasks
from jobs.models import Application
from jobs.services import camunda_ashby, cato_greenhouse, submissions
from tests.test_camunda_ashby import (  # noqa: F401
    SCHEMA,
    camunda_application,
    camunda_job,
)
from tests.test_cato_greenhouse import (  # noqa: F401
    IDENTITY,
    PAYLOAD,
    cato_application,
    cato_job,
)


@pytest.fixture(params=["cato", "camunda"])
def assisted_case(request, monkeypatch):
    name = request.param
    provider = cato_greenhouse if name == "cato" else camunda_ashby
    application = request.getfixturevalue(name + "_application")
    schema = cato_greenhouse.inspect_schema(PAYLOAD, IDENTITY) if name == "cato" else SCHEMA
    monkeypatch.setattr(provider, "read_schema", lambda job: schema)
    return provider, application


def test_approval_reservation_ui_and_worker_exclusion(
    assisted_case, client, monkeypatch, prepare_review_cv
):
    provider, application = assisted_case
    prepare_review_cv(application)
    client.force_login(application.owner)
    detail = client.get(f"/applications/{application.pk}/")
    assert b'name="assisted" value="1"' in detail.content
    response = client.post(
        f"/applications/{application.pk}/approve/", {"assisted": "1"}, follow=True
    )
    assert response.status_code == 200
    application.refresh_from_db()
    assert application.assisted_delivery and application.status == "approved"
    assert application.approval_digest == submissions.digest(application, "")
    assert b"Your application is approved but has not been sent." in response.content
    assert b"python manage.py" not in response.content
    assert b"--owner-id" not in response.content
    assert b"background worker" not in response.content
    send = Mock()
    monkeypatch.setattr(provider, "send", send)
    monkeypatch.setattr(tasks.send_browser_application, "delay", Mock())
    tasks.dispatch_submissions()
    tasks.send_browser_application.delay.assert_not_called()
    submissions.submit(application.pk)
    send.assert_not_called()


def test_generic_command_checks_owner_and_never_sends_twice(assisted_case, other, monkeypatch):
    provider, application = assisted_case
    submissions.approve(application.owner, application.pk, assisted=True)
    send = Mock(return_value="fixture:receipt")
    monkeypatch.setattr(provider, "send", send)
    with pytest.raises(CommandError):
        call_command("assist_application", application.pk, owner_id=other.pk)
    send.assert_not_called()
    call_command("assist_application", application.pk, owner_id=application.owner_id)
    application.refresh_from_db()
    assert application.status == "confirmed" and application.send_attempts == 1
    assert send.call_args.kwargs["on_human_verification"] is not None
    with pytest.raises(CommandError):
        call_command("assist_application", application.pk, owner_id=application.owner_id)
    send.assert_called_once()


@pytest.mark.parametrize("change", ["answers", "mode", "cv"])
def test_assisted_preflight_rejects_changed_approval(assisted_case, monkeypatch, change):
    provider, application = assisted_case
    submissions.approve(application.owner, application.pk, assisted=True)
    application.refresh_from_db()
    if change == "mode":
        application.assisted_delivery = False
    elif change == "answers":
        application.portal_answers["email"] = "changed@example.invalid"
    else:
        application.document.sha256 = "0" * 64
        application.document.save()
    application.save()
    send = Mock()
    monkeypatch.setattr(provider, "send", send)
    with pytest.raises(CommandError):
        call_command("assist_application", application.pk, owner_id=application.owner_id)
    send.assert_not_called()


def test_reapproval_can_release_assisted_reservation(assisted_case):
    _, application = assisted_case
    submissions.approve(application.owner, application.pk, assisted=True)
    application.refresh_from_db()
    application.status = Application.Status.FAILED
    application.save()
    submissions.approve(application.owner, application.pk)
    application.refresh_from_db()
    assert not application.assisted_delivery
    assert application.approval_digest == submissions.digest(application, "")


def test_assisted_cross_user_approval_denied(assisted_case, other):
    _, application = assisted_case
    with pytest.raises(Application.DoesNotExist):
        submissions.approve(other, application.pk, assisted=True)


def test_provider_forwards_visible_session_and_callback(assisted_case, monkeypatch):
    from jobs.services import ashby_browser, cato_browser

    provider, application = assisted_case
    submissions.approve(application.owner, application.pk, assisted=True)
    application.refresh_from_db()
    transport = cato_browser if provider is cato_greenhouse else ashby_browser
    delivery = Mock(return_value="fixture:receipt")
    monkeypatch.setattr(transport, "deliver", delivery)
    callback = Mock()
    provider.send(application, b"%PDF-fixture", on_human_verification=callback)
    assert delivery.call_args.kwargs == {"on_human_verification": callback, "assisted": True}


@pytest.mark.django_db(transaction=True)
def test_worker_and_two_assisted_sessions_have_one_winner(assisted_case, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections

    provider, application = assisted_case
    submissions.approve(application.owner, application.pk, assisted=True)
    send = Mock(return_value="fixture:receipt")
    monkeypatch.setattr(provider, "send", send)
    barrier = Barrier(3)

    def attempt(assisted):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                submissions.submit(
                    application.pk,
                    assisted_owner_id=application.owner_id if assisted else None,
                )
            except submissions.InvalidTransition:
                pass  # Another local session already claimed this exact approval.
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(attempt, assisted) for assisted in (False, True, True)]
        for future in futures:
            future.result(timeout=15)
    application.refresh_from_db()
    assert application.status == "confirmed" and application.send_attempts == 1
    send.assert_called_once()
