"""Regression tests for the suite's Python TCP egress boundary."""

import socket

import pytest


@pytest.mark.parametrize("host", ["203.0.113.1", "10.0.0.1", "::ffff:203.0.113.1", "example.com"])
@pytest.mark.parametrize("method", ["connect", "connect_ex"])
def test_external_connection_is_blocked(host, method):
    with socket.socket() as client:
        with pytest.raises(RuntimeError, match="Test network access blocked"):
            getattr(client, method)((host, 443))


def test_loopback_connection_is_allowed():
    with socket.socket() as server, socket.socket() as client:
        server.settimeout(2)
        client.settimeout(2)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client.connect(server.getsockname())
        peer, _ = server.accept()
        peer.close()
