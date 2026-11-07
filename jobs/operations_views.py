"""Staff-only operational views. Do not pass private runtime payloads to templates."""

from datetime import timedelta
from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_safe

from jobs.models import AgentExecution, Application, BatchItem, IngestionRun
from jobs.services.agent_gateway import AgentGateway, GatewayError
from jobs.services.operations import snapshot_context


def staff_only(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_active or not request.user.is_staff:
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapped


def counts(queryset, field):
    return [
        {"label": row[field], "count": row["count"]}
        for row in queryset.values(field).annotate(count=Count("pk")).order_by(field)
    ]


@require_safe
@staff_only
def index(request):
    now = timezone.now()
    hours = request.GET.get("hours", "24")
    if hours not in {"1", "24", "168"}:
        hours = "24"
    since = now - timedelta(hours=int(hours))
    kind = request.GET.get("kind", "evaluations")
    datasets = {
        "evaluations": (AgentExecution.objects.all(), "status", "created_at"),
        "ingestion": (IngestionRun.objects.all(), "status", "started_at"),
        "preparation": (BatchItem.objects.all(), "state", "created_at"),
        "delivery": (Application.objects.all(), "status", "created_at"),
    }
    if kind not in datasets:
        kind = "evaluations"
    queryset, status_field, time_field = datasets[kind]
    queryset = queryset.filter(**{f"{time_field}__gte": since})
    statuses = list(queryset.order_by(status_field).values_list(status_field, flat=True).distinct())
    status = request.GET.get("status", "")
    if status in statuses:
        queryset = queryset.filter(**{status_field: status})
    else:
        status = ""
    # Only project metadata. Never select snapshots, provider results, or applicant content.
    fields = ["pk", status_field, time_field, "error_code"]
    extra_fields = {
        "evaluations": [
            "attempt",
            "model",
            "next_dispatch_at",
            "deadline",
            "lease_until",
            "usage",
            "retryable",
        ],
        "ingestion": [
            "source_id",
            "finished_at",
            "imported",
            "rejected",
            "request_count",
            "duration_seconds",
            "queue_delay_seconds",
            "complete",
        ],
        "preparation": ["attempts", "next_attempt_at", "lease_until"],
        "delivery": [
            "send_attempts",
            "retry_not_before",
            "started_at",
            "assisted_delivery",
            "approved_at",
            "next_dispatch_at",
        ],
    }
    page = Paginator(
        queryset.order_by(f"-{time_field}", "-pk").values(*fields, *extra_fields[kind]), 25
    ).get_page(request.GET.get("page"))
    rows = []
    for record in page:
        overdue = False
        if kind == "evaluations":
            overdue = record["status"] in {"queued", "running"} and any(
                record[key] and record[key] < now for key in ("deadline", "lease_until")
            )
        elif kind == "ingestion":
            overdue = record["status"] == "running" and record["started_at"] < now - timedelta(
                minutes=10
            )
        elif kind == "preparation":
            overdue = record["state"] == "preparing" and bool(
                record["lease_until"] and record["lease_until"] < now
            )
        elif kind == "delivery":
            overdue = record["status"] == "submitting" and bool(
                record["started_at"]
                and record["started_at"]
                < now - timedelta(minutes=15 if record["assisted_delivery"] else 10)
            )
        rows.append(
            {
                "id": record["pk"],
                "status": record[status_field],
                "created_at": record[time_field],
                "error_code": record["error_code"],
                "overdue": overdue,
                "metadata": [
                    (key, value)
                    for key, value in record.items()
                    if key in extra_fields[kind] and key != "usage"
                ],
            }
        )
    # Token totals are from execution records only. Missing counts stay unknown.
    tokens = {"input_tokens": 0, "output_tokens": 0, "unknown": 0}
    for usage in (
        AgentExecution.objects.filter(created_at__gte=since)
        .values_list("usage", flat=True)
        .iterator()
    ):
        for key in ("input_tokens", "output_tokens"):
            value = usage.get(key)
            if type(value) is int and value >= 0:
                tokens[key] += value
        if any(type(usage.get(key)) is not int for key in ("input_tokens", "output_tokens")):
            tokens["unknown"] += 1
    return render(
        request,
        "jobs/operations.html",
        {
            "ai_rollout_approved": settings.AI_PILOT_ROLLOUT_APPROVED,
            "ai_batch_enabled": settings.JOB_NORMALIZATION_BATCH_ENABLED,
            "ai_costs": __import__(
                "jobs.services.ai_costs", fromlist=["operations_summary"]
            ).operations_summary(),
            "ai_normalization_enabled": settings.JOB_AI_NORMALIZATION_ENABLED,
            "normalization_model": settings.JOB_NORMALIZATION_MODEL,
            "evaluation_model": settings.INFINITY_MODEL,
            "infrastructure": snapshot_context(),
            "grafana_url": grafana_link(),
            "page": page,
            "rows": rows,
            "kind": kind,
            "kinds": datasets,
            "hours": hours,
            "status": status,
            "statuses": statuses,
            "tokens": tokens,
            "observed_at": now,
            "summaries": {
                "Evaluations": counts(AgentExecution.objects, "status"),
                "Ingestion": counts(IngestionRun.objects, "status"),
                "Preparation": counts(BatchItem.objects, "state"),
                "Delivery": counts(Application.objects, "status"),
            },
        },
    )


@require_safe
@staff_only
def execution(request, pk):
    record = get_object_or_404(
        AgentExecution.objects.only(
            "id",
            "attempt_id",
            "status",
            "model",
            "attempt",
            "error_code",
            "usage",
            "created_at",
            "updated_at",
            "deadline",
            "lease_until",
            "next_dispatch_at",
            "retryable",
        ),
        pk=pk,
    )
    try:
        calls = AgentGateway().provider_calls(str(record.pk))
        unavailable = False
    except GatewayError:
        calls, unavailable = [], True
    return render(
        request,
        "jobs/operation_execution.html",
        {
            "execution": record,
            "calls": calls,
            "provider_unavailable": unavailable,
            "grafana_logs_url": grafana_link(str(record.pk)),
            "input_tokens": record.usage.get("input_tokens"),
            "output_tokens": record.usage.get("output_tokens"),
        },
    )


def grafana_link(correlation=None):
    from urllib.parse import urlencode, urlsplit

    from jobs.observability.context import identifier

    value = settings.GRAFANA_URL
    try:
        parts = urlsplit(value)
        _port = parts.port  # Validate malformed ports without breaking operations pages.
    except ValueError:
        return ""
    if (
        parts.scheme in {"http", "https"}
        and parts.hostname
        and not parts.username
        and not parts.password
        and not parts.query
        and not parts.fragment
        and parts.path in {"", "/"}
    ):
        if identifier(correlation):
            return (
                value.rstrip("/")
                + "/d/jobstream-investigations?"
                + urlencode({"var-correlation": correlation, "from": "now-24h", "to": "now"})
            )
        return value.rstrip("/") + "/d/jobstream-overview"
    return ""
