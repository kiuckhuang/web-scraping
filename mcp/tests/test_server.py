"""Unit tests for the MCP server helpers (no network required).

Run inside the mcp container:  python -m pytest tests -q
or via CI / `make test-unit`.
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server as server_mod


def test_format_search_results():
    result = {
        "number_of_results": 2,
        "results": [
            {"title": "Foo", "url": "https://foo.com", "content": "snippet text"},
            {"title": "Bar", "url": "https://bar.com"},
        ],
    }
    out = server_mod._format_search_results(result)
    assert "Foo" in out and "https://foo.com" in out
    assert "snippet text" in out
    assert "Bar" in out


def test_format_search_results_compact():
    """Compact format: one markdown-link line per result, no per-result headings."""
    result = {
        "number_of_results": 2,
        "results": [
            {"title": "Foo", "url": "https://foo.com", "content": "snippet text"},
            {"title": "Bar", "url": "https://bar.com"},
        ],
    }
    out = server_mod._format_search_results(result)
    assert "[Foo](https://foo.com)" in out
    assert "### " not in out
    assert out.count("http") == 2


def test_format_scrape_result_markdown():
    result = {"url": "https://a.com", "title": "T", "markdown": "# hello", "tables": [{"rows": []}]}
    out = server_mod._format_scrape_result(result)
    assert "# hello" in out and "1 table(s)" in out


def test_format_combined_results_markdown_content():
    result = {
        "query": "q",
        "results": [
            {"title": "A", "url": "https://a.com", "content": {"url": "https://a.com", "markdown": "full content"}},
        ],
    }
    out = server_mod._format_combined_results(result)
    assert "full content" in out


def test_format_combined_results_fetch_mode():
    """Fetch-mode results carry text/html, not markdown — they must render too."""
    result = {
        "query": "q",
        "results": [
            {"title": "A", "url": "https://a.com", "content": {"url": "https://a.com", "text": "raw text here"}},
            {"title": "B", "url": "https://b.com", "content": None, "scrape_error": "boom"},
        ],
    }
    out = server_mod._format_combined_results(result)
    assert "raw text here" in out
    assert "boom" in out


def test_auth_no_key_allows_everything():
    old = server_mod.MCP_API_KEY
    server_mod.MCP_API_KEY = ""
    try:
        assert server_mod._auth_ok({"client": ("10.0.0.5", 123), "headers": []}) is True
    finally:
        server_mod.MCP_API_KEY = old


def test_bind_host_locality():
    assert server_mod._is_local_bind_host("localhost") is True
    assert server_mod._is_local_bind_host("127.0.0.1") is True
    assert server_mod._is_local_bind_host("::1") is True
    assert server_mod._is_local_bind_host("0.0.0.0") is False
    assert server_mod._is_local_bind_host("192.168.1.10") is False


def test_cors_allows_loopback_origins_on_any_port():
    old_policy = server_mod.MCP_ALLOWED_ORIGIN
    server_mod.MCP_ALLOWED_ORIGIN = "localhost"
    try:
        for origin in ("http://localhost:8185", "http://127.0.0.1:3000", "https://[::1]:8443"):
            scope = {"headers": [(b"origin", origin.encode())]}
            assert server_mod._cors_origin(scope) == origin
        assert server_mod._cors_origin({"headers": [(b"origin", b"http://192.168.1.10:3000")]}) is None
    finally:
        server_mod.MCP_ALLOWED_ORIGIN = old_policy


def test_read_body_limited_handles_chunked_body():
    async def go():
        old_limit = server_mod.MCP_MAX_BODY
        server_mod.MCP_MAX_BODY = 3
        try:
            messages = iter([
                {"type": "http.request", "body": b"ab", "more_body": True},
                {"type": "http.request", "body": b"cd", "more_body": False},
            ])

            async def receive():
                return next(messages)

            assert await server_mod._read_body_limited(receive) is None
        finally:
            server_mod.MCP_MAX_BODY = old_limit

    asyncio.run(go())


def test_auth_with_key():
    old_key, old_cidrs = server_mod.MCP_API_KEY, server_mod._TRUSTED_CIDRS
    server_mod.MCP_API_KEY = "sekrit"
    server_mod._TRUSTED_CIDRS = {"127.0.0.0/8", "::1/128"}
    try:
        assert server_mod._auth_ok({"client": ("127.0.0.1", 5), "headers": []}) is True
        untrusted = {"client": ("10.0.0.5", 5), "headers": [(b"authorization", b"Bearer wrong")]}
        assert server_mod._auth_ok(untrusted) is False
        good = {"client": ("10.0.0.5", 5), "headers": [(b"authorization", b"Bearer sekrit")]}
        assert server_mod._auth_ok(good) is True
        assert server_mod._auth_ok({"client": ("10.0.0.5", 5), "headers": []}) is False
    finally:
        server_mod.MCP_API_KEY = old_key
        server_mod._TRUSTED_CIDRS = old_cidrs


def test_trusted_cidrs():
    old = server_mod._TRUSTED_CIDRS
    server_mod._TRUSTED_CIDRS = {"127.0.0.0/8", "10.20.30.0/24"}
    try:
        assert server_mod._is_trusted({"client": ("10.20.30.55", 1)}) is True
        assert server_mod._is_trusted({"client": ("10.20.31.55", 1)}) is False
        assert server_mod._is_trusted({"client": ("127.0.0.1", 1)}) is True
    finally:
        server_mod._TRUSTED_CIDRS = old


def test_rate_limit():
    old_limit = server_mod.MCP_RATE_LIMIT
    server_mod.MCP_RATE_LIMIT = 2
    server_mod._RATE_WINDOW.clear()
    try:
        scope = {"client": ("9.9.9.9", 1)}
        assert server_mod._rate_limit_ok(scope) is True
        assert server_mod._rate_limit_ok(scope) is True
        assert server_mod._rate_limit_ok(scope) is False
        # other IPs are unaffected
        assert server_mod._rate_limit_ok({"client": ("8.8.8.8", 1)}) is True
    finally:
        server_mod.MCP_RATE_LIMIT = old_limit
        server_mod._RATE_WINDOW.clear()


def test_rate_limit_disabled():
    old_limit = server_mod.MCP_RATE_LIMIT
    server_mod.MCP_RATE_LIMIT = 0
    server_mod._RATE_WINDOW.clear()
    try:
        scope = {"client": ("9.9.9.9", 1)}
        for _ in range(50):
            assert server_mod._rate_limit_ok(scope) is True
    finally:
        server_mod.MCP_RATE_LIMIT = old_limit
        server_mod._RATE_WINDOW.clear()


def test_session_sweep_expires_idle():
    async def go():
        class FakeCM:
            async def __aenter__(self):
                return None, None

            async def __aexit__(self, *args):
                return None

        async def noop():
            pass

        task = asyncio.create_task(noop())
        await task
        old_ttl = server_mod.MCP_SESSION_TTL
        server_mod.MCP_SESSION_TTL = 1800
        try:
            server_mod._sessions.clear()
            stale = server_mod.Session(transport=None, task=task, cm=FakeCM(), last_seen=time.monotonic() - 99999)
            fresh = server_mod.Session(transport=None, task=task, cm=FakeCM(), last_seen=time.monotonic())
            server_mod._sessions["stale"] = stale
            server_mod._sessions["fresh"] = fresh
            await server_mod._sweep_sessions()
            assert "stale" not in server_mod._sessions
            assert "fresh" in server_mod._sessions
        finally:
            server_mod._sessions.clear()
            server_mod.MCP_SESSION_TTL = old_ttl

    asyncio.run(go())


def test_redact_args():
    args = {
        "url": "https://user:pass@example.com/page?token=abc123&q=keep",
        "query": "plain query",
        "max_results": 5,
    }
    out = server_mod._redact_args(args)
    assert "user:***@example.com" in out["url"]
    assert "abc123" not in out["url"]
    assert "q=keep" in out["url"]
    assert out["query"] == "plain query"
    assert out["max_results"] == 5
