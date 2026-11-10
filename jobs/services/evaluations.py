"""Application-owned evaluation snapshots, budgets, fencing and validation."""

import hashlib
import json
import logging
import textwrap
import uuid
from datetime import timedelta
from decimal import Decimal

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from jobs.models import AgentExecution, AIQuota, AuditEvent, Document, ResumeProfile

from .agent_budget import reservation_amount, settle_success
from .agent_gateway import AgentGateway, ExecutionRequest, GatewayError
from .ai_costs import BudgetUnavailable
from .ai_costs import reserve as reserve_cost
from .evaluation_context import runtime_context
from .preference_matching import evaluate, preferences_for

logger = logging.getLogger(__name__)
VERSION = "vacancy-evaluation-stack-v3"
CAPABILITY_SALT = "vacancy-evaluation-v1"
ACTIVE = {"queued", "running"}
POLICY_FIELDS = (
    "excluded_technologies",
    "preferred_locations",
    "preferred_titles",
    "wanted_technologies",
    "location_required",
    "title_required",
    "technology_required",
    "remote_only",
)


def enabled():
    return settings.AGENT_BACKEND == "infinity"


def capability(execution):
    return signing.dumps(
        {"execution_id": str(execution.pk), "attempt_id": str(execution.attempt_id)},
        key=settings.INFINITY_TOOL_SECRET,
        salt=CAPABILITY_SALT,
    )


def start(owner, job, profile_id=None, *, document_id=None):
    if not enabled() or not settings.INFINITY_TOOL_SECRET or not settings.INFINITY_MODEL:
        raise ValidationError("Vacancy evaluation is not configured.")
    profile = None
    if document_id is not None:
        if profile_id is not None:
            raise ValidationError("Choose one CV source for evaluation.")
        document = Document.objects.filter(pk=document_id, owner=owner).first()
        if document is None:
            raise ValidationError("Choose one of your uploaded CVs.")
        if settings.CV_REPRESENTATIONS_ENABLED:
            from .cv_profiles import ensure

            representation = ensure(document)
            if representation.state not in {"ready", "fallback"}:
                raise ValidationError(
                    "Your CV is being processed. Please try again shortly."
                    if representation.state != "failed"
                    else (
                        "This PDF could not be read. Upload a text-based PDF "
                        "or use optional fact review."
                    )
                )
            facts = representation.facts
        else:
            from .resumes import extract

            try:
                text = extract(document).strip()
            except (ValidationError, OSError, BotoCoreError, ClientError) as exc:
                raise ValidationError(
                    "This PDF could not be read for evaluation. Choose a text-based PDF, "
                    "or use an optional reviewed profile."
                ) from exc
            # Preserve extracted source text mechanically; no AI interpretation or manual
            # confirmation is needed to turn the uploaded CV into bounded input chunks.
            chunks = textwrap.wrap(
                text,
                width=4000,
                expand_tabs=False,
                replace_whitespace=False,
                drop_whitespace=False,
                break_on_hyphens=False,
            )
            facts = [
                {"id": f"fact-{i + 1}", "section": "Other", "text": chunk}
                for i, chunk in enumerate(chunks)
            ]
        source_key = f"document:{document.pk}:{document.sha256}"
    else:
        profile = ResumeProfile.objects.filter(pk=profile_id, owner=owner).first()
        if profile is None:
            raise ValidationError("Choose an uploaded CV or an optional reviewed profile.")
        document = profile.document
        facts = profile.facts
        source_key = profile.pk
    policy = preferences_for(owner.pk)
    vacancy = {
        name: getattr(job, name)
        for name in (
            "title",
            "company",
            "description",
            "location",
            "normalized",
        )
    }
    snapshot = {
        "limits": {
            "max_input_characters": settings.AI_MAX_INPUT_CHARACTERS,
            "max_output_tokens": settings.AI_MAX_OUTPUT_TOKENS,
        },
        "job": vacancy,
        "facts": facts,
        "preferences": {name: getattr(policy, name) for name in POLICY_FIELDS},
        "fit": evaluate(vacancy, policy, "\n".join(f["text"] for f in facts)).as_dict(),
    }
    if document_id is not None:
        snapshot["input_source"] = {
            "kind": "uploaded_pdf",
            "document_id": document.pk,
            "sha256": document.sha256,
        }
    from .evaluation_scoring import criteria_for, preference_blocks

    snapshot["assessment_version"] = 2
    snapshot["runtime_context_version"] = 1
    snapshot["reasoning_effort"] = settings.INFINITY_REASONING_EFFORT
    snapshot["criteria"] = criteria_for(vacancy)
    from .cv_profiles import exact_assessments

    snapshot["exact_assessments"] = exact_assessments(snapshot["criteria"], facts)
    snapshot["preference_blocks"] = preference_blocks(vacancy, snapshot["preferences"])
    encoded = json.dumps(runtime_context(snapshot), sort_keys=True, ensure_ascii=False)
    if len(encoded) > settings.AI_MAX_INPUT_CHARACTERS:
        raise ValidationError("The profile and vacancy exceed the AI input budget.")
    key = hashlib.sha256(
        json.dumps(
            [
                owner.pk,
                job.pk,
                source_key,
                snapshot,
                VERSION,
                settings.INFINITY_MODEL,
                settings.AI_MAX_INPUT_CHARACTERS,
                settings.AI_MAX_OUTPUT_TOKENS,
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    execution, created = AgentExecution.objects.get_or_create(
        key=key,
        defaults={
            "owner": owner,
            "job": job,
            "profile": profile,
            "document": document,
            "snapshot": snapshot,
            "model": settings.INFINITY_MODEL,
            "version": VERSION,
        },
    )
    if not created:
        AgentExecution.objects.filter(pk=execution.pk).update(cache_hits=F("cache_hits") + 1)
    if created:
        AuditEvent.objects.create(
            actor=owner,
            event="agent.requested",
            details={
                "execution_id": str(execution.pk),
                "job_id": job.pk,
            },
        )
    # Publication is an optimization; the periodic dispatcher is the recovery path.
    return execution


def validate_result(snapshot, result):
    if snapshot.get("assessment_version") == 2:
        from .evaluation_scoring import validate_assessment

        if isinstance(result, dict) and snapshot.get("exact_assessments"):
            result = {
                **result,
                "assessments": result.get("assessments", []) + snapshot["exact_assessments"],
            }
        return validate_assessment(snapshot, result)
    keys = {"ordered_fact_ids", "highlight_fact_ids", "unmatched_requirements"}
    if not isinstance(result, dict) or set(result) != keys:
        raise GatewayError("agent_invalid_result")
    ids = [fact["id"] for fact in snapshot["facts"]]
    order, highlights, gaps = (
        result[key]
        for key in (
            "ordered_fact_ids",
            "highlight_fact_ids",
            "unmatched_requirements",
        )
    )
    if any(not isinstance(values, list) for values in (order, highlights, gaps)):
        raise GatewayError("agent_invalid_result")
    if any(not isinstance(value, str) for values in (order, highlights, gaps) for value in values):
        raise GatewayError("agent_invalid_result")
    if (
        len(order) != len(ids)
        or set(order) != set(ids)
        or len(highlights) > 3
        or len(set(highlights)) != len(highlights)
        or not set(highlights) <= set(ids)
        or len(gaps) > 30
        or any(not gap or gap not in snapshot["job"]["description"] for gap in gaps)
    ):
        raise GatewayError("agent_unsupported_claim")
    return {
        "fit": snapshot["fit"],
        "ai_selection": result,
        "highlight_facts": [
            fact["text"]
            for fact_id in highlights
            for fact in snapshot["facts"]
            if fact["id"] == fact_id
        ],
    }


def cancel(execution):
    with transaction.atomic():
        current = AgentExecution.objects.select_for_update().get(pk=execution.pk)
        if current.status not in ACTIVE:
            return
        current.status = "cancelled"
        current.lease_until = None
        current.save()
        AuditEvent.objects.create(
            actor_id=current.owner_id,
            event="agent.cancelled",
            details={
                "execution_id": str(current.pk),
            },
        )
    # The dispatcher also retries cancellation delivery after transient service failure.
    try:
        deliver_cancellation(current)
    except GatewayError:
        logger.info("agent_cancel_pending", extra={"execution_id": str(current.pk)})


def deliver_cancellation(record):
    try:
        AgentGateway().cancel(str(record.pk), str(record.attempt_id))
    except GatewayError as exc:
        if exc.code != "agent_not_found":
            raise
    AgentExecution.objects.filter(pk=record.pk, status="cancelled").update(
        cancellation_delivered=True
    )


def retry(execution):
    with transaction.atomic():
        current = AgentExecution.objects.select_for_update().get(pk=execution.pk)
        budget_rejection = (
            current.error_code
            in {
                "ai_daily_budget_exceeded",
                "ai_evaluation_budget_too_small",
                "ai_monthly_budget_exceeded",
                "ai_user_daily_limit",
            }
            and current.deadline is None
        )
        if (
            current.status != "failed"
            or not current.retryable
            or (current.attempt >= 3 and not budget_rejection)
        ):
            raise ValidationError("This evaluation cannot be retried.")
        if current.next_dispatch_at > timezone.now() and not budget_rejection:
            raise ValidationError("The provider retry window has not elapsed yet.")
        if not budget_rejection:
            current.attempt += 1
            current.attempt_id = uuid.uuid4()
        # Unknown previous attempts keep their charge; never refund on retry.
        current.budget_day = None
        current.budget_reserved_tokens = 0
        current.budget_charged_tokens = None
        current.budget_settled = False
        current.status = "queued"
        current.deadline = None
        current.lease_until = None
        current.next_dispatch_at = timezone.now()
        current.error_code = ""
        current.save()
        AuditEvent.objects.create(
            actor_id=current.owner_id,
            event="agent.retried",
            details={
                "execution_id": str(current.pk),
                "attempt": current.attempt,
            },
        )


def add_usage(record, usage):
    for name in ("input_tokens", "output_tokens"):
        record.usage[name] = record.usage.get(name, 0) + usage.get(name, 0)
    prices = (
        settings.INFINITY_INPUT_COST_PER_MILLION,
        settings.INFINITY_OUTPUT_COST_PER_MILLION,
    )
    if all(prices):
        record.usage["estimated_cost"] = str(
            sum(
                Decimal(record.usage.get(name, 0)) * Decimal(price) / 1000000
                for name, price in zip(("input_tokens", "output_tokens"), prices, strict=True)
            )
        )


def process(pk):
    if not enabled():
        return
    cancelled = AgentExecution.objects.filter(pk=pk, status="cancelled").first()
    if cancelled:
        try:
            deliver_cancellation(cancelled)
        except GatewayError:
            logger.info("agent_cancel_pending", extra={"execution_id": str(pk)})
        return
    now = timezone.now()
    with transaction.atomic():
        record = AgentExecution.objects.select_for_update().get(pk=pk)
        if record.status not in ACTIVE or (record.lease_until and record.lease_until > now):
            return
        if (
            record.status == "queued"
            and record.snapshot.get("assessment_version") == 2
            and record.snapshot.get("preference_blocks")
        ):
            record.status = "completed"
            record.result = {
                "fit": record.snapshot["fit"],
                "match_score": 0,
                "cv_match_score": None,
                "summary": "Your saved preferences exclude this role.",
                "preference_blocks": record.snapshot["preference_blocks"],
                "assessment_skipped": True,
                "assessments": [],
                "strengths": [],
                "gaps": [],
            }
            record.usage = {"input_tokens": 0, "output_tokens": 0}
            record.lease_until = None
            record.retryable = False
            record.save()
            AuditEvent.objects.create(
                actor_id=record.owner_id,
                event="agent.completed",
                details={"execution_id": str(record.pk), "reason": "hard_preferences"},
            )
            logger.info(
                "agent_preference_override",
                extra={
                    "execution_id": str(record.pk),
                    "attempt_id": str(record.attempt_id),
                    "match_score": 0,
                },
            )
            return
        if (
            record.status == "queued"
            and record.snapshot.get("exact_assessments")
            and len(record.snapshot["exact_assessments"]) == len(record.snapshot["criteria"])
        ):
            record.result = validate_result(
                record.snapshot,
                {
                    "summary": "Your CV explicitly demonstrates the listed skills.",
                    "confidence": "high",
                    "assessments": [],
                },
            )
            record.status, record.lease_until = "completed", None
            record.usage = {"input_tokens": 0, "output_tokens": 0}
            record.save()
            return
        record.lease_until = now + timedelta(seconds=30)
        if record.deadline is None:
            AIQuota.objects.get_or_create(day=now.date())
            quota = AIQuota.objects.select_for_update().get(day=now.date())
            # Reserve a safe upper bound until successful usage is reported.
            reserve = reservation_amount(record.snapshot)
            if quota.reserved_tokens + reserve > settings.AI_DAILY_TOKEN_BUDGET:
                record.status = "failed"
                record.error_code = (
                    "ai_evaluation_budget_too_small"
                    if reserve > settings.AI_DAILY_TOKEN_BUDGET
                    else "ai_daily_budget_exceeded"
                )
                record.retryable = True
                record.next_dispatch_at = now + timedelta(hours=1)
                record.lease_until = None
                record.save()
                return
            try:
                reserve_cost(
                    f"execution:{record.attempt_id}",
                    record.model,
                    "comparison",
                    reserve,
                    record.snapshot["limits"]["max_output_tokens"],
                    owner=record.owner,
                    comparison=True,
                )
            except BudgetUnavailable as exc:
                record.status, record.error_code, record.retryable = "failed", exc.code, True
                record.next_dispatch_at = now + timedelta(hours=1)
                record.lease_until = None
                record.save()
                return
            quota.reserved_tokens += reserve
            quota.save(update_fields=["reserved_tokens"])
            record.budget_day = now.date()
            record.budget_reserved_tokens = reserve
            record.budget_charged_tokens = None
            record.budget_settled = False
            record.deadline = now + timedelta(seconds=180)
        was_running = record.status == "running"
        record.status = "running"
        record.save()
    gateway = AgentGateway()
    runtime_usage = None
    try:
        request = ExecutionRequest(
            execution_id=str(record.pk),
            attempt_id=str(record.attempt_id),
            correlation_id=str(record.pk),
            model=record.model,
            capability=capability(record),
            input_hash=record.key,
            max_input_characters=record.snapshot["limits"]["max_input_characters"],
            max_output_tokens=record.snapshot["limits"]["max_output_tokens"],
            deadline=record.deadline.isoformat(),
        )
        if record.deadline <= now:
            # A worker can return after the runtime finished before its deadline.
            # Recover that durable outcome, but never start expired inference.
            response = gateway.status(str(record.pk), str(record.attempt_id))
            if response.status != "completed":
                raise GatewayError("agent_timeout", True)
        elif was_running:
            try:
                response = gateway.status(str(record.pk), str(record.attempt_id))
            except GatewayError as exc:
                if exc.code != "agent_not_found":
                    raise
                response = gateway.start(request)
        else:
            response = gateway.start(request)
        if response.status != "running":
            runtime_usage = response.usage
        result = (
            validate_result(record.snapshot, response.result)
            if response.status == "completed"
            else {}
        )
        with transaction.atomic():
            current = AgentExecution.objects.select_for_update().get(pk=pk)
            if current.attempt_id != record.attempt_id or current.status not in ACTIVE:
                return
            current.status = response.status
            current.result = result
            # Usage is cumulative across attempts; each attempt is accepted once at terminal state.
            if response.status != "running":
                add_usage(current, response.usage)
                from jobs.models import AICharge

                from .ai_costs import settle as settle_cost

                charge = AICharge.objects.filter(key=f"execution:{current.attempt_id}").first()
                if charge:
                    settle_cost(charge.pk, response.usage)
                if response.status == "completed":
                    settle_success(current, response.usage)
                AuditEvent.objects.create(
                    actor_id=current.owner_id,
                    event="agent." + response.status,
                    details={"execution_id": str(pk), "attempt": current.attempt},
                )
            current.error_code = response.error_code
            current.retryable = response.retryable
            current.lease_until = None
            current.next_dispatch_at = timezone.now() + timedelta(
                seconds=max(5, response.retry_after)
            )
            current.save()
    except GatewayError as exc:
        with transaction.atomic():
            current = AgentExecution.objects.select_for_update().get(pk=pk)
            if current.attempt_id != record.attempt_id or current.status not in ACTIVE:
                return
            current.error_code, current.retryable = exc.code, exc.retryable
            current.lease_until = None
            current.next_dispatch_at = timezone.now() + timedelta(seconds=exc.delay)
            # Keep the same attempt on transport ambiguity. Starting it again is idempotent.
            if not exc.retryable or record.deadline <= timezone.now():
                current.status = "failed"
                if runtime_usage is not None:
                    add_usage(current, runtime_usage)
                AuditEvent.objects.create(
                    actor_id=current.owner_id,
                    event="agent.failed",
                    details={"execution_id": str(pk), "error_code": exc.code},
                )
            current.save()
    logger.info(
        "agent_evaluation",
        extra={
            "execution_id": str(pk),
            "attempt_id": str(record.attempt_id),
            "status": current.status,
            "error_code": current.error_code,
            "usage": current.usage,
        },
    )
