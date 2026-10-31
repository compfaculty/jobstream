import base64
import json
from email import message_from_bytes
from unittest.mock import Mock

import pytest
from cryptography.fernet import Fernet
from google.auth.exceptions import RefreshError

from jobs.services import gmail

pytestmark = pytest.mark.django_db


def test_encryption_roundtrip(settings):
    settings.TOKEN_ENCRYPTION_KEY = Fernet.generate_key().decode()
    encrypted = gmail.encrypt_credentials(Mock(to_json=Mock(return_value='{"token":"secret"}')))
    assert "secret" not in encrypted
    assert json.loads(gmail.cipher().decrypt(encrypted.encode())) == {"token": "secret"}
    settings.TOKEN_ENCRYPTION_KEY = ""
    with pytest.raises(gmail.ReconnectRequired):
        gmail.cipher()


def test_mime(application):
    application.message_id = "<unique@example.invalid>"
    raw = gmail.build_message(application, "sender@example.com", b"%PDF-1.4\n%%EOF")
    parsed = message_from_bytes(base64.urlsafe_b64decode(raw))
    assert parsed["Message-ID"] == application.message_id
    assert parsed["To"] == application.recipient
    assert parsed.is_multipart()
    assert not message_from_bytes(
        base64.urlsafe_b64decode(gmail.build_message(application, "sender@example.com", None))
    ).is_multipart()


@pytest.mark.parametrize(
    "failure", ["revoked", "no_refresh", "refresh_rejected", "refresh_ok", "valid"]
)
def test_credentials(connection, settings, monkeypatch, failure):
    settings.TOKEN_ENCRYPTION_KEY = Fernet.generate_key().decode()
    connection.encrypted_credentials = gmail.cipher().encrypt(b'{"token":"x"}').decode()
    credentials = Mock(
        valid=failure == "valid",
        refresh_token=None if failure == "no_refresh" else "refresh",
        to_json=Mock(return_value='{"token":"renewed"}'),
    )
    monkeypatch.setattr(
        gmail.Credentials, "from_authorized_user_info", Mock(return_value=credentials)
    )
    monkeypatch.setattr(gmail, "build", Mock(return_value="client"))
    if failure == "revoked":
        connection.revoked = True
    if failure == "refresh_rejected":
        credentials.refresh.side_effect = RefreshError("revoked")
    if failure in {"revoked", "no_refresh", "refresh_rejected"}:
        with pytest.raises(gmail.ReconnectRequired):
            gmail.gmail_client(connection)
    else:
        # AuthorizedHttp requires real credentials, so replace only the HTTP wrapper.
        import google_auth_httplib2

        monkeypatch.setattr(google_auth_httplib2, "AuthorizedHttp", Mock())
        assert gmail.gmail_client(connection) == "client"
    if failure == "refresh_rejected":
        connection.refresh_from_db()
        assert connection.revoked


def test_send_and_reconciliation(connection, application, monkeypatch):
    client = Mock()
    monkeypatch.setattr(gmail, "gmail_client", lambda connection: client)
    execute = client.users.return_value.messages.return_value.send.return_value.execute
    execute.return_value = {"id": "sent-id"}
    assert gmail.send(connection, application, None) == "sent-id"
    execute.assert_called_once_with(num_retries=0)
    execute.return_value = {}
    with pytest.raises(ValueError):
        gmail.send(connection, application, None)
    find = client.users.return_value.messages.return_value.list.return_value.execute
    find.return_value = {"messages": [{"id": "sent-id"}]}
    assert gmail.find_sent(connection, "<a@b>") == "sent-id"
    find.return_value = {}
    assert gmail.find_sent(connection, "<a@b>") is None
    find.return_value = {"messages": [{"id": "1"}, {"id": "2"}]}
    assert gmail.find_sent(connection, "<a@b>") is None


def test_corrupt_credentials_require_reconnect(connection, settings):
    settings.TOKEN_ENCRYPTION_KEY = Fernet.generate_key().decode()
    connection.encrypted_credentials = "invalid-ciphertext"
    with pytest.raises(gmail.ReconnectRequired):
        gmail.gmail_client(connection)
