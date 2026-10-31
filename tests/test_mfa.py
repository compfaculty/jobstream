"""Exercise QR payload and verification using only disposable test-account secrets."""

import base64
import hashlib
import hmac
import struct
import time
from urllib.parse import parse_qs, urlsplit

import pytest
import qrcode


@pytest.mark.django_db
def test_qr_payload_matches_setup_and_completes_enrollment(authenticated, user, monkeypatch):
    user.is_staff = True
    user.save(update_fields=["is_staff"])
    setup = "/account/two_factor/setup/"
    response = authenticated.get(setup)
    prefix = response.context["wizard"]["management_form"].prefix
    response = authenticated.post(setup, {f"{prefix}-current_step": "welcome"})
    assert response.context["wizard"]["steps"].current == "generator"
    displayed_uri = response.context["otpauth_url"]
    payloads = []
    original_make = qrcode.make

    def capture(data, **kwargs):
        payloads.append(data)
        return original_make(data, **kwargs)

    monkeypatch.setattr(qrcode, "make", capture)
    response = authenticated.get(response.context["QR_URL"])
    assert response.status_code == 200
    assert payloads == [displayed_uri]
    # Real browsers request this independently after loading the enrollment page.
    assert authenticated.get("/favicon.ico", follow=True).status_code == 204
    assert (
        authenticated.get(
            "/apple-touch-icon.png", HTTP_SEC_FETCH_DEST="image", follow=True
        ).status_code
        == 403
    )
    assert authenticated.get("/api/v1/jobs/", follow=True).status_code == 403
    parameters = parse_qs(urlsplit(payloads[0]).query)
    key = base64.b32decode(parameters["secret"][0])
    digest = hmac.new(key, struct.pack(">Q", int(time.time()) // 30), hashlib.sha1).digest()
    offset = digest[-1] & 15
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1000000
    response = authenticated.post(
        setup,
        {
            f"{prefix}-current_step": "generator",
            "generator-token": f"{code:06d}",
        },
    )
    assert response.status_code == 302
    assert response.url == "/account/two_factor/setup/complete/"
    assert user.totpdevice_set.filter(confirmed=True).exists()
