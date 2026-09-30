"""Returned error pages must not masquerade as successful scrapes."""
from __future__ import annotations

import asyncio

import bridge.main as main
import pytest
from fastapi import HTTPException


@pytest.mark.parametrize("content", [{"waf_challenge": True}, {"status": 403}, {"status": 503}])
def test_returned_browser_failure_triggers_jina(monkeypatch, content):
    monkeypatch.setattr(main, "HTTP_FASTPATH_ENABLED", False)
    monkeypatch.setattr(main, "JINA_SCRAPE_FALLBACK", True)
    monkeypatch.setattr(main, "EXA_SCRAPE_FALLBACK", False)

    async def browser(*args, **kwargs):
        return content

    async def jina(*args, **kwargs):
        return {"status": 200, "markdown": "real content"}

    monkeypatch.setattr(main, "browser_scrape", browser)
    monkeypatch.setattr(main, "jina_scrape", jina)
    out = asyncio.run(main._scrape_with_transports("https://example.com", mode="extract", session=None))
    assert out["transport"] == "jina"


@pytest.mark.parametrize("session,status", [(None, 404), ("login", 403)])
def test_not_found_and_named_sessions_do_not_use_cloud(monkeypatch, session, status):
    monkeypatch.setattr(main, "HTTP_FASTPATH_ENABLED", False)
    monkeypatch.setattr(main, "JINA_SCRAPE_FALLBACK", True)
    monkeypatch.setattr(main, "EXA_SCRAPE_FALLBACK", True)

    async def browser(*args, **kwargs):
        return {"status": status}

    def fail(*args, **kwargs):
        raise AssertionError("cloud fallback must not run")

    monkeypatch.setattr(main, "browser_scrape", browser)
    monkeypatch.setattr(main, "jina_scrape", fail)
    monkeypatch.setattr(main, "exa_scrape", fail)
    with pytest.raises((RuntimeError, HTTPException)):
        asyncio.run(main._scrape_with_transports("https://example.com", mode="extract", session=session))
