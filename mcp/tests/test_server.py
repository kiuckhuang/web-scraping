"""Unit tests for the MCP server helpers (no network required).

Run inside the mcp container:  python -m pytest tests -q
or via CI / `make test-unit`.
"""

import asyncio
import re
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


def test_auth_no_key_allows_everything(monkeypatch):
    monkeypatch.setattr(server_mod, "MCP_API_KEY", "")
    assert server_mod._auth_ok({"client": ("10.0.0.5", 123), "headers": []}) is True


def test_bind_host_locality():
    assert server_mod._is_local_bind_host("localhost") is True
    assert server_mod._is_local_bind_host("127.0.0.1") is True
    assert server_mod._is_local_bind_host("::1") is True
    assert server_mod._is_local_bind_host("0.0.0.0") is False
    assert server_mod._is_local_bind_host("192.168.1.10") is False


def test_cors_allows_loopback_origins_on_any_port(monkeypatch):
    monkeypatch.setattr(server_mod, "MCP_ALLOWED_ORIGIN", "localhost")
    for origin in ("http://localhost:8185", "http://127.0.0.1:3000", "https://[::1]:8443"):
        scope = {"headers": [(b"origin", origin.encode())]}
        assert server_mod._cors_origin(scope) == origin
    assert server_mod._cors_origin({"headers": [(b"origin", b"http://192.168.1.10:3000")]}) is None


def test_cors_wildcard_requires_api_key(monkeypatch):
    monkeypatch.setattr(server_mod, "MCP_ALLOWED_ORIGIN", "*")
    scope = {"headers": [(b"origin", b"http://hhnode-ib-185:8184")]}
    # Refused without an API key: loopback/podman clients bypass auth, so a
    # wildcard CORS policy would expose them to any website in a browser.
    monkeypatch.setattr(server_mod, "MCP_API_KEY", "")
    assert server_mod._cors_origin(scope) is None
    # With a key configured, any origin is reflected.
    monkeypatch.setattr(server_mod, "MCP_API_KEY", "secret")
    assert server_mod._cors_origin(scope) == "http://hhnode-ib-185:8184"


def test_cors_exact_origin_list(monkeypatch):
    monkeypatch.setattr(server_mod, "MCP_ALLOWED_ORIGIN", "http://a.example:8184, http://b.example")
    assert server_mod._cors_origin({"headers": [(b"origin", b"http://a.example:8184")]}) == "http://a.example:8184"
    assert server_mod._cors_origin({"headers": [(b"origin", b"http://b.example")]}) == "http://b.example"
    # Bare hostnames and ports alone never match.
    assert server_mod._cors_origin({"headers": [(b"origin", b"http://a.example")]}) is None
    assert server_mod._cors_origin({"headers": [(b"origin", b"http://c.example:8184")]}) is None


def test_public_dns_failure_is_reported_by_bridge_not_mcp():
    """HTTP errors remain tool errors without requiring exception tracebacks."""
    assert server_mod._format_scrape_result({"url": "https://example.com", "text": "ok"}).startswith("## Scraped:")


def test_read_body_limited_handles_chunked_body(monkeypatch):
    async def go():
        monkeypatch.setattr(server_mod, "MCP_MAX_BODY", 3)
        messages = iter([
            {"type": "http.request", "body": b"ab", "more_body": True},
            {"type": "http.request", "body": b"cd", "more_body": False},
        ])

        async def receive():
            return next(messages)

        assert await server_mod._read_body_limited(receive) is None

    asyncio.run(go())


def test_auth_with_key(monkeypatch):
    monkeypatch.setattr(server_mod, "MCP_API_KEY", "sekrit")
    monkeypatch.setattr(server_mod, "_TRUSTED_CIDRS", {"127.0.0.0/8", "::1/128"})
    assert server_mod._auth_ok({"client": ("127.0.0.1", 5), "headers": []}) is True
    untrusted = {"client": ("10.0.0.5", 5), "headers": [(b"authorization", b"Bearer wrong")]}
    assert server_mod._auth_ok(untrusted) is False
    good = {"client": ("10.0.0.5", 5), "headers": [(b"authorization", b"Bearer sekrit")]}
    assert server_mod._auth_ok(good) is True
    assert server_mod._auth_ok({"client": ("10.0.0.5", 5), "headers": []}) is False


def test_trusted_cidrs(monkeypatch):
    monkeypatch.setattr(server_mod, "_TRUSTED_CIDRS", {"127.0.0.0/8", "10.20.30.0/24"})
    assert server_mod._is_trusted({"client": ("10.20.30.55", 1)}) is True
    assert server_mod._is_trusted({"client": ("10.20.31.55", 1)}) is False
    assert server_mod._is_trusted({"client": ("127.0.0.1", 1)}) is True


def test_rate_limit(monkeypatch):
    monkeypatch.setattr(server_mod, "MCP_RATE_LIMIT", 2)
    server_mod._RATE_WINDOW.clear()
    try:
        scope = {"client": ("9.9.9.9", 1)}
        assert server_mod._rate_limit_ok(scope) is True
        assert server_mod._rate_limit_ok(scope) is True
        assert server_mod._rate_limit_ok(scope) is False
        # other IPs are unaffected
        assert server_mod._rate_limit_ok({"client": ("8.8.8.8", 1)}) is True
    finally:
        server_mod._RATE_WINDOW.clear()


def test_rate_limit_evicts_expired_entries_when_large(monkeypatch):
    monkeypatch.setattr(server_mod, "MCP_RATE_LIMIT", 5)
    now = time.monotonic()
    server_mod._RATE_WINDOW.clear()
    try:
        # Pre-populate with stale entries
        for i in range(10001):
            server_mod._RATE_WINDOW[f"10.0.{i // 256}.{i % 256}"] = [now - 120.0]
        # Adding a fresh IP should trigger prune of stale entries
        scope = {"client": ("1.2.3.4", 1)}
        assert server_mod._rate_limit_ok(scope) is True
        # Stale entries should have been pruned
        assert len(server_mod._RATE_WINDOW) == 1
        assert "1.2.3.4" in server_mod._RATE_WINDOW
    finally:
        server_mod._RATE_WINDOW.clear()


def test_rate_limit_disabled(monkeypatch):
    monkeypatch.setattr(server_mod, "MCP_RATE_LIMIT", 0)
    server_mod._RATE_WINDOW.clear()
    try:
        scope = {"client": ("9.9.9.9", 1)}
        for _ in range(50):
            assert server_mod._rate_limit_ok(scope) is True
    finally:
        server_mod._RATE_WINDOW.clear()


def test_session_sweep_expires_idle(monkeypatch):
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
        monkeypatch.setattr(server_mod, "MCP_SESSION_TTL", 1800)
        server_mod._sessions.clear()
        try:
            stale = server_mod.Session(transport=None, task=task, cm=FakeCM(), last_seen=time.monotonic() - 99999)  # type: ignore[arg-type]
            fresh = server_mod.Session(transport=None, task=task, cm=FakeCM(), last_seen=time.monotonic())  # type: ignore[arg-type]
            server_mod._sessions["stale"] = stale
            server_mod._sessions["fresh"] = fresh
            await server_mod._sweep_sessions()
            assert "stale" not in server_mod._sessions
            assert "fresh" in server_mod._sessions
        finally:
            server_mod._sessions.clear()

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


# ---------------------------------------------------------------------------
#  Bridge request-ID correlation
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
#  Transport integration — full MCP lifecycle through the real SDK transport
#  (network-free; guards against mcp SDK upgrades breaking our raw-ASGI usage)
# ---------------------------------------------------------------------------

def test_mcp_lifecycle_initialize_tools_delete(monkeypatch):
    async def go():
        monkeypatch.setattr(server_mod, "_ensure_cleanup", lambda: None)
        server_mod._sessions.clear()
        try:
            headers = [
                (b"content-type", b"application/json"),
                (b"accept", b"application/json, text/event-stream"),
            ]

            async def call(method, extra_headers, payload):
                scope = {
                    "type": "http",
                    "method": method,
                    "path": "/mcp",
                    "headers": headers + extra_headers,
                    "client": ("127.0.0.1", 5),
                }

                async def receive():
                    return {"type": "http.request", "body": payload, "more_body": False}

                sent = []

                async def send(message):
                    sent.append(message)

                await server_mod.app(scope, receive, send)
                start = next(m for m in sent if m["type"] == "http.response.start")
                out = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
                hdrs = {k.decode().lower(): v.decode() for k, v in start["headers"]}
                return start["status"], hdrs, out

            init_body = (
                b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
                b'{"protocolVersion":"2025-03-26","capabilities":{},'
                b'"clientInfo":{"name":"test","version":"0"}}}'
            )
            status, hdrs, out = await call("POST", headers, init_body)
            sid = hdrs.get("mcp-session-id", "")
            assert status == 200 and sid and b"web-scrape-bridge" in out

            status, _, out = await call(
                "POST", [(b"mcp-session-id", sid.encode())],
                b'{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}',
            )
            assert status == 200 and out.count(b'"name"') >= 5

            status, _, _ = await call("DELETE", [(b"mcp-session-id", sid.encode())], b"")
            assert status == 200 and sid not in server_mod._sessions

            status, _, _ = await call(
                "POST", [(b"mcp-session-id", sid.encode())],
                b'{"jsonrpc":"2.0","id":3,"method":"tools/list","params":{}}',
            )
            assert status == 404  # terminated session must not come back
        finally:
            server_mod._sessions.clear()

    asyncio.run(go())


def test_request_headers_attaches_x_request_id():
    headers = server_mod._request_headers("/search")
    assert re.fullmatch(r"[0-9a-f]{8}", headers["X-Request-ID"])


# ---------------------------------------------------------------------------
#  Session lifecycle — initialize gate + DELETE cleanup
# ---------------------------------------------------------------------------

def test_is_initialize_request():
    assert server_mod._is_initialize_request(
        b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
    ) is True
    assert server_mod._is_initialize_request(
        b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
    ) is False
    # JSON-RPC batch containing an initialize also counts.
    assert server_mod._is_initialize_request(
        b'[{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}]'
    ) is True
    assert server_mod._is_initialize_request(b"not json") is False
    assert server_mod._is_initialize_request(b"") is False


async def _post_mcp(body: bytes, monkeypatch, headers=None):
    """Drive one POST through the raw ASGI app; return the response start message."""
    monkeypatch.setattr(server_mod, "_ensure_cleanup", lambda: None)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": headers or [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 5),
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    sent = []

    async def send(message):
        sent.append(message)

    await server_mod.app(scope, receive, send)
    return next(m for m in sent if m["type"] == "http.response.start")


def test_headerless_non_initialize_post_rejected_without_session(monkeypatch):
    """Headerless non-initialize POSTs get 400 and spawn no session state."""

    async def go():
        server_mod._sessions.clear()
        try:
            start = await _post_mcp(
                b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}', monkeypatch
            )
            assert start["status"] == 400
            assert server_mod._sessions == {}
        finally:
            server_mod._sessions.clear()

    asyncio.run(go())


def test_get_without_session_rejected(monkeypatch):
    """GET/DELETE without a session ID cannot be routed to any session."""

    async def go():
        monkeypatch.setattr(server_mod, "_ensure_cleanup", lambda: None)
        server_mod._sessions.clear()
        try:
            scope = {"type": "http", "method": "GET", "path": "/mcp", "headers": [], "client": ("127.0.0.1", 5)}

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            sent = []

            async def send(message):
                sent.append(message)

            await server_mod.app(scope, receive, send)
            start = next(m for m in sent if m["type"] == "http.response.start")
            assert start["status"] == 400
            assert server_mod._sessions == {}
        finally:
            server_mod._sessions.clear()

    asyncio.run(go())


class _FakeTransport:
    def __init__(self):
        self.handled = []

    async def handle_request(self, scope, receive, send):
        self.handled.append(scope.get("method"))


class _FakeCM:
    def __init__(self):
        self.exited = False

    async def __aexit__(self, *args):
        self.exited = True


def test_delete_session_frees_state(monkeypatch):
    """DELETE must remove the session (task cancelled, context closed) immediately."""

    async def go():
        monkeypatch.setattr(server_mod, "_ensure_cleanup", lambda: None)
        monkeypatch.setattr(server_mod, "MCP_API_KEY", "")
        server_mod._sessions.clear()
        try:
            async def noop():
                pass

            task = asyncio.create_task(noop())
            await task
            transport, cm = _FakeTransport(), _FakeCM()
            server_mod._sessions["sess-del"] = server_mod.Session(
                transport=transport, task=task, cm=cm, last_seen=time.monotonic()
            )
            scope = {
                "type": "http",
                "method": "DELETE",
                "path": "/mcp",
                "headers": [(b"mcp-session-id", b"sess-del")],
                "client": ("127.0.0.1", 5),
            }

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            sent = []

            async def send(message):
                sent.append(message)

            await server_mod.app(scope, receive, send)
            assert "sess-del" not in server_mod._sessions
            assert transport.handled == ["DELETE"]
            assert cm.exited is True
        finally:
            server_mod._sessions.clear()

    asyncio.run(go())


# ---------------------------------------------------------------------------
#  Hardening — rate limit headers, malformed headers, CORS Vary
# ---------------------------------------------------------------------------

def test_rate_limited_response_has_retry_after(monkeypatch):
    async def go():
        monkeypatch.setattr(server_mod, "_ensure_cleanup", lambda: None)
        now = time.monotonic()
        server_mod._RATE_WINDOW.clear()
        try:
            server_mod._RATE_WINDOW["9.9.9.9"] = [now] * 120
            scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": [], "client": ("9.9.9.9", 1)}

            async def receive():
                return {"type": "http.request", "body": b"{}", "more_body": False}

            sent = []

            async def send(message):
                sent.append(message)

            await server_mod.app(scope, receive, send)
            start = next(m for m in sent if m["type"] == "http.response.start")
            assert start["status"] == 429
            assert dict(start["headers"])[b"retry-after"] == b"60"
        finally:
            server_mod._RATE_WINDOW.clear()

    asyncio.run(go())


def test_malformed_content_length_does_not_500(monkeypatch):
    async def go():
        start = await _post_mcp(
            b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}',
            monkeypatch,
            headers=[(b"content-type", b"application/json"), (b"content-length", b"not-a-number")],
        )
        # Not a 500 — falls through to the initialize gate (400).
        assert start["status"] == 400

    asyncio.run(go())


def test_cors_sends_vary_even_when_origin_rejected(monkeypatch):
    monkeypatch.setattr(server_mod, "MCP_ALLOWED_ORIGIN", "localhost")
    headers = dict(server_mod._cors_headers({"headers": [(b"origin", b"http://evil.example")]}))
    assert b"access-control-allow-origin" not in headers
    assert headers[b"vary"] == b"Origin"


def test_trusted_cidrs_env_override(monkeypatch):
    monkeypatch.setenv("MCP_TRUSTED_CIDRS", "10.50.0.0/16, not-a-cidr")
    cidrs = server_mod._get_trusted_cidrs()
    assert "10.50.0.0/16" in cidrs
    assert "127.0.0.0/8" in cidrs and "::1/128" in cidrs
    assert "not-a-cidr" not in cidrs


# ---------------------------------------------------------------------------
#  Tool-output formatting
# ---------------------------------------------------------------------------

def test_format_scrape_result_inlines_tables():
    result = {
        "url": "https://a.com",
        "title": "T",
        "markdown": "body",
        "tables": [{"rows": [["Name", "Qty"], ["foo", "3"]]}],
    }
    out = server_mod._format_scrape_result(result)
    assert "1 table(s)" in out
    assert "| Name | Qty |" in out
    assert "| foo | 3 |" in out


def test_format_scrape_result_skips_empty_tables():
    result = {"url": "https://a.com", "markdown": "body", "tables": [{"rows": []}]}
    out = server_mod._format_scrape_result(result)
    assert "1 table(s)" in out
    assert "|" not in out.replace("1 table(s)", "")


def test_format_search_results_notes_unresponsive_engines():
    result = {
        "number_of_results": 1,
        "results": [{"title": "A", "url": "https://a.com"}],
        "unresponsive_engines": [["bing", "timeout"], "google"],
    }
    out = server_mod._format_search_results(result)
    assert "unresponsive" in out
    assert "bing" in out and "google" in out


def test_format_search_results_no_engine_note_when_all_healthy():
    result = {"number_of_results": 1, "results": [{"title": "A", "url": "https://a.com"}]}
    assert "unresponsive" not in server_mod._format_search_results(result)
