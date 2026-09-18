"""Unit tests for the optional Exa transports — network-free.

The Exa HTTP seam (`exa_client._request_json`) is monkeypatched, as are the
stage functions inside `bridge.main`. Mirrors test_jina.py. Covers:

- client behavior: gating, normalization, error-envelope decoding
- chain wiring: exa strictly AFTER every self-hosted stage AND the Jina stage
- scrape transport: exa only after the browser AND the Jina reader failed,
  never for sessions
- /search_and_scrape reuse of Exa's server-side page texts
- /exa_search + /exa_scrape endpoints (disabled state, SSRF, mapping)
"""

from __future__ import annotations

import asyncio
import socket

import bridge.exa_client as ec
import bridge.main as main_mod
import pytest
from bridge.exa_client import ExaError
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
def _exa_enabled(monkeypatch):
    """Most tests exercise the enabled path; the disabled-state tests override."""
    monkeypatch.setattr(ec, "EXA_ENABLED", True)
    monkeypatch.setattr(ec, "EXA_API_KEY", "exa_test_key")


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
    """Replace the Exa request seam; handler(method, url, json_body)."""
    calls: list[dict] = []

    async def fake_request(method, url, *, json_body=None):
        calls.append({"method": method, "url": url, "json_body": json_body})
        return handler(method, url, json_body)

    monkeypatch.setattr(ec, "_request_json", fake_request)
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
    monkeypatch.setattr(ec, "EXA_ENABLED", False)
    with pytest.raises(ExaError) as exc:
        asyncio.run(ec.search("q"))
    assert exc.value.bridge_status == 503


def test_search_without_key_returns_503(monkeypatch):
    """api.exa.ai has no anonymous tier — refuse before spending the request."""
    monkeypatch.setattr(ec, "EXA_API_KEY", "")
    with pytest.raises(ExaError) as exc:
        asyncio.run(ec.search("q"))
    assert exc.value.bridge_status == 503
    assert "EXA_API_KEY" in str(exc.value)


def test_search_normalizes_to_searxng_shape(monkeypatch):
    payload = {
        "results": [
            {"title": "T1", "url": "https://a.example/", "text": "# full page"},
            {"title": "T2", "url": "https://b.example/", "text": "body text"},
            {"url": ""},  # dropped — no url
        ],
    }
    calls = _serve_json(monkeypatch, lambda *a: (200, payload))
    out = asyncio.run(ec.search("q", max_results=3))
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == f"{ec.EXA_BASE_URL}/search"
    assert calls[0]["json_body"]["query"] == "q"
    assert calls[0]["json_body"]["numResults"] == 3
    assert calls[0]["json_body"]["contents"] == {"text": {"maxCharacters": ec.MAX_CHARACTERS}}
    assert out["results"] == [
        {"title": "T1", "url": "https://a.example/", "content": "# full page", "engine": "exa"},
        {"title": "T2", "url": "https://b.example/", "content": "body text", "engine": "exa"},
    ]
    assert out["engine"] == "exa"
    assert out["exa_pages"]["https://a.example/"] == {"title": "T1", "markdown": "# full page"}


def test_search_numresults_clamped_to_limit(monkeypatch):
    calls = _serve_json(monkeypatch, lambda *a: (200, {"results": []}))
    asyncio.run(ec.search("q", max_results=50))
    assert calls[0]["json_body"]["numResults"] == ec.MAX_SEARCH_RESULTS


def test_search_filters_passed_through(monkeypatch):
    calls = _serve_json(monkeypatch, lambda *a: (200, {"results": []}))
    asyncio.run(ec.search("q", category="github", include_domains=["github.com"]))
    assert calls[0]["json_body"]["category"] == "github"
    assert calls[0]["json_body"]["includeDomains"] == ["github.com"]


def test_search_401_maps_to_auth_error(monkeypatch):
    _serve_json(monkeypatch, lambda *a: (401, {"requestId": "r1", "message": "Invalid API key"}))
    with pytest.raises(ExaError) as exc:
        asyncio.run(ec.search("q"))
    assert "EXA_API_KEY" in str(exc.value)


def test_search_429_maps_to_rate_limit_error(monkeypatch):
    _serve_json(monkeypatch, lambda *a: (429, {"requestId": "r1", "message": "Rate limit exceeded"}))
    with pytest.raises(ExaError) as exc:
        asyncio.run(ec.search("q"))
    assert exc.value.bridge_status == 429


def test_search_402_maps_to_quota_error(monkeypatch):
    _serve_json(monkeypatch, lambda *a: (402, {"requestId": "r1", "message": "Payment required"}))
    with pytest.raises(ExaError) as exc:
        asyncio.run(ec.search("q"))
    assert "quota/billing" in str(exc.value)


def test_search_structured_error_envelope_decoded(monkeypatch):
    """Current envelope shape: {"error": {"type", "code", "message"}, "requestId"}."""
    payload = {"error": {"type": "INVALID_REQUEST", "code": "BAD_QUERY", "message": "bad query"}, "requestId": "r1"}
    _serve_json(monkeypatch, lambda *a: (400, payload))
    with pytest.raises(ExaError) as exc:
        asyncio.run(ec.search("q"))
    assert "bad query" in str(exc.value)
    assert exc.value.bridge_status == 502


# ---------------------------------------------------------------------------
#  Client — scrape (POST /contents)
# ---------------------------------------------------------------------------

def test_scrape_disabled_returns_503(monkeypatch):
    monkeypatch.setattr(ec, "EXA_ENABLED", False)
    with pytest.raises(ExaError) as exc:
        asyncio.run(ec.scrape("https://example.com/"))
    assert exc.value.bridge_status == 503


def test_scrape_extract_mode_returns_markdown(monkeypatch):
    payload = {"results": [{"id": "https://example.com/", "title": "Page", "url": "https://example.com/", "text": "# hello"}]}
    calls = _serve_json(monkeypatch, lambda *a: (200, payload))
    out = asyncio.run(ec.scrape("https://example.com/", mode="extract"))
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == f"{ec.EXA_BASE_URL}/contents"
    assert calls[0]["json_body"] == {"urls": ["https://example.com/"], "text": {"maxCharacters": ec.MAX_CHARACTERS}}
    assert out == {
        "url": "https://example.com/",
        "title": "Page",
        "markdown": "# hello",
        "tables": [],
        "status": 200,
    }


def test_scrape_fetch_mode_returns_cleaned_text_without_html(monkeypatch):
    """Exa never returns raw HTML — fetch mode degrades to the cleaned text."""
    payload = {"results": [{"title": "Page", "url": "https://example.com/", "text": "cleaned"}]}
    _serve_json(monkeypatch, lambda *a: (200, payload))
    out = asyncio.run(ec.scrape("https://example.com/", mode="fetch"))
    assert out["text"] == "cleaned"
    assert out["html"] == ""


def test_scrape_no_results_surfaces_status_error_tag(monkeypatch):
    payload = {
        "results": [],
        "statuses": [{"id": "https://example.com/", "status": "error", "error": {"tag": "UNSUPPORTED_CONTENT"}}],
    }
    _serve_json(monkeypatch, lambda *a: (200, payload))
    with pytest.raises(ExaError) as exc:
        asyncio.run(ec.scrape("https://example.com/"))
    assert "UNSUPPORTED_CONTENT" in str(exc.value)
    assert exc.value.bridge_status == 502


# ---------------------------------------------------------------------------
#  Chain wiring — exa strictly after every self-hosted stage AND jina
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
        return {"query": query, "number_of_results": 0, "results": [], "engine": "jina"}

    async def fake_exa(query, *, max_results=5):
        order.append("exa")
        return {
            "query": query,
            "number_of_results": 1,
            "results": [{"title": "E", "url": "https://e.example/", "content": "snippet", "engine": "exa"}],
            "engine": "exa",
            "exa_pages": {"https://e.example/": {"title": "E", "markdown": "# e"}},
        }

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "browser_web_search", fake_browser)
    monkeypatch.setattr(main_mod, "jina_search", fake_jina)
    monkeypatch.setattr(main_mod, "exa_search", fake_exa)


def test_exa_is_the_final_stage(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(main_mod, "JINA_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "EXA_SEARCH_FALLBACK", True)
    _empty_all_stages(monkeypatch, order)
    resp = _run_search()
    assert order == ["searxng:q", "searxng:!bing q", "browser", "jina", "exa"]
    assert resp["fallback"] == "exa"
    assert resp["results"][0]["engine"] == "exa"
    # The full page reads ride along for /search_and_scrape to reuse.
    assert resp["exa_pages"]["https://e.example/"]["markdown"] == "# e"


def test_exa_stage_absent_when_disabled(monkeypatch):
    """With the Exa stage off, an all-stages-empty run behaves exactly as
    before Exa existed — the (empty) primary SearXNG response is returned."""
    order: list[str] = []
    monkeypatch.setattr(main_mod, "JINA_SEARCH_FALLBACK", False)
    monkeypatch.setattr(main_mod, "EXA_SEARCH_FALLBACK", False)
    _empty_all_stages(monkeypatch, order)
    resp = _run_search()
    assert "exa" not in order
    assert resp["results"] == []


def test_exa_only_runs_after_jina_returned_nothing(monkeypatch):
    order: list[str] = []

    async def fake_jina(query, *, max_results=5):
        order.append("jina")
        return {"query": query, "number_of_results": 0, "results": [], "engine": "jina"}

    async def fake_exa(query, *, max_results=5):
        order.append("exa")
        return {
            "query": query,
            "number_of_results": 1,
            "results": [{"title": "E", "url": "https://e.example/", "content": "s", "engine": "exa"}],
            "engine": "exa",
        }

    monkeypatch.setattr(main_mod, "JINA_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "EXA_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BING", False)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BROWSER", False)

    async def fake_searxng(query, **_k):
        order.append("searxng")
        return _searxng_response([])

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "jina_search", fake_jina)
    monkeypatch.setattr(main_mod, "exa_search", fake_exa)
    resp = _run_search()
    assert order == ["searxng", "jina", "exa"]
    assert resp["fallback"] == "exa"


def test_exa_stage_error_swallowed_when_primary_alive(monkeypatch):
    """An Exa failure must degrade like any other stage error — the (empty)
    primary response is returned, the exception only logged."""

    async def fake_searxng(query, **_k):
        return _searxng_response([])

    async def fake_exa(query, *, max_results=5):
        raise ExaError("Exa rate limit exceeded", bridge_status=429)

    monkeypatch.setattr(main_mod, "JINA_SEARCH_FALLBACK", False)
    monkeypatch.setattr(main_mod, "EXA_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BING", False)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BROWSER", False)
    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "exa_search", fake_exa)
    resp = _run_search()
    assert resp["results"] == []


def test_exa_error_with_searxng_down_raises_502(monkeypatch):
    """SearXNG unreachable AND Exa failing: nothing served → 502."""

    async def fake_searxng(query, **_k):
        raise RuntimeError("connection refused")

    async def fake_exa(query, *, max_results=5):
        raise ExaError("Exa API error 500: boom")

    monkeypatch.setattr(main_mod, "JINA_SEARCH_FALLBACK", False)
    monkeypatch.setattr(main_mod, "EXA_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BING", False)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BROWSER", False)
    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "exa_search", fake_exa)
    with pytest.raises(HTTPException) as excinfo:
        _run_search()
    assert excinfo.value.status_code == 502


def test_exa_never_runs_when_self_hosted_stages_serve(monkeypatch):
    async def fake_searxng(query, **_k):
        return _searxng_response([_hit()])

    def fail(*_a, **_k):
        raise AssertionError("exa must not run when earlier stages serve results")

    monkeypatch.setattr(main_mod, "JINA_SEARCH_FALLBACK", False)
    monkeypatch.setattr(main_mod, "EXA_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "exa_search", fail)
    resp = _run_search()
    assert "fallback" not in resp
    assert resp["results"][0]["engine"] == "x"


@pytest.mark.parametrize("kwargs", [{"pageno": 2}, {"categories": "images"}])
def test_exa_respects_fallback_gating(monkeypatch, kwargs):
    monkeypatch.setattr(main_mod, "JINA_SEARCH_FALLBACK", False)
    monkeypatch.setattr(main_mod, "EXA_SEARCH_FALLBACK", True)

    async def fake_searxng(query, **_k):
        return _searxng_response([])

    def fail(*_a, **_k):
        raise AssertionError("exa must respect the page-1 general-web gate")

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "exa_search", fail)
    resp = _run_search(**kwargs)
    assert resp["results"] == []


def test_search_endpoint_strips_exa_pages(monkeypatch):
    """The /search response must never carry full page markdown per result."""
    order: list[str] = []
    monkeypatch.setattr(main_mod, "JINA_SEARCH_FALLBACK", False)
    monkeypatch.setattr(main_mod, "EXA_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BING", False)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BROWSER", False)

    async def fake_searxng(query, **_k):
        order.append("searxng")
        return _searxng_response([])

    async def fake_exa(query, *, max_results=5):
        return {
            "query": query,
            "number_of_results": 1,
            "results": [_hit("Exa hit")],
            "engine": "exa",
            "exa_pages": {"https://example.com/": {"title": "T", "markdown": "# big"}},
        }

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "exa_search", fake_exa)
    resp = asyncio.run(main_mod.search(q="q", categories=None, language="en", pageno=1, time_range=None, safesearch=0, max_results=10))
    assert "exa_pages" not in resp
    assert resp["fallback"] == "exa"


# ---------------------------------------------------------------------------
#  Scrape transports — exa only after the browser AND jina failed
# ---------------------------------------------------------------------------

def _escalate(url, *, mode):
    from bridge.http_client import Escalation

    raise Escalation("disabled in unit tests")


def test_browser_and_jina_failures_fall_back_to_exa(monkeypatch):
    async def fail_browser(url, *, mode, session):
        raise RuntimeError("browser gave up (WAF)")

    async def fail_jina(url, *, mode):
        raise ExaError("Jina API error 502: down")

    async def fake_exa_scrape(url, *, mode):
        return {"url": url, "title": "Via Exa", "markdown": "# exa", "tables": [], "status": 200}

    monkeypatch.setattr(main_mod, "http_scrape", _escalate)
    monkeypatch.setattr(main_mod, "browser_scrape", fail_browser)
    monkeypatch.setattr(main_mod, "JINA_SCRAPE_FALLBACK", True)
    monkeypatch.setattr(main_mod, "jina_scrape", fail_jina)
    monkeypatch.setattr(main_mod, "EXA_SCRAPE_FALLBACK", True)
    monkeypatch.setattr(main_mod, "exa_scrape", fake_exa_scrape)
    out = asyncio.run(main_mod._scrape_with_transports("https://waf.example/", mode="extract", session=None))
    assert out["transport"] == "exa"
    assert out["markdown"] == "# exa"


def test_exa_serves_when_jina_fallback_disabled(monkeypatch):
    async def fail_browser(url, *, mode, session):
        raise RuntimeError("browser gave up")

    async def fake_exa_scrape(url, *, mode):
        return {"url": url, "title": "Via Exa", "markdown": "# exa", "tables": [], "status": 200}

    monkeypatch.setattr(main_mod, "http_scrape", _escalate)
    monkeypatch.setattr(main_mod, "browser_scrape", fail_browser)
    monkeypatch.setattr(main_mod, "JINA_SCRAPE_FALLBACK", False)
    monkeypatch.setattr(main_mod, "EXA_SCRAPE_FALLBACK", True)
    monkeypatch.setattr(main_mod, "exa_scrape", fake_exa_scrape)
    out = asyncio.run(main_mod._scrape_with_transports("https://waf.example/", mode="extract", session=None))
    assert out["transport"] == "exa"


def test_browser_failure_with_exa_disabled_reraises(monkeypatch):
    async def fail_browser(url, *, mode, session):
        raise RuntimeError("browser gave up")

    monkeypatch.setattr(main_mod, "http_scrape", _escalate)
    monkeypatch.setattr(main_mod, "browser_scrape", fail_browser)
    monkeypatch.setattr(main_mod, "JINA_SCRAPE_FALLBACK", False)
    monkeypatch.setattr(main_mod, "EXA_SCRAPE_FALLBACK", False)

    def fail_exa(*_a, **_k):
        raise AssertionError("exa must not run when EXA_SCRAPE_FALLBACK is off")

    monkeypatch.setattr(main_mod, "exa_scrape", fail_exa)
    with pytest.raises(RuntimeError, match="browser gave up"):
        asyncio.run(main_mod._scrape_with_transports("https://waf.example/", mode="extract", session=None))


def test_session_scrape_never_uses_exa(monkeypatch):
    """Login cookies must never influence a cookieless cloud fetch."""

    async def fail_browser(url, *, mode, session):
        raise RuntimeError("browser gave up")

    monkeypatch.setattr(main_mod, "http_scrape", _escalate)
    monkeypatch.setattr(main_mod, "browser_scrape", fail_browser)
    monkeypatch.setattr(main_mod, "JINA_SCRAPE_FALLBACK", False)
    monkeypatch.setattr(main_mod, "EXA_SCRAPE_FALLBACK", True)

    def fail_exa(*_a, **_k):
        raise AssertionError("named sessions must skip the Exa fallback")

    monkeypatch.setattr(main_mod, "exa_scrape", fail_exa)
    with pytest.raises(RuntimeError, match="browser gave up"):
        asyncio.run(main_mod._scrape_with_transports("https://waf.example/", mode="extract", session="github"))


def test_exa_failure_reraises_browser_error(monkeypatch):
    async def fail_browser(url, *, mode, session):
        raise RuntimeError("browser gave up")

    async def fail_exa(url, *, mode):
        raise ExaError("Exa API error 502: boom")

    monkeypatch.setattr(main_mod, "http_scrape", _escalate)
    monkeypatch.setattr(main_mod, "browser_scrape", fail_browser)
    monkeypatch.setattr(main_mod, "JINA_SCRAPE_FALLBACK", False)
    monkeypatch.setattr(main_mod, "EXA_SCRAPE_FALLBACK", True)
    monkeypatch.setattr(main_mod, "exa_scrape", fail_exa)
    with pytest.raises(RuntimeError, match="browser gave up"):
        asyncio.run(main_mod._scrape_with_transports("https://waf.example/", mode="extract", session=None))


# ---------------------------------------------------------------------------
#  /search_and_scrape — reuse of Exa's server-side page texts
# ---------------------------------------------------------------------------

def test_search_and_scrape_reuses_exa_pages(monkeypatch):
    async def fake_chain(q, **_k):
        return {
            "query": q,
            "number_of_results": 1,
            "results": [{"title": "A", "url": "https://a.example/"}],
            "fallback": "exa",
            "exa_pages": {"https://a.example/": {"title": "A", "markdown": "# exa page"}},
        }

    monkeypatch.setattr(main_mod, "_search_with_fallbacks", fake_chain)
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)

    def fail(*_a, **_k):
        raise AssertionError("local transports must not re-scrape a page Exa already fetched")

    monkeypatch.setattr(main_mod, "http_scrape", fail)
    monkeypatch.setattr(main_mod, "browser_scrape", fail)
    req = SearchAndScrapeRequest(query="q", max_results=1, scrape_mode="extract")
    resp = asyncio.run(main_mod.search_and_scrape(req))
    content = resp["results"][0]["content"]
    assert content["markdown"] == "# exa page"
    assert content["transport"] == "exa"
    assert resp["results"][0].get("cached") is not True


def test_jina_pages_take_precedence_over_exa_pages(monkeypatch):
    """When both fallbacks somehow contributed reads, the earlier stage wins."""

    async def fake_chain(q, **_k):
        return {
            "query": q,
            "number_of_results": 1,
            "results": [{"title": "A", "url": "https://a.example/"}],
            "fallback": "jina",
            "jina_pages": {"https://a.example/": {"title": "A", "markdown": "# jina page"}},
            "exa_pages": {"https://a.example/": {"title": "A", "markdown": "# exa page"}},
        }

    monkeypatch.setattr(main_mod, "_search_with_fallbacks", fake_chain)
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "http_scrape", _escalate)
    monkeypatch.setattr(main_mod, "browser_scrape", _escalate)
    req = SearchAndScrapeRequest(query="q", max_results=1, scrape_mode="extract")
    resp = asyncio.run(main_mod.search_and_scrape(req))
    assert resp["results"][0]["content"]["transport"] == "jina"
    assert resp["results"][0]["content"]["markdown"] == "# jina page"


def test_exa_pages_not_reused_in_fetch_mode(monkeypatch):
    """Fetch mode wants raw html/text — the cleaned text read does not serve it."""

    async def fake_browser(url, *, mode, session):
        return {"url": url, "title": "A", "text": "browser text", "html": "<b>raw</b>", "status": 200}

    async def fake_chain(q, **_k):
        return {
            "query": q,
            "number_of_results": 1,
            "results": [{"title": "A", "url": "https://a.example/"}],
            "fallback": "exa",
            "exa_pages": {"https://a.example/": {"title": "A", "markdown": "# exa page"}},
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

def test_exa_search_endpoint_disabled(monkeypatch):
    monkeypatch.setattr(ec, "EXA_ENABLED", False)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(main_mod.exa_search_endpoint(q="q", max_results=5))
    assert excinfo.value.status_code == 503


def test_exa_search_endpoint_without_key(monkeypatch):
    monkeypatch.setattr(ec, "EXA_API_KEY", "")
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(main_mod.exa_search_endpoint(q="q", max_results=5))
    assert excinfo.value.status_code == 503
    assert "EXA_API_KEY" in excinfo.value.detail


def test_exa_scrape_endpoint_rejects_private_url(monkeypatch):
    # The mixed resolver answers 192.x privately → the SSRF guard must reject
    # the URL before anything is handed to Exa.
    def fail_exa(*_a, **_k):
        raise AssertionError("a blocked URL must never reach the Exa client")

    monkeypatch.setattr(socket, "getaddrinfo", _mixed_getaddrinfo)
    monkeypatch.setattr(main_mod, "exa_scrape", fail_exa)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(main_mod.exa_scrape_endpoint(req=ScrapeRequest(url="http://192.168.1.10/admin", mode="extract")))
    assert excinfo.value.status_code == 403


def test_exa_scrape_endpoint_rejects_sessions(monkeypatch):
    # The URL is validated (DNS-resolved) before the session check — fake it
    # so the test stays network-free.
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(main_mod.exa_scrape_endpoint(
            req=ScrapeRequest(url="https://example.com/", mode="extract", session="github"),
        ))
    assert excinfo.value.status_code == 400


def test_exa_scrape_endpoint_maps_exa_errors(monkeypatch):
    async def fake_scrape(url, *, mode):
        raise ExaError("Exa rate limit exceeded", bridge_status=429, retry_after=9.0)

    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "exa_scrape", fake_scrape)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(main_mod.exa_scrape_endpoint(req=ScrapeRequest(url="https://example.com/", mode="extract")))
    assert excinfo.value.status_code == 429
    assert excinfo.value.headers is not None and excinfo.value.headers.get("Retry-After") == "9"


def test_exa_scrape_endpoint_serves_and_caches(monkeypatch):
    calls: list[str] = []

    async def fake_scrape(url, *, mode):
        calls.append(url)
        return {"url": url, "title": "T", "markdown": "# m", "tables": [], "status": 200}

    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    monkeypatch.setattr(main_mod, "exa_scrape", fake_scrape)
    req = ScrapeRequest(url="https://example.com/page", mode="extract")
    first = asyncio.run(main_mod.exa_scrape_endpoint(req=req))
    assert first["transport"] == "exa"
    second = asyncio.run(main_mod.exa_scrape_endpoint(req=req))
    assert second["cached"] is True
    assert calls == ["https://example.com/page"]


def test_exa_status_reflects_config(monkeypatch):
    monkeypatch.setattr(ec, "EXA_ENABLED", False)
    assert ec.status() == "off"
    monkeypatch.setattr(ec, "EXA_ENABLED", True)
    monkeypatch.setattr(ec, "EXA_API_KEY", "")
    assert ec.status() == "unconfigured"
    monkeypatch.setattr(ec, "EXA_API_KEY", "exa_k")
    assert ec.status() == "ready"
