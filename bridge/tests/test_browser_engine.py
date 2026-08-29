"""Unit tests for browser-engine selection (Fortress CDP vs Camoufox websocket).

Network-free: the Playwright entrypoints are faked, no browser is launched.
Run inside the bridge container or via CI / `make test-unit`.
"""

import asyncio

import bridge.fortress_client as fc
import pytest


@pytest.fixture(autouse=True)
def _reset_browser_state():
    """Keep module-level browser/playwright handles from leaking between tests."""
    fc._browser = None
    fc._playwright_ctx = None
    yield
    fc._browser = None
    fc._playwright_ctx = None


class _FakeBrowser:
    def __init__(self):
        self.connected = True

    def is_connected(self):
        return self.connected


class _FakeFirefoxType:
    def __init__(self, recorder):
        self._recorder = recorder

    async def connect(self, ws_url, **kwargs):
        self._recorder["firefox_connect"] = ws_url
        return _FakeBrowser()


class _FakeChromiumType:
    def __init__(self, recorder):
        self._recorder = recorder

    async def connect_over_cdp(self, url, **kwargs):
        self._recorder["cdp_connect"] = url
        return _FakeBrowser()


class _FakePlaywright:
    def __init__(self, recorder):
        self.firefox = _FakeFirefoxType(recorder)
        self.chromium = _FakeChromiumType(recorder)

    async def stop(self):
        pass


@pytest.fixture
def fake_playwright(monkeypatch):
    recorder = {}

    class _FakePlaywrightFactory:
        def __init__(self):
            pass

        async def start(self):
            return _FakePlaywright(recorder)

    monkeypatch.setattr(fc, "async_playwright", _FakePlaywrightFactory)
    return recorder


def test_camoufox_engine_uses_firefox_connect(fake_playwright, monkeypatch):
    monkeypatch.setattr(fc, "BROWSER_ENGINE", "camoufox")
    monkeypatch.setattr(fc, "CAMOUFOX_WS_URL", "ws://camoufox:9222/browser")

    browser = asyncio.run(fc._get_browser())

    assert fake_playwright["firefox_connect"] == "ws://camoufox:9222/browser"
    assert "cdp_connect" not in fake_playwright
    assert browser.is_connected() is True


def test_fortress_engine_uses_cdp_connect(fake_playwright, monkeypatch):
    monkeypatch.setattr(fc, "BROWSER_ENGINE", "fortress")
    monkeypatch.setattr(fc, "FORTRESS_CDP_URL", "http://fortress:9222")
    monkeypatch.setattr(fc, "_resolve_cdp_url", lambda url: "http://10.89.0.5:9222")

    browser = asyncio.run(fc._get_browser())

    assert fake_playwright["cdp_connect"] == "http://10.89.0.5:9222"
    assert "firefox_connect" not in fake_playwright
    assert browser.is_connected() is True


def test_invalid_engine_rejected():
    with pytest.raises(RuntimeError, match="BROWSER_ENGINE"):
        fc._normalize_engine("puppeteer")
    assert fc._normalize_engine("Fortress ") == "fortress"
    assert fc._normalize_engine("CAMOUFOX") == "camoufox"


def test_camoufox_health_tcp_probe(monkeypatch):
    """Camoufox health is a TCP connect; a closed port must report unhealthy."""
    monkeypatch.setattr(fc, "BROWSER_ENGINE", "camoufox")
    monkeypatch.setattr(fc, "CAMOUFOX_WS_URL", "ws://127.0.0.1:1/browser")  # nothing listens on port 1
    assert asyncio.run(fc.health()) is False


def test_fortress_health_http_probe(fake_playwright, monkeypatch):
    """Fortress health still goes through the CDP /json/version HTTP probe."""
    monkeypatch.setattr(fc, "BROWSER_ENGINE", "fortress")
    monkeypatch.setattr(fc, "FORTRESS_CDP_URL", "http://fortress:9222")
    monkeypatch.setattr(fc, "_resolve_cdp_url", lambda url: "http://10.89.0.5:9222")

    class _FakeResp:
        status_code = 200

    class _FakeClient:
        async def get(self, url):
            fake_playwright["health_url"] = url
            return _FakeResp()

    monkeypatch.setattr(fc, "_get_health_client", lambda: _FakeClient())
    assert asyncio.run(fc.health()) is True
    assert fake_playwright["health_url"] == "http://10.89.0.5:9222/json/version"
