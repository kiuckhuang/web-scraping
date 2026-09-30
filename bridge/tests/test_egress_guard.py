"""Connection-time DNS checks and pinning, without network traffic."""
from __future__ import annotations

import socket

import pytest

from bridge import egress_guard, ssrf


@pytest.mark.parametrize("address", ["100.64.0.1", "127.0.0.1", "169.254.169.254", "192.168.1.1",
                                    "::ffff:127.0.0.1", "2001:db8::1", "224.0.0.1"])
def test_non_global_connection_rejected(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, 1, 6, "", (address, 443))])
    with pytest.raises(ssrf.UrlBlockedError):
        egress_guard.open_public_connection("target.example", 443)


def test_upstream_receives_validated_ip_not_hostname(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, 1, 6, "", ("93.184.216.34", 443))])
    calls = []
    marker = object()

    def tunnel(*args):
        calls.append(args)
        return marker

    monkeypatch.setattr(egress_guard, "_upstream_tunnel", tunnel)
    assert egress_guard.open_public_connection("target.example", 443, "http://proxy:8080") is marker
    assert calls == [("93.184.216.34", 443, "http://proxy:8080")]


def test_cached_public_verdict_cannot_bypass_rebinding(monkeypatch):
    monkeypatch.setattr(ssrf, "_dns_cache", {"target.example": (0, "public")})
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ssrf.UrlBlockedError):
        egress_guard.open_public_connection("target.example", 443)


class FakeSocket:
    def __init__(self, reply):
        self.reply = bytearray(reply)
        self.sent = []
        self.closed = False

    def recv(self, size):
        chunk = bytes(self.reply[:size])
        del self.reply[:size]
        return chunk

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True


def test_http_upstream_connect_pins_ip_and_encodes_credentials(monkeypatch):
    sock = FakeSocket(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: sock)
    assert egress_guard._upstream_tunnel("93.184.216.34", 443, "http://user:pass@proxy:8080") is sock
    assert b"CONNECT 93.184.216.34:443 HTTP/1.1" in sock.sent[0]
    assert b"Proxy-Authorization: Basic dXNlcjpwYXNz" in sock.sent[0]


def test_socks_upstream_uses_ip_address_type(monkeypatch):
    sock = FakeSocket(b"\x05\x00\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: sock)
    egress_guard._upstream_tunnel("93.184.216.34", 443, "socks5://proxy:1080")
    assert sock.sent[1] == b"\x05\x01\x00\x01" + socket.inet_aton("93.184.216.34") + b"\x01\xbb"


def test_upstream_rejection_closes_connection(monkeypatch):
    sock = FakeSocket(b"HTTP/1.1 403 Forbidden\r\n\r\n")
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: sock)
    with pytest.raises(OSError):
        egress_guard._upstream_tunnel("93.184.216.34", 443, "http://proxy:8080")
    assert sock.closed
