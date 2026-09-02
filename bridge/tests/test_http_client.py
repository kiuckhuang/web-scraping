"""Unit tests for the HTTP fast path (curl_cffi) — network-free.

The curl_cffi request seam (`http_client._request_raw`) is monkeypatched;
SSRF DNS is faked with socket.getaddrinfo like the other test modules.
"""

from __future__ import annotations

import asyncio
import socket

import bridge.http_client as hc
import pytest
from bridge.http_client import Escalation

from bridge import ssrf


@pytest.fixture(autouse=True)
def _clear_dns_cache():
    """DNS verdicts must not leak between tests (same policy as test_main)."""
    ssrf.clear_dns_cache()
    yield
    ssrf.clear_dns_cache()


def _public_getaddrinfo(host, port=None, family=0, type=0, proto=0, flags=0):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


def _mixed_getaddrinfo(host, port=None, family=0, type=0, proto=0, flags=0):
    """192.x hosts resolve privately, everything else publicly."""
    if host.startswith("192.") or host == "localhost":
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 0))]
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


_STATIC_PAGE = """<html><head><title>Static Page</title></head><body>
<nav>menu noise</nav><script>var tracking = 1;</script><style>.a{}</style>
<main><h1>Hello</h1>
<p>World of static content served without any browser at all.</p>
<p>This paragraph carries enough text that the page is clearly not a
JavaScript-rendered app shell, so the fast path should serve it directly
instead of escalating to Camoufox for rendering.</p>
<table><tr><th>k</th><td>v</td></tr></table></main>
</body></html>"""


def _serve(monkeypatch, responses):
    """Script the curl_cffi seam; returns the list of requested URLs."""
    calls: list[str] = []
    queue = iter(responses)

    def fake_raw(url):
        calls.append(url)
        return next(queue)

    monkeypatch.setattr(hc, "_request_raw", fake_raw)
    return calls


# ---------------------------------------------------------------------------
#  Conversion (extract / fetch parity with the browser shapes)
# ---------------------------------------------------------------------------

def test_extract_mode_serves_markdown(monkeypatch):
    _serve(monkeypatch, [(200, "https://example.com/a", {}, _STATIC_PAGE)])
    out = asyncio.run(hc.scrape("https://example.com/a", mode="extract"))
    assert out["title"] == "Static Page"
    assert "Hello" in out["markdown"] and "World" in out["markdown"]
    assert "menu noise" not in out["markdown"]  # nav removed
    assert "tracking" not in out["markdown"]  # scripts removed
    assert out["tables"] == [{"rows": [["k", "v"]]}]
    assert out["status"] == 200


def test_fetch_mode_returns_raw_html_and_text(monkeypatch):
    _serve(monkeypatch, [(200, "https://example.com/a", {}, _STATIC_PAGE)])
    out = asyncio.run(hc.scrape("https://example.com/a", mode="fetch"))
    assert out["html"] == _STATIC_PAGE
    assert "Hello" in out["text"]
    assert "tracking" not in out["text"]  # script text excluded


# ---------------------------------------------------------------------------
#  Escalation triggers — anything the browser must handle
# ---------------------------------------------------------------------------

def test_waf_challenge_escalates(monkeypatch):
    body = "<html><head><title>Just a moment...</title></head><body>cf-chl-widget</body></html>"
    _serve(monkeypatch, [(200, "https://example.com/", {}, body)])
    with pytest.raises(Escalation):
        asyncio.run(hc.scrape("https://example.com/", mode="extract"))


def test_http_error_escalates(monkeypatch):
    _serve(monkeypatch, [(403, "https://example.com/", {}, "forbidden")])
    with pytest.raises(Escalation):
        asyncio.run(hc.scrape("https://example.com/", mode="extract"))


def test_js_app_shell_escalates(monkeypatch):
    body = (
        "<html><head><title>App</title></head><body><div id='root'></div>"
        + "<script>hydrate()</script>" * 5
        + "</body></html>"
    )
    _serve(monkeypatch, [(200, "https://example.com/", {}, body)])
    with pytest.raises(Escalation):
        asyncio.run(hc.scrape("https://example.com/", mode="extract"))


def test_static_page_with_scripts_is_not_escalated(monkeypatch):
    """A real static page that merely embeds a script tag must be served."""
    text = "Meaningful content " * 20
    body = f"<html><head><title>Ok</title></head><body><main><p>{text}</p></main><script>x()</script></body></html>"
    _serve(monkeypatch, [(200, "https://example.com/", {}, body)])
    out = asyncio.run(hc.scrape("https://example.com/", mode="extract"))
    assert "Meaningful content" in out["markdown"]


# ---------------------------------------------------------------------------
#  Redirect handling — follow public hops, block everything else
# ---------------------------------------------------------------------------

def test_public_redirect_is_followed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    calls = _serve(
        monkeypatch,
        [
            (301, "https://example.com/a", {"location": "https://example.com/b"}, ""),
            (200, "https://example.com/b", {}, _STATIC_PAGE),
        ],
    )
    out = asyncio.run(hc.scrape("https://example.com/a", mode="extract"))
    assert out["url"] == "https://example.com/b"
    assert calls == ["https://example.com/a", "https://example.com/b"]


def test_relative_redirect_is_resolved(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    calls = _serve(
        monkeypatch,
        [
            (302, "https://example.com/a", {"location": "/b?x=1"}, ""),
            (200, "https://example.com/b?x=1", {}, _STATIC_PAGE),
        ],
    )
    out = asyncio.run(hc.scrape("https://example.com/a", mode="extract"))
    assert out["url"] == "https://example.com/b?x=1"
    assert calls[-1] == "https://example.com/b?x=1"


def test_redirect_to_private_host_is_blocked(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mixed_getaddrinfo)
    calls = _serve(
        monkeypatch,
        [(302, "https://example.com/a", {"location": "http://192.168.1.1/admin"}, "")],
    )
    with pytest.raises(Escalation):
        asyncio.run(hc.scrape("https://example.com/a", mode="extract"))
    assert calls == ["https://example.com/a"]  # the private hop was never requested


def test_private_initial_url_never_reaches_the_wire(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _mixed_getaddrinfo)

    def fail_raw(url):
        raise AssertionError(f"must not request a private URL: {url}")

    monkeypatch.setattr(hc, "_request_raw", fail_raw)
    with pytest.raises(Escalation):
        asyncio.run(hc.scrape("http://192.168.1.1/admin", mode="extract"))


def test_unresolvable_redirect_host_is_blocked(monkeypatch):
    def invalid_fails_getaddrinfo(host, *args, **kwargs):
        if host.endswith(".invalid"):
            raise OSError("reserved TLD never resolves")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", invalid_fails_getaddrinfo)
    calls = _serve(
        monkeypatch,
        [(302, "https://example.com/a", {"location": "http://definitely-not-a-real-host.invalid/"}, "")],
    )
    with pytest.raises(Escalation):
        asyncio.run(hc.scrape("https://example.com/a", mode="extract"))
    assert calls == ["https://example.com/a"]  # the unresolvable hop was never requested


def test_redirect_loop_is_bounded(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _public_getaddrinfo)
    calls = _serve(
        monkeypatch,
        [(301, f"https://example.com/hop{i}", {"location": f"https://example.com/hop{i + 1}"}, "")
         for i in range(hc.MAX_REDIRECTS + 2)],
    )
    with pytest.raises(Escalation):
        asyncio.run(hc.scrape("https://example.com/hop0", mode="extract"))
    assert len(calls) == hc.MAX_REDIRECTS + 1
