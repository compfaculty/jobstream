import json
import socket
from unittest.mock import Mock

import pytest

from jobs.services import network
from jobs.services.sources import ComeetAdapter, JSONLDAdapter, Listing, RSSAdapter, normalize


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://localhost/",
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://169.254.169.254/",
        "https://user:password@example.com",
        "https://example.com:8443/",
        "https://foo.internal/",
        "https://224.0.0.1/",
        "javascript:alert(1)",
    ],
)
def test_unsafe_urls(url):
    with pytest.raises(ValueError):
        network.canonical_url(url)


def test_canonicalization():
    assert (
        network.canonical_url("https://EXAMPLE.com/job?utm_source=x&b=2&a=1#top")
        == "https://example.com/job?a=1&b=2"
    )


@pytest.mark.parametrize(
    "addresses", [["127.0.0.1"], ["8.8.8.8", "10.0.0.1"], ["::ffff:127.0.0.1"]]
)
def test_dns_rejects_private(monkeypatch, addresses):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", (ip, 443)) for ip in addresses]
    )
    with pytest.raises(network.UnsafeURL):
        network.public_addresses("example.com")


def test_redirect_revalidated(monkeypatch):
    monkeypatch.setattr(network, "public_addresses", lambda host: ["8.8.8.8"])
    response = Mock(status=302)
    response.getheader.return_value = "https://127.0.0.1/private"
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.getresponse.return_value = response
    constructor = Mock(return_value=connection)
    monkeypatch.setattr(network, "PinnedHTTPSConnection", constructor)
    with pytest.raises(network.UnsafeURL):
        network.fetch_public("https://example.com")
    constructor.assert_called_once_with("example.com", "8.8.8.8")


def test_fetch_limits(monkeypatch):
    monkeypatch.setattr(network, "public_addresses", lambda host: ["8.8.8.8"])
    response = Mock(status=200)
    response.read.return_value = b"x" * (network.MAX_BYTES + 1)
    connection = Mock()
    connection.__enter__ = Mock(return_value=connection)
    connection.__exit__ = Mock(return_value=False)
    connection.getresponse.return_value = response
    monkeypatch.setattr(network, "PinnedHTTPSConnection", Mock(return_value=connection))
    with pytest.raises(ValueError):
        network.fetch_public("https://example.com")


def test_rss_normalizes_untrusted_content():
    data = b'<rss version="2.0"><channel><item><title>Engineer</title><link>https://example.com/job?utm_source=x</link><description>&lt;b&gt;Hello&lt;/b&gt;</description></item></channel></rss>'
    values = RSSAdapter().discover(data, "https://example.com", 5)
    assert normalize(values[0])["description"] == "Hello"
    assert normalize(values[0])["canonical_url"] == "https://example.com/job"
    with pytest.raises(ValueError):
        RSSAdapter().discover(b"broken", "https://example.com", 5)
    with pytest.raises(ValueError):
        normalize(Listing("https://example.com", ""))


def test_jsonld_and_comeet():
    node = {
        "@type": "JobPosting",
        "title": "Developer",
        "url": "/jobs/1",
        "hiringOrganization": {"name": "Company"},
        "jobLocation": {"address": {"addressLocality": "Tel Aviv"}},
        "description": "<b>Python</b>",
    }
    data = (
        '<script type="application/ld+json">' + json.dumps({"@graph": [node]}) + "</script>"
    ).encode()
    result = JSONLDAdapter().discover(data, "https://example.com", 10)
    assert result[0].company == "Company"
    assert result[0].description == "Python"
    comeet = [
        {
            "name": "Developer",
            "url_active_page": "https://example.com/job",
            "location": {"name": "Israel"},
            "details": [{"value": "<p>Linux</p>"}],
        }
    ]
    assert (
        ComeetAdapter()
        .discover(json.dumps(comeet).encode(), "https://example.com", 10)[0]
        .description
        == "Linux"
    )


def test_board_discovery_and_metadata():
    from jobs.services.sources import BoardAdapter

    adapter = BoardAdapter("div.wpjb-col-title a[href]")
    board = (
        b'<div class="wpjb-col-title"><a href="/job/1">Engineer</a>'
        b'<a href="/job/1">Engineer</a></div>'
    )
    listings = adapter.discover(board, "https://example.com", 10)
    assert len(listings) == 1
    metadata = adapter.metadata(
        listings[0], b'<h1>Senior Engineer</h1><div class="wpjb-text"><p>Python</p></div>'
    )
    assert metadata.title == "Senior Engineer"
    assert metadata.description == "Python"
    with pytest.raises(ValueError):
        adapter.discover(b"<html>Challenge</html>", "https://example.com", 10)


def test_pinned_tls_connection(monkeypatch):
    # Assert the socket target is the validated literal and TLS uses the original hostname.
    raw = Mock()
    create = Mock(return_value=raw)
    context = Mock()
    monkeypatch.setattr(socket, "create_connection", create)
    monkeypatch.setattr(network.ssl, "create_default_context", lambda: context)
    connection = network.PinnedHTTPSConnection("example.com", "8.8.8.8")
    connection.connect()
    create.assert_called_once_with(("8.8.8.8", 443), 15)
    context.wrap_socket.assert_called_once_with(raw, server_hostname="example.com")
