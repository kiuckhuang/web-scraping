"""Disabled SearXNG never receives searches or health probes."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from bridge import main


@pytest.fixture(autouse=True)
def disabled(monkeypatch):
    monkeypatch.setattr(main, "SEARXNG_ENABLED", False)
    monkeypatch.setattr(main, "SEARCH_PRIMARY", "searxng")
    monkeypatch.setattr(main, "SEARCH_FALLBACK_BING", True)
    monkeypatch.setattr(main, "SEARCH_FALLBACK_BROWSER", True)
    monkeypatch.setattr(main, "JINA_SEARCH_FALLBACK", False)
    monkeypatch.setattr(main, "EXA_SEARCH_FALLBACK", False)

    def fail(*args, **kwargs):
        raise AssertionError("disabled SearXNG must never be contacted")

    monkeypatch.setattr(main, "searxng_search", fail)
    monkeypatch.setattr(main, "searxng_health", fail)


def search(**kwargs):
    params = dict(categories=None, language="en", pageno=1, time_range=None, safesearch=0, max_results=3)
    return asyncio.run(main._search_with_fallbacks("q", **{**params, **kwargs}))


def test_browser_serves_despite_searxng_primary_setting(monkeypatch):
    async def browser(*args, **kwargs):
        return {"engine": "duckduckgo", "results": [{"title": "A", "url": "https://example.com"}]}

    monkeypatch.setattr(main, "browser_web_search", browser)
    out = search()
    assert out["provider"] == "browser:duckduckgo"
    assert out["fallback_used"] is False
    assert [a["provider"] for a in out["attempts"]] == ["browser"]


def test_exa_follows_browser_without_searxng(monkeypatch):
    calls = []

    async def browser(*args, **kwargs):
        calls.append("browser")
        return {"results": []}

    async def exa(*args, **kwargs):
        calls.append("exa")
        return {"results": [{"title": "A", "url": "https://example.com"}]}

    monkeypatch.setattr(main, "browser_web_search", browser)
    monkeypatch.setattr(main, "EXA_SEARCH_FALLBACK", True)
    monkeypatch.setattr(main, "exa_search", exa)
    out = search()
    assert calls == ["browser", "exa"]
    assert out["provider"] == "exa" and out["fallback_used"]


@pytest.mark.parametrize("kwargs", [{"categories": "news"}, {"categories": "general,it"}, {"pageno": 2}])
def test_unsupported_search_requires_explicit_enablement(kwargs):
    with pytest.raises(HTTPException, match="SEARXNG_ENABLED=true") as exc:
        search(**kwargs)
    assert exc.value.status_code == 400


def test_bang_search_requires_explicit_enablement():
    with pytest.raises(HTTPException, match="SEARXNG_ENABLED=true"):
        asyncio.run(main._run_search_chain("!google q", categories=None, language="en", pageno=1,
                                           time_range=None, safesearch=0, max_results=3))


def test_health_reports_off_without_probing_searxng(monkeypatch):
    async def browser():
        return True

    monkeypatch.setattr(main, "browser_health", browser)
    out = asyncio.run(main.health_check())
    assert out["status"] == "ok"
    assert out["services"]["searxng"] == "off"


def test_enabled_searxng_is_probed_and_can_degrade_health(monkeypatch):
    async def searxng():
        return False

    async def browser():
        return True

    monkeypatch.setattr(main, "SEARXNG_ENABLED", True)
    monkeypatch.setattr(main, "searxng_health", searxng)
    monkeypatch.setattr(main, "browser_health", browser)
    out = asyncio.run(main.health_check())
    assert out["status"] == "degraded" and out["services"]["searxng"] == "down"
