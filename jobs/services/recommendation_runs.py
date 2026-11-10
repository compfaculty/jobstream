"""Bounded coordinator; only existing evaluation workers may call the model.

Shortlisting scans at most 500 recent active jobs and sends at most 20 independent
vacancy evaluations. These are screening limits, not a claim of exhaustive search.
"""

from datetime import timedelta

from botocore.exceptions import BotoCoreError, ClientError
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone

from jobs.models import Document, Job, RecommendationRun, ResumeProfile

from . import evaluations
from .evaluation_scoring import preference_blocks
from .matching import technologies
from .preference_matching import evaluate, preferences_for
from .recommendations import JOB_FIELDS, sources_for

ACTIVE = ("queued", "preparing", "evaluating")
SCAN_LIMIT = 500
EVALUATION_LIMIT = 20


def start(owner, selection):
    if not evaluations.enabled():
        raise ValidationError("Vacancy evaluation is currently unavailable.")
    source = sources_for(owner, selection)["recommendation_source"]
    if source is None or not selection:
        raise ValidationError("Choose one of your CVs.")
    if not settings.INFINITY_TOOL_SECRET or not settings.INFINITY_MODEL:
        raise ValidationError("Vacancy evaluation is not configured.")
    active = RecommendationRun.objects.filter(owner=owner, status__in=ACTIVE).first()
    if active:
        return active
    try:
        with transaction.atomic():
            return RecommendationRun.objects.create(
                owner=owner,
                document=source if isinstance(source, Document) else source.document,
                profile=source if isinstance(source, ResumeProfile) else None,
            )
    except IntegrityError:
        # The partial unique constraint serializes simultaneous button clicks.
        active = RecommendationRun.objects.filter(owner=owner, status__in=ACTIVE).first()
        if active is None:
            raise
        return active


def source_value(run):
    return f"profile:{run.profile_id}" if run.profile_id else f"document:{run.document_id}"


def progress(run):
    counts = run.executions.aggregate(
        completed=Count("pk", filter=Q(status="completed")),
        failed=Count("pk", filter=Q(status__in=["failed", "cancelled"])),
        pending=Count("pk", filter=Q(status__in=["queued", "running"])),
    )
    return {**counts, "rejected": len(run.rejected_ids), "selected": len(run.job_ids)}


def latest(owner, source):
    if source is None:
        return {}
    query = RecommendationRun.objects.filter(owner=owner)
    if isinstance(source, Document):
        query = query.filter(document=source, profile__isnull=True)
    else:
        query = query.filter(profile=source)
    run = query.order_by("-created_at").first()
    return {
        "recommendation_run": run,
        "recommendation_progress": progress(run) if run else None,
        "recommendation_poll": bool(run and run.status in ACTIVE),
        "evaluation_enabled": evaluations.enabled(),
        "recommendation_executions": (
            run.executions.select_related("job").order_by("created_at")[:EVALUATION_LIMIT]
            if run
            else []
        ),
    }


def shortlist(run):
    policy = preferences_for(run.owner_id)
    preferences = {name: getattr(policy, name) for name in evaluations.POLICY_FIELDS}
    if run.profile_id:
        text = "\n".join(fact["text"] for fact in run.profile.facts)
    else:
        from .resumes import extract

        text = extract(run.document)
    cv_stack = technologies(text)
    candidates = []
    scanned = 0
    for job in Job.objects.filter(active=True).order_by("-discovered_at", "-pk")[:SCAN_LIMIT]:
        scanned += 1
        vacancy = {name: getattr(job, name) for name in JOB_FIELDS}
        if preference_blocks(vacancy, preferences):
            continue
        fit = evaluate(vacancy, policy)
        # Overlap orders candidates; it never excludes jobs on its own.
        overlap = len(cv_stack & technologies(job.title + "\n" + job.description))
        candidates.append((len(fit.preferred_matches), overlap, job.pk))
    candidates.sort(reverse=True)
    return [pk for _, _, pk in candidates[:EVALUATION_LIMIT]], scanned


def process(pk):
    now = timezone.now()
    with transaction.atomic():
        run = (
            RecommendationRun.objects.select_for_update(skip_locked=True)
            .filter(pk=pk, status__in=ACTIVE)
            .filter(Q(lease_until__isnull=True) | Q(lease_until__lte=now))
            .first()
        )
        if run is None:
            return
        run.lease_until = now + timedelta(minutes=5)
        if run.status == "queued":
            run.status = "preparing"
        run.save(update_fields=["lease_until", "status"])
    lease = run.lease_until
    # No database locks are held while reading CV storage or building snapshots.
    run = (
        RecommendationRun.objects.select_related("owner", "document", "profile")
        .filter(pk=pk, lease_until=lease)
        .first()
    )
    if run is None:
        return

    def persist(**fields):
        # Fence an interrupted worker after a replacement acquires the expired lease.
        return RecommendationRun.objects.filter(pk=pk, lease_until=lease).update(**fields)

    try:
        if not evaluations.enabled():
            raise ValidationError("Evaluation disabled")
        if run.document.owner_id != run.owner_id or (
            run.profile_id
            and (run.profile.owner_id != run.owner_id or run.profile.document_id != run.document_id)
        ):
            raise ValidationError("Invalid source")
        if run.status == "preparing":
            run.job_ids, run.scanned = shortlist(run)
            run.status = "evaluating"
            if not persist(job_ids=run.job_ids, scanned=run.scanned, status=run.status):
                return
        existing = set(run.executions.values_list("job_id", flat=True))
        for job_id in run.job_ids:
            if job_id in existing or job_id in run.rejected_ids:
                continue
            renewed = timezone.now() + timedelta(minutes=5)
            if not persist(lease_until=renewed):
                return
            lease = renewed
            job = Job.objects.filter(pk=job_id, active=True).first()
            try:
                if job is None:
                    raise ValidationError("Job no longer active")
                execution = (
                    evaluations.start(run.owner, job, run.profile_id)
                    if run.profile_id
                    else evaluations.start(run.owner, job, document_id=run.document_id)
                )
            except ValidationError:
                run.rejected_ids.append(job_id)
                if not persist(rejected_ids=run.rejected_ids):
                    return
            else:
                # A crash before attachment reuses evaluations.start's unique input key.
                with transaction.atomic():
                    current = (
                        RecommendationRun.objects.select_for_update()
                        .filter(pk=pk, lease_until=lease)
                        .first()
                    )
                    if current is None:
                        return
                    current.executions.add(execution)
        if not progress(run)["pending"]:
            run.status = "completed"
    except ValidationError, OSError, BotoCoreError, ClientError:
        run.status = "failed"
        run.error_code = "cv_or_configuration_unavailable"
    finally:
        # Unexpected exceptions propagate for observability; release the lease for recovery.
        persist(status=run.status, error_code=run.error_code, lease_until=None)
