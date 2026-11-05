from datetime import timedelta
from unittest.mock import Mock

import pytest
from django.db import transaction
from django.utils import timezone

from jobs.models import AIQuota, AuditEvent
from jobs.services import evaluations
from jobs.services.agent_budget import (
    reconcile_legacy_successes,
    reservation_amount,
    settle_success,
)
from jobs.services.agent_gateway import AgentGateway, GatewayError
from tests.test_evaluations import response

pytestmark = pytest.mark.django_db


@pytest.fixture
def record(user, job, evaluation_profile, agent_settings):
    return evaluations.start(user, job, evaluation_profile.pk)


def test_success_charges_actual_usage_once(record, monkeypatch):
    monkeypatch.setattr(AgentGateway, "start", lambda *args: response(record))
    evaluations.process(record.pk)
    evaluations.process(record.pk)
    record.refresh_from_db()
    assert record.budget_settled
    assert record.budget_reserved_tokens > 15
    assert record.budget_charged_tokens == 15
    assert AIQuota.objects.get().reserved_tokens == 15
    assert AuditEvent.objects.filter(event="agent.budget_settled").count() == 1


def test_uncertain_transport_keeps_entire_reservation(record, monkeypatch):
    monkeypatch.setattr(
        AgentGateway, "start", Mock(side_effect=GatewayError("agent_unavailable", True))
    )
    evaluations.process(record.pk)
    record.refresh_from_db()
    assert not record.budget_settled
    assert AIQuota.objects.get().reserved_tokens == reservation_amount(record.snapshot)


def test_missing_usage_is_not_refunded(record):
    record.budget_day = timezone.now().date()
    record.budget_reserved_tokens = 100
    AIQuota.objects.create(day=record.budget_day, reserved_tokens=100)
    with transaction.atomic():
        assert not settle_success(record, {})
    assert AIQuota.objects.get().reserved_tokens == 100


def test_settlement_uses_original_day_and_preserves_other_reservations(record):
    yesterday = timezone.now().date() - timedelta(days=1)
    record.budget_day = yesterday
    record.budget_reserved_tokens = 100
    AIQuota.objects.create(day=yesterday, reserved_tokens=250)
    today = AIQuota.objects.create(day=timezone.now().date(), reserved_tokens=80)
    with transaction.atomic():
        assert settle_success(record, {"input_tokens": 10, "output_tokens": 5})
        assert not settle_success(record, {"input_tokens": 10, "output_tokens": 5})
    assert AIQuota.objects.get(day=yesterday).reserved_tokens == 165
    today.refresh_from_db()
    assert today.reserved_tokens == 80


def test_historical_success_reconciles_exactly_once(record):
    record.status = "completed"
    record.deadline = timezone.now() + timedelta(seconds=180)
    record.usage = {"input_tokens": 10, "output_tokens": 5}
    record.save()
    reserve = reservation_amount(record.snapshot)
    AIQuota.objects.create(day=timezone.now().date(), reserved_tokens=reserve + 80)
    assert reconcile_legacy_successes() == {
        "settled_executions": 1,
        "released_tokens": reserve - 15,
    }
    assert AIQuota.objects.get().reserved_tokens == 95
    assert reconcile_legacy_successes() == {"settled_executions": 0, "released_tokens": 0}
    assert AIQuota.objects.get().reserved_tokens == 95


@pytest.mark.parametrize("status,attempt", [("failed", 1), ("running", 1), ("completed", 2)])
def test_uncertain_legacy_outcomes_are_not_refunded(record, status, attempt):
    record.status, record.attempt = status, attempt
    record.deadline = timezone.now() + timedelta(seconds=180)
    record.usage = {"input_tokens": 10, "output_tokens": 5}
    record.save()
    AIQuota.objects.create(day=timezone.now().date(), reserved_tokens=200000)
    assert reconcile_legacy_successes()["settled_executions"] == 0
    assert AIQuota.objects.get().reserved_tokens == 200000


def test_budget_denial_can_retry_without_wait_or_consuming_attempt(record, settings, monkeypatch):
    settings.AI_DAILY_TOKEN_BUDGET = 1
    start = Mock()
    monkeypatch.setattr(AgentGateway, "start", start)
    evaluations.process(record.pk)
    record.refresh_from_db()
    original_attempt_id = record.attempt_id
    assert record.error_code == "ai_evaluation_budget_too_small"
    assert record.deadline is None
    evaluations.retry(record)
    record.refresh_from_db()
    assert record.status == "queued"
    assert record.attempt == 1
    assert record.attempt_id == original_attempt_id
    start.assert_not_called()


def test_shared_capacity_denial_has_clear_message(record, settings, monkeypatch):
    reserve = reservation_amount(record.snapshot)
    settings.AI_DAILY_TOKEN_BUDGET = reserve + 1
    AIQuota.objects.create(day=timezone.now().date(), reserved_tokens=2)
    start = Mock()
    monkeypatch.setattr(AgentGateway, "start", start)
    evaluations.process(record.pk)
    record.refresh_from_db()
    assert record.error_code == "ai_daily_budget_exceeded"
    assert "No OpenAI request was sent" in record.error_message
    assert record.budget_day is None
    assert AIQuota.objects.get().reserved_tokens == 2
    start.assert_not_called()
