"""Explicit synthetic-account transport to a loopback-only Mailpit sink."""

import base64
import re
import smtplib
from email import policy
from email.parser import BytesParser

import httpx
from django.conf import settings
from django.core.exceptions import ValidationError

from jobs.models import Application, GmailConnection

USERNAME = "jobstream-mail-sandbox"
SENDER = "applicant@jobstream.invalid"
RECIPIENT = "recruiter@jobstream.invalid"
JOB_URL = "https://employer.jobstream.invalid/synthetic-email-job"
MARKER = "sandbox-mail-v1:no-oauth-credentials"


def marked(connection: GmailConnection) -> bool:
    # Partial/corrupt sandbox records must never fall through to real Gmail.
    return (
        connection.encrypted_credentials == MARKER
        or connection.email == SENDER
        or connection.owner.username == USERNAME
    )


def validate(connection: GmailConnection, application: Application | None = None) -> None:
    if not (
        settings.DEBUG
        and settings.SANDBOX_MAIL_ENABLED
        and not connection.revoked
        and connection.encrypted_credentials == MARKER
        and connection.email == SENDER
        and connection.owner.username == USERNAME
        and connection.owner.email == SENDER
        and not connection.owner.is_staff
        and not connection.owner.is_superuser
    ):
        raise ValidationError("Synthetic mail is disabled or its account is invalid.")
    if application is not None and not (
        application.owner_id == connection.owner_id
        and application.channel == "email"
        and application.recipient == RECIPIENT
        and application.job.canonical_url == JOB_URL
    ):
        raise ValidationError("Synthetic mail requires the synthetic job and recipient.")


def api_get(path: str, **params: str | int) -> httpx.Response:
    # No environment proxies, redirects, credentials or external DNS resolution.
    with httpx.Client(timeout=5, trust_env=False) as client:
        with client.stream(
            "GET",
            f"http://127.0.0.1:{settings.SANDBOX_MAIL_API_PORT}/api/v1/{path}",
            params=params,
        ) as response:
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > 8 * 1024 * 1024:
                    raise ValueError("Mailpit response exceeds limit")
            return httpx.Response(200, content=bytes(data))


def find_sent(connection: GmailConnection, message_id: str) -> str | None:
    validate(connection)
    result = api_get("search", query=f"message-id:{message_id.strip('<>')}", limit=2).json()
    messages = result.get("messages", [])
    if len(messages) != 1:
        return None
    identity = messages[0]["ID"]
    if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identity):
        raise ValueError("Invalid Mailpit receipt ID")
    # Check the raw header as well as the search result before confirming a receipt.
    raw = api_get(f"message/{identity}/raw").content
    parsed = BytesParser(policy=policy.default).parsebytes(raw)
    if parsed["Message-ID"] != message_id or parsed["From"] != SENDER or parsed["To"] != RECIPIENT:
        return None
    return f"mailpit:{identity}"


def send(connection: GmailConnection, application: Application, attachment: bytes | None) -> str:
    from .gmail import build_message

    validate(connection, application)
    raw = base64.urlsafe_b64decode(build_message(application, SENDER, attachment))
    with smtplib.SMTP("127.0.0.1", settings.SANDBOX_MAIL_SMTP_PORT, timeout=5) as client:
        refused = client.sendmail(SENDER, [RECIPIENT], raw)
        if refused:
            raise ValueError("Mailpit rejected the synthetic recipient")
    receipt = find_sent(connection, application.message_id)
    if receipt is None:
        # Acceptance without a visible receipt is uncertain; never blindly resend.
        raise ValueError("Mailpit receipt not yet available")
    return receipt
