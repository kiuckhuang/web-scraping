"""Bounded search/scrape waits and cache isolation."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from bridge import main, searxng_client


def test_search_deadline_cancels_running_stage(monkeypatch):
    cancelled = []

    async def slow(*args, **kwargs):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)

    monkeypatch.setattr(main, "_run_search_chain", slow)
    monkeypatch.setattr(main, "SEARCH_MAX_SECONDS", 0.01)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main._search_with_fallbacks("q", categories=None, language="en", pageno=1,
                                               time_range=None, safesearch=0, max_results=3))
    assert exc.value.status_code == 504
    assert cancelled


def test_scrape_deadline_includes_queue_wait(monkeypatch):
    async def slow(*args, **kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(main, "_run_scrape_transports", slow)
    monkeypatch.setattr(main, "SCRAPE_MAX_SECONDS", 0.01)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main._scrape_with_transports("https://example.com", mode="extract", session=None))
    assert exc.value.status_code == 504


def test_http_deadline_cancels_handler_and_returns_504(monkeypatch):
    cancelled = []
    messages = []

    async def app(scope, receive, send):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)

    async def send(message):
        messages.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    monkeypatch.setattr(main, "COMBINED_MAX_SECONDS", 0.01)
    asyncio.run(main.OperationDeadline(app)({"type": "http", "path": "/search_and_scrape"}, receive, send))
    assert messages[0]["status"] == 504
    assert cancelled


def test_search_cache_is_filter_scoped_and_returns_copies(monkeypatch):
    calls = []

    async def chain(*args, **kwargs):
        calls.append(kwargs)
        return {"results": [{"title": "A", "url": "https://example.com"}]}

    monkeypatch.setattr(main, "_run_search_chain", chain)
    monkeypatch.setattr(main, "SEARCH_CACHE_TTL", 60)
    monkeypatch.setattr(main, "SEARCH_CACHE_MAX", 10)

    async def run():
        kwargs = dict(categories=None, language="en", pageno=1, time_range=None, safesearch=0, max_results=3)
        first = await main._search_with_fallbacks("q", **kwargs)
        first["results"].clear()
        cached = await main._search_with_fallbacks("q", **kwargs)
        assert cached["cached"] and cached["results"]
        await main._search_with_fallbacks("q", **{**kwargs, "time_range": "week"})

    asyncio.run(run())
    assert len(calls) == 2


def test_searxng_breaker_skips_repeated_captcha_but_not_categories(monkeypatch):
    import httpx
    calls = []

    class Client:
        async def get(self, url, *, params):
            calls.append(params)
            return httpx.Response(200, json={"results": [], "unresponsive_engines": [["google", "CAPTCHA"]]},
                                  request=httpx.Request("GET", url))

    monkeypatch.setattr(searxng_client, "_get_client", lambda: Client())
    monkeypatch.setattr(searxng_client, "BREAKER_THRESHOLD", 2)
    monkeypatch.setattr(searxng_client, "BREAKER_COOLDOWN", 60)

    async def run():
        await searxng_client.search("q")
        await searxng_client.search("q")
        with pytest.raises(RuntimeError, match="circuit breaker"):
            await searxng_client.search("q")
        await searxng_client.search("q", categories="it")

    asyncio.run(run())
    assert len(calls) == 3
