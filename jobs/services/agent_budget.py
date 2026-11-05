"""Settle known-successful agent reservations without refunding uncertain requests."""

import json
import logging
from datetime import timedelta

from django.db import transaction

from jobs.models import AgentExecution, AICharge, AIQuota, AuditEvent

from .ai_costs import settle as settle_cost
from .evaluation_context import runtime_context

logger = logging.getLogger(__name__)


def reservation_amount(snapshot: dict) -> int:
    return 4 * (
        2 * len(json.dumps(runtime_context(snapshot)).encode())
        + 8192
        + snapshot["limits"]["max_output_tokens"]
    )


def reported_total(usage: dict) -> int | None:
    values = [usage.get(key) for key in ("input_tokens", "output_tokens")]
    if any(type(value) is not int or value < 0 for value in values):
        return None
    return sum(values)


def settle_success(record: AgentExecution, usage: dict) -> bool:
    """Caller holds the execution row lock; quota locks follow it consistently."""
    actual = reported_total(usage)
    if record.budget_settled or record.budget_day is None or actual is None:
        return False
    quota = AIQuota.objects.select_for_update().filter(day=record.budget_day).first()
    if quota is None or quota.reserved_tokens < record.budget_reserved_tokens:
        logger.warning(
            "agent_budget_accounting_inconsistent", extra={"execution_id": str(record.pk)}
        )
        return False
    quota.reserved_tokens += actual - record.budget_reserved_tokens
    quota.save(update_fields=["reserved_tokens"])
    charge = AICharge.objects.filter(key=f"execution:{record.attempt_id}").first()
    if charge:
        settle_cost(charge.pk, usage)
    record.budget_charged_tokens = actual
    record.budget_settled = True
    AuditEvent.objects.create(
        actor_id=record.owner_id,
        event="agent.budget_settled",
        details={
            "execution_id": str(record.pk),
            "attempt_id": str(record.attempt_id),
            "day": str(record.budget_day),
            "reserved_tokens": record.budget_reserved_tokens,
            "charged_tokens": actual,
        },
    )
    return True


def reconcile_legacy_successes() -> dict:
    """Only single-attempt completed executions have attributable historical usage.

    A deadline proves admission; subtract 180s to recover its original UTC quota day.
    Unknown outcomes and multi-attempt legacy records keep their conservative charge.
    """
    count, released = 0, 0
    ids = AgentExecution.objects.filter(
        status="completed", attempt=1, deadline__isnull=False, budget_day__isnull=True
    ).values_list("pk", flat=True)
    for pk in ids.iterator():
        with transaction.atomic():
            record = AgentExecution.objects.select_for_update().get(pk=pk)
            actual = reported_total(record.usage)
            if record.budget_day is not None or record.status != "completed" or actual is None:
                continue
            record.budget_day = (record.deadline - timedelta(seconds=180)).date()
            record.budget_reserved_tokens = reservation_amount(record.snapshot)
            if not settle_success(record, record.usage):
                continue
            record.save(
                update_fields=[
                    "budget_day",
                    "budget_reserved_tokens",
                    "budget_charged_tokens",
                    "budget_settled",
                ]
            )
            count += 1
            released += record.budget_reserved_tokens - actual
    return {"settled_executions": count, "released_tokens": released}
