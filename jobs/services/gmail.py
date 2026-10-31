"""Gmail transport; no automatic retries on the non-idempotent send operation."""

import base64
import json
from email.message import EmailMessage
from email.policy import SMTP

import httplib2
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from jobs.models import GmailConnection


class ReconnectRequired(Exception):
    pass


def cipher() -> Fernet:
    if not settings.TOKEN_ENCRYPTION_KEY:
        raise ReconnectRequired("Token encryption key is not configured")
    return Fernet(settings.TOKEN_ENCRYPTION_KEY.encode())


def encrypt_credentials(credentials: Credentials) -> str:
    return cipher().encrypt(credentials.to_json().encode()).decode()


def gmail_client(connection: GmailConnection):
    if connection.revoked:
        raise ReconnectRequired("Reconnect Gmail")
    try:
        credentials = Credentials.from_authorized_user_info(
            json.loads(cipher().decrypt(connection.encrypted_credentials.encode())),
            scopes=settings.GOOGLE_SCOPES,
        )
    except (InvalidToken, ValueError, TypeError) as exc:
        raise ReconnectRequired("Stored Gmail credentials are unavailable") from exc
    if not credentials.valid:
        if not credentials.refresh_token:
            raise ReconnectRequired("Reconnect Gmail")
        from google.auth.exceptions import RefreshError

        try:
            credentials.refresh(Request())
        except RefreshError as exc:
            connection.revoked = True
            connection.save(update_fields=["revoked"])
            raise ReconnectRequired("Reconnect Gmail") from exc
        connection.encrypted_credentials = encrypt_credentials(credentials)
        connection.save(update_fields=["encrypted_credentials"])
    from google_auth_httplib2 import AuthorizedHttp

    return build(
        "gmail",
        "v1",
        http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=30)),
        cache_discovery=False,
    )


def build_message(application, sender: str, attachment: bytes | None) -> str:
    message = EmailMessage(policy=SMTP)
    message["From"] = sender
    message["To"] = application.recipient
    message["Subject"] = application.subject
    message["Message-ID"] = application.message_id
    message.set_content(application.body)
    if attachment is not None:
        message.add_attachment(
            attachment, maintype="application", subtype="pdf", filename="resume.pdf"
        )
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


def send(connection, application, attachment: bytes | None) -> str:
    client = gmail_client(connection)
    result = (
        client.users()
        .messages()
        .send(userId="me", body={"raw": build_message(application, connection.email, attachment)})
        .execute(num_retries=0)
    )
    if not result.get("id"):
        raise ValueError("Missing Gmail message ID")
    return result["id"]


def find_sent(connection, message_id: str) -> str | None:
    client = gmail_client(connection)
    result = (
        client.users()
        .messages()
        .list(
            userId="me", labelIds=["SENT"], q=f"rfc822msgid:{message_id.strip('<>')}", maxResults=2
        )
        .execute(num_retries=0)
    )
    messages = result.get("messages", [])
    # Absence is never proof of failure; Gmail indexing may be delayed.
    return messages[0]["id"] if len(messages) == 1 else None
