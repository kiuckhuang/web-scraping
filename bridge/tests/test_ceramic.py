"""Unit tests for the optional Ceramic search transport — network-free.

The Ceramic HTTP seam (`ceramic_client._request_json`) is monkeypatched, as
are the stage functions inside `bridge.main`. Mirrors the search half of
test_exa.py; Ceramic has no contents endpoint, so there is no scrape section.
Covers:

- client behavior: gating, normalization, request body, error-envelope decoding
- chain wiring: ceramic strictly AFTER every self-hosted stage, Jina and Exa
- /ceramic_search endpoint (disabled state, error mapping)
"""

from __future__ import annotations

import asyncio

import bridge.ceramic_client as cc
import bridge.main as main_mod
import pytest
from bridge.ceramic_client import CeramicError
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def _ceramic_enabled(monkeypatch):
    """Most tests exercise the enabled path; the disabled-state tests override."""
    monkeypatch.setattr(cc, "CERAMIC_ENABLED", True)
    monkeypatch.setattr(cc, "CERAMIC_API_KEY", "cer_test_key")


@pytest.fixture(autouse=True)
def _pin_chain(monkeypatch):
    """Pin the classic chain so tests drive stages explicitly. The deployed
    image's .env may enable the Jina/Exa fallbacks — tests that want them pin
    them per-test (deployment-config rule, AGENTS.md)."""
    monkeypatch.setattr(main_mod, "SEARCH_PRIMARY", "searxng")
    monkeypatch.setattr(main_mod, "SEARXNG_ENABLED", True)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BING", True)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BROWSER", True)
    monkeypatch.setattr(main_mod, "JINA_SEARCH_FALLBACK", False)
    monkeypatch.setattr(main_mod, "JINA_SCRAPE_FALLBACK", False)
    monkeypatch.setattr(main_mod, "EXA_SEARCH_FALLBACK", False)
    monkeypatch.setattr(main_mod, "EXA_SCRAPE_FALLBACK", False)


def _serve_json(monkeypatch, handler):
    """Replace the Ceramic request seam; handler(method, url, json_body)."""
    calls: list[dict] = []

    async def fake_request(method, url, *, json_body=None):
        calls.append({"method": method, "url": url, "json_body": json_body})
        return handler(method, url, json_body)

    monkeypatch.setattr(cc, "_request_json", fake_request)
    return calls


def _hit(title: str = "Example") -> dict:
    return {"title": title, "url": "https://example.com/", "content": "snippet", "engine": "x"}


def _searxng_response(results: list[dict]) -> dict:
    return {"query": "q", "number_of_results": len(results), "results": results, "unresponsive_engines": []}


def _ceramic_payload(n: int = 1) -> dict:
    return {"requestId": "r1", "result": {"results": [
        {"title": f"T{i}", "url": f"https://{i}.example/", "description": "d" * 500} for i in range(n)
    ], "searchMetadata": {"executionTime": 0.1}, "totalResults": n}}


def _run_search(**kwargs) -> dict:
    defaults = {"categories": None, "language": "en", "pageno": 1, "time_range": None, "safesearch": 0, "max_results": 10}
    defaults.update(kwargs)
    return asyncio.run(main_mod._search_with_fallbacks("q", **defaults))


# ---------------------------------------------------------------------------
#  Client
# ---------------------------------------------------------------------------

def test_search_disabled_returns_503(monkeypatch):
    monkeypatch.setattr(cc, "CERAMIC_ENABLED", False)
    with pytest.raises(CeramicError) as exc:
        asyncio.run(cc.search("q"))
    assert exc.value.bridge_status == 503


def test_search_without_key_returns_503(monkeypatch):
    """api.ceramic.ai has no anonymous tier — refuse before spending credits."""
    monkeypatch.setattr(cc, "CERAMIC_API_KEY", "")
    with pytest.raises(CeramicError) as exc:
        asyncio.run(cc.search("q"))
    assert exc.value.bridge_status == 503
    assert "CERAMIC_API_KEY" in str(exc.value)


def test_search_normalizes_to_searxng_shape(monkeypatch):
    payload = {"result": {"results": [
        {"title": "T1", "url": "https://a.example/", "description": "desc one"},
        {"title": "T2", "url": "https://b.example/", "description": "x" * 500},
        {"title": "no url", "url": "", "description": "dropped"},
    ]}}
    calls = _serve_json(monkeypatch, lambda *a: (200, payload))
    out = asyncio.run(cc.search("q", max_results=3))
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == f"{cc.CERAMIC_BASE_URL}/search"
    assert calls[0]["json_body"]["query"] == "q"
    assert calls[0]["json_body"]["maxResults"] == 3
    assert calls[0]["json_body"]["maxDescriptionLength"] == cc._DESCRIPTION_CHARS
    assert out["results"][0] == {"title": "T1", "url": "https://a.example/", "content": "desc one", "engine": "ceramic"}
    assert len(out["results"][1]["content"]) == cc._SNIPPET_CHARS
    assert out["number_of_results"] == 2
    assert out["engine"] == "ceramic"
    assert "ceramic_pages" not in out  # search-only transport: no page channel


def test_search_maxresults_clamped_to_limit(monkeypatch):
    calls = _serve_json(monkeypatch, lambda *a: (200, {"result": {"results": []}}))
    asyncio.run(cc.search("q", max_results=50))
    assert calls[0]["json_body"]["maxResults"] == cc.MAX_SEARCH_RESULTS


def test_search_401_maps_to_auth_error(monkeypatch):
    _serve_json(monkeypatch, lambda *a: (401, {"title": "Unauthorized", "status": 401, "detail": "invalid key"}))
    with pytest.raises(CeramicError) as exc:
        asyncio.run(cc.search("q"))
    assert "CERAMIC_API_KEY" in str(exc.value)


def test_search_429_maps_to_rate_limit_with_retry_after(monkeypatch):
    _serve_json(monkeypatch, lambda *a: (429, {"detail": "slow down", "retry_after_seconds": 7}))
    with pytest.raises(CeramicError) as exc:
        asyncio.run(cc.search("q"))
    assert exc.value.bridge_status == 429
    assert exc.value.retry_after == 7.0


def test_search_402_maps_to_quota_error(monkeypatch):
    _serve_json(monkeypatch, lambda *a: (402, {"title": "Payment Required", "detail": "credits exhausted"}))
    with pytest.raises(CeramicError) as exc:
        asyncio.run(cc.search("q"))
    assert "quota/billing" in str(exc.value)


def test_search_422_error_envelope_decoded(monkeypatch):
    payload = {"title": "Unprocessable Content", "status": 422, "detail": "Unsupported parameter: prompt", "code": "unsupported_parameter"}
    _serve_json(monkeypatch, lambda *a: (422, payload))
    with pytest.raises(CeramicError) as exc:
        asyncio.run(cc.search("q"))
    assert "Unsupported parameter" in str(exc.value)
    assert exc.value.bridge_status == 502


def test_status_values():
    assert cc.status() in ("off", "unconfigured", "ready")


# ---------------------------------------------------------------------------
#  Chain wiring — ceramic strictly after every self-hosted stage, Jina and Exa
# ---------------------------------------------------------------------------

def _empty_all_stages(monkeypatch, order: list[str]):
    async def fake_searxng(query, **_k):
        order.append("searxng")
        return _searxng_response([])

    async def fake_browser(query, count=10, engines=None):
        order.append("browser")
        return {"engine": "duckduckgo", "results": []}

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
    monkeypatch.setattr(main_mod, "jina_search", fake_jina)
    monkeypatch.setattr(main_mod, "exa_search", fake_exa)
    monkeypatch.setattr(main_mod, "ceramic_search", fake_ceramic)


def test_ceramic_is_the_final_stage(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BING", False)
    monkeypatch.setattr(main_mod, "JINA_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "EXA_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "CERAMIC_SEARCH_FALLBACK", True)
    _empty_all_stages(monkeypatch, order)
    resp = _run_search()
    assert order == ["searxng", "browser", "jina", "exa", "ceramic"]
    assert resp["fallback"] == "ceramic"
    assert resp["results"][0]["engine"] == "ceramic"


def test_ceramic_stage_absent_when_disabled(monkeypatch):
    """With the Ceramic stage off, an all-stages-empty run behaves exactly as
    before Ceramic existed — the (empty) primary SearXNG response is returned."""
    order: list[str] = []
    monkeypatch.setattr(main_mod, "JINA_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "EXA_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "CERAMIC_SEARCH_FALLBACK", False)
    _empty_all_stages(monkeypatch, order)
    resp = _run_search()
    assert "ceramic" not in order
    assert resp["results"] == []


def test_ceramic_never_runs_when_earlier_stages_serve(monkeypatch):
    async def fake_searxng(query, **_k):
        return _searxng_response([_hit()])

    def fail(*_a, **_k):
        raise AssertionError("ceramic must not run when earlier stages serve results")

    monkeypatch.setattr(main_mod, "JINA_SEARCH_FALLBACK", False)
    monkeypatch.setattr(main_mod, "EXA_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "CERAMIC_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "exa_search", fail)
    monkeypatch.setattr(main_mod, "ceramic_search", fail)
    resp = _run_search()
    assert "fallback" not in resp


@pytest.mark.parametrize("kwargs", [{"pageno": 2}, {"categories": "images"}, {"time_range": "week"}])
def test_ceramic_respects_fallback_gating(monkeypatch, kwargs):
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BING", False)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BROWSER", False)
    monkeypatch.setattr(main_mod, "CERAMIC_SEARCH_FALLBACK", True)

    async def fake_searxng(query, **_k):
        return _searxng_response([])

    def fail(*_a, **_k):
        raise AssertionError("ceramic must respect the page-1 general-web and filter gates")

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "ceramic_search", fail)
    resp = _run_search(**kwargs)
    assert resp["results"] == []


def test_ceramic_stage_error_swallowed_when_primary_alive(monkeypatch):
    async def fake_searxng(query, **_k):
        return _searxng_response([])

    async def fake_ceramic(query, *, max_results=5):
        raise CeramicError("Ceramic rate limit exceeded", bridge_status=429)

    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BING", False)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BROWSER", False)
    monkeypatch.setattr(main_mod, "CERAMIC_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "ceramic_search", fake_ceramic)
    resp = _run_search()
    assert resp["results"] == []


# ---------------------------------------------------------------------------
#  /ceramic_search endpoint
# ---------------------------------------------------------------------------

def test_endpoint_disabled_returns_503(monkeypatch):
    monkeypatch.setattr(cc, "CERAMIC_ENABLED", False)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(main_mod.ceramic_search_endpoint(q="q", max_results=5))
    assert excinfo.value.status_code == 503


def test_endpoint_serves_normalized_results(monkeypatch):
    _serve_json(monkeypatch, lambda *a: (200, _ceramic_payload(2)))
    out = asyncio.run(main_mod.ceramic_search_endpoint(q="california rental laws", max_results=2))
    assert out["engine"] == "ceramic"
    assert out["number_of_results"] == 2


def test_endpoint_429_carries_retry_after(monkeypatch):
    _serve_json(monkeypatch, lambda *a: (429, {"detail": "slow down", "retry_after_seconds": 3}))
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(main_mod.ceramic_search_endpoint(q="q", max_results=2))
    assert excinfo.value.status_code == 429
    assert excinfo.value.headers == {"Retry-After": "3"}
