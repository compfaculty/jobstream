"""Explicit HTTPX boundary instrumentation, no global monkeypatching."""

from contextlib import contextmanager
from contextvars import ContextVar

from .context import outbound_headers
from .metrics import outbound

current_attempt = ContextVar("http_attempt", default=None)


@contextmanager
def stream(client, method, url, *, provider="other", **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    if provider == "infinity":
        headers.update(outbound_headers())
    with outbound(provider) as telemetry:
        with client.stream(method, url, headers=headers, **kwargs) as response:
            telemetry.status = response.status_code
            token = current_attempt.set(telemetry)
            try:
                yield response
            finally:
                current_attempt.reset(token)
            # HTTPX tracks raw downloaded bytes, which may be compressed; do not
            # mislabel this as decoded payload. Callers record decoded chunks.


def payload(provider, size):
    attempt = current_attempt.get()
    if attempt is not None:
        attempt.size = (attempt.size or 0) + size
