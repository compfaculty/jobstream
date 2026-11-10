"""Verified Camunda Ashby board: public form schema and approved browser delivery."""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta

from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator, validate_email
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from jobs.models import Application, AuditEvent

from .network import HTTPStatusError, request_public_document
from .secret_telaviv import PortalRateLimited, PortalRejected

PROVIDER = "camunda_ashby"
API = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"
FORM_QUERY = """query ApiJobPosting($organizationHostedJobsPageName:String!,$jobPostingId:String!){
 jobPosting(organizationHostedJobsPageName:$organizationHostedJobsPageName,jobPostingId:$jobPostingId){
 id isListed applicationLimitCalloutHtml shouldAskForTextingConsent shouldAskForWhatsAppConsent
 candidateTextingPrivacyPolicyUrl candidateTextingTermsAndConditionsUrl
 automatedProcessingLegalNotice{automatedProcessingLegalNoticeHtml}
 applicationForm{...FormParts} surveyForms{...FormParts}
 }} fragment FormParts on FormRender{
 id sourceFormDefinitionId sections{title descriptionHtml isHidden
 fieldEntries{id field isRequired descriptionHtml isHidden}}}
"""
TYPES = {"String", "Email", "Phone", "Location", "Boolean", "ValueSelect", "MultiValueSelect"}
LIMIT_TEXT = (
    "Please Note: we have set up limits for applications for this role. "
    "The following limits apply to applications for all jobs: "
    "Candidates may not apply more than 2 times in any 30 day span for any job "
    "Candidates may not re-apply to the same role within 60 days if not presented with an offer"
)


def posting_id(job):
    key = job.provider_key or ""
    if not key.startswith("ashby:camunda:"):
        raise ValueError("Not a Camunda posting")
    identity = str(uuid.UUID(key.removeprefix("ashby:camunda:")))
    company, source = job.company_profile, job.employer_source
    if (
        company is None
        or not company.website_verified
        or company.official_domain != "camunda.com"
        or source is None
        or not source.authoritative
        or source.company_id != company.pk
        or source.adapter != "ashby"
        or source.url != "https://api.ashbyhq.com/posting-api/job-board/camunda"
    ):
        raise ValueError("Unverified Camunda endpoint")
    return identity


def job_url(job):
    return f"https://jobs.ashbyhq.com/camunda/{posting_id(job)}/application"


def supported(application):
    if application.channel != "portal":
        return False
    try:
        job_url(application.job)
    except ValueError, AttributeError:
        return False
    return True


def plain(html):
    return " ".join(BeautifulSoup(html or "", "html.parser").get_text(" ").split())


@dataclass(frozen=True)
class Schema:
    fingerprint: str
    entries: tuple[dict, ...]
    payload: dict


def inspect_schema(payload, identity):
    try:
        payload = {
            key: payload.get(key)
            for key in (
                "id",
                "isListed",
                "applicationLimitCalloutHtml",
                "shouldAskForTextingConsent",
                "shouldAskForWhatsAppConsent",
                "candidateTextingPrivacyPolicyUrl",
                "candidateTextingTermsAndConditionsUrl",
                "automatedProcessingLegalNotice",
                "applicationForm",
                "surveyForms",
            )
        }
        notice = payload["automatedProcessingLegalNotice"]
        if notice:
            payload["automatedProcessingLegalNotice"] = {
                "automatedProcessingLegalNoticeHtml": notice.get(
                    "automatedProcessingLegalNoticeHtml"
                )
            }

        def project(form):
            return {
                "id": form["id"],
                "sourceFormDefinitionId": form["sourceFormDefinitionId"],
                "sections": [
                    {
                        "title": s.get("title"),
                        "descriptionHtml": s.get("descriptionHtml"),
                        "isHidden": s.get("isHidden"),
                        "fieldEntries": [
                            {
                                key: e.get(key)
                                for key in (
                                    "id",
                                    "field",
                                    "isRequired",
                                    "descriptionHtml",
                                    "isHidden",
                                )
                            }
                            for e in s["fieldEntries"]
                        ],
                    }
                    for s in form["sections"]
                ],
            }

        payload["applicationForm"] = project(payload["applicationForm"])
        payload["surveyForms"] = [project(f) for f in payload["surveyForms"]]
        if payload["id"] != identity or payload["isListed"] is not True:
            raise ValueError
        if plain(payload.get("applicationLimitCalloutHtml")) != LIMIT_TEXT:
            raise PortalRejected("camunda_application_limits_changed")
        if payload.get("shouldAskForWhatsAppConsent"):
            raise PortalRejected("camunda_whatsapp_review_required")
        entries = []
        paths = set()
        for form in [payload["applicationForm"], *payload["surveyForms"]]:
            for section in form["sections"]:
                if section.get("isHidden"):
                    raise PortalRejected("camunda_conditional_form_requires_review")
                for entry in section["fieldEntries"]:
                    field = entry["field"]
                    path, kind = field["path"], field["type"]
                    if (
                        not re.fullmatch(r"[_a-zA-Z0-9-]{1,100}", path)
                        or not re.fullmatch(r"[_a-zA-Z0-9-]{1,200}", entry["id"])
                        or not isinstance(entry["isRequired"], bool)
                        or path in paths
                        or entry.get("isHidden")
                    ):
                        raise ValueError
                    paths.add(path)
                    if kind == "File":
                        if path != "_systemfield_resume":
                            raise PortalRejected("camunda_additional_upload_requires_review")
                    elif kind not in TYPES:
                        raise PortalRejected("camunda_field_type_requires_review")
                    if kind in {"ValueSelect", "MultiValueSelect"}:
                        choices = field["selectableValues"]
                        if not choices or any(
                            not isinstance(v["label"], str) or not isinstance(v["value"], str)
                            for v in choices
                        ):
                            raise ValueError
                    entries.append(entry)
        if (
            not {
                "_systemfield_name",
                "_systemfield_email",
                "_systemfield_resume",
                "_systemfield_location",
            }
            <= paths
        ):
            raise ValueError
        # Render/session identifiers change on every read; question identities and meaning do not.
        bound = json.loads(json.dumps(payload))
        for form in [bound["applicationForm"], *bound["surveyForms"]]:
            form.pop("id")
            for section in form["sections"]:
                for entry in section["fieldEntries"]:
                    entry.pop("id")
                    entry["field"].pop("id", None)
        fingerprint = hashlib.sha256(json.dumps(bound, sort_keys=True).encode()).hexdigest()
        return Schema(fingerprint, tuple(entries), payload)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise PortalRejected("camunda_schema_changed") from exc


def read_schema(job):
    identity = posting_id(job)
    try:
        body, _ = request_public_document(
            API,
            payload={
                "operationName": "ApiJobPosting",
                "query": FORM_QUERY,
                "variables": {
                    "organizationHostedJobsPageName": "camunda",
                    "jobPostingId": identity,
                },
            },
        )
        response = json.loads(body)
        if response.get("errors"):
            raise ValueError
        return inspect_schema(response["data"]["jobPosting"], identity)
    except HTTPStatusError as exc:
        if exc.status == 429:
            raise PortalRateLimited(exc.retry_after) from exc
        raise PortalRejected("camunda_form_unavailable") from exc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PortalRejected("camunda_schema_unavailable") from exc


def _validate_envelope(answers):
    # Detailed validation uses the current schema at approval and again before upload.
    if (
        not isinstance(answers, dict)
        or not isinstance(answers.get("_schema"), dict)
        or len(answers) > 100
        or any(
            not isinstance(k, str)
            or not isinstance(v, (str, list))
            or (isinstance(v, str) and len(v) > 2000)
            or (
                isinstance(v, list)
                and (len(v) > 100 or any(not isinstance(s, str) or len(s) > 500 for s in v))
            )
            for k, v in answers.items()
            if k != "_schema"
        )
    ):
        raise ValidationError("Invalid Camunda answers.")
    if len(json.dumps(answers)) > 256 * 1024:
        raise ValidationError("Camunda answers exceed the size limit.")


def validate_answers(answers):
    _validate_envelope(answers)
    try:
        schema = inspect_schema(answers["_schema"], answers["_schema"]["id"])
    except (PortalRejected, KeyError) as exc:
        raise ValidationError("Review the current Camunda questions.") from exc
    validate_schema_answers(schema, answers)


def validate_schema_answers(schema, answers):
    _validate_envelope(answers)
    if inspect_schema(answers["_schema"], schema.payload["id"]).fingerprint != schema.fingerprint:
        raise ValidationError("Camunda form changed; review the draft again.")
    allowed = {e["field"]["path"] for e in schema.entries if e["field"]["type"] != "File"}
    allowed.add("_schema")
    if schema.payload.get("shouldAskForTextingConsent"):
        allowed.add("sms_consent")
    if set(answers) - allowed:
        raise ValidationError("Camunda questions changed; review the draft again.")
    for entry in schema.entries:
        field = entry["field"]
        kind, path = field["type"], field["path"]
        if kind == "File":
            continue
        value = answers.get(path, [] if kind == "MultiValueSelect" else "")
        if entry["isRequired"] and not value:
            raise ValidationError(f"Answer required: {field['title']}")
        if kind == "MultiValueSelect":
            if not isinstance(value, list) or set(value) - {
                v["value"] for v in field["selectableValues"]
            }:
                raise ValidationError("Invalid Camunda selection.")
        elif not isinstance(value, str):
            raise ValidationError("Invalid Camunda answer type.")
        elif value:
            if kind == "Boolean" and value not in {"yes", "no"}:
                raise ValidationError("Choose Yes or No explicitly.")
            if kind == "ValueSelect" and value not in {
                v["value"] for v in field["selectableValues"]
            }:
                raise ValidationError("Invalid Camunda selection.")
            if kind == "Email":
                validate_email(value)
            if field["title"] == "LinkedIn Profile":
                URLValidator(schemes=["https"])(value)
    if answers.get("sms_consent", "") not in {"", "yes", "no"}:
        raise ValidationError("Invalid SMS consent choice.")


def validate_limits(application):
    """Reserve a user's slots on approval; uncertain sends consume capacity too."""
    if connection.in_atomic_block:
        get_user_model().objects.select_for_update(no_key=True).get(pk=application.owner_id)
    history = (
        Application.objects.filter(
            owner_id=application.owner_id,
            job__provider_key__startswith="ashby:camunda:",
        )
        .filter(
            Q(status__in=["approved", "submitting", "confirmed", "unknown"])
            | Q(
                tracking_status__in=[
                    "applied",
                    "in_process",
                    "ghosted",
                    "rejected",
                    "withdrawn",
                    "interview",
                    "offer",
                ]
            )
        )
        .exclude(pk=application.pk)
    )
    now = timezone.now()
    recent = history.filter(
        Q(started_at__gte=now - timedelta(days=30))
        | Q(approved_at__gte=now - timedelta(days=30))
        # Manual tracking lacks an exact submission time; count recent activity conservatively.
        | Q(
            started_at__isnull=True,
            approved_at__isnull=True,
            updated_at__gte=now - timedelta(days=30),
        )
    )
    if recent.count() >= 2:
        raise ValidationError(
            "Camunda permits at most two applications in 30 days; queued and uncertain sends count."
        )
    if (
        history.filter(job__provider_key=application.job.provider_key)
        .filter(
            Q(started_at__gte=now - timedelta(days=60))
            | Q(approved_at__gte=now - timedelta(days=60))
        )
        .exists()
    ):
        raise ValidationError("Camunda does not permit reapplying to this role within 60 days.")


def approve_portal(user, pk, *, assisted=False):
    from .submissions import InvalidTransition, digest, validate_content
    from .throttles import portal_pause_seconds

    candidate = Application.objects.select_related("document", "job").get(pk=pk, owner=user)
    if candidate.status not in {"draft", "failed"}:
        raise InvalidTransition("This application cannot be approved in its current state.")
    validate_content(candidate)
    original = digest(candidate, "")
    if portal_pause_seconds(PROVIDER):
        raise ValidationError("Camunda requested a pause; try approval later.")
    try:
        schema = read_schema(candidate.job)
    except PortalRateLimited as exc:
        from .retries import RetryWindowTooLong, retry_after_seconds
        from .throttles import defer_portal

        try:
            defer_portal(
                timezone.now() + timedelta(seconds=retry_after_seconds(exc.retry_after, 60)),
                PROVIDER,
            )
        except RetryWindowTooLong:
            pass
        raise ValidationError("Camunda requested a pause; try approval later.") from exc
    except PortalRejected as exc:
        raise ValidationError("Camunda form unavailable; review it later.") from exc
    if candidate.portal_fingerprint != schema.fingerprint:
        raise ValidationError(
            "Camunda form changed; open Edit draft and review its current questions."
        )
    validate_schema_answers(schema, candidate.portal_answers)
    with transaction.atomic():
        # Same order as dispatch and batch approval: owner quota lock, then application.
        get_user_model().objects.select_for_update(no_key=True).get(pk=candidate.owner_id)
        app = (
            Application.objects.select_for_update(of=("self",))
            .select_related("document", "job")
            .get(pk=pk, owner=user)
        )
        if app.status not in {"draft", "failed"} or digest(app, "") != original:
            raise InvalidTransition("Application changed during review.")
        validate_content(app)
        app.portal_url = job_url(app.job)
        app.assisted_delivery = assisted
        app.approval_digest = digest(app, "")
        app.approved_at = app.next_dispatch_at = timezone.now()
        app.retry_not_before = None
        app.send_attempts = 0
        app.status, app.error_code = "approved", ""
        app.save()
        AuditEvent.objects.create(actor=user, application=app, event="application.approved")
        return app


def send(application, attachment, *, on_human_verification=None):
    from .ashby_browser import deliver

    if not attachment or not attachment.startswith(b"%PDF-") or len(attachment) > 5 * 1024 * 1024:
        raise PortalRejected("camunda_pdf_required")
    schema = read_schema(application.job)
    if schema.fingerprint != application.portal_fingerprint:
        raise PortalRejected("camunda_form_changed")
    try:
        validate_schema_answers(schema, application.portal_answers)
    except ValidationError as exc:
        raise PortalRejected("camunda_answers_require_review") from exc
    args = (posting_id(application.job), application.portal_answers, attachment, schema)
    if application.assisted_delivery:
        return deliver(*args, on_human_verification=on_human_verification, assisted=True)
    return deliver(*args)
