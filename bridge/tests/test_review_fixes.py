"""Regression tests for the code-review hardening batch."""
from __future__ import annotations

import asyncio
import time

import pytest
from bridge.exa_client import ExaError
from bridge.http_client import Escalation, _build_result, _fetch_public_sync
from bridge.logging_utils import redact
from bridge.retry import parse_retry_after
from fastapi import HTTPException

from bridge import exa_client, main, ssrf


def test_malformed_url_is_400_not_500():
    with pytest.raises(HTTPException) as excinfo:
        main._validate_public_url("http://[::1")
    assert excinfo.value.status_code == 400
    assert main._is_public_url("http://[::1") is False


def test_nat64_embedded_loopback_is_blocked():
    assert ssrf.is_public_http_url("http://[64:ff9b::7f00:1]/") is False
    assert ssrf.is_public_http_url("http://[64:ff9b::a9fe:a9fe]/") is False
    with pytest.raises(ssrf.UrlBlockedError):
        ssrf.resolve_public_addresses("64:ff9b::7f00:1", 80)


def test_trailing_bang_disables_fallback_stages():
    assert main._fallback_allowed(None, 1, "laptop !ddg") is False
    assert main._fallback_allowed(None, 1, "laptop") is True


def test_remaining_budget_tracks_request_deadline():
    token = main._request_deadline.set(time.monotonic() + 5)
    try:
        assert 0 < main._remaining(75) <= 5
    finally:
        main._request_deadline.reset(token)
    assert main._remaining(75) == 75


@pytest.mark.parametrize("status, body", [(204, ""), (304, ""), (404, "gone"), (200, "   ")])
def test_http_fastpath_escalates_non_200_or_empty(monkeypatch, status, body):
    monkeypatch.setattr("bridge.http_client._request_raw", lambda url: (status, url, {}, body))
    monkeypatch.setattr("bridge.http_client.ssrf.validate_public_url", lambda url: url)
    with pytest.raises(Escalation):
        _fetch_public_sync("https://example.com/", mode="extract")


def test_http_fastpath_fetch_keeps_nav_and_drops_scripts():
    html = "<html><head><title>T</title><script>var x=1</script></head><body><nav>menu</nav><p>body</p><footer>foot</footer></body></html>"
    result = _build_result("https://example.com/", html, "fetch", 200)
    assert "menu" in result["text"] and "foot" in result["text"]
    assert "var x" not in result["text"]


def test_http_fastpath_tables_include_footer_tables():
    html = "<html><body><footer><table><tr><td>footer-cell</td></tr></table></footer></body></html>"
    result = _build_result("https://example.com/", html, "extract", 200)
    assert any("footer-cell" in cell for table in result["tables"] for row in table["rows"] for cell in row)


def test_redaction_covers_prefixed_secret_names_and_encoding():
    cases = {
        "https://x/?access_token=SECRET": "SECRET",
        "https://x/?api-key=KEY2": "KEY2",
        "https://x/?X-Amz-Signature=AWS": "AWS",
        "https%3A%2F%2Fuser:pass%40host%3FX-Amz-Signature%3DAWS": "AWS",
    }
    for raw, secret in cases.items():
        masked = redact(raw)
        assert secret not in masked
        assert "***" in masked


def test_parse_retry_after_accepts_seconds_and_http_date():
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after("2.5") == 2.5
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") is not None
    assert parse_retry_after("not-a-date") is None
    assert parse_retry_after(None) is None
    assert parse_retry_after("-3") is None


def test_exa_scrape_without_key_refuses_to_call_out(monkeypatch):
    monkeypatch.setattr(exa_client, "EXA_ENABLED", True)
    monkeypatch.setattr(exa_client, "EXA_API_KEY", "")
    with pytest.raises(ExaError) as excinfo:
        asyncio.run(exa_client.scrape("https://example.com/"))
    assert excinfo.value.bridge_status == 503


def test_operation_deadline_treats_tinyfish_search_as_search(monkeypatch):
    async def slow(_scope, _receive, _send):
        await asyncio.sleep(10)

    monkeypatch.setattr(main, "SEARCH_MAX_SECONDS", 0.05)
    monkeypatch.setattr(main, "SCRAPE_MAX_SECONDS", 5)
    messages = []

    async def send(message):
        messages.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(main.OperationDeadline(slow)({"type": "http", "path": "/tinyfish_search"}, receive, send))
    assert messages[0]["status"] == 504


def test_search_and_scrape_bad_result_url_is_isolated(monkeypatch):
    """One malformed result URL must not abort the whole combined request."""

    async def fake_search(*_a, **_k):
        return {"results": [{"title": "bad", "url": "http://[::1"}]}

    monkeypatch.setattr(main, "_search_with_fallbacks", fake_search)
    req = main.SearchAndScrapeRequest(query="q", max_results=1)
    resp = asyncio.run(main.search_and_scrape(req))
    assert resp["results"][0]["content"] is None
    assert "scrape_error" in resp["results"][0]
