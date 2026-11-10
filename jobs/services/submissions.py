"""Approval and durable outbox. UNKNOWN is deliberately never automatically resent."""

import hashlib
import json
import logging
import random
import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
from googleapiclient.errors import HttpError

from jobs.models import Application, AuditEvent, GmailConnection
from jobs.observability.context import context
from jobs.observability.events import phase

from . import gmail
from .retries import RetryWindowTooLong, retry_after_seconds
from .secret_telaviv import PortalRateLimited, PortalRejected, PortalUnknown

logger = logging.getLogger(__name__)


class InvalidTransition(ValidationError):
    pass


def digest(application: Application, sender: str) -> str:
    payload = {
        "recipient": application.recipient,
        "subject": application.subject,
        "body": application.body,
        "document": application.document_id,
        "document_sha256": application.document.sha256
        if application.document is not None
        else None,
        "sender": sender,
    }
    if application.channel == "portal":
        payload.update(
            channel=application.channel,
            portal_url=application.portal_url,
            portal_answers=application.portal_answers,
            portal_fingerprint=application.portal_fingerprint,
        )
    if application.assisted_delivery:
        payload["assisted_delivery"] = True
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def validate_content(application: Application, *, for_send: bool = True) -> None:
    if for_send:
        from .preference_matching import evaluate, preferences_for

        policy = preferences_for(application.owner_id)
        if (
            policy.excluded_technologies
            or policy.location_required
            or policy.title_required
            or policy.technology_required
            or policy.remote_only
        ):
            fit = evaluate(
                {
                    "description": application.job.description,
                    "normalized": application.job.normalized,
                    "title": application.job.title,
                    "location": application.job.location,
                },
                policy,
            )
            if fit.state != "eligible":
                raise ValidationError(
                    "Current application preferences require skipping or reviewing this job."
                )
    if not application.job.active:
        raise ValidationError("This vacancy is no longer active.")
    if for_send and application.channel != "email":
        from .portal_delivery import adapter

        provider = adapter(application)
        if provider is None:
            raise ValidationError(
                "Complete this application on the employer portal and update tracking."
            )
        provider.validate_answers(application.portal_answers)
        from .camunda_ashby import supported as camunda_supported
        from .camunda_ashby import validate_limits

        if camunda_supported(application):
            validate_limits(application)
        if not application.document_id:
            raise ValidationError("Choose a CV for this application.")
    if application.channel == "email":
        validate_email(application.recipient)
    if any(c in application.recipient + application.subject for c in "\r\n"):
        raise ValidationError("Mail headers cannot contain newlines.")
    if not application.subject.strip() or not application.body.strip():
        raise ValidationError("Subject and message are required.")
    if application.document is not None and application.document.owner_id != application.owner_id:
        raise ValidationError("Document does not belong to applicant.")


@phase("approval")
def approve(user, pk: int, *, assisted: bool = False) -> Application:
    candidate = Application.objects.get(pk=pk, owner=user)
    if candidate.channel == "portal":
        from .portal_delivery import adapter

        provider = adapter(candidate)
        if provider is None:
            raise ValidationError("Complete this application on the employer portal.")
        if assisted:
            from .portal_delivery import assisted_supported

            if not assisted_supported(candidate):
                raise ValidationError(
                    "Local assisted delivery requires a supported browser application."
                )
            return provider.approve_portal(user, pk, assisted=True)
        return provider.approve_portal(user, pk)
    if assisted:
        raise ValidationError("Local assisted delivery requires a supported browser application.")
    with transaction.atomic():
        application = (
            Application.objects.select_for_update(of=("self",))
            .select_related("document")
            .get(pk=pk, owner=user)
        )
        if application.status not in {Application.Status.DRAFT, Application.Status.FAILED}:
            raise InvalidTransition("This application cannot be approved in its current state.")
        validate_content(application)
        try:
            connection = GmailConnection.objects.get(owner=user, revoked=False)
        except GmailConnection.DoesNotExist as exc:
            raise ValidationError("Connect Gmail before approving.") from exc
        application.send_attempts = 0
        application.retry_not_before = None
        application.approval_digest = digest(application, connection.email)
        application.approved_at = timezone.now()
        application.next_dispatch_at = timezone.now()
        application.message_id = f"<{uuid.uuid4().hex}@applications.jobstream.invalid>"
        application.status = Application.Status.APPROVED
        application.error_code = ""
        application.save()
        AuditEvent.objects.create(actor=user, application=application, event="application.approved")
        fields = {**context.get(), "outcome": "approved"}
        transaction.on_commit(lambda: logger.info("approval_granted", extra=fields), robust=True)
        return application


def edit(user, pk: int, values: dict) -> Application:
    with transaction.atomic():
        application = Application.objects.select_for_update(of=("self",)).get(pk=pk, owner=user)
        if application.status in {
            Application.Status.SUBMITTING,
            Application.Status.CONFIRMED,
            Application.Status.UNKNOWN,
        }:
            raise InvalidTransition("Submission content is locked; update tracking separately.")
        fields = ["recipient", "subject", "body", "document", "portal_answers"]
        from .camunda_ashby import supported as camunda_supported
        from .gitlab_greenhouse import supported as gitlab_supported

        if camunda_supported(application) or gitlab_supported(application):
            fields.append("portal_fingerprint")
        for field in fields:
            if field in values:
                setattr(application, field, values[field])
        validate_content(application, for_send=False)
        application.assisted_delivery = False
        application.status = Application.Status.DRAFT
        application.approval_digest = ""
        application.approved_at = None
        application.save()
        AuditEvent.objects.create(actor=user, application=application, event="application.edited")
        from .batches import refresh_ready

        transaction.on_commit(lambda: refresh_ready(application.pk))
        return application


def attachment_bytes(application: Application) -> bytes | None:
    if application.document is None:
        return None
    with application.document.file.open("rb") as stream:
        content = stream.read(5 * 1024 * 1024 + 1)
    if (
        len(content) != application.document.size
        or hashlib.sha256(content).hexdigest() != application.document.sha256
    ):
        raise ValidationError("Attachment integrity check failed.")
    return content


@phase("delivery")
def submit(pk: int, *, assisted_owner_id: int | None = None, on_human_verification=None) -> None:
    with transaction.atomic():
        from django.contrib.auth import get_user_model

        quota_owner = (
            Application.objects.filter(
                pk=pk, channel="portal", job__provider_key__startswith="ashby:camunda:"
            )
            .values_list("owner_id", flat=True)
            .first()
        )
        if quota_owner is not None:
            # Serialize quota decisions without blocking completion audit FK references.
            get_user_model().objects.select_for_update(no_key=True).get(pk=quota_owner)
        application = (
            Application.objects.select_for_update(of=("self",))
            .select_related("document")
            .get(pk=pk)
        )
        if assisted_owner_id is not None:
            from .portal_delivery import assisted_supported

            if application.owner_id != assisted_owner_id:
                raise Application.DoesNotExist
            if not application.assisted_delivery or not assisted_supported(application):
                raise InvalidTransition("Approve this application for local assisted delivery.")
        elif application.assisted_delivery:
            logger.info("delivery_skipped", extra={"reason": "assisted_only"})
            return
        if application.status != Application.Status.APPROVED:
            if assisted_owner_id is not None:
                raise InvalidTransition("Application must be approved and not already claimed.")
            logger.info("delivery_skipped", extra={"reason": "not_approved_or_duplicate"})
            return
        if application.retry_not_before and application.retry_not_before > timezone.now():
            logger.info("delivery_deferred", extra={"reason": "retry_window"})
            return
        try:
            connection = None
            sender = ""
            if application.channel == "email":
                connection = GmailConnection.objects.get(owner=application.owner, revoked=False)
                sender = connection.email
            validate_content(application)
            if digest(application, sender) != application.approval_digest:
                raise ValidationError("Approval no longer matches content.")
            attachment = attachment_bytes(application)
        except GmailConnection.DoesNotExist, ValidationError, OSError:
            logger.warning("delivery_preflight_failed", extra={"error_category": "validation"})
            application.status = Application.Status.FAILED
            application.error_code = "preflight_failed"
            application.save()
            AuditEvent.objects.create(
                actor=application.owner,
                application=application,
                event="application.preflight_failed",
            )
            return
        from .throttles import reserve

        if not reserve(connection, application):
            logger.info("delivery_deferred", extra={"reason": "capacity"})
            return
        application.send_attempts += 1
        application.status = Application.Status.SUBMITTING
        application.started_at = timezone.now()
        application.save()
        AuditEvent.objects.create(
            actor=application.owner, application=application, event="application.submitting"
        )
    try:
        if application.channel == "portal":
            from .portal_delivery import adapter

            provider = adapter(application)
            if provider is None:
                raise PortalRejected("unsupported_portal")
            if assisted_owner_id is not None:
                provider_id = provider.send(
                    application, attachment, on_human_verification=on_human_verification
                )
            else:
                provider_id = provider.send(application, attachment)
        else:
            provider_id = gmail.send(connection, application, attachment)
    except KeyboardInterrupt, SystemExit:
        finish(pk, Application.Status.UNKNOWN, error_code="assisted_delivery_interrupted")
        raise
    except PortalRateLimited as exc:
        retry_rate_limited(pk, exc.retry_after)
    except PortalUnknown as exc:
        finish(pk, Application.Status.UNKNOWN, error_code=str(exc), receipt_url=exc.receipt_url)
    except PortalRejected as exc:
        finish(pk, Application.Status.FAILED, error_code=str(exc))
    except gmail.ReconnectRequired:
        finish(pk, Application.Status.FAILED, error_code="gmail_reconnect_required")
    except HttpError as exc:
        # Only explicit rejection is safe to retry with a NEW approval.
        if exc.resp.status == 429:
            retry_rate_limited(pk, exc.resp.get("retry-after", ""))
            return
        rejected = exc.resp.status in {400, 401, 403, 404, 413}
        finish(
            pk,
            Application.Status.FAILED if rejected else Application.Status.UNKNOWN,
            error_code=f"gmail_http_{exc.resp.status}",
        )
    except Exception:
        finish(pk, Application.Status.UNKNOWN, error_code="transport_outcome_unknown")
    else:
        finish(pk, Application.Status.CONFIRMED, provider_id=provider_id)


@phase("delivery_transition")
def finish(
    pk: int, status: str, provider_id: str = "", error_code: str = "", receipt_url: str = ""
) -> None:
    with transaction.atomic():
        application = Application.objects.select_for_update(of=("self",)).get(pk=pk)
        if application.status not in {Application.Status.SUBMITTING, Application.Status.UNKNOWN}:
            return
        application.status = status
        if status == Application.Status.CONFIRMED and application.tracking_status == "interested":
            application.tracking_status = "applied"
        application.provider_id = provider_id
        if receipt_url:
            application.portal_receipt_url = receipt_url
        elif application.channel == "portal" and provider_id.startswith("gitlab_greenhouse:"):
            from .gitlab_greenhouse import posting_id

            identity = posting_id(application.job)
            if provider_id == f"gitlab_greenhouse:{identity}":
                application.portal_receipt_url = (
                    f"https://job-boards.greenhouse.io/gitlab/jobs/{identity}/confirmation"
                )
        elif application.channel == "portal" and provider_id.startswith("secret_telaviv:"):
            receipt_id = provider_id.split(":", 1)[1]
            if receipt_id.isascii() and receipt_id.isdecimal() and application.portal_url:
                application.portal_receipt_url = (
                    application.portal_url.split("?", 1)[0].rstrip("/") + f"/applied/{receipt_id}/"
                )
        application.error_code = error_code
        application.save()
        AuditEvent.objects.create(
            actor=application.owner, application=application, event=f"application.{status}"
        )
        state = status if status in Application.Status.values else "other"
        fields = {**context.get(), "outcome": state}
        transaction.on_commit(
            lambda: logger.info("delivery_state_changed", extra=fields), robust=True
        )


def reconcile(pk: int) -> None:
    application = Application.objects.get(pk=pk)
    if application.status != Application.Status.UNKNOWN:
        return
    if application.channel == "portal":
        from .portal_delivery import browser_supported

        if browser_supported(application):
            # No authenticated read-only receipt lookup exists. Never replay the form.
            return
        if application.portal_receipt_url:
            from .secret_telaviv import reconcile as reconcile_portal

            try:
                provider_id = reconcile_portal(application)
            except Exception:
                return
            if provider_id:
                finish(pk, Application.Status.CONFIRMED, provider_id=provider_id)
        return
    try:
        connection = GmailConnection.objects.get(owner=application.owner, revoked=False)
        provider_id = gmail.find_sent(connection, application.message_id)
    except Exception:
        # No state change on unavailable mailbox; retry the read-only reconciliation later.
        return
    if provider_id:
        finish(pk, Application.Status.CONFIRMED, provider_id=provider_id)


@transaction.atomic
def retry_rate_limited(pk: int, retry_after: str) -> None:
    application = Application.objects.select_for_update().get(pk=pk)
    if application.status != Application.Status.SUBMITTING:
        return
    if application.send_attempts >= 5:
        application.status = Application.Status.FAILED
        application.error_code = "rate_limit_retries_exhausted"
    else:
        delay = min(60 * 2 ** max(application.send_attempts - 1, 0), 3600)
        # Retry jitter is not a security token.
        delay += random.uniform(0, delay * 0.25)  # noqa: S311
        now = timezone.now()
        try:
            delay = retry_after_seconds(retry_after, delay, now=now)
        except RetryWindowTooLong:
            application.status = Application.Status.FAILED
            application.error_code = "rate_limit_pause_requires_review"
            application.save()
            AuditEvent.objects.create(
                actor=application.owner,
                application=application,
                event="application.rate_limit_pause_requires_review",
            )
            return
        application.status = Application.Status.APPROVED
        application.next_dispatch_at = now + timedelta(seconds=delay)
        application.retry_not_before = application.next_dispatch_at
        application.error_code = "rate_limit_retry_scheduled"
        from .throttles import defer_delivery

        defer_delivery(application, application.retry_not_before)
    application.save()
    AuditEvent.objects.create(
        actor=application.owner,
        application=application,
        event=f"application.{application.error_code}",
    )
