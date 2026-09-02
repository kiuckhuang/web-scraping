"""Unit tests for the bridge REST API helpers (no network required).

Run inside the bridge container:  python -m pytest tests -q
or via CI / `make test-unit`.
"""

import asyncio
import re
import socket

import pytest
from bridge.main import (
    ScrapeRequest,
    SearchAndScrapeRequest,
    _is_public_url,
    _validate_public_url,
)
from fastapi import HTTPException

from bridge import ssrf


@pytest.fixture(autouse=True)
def _reset_caches():
    """Bridge scrape cache and SSRF DNS verdicts must not leak between tests."""
    import bridge.main as main_mod

    main_mod._cache.clear()
    main_mod._cache_bytes = 0
    ssrf.clear_dns_cache()
    yield
    main_mod._cache.clear()
    main_mod._cache_bytes = 0
    ssrf.clear_dns_cache()


@pytest.fixture(autouse=True)
def _pin_searxng_primary(monkeypatch):
    """Tests here fake `searxng_search` and assert on its exact results.

    The search endpoints route through `_search_with_fallbacks`, whose stage
    order follows the deployed SEARCH_PRIMARY. With the default
    SEARCH_PRIMARY=browser the browser-SERP stage runs a *live* search before
    the fake is ever reached (network-dependent, machine-dependent results —
    and a real SERP hit on every container test run). Pin the chain to the
    SearXNG stage only; fallback behavior has dedicated tests in
    test_search_fallback.py, which fake every stage it exercises.
    """
    import bridge.main as main_mod

    monkeypatch.setattr(main_mod, "SEARCH_PRIMARY", "searxng")
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BING", False)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BROWSER", False)


@pytest.fixture(autouse=True)
def _disable_http_fastpath(monkeypatch):
    """The HTTP fast path (curl_cffi) would otherwise attempt real network
    fetches in every test that exercises /scrape or search_and_scrape.
    Escalate unconditionally so tests here drive the browser path; fast-path
    behavior is covered in test_http_client.py and the transports tests below,
    which override this stub explicitly.
    """
    import bridge.main as main_mod
    from bridge.http_client import Escalation

    async def _escalate(url, *, mode):
        raise Escalation("disabled in unit tests")

    monkeypatch.setattr(main_mod, "http_scrape", _escalate)


def _public_getaddrinfo(host, port=None, family=0, type=0, proto=0, flags=0):
    """Fake resolver: everything resolves to a public IP."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


def _mixed_getaddrinfo(host, port=None, family=0, type=0, proto=0, flags=0):
    """Fake resolver: 192.x hosts resolve privately, everything else publicly."""
    if host.startswith("192.") or host == "localhost":
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 0))]
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


def test_rejects_localhost_names():
    for host in ("localhost", "127.0.0.1", "0.0.0.0", "[::1]"):
        with pytest.raises(HTTPException) as exc:
            _validate_public_url(f"http://{host}:8000/admin")
        assert exc.value.status_code == 403


def test_rejects_private_and_link_local_ips():
    for ip in (
        "10.0.0.1",
        "192.168.1.10",
        "172.16.0.5",
        "127.0.0.1",
        "169.254.169.254",
        "224.0.0.1",
        "0.0.0.0",
        "::1",
        "::ffff:127.0.0.1",
        "::ffff:192.168.1.1",
    ):
        with pytest.raises(HTTPException) as exc:
            _validate_public_url(f"http://[{ip}]/" if ":" in ip else f"http://{ip}/")
        assert exc.value.status_code == 403


def test_accepts_scheme_case_insensitive(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    assert _validate_public_url("HTTP://example.com/") == "HTTP://example.com/"
    assert _validate_public_url("Https://example.com/") == "Https://example.com/"


def test_rejects_non_http_schemes():
    with pytest.raises(HTTPException) as exc:
        _validate_public_url("ftp://example.com/file")
    assert exc.value.status_code == 400
    with pytest.raises(HTTPException) as exc:
        _validate_public_url("file:///etc/passwd")
    assert exc.value.status_code == 400


def test_rejects_unresolvable_host():
    # .invalid is a reserved TLD that must never resolve — the validator
    # rejects it rather than letting the browser re-resolve later.
    with pytest.raises(HTTPException) as exc:
        _validate_public_url("http://definitely-not-a-real-host.invalid/")
    assert exc.value.status_code == 403


def test_rejects_missing_host():
    with pytest.raises(HTTPException) as exc:
        _validate_public_url("http:///path-only")
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


def test_search_and_scrape_limits_results():
    with pytest.raises(ValueError):
        SearchAndScrapeRequest(query="q", max_results=51)
    with pytest.raises(ValueError):
        SearchAndScrapeRequest(query="q", max_results=0)


def test_is_public_url(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mixed_getaddrinfo)
    assert _is_public_url("https://example.com/") is True
    assert _is_public_url("http://localhost:8000/") is False
    assert _is_public_url("ftp://example.com/") is False
    assert _is_public_url("http://192.168.1.1/") is False


def test_search_and_scrape_skips_private_urls(monkeypatch):
    """A search result pointing at a private address must not reach the browser."""
    import bridge.main as main_mod

    async def fake_search(*args, **kwargs):
        return {
            "number_of_results": 2,
            "results": [
                {"url": "http://192.168.1.1/admin", "title": "internal"},
                {"url": "https://example.com/page", "title": "public"},
            ],
        }

    async def fake_scrape(url, *, mode, session=None):
        return {"url": url, "markdown": "scraped!"}

    monkeypatch.setattr(socket, "getaddrinfo", _mixed_getaddrinfo)
    monkeypatch.setattr(main_mod, "searxng_search", fake_search)
    monkeypatch.setattr(main_mod, "browser_scrape", fake_scrape)

    req = SearchAndScrapeRequest(query="q", max_results=5)
    out = asyncio.run(main_mod.search_and_scrape(req))

    internal = out["results"][0]
    assert internal["content"] is None
    assert "blocked" in internal["scrape_error"]
    assert out["results"][1]["content"]["markdown"] == "scraped!"


# ---------------------------------------------------------------------------
#  Shared SSRF guard (bridge.bridge.ssrf)
# ---------------------------------------------------------------------------

def test_browser_guard_allows_inert_schemes():
    """data:/blob:/about: are Chromium-internal and carry no network authority."""
    assert ssrf.is_public_http_url("data:text/html,hello") is True
    assert ssrf.is_public_http_url("blob:https://example.com/uuid") is True
    assert ssrf.is_public_http_url("about:blank") is True


def test_edge_validator_rejects_inert_schemes():
    """The edge validator is stricter than the browser guard: no data:/file:."""
    with pytest.raises(HTTPException) as exc:
        _validate_public_url("data:text/html,hello")
    assert exc.value.status_code == 400


def test_browser_guard_blocks_private(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mixed_getaddrinfo)
    assert ssrf.is_public_http_url("http://192.168.1.1/x") is False
    assert ssrf.is_public_http_url("http://example.com/x") is True


def test_dns_verdict_is_cached(monkeypatch):
    """Repeated verdicts within the TTL resolve only once (subresource flood guard)."""
    calls = {"n": 0}

    def counting_getaddrinfo(host, *args, **kwargs):
        calls["n"] += 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(ssrf, "DNS_CACHE_TTL", 60.0)
    monkeypatch.setattr(socket, "getaddrinfo", counting_getaddrinfo)
    assert ssrf.dns_verdict("example.com") == "public"
    assert ssrf.dns_verdict("example.com") == "public"
    assert calls["n"] == 1


def test_dns_cache_disabled_with_zero_ttl(monkeypatch):
    monkeypatch.setattr(ssrf, "DNS_CACHE_TTL", 0.0)
    calls = {"n": 0}

    def counting_getaddrinfo(host, *args, **kwargs):
        calls["n"] += 1
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", counting_getaddrinfo)
    ssrf.dns_verdict("example.com")
    ssrf.dns_verdict("example.com")
    assert calls["n"] == 2
    assert ssrf._dns_cache == {}


def test_dns_cache_prunes_oldest_when_full(monkeypatch):
    monkeypatch.setattr(ssrf, "DNS_CACHE_TTL", 60.0)
    monkeypatch.setattr(ssrf, "DNS_CACHE_MAX", 4)
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    for i in range(6):
        ssrf.dns_verdict(f"host{i}.example.com")
    assert len(ssrf._dns_cache) <= 4


def test_close_page_closes_page_and_isolated_context(monkeypatch):
    import bridge.browser_client as browser_mod

    class FakeContext:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    class FakePage:
        def __init__(self):
            self.context = FakeContext()
            self.closed = False

        async def close(self):
            self.closed = True

    page = FakePage()
    monkeypatch.setattr(browser_mod, "ISOLATE_CONTEXTS", True)
    asyncio.run(browser_mod._close_page(page))
    assert page.closed is True
    assert page.context.closed is True


# ---------------------------------------------------------------------------
#  Scrape cache
# ---------------------------------------------------------------------------

def test_scrape_cache_hits(monkeypatch):
    """Repeated scrapes of the same URL within the TTL skip the browser."""
    import bridge.main as main_mod

    calls = {"n": 0}

    async def fake_scrape(url, *, mode, session=None):
        calls["n"] += 1
        return {"url": url, "markdown": "cached body"}

    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "browser_scrape", fake_scrape)

    req = ScrapeRequest(url="https://example.com/", mode="extract")
    first = asyncio.run(main_mod.scrape(req))
    second = asyncio.run(main_mod.scrape(req))
    assert calls["n"] == 1
    assert "cached" not in first
    assert second["cached"] is True
    assert second["markdown"] == "cached body"


def test_cache_skips_waf_challenge_pages(monkeypatch):
    """WAF challenge pages are served but never cached — retry must re-scrape."""
    import bridge.main as main_mod

    calls = {"n": 0}

    async def fake_scrape(url, *, mode, session=None):
        calls["n"] += 1
        return {"url": url, "markdown": "just a moment...", "waf_challenge": True}

    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "browser_scrape", fake_scrape)

    req = ScrapeRequest(url="https://example.com/challenge", mode="extract")
    asyncio.run(main_mod.scrape(req))
    asyncio.run(main_mod.scrape(req))
    assert calls["n"] == 2  # a cached challenge would have made this 1


def test_cache_skips_http_error_pages(monkeypatch):
    """4xx/5xx responses are transient — they must not stick for the TTL."""
    import bridge.main as main_mod

    calls = {"n": 0}

    async def fake_scrape(url, *, mode, session=None):
        calls["n"] += 1
        return {"url": url, "text": "error", "status": 503}

    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "browser_scrape", fake_scrape)

    req = ScrapeRequest(url="https://example.com/flaky", mode="fetch")
    asyncio.run(main_mod.scrape(req))
    asyncio.run(main_mod.scrape(req))
    assert calls["n"] == 2


def test_cache_keeps_ok_status(monkeypatch):
    import bridge.main as main_mod

    calls = {"n": 0}

    async def fake_scrape(url, *, mode, session=None):
        calls["n"] += 1
        return {"url": url, "markdown": "fine", "status": 200}

    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "browser_scrape", fake_scrape)

    req = ScrapeRequest(url="https://example.com/ok", mode="extract")
    asyncio.run(main_mod.scrape(req))
    asyncio.run(main_mod.scrape(req))
    assert calls["n"] == 1


def test_cache_respects_max_entries(monkeypatch):
    """The cache is size-bounded: old entries are evicted."""
    import bridge.main as main_mod

    async def fake_scrape(url, *, mode, session=None):
        return {"url": url, "markdown": "x"}

    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "browser_scrape", fake_scrape)
    monkeypatch.setattr(main_mod, "BRIDGE_CACHE_MAX", 2)

    for i in range(3):
        req = ScrapeRequest(url=f"https://example.com/{i}", mode="extract")
        asyncio.run(main_mod.scrape(req))
    assert len(main_mod._cache) <= 2


def test_cache_respects_max_bytes(monkeypatch):
    """The cache evicts entries when the serialized byte budget is exceeded."""
    import bridge.main as main_mod

    async def fake_scrape(url, *, mode, session=None):
        return {"url": url, "markdown": "x" * 100}

    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "browser_scrape", fake_scrape)
    monkeypatch.setattr(main_mod, "BRIDGE_CACHE_MAX_BYTES", 150)

    for i in range(3):
        req = ScrapeRequest(url=f"https://example.com/{i}", mode="extract")
        asyncio.run(main_mod.scrape(req))
    assert main_mod._cache_bytes <= main_mod.BRIDGE_CACHE_MAX_BYTES


# ---------------------------------------------------------------------------
#  HTTP layer
# ---------------------------------------------------------------------------

def test_health_endpoint_routes(monkeypatch):
    import bridge.main as main_mod
    import httpx

    async def fake_searxng_health():
        return True

    async def fake_browser_health():
        return True

    monkeypatch.setattr(main_mod, "searxng_health", fake_searxng_health)
    monkeypatch.setattr(main_mod, "browser_health", fake_browser_health)

    async def run():
        transport = httpx.ASGITransport(app=main_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            # The engine label is static now that Camoufox is the only engine.
            assert data["engine"] == "camoufox"
            assert data["services"]["searxng"] == "up"
            assert data["services"]["browser"] == "up"
            assert "x-request-id" in resp.headers

    asyncio.run(run())


def test_request_id_echoes_valid_client_id(monkeypatch):
    import bridge.main as main_mod
    import httpx

    async def run():
        transport = httpx.ASGITransport(app=main_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health", headers={"X-Request-ID": "abc-123._x"})
            assert resp.headers["x-request-id"] == "abc-123._x"

    asyncio.run(run())


def test_request_id_replaces_unsafe_client_id(monkeypatch):
    """Oversized/odd client IDs must not be echoed back or logged verbatim."""
    import bridge.main as main_mod
    import httpx

    unsafe = "bad id with spaces and a very long " + "x" * 200

    async def run():
        transport = httpx.ASGITransport(app=main_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health", headers={"X-Request-ID": unsafe})
            echoed = resp.headers["x-request-id"]
            assert echoed != unsafe
            assert re.fullmatch(r"[0-9a-f]{8}", echoed)

    asyncio.run(run())


def test_scrape_endpoint_blocks_private_url():
    import bridge.main as main_mod
    import httpx

    async def run():
        transport = httpx.ASGITransport(app=main_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/scrape", json={"url": "http://127.0.0.1:8000/admin", "mode": "extract"})
            assert resp.status_code == 403
            assert "internal" in resp.json()["detail"]

    asyncio.run(run())


def test_search_endpoint_routes(monkeypatch):
    import bridge.main as main_mod
    import httpx

    async def fake_search(q, **kwargs):
        return {"query": q, "number_of_results": 1, "results": [{"title": "Test", "url": "https://example.com"}]}

    monkeypatch.setattr(main_mod, "searxng_search", fake_search)

    async def run():
        transport = httpx.ASGITransport(app=main_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/search?q=test")
            assert resp.status_code == 200
            assert resp.json()["query"] == "test"

    asyncio.run(run())


# ---------------------------------------------------------------------------
#  Named sessions API
# ---------------------------------------------------------------------------

def test_sessions_endpoints(monkeypatch):
    import bridge.main as main_mod
    import httpx

    async def fake_create(name):
        return True

    deleted = set()

    async def fake_close(name):
        if name in deleted:
            return False
        deleted.add(name)
        return True

    monkeypatch.setattr(main_mod, "browser_create_session", fake_create)
    monkeypatch.setattr(main_mod, "browser_list_sessions", lambda: ["work"])
    monkeypatch.setattr(main_mod, "browser_close_session", fake_close)

    async def run():
        transport = httpx.ASGITransport(app=main_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/sessions", json={"name": "work"})
            assert resp.status_code == 200 and resp.json() == {"name": "work", "created": True}

            resp = await client.get("/sessions")
            assert resp.json() == {"sessions": ["work"]}

            resp = await client.delete("/sessions/work")
            assert resp.status_code == 200 and resp.json()["deleted"] is True

            resp = await client.delete("/sessions/work")
            assert resp.status_code == 404  # already deleted

            resp = await client.post("/sessions", json={"name": "bad name!"})
            assert resp.status_code == 422  # invalid name pattern

            resp = await client.delete("/sessions/also_bad!")
            assert resp.status_code == 422

    asyncio.run(run())


def test_scrape_with_session_passes_through_and_caches_separately(monkeypatch):
    """Same URL via different sessions must not share cache entries."""
    import bridge.main as main_mod
    import httpx

    calls = []

    async def fake_scrape(url, *, mode, session=None):
        calls.append((url, mode, session))
        return {"url": url, "markdown": f"body for {session}"}

    async def fake_public(url):
        return True

    monkeypatch.setattr(main_mod, "browser_scrape", fake_scrape)
    monkeypatch.setattr(main_mod, "_validate_public_url", lambda url: url)

    async def run():
        transport = httpx.ASGITransport(app=main_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for session in ("a", "b", None):
                payload = {"url": "https://example.com/page", "mode": "extract"}
                if session:
                    payload["session"] = session
                resp = await client.post("/scrape", json=payload)
                assert resp.status_code == 200
                assert "cached" not in resp.json()
            # Distinct cache keys: session a, session b, and sessionless.
            assert len(calls) == 3
            assert calls[0][2] == "a" and calls[1][2] == "b" and calls[2][2] is None

    asyncio.run(run())


# ---------------------------------------------------------------------------
#  Scrape transports (HTTP fast path → stealth browser)
# ---------------------------------------------------------------------------

def test_scrape_endpoint_serves_from_fast_path(monkeypatch):
    import bridge.main as main_mod
    import httpx

    async def fake_http_scrape(url, *, mode):
        return {"url": url, "title": "t", "markdown": "static md", "tables": [], "status": 200}

    async def fail_browser(*args, **kwargs):
        raise AssertionError("browser must not run when the fast path serves")

    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "http_scrape", fake_http_scrape)
    monkeypatch.setattr(main_mod, "browser_scrape", fail_browser)

    async def run():
        transport = httpx.ASGITransport(app=main_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/scrape", json={"url": "https://example.com/fast", "mode": "extract"})
            assert resp.status_code == 200
            body = resp.json()
            assert body["markdown"] == "static md"
            assert body["transport"] == "http"

    asyncio.run(run())


def test_scrape_fast_path_escalation_falls_back_to_browser(monkeypatch):
    import bridge.main as main_mod
    import httpx
    from bridge.http_client import Escalation

    async def escalating(url, *, mode):
        raise Escalation("WAF challenge")

    captured = {}

    async def fake_browser(url, *, mode, session=None):
        captured["call"] = (url, mode, session)
        return {"url": url, "title": "t", "markdown": "browser md", "tables": []}

    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "http_scrape", escalating)
    monkeypatch.setattr(main_mod, "browser_scrape", fake_browser)

    async def run():
        transport = httpx.ASGITransport(app=main_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/scrape", json={"url": "https://example.com/waf", "mode": "extract"})
            assert resp.status_code == 200
            body = resp.json()
            assert body["markdown"] == "browser md"
            assert "transport" not in body  # browser results are unchanged

    asyncio.run(run())
    assert captured["call"] == ("https://example.com/waf", "extract", None)


def test_scrape_with_session_skips_fast_path(monkeypatch):
    import bridge.main as main_mod
    import httpx

    async def fail_http(url, *, mode):
        raise AssertionError("named sessions must not use the cookieless fast path")

    captured = {}

    async def fake_browser(url, *, mode, session=None):
        captured["session"] = session
        return {"url": url, "title": "t", "markdown": "md", "tables": []}

    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "http_scrape", fail_http)
    monkeypatch.setattr(main_mod, "browser_scrape", fake_browser)

    async def run():
        transport = httpx.ASGITransport(app=main_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/scrape", json={"url": "https://example.com/x", "mode": "extract", "session": "work"}
            )
            assert resp.status_code == 200
            assert "transport" not in resp.json()

    asyncio.run(run())
    assert captured["session"] == "work"


def test_http_fastpath_disabled_goes_straight_to_browser(monkeypatch):
    import bridge.main as main_mod
    import httpx

    async def fail_http(url, *, mode):
        raise AssertionError("fast path must not run when HTTP_FASTPATH is off")

    async def fake_browser(url, *, mode, session=None):
        return {"url": url, "title": "t", "markdown": "md", "tables": []}

    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "HTTP_FASTPATH_ENABLED", False)
    monkeypatch.setattr(main_mod, "http_scrape", fail_http)
    monkeypatch.setattr(main_mod, "browser_scrape", fake_browser)

    async def run():
        transport = httpx.ASGITransport(app=main_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/scrape", json={"url": "https://example.com/x", "mode": "extract"})
            assert resp.status_code == 200

    asyncio.run(run())


def test_search_and_scrape_serves_static_results_via_fast_path(monkeypatch):
    import bridge.main as main_mod
    import httpx

    async def fake_search(*args, **kwargs):
        return {
            "number_of_results": 1,
            "results": [{"url": "https://example.com/article", "title": "static"}],
        }

    async def fake_http_scrape(url, *, mode):
        return {"url": url, "title": "t", "markdown": "fast md", "tables": [], "status": 200}

    async def fail_browser(*args, **kwargs):
        raise AssertionError("browser must not run when the fast path serves")

    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "searxng_search", fake_search)
    monkeypatch.setattr(main_mod, "http_scrape", fake_http_scrape)
    monkeypatch.setattr(main_mod, "browser_scrape", fail_browser)

    async def run():
        transport = httpx.ASGITransport(app=main_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/search_and_scrape", json={"query": "q", "max_results": 3})
            assert resp.status_code == 200
            body = resp.json()
            assert body["results"][0]["content"]["markdown"] == "fast md"
            assert body["results"][0]["content"]["transport"] == "http"

    asyncio.run(run())
