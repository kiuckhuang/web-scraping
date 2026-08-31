"""Unit tests for the /search fallback chain (no network required).

Chain under test: SearXNG merge → SearXNG "!bing" forced query → stealth-
browser SERP (google, then duckduckgo). All clients are faked; the tests
assert wiring and gating, not network behavior.
"""

import asyncio

import bridge.main as main_mod
import pytest
from fastapi import HTTPException


def _hit(title: str = "Example") -> dict:
    return {"title": title, "url": "https://example.com/", "content": "example snippet", "engine": "x"}


def _searxng_response(results: list[dict], unresponsive: list | None = None) -> dict:
    return {
        "query": "q",
        "number_of_results": len(results),
        "results": results,
        "unresponsive_engines": unresponsive or [],
    }


def _run(q: str = "q", **kwargs) -> dict:
    defaults = {"categories": None, "language": "en", "pageno": 1, "time_range": None, "safesearch": 0, "max_results": 10}
    defaults.update(kwargs)
    return asyncio.run(main_mod._search_with_fallbacks(q, **defaults))


def _boom(*_a, **_k):
    raise RuntimeError("connection refused")


# ---------------------------------------------------------------------------
#  Stage 0 — primary SearXNG merge succeeds
# ---------------------------------------------------------------------------

def test_primary_results_skip_fallbacks(monkeypatch):
    """A non-empty primary response must be returned untouched, no fallback calls."""
    calls: list[str] = []

    async def fake_searxng(query, **_k):
        calls.append(f"searxng:{query}")
        return _searxng_response([_hit()])

    def fail(*_a, **_k):
        raise AssertionError("fallback must not run when the primary merge returns results")

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "browser_web_search", fail)
    resp = _run()
    assert calls == ["searxng:q"]
    assert "fallback" not in resp
    assert resp["results"][0]["engine"] == "x"


# ---------------------------------------------------------------------------
#  Stage 1 — bing fallback via SearXNG bang
# ---------------------------------------------------------------------------

def test_bing_fallback_when_merge_empty(monkeypatch):
    seen: list[str] = []

    async def fake_searxng(query, **_k):
        seen.append(query)
        if query.startswith("!bing"):
            return _searxng_response([_hit("Bing result")])
        return _searxng_response([], unresponsive=[["duckduckgo", "CAPTCHA"]])

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    resp = _run()
    assert seen == ["q", "!bing q"]
    assert resp["fallback"] == "bing"
    # The bang must not leak into the query the client sees.
    assert resp["query"] == "q"
    # Primary's unresponsive list is preserved so clients still see the gap.
    assert resp["unresponsive_engines"] == [["duckduckgo", "CAPTCHA"]]


def test_bing_error_falls_through_to_browser(monkeypatch):
    async def fake_searxng(query, **_k):
        if query.startswith("!bing"):
            raise RuntimeError("searxng exploded")
        return _searxng_response([])

    async def fake_browser(query, count=10, engines=("duckduckgo",)):
        assert engines == main_mod.BROWSER_SEARCH_ENGINES
        return {"engine": "google", "results": [{"title": "G", "url": "https://g.example/", "snippet": "s"}]}

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "browser_web_search", fake_browser)
    resp = _run()
    assert resp["fallback"] == "browser:google"


# ---------------------------------------------------------------------------
#  Stage 2 — stealth-browser SERP fallback
# ---------------------------------------------------------------------------

def test_browser_fallback_normalizes_results(monkeypatch):
    async def fake_searxng(query, **_k):
        return _searxng_response([])

    async def fake_browser(query, count=10, engines=("google", "duckduckgo")):
        return {
            "engine": "google",
            "results": [
                {"title": "G1", "url": "https://a.example/", "snippet": " alpha "},
                {"title": "no url"},  # dropped — no url
            ],
        }

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "browser_web_search", fake_browser)
    resp = _run()
    assert resp["fallback"] == "browser:google"
    assert resp["number_of_results"] == 1
    assert resp["results"] == [{"title": "G1", "url": "https://a.example/", "content": "alpha", "engine": "google"}]


def test_browser_falls_back_to_ddg_when_google_empty(monkeypatch):
    async def fake_searxng(query, **_k):
        return _searxng_response([])

    async def fake_browser(query, count=10, engines=("google", "duckduckgo")):
        assert list(engines) == ["google", "duckduckgo"]
        return {"engine": "duckduckgo", "results": [{"title": "D", "url": "https://d.example/", "snippet": "s"}]}

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "browser_web_search", fake_browser)
    resp = _run()
    assert resp["fallback"] == "browser:duckduckgo"


def test_everything_empty_returns_primary(monkeypatch):
    async def fake_searxng(query, **_k):
        return _searxng_response([])

    async def fake_browser(query, count=10, engines=("google", "duckduckgo")):
        return {"engine": "duckduckgo", "results": [], "engines_tried": list(engines)}

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "browser_web_search", fake_browser)
    resp = _run()
    assert resp["results"] == []
    assert "fallback" not in resp


def test_searxng_down_browser_serves(monkeypatch):
    """SearXNG container down entirely → the browser fallback still answers."""

    async def fake_searxng(query, **_k):
        _boom()

    async def fake_browser(query, count=10, engines=("google", "duckduckgo")):
        return {"engine": "duckduckgo", "results": [{"title": "D", "url": "https://d.example/", "snippet": "s"}]}

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "browser_web_search", fake_browser)
    resp = _run()
    assert resp["fallback"] == "browser:duckduckgo"
    assert resp["results"][0]["url"] == "https://d.example/"


def test_searxng_down_and_nothing_else_serves(monkeypatch):
    async def fake_searxng(query, **_k):
        _boom()

    async def fake_browser(query, count=10, engines=("google", "duckduckgo")):
        return {"engine": "duckduckgo", "results": []}

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "browser_web_search", fake_browser)
    with pytest.raises(HTTPException) as excinfo:
        _run()
    assert excinfo.value.status_code == 502
    assert "All search backends failed" in excinfo.value.detail


# ---------------------------------------------------------------------------
#  Gating
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs", [
    {"categories": "images"},
    {"categories": "it,news"},
    {"pageno": 2},
])
def test_non_general_or_paged_queries_never_fall_back(monkeypatch, kwargs):
    async def fake_searxng(query, **_k):
        return _searxng_response([])

    def fail(*_a, **_k):
        raise AssertionError("fallback must be gated to general first-page searches")

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "browser_web_search", fail)
    resp = _run(**kwargs)
    assert resp["results"] == []
    assert "fallback" not in resp


def test_general_category_still_falls_back(monkeypatch):
    async def fake_searxng(query, **_k):
        return _searxng_response([])

    async def fake_browser(query, count=10, engines=("google", "duckduckgo")):
        return {"engine": "google", "results": [{"title": "G", "url": "https://g.example/", "snippet": "s"}]}

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "browser_web_search", fake_browser)
    resp = _run(categories="general,it")
    assert resp["fallback"] == "browser:google"


def test_explicit_bang_query_never_falls_back(monkeypatch):
    async def fake_searxng(query, **_k):
        return _searxng_response([])

    def fail(*_a, **_k):
        raise AssertionError("an explicit !bang means the caller directs engines — no fallback")

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "browser_web_search", fail)
    resp = _run(q="!google rust")
    assert resp["results"] == []


# ---------------------------------------------------------------------------
#  Env knobs
# ---------------------------------------------------------------------------

def test_bing_knob_off_skips_bing(monkeypatch):
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BING", False)

    async def fake_searxng(query, **_k):
        if query.startswith("!bing"):
            raise AssertionError("bing stage must be skipped when SEARCH_FALLBACK_BING is off")
        return _searxng_response([])

    async def fake_browser(query, count=10, engines=("google", "duckduckgo")):
        return {"engine": "google", "results": [{"title": "G", "url": "https://g.example/", "snippet": "s"}]}

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "browser_web_search", fake_browser)
    resp = _run()
    assert resp["fallback"] == "browser:google"


def test_browser_knob_off_skips_browser(monkeypatch):
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BROWSER", False)

    async def fake_searxng(query, **_k):
        return _searxng_response([])

    def fail(*_a, **_k):
        raise AssertionError("browser stage must be skipped when SEARCH_FALLBACK_BROWSER is off")

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "browser_web_search", fail)
    resp = _run()
    assert resp["results"] == []
    assert "fallback" not in resp
