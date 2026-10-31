"""Bounded public HTTPS fetches. Pin the validated DNS address for each connection."""

import http.client
import ipaddress
import socket
import ssl
from contextlib import closing
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

MAX_BYTES = 2 * 1024 * 1024


class UnsafeURL(ValueError):
    pass


def public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return (
        address.is_global
        and not address.is_multicast
        and not address.is_reserved
        and not getattr(address, "sixtofour", None)
        and not getattr(address, "teredo", None)
    )


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
    ):
        raise UnsafeURL("Only public HTTPS URLs on port 443 are supported")
    host = parts.hostname.encode("idna").decode("ascii").lower()
    try:
        if not public_ip(host):
            raise UnsafeURL("Non-public address")
    except ValueError as exc:
        if isinstance(exc, UnsafeURL):
            raise
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        raise UnsafeURL("Local hostname")
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}
    ]
    return urlunsplit(
        (
            "https",
            f"[{host}]" if ":" in host else host,
            parts.path or "/",
            urlencode(sorted(query)),
            "",
        )
    )


def public_addresses(host: str) -> list[str]:
    addresses = sorted(
        {str(record[4][0]) for record in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    )
    if not addresses or any(not public_ip(ip) for ip in addresses):
        raise UnsafeURL("DNS contains non-public addresses")
    return addresses


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str):
        super().__init__(host, timeout=15, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # No second DNS lookup: connect only to the checked literal IP; preserve TLS SNI.
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def fetch_public_document(url: str, *, max_bytes: int = MAX_BYTES) -> tuple[bytes, str]:
    if not 1 <= max_bytes <= 16 * 1024 * 1024:
        raise ValueError("Invalid response byte limit")
    for _ in range(4):
        url = canonical_url(url)
        parts = urlsplit(url)
        host = parts.hostname
        if host is None:
            raise UnsafeURL("Missing hostname")
        addresses = public_addresses(host)
        with closing(PinnedHTTPSConnection(host, addresses[0])) as connection:
            target = parts.path + ("?" + parts.query if parts.query else "")
            connection.request(
                "GET",
                target,
                headers={"User-Agent": "Jobstream/0.1", "Accept-Encoding": "identity"},
            )
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise UnsafeURL("Redirect without location")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError(f"HTTP_{response.status}")
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("Source response exceeds limit")
            return data, url
    raise UnsafeURL("Too many redirects")


def fetch_public(url: str, *, max_bytes: int = MAX_BYTES) -> bytes:
    return fetch_public_document(url, max_bytes=max_bytes)[0]
