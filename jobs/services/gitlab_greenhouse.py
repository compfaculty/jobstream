"""GitLab's official Greenhouse application. Public schema GET; browser-only delivery."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

import phonenumbers
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from jobs.models import Application, AuditEvent

from .network import HTTPStatusError, fetch_public
from .secret_telaviv import PortalRateLimited, PortalRejected


def posting_id(job) -> str:
    match = re.fullmatch(r"greenhouse:gitlab:([1-9][0-9]{0,14})", job.provider_key or "")
    company = job.company_profile
    source = job.employer_source
    if (
        not match
        or company is None
        or not company.website_verified
        or company.official_domain not in {"gitlab.com", "about.gitlab.com"}
        or source is None
        or not source.authoritative
        or source.company_id != company.pk
        or source.adapter != "greenhouse"
        or urlsplit(source.url).scheme != "https"
        or urlsplit(source.url).hostname != "boards-api.greenhouse.io"
        or urlsplit(source.url).path != "/v1/boards/gitlab/jobs"
    ):
        raise ValueError("Not a verified GitLab Greenhouse vacancy")
    return match[1]


def job_url(job) -> str:
    return "https://job-boards.greenhouse.io/gitlab/jobs/" + posting_id(job)


def supported(application) -> bool:
    if application.channel != "portal":
        return False
    try:
        job_url(application.job)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class Schema:
    fingerprint: str
    entries: tuple[dict, ...]
    payload: dict


def project_questions(questions):
    return [
        {
            "label": q["label"],
            "description": q.get("description"),
            "required": q["required"],
            "fields": [
                {
                    "name": f["name"],
                    "type": f["type"],
                    "values": [
                        {"label": v["label"], "value": str(v["value"])} for v in f.get("values", [])
                    ],
                }
                for f in q["fields"]
            ],
        }
        for q in questions
    ]


def inspect_schema(payload: dict, identity: str) -> Schema:
    """Support reviewed text/single-choice questions; never invent consent/demographics."""
    try:
        if str(payload["id"]) != identity or not re.fullmatch(r"[1-9][0-9]{0,14}", identity):
            raise ValueError
        if payload.get("location_questions") or payload.get("demographic_questions"):
            raise PortalRejected("gitlab_additional_questions_require_review")
        for item in payload.get("data_compliance", []) or []:
            if any(
                item.get(k)
                for k in (
                    "requires_consent",
                    "requires_processing_consent",
                    "requires_retention_consent",
                    "demographic_data_consent_applies",
                )
            ):
                raise PortalRejected("gitlab_consent_review_required")
        for section in payload.get("compliance", []) or []:
            if section.get("type") != "eeoc" or any(
                q.get("required") for q in section.get("questions", [])
            ):
                raise PortalRejected("gitlab_compliance_review_required")
        questions = project_questions(payload["questions"])
        if not 1 <= len(questions) <= 80:
            raise ValueError
        entries, names, basics = [], set(), set()
        for q in questions:
            if not isinstance(q["label"], str) or not isinstance(q["required"], bool):
                raise ValueError
            fields = q["fields"]
            field_names = {f["name"] for f in fields}
            if field_names == {"resume", "resume_text"}:
                if any(
                    f["type"] != ("input_file" if f["name"] == "resume" else "textarea")
                    for f in fields
                ):
                    raise ValueError
                basics.add("resume")
                continue
            if field_names == {"cover_letter", "cover_letter_text"} and not q["required"]:
                continue  # No cover letter is sent by this adapter.
            if len(fields) != 1:
                raise PortalRejected("gitlab_field_requires_review")
            f = fields[0]
            name, kind = f["name"], f["type"]
            if name in names or not re.fullmatch(
                r"(?:first_name|last_name|email|phone|question_[0-9]+)", name
            ):
                raise ValueError
            if kind not in {"input_text", "textarea", "multi_value_single_select"}:
                raise PortalRejected("gitlab_field_requires_review")
            if kind == "multi_value_single_select":
                vals = f["values"]
                if (
                    not vals
                    or len(vals) > 500
                    or len({v["value"] for v in vals}) != len(vals)
                    or len({v["label"] for v in vals}) != len(vals)
                ):
                    raise ValueError
            names.add(name)
            basics.add(name)
            entries.append(q)
        if not {"first_name", "last_name", "email", "resume"} <= basics:
            raise ValueError
        bound = {
            k: payload.get(k)
            for k in (
                "id",
                "location_questions",
                "data_compliance",
                "demographic_questions",
                "compliance",
            )
        }
        bound["questions"] = questions
        fingerprint = hashlib.sha256(json.dumps(bound, sort_keys=True).encode()).hexdigest()
        return Schema(fingerprint, tuple(entries), bound)
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise PortalRejected("gitlab_schema_changed") from exc


def validate_answers(answers):
    if (
        not isinstance(answers, dict)
        or len(answers) > 82
        or any(
            not isinstance(k, str) or not isinstance(v, str) or len(v) > 4000
            for k, v in answers.items()
            if k != "_schema"
        )
        or not isinstance(answers.get("_schema"), dict)
        or len(json.dumps(answers)) > 256 * 1024
    ):
        raise ValidationError("Invalid GitLab answers.")
    try:
        schema = inspect_schema(answers["_schema"], str(answers["_schema"]["id"]))
    except (PortalRejected, KeyError, TypeError) as exc:
        raise ValidationError("Review the current GitLab questions.") from exc
    allowed = {q["fields"][0]["name"] for q in schema.entries} | {"_schema", "phone_country"}
    if set(answers) - allowed:
        raise ValidationError("Unsupported GitLab answers.")
    for q in schema.entries:
        f = q["fields"][0]
        value = answers.get(f["name"], "")
        if not isinstance(value, str) or len(value) > 4000 or (q["required"] and not value.strip()):
            raise ValidationError("Complete the required GitLab questions.")
        if (
            value
            and f["type"] == "multi_value_single_select"
            and value not in {v["value"] for v in f["values"]}
        ):
            raise ValidationError("Choose a listed GitLab answer.")
    validate_email(answers["email"])
    if answers.get("phone"):
        region = answers.get("phone_country", "")
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
            raise ValidationError("Use an international phone number and its ISO country code.")


def validate_schema_answers(schema, answers):
    validate_answers(answers)
    reviewed = inspect_schema(answers["_schema"], str(schema.payload["id"]))
    if reviewed.fingerprint != schema.fingerprint:
        raise ValidationError("GitLab questions changed; review answers again.")


def read_schema(job) -> Schema:
    identity = posting_id(job)
    try:
        payload = json.loads(
            fetch_public(
                f"https://boards-api.greenhouse.io/v1/boards/gitlab/jobs/{identity}?questions=true"
            )
        )
        return inspect_schema(payload, identity)
    except HTTPStatusError as exc:
        if exc.status == 429:
            raise PortalRateLimited(exc.retry_after) from exc
        raise PortalRejected("gitlab_vacancy_unavailable") from exc
    except (OSError, ValueError) as exc:
        raise PortalRejected("gitlab_schema_unavailable") from exc


def approve_portal(user, pk, *, assisted=False):
    from .submissions import InvalidTransition, digest, validate_content

    candidate = Application.objects.select_related("document", "job").get(pk=pk, owner=user)
    if candidate.status not in {"draft", "failed"}:
        raise InvalidTransition("This application cannot be approved in its current state.")
    validate_content(candidate)
    original = digest(candidate, "")
    from .throttles import defer_portal, portal_pause_seconds

    if portal_pause_seconds("gitlab_greenhouse"):
        raise ValidationError("GitLab requested a pause; try approval later.")
    try:
        schema = read_schema(candidate.job)
    except PortalRateLimited as exc:
        from .retries import RetryWindowTooLong, retry_after_seconds

        try:
            defer_portal(
                timezone.now() + timedelta(seconds=retry_after_seconds(exc.retry_after, 60)),
                "gitlab_greenhouse",
            )
        except RetryWindowTooLong:
            pass
        raise ValidationError("GitLab requested a pause; try approval later.") from exc
    except PortalRejected as exc:
        raise ValidationError("GitLab form unavailable or needs review; try again later.") from exc
    validate_schema_answers(schema, candidate.portal_answers)
    if schema.fingerprint != candidate.portal_fingerprint:
        raise ValidationError("GitLab questions changed; edit and review the draft again.")
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
        application.assisted_delivery = assisted
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


def send(application, attachment, *, on_human_verification=None):
    from .gitlab_browser import deliver

    validate_answers(application.portal_answers)
    if not attachment or not attachment.startswith(b"%PDF-") or len(attachment) > 5 * 1024 * 1024:
        raise PortalRejected("gitlab_pdf_required")
    schema = read_schema(application.job)
    if schema.fingerprint != application.portal_fingerprint:
        raise PortalRejected("gitlab_form_changed")
    try:
        validate_schema_answers(schema, application.portal_answers)
    except ValidationError as exc:
        raise PortalRejected("gitlab_form_changed") from exc
    args = (posting_id(application.job), application.portal_answers, attachment, schema)
    if application.assisted_delivery:
        return deliver(*args, on_human_verification=on_human_verification, assisted=True)
    return deliver(*args)
