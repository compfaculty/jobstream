"""Atomic application-side USD reservations. Unknown outcomes are never refunded."""

from decimal import ROUND_UP, Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from jobs.models import AICharge, AICostMonth, AIQuota, UserComparisonDay


class BudgetUnavailable(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def prices(model, batch=False):
    values = settings.AI_MODEL_PRICES.get(model)
    if not values or len(values) != 2:
        raise BudgetUnavailable("ai_model_price_unknown")
    rates = [Decimal(str(v)) for v in values]
    if any(not v.is_finite() or v < 0 for v in rates):
        raise BudgetUnavailable("ai_model_price_unknown")
    factor = Decimal("0.5") if batch else Decimal(1)
    return {
        "input": str(rates[0] * factor),
        "output": str(rates[1] * factor),
        "version": settings.AI_PRICING_VERSION,
        "batch": batch,
    }


def cost(pricing, input_tokens, output_tokens):
    return (
        (Decimal(pricing["input"]) * input_tokens + Decimal(pricing["output"]) * output_tokens)
        / Decimal(1000000)
    ).quantize(Decimal("0.00000001"), rounding=ROUND_UP)


@transaction.atomic
def reserve(
    key,
    model,
    workload,
    input_bound,
    output_bound,
    *,
    owner=None,
    batch=False,
    daily=False,
    comparison=False,
):
    existing = AICharge.objects.filter(key=key).first()
    if existing:
        return existing
    now = timezone.now()
    day, month = now.date(), now.date().replace(day=1)
    token_bound = input_bound + output_bound
    if daily:
        AIQuota.objects.get_or_create(day=day)
        quota = AIQuota.objects.select_for_update().get(day=day)
        if quota.reserved_tokens + token_bound > settings.AI_DAILY_TOKEN_BUDGET:
            raise BudgetUnavailable("ai_daily_budget_exceeded")
        quota.reserved_tokens += token_bound
        quota.save(update_fields=["reserved_tokens"])
    AICostMonth.objects.get_or_create(month=month)
    bucket = AICostMonth.objects.select_for_update().get(month=month)
    # Recheck after serialization: concurrent duplicate admissions must not double charge.
    existing = AICharge.objects.filter(key=key).first()
    if existing:
        if daily:
            quota.reserved_tokens -= token_bound
            quota.save(update_fields=["reserved_tokens"])
        return existing
    pricing = prices(model, batch)
    amount = cost(pricing, input_bound, output_bound)
    if bucket.committed_usd + amount > Decimal(settings.AI_MONTHLY_BUDGET_USD):
        raise BudgetUnavailable("ai_monthly_budget_exceeded")
    if comparison and owner and not owner.is_staff:
        UserComparisonDay.objects.get_or_create(owner=owner, day=day)
        allowance = UserComparisonDay.objects.select_for_update().get(owner=owner, day=day)
        if allowance.admitted >= settings.AI_USER_DAILY_COMPARISONS:
            raise BudgetUnavailable("ai_user_daily_limit")
        allowance.admitted += 1
        allowance.save(update_fields=["admitted"])
    bucket.committed_usd += amount
    bucket.save(update_fields=["committed_usd"])
    return AICharge.objects.create(
        key=key,
        month=bucket,
        owner=owner,
        workload=workload,
        model=model,
        pricing=pricing,
        reserved_usd=amount,
        day=day,
        daily_tokens=token_bound if daily else 0,
    )


@transaction.atomic
def settle(charge_id, usage):
    # Same lock order as admission: daily bucket, monthly bucket, charge.
    initial = AICharge.objects.get(pk=charge_id)
    values = (
        [usage.get(name) for name in ("input_tokens", "output_tokens")]
        if isinstance(usage, dict)
        else []
    )
    if len(values) != 2 or any(type(v) is not int or v < 0 for v in values):
        return False
    quota = None
    if initial.daily_tokens:
        quota = AIQuota.objects.select_for_update().get(day=initial.day)
    month = AICostMonth.objects.select_for_update().get(pk=initial.month_id)
    charge = AICharge.objects.select_for_update().get(pk=charge_id)
    if charge.state != "reserved":
        return False
    actual = cost(charge.pricing, *values)
    month.committed_usd += actual - charge.reserved_usd
    month.save(update_fields=["committed_usd"])
    if quota:
        quota.reserved_tokens = max(0, quota.reserved_tokens + sum(values) - charge.daily_tokens)
        quota.save(update_fields=["reserved_tokens"])
    charge.charged_usd, charge.usage, charge.state = (
        actual,
        dict(zip(("input_tokens", "output_tokens"), values, strict=True)),
        "settled",
    )
    charge.save(update_fields=["charged_usd", "usage", "state"])
    return True


def operations_summary():
    from django.db.models import Count, Sum

    from jobs.models import AgentExecution, AIResult, CVRepresentation, NormalizationWork

    now = timezone.now()
    month = AICostMonth.objects.filter(month=now.date().replace(day=1)).first()
    backlog = NormalizationWork.objects.filter(
        state__in=["pending", "prepared", "submitted", "budget_blocked", "retry"]
    )
    oldest = backlog.order_by("created_at").values_list("created_at", flat=True).first()
    return {
        "monthly_limit": settings.AI_MONTHLY_BUDGET_USD,
        "monthly_committed": str(month.committed_usd if month else 0),
        "unsettled": AICharge.objects.filter(state="reserved").count(),
        "workloads": list(
            AICharge.objects.filter(month__month=now.date().replace(day=1))
            .values("workload")
            .annotate(
                requests=Count("pk"), reserved=Sum("reserved_usd"), charged=Sum("charged_usd")
            )
        ),
        "cache_hits": (AIResult.objects.aggregate(n=Sum("cache_hits"))["n"] or 0)
        + (AgentExecution.objects.aggregate(n=Sum("cache_hits"))["n"] or 0),
        "backlog": backlog.count(),
        "backlog_age_seconds": int((now - oldest).total_seconds()) if oldest else 0,
        "budget_blocked": NormalizationWork.objects.filter(state="budget_blocked").count(),
        "cv_states": list(CVRepresentation.objects.values("state").annotate(count=Count("pk"))),
    }
