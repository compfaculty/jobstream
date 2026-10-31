from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.utils import timezone

from jobs.models import Application, Job
from jobs.services import gmail, submissions
from jobs.services.throttles import defer_account

pytestmark = pytest.mark.django_db


def test_worker_wide_spacing(application, connection, monkeypatch):
    second_job = Job.objects.create(canonical_url="https://example.com/jobs/2", title="Other")
    second = Application.objects.create(
        owner=application.owner,
        job=second_job,
        recipient="jobs@example.com",
        subject="Application",
        body="Hello",
    )
    submissions.approve(application.owner, application.pk)
    submissions.approve(application.owner, second.pk)
    send = Mock(return_value="provider-id")
    monkeypatch.setattr(gmail, "send", send)
    submissions.submit(application.pk)
    submissions.submit(second.pk)
    second.refresh_from_db()
    assert send.call_count == 1
    assert second.status == "approved"
    assert second.error_code == "delivery_throttled"
    assert second.next_dispatch_at > timezone.now()


def test_account_cooldown_blocks_other_jobs(application, connection, monkeypatch):
    submissions.approve(application.owner, application.pk)
    until = timezone.now() + timedelta(minutes=10)
    defer_account(application.owner_id, until)
    send = Mock()
    monkeypatch.setattr(gmail, "send", send)
    submissions.submit(application.pk)
    application.refresh_from_db()
    assert application.status == "approved"
    assert application.next_dispatch_at >= until
    send.assert_not_called()
