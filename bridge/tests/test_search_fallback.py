"""Unit tests for the /search stage chain (no network required).

Chain under test (SEARCH_PRIMARY): SearXNG merge → SearXNG "!bing" forced
query → stealth-browser SERPs, or browser-first when SEARCH_PRIMARY=browser.
All clients are faked; the tests assert wiring and gating, not network
behavior.
"""

import asyncio

import bridge.main as main_mod
import pytest
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def _pin_classic_chain(monkeypatch):
    """Pin the classic searxng-primary chain: module-level knobs are read at
    import time and defaults changed over time — tests must not depend on
    deployment config. Individual tests override via monkeypatch."""
    monkeypatch.setattr(main_mod, "SEARCH_PRIMARY", "searxng")
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BING", True)
    monkeypatch.setattr(main_mod, "SEARCH_FALLBACK_BROWSER", True)


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

    async def fake_browser(query, count=10, engines=None):
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

    async def fake_browser(query, count=10, engines=None):
        assert list(engines) == list(main_mod.BROWSER_SEARCH_ENGINES)
        return {"engine": "duckduckgo", "results": [{"title": "D", "url": "https://d.example/", "snippet": "s"}]}

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "browser_web_search", fake_browser)
    resp = _run()
    assert resp["fallback"] == "browser:duckduckgo"


def test_everything_empty_returns_primary(monkeypatch):
    async def fake_searxng(query, **_k):
        return _searxng_response([])

    async def fake_browser(query, count=10, engines=None):
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

    async def fake_browser(query, count=10, engines=None):
        return {"engine": "duckduckgo", "results": [{"title": "D", "url": "https://d.example/", "snippet": "s"}]}

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "browser_web_search", fake_browser)
    resp = _run()
    assert resp["fallback"] == "browser:duckduckgo"
    assert resp["results"][0]["url"] == "https://d.example/"


def test_searxng_down_and_nothing_else_serves(monkeypatch):
    async def fake_searxng(query, **_k):
        _boom()

    async def fake_browser(query, count=10, engines=None):
        return {"engine": "duckduckgo", "results": []}

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "browser_web_search", fake_browser)
    with pytest.raises(HTTPException) as excinfo:
        _run()
    assert excinfo.value.status_code == 502
    assert "All search stages failed" in excinfo.value.detail


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

    async def fake_browser(query, count=10, engines=None):
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

    async def fake_browser(query, count=10, engines=None):
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


# ---------------------------------------------------------------------------
#  SEARCH_PRIMARY=browser — browser SERPs first, SearXNG as fallback
# ---------------------------------------------------------------------------

def test_browser_primary_serves_without_searxng(monkeypatch):
    """In browser-primary mode a working browser stage must not touch SearXNG."""
    monkeypatch.setattr(main_mod, "SEARCH_PRIMARY", "browser")

    def fail(*_a, **_k):
        raise AssertionError("searxng stage must not run when the browser stage serves")

    async def fake_browser(query, count=10, engines=None):
        assert list(engines) == list(main_mod.BROWSER_SEARCH_ENGINES)
        return {"engine": "duckduckgo", "results": [{"title": "D", "url": "https://d.example/", "snippet": "s"}]}

    monkeypatch.setattr(main_mod, "searxng_search", fail)
    monkeypatch.setattr(main_mod, "browser_web_search", fake_browser)
    resp = _run()
    assert resp["fallback"] == "browser:duckduckgo"


def test_browser_primary_falls_back_to_searxng(monkeypatch):
    monkeypatch.setattr(main_mod, "SEARCH_PRIMARY", "browser")
    seen: list[str] = []

    async def fake_searxng(query, **_k):
        seen.append(query)
        return _searxng_response([_hit("SearXNG result")])

    async def fake_browser(query, count=10, engines=None):
        return {"engine": "duckduckgo", "results": []}

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "browser_web_search", fake_browser)
    resp = _run()
    assert seen == ["q"]
    assert "fallback" not in resp
    assert resp["results"][0]["engine"] == "x"


def test_browser_primary_searxng_down_raises_502(monkeypatch):
    """Browser-primary + browser empty + SearXNG unreachable: every stage
    failed to produce a response, so the endpoint raises a 502."""
    monkeypatch.setattr(main_mod, "SEARCH_PRIMARY", "browser")

    async def fake_searxng(query, **_k):
        _boom()

    async def fake_browser(query, count=10, engines=None):
        return {"engine": "duckduckgo", "results": []}

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "browser_web_search", fake_browser)
    with pytest.raises(HTTPException) as excinfo:
        _run()
    assert excinfo.value.status_code == 502
    assert "All search stages failed" in excinfo.value.detail


def test_browser_primary_bing_stage_absent(monkeypatch):
    """Bing is dropped from the chain — it must never be consulted, even as
    a stage between browser and searxng in browser-primary mode."""
    monkeypatch.setattr(main_mod, "SEARCH_PRIMARY", "browser")

    async def fake_searxng(query, **_k):
        assert not query.startswith("!bing"), "bing stage must not exist anymore"
        return _searxng_response([])

    async def fake_browser(query, count=10, engines=None):
        return {"engine": "duckduckgo", "results": []}

    monkeypatch.setattr(main_mod, "searxng_search", fake_searxng)
    monkeypatch.setattr(main_mod, "browser_web_search", fake_browser)
    resp = _run()
    assert resp["results"] == []
