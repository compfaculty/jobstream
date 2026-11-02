"""Assisted sends use the same durable outbox and synthetic applicant fixtures."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections

from jobs import tasks
from jobs.models import AuditEvent
from jobs.services import gitlab_greenhouse as gitlab
from jobs.services import submissions
from tests.test_gitlab_greenhouse import (  # noqa: F401
    IDENTITY,
    SCHEMA,
    gitlab_application,
    gitlab_job,
)


@pytest.fixture
def approved(gitlab_application, monkeypatch):  # noqa: F811
    monkeypatch.setattr(gitlab, "read_schema", lambda job: SCHEMA)
    return gitlab.approve_portal(gitlab_application.owner, gitlab_application.pk, assisted=True)


def test_reserved_approval_ui_and_worker_exclusion(approved, client, monkeypatch):
    client.force_login(approved.owner)
    response = client.get(f"/applications/{approved.pk}/")
    assert response.status_code == 200
    assert b"assist_gitlab_application" in response.content
    assert b"Reserved for local assisted delivery" in response.content
    ordinary, browser = Mock(), Mock()
    monkeypatch.setattr(tasks.send_application, "delay", ordinary)
    monkeypatch.setattr(tasks.send_browser_application, "delay", browser)
    tasks.dispatch_submissions()
    submissions.submit(approved.pk)  # Even a task queued before reservation cannot claim it.
    approved.refresh_from_db()
    assert approved.status == "approved"
    assert approved.send_attempts == 0
    ordinary.assert_not_called()
    browser.assert_not_called()


def test_assisted_command_success_and_duplicate(approved, monkeypatch):
    send = Mock(return_value=f"gitlab_greenhouse:{IDENTITY}")
    monkeypatch.setattr(gitlab, "send", send)
    call_command("assist_gitlab_application", approved.pk, owner_id=approved.owner_id)
    approved.refresh_from_db()
    assert approved.status == "confirmed"
    assert approved.tracking_status == "applied"
    assert approved.portal_receipt_url.endswith(f"/{IDENTITY}/confirmation")
    assert send.call_args.kwargs["on_human_verification"] is not None
    assert (
        AuditEvent.objects.filter(application=approved, event="application.submitting").count() == 1
    )
    with pytest.raises(CommandError):
        call_command("assist_gitlab_application", approved.pk, owner_id=approved.owner_id)
    send.assert_called_once()


@pytest.mark.parametrize("change", ["owner", "draft", "mode", "answers", "cv", "file"])
def test_assisted_command_preflight(approved, other, monkeypatch, change):
    send = Mock()
    monkeypatch.setattr(gitlab, "send", send)
    owner_id = approved.owner_id
    if change == "owner":
        owner_id = other.pk
    elif change == "draft":
        approved.status = "draft"
    elif change == "mode":
        approved.assisted_delivery = False
    elif change == "answers":
        approved.portal_answers["first_name"] = "Changed"
    elif change == "cv":
        approved.document.sha256 = "0" * 64
        approved.document.save()
    else:
        with approved.document.file.open("wb") as stream:
            stream.write(b"tampered")
    approved.save()
    with pytest.raises(CommandError):
        call_command("assist_gitlab_application", approved.pk, owner_id=owner_id)
    send.assert_not_called()


def test_assisted_edit_clears_reservation_and_approval(approved):
    submissions.edit(approved.owner, approved.pk, {"body": "Revised private note"})
    approved.refresh_from_db()
    assert not approved.assisted_delivery
    assert approved.status == "draft"
    assert not approved.approval_digest


@pytest.mark.parametrize("outcome", ["429", "cancel", "error"])
def test_assisted_outcomes(approved, monkeypatch, outcome):
    from jobs.services.secret_telaviv import PortalRateLimited

    error = {"429": PortalRateLimited("60"), "cancel": KeyboardInterrupt(), "error": OSError()}
    monkeypatch.setattr(gitlab, "send", Mock(side_effect=error[outcome]))
    if outcome == "cancel":
        with pytest.raises(KeyboardInterrupt):
            submissions.submit(approved.pk, assisted_owner_id=approved.owner_id)
    else:
        submissions.submit(approved.pk, assisted_owner_id=approved.owner_id)
    approved.refresh_from_db()
    assert approved.status == ("approved" if outcome == "429" else "unknown")
    assert approved.assisted_delivery
    if outcome == "429":
        assert approved.retry_not_before is not None
        # A cooldown cannot be bypassed by invoking the command again.
        submissions.submit(approved.pk, assisted_owner_id=approved.owner_id)
        assert approved.send_attempts == 1


@pytest.mark.django_db(transaction=True)
def test_assisted_and_worker_race(approved, monkeypatch):
    send = Mock(return_value=f"gitlab_greenhouse:{IDENTITY}")
    monkeypatch.setattr(gitlab, "send", send)
    barrier = Barrier(3)

    def run(assisted):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                submissions.submit(
                    approved.pk, assisted_owner_id=approved.owner_id if assisted else None
                )
            except submissions.InvalidTransition:
                return "already claimed"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(run, assisted) for assisted in (False, True, True)]
        for future in futures:
            future.result(timeout=15)
    approved.refresh_from_db()
    assert approved.status == "confirmed"
    assert approved.send_attempts == 1
    send.assert_called_once()


def test_assisted_approval_through_ui(gitlab_application, client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(gitlab, "read_schema", lambda job: SCHEMA)
    client.force_login(gitlab_application.owner)
    detail = client.get(f"/applications/{gitlab_application.pk}/")
    assert b'name="assisted" value="1" checked' in detail.content
    response = client.post(
        f"/applications/{gitlab_application.pk}/approve/", {"assisted": "1"}, follow=True
    )
    assert response.status_code == 200
    gitlab_application.refresh_from_db()
    assert gitlab_application.assisted_delivery
    assert gitlab_application.status == "approved"
    assert gitlab_application.approval_digest == submissions.digest(gitlab_application, "")


def test_assisted_mode_not_available_for_email(application):
    from django.core.exceptions import ValidationError

    with pytest.raises(ValidationError, match="only for GitLab"):
        submissions.approve(application.owner, application.pk, assisted=True)


@pytest.mark.parametrize("minutes, expected", [(11, "submitting"), (16, "unknown")])
def test_assisted_stale_recovery(approved, monkeypatch, minutes, expected):
    from datetime import timedelta

    from django.utils import timezone

    approved.status = "submitting"
    approved.started_at = timezone.now() - timedelta(minutes=minutes)
    approved.save()
    monkeypatch.setattr(tasks, "reconcile", Mock())
    tasks.reconcile_submissions()
    approved.refresh_from_db()
    assert approved.status == expected
