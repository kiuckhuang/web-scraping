"""Unit tests for the Camoufox browser client (bridge.bridge.browser_client).

Network-free: the Playwright entrypoints are faked, no browser is launched.
Run inside the bridge container or via CI / `make test-unit`.
"""

import asyncio

import bridge.browser_client as bc
import pytest


@pytest.fixture(autouse=True)
def _reset_browser_state():
    """Keep module-level browser/playwright handles from leaking between tests."""
    bc._browser = None
    bc._playwright_ctx = None
    yield
    bc._browser = None
    bc._playwright_ctx = None


class _FakeBrowser:
    def __init__(self):
        self.connected = True
        self.contexts = []

    def is_connected(self):
        return self.connected


class _FakeFirefoxType:
    def __init__(self, recorder):
        self._recorder = recorder

    async def connect(self, ws_url, **kwargs):
        self._recorder["connect"] = ws_url
        return _FakeBrowser()


class _FakePlaywright:
    def __init__(self, recorder):
        self.firefox = _FakeFirefoxType(recorder)

    async def stop(self):
        pass


@pytest.fixture
def fake_playwright(monkeypatch):
    recorder = {}

    class _FakePlaywrightFactory:
        async def start(self):
            return _FakePlaywright(recorder)

    monkeypatch.setattr(bc, "async_playwright", _FakePlaywrightFactory)
    return recorder


def test_get_browser_uses_firefox_connect(fake_playwright, monkeypatch):
    monkeypatch.setattr(bc, "CAMOUFOX_WS_URL", "ws://camoufox:9222/browser")

    browser = asyncio.run(bc._get_browser())

    assert fake_playwright["connect"] == "ws://camoufox:9222/browser"
    assert browser.is_connected() is True


def test_get_browser_reuses_connection(fake_playwright):
    asyncio.run(bc._get_browser())
    first_endpoint = fake_playwright["connect"]
    asyncio.run(bc._get_browser())
    # Only one websocket connect for repeated calls.
    assert fake_playwright["connect"] == first_endpoint


def test_camoufox_health_tcp_probe(monkeypatch):
    """Health is a TCP connect; a closed port must report unhealthy."""
    monkeypatch.setattr(bc, "CAMOUFOX_WS_URL", "ws://127.0.0.1:1/browser")  # nothing listens on port 1
    assert asyncio.run(bc.health()) is False


def test_close_page_closes_page_and_isolated_context(monkeypatch):
    class FakeContext:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    class FakePage:
        def __init__(self):
            self.context = FakeContext()
            self.closed = False

        async def close(self):
            self.closed = True

    page = FakePage()
    monkeypatch.setattr(bc, "ISOLATE_CONTEXTS", True)
    asyncio.run(bc._close_page(page))
    assert page.closed is True
    assert page.context.closed is True


def test_close_page_keeps_shared_context(monkeypatch):
    """With isolation off, the page closes but the shared context survives."""

    class FakeContext:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    class FakePage:
        def __init__(self):
            self.context = FakeContext()
            self.closed = False

        async def close(self):
            self.closed = True

    page = FakePage()
    monkeypatch.setattr(bc, "ISOLATE_CONTEXTS", False)
    asyncio.run(bc._close_page(page))
    assert page.closed is True
    assert page.context.closed is False
