"""Cato's official Greenhouse application. Public schema GET; browser-only delivery."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

import phonenumbers
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator, validate_email
from django.db import transaction
from django.utils import timezone

from jobs.models import Application, AuditEvent

from .network import HTTPStatusError, fetch_public
from .secret_telaviv import PortalRateLimited, PortalRejected

FIELDS = frozenset(
    {"first_name", "last_name", "email", "phone", "phone_country", "city", "country", "linkedin"}
)


def posting_id(job) -> str:
    match = re.fullmatch(r"greenhouse:catonetworks:([1-9][0-9]{0,14})", job.provider_key or "")
    company = job.company_profile
    source = job.employer_source
    if (
        not match
        or company is None
        or not company.website_verified
        or company.official_domain != "catonetworks.com"
        or source is None
        or not source.authoritative
        or source.company_id != company.pk
        or source.adapter != "greenhouse"
        or urlsplit(source.url).hostname != "boards-api.greenhouse.io"
        or urlsplit(source.url).path != "/v1/boards/catonetworks/jobs"
    ):
        raise ValueError("Not a verified Cato Greenhouse vacancy")
    return match[1]


def job_url(job) -> str:
    return "https://job-boards.greenhouse.io/embed/job_app?for=catonetworks&token=" + posting_id(
        job
    )


def supported(application) -> bool:
    if application.channel != "portal":
        return False
    try:
        job_url(application.job)
    except ValueError:
        return False
    return True


def validate_answers(answers: dict) -> None:
    if not isinstance(answers, dict) or set(answers) - FIELDS:
        raise ValidationError("Unsupported Cato application answers.")
    if any(not isinstance(v, str) or len(v) > 500 for v in answers.values()):
        raise ValidationError("Invalid Cato application answers.")
    if any(not answers.get(k, "").strip() for k in FIELDS - {"linkedin"}):
        raise ValidationError("Cato needs name, email, phone/country and current city/country.")
    validate_email(answers["email"])
    region = answers["phone_country"].upper()
    try:
        number = phonenumbers.parse(answers["phone"], None)
        valid = (
            answers["phone"].startswith("+")
            and region in phonenumbers.SUPPORTED_REGIONS
            and phonenumbers.is_valid_number(number)
            and number.country_code == phonenumbers.country_code_for_region(region)
        )
    except phonenumbers.NumberParseException:
        valid = False
    if not valid:
        raise ValidationError("Use a valid international phone number and its country code.")
    if answers.get("linkedin"):
        URLValidator(schemes=["https"])(answers["linkedin"])


@dataclass(frozen=True)
class Schema:
    fingerprint: str
    linkedin_field: str = ""
    linkedin_required: bool = False


def inspect_schema(payload: dict, identity: str) -> Schema:
    """Unknown required fields/consents stop automation; optional answers stay blank."""
    try:
        if str(payload["id"]) != identity or not isinstance(payload["questions"], list):
            raise ValueError
        questions = payload["questions"]
        location = payload.get("location_questions", [])
        if not isinstance(location, list):
            raise ValueError
        linkedin = ""
        linkedin_required = False
        known = {"first_name", "last_name", "email", "phone", "resume", "resume_text"}
        basics = set()
        for question in questions:
            fields = question["fields"]
            names = {field["name"] for field in fields}
            if not fields or any(not isinstance(name, str) for name in names):
                raise ValueError
            basics |= names & {"first_name", "last_name", "email", "phone"}
            if (
                question["label"].strip().casefold() == "linkedin profile"
                and len(fields) == 1
                and fields[0]["type"] == "input_text"
                and re.fullmatch(r"question_[0-9]+", fields[0]["name"])
            ):
                linkedin = fields[0]["name"]
                linkedin_required = bool(question["required"])
            elif question["required"] and not names <= known:
                raise PortalRejected("cato_additional_answers_required")
            elif names & known and not names <= known:
                raise ValueError
        if basics != {"first_name", "last_name", "email", "phone"}:
            raise ValueError
        for question in location:
            if not {field["name"] for field in question["fields"]} <= {
                "location",
                "latitude",
                "longitude",
            }:
                raise PortalRejected("cato_location_schema_changed")
        for compliance in payload.get("data_compliance", []) or []:
            if any(
                compliance.get(key)
                for key in (
                    "requires_consent",
                    "requires_processing_consent",
                    "requires_retention_consent",
                    "demographic_data_consent_applies",
                )
            ):
                raise PortalRejected("cato_consent_review_required")
        if payload.get("demographic_questions") or payload.get("compliance"):
            raise PortalRejected("cato_additional_answers_required")
        bound = {
            key: payload.get(key)
            for key in (
                "id",
                "questions",
                "location_questions",
                "data_compliance",
                "demographic_questions",
                "compliance",
            )
        }
        fingerprint = hashlib.sha256(json.dumps(bound, sort_keys=True).encode()).hexdigest()
        return Schema(fingerprint, linkedin, linkedin_required)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise PortalRejected("cato_schema_changed") from exc


def read_schema(job) -> Schema:
    identity = posting_id(job)
    try:
        payload = json.loads(
            fetch_public(
                f"https://boards-api.greenhouse.io/v1/boards/catonetworks/jobs/{identity}?questions=true"
            )
        )
        return inspect_schema(payload, identity)
    except HTTPStatusError as exc:
        if exc.status == 429:
            raise PortalRateLimited(exc.retry_after) from exc
        raise PortalRejected("cato_vacancy_unavailable") from exc
    except (OSError, ValueError) as exc:
        raise PortalRejected("cato_schema_unavailable") from exc


def approve_portal(user, pk):
    from .submissions import InvalidTransition, digest, validate_content

    candidate = Application.objects.select_related("document", "job").get(pk=pk, owner=user)
    if candidate.status not in {"draft", "failed"}:
        raise InvalidTransition("This application cannot be approved in its current state.")
    validate_content(candidate)
    original = digest(candidate, "")
    from .throttles import defer_portal, portal_pause_seconds

    if portal_pause_seconds("cato_greenhouse"):
        raise ValidationError("Cato requested a pause; try approval later.")
    try:
        schema = read_schema(candidate.job)
    except PortalRateLimited as exc:
        from .retries import RetryWindowTooLong, retry_after_seconds

        try:
            defer_portal(
                timezone.now() + timedelta(seconds=retry_after_seconds(exc.retry_after, 60)),
                "cato_greenhouse",
            )
        except RetryWindowTooLong:
            pass
        raise ValidationError("Cato requested a pause; try approval later.") from exc
    except PortalRejected as exc:
        raise ValidationError("Cato form unavailable or needs review; try again later.") from exc
    if schema.linkedin_required and not candidate.portal_answers.get("linkedin"):
        raise ValidationError("This Cato vacancy requires a LinkedIn profile; edit the draft.")
    with transaction.atomic():
        application = (
            Application.objects.select_for_update(of=("self",))
            .select_related("document", "job")
            .get(pk=pk, owner=user)
        )
        if application.status not in {"draft", "failed"} or digest(application, "") != original:
            raise InvalidTransition("Application changed during review; review it again.")
        validate_content(application)
        application.portal_url = job_url(application.job)
        application.portal_fingerprint = schema.fingerprint
        application.approval_digest = digest(application, "")
        application.approved_at = timezone.now()
        application.next_dispatch_at = timezone.now()
        application.retry_not_before = None
        application.send_attempts = 0
        application.status = "approved"
        application.error_code = ""
        application.save()
        AuditEvent.objects.create(actor=user, application=application, event="application.approved")
        return application


def send(application, attachment):
    from .cato_browser import deliver

    validate_answers(application.portal_answers)
    if not attachment or not attachment.startswith(b"%PDF-") or len(attachment) > 5 * 1024 * 1024:
        raise PortalRejected("cato_pdf_required")
    schema = read_schema(application.job)
    if schema.fingerprint != application.portal_fingerprint:
        raise PortalRejected("cato_form_changed")
    if schema.linkedin_required and not application.portal_answers.get("linkedin"):
        raise PortalRejected("cato_linkedin_required")
    return deliver(
        posting_id(application.job), application.portal_answers, attachment, schema.linkedin_field
    )
