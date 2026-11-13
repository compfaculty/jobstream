"""Secret Tel Aviv's native multipart form, ported from compfaculty/jf.

One POST per attempt. Explicit rejection can be retried; uncertain POST outcomes cannot.
"""

import hashlib
import http.client
import json
import re
import secrets
from contextlib import closing
from dataclasses import dataclass
from http.cookiejar import CookieJar
from urllib.parse import urljoin, urlsplit
from urllib.request import Request

from bs4 import BeautifulSoup
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from .network import PinnedHTTPSConnection, canonical_url, public_addresses
from .portals import (
    SECRET_HOST,
    UPLOAD_PARAMS,
    UPLOAD_URL,
    inspect_secret,
    secret_url,
    upload_configuration,
)

TERMS_URL = "https://www.secrettelaviv.com/terms-and-conditions-2"
ANSWER_FIELDS = {
    "first_name",
    "last_name",
    "email",
    "phone",
    "agree_terms",
    "forward_headhunters",
    "terms_url",
}


class PortalRejected(Exception):
    pass


class PortalUnknown(Exception):
    def __init__(self, code, receipt_url=""):
        self.receipt_url = receipt_url
        super().__init__(code)


class PortalRateLimited(Exception):
    def __init__(self, retry_after=""):
        self.retry_after = retry_after
        super().__init__("portal_rate_limited")


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict
    body: bytes


class SecretSession:
    """Cookies are ephemeral; every connection pins public DNS and stays on the portal."""

    def __init__(self):
        self.cookies = CookieJar()

    def request(self, method, url, body=None, content_type=None):
        url = canonical_url(url)
        parts = urlsplit(url)
        if parts.hostname != SECRET_HOST or method not in {"GET", "POST"}:
            raise ValueError("Unexpected portal destination")
        # canonical_url enforces HTTPS/443; the hostname allowlist above is mandatory.
        request = Request(url, method=method)  # noqa: S310
        self.cookies.add_cookie_header(request)
        headers = {"User-Agent": "Jobstream/0.1", "Accept-Encoding": "identity"}
        if request.has_header("Cookie"):
            headers["Cookie"] = request.get_header("Cookie")
        if content_type:
            headers["Content-Type"] = content_type
            headers["Origin"] = f"https://{SECRET_HOST}"
            headers["Referer"] = f"https://{SECRET_HOST}{parts.path}"
        with closing(
            PinnedHTTPSConnection(parts.hostname, public_addresses(parts.hostname)[0])
        ) as connection:
            connection.request(
                method,
                parts.path + ("?" + parts.query if parts.query else ""),
                body=body,
                headers=headers,
            )
            response = connection.getresponse()
            self.cookies.extract_cookies(response, request)
            result_headers = {key.lower(): value for key, value in response.getheaders()}
            data = b"" if response.status == 429 else response.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError("Portal response exceeds limit")
            return Response(response.status, result_headers, data)


def target(application):
    return secret_url(
        application.portal_url or application.job.apply_url or application.job.canonical_url
    )


def supported(application):
    try:
        target(application)
    except ValueError:
        return False
    return application.channel == "portal"


def phone_for_submission(value):
    """The portal uses a numeric validator, so remove display-only formatting.

    Keep the country code and leading zeroes as text; never convert a telephone
    number to an actual float or guess a country/extension.
    """
    value = value.strip()
    if not re.fullmatch(r"\+?[0-9][0-9 ().-]*", value):
        raise ValidationError("Use a telephone number with digits and optional country code.")
    digits = re.sub(r"[^0-9]", "", value)
    if not 7 <= len(digits) <= 15:
        raise ValidationError("Use a telephone number with 7 to 15 digits.")
    return digits


def validate_answers(answers):
    if not isinstance(answers, dict) or set(answers) - ANSWER_FIELDS:
        raise ValidationError("Invalid Secret Tel Aviv applicant details.")
    for field in ("first_name", "last_name", "email", "phone"):
        value = answers.get(field)
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > 250
            or any(c in value for c in "\r\n\x00")
        ):
            raise ValidationError(
                "Provide first name, family name, email, and telephone for Secret Tel Aviv."
            )
    validate_email(answers["email"])
    phone_for_submission(answers["phone"])
    if answers.get("agree_terms") is not True or answers.get("terms_url") != TERMS_URL:
        raise ValidationError("Review and accept the Secret Tel Aviv terms before approval.")
    if type(answers.get("forward_headhunters", False)) is not bool:
        raise ValidationError("Headhunter forwarding must be an explicit choice.")


def read_form(session, url):
    original_path = urlsplit(secret_url(url)).path.rstrip("/")
    try:
        for _ in range(4):
            response = session.request("GET", secret_url(url))
            if response.status not in {301, 302, 303, 307, 308}:
                break
            url = secret_url(urljoin(url, response.headers.get("location", "")))
            if urlsplit(url).path.rstrip("/") != original_path:
                raise ValueError("Portal redirected to another vacancy")
        else:
            raise ValueError("Too many portal redirects")
    except (ValueError, OSError, http.client.HTTPException) as exc:
        raise PortalRejected("portal_page_unavailable") from exc
    if response.status == 429:
        raise PortalRateLimited(response.headers.get("retry-after", ""))
    if response.status != 200:
        raise PortalRejected("portal_page_unavailable")
    try:
        assessment = inspect_secret(response.body, url)
    except (ValueError, TypeError) as exc:
        raise PortalRejected("portal_form_changed") from exc
    if assessment.issues:
        raise PortalRejected("portal_form_changed")
    if assessment.terms_url != TERMS_URL:
        raise PortalRejected("portal_terms_changed")
    soup = BeautifulSoup(response.body, "html.parser")
    form = soup.select_one("form#wpjb-apply-form")
    fields = {
        node["name"]: node.get("value", "") for node in form.select('input[type="hidden"][name]')
    }
    if fields.get("_wpjb_action") != "apply" or not re.fullmatch(
        r"[1-9][0-9]*", fields.get("_job_id", "")
    ):
        raise PortalRejected("portal_hidden_fields_changed")
    return assessment, form, fields


def multipart(fields, attachment=None, *, filename="resume.pdf"):
    from .documents import pdf_filename

    filename = pdf_filename(filename)
    boundary = "jobstream-" + secrets.token_hex(24)
    parts = []
    for name, value in fields.items():
        parts.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            ).encode()
        )
    if attachment is not None:
        parts.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{filename}"\r\nContent-Type: application/pdf\r\n\r\n'
            ).encode()
        )
        parts += [attachment, b"\r\n"]
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def validation_code(soup):
    """Return only allowlisted error categories, never reflected applicant data."""
    errors = [
        node.get_text(" ", strip=True).casefold()
        for node in soup.select(".wpjb-flash-error, .wpjb-error, .wpjb-errors")
        if node.get_text(strip=True)
    ]
    if not errors:
        return ""
    text = " ".join(errors)
    for pattern, code in (
        (r"already (?:applied|submitted)|duplicate application", "portal_duplicate_application"),
        (
            (
                r"(?:cv|resume|file|attachment).{0,60}(?:required|missing|upload)|"
                r"(?:required|missing).{0,40}(?:cv|resume|file|attachment)"
            ),
            "portal_cv_required",
        ),
        (r"(?:e-?mail).{0,40}(?:invalid|required|incorrect)", "portal_email_rejected"),
        (
            r"(?:phone|telephone).{0,60}(?:invalid|required|incorrect|not a float|numeric)",
            "portal_phone_rejected",
        ),
        (r"(?:terms|consent).{0,40}(?:required|accept|agree)", "portal_terms_required"),
    ):
        if re.search(pattern, text):
            return code
    return "portal_validation_rejected"


def upload_attachment(session, attachment, filename):
    """One session-bound temporary upload; never submit after an uncertain upload."""
    body, content_type = multipart(UPLOAD_PARAMS, attachment, filename=filename)
    try:
        response = session.request("POST", UPLOAD_URL, body, content_type)
    except (ValueError, OSError, http.client.HTTPException) as exc:
        # Uploading a temporary CV cannot submit an application. Do not follow it
        # with the application POST when attachment acceptance is unverified.
        raise PortalRejected("portal_upload_unconfirmed") from exc
    if response.status == 429:
        raise PortalRateLimited(response.headers.get("retry-after", ""))
    if response.status != 200:
        raise PortalRejected("portal_upload_rejected")
    try:
        result = json.loads(response.body)
    except (ValueError, RecursionError) as exc:
        raise PortalRejected("portal_upload_unconfirmed") from exc
    if not isinstance(result, dict) or type(result.get("result")) is not int:
        raise PortalRejected("portal_upload_unconfirmed")
    if result["result"] != 1:
        raise PortalRejected("portal_upload_rejected")
    # The site's public uploader keeps attachment state in the cookie session;
    # it does not add a file token to the final application form. Never follow
    # response-supplied URLs or persist temporary file paths.


def confirmation(response, redirected_url=""):
    soup = BeautifulSoup(response.body, "html.parser")
    code = validation_code(soup)
    if code:
        raise PortalRejected(code)
    for node in soup.select(".wpjb-flash-info, .wpjb-flash-success, .wpjb-success"):
        if node.find_parent(class_="wpjb-text"):
            continue
        text = " ".join(node.get_text(" ", strip=True).split()).casefold()
        if re.search(
            r"\b(?:not|never|unable|failed|unsuccessful|unsuccessfully|error|invalid)\b", text
        ):
            continue
        if re.search(
            r"\b(?:your\s+)?application\s+(?:(?:has\s+been|was|is)\s+)?"
            r"(?:successfully\s+)?(?:sent|submitted|received)\b|\bsuccessfully\s+applied\b",
            text,
        ):
            match = re.search(r"/applied/([1-9][0-9]*)/?$", urlsplit(redirected_url).path)
            return "secret_telaviv:" + (
                match[1] if match else hashlib.sha256(node.get_text().encode()).hexdigest()[:24]
            )
    raise PortalUnknown("portal_confirmation_missing")


def send(application, attachment, *, session=None):
    validate_answers(application.portal_answers)
    if not attachment or len(attachment) > 5 * 1024 * 1024 or not attachment.startswith(b"%PDF-"):
        raise PortalRejected("portal_cv_required")
    session = session or SecretSession()
    url = target(application)
    assessment, form, fields = read_form(session, url)
    if assessment.fingerprint != application.portal_fingerprint:
        raise PortalRejected("portal_form_changed")
    answers = application.portal_answers
    fields.update(
        applicant_name=answers["first_name"],
        applicant_surname=answers["last_name"],
        email=answers["email"],
        telephone_number_apply_form=phone_for_submission(answers["phone"]),
    )
    fields["similar_employers_cv[]"] = form.select_one('[name="similar_employers_cv[]"]').get(
        "value", "1"
    )
    if answers.get("forward_headhunters"):
        fields["headhunters[]"] = form.select_one('[name="headhunters[]"]').get("value", "1")
    from .documents import pdf_filename

    document = getattr(application, "document", None)
    filename = pdf_filename(document.name if document else "resume.pdf")
    config = upload_configuration(form.find_parent("html") or form.parent)
    if config:
        upload_attachment(session, attachment, filename)
    body, content_type = multipart(fields, None if config else attachment, filename=filename)
    # Do not try other file-field names after an error: the first POST may have succeeded.
    response = session.request("POST", assessment.form_url, body, content_type)
    if response.status == 429:
        raise PortalRateLimited(response.headers.get("retry-after", ""))
    if response.status in {400, 401, 403, 404, 413, 422}:
        raise PortalRejected(f"portal_http_{response.status}")
    destination = ""
    if response.status in {302, 303}:
        destination = canonical_url(
            urljoin(assessment.form_url, response.headers.get("location", ""))
        )
        parts = urlsplit(destination)
        job_path = urlsplit(url).path.rstrip("/")
        expected_path = re.escape(job_path) + r"(?:/applied/[1-9][0-9]*)?/?"
        if parts.hostname != SECRET_HOST or not re.fullmatch(expected_path, parts.path):
            raise PortalUnknown("portal_redirect_unrecognized")
        try:
            response = session.request("GET", destination)
        except (ValueError, OSError, http.client.HTTPException) as exc:
            raise PortalUnknown("portal_confirmation_unavailable", destination) from exc
    if response.status != 200:
        raise PortalUnknown("portal_outcome_unknown", destination)
    try:
        return confirmation(response, destination)
    except PortalUnknown as exc:
        raise PortalUnknown(str(exc), destination) from exc


def reconcile(application):
    url = canonical_url(application.portal_receipt_url)
    parts = urlsplit(url)
    job_path = urlsplit(target(application)).path.rstrip("/")
    if (
        parts.hostname != SECRET_HOST
        or parts.query
        or not re.fullmatch(re.escape(job_path) + r"/applied/[1-9][0-9]*/?", parts.path)
    ):
        return None
    response = SecretSession().request("GET", url)
    return confirmation(response, url) if response.status == 200 else None


def approve_portal(user, pk):
    from django.db import transaction
    from django.utils import timezone

    from jobs.models import Application, AuditEvent

    from .submissions import InvalidTransition, digest, validate_content

    candidate = Application.objects.select_related("document", "job").get(pk=pk, owner=user)
    if candidate.status not in {"draft", "failed"}:
        raise InvalidTransition("This application cannot be approved in its current state.")
    validate_content(candidate)
    original = digest(candidate, "")
    fingerprint = candidate.portal_fingerprint
    if not fingerprint or candidate.status == "failed":
        from .throttles import portal_pause_seconds

        if portal_pause_seconds():
            raise ValidationError("Secret Tel Aviv requested a pause; try approval later.")
        try:
            assessment, _, _ = read_form(SecretSession(), target(candidate))
            fingerprint = assessment.fingerprint
        except PortalRateLimited as exc:
            from datetime import timedelta

            from .retries import RetryWindowTooLong, retry_after_seconds
            from .throttles import defer_portal

            try:
                defer_portal(
                    timezone.now() + timedelta(seconds=retry_after_seconds(exc.retry_after, 60))
                )
            except RetryWindowTooLong:
                pass
            raise ValidationError("Secret Tel Aviv requested a pause; try approval later.") from exc
        except PortalRejected as exc:
            raise ValidationError(
                "Secret Tel Aviv is unavailable or changed; try approval later."
            ) from exc
    with transaction.atomic():
        application = (
            Application.objects.select_for_update(of=("self",))
            .select_related("document", "job")
            .get(pk=pk, owner=user)
        )
        if application.status not in {"draft", "failed"}:
            raise InvalidTransition("This application cannot be approved in its current state.")
        if digest(application, "") != original:
            raise ValidationError("Application changed during review; review it again.")
        validate_content(application)
        application.portal_url = target(application)
        application.portal_fingerprint = fingerprint
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
