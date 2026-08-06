"""Unit tests for the bridge REST API helpers (no network required).

Run inside the bridge container:  python -m pytest tests -q
or via CI / `make test-unit`.
"""

import asyncio
import socket

import pytest
from fastapi import HTTPException

from bridge.main import (
    ScrapeRequest,
    SearchAndScrapeRequest,
    _is_public_url,
    _validate_public_url,
)


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


def test_close_page_closes_page_and_isolated_context():
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
    old_isolate = fortress_mod.ISOLATE_CONTEXTS
    fortress_mod.ISOLATE_CONTEXTS = True
    try:
        asyncio.run(fortress_mod._close_page(page))
        assert page.closed is True
        assert page.context.closed is True
    finally:
        fortress_mod.ISOLATE_CONTEXTS = old_isolate


def test_scrape_cache_hits(monkeypatch):
    """Repeated scrapes of the same URL within the TTL skip the browser."""
    import bridge.main as main_mod

    calls = {"n": 0}

    async def fake_scrape(url, *, mode):
        calls["n"] += 1
        return {"url": url, "markdown": "cached body"}

    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "fortress_scrape", fake_scrape)
    main_mod._cache.clear()
    main_mod._cache_bytes = 0

    try:
        req = ScrapeRequest(url="https://example.com/", mode="extract")
        first = asyncio.run(main_mod.scrape(req))
        second = asyncio.run(main_mod.scrape(req))
        assert calls["n"] == 1
        assert "cached" not in first
        assert second["cached"] is True
        assert second["markdown"] == "cached body"
    finally:
        main_mod._cache.clear()
        main_mod._cache_bytes = 0


def test_cache_respects_max_entries(monkeypatch):
    """The cache is size-bounded: old entries are evicted."""
    import bridge.main as main_mod

    async def fake_scrape(url, *, mode):
        return {"url": url, "markdown": "x"}

    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "fortress_scrape", fake_scrape)
    old_max = main_mod.BRIDGE_CACHE_MAX
    main_mod.BRIDGE_CACHE_MAX = 2
    main_mod._cache.clear()
    main_mod._cache_bytes = 0
    try:
        for i in range(3):
            req = ScrapeRequest(url=f"https://example.com/{i}", mode="extract")
            asyncio.run(main_mod.scrape(req))
        assert len(main_mod._cache) <= 2
    finally:
        main_mod.BRIDGE_CACHE_MAX = old_max
        main_mod._cache.clear()
        main_mod._cache_bytes = 0


def test_cache_respects_max_bytes(monkeypatch):
    """The cache evicts entries when the serialized byte budget is exceeded."""
    import bridge.main as main_mod

    async def fake_scrape(url, *, mode):
        return {"url": url, "markdown": "x" * 100}

    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "fortress_scrape", fake_scrape)
    old_bytes = main_mod.BRIDGE_CACHE_MAX_BYTES
    main_mod.BRIDGE_CACHE_MAX_BYTES = 150
    main_mod._cache.clear()
    main_mod._cache_bytes = 0
    try:
        for i in range(3):
            req = ScrapeRequest(url=f"https://example.com/{i}", mode="extract")
            asyncio.run(main_mod.scrape(req))
        assert main_mod._cache_bytes <= main_mod.BRIDGE_CACHE_MAX_BYTES
    finally:
        main_mod.BRIDGE_CACHE_MAX_BYTES = old_bytes
        main_mod._cache.clear()
        main_mod._cache_bytes = 0
