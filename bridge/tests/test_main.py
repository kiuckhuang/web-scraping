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
    """A search result pointing at a private address must not reach Fortress."""
    import bridge.main as main_mod

    async def fake_search(*args, **kwargs):
        return {
            "number_of_results": 2,
            "results": [
                {"url": "http://192.168.1.1/admin", "title": "internal"},
                {"url": "https://example.com/page", "title": "public"},
            ],
        }

    async def fake_scrape(url, *, mode):
        return {"url": url, "markdown": "scraped!"}

    monkeypatch.setattr(socket, "getaddrinfo", _mixed_getaddrinfo)
    monkeypatch.setattr(main_mod, "searxng_search", fake_search)
    monkeypatch.setattr(main_mod, "fortress_scrape", fake_scrape)

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
    import bridge.fortress_client as fortress_mod

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
    monkeypatch.setattr(fortress_mod, "ISOLATE_CONTEXTS", True)
    asyncio.run(fortress_mod._close_page(page))
    assert page.closed is True
    assert page.context.closed is True


# ---------------------------------------------------------------------------
#  Scrape cache
# ---------------------------------------------------------------------------

def test_scrape_cache_hits(monkeypatch):
    """Repeated scrapes of the same URL within the TTL skip the browser."""
    import bridge.main as main_mod

    calls = {"n": 0}

    async def fake_scrape(url, *, mode):
        calls["n"] += 1
        return {"url": url, "markdown": "cached body"}

    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "fortress_scrape", fake_scrape)

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

    async def fake_scrape(url, *, mode):
        calls["n"] += 1
        return {"url": url, "markdown": "just a moment...", "waf_challenge": True}

    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "fortress_scrape", fake_scrape)

    req = ScrapeRequest(url="https://example.com/challenge", mode="extract")
    asyncio.run(main_mod.scrape(req))
    asyncio.run(main_mod.scrape(req))
    assert calls["n"] == 2  # a cached challenge would have made this 1


def test_cache_skips_http_error_pages(monkeypatch):
    """4xx/5xx responses are transient — they must not stick for the TTL."""
    import bridge.main as main_mod

    calls = {"n": 0}

    async def fake_scrape(url, *, mode):
        calls["n"] += 1
        return {"url": url, "text": "error", "status": 503}

    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "fortress_scrape", fake_scrape)

    req = ScrapeRequest(url="https://example.com/flaky", mode="fetch")
    asyncio.run(main_mod.scrape(req))
    asyncio.run(main_mod.scrape(req))
    assert calls["n"] == 2


def test_cache_keeps_ok_status(monkeypatch):
    import bridge.main as main_mod

    calls = {"n": 0}

    async def fake_scrape(url, *, mode):
        calls["n"] += 1
        return {"url": url, "markdown": "fine", "status": 200}

    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "fortress_scrape", fake_scrape)

    req = ScrapeRequest(url="https://example.com/ok", mode="extract")
    asyncio.run(main_mod.scrape(req))
    asyncio.run(main_mod.scrape(req))
    assert calls["n"] == 1


def test_cache_respects_max_entries(monkeypatch):
    """The cache is size-bounded: old entries are evicted."""
    import bridge.main as main_mod

    async def fake_scrape(url, *, mode):
        return {"url": url, "markdown": "x"}

    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "fortress_scrape", fake_scrape)
    monkeypatch.setattr(main_mod, "BRIDGE_CACHE_MAX", 2)

    for i in range(3):
        req = ScrapeRequest(url=f"https://example.com/{i}", mode="extract")
        asyncio.run(main_mod.scrape(req))
    assert len(main_mod._cache) <= 2


def test_cache_respects_max_bytes(monkeypatch):
    """The cache evicts entries when the serialized byte budget is exceeded."""
    import bridge.main as main_mod

    async def fake_scrape(url, *, mode):
        return {"url": url, "markdown": "x" * 100}

    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "fortress_scrape", fake_scrape)
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

    async def fake_fortress_health():
        return True

    monkeypatch.setattr(main_mod, "searxng_health", fake_searxng_health)
    monkeypatch.setattr(main_mod, "fortress_health", fake_fortress_health)

    async def run():
        transport = httpx.ASGITransport(app=main_mod.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            # The engine label reflects deployment config (BROWSER_ENGINE env at
            # import time) — CI defaults to fortress while a container deployed
            # with the Camoufox engine reports camoufox. Assert the wiring, not
            # the configured value, then prove the label is engine-driven.
            assert data["engine"] == main_mod.BROWSER_ENGINE
            assert data["services"]["searxng"] == "up"
            assert data["services"]["browser"] == "up"
            assert "x-request-id" in resp.headers
            monkeypatch.setattr(main_mod, "BROWSER_ENGINE", "camoufox")
            resp2 = await client.get("/health")
            assert resp2.json()["engine"] == "camoufox"
            monkeypatch.setattr(main_mod, "BROWSER_ENGINE", "fortress")
            resp3 = await client.get("/health")
            assert resp3.json()["engine"] == "fortress"

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
