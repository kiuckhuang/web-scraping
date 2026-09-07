"""Unit tests for the optional Jina AI transports — network-free.

The Jina HTTP seam (`jina_client._request_json`) is monkeypatched, as are the
stage functions inside `bridge.main`. Covers:

- client behavior: gating, normalization, error-envelope decoding
- chain wiring: jina strictly AFTER every self-hosted stage, gating included
- scrape transport: jina only after the browser failed, never for sessions
- /search_and_scrape reuse of Jina's server-side page reads
- /jina_search + /jina_scrape endpoints (disabled state, SSRF, mapping)
"""

from __future__ import annotations

import asyncio
import socket

import bridge.jina_client as jc
import bridge.main as main_mod
import pytest
from bridge.jina_client import JinaError
from bridge.main import ScrapeRequest, SearchAndScrapeRequest
from fastapi import HTTPException

from bridge import ssrf


@pytest.fixture(autouse=True)
def _reset_caches():
    """Bridge scrape cache and SSRF DNS verdicts must not leak between tests."""
    main_mod._cache.clear()
    main_mod._cache_bytes = 0
    ssrf.clear_dns_cache()
    yield
    main_mod._cache.clear()
    main_mod._cache_bytes = 0
    ssrf.clear_dns_cache()


@pytest.fixture(autouse=True)
def _jina_enabled(monkeypatch):
    """Most tests exercise the enabled path; the disabled-state tests override."""
    monkeypatch.setattr(jc, "JINA_ENABLED", True)
    monkeypatch.setattr(jc, "JINA_API_KEY", "jina_test_key")


@pytest.fixture(autouse=True)
def _pin_chain(monkeypatch):
    """Pin the classic chain so tests drive stages explicitly."""
    monkeypatch.setattr(main_mod, "SEARCH_PRIMARY", "searxng")
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BING", True)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BROWSER", True)


def _public_getaddrinfo(host, port=None, family=0, type=0, proto=0, flags=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


def _mixed_getaddrinfo(host, port=None, family=0, type=0, proto=0, flags=0):
    """Fake resolver: 192.x hosts resolve privately, everything else publicly."""
    if host.startswith("192.") or host == "localhost":
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 0))]
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


def _serve_json(monkeypatch, handler):
    """Replace the Jina request seam; handler(method, url, json_body, headers)."""
    calls: list[dict] = []

    async def fake_request(method, url, *, json_body=None, headers=None):
        calls.append({"method": method, "url": url, "json_body": json_body, "headers": headers or {}})
        return handler(method, url, json_body, headers or {})

    monkeypatch.setattr(jc, "_request_json", fake_request)
    return calls


def _hit(title: str = "Example") -> dict:
    return {"title": title, "url": "https://example.com/", "content": "snippet", "engine": "x"}


def _searxng_response(results: list[dict]) -> dict:
    return {"query": "q", "number_of_results": len(results), "results": results, "unresponsive_engines": []}


def _run_search(**kwargs) -> dict:
    defaults = {"categories": None, "language": "en", "pageno": 1, "time_range": None, "safesearch": 0, "max_results": 10}
    defaults.update(kwargs)
    return asyncio.run(main_mod._search_with_fallbacks("q", **defaults))


# ---------------------------------------------------------------------------
#  Client — search
# ---------------------------------------------------------------------------

def test_search_disabled_returns_503(monkeypatch):
    monkeypatch.setattr(jc, "JINA_ENABLED", False)
    with pytest.raises(JinaError) as exc:
        asyncio.run(jc.search("q"))
    assert exc.value.bridge_status == 503


def test_search_without_key_returns_503(monkeypatch):
    """s.jina.ai requires an API key — refuse before spending the request."""
    monkeypatch.setattr(jc, "JINA_API_KEY", "")
    with pytest.raises(JinaError) as exc:
        asyncio.run(jc.search("q"))
    assert exc.value.bridge_status == 503
    assert "JINA_API_KEY" in str(exc.value)


def test_search_normalizes_to_searxng_shape(monkeypatch):
    payload = {
        "code": 200,
        "data": [
            {"title": "T1", "url": "https://a.example/", "description": "desc", "content": "# full page"},
            {"title": "T2", "url": "https://b.example/", "description": "", "content": "body text"},
            {"url": ""},  # dropped — no url
        ],
    }
    calls = _serve_json(monkeypatch, lambda *a: (200, payload))
    out = asyncio.run(jc.search("q", max_results=3))
    assert calls[0]["method"] == "POST"
    assert calls[0]["json_body"] == {"q": "q", "num": 3}
    assert calls[0]["headers"]["Accept"] == "application/json"
    assert out["results"] == [
        {"title": "T1", "url": "https://a.example/", "content": "desc", "engine": "jina"},
        {"title": "T2", "url": "https://b.example/", "content": "body text", "engine": "jina"},
    ]
    assert out["engine"] == "jina"
    assert out["jina_pages"]["https://a.example/"] == {"title": "T1", "markdown": "# full page"}


def test_search_num_clamped_to_jina_limit(monkeypatch):
    """s.jina.ai validates num in 0..20 — larger values must be clamped."""
    calls = _serve_json(monkeypatch, lambda *a: (200, {"code": 200, "data": []}))
    asyncio.run(jc.search("q", max_results=50))
    assert calls[0]["json_body"]["num"] == 20


def test_search_401_maps_to_auth_error(monkeypatch):
    envelope = {"code": 401, "status": 40103, "name": "AuthenticationRequiredError", "message": "Authentication is required"}
    _serve_json(monkeypatch, lambda *a: (401, envelope))
    with pytest.raises(JinaError) as exc:
        asyncio.run(jc.search("q"))
    assert "JINA_API_KEY" in str(exc.value)


def test_search_429_surfaces_retry_after(monkeypatch):
    envelope = {"code": 429, "status": 42903, "name": "RateLimitTriggeredError", "message": "Per IP rate limit exceeded", "retryAfter": 7}
    _serve_json(monkeypatch, lambda *a: (429, envelope))
    with pytest.raises(JinaError) as exc:
        asyncio.run(jc.search("q"))
    assert exc.value.bridge_status == 429
    assert exc.value.retry_after == 7.0


def test_search_upstream_500_maps_to_502(monkeypatch):
    envelope = {"code": 500, "status": 50002, "name": "DownstreamServiceError", "message": "boom"}
    _serve_json(monkeypatch, lambda *a: (500, envelope))
    with pytest.raises(JinaError) as exc:
        asyncio.run(jc.search("q"))
    assert exc.value.bridge_status == 502


# ---------------------------------------------------------------------------
#  Client — scrape (r.jina.ai)
# ---------------------------------------------------------------------------

def test_scrape_disabled_returns_503(monkeypatch):
    monkeypatch.setattr(jc, "JINA_ENABLED", False)
    with pytest.raises(JinaError) as exc:
        asyncio.run(jc.scrape("https://example.com/"))
    assert exc.value.bridge_status == 503


def test_scrape_extract_mode_returns_markdown(monkeypatch):
    payload = {"code": 200, "data": {"title": "Page", "content": "# hello", "url": "https://example.com/", "httpStatus": 200}}
    calls = _serve_json(monkeypatch, lambda *a: (200, payload))
    out = asyncio.run(jc.scrape("https://example.com/", mode="extract"))
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == f"{jc.JINA_READER_URL}/https://example.com/"
    assert out == {
        "url": "https://example.com/",
        "title": "Page",
        "markdown": "# hello",
        "tables": [],
        "status": 200,
    }


def test_scrape_fetch_mode_requests_html(monkeypatch):
    payload = {"code": 200, "data": {"title": "Page", "html": "<html><body>raw</body></html>", "httpStatus": 200}}
    calls = _serve_json(monkeypatch, lambda *a: (200, payload))
    out = asyncio.run(jc.scrape("https://example.com/", mode="fetch"))
    assert calls[0]["headers"]["X-Respond-With"] == "html"
    assert out["html"] == "<html><body>raw</body></html>"
    assert "raw" in out["text"]


def test_scrape_fetch_mode_without_html_derives_text(monkeypatch):
    """When Jina returns no html field, text falls back to content."""
    payload = {"code": 200, "data": {"title": "Page", "content": "plain content", "httpStatus": 200}}
    _serve_json(monkeypatch, lambda *a: (200, payload))
    out = asyncio.run(jc.scrape("https://example.com/", mode="fetch"))
    assert out["text"] == "plain content"


# ---------------------------------------------------------------------------
#  Chain wiring — jina strictly after every self-hosted stage
# ---------------------------------------------------------------------------

def _empty_all_stages(monkeypatch, order: list[str]):
    async def fake_searxng(query, **_k):
        order.append(f"searxng:{query}")
        return _searxng_response([])

    async def fake_browser(query, count=10, engines=None):
        order.append("browser")
        return {"engine": "duckduckgo", "results": []}

    async def fake_jina(query, *, max_results=5):
        order.append("jina")
        return {
            "query": query,
            "number_of_results": 1,
            "results": [{"title": "J", "url": "https://j.example/", "content": "snippet", "engine": "jina"}],
            "engine": "jina",
            "jina_pages": {"https://j.example/": {"title": "J", "markdown": "# j"}},
        }

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "browser_web_search", fake_browser)
    monkeypatch.setattr(main_mod, "jina_search", fake_jina)


def test_jina_is_the_final_stage(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(main_mod, "JINA_SEARCH_FALLBACK", True)
    _empty_all_stages(monkeypatch, order)
    resp = _run_search()
    assert order == ["searxng:q", "searxng:!bing q", "browser", "jina"]
    assert resp["fallback"] == "jina"
    assert resp["results"][0]["engine"] == "jina"
    # The full page reads ride along for /search_and_scrape to reuse.
    assert resp["jina_pages"]["https://j.example/"]["markdown"] == "# j"


def test_jina_stage_absent_when_disabled(monkeypatch):
    """With the Jina stage off, an all-stages-empty run returns the (empty)
    primary SearXNG response — exactly the pre-Jina behavior."""
    order: list[str] = []
    monkeypatch.setattr(main_mod, "JINA_SEARCH_FALLBACK", False)
    _empty_all_stages(monkeypatch, order)
    resp = _run_search()
    assert "jina" not in order
    assert resp["results"] == []


def test_jina_stage_error_swallowed_when_primary_alive(monkeypatch):
    """A Jina failure must degrade like any other stage error — the (empty)
    primary response is returned, the exception only logged."""

    async def fake_searxng(query, **_k):
        return _searxng_response([])

    async def fake_jina(query, *, max_results=5):
        raise JinaError("Jina rate limit exceeded", bridge_status=429, retry_after=1.0)

    monkeypatch.setattr(main_mod, "JINA_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BING", False)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BROWSER", False)
    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "jina_search", fake_jina)
    resp = _run_search()
    assert resp["results"] == []


def test_jina_error_with_searxng_down_raises_502(monkeypatch):
    """SearXNG unreachable AND Jina failing: nothing served → 502."""

    async def fake_searxng(query, **_k):
        raise RuntimeError("connection refused")

    async def fake_jina(query, *, max_results=5):
        raise JinaError("Jina API error 500: boom")

    monkeypatch.setattr(main_mod, "JINA_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BING", False)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BROWSER", False)
    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "jina_search", fake_jina)
    with pytest.raises(HTTPException) as excinfo:
        _run_search()
    assert excinfo.value.status_code == 502


def test_jina_never_runs_when_self_hosted_stages_serve(monkeypatch):
    async def fake_searxng(query, **_k):
        return _searxng_response([_hit()])

    def fail(*_a, **_k):
        raise AssertionError("jina must not run when earlier stages serve results")

    monkeypatch.setattr(main_mod, "JINA_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "jina_search", fail)
    resp = _run_search()
    assert "fallback" not in resp
    assert resp["results"][0]["engine"] == "x"


@pytest.mark.parametrize("kwargs", [{"pageno": 2}, {"categories": "images"}])
def test_jina_respects_fallback_gating(monkeypatch, kwargs):
    monkeypatch.setattr(main_mod, "JINA_SEARCH_FALLBACK", True)

    async def fake_searxng(query, **_k):
        return _searxng_response([])

    def fail(*_a, **_k):
        raise AssertionError("jina must respect the page-1 general-web gate")

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "jina_search", fail)
    resp = _run_search(**kwargs)
    assert resp["results"] == []


def test_search_endpoint_strips_jina_pages(monkeypatch):
    """The /search response must never carry full page markdown per result."""
    async def fake_searxng(query, **_k):
        return _searxng_response([])

    async def fake_jina(query, *, max_results=5):
        return {
            "query": query,
            "number_of_results": 1,
            "results": [_hit("Jina hit")],
            "engine": "jina",
            "jina_pages": {"https://example.com/": {"title": "T", "markdown": "# big"}},
        }

    monkeypatch.setattr(main_mod, "JINA_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BING", False)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BROWSER", False)
    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "jina_search", fake_jina)
    resp = asyncio.run(main_mod.search(q="q", categories=None, language="en", pageno=1, time_range=None, safesearch=0, max_results=10))
    assert "jina_pages" not in resp
    assert resp["fallback"] == "jina"


# ---------------------------------------------------------------------------
#  Scrape transports — jina only after the browser failed
# ---------------------------------------------------------------------------

def _escalate(url, *, mode):
    from bridge.http_client import Escalation

    raise Escalation("disabled in unit tests")


def test_browser_failure_falls_back_to_jina(monkeypatch):
    async def fail_browser(url, *, mode, session):
        raise RuntimeError("browser gave up (WAF)")

    async def fake_jina_scrape(url, *, mode):
        return {"url": url, "title": "Via Jina", "markdown": "# jina", "tables": [], "status": 200}

    monkeypatch.setattr(main_mod, "http_scrape", _escalate)
    monkeypatch.setattr(main_mod, "browser_scrape", fail_browser)
    monkeypatch.setattr(main_mod, "JINA_SCRAPE_FALLBACK", True)
    monkeypatch.setattr(main_mod, "jina_scrape", fake_jina_scrape)
    out = asyncio.run(main_mod._scrape_with_transports("https://waf.example/", mode="extract", session=None))
    assert out["transport"] == "jina"
    assert out["markdown"] == "# jina"


def test_browser_failure_with_jina_disabled_reraises(monkeypatch):
    async def fail_browser(url, *, mode, session):
        raise RuntimeError("browser gave up")

    monkeypatch.setattr(main_mod, "http_scrape", _escalate)
    monkeypatch.setattr(main_mod, "browser_scrape", fail_browser)
    monkeypatch.setattr(main_mod, "JINA_SCRAPE_FALLBACK", False)

    def fail_jina(*_a, **_k):
        raise AssertionError("jina must not run when JINA_SCRAPE_FALLBACK is off")

    monkeypatch.setattr(main_mod, "jina_scrape", fail_jina)
    with pytest.raises(RuntimeError, match="browser gave up"):
        asyncio.run(main_mod._scrape_with_transports("https://waf.example/", mode="extract", session=None))


def test_session_scrape_never_uses_jina(monkeypatch):
    """Login cookies must never influence a cookieless cloud fetch."""

    async def fail_browser(url, *, mode, session):
        raise RuntimeError("browser gave up")

    monkeypatch.setattr(main_mod, "http_scrape", _escalate)
    monkeypatch.setattr(main_mod, "browser_scrape", fail_browser)
    monkeypatch.setattr(main_mod, "JINA_SCRAPE_FALLBACK", True)

    def fail_jina(*_a, **_k):
        raise AssertionError("named sessions must skip the Jina fallback")

    monkeypatch.setattr(main_mod, "jina_scrape", fail_jina)
    with pytest.raises(RuntimeError, match="browser gave up"):
        asyncio.run(main_mod._scrape_with_transports("https://waf.example/", mode="extract", session="github"))


def test_jina_failure_reraises_browser_error(monkeypatch):
    async def fail_browser(url, *, mode, session):
        raise RuntimeError("browser gave up")

    async def fail_jina(url, *, mode):
        raise JinaError("Jina API error 502: boom")

    monkeypatch.setattr(main_mod, "http_scrape", _escalate)
    monkeypatch.setattr(main_mod, "browser_scrape", fail_browser)
    monkeypatch.setattr(main_mod, "JINA_SCRAPE_FALLBACK", True)
    monkeypatch.setattr(main_mod, "jina_scrape", fail_jina)
    with pytest.raises(RuntimeError, match="browser gave up"):
        asyncio.run(main_mod._scrape_with_transports("https://waf.example/", mode="extract", session=None))


# ---------------------------------------------------------------------------
#  /search_and_scrape — reuse of Jina's server-side page reads
# ---------------------------------------------------------------------------

def _drive_search_and_scrape(monkeypatch, *, scrape_mode="extract"):
    async def fake_chain(q, **_k):
        return {
            "query": q,
            "number_of_results": 1,
            "results": [{"title": "A", "url": "https://a.example/"}],
            "fallback": "jina",
            "jina_pages": {"https://a.example/": {"title": "A", "markdown": "# jina page"}},
        }

    monkeypatch.setattr(main_mod, "_search_with_fallbacks", fake_chain)
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)

    def fail(*_a, **_k):
        raise AssertionError("local transports must not re-scrape a page Jina already fetched")

    monkeypatch.setattr(main_mod, "http_scrape", fail)
    monkeypatch.setattr(main_mod, "browser_scrape", fail)
    req = SearchAndScrapeRequest(query="q", max_results=1, scrape_mode=scrape_mode)
    return asyncio.run(main_mod.search_and_scrape(req))


def test_search_and_scrape_reuses_jina_pages(monkeypatch):
    resp = _drive_search_and_scrape(monkeypatch)
    content = resp["results"][0]["content"]
    assert content["markdown"] == "# jina page"
    assert content["transport"] == "jina"
    assert resp["results"][0].get("cached") is not True


def test_jina_pages_not_reused_in_fetch_mode(monkeypatch):
    """Fetch mode wants raw html/text — the markdown read does not serve it."""

    async def fake_browser(url, *, mode, session):
        return {"url": url, "title": "A", "text": "browser text", "html": "<b>raw</b>", "status": 200}

    async def fake_chain(q, **_k):
        return {
            "query": q,
            "number_of_results": 1,
            "results": [{"title": "A", "url": "https://a.example/"}],
            "fallback": "jina",
            "jina_pages": {"https://a.example/": {"title": "A", "markdown": "# jina page"}},
        }

    monkeypatch.setattr(main_mod, "_search_with_fallbacks", fake_chain)
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "http_scrape", _escalate)
    monkeypatch.setattr(main_mod, "browser_scrape", fake_browser)
    req = SearchAndScrapeRequest(query="q", max_results=1, scrape_mode="fetch")
    resp = asyncio.run(main_mod.search_and_scrape(req))
    assert resp["results"][0]["content"]["html"] == "<b>raw</b>"


# ---------------------------------------------------------------------------
#  Explicit endpoints
# ---------------------------------------------------------------------------

def test_jina_search_endpoint_disabled(monkeypatch):
    monkeypatch.setattr(jc, "JINA_ENABLED", False)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(main_mod.jina_search_endpoint(q="q", max_results=5))
    assert excinfo.value.status_code == 503


def test_jina_scrape_endpoint_rejects_private_url(monkeypatch):
    # The mixed resolver answers 192.x privately → the SSRF guard must reject
    # the URL before anything is handed to Jina.
    def fail_jina(*_a, **_k):
        raise AssertionError("a blocked URL must never reach the Jina client")

    monkeypatch.setattr(socket, "getaddrinfo", _mixed_getaddrinfo)
    monkeypatch.setattr(main_mod, "jina_scrape", fail_jina)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(main_mod.jina_scrape_endpoint(req=ScrapeRequest(url="http://192.168.1.10/admin", mode="extract")))
    assert excinfo.value.status_code == 403


def test_jina_scrape_endpoint_rejects_sessions(monkeypatch):
    # The URL is validated (DNS-resolved) before the session check — fake it
    # so the test stays network-free.
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(main_mod.jina_scrape_endpoint(
            req=ScrapeRequest(url="https://example.com/", mode="extract", session="github"),
        ))
    assert excinfo.value.status_code == 400


def test_jina_scrape_endpoint_maps_jina_errors(monkeypatch):
    async def fake_scrape(url, *, mode):
        raise JinaError("Jina rate limit exceeded", bridge_status=429, retry_after=9.0)

    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "jina_scrape", fake_scrape)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(main_mod.jina_scrape_endpoint(req=ScrapeRequest(url="https://example.com/", mode="extract")))
    assert excinfo.value.status_code == 429
    assert excinfo.value.headers is not None and excinfo.value.headers.get("Retry-After") == "9"


def test_jina_scrape_endpoint_serves_and_caches(monkeypatch):
    calls: list[str] = []

    async def fake_scrape(url, *, mode):
        calls.append(url)
        return {"url": url, "title": "T", "markdown": "# m", "tables": [], "status": 200}

    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "jina_scrape", fake_scrape)
    req = ScrapeRequest(url="https://example.com/page", mode="extract")
    first = asyncio.run(main_mod.jina_scrape_endpoint(req=req))
    assert first["transport"] == "jina"
    second = asyncio.run(main_mod.jina_scrape_endpoint(req=req))
    assert second["cached"] is True
    assert calls == ["https://example.com/page"]


def test_jina_status_reflects_config(monkeypatch):
    monkeypatch.setattr(jc, "JINA_ENABLED", False)
    assert jc.status() == "off"
    monkeypatch.setattr(jc, "JINA_ENABLED", True)
    monkeypatch.setattr(jc, "JINA_API_KEY", "")
    assert jc.status() == "anonymous"
    monkeypatch.setattr(jc, "JINA_API_KEY", "jina_k")
    assert jc.status() == "ready"
