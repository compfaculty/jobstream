"""Durable batch preparation. Preparation never approves or sends an application."""

import hashlib
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from jobs.models import (
    Application,
    ApplicationBatch,
    AuditEvent,
    BatchItem,
    Document,
    Job,
    ResumeProfile,
)

from .submissions import approve


@transaction.atomic
def create(
    owner,
    document_id,
    job_ids,
    request_key,
    profile_id=None,
    use_ai=False,
    portal_answers=None,
    check_fit=False,
):
    get_user_model().objects.select_for_update().get(pk=owner.pk)
    existing = ApplicationBatch.objects.filter(owner=owner, request_key=request_key).first()
    if existing:
        return existing
    document = Document.objects.filter(owner=owner, pk=document_id).first()
    ids = set(job_ids)
    jobs = list(Job.objects.filter(pk__in=ids, active=True))
    if not document or not ids or len(ids) > 100 or len(jobs) != len(ids):
        raise ValidationError("Select your CV and between 1 and 100 active jobs.")
    if portal_answers:
        from .secret_telaviv import validate_answers

        validate_answers(portal_answers)
    profile = None
    if profile_id:
        profile = ResumeProfile.objects.filter(
            pk=profile_id, owner=owner, document=document
        ).first()
        if not profile:
            raise ValidationError("Select a reviewed profile belonging to the base CV.")
    if use_ai:
        from .ai import configured

        if not profile or not configured():
            raise ValidationError(
                "AI tailoring requires a reviewed profile and configured OpenAI provider."
            )
    batch = ApplicationBatch.objects.create(
        owner=owner,
        document=document,
        request_key=request_key,
        profile=profile,
        use_ai=use_ai,
        check_fit=check_fit,
        portal_answers=portal_answers or {},
        document_sha256=document.sha256,
    )
    BatchItem.objects.bulk_create(
        [
            BatchItem(
                batch=batch,
                job=job,
                job_snapshot={
                    "title": job.title,
                    "company": job.company,
                    "description": job.description,
                    "normalized": job.normalized,
                    "location": job.location,
                    "recipient": job.apply_email,
                    "apply_url": job.apply_url,
                    "portal_access": job.portal_access,
                },
            )
            for job in jobs
        ]
    )
    AuditEvent.objects.create(actor=owner, event="batch.created")
    return batch


def prepare(item_id):
    from .ai import AIUnavailable
    from .resumes import tailor
    from .retries import RetryWindowTooLong, retry_after_seconds
    from .secret_telaviv import PortalRateLimited, PortalRejected, SecretSession, read_form

    token = uuid.uuid4()
    with transaction.atomic():
        item = (
            BatchItem.objects.select_for_update(of=("self",))
            .select_related("batch__profile", "batch__document", "job")
            .get(pk=item_id)
        )
        now = timezone.now()
        if item.next_attempt_at > now:
            return
        if item.state == "preparing" and item.lease_until and item.lease_until > now:
            return
        if item.state not in {"queued", "preparing"}:
            return
        if item.batch.cancelled_at:
            item.state = "cancelled"
            item.save(update_fields=["state"])
            return
        existing = Application.objects.filter(
            owner_id=item.batch.owner_id, job_id=item.job_id
        ).first()
        if existing:
            item.application = existing
            item.state = "existing"
            item.save(update_fields=["application", "state"])
            return
        if item.attempts >= 3:
            item.state = "failed"
            item.error_code = "preparation_retries_exhausted"
            item.save(update_fields=["state", "error_code"])
            return
        item.state = "preparing"
        item.lease_token = token
        item.lease_until = now + timedelta(minutes=6)
        item.attempts += 1
        item.save()
    # Expensive extraction/generation runs without row locks, allowing jobs to prepare concurrently.
    generated = None
    provenance = {}
    job = item.job_snapshot or {
        "title": item.job.title,
        "company": item.job.company,
        "description": item.job.description,
        "recipient": item.job.apply_email,
    }
    from .matching import FitResult
    from .preference_matching import evaluate, preferences_for

    policy = preferences_for(item.batch.owner_id)
    fit = evaluate(job, policy)
    if fit.state == "eligible" and item.batch.check_fit:
        from .resumes import extract

        try:
            cv_text = (
                "\n".join(fact["text"] for fact in item.batch.profile.facts)
                if item.batch.profile_id
                else extract(item.batch.document)
            )
            fit = evaluate(job, policy, cv_text)
        except ValidationError, OSError:
            fit = FitResult("needs_action", "fit_cv_needs_review")
    provenance["fit"] = fit.as_dict()
    if fit.state != "eligible":
        with transaction.atomic():
            current = BatchItem.objects.select_for_update().select_related("batch").get(pk=item_id)
            if current.lease_token == token and current.state == "preparing":
                current.state = "cancelled" if current.batch.cancelled_at else fit.state
                current.error_code = fit.reason
                current.provenance = provenance
                current.lease_until = None
                current.save()
        return
    error = ""
    portal_fingerprint = ""
    cato_portal = False
    camunda_portal = False
    gitlab_portal = False
    applicant_answers = item.batch.portal_answers
    retry_delay = 60
    body = (
        f"Hello,\n\nPlease consider my application for {job['title']}. "
        "My CV is attached for your review.\n\nThank you for your consideration."
    )
    try:
        with item.batch.document.file.open("rb") as stream:
            digest = hashlib.sha256(stream.read(5 * 1024 * 1024 + 1)).hexdigest()
        if digest != (item.batch.document_sha256 or item.batch.document.sha256):
            raise ValidationError("Base CV changed; upload and review again.")
        if item.batch.profile_id:
            generated, tailoring, body = tailor(item.batch.profile, job, use_ai=item.batch.use_ai)
            provenance.update(tailoring)
        from .secret_telaviv import secret_url

        try:
            portal_url = secret_url(job.get("apply_url") or item.job.canonical_url)
        except ValueError:
            portal_url = ""
        from . import camunda_ashby, cato_greenhouse, gitlab_greenhouse

        if not job.get("recipient"):
            try:
                camunda_url = camunda_ashby.job_url(item.job)
            except ValueError:
                camunda_url = ""
            if camunda_url:
                camunda_portal = True
                portal_url = camunda_url
                schema = camunda_ashby.read_schema(item.job)
                portal_fingerprint = schema.fingerprint
                applicant_answers = {"_schema": schema.payload}
            try:
                cato_url = cato_greenhouse.job_url(item.job)
            except ValueError:
                cato_url = ""
            if cato_url:
                cato_portal = True
                portal_url = cato_url
                portal_fingerprint = cato_greenhouse.read_schema(item.job).fingerprint
                from .saved_answers import read_answers

                saved, reuse = read_answers(item.batch.owner_id)
                applicant_answers = {
                    key: value
                    for key, value in saved.items()
                    if reuse and key in cato_greenhouse.FIELDS
                }
        if not job.get("recipient"):
            try:
                gitlab_url = gitlab_greenhouse.job_url(item.job)
            except ValueError:
                gitlab_url = ""
            if gitlab_url:
                gitlab_portal = True
                portal_url = gitlab_url
                schema = gitlab_greenhouse.read_schema(item.job)
                portal_fingerprint = schema.fingerprint
                applicant_answers = {"_schema": schema.payload}
        if (
            not job.get("recipient")
            and portal_url
            and not cato_portal
            and not camunda_portal
            and not gitlab_portal
        ):
            from .throttles import portal_pause_seconds

            pause = portal_pause_seconds()
            if pause:
                raise PortalRateLimited(str(pause))
            assessment, _, _ = read_form(SecretSession(), portal_url)
            portal_fingerprint = assessment.fingerprint
    except PortalRateLimited as exc:
        error = "portal_rate_limited"
        try:
            retry_delay = retry_after_seconds(exc.retry_after, 60)
            from .throttles import defer_portal

            defer_portal(
                timezone.now() + timedelta(seconds=retry_delay),
                "gitlab_greenhouse"
                if gitlab_portal
                else "camunda_ashby"
                if camunda_portal
                else "cato_greenhouse"
                if cato_portal
                else "secret_telaviv",
            )
        except RetryWindowTooLong:
            error = "portal_pause_requires_review"
    except PortalRejected as exc:
        error = str(exc)
    except AIUnavailable as exc:
        error = exc.code
        retry_delay = exc.delay
    except ValidationError:
        error = "cv_validation_failed"
    except Exception:
        # Safe diagnostic category only; CV content never enters logs.
        error = "preparation_unavailable"
    try:
        with transaction.atomic():
            get_user_model().objects.select_for_update().get(pk=item.batch.owner_id)
            current = (
                BatchItem.objects.select_for_update(of=("self",))
                .select_related("batch")
                .get(pk=item_id)
            )
            if current.lease_token != token or current.state != "preparing":
                return
            if current.batch.cancelled_at:
                current.state = "cancelled"
            elif error:
                current.state = (
                    "needs_action"
                    if error.startswith(("cato_", "camunda_", "gitlab_"))
                    or error
                    in {
                        "cv_validation_failed",
                        "portal_pause_requires_review",
                        "portal_form_changed",
                        "portal_terms_changed",
                        "portal_hidden_fields_changed",
                        "ai_not_configured",
                        "ai_retry_window_requires_review",
                        "ai_input_budget_exceeded",
                        "ai_unsupported_claim",
                        "ai_http_401",
                        "ai_http_403",
                        "ai_http_400",
                    }
                    else "queued"
                )
                current.error_code = error
                current.next_dispatch_at = timezone.now() + timedelta(
                    seconds=max(retry_delay, 60 * 2**current.attempts)
                )
                current.next_attempt_at = current.next_dispatch_at
            else:
                application = Application.objects.filter(
                    owner_id=item.batch.owner_id, job=item.job
                ).first()
                if application:
                    current.application = application
                    current.state = "existing"
                elif not Job.objects.filter(pk=item.job_id, active=True).exists():
                    current.state = "needs_action"
                    current.error_code = "vacancy_inactive"
                else:
                    current.application = Application.objects.create(
                        owner_id=item.batch.owner_id,
                        job=item.job,
                        document=generated or item.batch.document,
                        recipient=job.get("recipient", ""),
                        channel="email" if job.get("recipient") else "portal",
                        portal_access=job.get("portal_access", "unknown"),
                        portal_url=portal_url or job.get("apply_url", ""),
                        portal_fingerprint=portal_fingerprint,
                        portal_answers=applicant_answers,
                        subject=f"Application: {job['title']}"[:250],
                        body=body,
                    )
                    current.provenance = provenance
                    current.state = "ready" if job.get("recipient") else "needs_action"
                    current.error_code = "" if job.get("recipient") else "manual_portal_required"
                    from .portal_delivery import adapter

                    provider = adapter(current.application)
                    if provider is not None:
                        try:
                            provider.validate_answers(current.application.portal_answers)
                        except ValidationError:
                            current.error_code = (
                                "gitlab_details_required"
                                if gitlab_portal
                                else "camunda_details_required"
                                if camunda_portal
                                else "cato_details_required"
                                if cato_portal
                                else "secret_telaviv_details_required"
                            )
                        else:
                            current.state = "ready"
                            current.error_code = ""
                    AuditEvent.objects.create(
                        actor_id=item.batch.owner_id,
                        application=current.application,
                        event="application.batch_prepared",
                    )
            current.lease_until = None
            current.save()
    finally:
        # Cached preparations are retained for reuse; only uncached orphans are removed.
        if (
            generated
            and not generated.prepared_resumes.exists()
            and not Application.objects.filter(document=generated).exists()
        ):
            generated.file.delete(save=False)
            generated.delete()


@transaction.atomic
def approve_items(owner, batch_id, item_ids):
    batch = ApplicationBatch.objects.select_for_update().get(pk=batch_id, owner=owner)
    ids = set(item_ids)
    items = list(batch.items.select_for_update().filter(pk__in=ids))
    if batch.cancelled_at or not ids or len(items) != len(ids):
        raise ValidationError("Invalid or cancelled batch selection.")
    if any(item.state != "ready" for item in items):
        raise ValidationError("Only prepared applications can be approved.")
    for item in items:
        approve(owner, item.application_id)
        item.state = "approved"
        item.save(update_fields=["state"])


@transaction.atomic
def cancel(owner, batch_id):
    batch = ApplicationBatch.objects.select_for_update().get(pk=batch_id, owner=owner)
    batch.cancelled_at = timezone.now()
    batch.save(update_fields=["cancelled_at"])
    items = list(batch.items.select_for_update().order_by("pk"))
    for item in items:
        if item.state in {"queued", "preparing", "ready"}:
            item.state = "cancelled"
        elif item.state == "approved" and item.application_id:
            application = Application.objects.select_for_update().get(pk=item.application_id)
            if application.status == Application.Status.APPROVED:
                application.status = Application.Status.DRAFT
                application.approval_digest = ""
                application.approved_at = None
                application.save()
                item.state = "cancelled"
                AuditEvent.objects.create(
                    actor=owner, application=application, event="application.batch_cancelled"
                )
        item.save(update_fields=["state"])


def refresh_ready(application_id):
    from .portal_delivery import adapter

    application = Application.objects.select_related("job").get(pk=application_id)
    provider = adapter(application)
    if application.status != "draft" or provider is None:
        return
    try:
        provider.validate_answers(application.portal_answers)
    except ValidationError:
        return
    BatchItem.objects.filter(
        application=application, state="needs_action", batch__cancelled_at__isnull=True
    ).update(state="ready", error_code="")


@transaction.atomic
def recheck_fit(owner, batch_id):
    batch = ApplicationBatch.objects.select_for_update().get(pk=batch_id, owner=owner)
    if batch.cancelled_at:
        raise ValidationError("This batch is cancelled.")
    batch.items.filter(
        state__in=["skipped", "needs_action"],
        error_code__startswith="fit_",
        application__isnull=True,
    ).update(
        state="queued",
        error_code="",
        attempts=0,
        lease_until=None,
        next_dispatch_at=timezone.now(),
        next_attempt_at=timezone.now(),
    )
