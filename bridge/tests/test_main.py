"""Unit tests for the bridge REST API helpers (no network required).

Run inside the bridge container:  python -m pytest tests -q
or via CI / `make test-unit`.
"""

import socket

import pytest
from fastapi import HTTPException

from bridge.main import ScrapeRequest, SearchAndScrapeRequest, _validate_public_url


def _public_getaddrinfo(host, port=None, family=0, type=0, proto=0, flags=0):
    """Fake resolver: everything resolves to a public IP."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


def test_rejects_localhost_names():
    for host in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]"):
        with pytest.raises(HTTPException) as exc:
            _validate_public_url(f"http://{host}:8000/admin")
        assert exc.value.status_code == 403


def test_rejects_private_and_link_local_ips():
    for ip in ("10.0.0.1", "192.168.1.10", "172.16.0.5", "127.0.0.1", "169.254.169.254"):
        with pytest.raises(HTTPException) as exc:
            _validate_public_url(f"http://{ip}/")
        assert exc.value.status_code == 403


def test_rejects_non_http_schemes():
    with pytest.raises(HTTPException) as exc:
        _validate_public_url("ftp://example.com/file")
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        _validate_public_url("file:///etc/passwd")
    assert exc.value.status_code == 400


def test_accepts_public_domain(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    assert _validate_public_url("https://example.com/page?q=1") == "https://example.com/page?q=1"


def test_accepts_public_ip(monkeypatch):
    assert _validate_public_url("https://93.184.216.34/") == "https://93.184.216.34/"


def test_request_models_validate_modes():
    ok = ScrapeRequest(url="https://example.com", mode="fetch")
    assert ok.mode == "fetch"
    ok2 = SearchAndScrapeRequest(query="q", scrape_mode="extract")
    assert ok2.scrape_mode == "extract"
