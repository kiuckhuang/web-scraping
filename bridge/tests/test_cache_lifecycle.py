"""Cache replacement and browser-context lifecycle regressions."""
from __future__ import annotations

import asyncio

import bridge.browser_client as browser
import bridge.main as main
import pytest


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    monkeypatch.setattr(main, "_cache", {})
    monkeypatch.setattr(main, "_cache_bytes", 0)
    monkeypatch.setattr(main, "BRIDGE_CACHE_MAX", 100)
    monkeypatch.setattr(main, "BRIDGE_CACHE_TTL", 300)
    monkeypatch.setattr(main, "BRIDGE_CACHE_MAX_BYTES", 10000)
    monkeypatch.setattr(browser, "_sessions", {})
    monkeypatch.setattr(browser, "_browser", None)
    monkeypatch.setattr(browser, "_playwright_ctx", None)
    monkeypatch.setattr(browser, "_session_invalidator", main._cache_invalidate_session)


def test_cache_overwrite_counts_bytes_once():
    for session in (None, "login"):
        main._cache_set("https://example.com", "extract", {"markdown": "old"}, session)
        main._cache_set("https://example.com", "extract", {"markdown": "new content"}, session)
    assert len(main._cache) == 2
    assert main._cache_bytes == sum(item[2] for item in main._cache.values())


def test_delete_and_recreate_session_does_not_reuse_content(monkeypatch):
    main._cache_set("https://example.com", "extract", {"markdown": "private"}, "login")

    async def close(name):
        return True

    monkeypatch.setattr(main, "browser_close_session", close)
    asyncio.run(main.delete_session("login"))
    assert asyncio.run(main._cache_get("https://example.com", "extract", "login")) is None
    assert main._cache_bytes == 0


def test_provider_cache_is_namespaced():
    """An explicit Jina/Exa call must not consume a generic browser entry."""
    main._cache_set("https://example.com", "extract", {"markdown": "browser"})
    assert asyncio.run(main._cache_get("https://example.com", "extract", transport="jina")) is None
    main._cache_set("https://example.com", "extract", {"markdown": "jina"}, transport="jina")
    assert asyncio.run(main._cache_get("https://example.com", "extract", transport="jina"))["markdown"] == "jina"
    assert asyncio.run(main._cache_get("https://example.com", "extract"))["markdown"] == "browser"


def test_disabled_jina_endpoint_does_not_serve_generic_cache(monkeypatch):
    class Req:
        url = "https://example.com"
        mode = "extract"
        session = None

    main._cache_set(Req.url, Req.mode, {"markdown": "browser"})
    monkeypatch.setattr(main, "_validate_public_url", lambda url: url)
    monkeypatch.setattr(main, "jina_status", lambda: "off")
    with pytest.raises(main.HTTPException) as exc:
        asyncio.run(main.jina_scrape_endpoint(Req()))  # type: ignore[arg-type]
    assert exc.value.status_code == 503


def test_browser_reset_invalidates_only_named_content():
    for session in (None, "a", "b"):
        main._cache_set("https://example.com", "extract", {"markdown": "body"}, session)
    asyncio.run(browser._reset_browser())
    assert list(main._cache) == [("https://example.com", "extract", "")]
    assert main._cache_bytes == sum(item[2] for item in main._cache.values())


def test_disconnected_browser_cannot_serve_session_cache(monkeypatch):
    class Disconnected:
        def is_connected(self):
            return False

    monkeypatch.setattr(browser, "_browser", Disconnected())
    main._cache_set("https://example.com", "extract", {"markdown": "private"}, "a")
    assert asyncio.run(main._cache_get("https://example.com", "extract", "a")) is None
