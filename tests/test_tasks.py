from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.utils import timezone

from jobs import tasks
from jobs.models import Source

pytestmark = pytest.mark.django_db


def test_scheduler_and_outbox(application, monkeypatch):
    source = Source.objects.create(
        name="Source", adapter="rss", url="https://example.com", enabled=True
    )
    ingestion = Mock()
    sending = Mock()
    monkeypatch.setattr(tasks.ingest_source, "delay", ingestion)
    monkeypatch.setattr(tasks.send_application, "delay", sending)
    tasks.schedule_sources()
    ingestion.assert_called_once_with(source.pk)
    application.status = "approved"
    application.save()
    tasks.dispatch_submissions()
    sending.assert_called_once_with(application.pk)
    tasks.dispatch_submissions()
    tasks.schedule_sources()
    sending.assert_called_once()
    ingestion.assert_called_once()


def test_stale_submission_is_not_resent(application, monkeypatch):
    application.status = "submitting"
    application.started_at = timezone.now() - timedelta(minutes=20)
    application.save()
    reconcile = Mock()
    monkeypatch.setattr(tasks, "reconcile", reconcile)
    tasks.reconcile_submissions()
    application.refresh_from_db()
    assert application.status == "unknown"
    reconcile.assert_called_once_with(application.pk)
    tasks.reconcile_submissions()
    reconcile.assert_called_once_with(application.pk)
