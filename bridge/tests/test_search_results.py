"""SERP fixture coverage; no browser or network is needed."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from bridge.search_results import normalize_destination, parse_serp

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("engine,fixture,urls", [
    ("google", "google.html", ["https://docs.python.org/3/library/asyncio.html", "https://example.com/guide"]),
    ("duckduckgo", "duckduckgo.html", ["https://example.com/guide"]),
    ("duckduckgo lite", "duckduckgo-lite.html", ["https://example.com/guide"]),
    ("google", "consent.html", []),
    ("google", "captcha.html", []),
])
def test_saved_serps(engine, fixture, urls):
    out = parse_serp((FIXTURES / fixture).read_text(), engine, 10)
    assert [r["url"] for r in out] == urls
    if out:
        assert out[0]["snippet"]


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://www.google.com/goto?url=opaque",
                                "https://user:pass@example.com", "https://example.com:bad"])
def test_unsafe_or_opaque_destinations_rejected(url):
    assert normalize_destination(url) is None


def test_google_query_enforces_filters(monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    from bridge import browser_client

    urls = []

    class Page:
        async def goto(self, url, **kwargs):
            urls.append(url)

        async def wait_for_selector(self, *args, **kwargs):
            pass

        async def wait_for_timeout(self, *args):
            pass

        async def content(self):
            return (FIXTURES / "google.html").read_text()

    asyncio.run(browser_client._serp_google(Page(), "q", 3, language="fr", time_range="week", safesearch=2))
    params = parse_qs(urlsplit(urls[0]).query)
    assert params["hl"] == ["fr"] and params["lr"] == ["lang_fr"]
    assert params["safe"] == ["active"] and params["tbs"] == ["qdr:w"]
