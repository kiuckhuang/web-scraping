"""HTTP fast path — shaped-TLS fetching via curl_cffi (no browser).

Most pages are static: starting a Camoufox context for them is slow and
wasteful. This module fetches such pages directly with curl_cffi, whose TLS
stack impersonates a real Chrome handshake (JA3/JA4 + HTTP/2 fingerprint),
and converts the HTML into the same result shapes the browser path returns
(`extract` → markdown + tables, `fetch` → raw HTML + text).

Anything that smells like a WAF challenge, a JS-rendered app shell, or an
HTTP error raises `Escalation` — the caller retries through the stealth
browser, which stays the authority for JS-heavy and bot-walled targets.

SSRF: every hop (the initial URL and each redirect target) is validated
through the shared guard — private/unresolvable hosts are rejected, never
followed (mirrors the browser's `_guard_request`). Requests are issued with
redirects disabled so no hop can bypass the check. Runs entirely in worker
threads (asyncio.to_thread at the call site), like the DNS checks.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests
from markdownify import markdownify

from . import ssrf
from .browser_client import WAF_MARKERS

logger = logging.getLogger(__name__)

HTTP_FASTPATH_TIMEOUT = float(os.environ.get("HTTP_FASTPATH_TIMEOUT", "15"))
# curl_cffi impersonation target: selects the bundled Chrome TLS/HTTP2
# fingerprint shape. "chrome" tracks the newest Chrome the installed
# curl_cffi release ships fingerprints for.
HTTP_FASTPATH_IMPERSONATE = os.environ.get("HTTP_FASTPATH_IMPERSONATE", "chrome").strip()
# Optional egress proxy for the HTTP path (empty = direct). Deliberately
# separate from CAMOUFOX_PROXY_* so both transports can be pointed at the
# same exit from compose without coupling their configs.
HTTP_FASTPATH_PROXY = os.environ.get("HTTP_FASTPATH_PROXY", "").strip()

MAX_REDIRECTS = 5
# A 200 page whose extracted text is tiny while the HTML is script-heavy is a
# JS app shell (React/Next.js): the browser must render it instead.
JS_SHELL_MIN_TEXT = 200
MAX_MARKDOWN_CHARS = 50_000


class Escalation(Exception):
    """The page needs the stealth browser (challenge / JS shell / error)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
#  Request seam (monkeypatched in tests) + SSRF-guarded redirect loop
# ---------------------------------------------------------------------------

def _request_raw(url: str) -> tuple[int, str, dict[str, str], str]:
    """One no-redirect GET through curl_cffi with the configured shape.

    Returns (status, final_url, headers, body). This is the only place that
    touches the network; tests monkeypatch it.
    """
    with curl_requests.Session(
        impersonate=HTTP_FASTPATH_IMPERSONATE,
        proxy=HTTP_FASTPATH_PROXY or None,
        timeout=HTTP_FASTPATH_TIMEOUT,
    ) as session:
        resp = session.get(url, allow_redirects=False)
        return resp.status_code, str(resp.url), dict(resp.headers), resp.text


def _ensure_public(url: str, what: str) -> None:
    """Reject non-public hops before any request is issued (SSRF guard)."""
    try:
        ssrf.validate_public_url(url)
    except ssrf.UrlBlockedError as exc:
        raise Escalation(f"{what} blocked by SSRF guard: {exc.detail}") from exc


def _fetch_public_sync(url: str, *, mode: str) -> dict[str, Any]:
    """Fetch a public URL, following redirects hop-by-hop (each validated).

    Raises Escalation when the browser must take over: WAF challenge,
    JS-rendered shell, HTTP >= 400, blocked hop, or too many redirects.
    """
    current = url
    for hop in range(MAX_REDIRECTS + 1):
        _ensure_public(current, "requested URL" if hop == 0 else "redirect target")
        status, final_url, headers, body = _request_raw(current)
        if 300 <= status < 400:
            location = headers.get("location", "")
            if location and hop < MAX_REDIRECTS:
                current = urljoin(current, location)
                continue
            if location:
                raise Escalation(f"redirect limit ({MAX_REDIRECTS}) exceeded at {current}")
            # 3xx without a Location header — serve whatever body came with it.
        if status >= 400:
            raise Escalation(f"HTTP {status} — letting the browser retry")
        return _build_result(final_url, body, mode, status)
    raise Escalation(f"redirect limit ({MAX_REDIRECTS}) exceeded at {current}")


async def scrape(url: str, *, mode: str = "extract") -> dict[str, Any]:
    """HTTP fast-path scrape; raises Escalation when the browser is needed."""
    return await asyncio.to_thread(_fetch_public_sync, url, mode=mode)


# ---------------------------------------------------------------------------
#  HTML → result conversion (mirrors browser_client's extract/fetch shapes)
# ---------------------------------------------------------------------------

def _looks_like_challenge(title: str, html: str) -> bool:
    """Cheap WAF detection: markers in the title or the first 4 KB of HTML."""
    if any(marker in (title or "").lower() for marker in WAF_MARKERS):
        return True
    head = html[:4096].lower()
    return any(marker in head for marker in WAF_MARKERS)


def _collapse_blank_lines(md: str) -> str:
    """Collapse excessive blank lines (mirror of browser_client's post-processing)."""
    result: list[str] = []
    prev_blank = False
    for line in (ln.rstrip() for ln in md.split("\n")):
        if not line.strip():
            if not prev_blank:
                result.append("")
            prev_blank = True
        else:
            result.append(line)
            prev_blank = False
    return "\n".join(result)


# Same noise removal + main-content selection as the browser path's
# in-page JS (browser_client._extract_markdown), expressed in BeautifulSoup.
_NOISE_SELECTORS = (
    "script, style, noscript, iframe, svg, nav, footer, header, aside",
    '[role="navigation"], [role="banner"], [role="contentinfo"]',
    ".ad, .ads, .advertisement, .sidebar, .menu, .cookie",
)


def _soup_from(body: str) -> BeautifulSoup:
    soup = BeautifulSoup(body, "html.parser")
    for selector in _NOISE_SELECTORS:
        for element in soup.select(selector):
            element.decompose()
    return soup


def _title_of(soup: BeautifulSoup) -> str:
    return soup.title.get_text(strip=True) if soup.title else ""


def _extract_tables(soup: BeautifulSoup) -> list[dict[str, Any]]:
    """Extract up to 20 tables as {"rows": [[cell, ...], ...]} (browser parity)."""
    try:
        tables = []
        for table in soup.find_all("table")[:20]:
            rows = [
                [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
                for row in table.find_all("tr")
            ]
            tables.append({"rows": rows})
        return tables
    except Exception:
        return []


def _build_result(url: str, body: str, mode: str, status: int) -> dict[str, Any]:
    soup = _soup_from(body)
    title = _title_of(soup)
    if _looks_like_challenge(title, body):
        raise Escalation("WAF challenge detected — letting the browser verify")
    if mode == "fetch":
        return {
            "url": url,
            "title": title,
            "text": soup.get_text("\n"),
            "html": body,
            "status": status,
        }
    main = soup.select_one("main, article, [role='main'], #content, .content") or soup.body or soup
    md = markdownify(f"<body>{main.decode_contents()}</body>", heading_style="ATX", bullets="-")
    markdown = _collapse_blank_lines(md).strip()[:MAX_MARKDOWN_CHARS]
    # Script presence is checked on the *raw* body — _soup_from already
    # decomposed them, and the heuristic is about the served HTML's shape.
    if len(markdown) < JS_SHELL_MIN_TEXT and "<script" in body.lower():
        raise Escalation("JS-rendered app shell (no static content) — browser must render")
    return {
        "url": url,
        "title": title,
        "markdown": markdown,
        "tables": _extract_tables(soup),
        "status": status,
    }
