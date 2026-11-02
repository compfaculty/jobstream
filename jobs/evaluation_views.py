"""Owner-facing evaluation UI and capability-authenticated read-only runtime tool."""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from jobs.models import AgentExecution, Job
from jobs.services import evaluations


@login_required
@require_POST
def start(request, pk):
    job = get_object_or_404(Job, pk=pk)
    try:
        profile_id = int(request.POST.get("profile", "0"))
        evaluations.start(request.user, job, profile_id)
    except (ValidationError, ValueError) as exc:
        messages.error(request, str(exc))
    return redirect("job", pk=pk)


@login_required
@require_GET
def panel(request, pk):
    job = get_object_or_404(Job, pk=pk)
    executions = AgentExecution.objects.filter(owner=request.user, job=job).order_by("-created_at")[
        :10
    ]
    return render(
        request,
        "jobs/evaluation_panel.html",
        {
            "job": job,
            "evaluations": executions,
            "evaluation_enabled": evaluations.enabled(),
            "evaluation_poll": evaluations.enabled()
            and any(item.status in {"queued", "running"} for item in executions),
        },
    )


@login_required
@require_POST
def action(request, pk, operation):
    execution = get_object_or_404(AgentExecution, pk=pk, owner=request.user)
    if operation == "cancel":
        evaluations.cancel(execution)
    elif operation == "retry" and evaluations.enabled():
        try:
            evaluations.retry(execution)
        except ValidationError as exc:
            messages.error(request, str(exc))
    else:
        raise Http404
    return redirect("job", pk=execution.job_id)


@require_GET
def context(request, pk, attempt_id):
    if not evaluations.enabled() or not settings.INFINITY_TOOL_SECRET:
        raise Http404
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer ") or len(header) > 2048:
        raise Http404
    try:
        claims = signing.loads(
            header[7:],
            key=settings.INFINITY_TOOL_SECRET,
            salt=evaluations.VERSION,
            max_age=180,
            fallback_keys=[],
        )
    except signing.BadSignature:
        raise Http404 from None
    if claims != {"execution_id": str(pk), "attempt_id": str(attempt_id)}:
        raise Http404
    execution = get_object_or_404(
        AgentExecution, pk=pk, attempt_id=attempt_id, status="running", deadline__gt=timezone.now()
    )
    return JsonResponse(
        {
            "schema_version": 1,
            "execution_id": str(pk),
            "attempt_id": str(attempt_id),
            "context": execution.snapshot,
        }
    )
