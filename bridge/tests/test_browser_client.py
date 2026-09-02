"""Unit tests for the Camoufox browser client (bridge.bridge.browser_client).

Network-free: the Playwright entrypoints are faked, no browser is launched.
Run inside the bridge container or via CI / `make test-unit`.
"""

import asyncio

import bridge.browser_client as bc
import pytest


@pytest.fixture(autouse=True)
def _reset_browser_state():
    """Keep module-level browser/session state from leaking between tests."""
    bc._browser = None
    bc._playwright_ctx = None
    bc._sessions.clear()
    bc._timezone_id = None
    bc._timezone_resolved = False
    yield
    bc._browser = None
    bc._playwright_ctx = None
    bc._sessions.clear()
    bc._timezone_id = None
    bc._timezone_resolved = False


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


# ---------------------------------------------------------------------------
#  Named sessions (login persistence)
# ---------------------------------------------------------------------------

class _FakeSessionContext:
    """Context double that records pages and participates in session bookkeeping."""

    def __init__(self, recorder):
        self._recorder = recorder
        self.closed = False

    async def new_page(self):
        self._recorder["pages"] = self._recorder.get("pages", 0) + 1

        class _FakePage:
            def __init__(self, context):
                self.context = context
                self.routes = []
                self.closed = False

            async def route(self, pattern, handler):
                self.routes.append(pattern)

            async def close(self):
                self.closed = True

        return _FakePage(self)

    async def close(self):
        self.closed = True


def _session_fake_browser(recorder):
    class _FakeBrowserWithSessions:
        def __init__(self):
            self.connected = True
            self.contexts = []

        def is_connected(self):
            return self.connected

        async def new_context(self, **kwargs):
            recorder["contexts"] = recorder.get("contexts", 0) + 1
            recorder.setdefault("context_kwargs", []).append(kwargs)
            return _FakeSessionContext(recorder)

    return _FakeBrowserWithSessions()


@pytest.fixture
def session_playwright(fake_playwright):
    """Replace the fake factory so connects return a session-capable browser."""
    recorder = fake_playwright

    browser = _session_fake_browser(recorder)

    class _FakePlaywright:
        firefox = type("F", (), {})()

        async def start(self):
            return self

        async def stop(self):
            pass

    async def _connect(ws_url, **kwargs):
        recorder["connect"] = ws_url
        return browser

    _FakePlaywright.firefox.connect = staticmethod(_connect)
    import bridge.browser_client as bcl

    bcl.async_playwright = lambda: _FakePlaywright()
    return recorder


def test_get_session_creates_once(session_playwright):
    a1 = asyncio.run(bc.get_session("work"))
    a2 = asyncio.run(bc.get_session("work"))
    b = asyncio.run(bc.get_session("other"))
    assert session_playwright["contexts"] == 2  # one per distinct name, not per call
    assert a1 is a2 and a1 is not b
    assert bc.list_sessions() == ["other", "work"]


def test_session_limit_rejects_excess(session_playwright, monkeypatch):
    monkeypatch.setattr(bc, "MAX_SESSIONS", 1)
    asyncio.run(bc.get_session("a"))
    with pytest.raises(bc.SessionLimitError, match="Session limit reached"):
        asyncio.run(bc.get_session("b"))
    assert asyncio.run(bc.close_session("a")) is True  # freeing makes room
    asyncio.run(bc.get_session("b"))


def test_close_session_closes_context(session_playwright):
    ctx = asyncio.run(bc.get_session("work"))
    assert asyncio.run(bc.close_session("work")) is True
    assert ctx.closed is True
    assert bc.list_sessions() == []
    assert asyncio.run(bc.close_session("work")) is False  # already gone


def test_close_page_keeps_session_context(session_playwright):
    """Pages served from a named session must not close that context."""
    ctx = asyncio.run(bc.get_session("work"))
    page = asyncio.run(ctx.new_page())
    asyncio.run(bc._close_page(page))
    assert page.closed is True
    assert ctx.closed is False  # the session survives the request
    assert bc.list_sessions() == ["work"]


def test_new_page_uses_session_context_and_guard(session_playwright):
    asyncio.run(bc._new_page("work"))
    assert session_playwright["contexts"] == 1
    asyncio.run(bc._new_page("work"))
    assert session_playwright["contexts"] == 1  # second request reuses the session



def test_context_timezone_derived_from_proxy(session_playwright, monkeypatch):
    """With a proxy configured, the egress timezone (resolved once through the
    proxy) is applied to every new context."""
    monkeypatch.setattr(bc, "CAMOUFOX_PROXY_SERVER", "http://10.8.8.1:8088")
    monkeypatch.setattr(bc, "CAMOUFOX_TIMEZONE", "")
    monkeypatch.setattr(bc, "_resolve_timezone_sync", lambda: "Asia/Hong_Kong")

    asyncio.run(bc._new_page("work"))
    asyncio.run(bc._new_page())
    kwargs = session_playwright["context_kwargs"]
    assert kwargs[0] == {"timezone_id": "Asia/Hong_Kong"}
    assert kwargs[1] == {"timezone_id": "Asia/Hong_Kong"}
    # resolved once, cached globally
    assert session_playwright["contexts"] == 2


def test_context_timezone_override_wins(session_playwright, monkeypatch):
    monkeypatch.setattr(bc, "CAMOUFOX_PROXY_SERVER", "http://10.8.8.1:8088")
    monkeypatch.setattr(bc, "CAMOUFOX_TIMEZONE", "Asia/Tokyo")
    called = {"n": 0}

    def fake_resolve():
        called["n"] += 1
        return "Asia/Hong_Kong"

    monkeypatch.setattr(bc, "_resolve_timezone_sync", fake_resolve)
    asyncio.run(bc._new_page())
    assert session_playwright["context_kwargs"][-1] == {"timezone_id": "Asia/Tokyo"}
    assert called["n"] == 0  # explicit override: no lookup at all


def test_context_timezone_skipped_without_proxy(session_playwright, monkeypatch):
    monkeypatch.setattr(bc, "CAMOUFOX_PROXY_SERVER", "")
    monkeypatch.setattr(bc, "CAMOUFOX_TIMEZONE", "")
    asyncio.run(bc._new_page())
    assert session_playwright["context_kwargs"][-1] == {}


# ---------------------------------------------------------------------------
#  SERP circuit breaker (per-engine empty-results skip)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_serp_breaker():
    bc._serp_breaker.clear()
    yield
    bc._serp_breaker.clear()


def test_serp_breaker_opens_after_threshold():
    bc._serp_breaker_record("google", False)
    assert not bc._serp_breaker_skip("google")  # one failure: still tried
    bc._serp_breaker_record("google", False)
    assert bc._serp_breaker_skip("google")  # threshold reached: skipped


def test_serp_breaker_resets_on_success():
    bc._serp_breaker_record("google", False)
    bc._serp_breaker_record("google", False)
    bc._serp_breaker_record("google", True)
    assert not bc._serp_breaker_skip("google")


def test_serp_breaker_cooldown_expires(monkeypatch):
    bc._serp_breaker_record("google", False)
    bc._serp_breaker_record("google", False)
    assert bc._serp_breaker_skip("google")
    # Cooldowns use time.monotonic(); fake the clock past the expiry.
    real_monotonic = bc.time.monotonic
    monkeypatch.setattr(bc.time, "monotonic", lambda: real_monotonic() + bc.SERP_BREAKER_COOLDOWN + 1)
    assert not bc._serp_breaker_skip("google")


def test_serp_breaker_tracks_engines_independently():
    bc._serp_breaker_record("google", False)
    bc._serp_breaker_record("google", False)
    assert bc._serp_breaker_skip("google")
    assert not bc._serp_breaker_skip("duckduckgo")


def test_search_web_rejects_unknown_engine():
    with pytest.raises(ValueError, match="Unknown browser search engine"):
        asyncio.run(bc.search_web("q", engines=("ask jeeves",)))
