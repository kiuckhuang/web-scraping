"""Unit tests for the optional TinyFish free-tier transports — network-free.

The TinyFish HTTP seam (`tinyfish_client._request_json`) is monkeypatched, as
are the stage functions inside `bridge.main`. Mirrors test_ceramic.py (search)
and the scrape half of test_jina.py. Covers:

- client behavior: gating, normalization, slicing, request shape, error-envelope
  decoding (X-API-Key auth, header-carried Retry-After)
- chain wiring: tinyfish staged BEFORE the paid Jina/Exa/Ceramic fallbacks
- scrape chain: tinyfish fetch consulted before the Jina reader
- /tinyfish_search and /tinyfish_scrape endpoints
"""

from __future__ import annotations

import asyncio

import bridge.main as main_mod
import bridge.tinyfish_client as tc
import pytest
from bridge.tinyfish_client import TinyFishError
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def _pin_chain(monkeypatch):
    """Pin the classic chain so tests drive stages explicitly. The deployed
    image's .env may enable the other fallbacks — tests that want them pin
    them per-test (deployment-config rule, AGENTS.md)."""
    monkeypatch.setattr(main_mod, "SEARCH_PRIMARY", "searxng")
    monkeypatch.setattr(main_mod, "SEARXNG_ENABLED", True)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BING", True)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BROWSER", True)
    monkeypatch.setattr(main_mod, "TINYFISH_SEARCH_FALLBACK", False)
    monkeypatch.setattr(main_mod, "TINYFISH_SCRAPE_FALLBACK", False)
    monkeypatch.setattr(main_mod, "JINA_SEARCH_FALLBACK", False)
    monkeypatch.setattr(main_mod, "JINA_SCRAPE_FALLBACK", False)
    monkeypatch.setattr(main_mod, "EXA_SEARCH_FALLBACK", False)
    monkeypatch.setattr(main_mod, "EXA_SCRAPE_FALLBACK", False)


@pytest.fixture(autouse=True)
def _tinyfish_enabled(monkeypatch):
    """Most tests exercise the enabled path; the disabled-state tests override."""
    monkeypatch.setattr(tc, "TINYFISH_ENABLED", True)
    monkeypatch.setattr(tc, "TINYFISH_API_KEY", "tf_test_key")


def _serve_json(monkeypatch, handler):
    """Replace the TinyFish request seam; handler(method, url, json_body)."""
    calls: list[dict] = []

    async def fake_request(method, url, *, json_body=None):
        calls.append({"method": method, "url": url, "json_body": json_body})
        return handler(method, url, json_body)

    monkeypatch.setattr(tc, "_request_json", fake_request)
    return calls


def _hit(title: str = "Example") -> dict:
    return {"title": title, "url": "https://example.com/", "content": "snippet", "engine": "x"}


def _searxng_response(results: list[dict]) -> dict:
    return {"query": "q", "number_of_results": len(results), "results": results, "unresponsive_engines": []}


def _tinyfish_search_payload(n: int = 1) -> dict:
    return {
        "query": "q",
        "results": [
            {"position": i + 1, "site_name": "example.com", "title": f"T{i}", "snippet": "s" * 500,
             "url": f"https://{i}.example/"}
            for i in range(n)
        ],
        "total_results": n,
        "page": 0,
    }


def _run_search(**kwargs) -> dict:
    defaults = {"categories": None, "language": "en", "pageno": 1, "time_range": None, "safesearch": 0, "max_results": 10}
    defaults.update(kwargs)
    return asyncio.run(main_mod._search_with_fallbacks("q", **defaults))


# ---------------------------------------------------------------------------
#  Client — search
# ---------------------------------------------------------------------------

def test_search_disabled_returns_503(monkeypatch):
    monkeypatch.setattr(tc, "TINYFISH_ENABLED", False)
    with pytest.raises(TinyFishError) as exc:
        asyncio.run(tc.search("q"))
    assert exc.value.bridge_status == 503


def test_search_without_key_returns_503(monkeypatch):
    """The TinyFish REST API has no anonymous tier — refuse before spending
    the daily free allowance."""
    monkeypatch.setattr(tc, "TINYFISH_API_KEY", "")
    with pytest.raises(TinyFishError) as exc:
        asyncio.run(tc.search("q"))
    assert exc.value.bridge_status == 503
    assert "TINYFISH_API_KEY" in str(exc.value)


def test_search_normalizes_to_searxng_shape(monkeypatch):
    """GET with the URL-encoded query; snippets map to content; results are
    sliced client-side (the API has no result-count parameter)."""
    payload = _tinyfish_search_payload(3)
    calls = _serve_json(monkeypatch, lambda *a: (200, payload, {}))
    out = asyncio.run(tc.search("california rental laws", max_results=2))
    assert calls[0]["method"] == "GET"
    assert calls[0]["url"] == f"{tc.TINYFISH_SEARCH_URL}?query=california%20rental%20laws"
    assert out["results"][0] == {"title": "T0", "url": "https://0.example/", "content": "s" * 400, "engine": "tinyfish"}
    assert len(out["results"]) == 2  # sliced to max_results even though 3 came back
    assert out["number_of_results"] == 2
    assert out["engine"] == "tinyfish"
    assert "tinyfish_pages" not in out  # snippets only: no page channel


def test_search_drops_urlless_hits(monkeypatch):
    payload = {"results": [
        {"title": "T0", "snippet": "s", "url": "https://a.example/"},
        {"title": "no url", "snippet": "s", "url": ""},
    ]}
    _serve_json(monkeypatch, lambda *a: (200, payload, {}))
    out = asyncio.run(tc.search("q"))
    assert [r["url"] for r in out["results"]] == ["https://a.example/"]


def test_search_401_maps_to_auth_error(monkeypatch):
    _serve_json(monkeypatch, lambda *a: (401, {"error": {"code": "INVALID_API_KEY", "message": "bad key"}}, {}))
    with pytest.raises(TinyFishError) as exc:
        asyncio.run(tc.search("q"))
    assert "TINYFISH_API_KEY" in str(exc.value)


def test_search_429_surfaces_retry_after_header(monkeypatch):
    """Unlike Jina (body field) and Ceramic (body field), a TinyFish 429
    carries Retry-After as an HTTP header."""
    _serve_json(monkeypatch, lambda *a: (429, {"error": {"code": "RATE_LIMITED", "message": "slow down"}}, {"retry-after": "7"}))
    with pytest.raises(TinyFishError) as exc:
        asyncio.run(tc.search("q"))
    assert exc.value.bridge_status == 429
    assert exc.value.retry_after == 7.0


def test_search_402_maps_to_wallet_error(monkeypatch):
    _serve_json(monkeypatch, lambda *a: (402, {"error": {"code": "INSUFFICIENT_CREDITS", "message": "wallet too low"}}, {}))
    with pytest.raises(TinyFishError) as exc:
        asyncio.run(tc.search("q"))
    assert "wallet/billing" in str(exc.value)


def test_search_error_envelope_decoded(monkeypatch):
    payload = {"error": {"code": "INVALID_INPUT", "message": 'Field "query": Invalid input'}}
    _serve_json(monkeypatch, lambda *a: (400, payload, {}))
    with pytest.raises(TinyFishError) as exc:
        asyncio.run(tc.search("q"))
    assert "Invalid input" in str(exc.value)
    assert exc.value.bridge_status == 502


def test_status_values():
    assert tc.status() in ("off", "unconfigured", "ready")


# ---------------------------------------------------------------------------
#  Client — fetch (scrape)
# ---------------------------------------------------------------------------

def _fetch_payload(text: str, title: str = "Page") -> dict:
    return {"results": [{"url": "https://example.com/", "final_url": "https://example.com/", "title": title,
                         "description": "d", "language": "en", "format": "markdown", "text": text}],
            "errors": []}


def test_scrape_extract_returns_markdown(monkeypatch):
    calls = _serve_json(monkeypatch, lambda *a: (200, _fetch_payload("# Hello\n\nWorld"), {}))
    out = asyncio.run(tc.scrape("https://example.com/", mode="extract"))
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == tc.TINYFISH_FETCH_URL
    assert calls[0]["json_body"] == {"urls": ["https://example.com/"], "format": "markdown"}
    assert out["markdown"].startswith("# Hello")
    assert out["status"] == 200
    assert out["title"] == "Page"


def test_scrape_fetch_returns_html_with_derived_text(monkeypatch):
    calls = _serve_json(monkeypatch, lambda *a: (200, _fetch_payload("<h1>Hi</h1><p>Body</p>"), {}))
    out = asyncio.run(tc.scrape("https://example.com/", mode="fetch"))
    assert calls[0]["json_body"]["format"] == "html"
    assert out["html"] == "<h1>Hi</h1><p>Body</p>"
    assert "Hi" in out["text"] and "Body" in out["text"]


def test_scrape_per_url_errors_raise(monkeypatch):
    """Per-URL failures arrive as errors[] beside a 200 — never silently empty."""
    payload = {"results": [], "errors": [{"url": "https://example.com/", "error": "timeout"}]}
    _serve_json(monkeypatch, lambda *a: (200, payload, {}))
    with pytest.raises(TinyFishError) as exc:
        asyncio.run(tc.scrape("https://example.com/"))
    assert "timeout" in str(exc.value)


def test_scrape_disabled_returns_503(monkeypatch):
    monkeypatch.setattr(tc, "TINYFISH_ENABLED", False)
    with pytest.raises(TinyFishError) as exc:
        asyncio.run(tc.scrape("https://example.com/"))
    assert exc.value.bridge_status == 503


# ---------------------------------------------------------------------------
#  Chain wiring — tinyfish staged BEFORE the paid fallbacks
# ---------------------------------------------------------------------------

def _empty_all_stages(monkeypatch, order: list[str]):
    async def fake_searxng(query, **_k):
        order.append("searxng")
        return _searxng_response([])

    async def fake_browser(query, count=10, engines=None):
        order.append("browser")
        return {"engine": "duckduckgo", "results": []}

    async def fake_tinyfish(query, *, max_results=5):
        order.append("tinyfish")
        return {"query": query, "number_of_results": 0, "results": [], "engine": "tinyfish"}

    async def fake_jina(query, *, max_results=5):
        order.append("jina")
        return {"query": query, "number_of_results": 0, "results": [], "engine": "jina"}

    async def fake_exa(query, *, max_results=5):
        order.append("exa")
        return {"query": query, "number_of_results": 0, "results": [], "engine": "exa"}

    async def fake_ceramic(query, *, max_results=5):
        order.append("ceramic")
        return {
            "query": query,
            "number_of_results": 1,
            "results": [{"title": "C", "url": "https://c.example/", "content": "snippet", "engine": "ceramic"}],
            "engine": "ceramic",
        }

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "browser_web_search", fake_browser)
    monkeypatch.setattr(main_mod, "tinyfish_search", fake_tinyfish)
    monkeypatch.setattr(main_mod, "jina_search", fake_jina)
    monkeypatch.setattr(main_mod, "exa_search", fake_exa)
    monkeypatch.setattr(main_mod, "ceramic_search", fake_ceramic)


def test_tinyfish_stages_before_the_paid_fallbacks(monkeypatch):
    """With every stage empty up to Ceramic, the chain visits tinyfish between
    the self-hosted stages and the paid Jina/Exa/Ceramic fallbacks."""
    order: list[str] = []
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BING", False)
    monkeypatch.setattr(main_mod, "TINYFISH_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "JINA_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "EXA_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "CERAMIC_SEARCH_FALLBACK", True)
    _empty_all_stages(monkeypatch, order)
    resp = _run_search()
    assert order == ["searxng", "browser", "tinyfish", "jina", "exa", "ceramic"]
    assert resp["fallback"] == "ceramic"
    assert resp["results"][0]["engine"] == "ceramic"


def test_tinyfish_serves_before_the_paid_stages_run(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BING", False)
    monkeypatch.setattr(main_mod, "TINYFISH_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "JINA_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "EXA_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "CERAMIC_SEARCH_FALLBACK", True)
    _empty_all_stages(monkeypatch, order)

    async def fake_tinyfish(query, *, max_results=5):
        order.append("tinyfish")
        return {
            "query": query,
            "number_of_results": 1,
            "results": [{"title": "TF", "url": "https://tf.example/", "content": "snippet", "engine": "tinyfish"}],
            "engine": "tinyfish",
        }

    def fail(*_a, **_k):
        raise AssertionError("paid fallbacks must not run when the free TinyFish stage serves results")

    monkeypatch.setattr(main_mod, "tinyfish_search", fake_tinyfish)
    monkeypatch.setattr(main_mod, "jina_search", fail)
    monkeypatch.setattr(main_mod, "exa_search", fail)
    monkeypatch.setattr(main_mod, "ceramic_search", fail)
    resp = _run_search()
    assert order == ["searxng", "browser", "tinyfish"]
    assert resp["fallback"] == "tinyfish"
    assert resp["results"][0]["engine"] == "tinyfish"


def test_tinyfish_stage_absent_when_disabled(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(main_mod, "TINYFISH_SEARCH_FALLBACK", False)
    monkeypatch.setattr(main_mod, "JINA_SEARCH_FALLBACK", True)
    _empty_all_stages(monkeypatch, order)
    _run_search()
    assert "tinyfish" not in order


def test_tinyfish_never_runs_when_earlier_stages_serve(monkeypatch):
    async def fake_searxng(query, **_k):
        return _searxng_response([_hit()])

    def fail(*_a, **_k):
        raise AssertionError("tinyfish must not run when earlier stages serve results")

    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BING", False)
    monkeypatch.setattr(main_mod, "TINYFISH_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "JINA_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "tinyfish_search", fail)
    monkeypatch.setattr(main_mod, "jina_search", fail)
    resp = _run_search()
    assert "fallback" not in resp


@pytest.mark.parametrize("kwargs", [{"pageno": 2}, {"categories": "images"}, {"time_range": "week"}, {"safesearch": 1}, {"language": "de"}])
def test_tinyfish_respects_fallback_gating(monkeypatch, kwargs):
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BING", False)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BROWSER", False)
    monkeypatch.setattr(main_mod, "TINYFISH_SEARCH_FALLBACK", True)

    async def fake_searxng(query, **_k):
        return _searxng_response([])

    def fail(*_a, **_k):
        raise AssertionError("tinyfish must respect the page-1 general-web and filter gates")

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "tinyfish_search", fail)
    resp = _run_search(**kwargs)
    assert resp["results"] == []


def test_tinyfish_stage_error_swallowed_when_primary_alive(monkeypatch):
    async def fake_searxng(query, **_k):
        return _searxng_response([])

    async def fake_tinyfish(query, *, max_results=5):
        raise TinyFishError("TinyFish rate limit exceeded", bridge_status=429)

    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BING", False)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BROWSER", False)
    monkeypatch.setattr(main_mod, "TINYFISH_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "tinyfish_search", fake_tinyfish)
    resp = _run_search()
    assert resp["results"] == []


# ---------------------------------------------------------------------------
#  Scrape chain — tinyfish fetch consulted before the Jina reader
# ---------------------------------------------------------------------------

def test_scrape_chain_tinyfish_before_jina(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(main_mod, "HTTP_FASTPATH_ENABLED", False)
    monkeypatch.setattr(main_mod, "TINYFISH_SCRAPE_FALLBACK", True)
    monkeypatch.setattr(main_mod, "JINA_SCRAPE_FALLBACK", True)
    monkeypatch.setattr(main_mod, "EXA_SCRAPE_FALLBACK", True)

    async def fail_browser(url, *, mode, session):
        order.append("browser")
        raise RuntimeError("WAF hard block")

    async def fake_tinyfish(url, *, mode):
        order.append("tinyfish")
        return {"url": url, "title": "TF", "markdown": "# served", "tables": [], "status": 200}

    def fail_jina(*_a, **_k):
        raise AssertionError("jina must not run when tinyfish serves the page")

    monkeypatch.setattr(main_mod, "browser_scrape", fail_browser)
    monkeypatch.setattr(main_mod, "tinyfish_scrape", fake_tinyfish)
    monkeypatch.setattr(main_mod, "jina_scrape", fail_jina)
    out = asyncio.run(main_mod._run_scrape_transports("https://example.com/", mode="extract", session=None))
    assert order == ["browser", "tinyfish"]
    assert out["transport"] == "tinyfish"
    assert out["markdown"].startswith("# served")


def test_scrape_chain_skips_tinyfish_for_named_sessions(monkeypatch):
    """Named sessions never leak cookies into a cookieless cloud fetch."""
    monkeypatch.setattr(main_mod, "HTTP_FASTPATH_ENABLED", False)
    monkeypatch.setattr(main_mod, "TINYFISH_SCRAPE_FALLBACK", True)
    monkeypatch.setattr(main_mod, "JINA_SCRAPE_FALLBACK", False)
    monkeypatch.setattr(main_mod, "EXA_SCRAPE_FALLBACK", False)

    async def fail_browser(url, *, mode, session):
        raise RuntimeError("WAF hard block")

    def fail(*_a, **_k):
        raise AssertionError("named sessions must never reach the cloud fallbacks")

    monkeypatch.setattr(main_mod, "browser_scrape", fail_browser)
    monkeypatch.setattr(main_mod, "tinyfish_scrape", fail)
    monkeypatch.setattr(main_mod, "jina_scrape", fail)
    monkeypatch.setattr(main_mod, "exa_scrape", fail)
    with pytest.raises(RuntimeError, match="WAF hard block"):
        asyncio.run(main_mod._run_scrape_transports("https://example.com/", mode="extract", session="mysession"))


# ---------------------------------------------------------------------------
#  /tinyfish_search and /tinyfish_scrape endpoints
# ---------------------------------------------------------------------------

def test_search_endpoint_disabled_returns_503(monkeypatch):
    monkeypatch.setattr(tc, "TINYFISH_ENABLED", False)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(main_mod.tinyfish_search_endpoint(q="q", max_results=5))
    assert excinfo.value.status_code == 503


def test_search_endpoint_maps_transport_errors(monkeypatch):
    async def fake_search(*_a, **_k):
        raise RuntimeError("upstream timeout")

    monkeypatch.setattr(main_mod, "tinyfish_search", fake_search)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(main_mod.tinyfish_search_endpoint(q="q", max_results=5))
    assert excinfo.value.status_code == 502


def test_search_endpoint_serves_normalized_results(monkeypatch):
    _serve_json(monkeypatch, lambda *a: (200, _tinyfish_search_payload(2), {}))
    out = asyncio.run(main_mod.tinyfish_search_endpoint(q="california rental laws", max_results=2))
    assert out["engine"] == "tinyfish"
    assert out["number_of_results"] == 2


def test_search_endpoint_429_carries_retry_after(monkeypatch):
    _serve_json(monkeypatch, lambda *a: (429, {"error": {"code": "RATE_LIMITED", "message": "slow down"}}, {"retry-after": "3"}))
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(main_mod.tinyfish_search_endpoint(q="q", max_results=2))
    assert excinfo.value.status_code == 429
    assert excinfo.value.headers == {"Retry-After": "3"}


def test_scrape_endpoint_rejects_named_sessions(monkeypatch):
    req = main_mod.ScrapeRequest(url="https://example.com/", session="mysession")
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(main_mod.tinyfish_scrape_endpoint(req))
    assert excinfo.value.status_code == 400


def test_scrape_endpoint_unconfigured_returns_503(monkeypatch):
    monkeypatch.setattr(tc, "TINYFISH_ENABLED", True)
    monkeypatch.setattr(tc, "TINYFISH_API_KEY", "")
    req = main_mod.ScrapeRequest(url="https://example.com/")
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(main_mod.tinyfish_scrape_endpoint(req))
    assert excinfo.value.status_code == 503


def test_scrape_endpoint_serves_result(monkeypatch):
    _serve_json(monkeypatch, lambda *a: (200, _fetch_payload("# Hello"), {}))
    req = main_mod.ScrapeRequest(url="https://example.com/")
    out = asyncio.run(main_mod.tinyfish_scrape_endpoint(req))
    assert out["transport"] == "tinyfish"
    assert out["markdown"].startswith("# Hello")
