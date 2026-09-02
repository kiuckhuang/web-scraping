"""Browser client — stealth scraping via Camoufox over Playwright.

Connects to the ws-camoufox container's Playwright websocket endpoint
(ws://camoufox:9222/browser, firefox.connect) and provides high-level
operations on the engine-agnostic Playwright API:

  - fetch_page:    get raw HTML + title + text of any URL
  - extract_page:  get clean markdown + tables from any URL
  - scrape:        convenience wrapper that tries extract first, falls back to fetch
  - crawl_site:    BFS crawl of a whole site
  - search_web:    web search through the stealth browser
  - session helpers: named long-lived contexts for login persistence
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import urllib.request
from collections import deque
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from markdownify import markdownify
from playwright.async_api import Browser, Error as PlaywrightError, async_playwright

from . import ssrf

logger = logging.getLogger(__name__)

ENGINE = "camoufox"
CAMOUFOX_WS_URL = os.environ.get("CAMOUFOX_WS_URL", "ws://camoufox:9222/browser")
# Proxy settings (bridge-side copy of the ws-camoufox container's env) — used
# only to derive the egress timezone for context creation. Camoufox's server
# mode applies fingerprint geo data browser-wide (WebRTC IP via prefs) but
# cannot set per-context timezone/locale for remotely created contexts, so the
# bridge derives them the same way camoufox upstream does: ip-api.com through
# the proxy, resolved once and cached.
CAMOUFOX_PROXY_SERVER = os.environ.get("CAMOUFOX_PROXY_SERVER", "").strip()
CAMOUFOX_PROXY_USERNAME = os.environ.get("CAMOUFOX_PROXY_USERNAME", "").strip()
CAMOUFOX_PROXY_PASSWORD = os.environ.get("CAMOUFOX_PROXY_PASSWORD", "").strip()
CAMOUFOX_TIMEZONE = os.environ.get("CAMOUFOX_TIMEZONE", "").strip()
SCRAPE_TIMEOUT = float(os.environ.get("CAMOUFOX_TIMEOUT", "60"))
NAV_WAIT = os.environ.get("CAMOUFOX_NAV_WAIT", "domcontentloaded")
NAV_DELAY = float(os.environ.get("CAMOUFOX_NAV_DELAY", "400"))
MAX_CONCURRENT_PAGES = int(os.environ.get("CAMOUFOX_MAX_CONCURRENT_PAGES", "3"))
WAF_WAIT = float(os.environ.get("CAMOUFOX_WAF_WAIT", "15"))
ISOLATE_CONTEXTS = os.environ.get("CAMOUFOX_ISOLATE_CONTEXTS", "true").lower() not in {"0", "false", "no"}
# Max concurrent named sessions (login persistence). Each session is a live
# BrowserContext on the remote browser — bounded so a broken client cannot
# exhaust the container's memory.
MAX_SESSIONS = int(os.environ.get("CAMOUFOX_MAX_SESSIONS", "16"))
# Wall-clock budget for a single /crawl call (seconds, 0 = unlimited). Without
# it a 200-page crawl against slow/challenging targets can run for hours while
# the MCP client has long timed out.
CRAWL_MAX_SECONDS = float(os.environ.get("CRAWL_MAX_SECONDS", "1800"))
# Playwright's remote protocol enforces client/server minor-version parity
# (the server answers 428 on mismatch); the camoufox image pins playwright
# 1.60.x (camoufox 0.5.5 requires <1.61) — keep bridge/pyproject.toml on the
# same line and bump both pins together (see AGENTS.md).
WAF_MARKERS = (
    "just a moment",
    "performing security verification",
    "checking your browser",
    "cf-chl-",
    "challenge-platform",
)

_browser: Browser | None = None
_playwright_ctx: Any = None
_lock = asyncio.Lock()
_page_slots = asyncio.Semaphore(MAX_CONCURRENT_PAGES)

# Named sessions: name -> long-lived BrowserContext. Cookies/localStorage in a
# session survive across scrape calls and die with the browser connection
# (Playwright's launchServer cannot serve a persistent profile).
_sessions: dict[str, Any] = {}
_sessions_lock = asyncio.Lock()


class SessionLimitError(RuntimeError):
    """Raised when creating a session would exceed MAX_SESSIONS."""


async def _reset_browser() -> None:
    """Drop a broken connection so the next request reconnects cleanly."""
    global _browser, _playwright_ctx
    browser, playwright_ctx = _browser, _playwright_ctx
    _browser = None
    _playwright_ctx = None
    # Contexts die with the browser connection — drop the session registry so
    # callers don't hand out dead contexts after a reconnect.
    _sessions.clear()
    if browser is not None:
        try:
            await browser.close()
        except Exception:
            pass
    if playwright_ctx is not None:
        try:
            await playwright_ctx.stop()
        except Exception:
            pass


async def _get_browser() -> Browser:
    """Lazily connect to the Camoufox websocket endpoint and reuse the connection."""
    global _browser, _playwright_ctx
    if _browser is None or not _browser.is_connected():
        async with _lock:
            if _browser is None or not _browser.is_connected():
                logger.info("Connecting to Camoufox at %s", CAMOUFOX_WS_URL)
                try:
                    _playwright_ctx = await async_playwright().start()
                    _browser = await _playwright_ctx.firefox.connect(CAMOUFOX_WS_URL)
                    logger.info("Connected to Camoufox")
                except Exception:
                    await _reset_browser()
                    raise
    return _browser


async def _new_page(session: str | None = None):
    """Get a page with browser-side SSRF filtering.

    With `session`, the page lives in the named long-lived context (cookies
    and localStorage persist across calls); otherwise each page gets an
    isolated context (or the shared default one when isolation is disabled).
    """
    browser = await _get_browser()
    tz = await _context_timezone()
    context_kwargs = {"timezone_id": tz} if tz else {}
    if session is not None:
        context = await get_session(session, **context_kwargs)
    elif ISOLATE_CONTEXTS:
        context = await browser.new_context(**context_kwargs)
    else:
        context = browser.contexts[0] if browser.contexts else await browser.new_context(**context_kwargs)
    page = await context.new_page()
    await page.route("**/*", _guard_request)
    return page


async def get_session(name: str, **context_kwargs):
    """Return the named long-lived context, creating it on first use."""
    if name in _sessions:
        return _sessions[name]
    async with _sessions_lock:
        if name in _sessions:
            return _sessions[name]
        if len(_sessions) >= MAX_SESSIONS:
            raise SessionLimitError(
                f"Session limit reached ({MAX_SESSIONS}); delete a session first (DELETE /sessions/{{name}})"
            )
        browser = await _get_browser()
        context = await browser.new_context(**context_kwargs)
        _sessions[name] = context
        logger.info("Created browser session %r (%d/%d in use)", name, len(_sessions), MAX_SESSIONS)
        return context


async def create_session(name: str) -> bool:
    """Create the named long-lived context; returns False if it already existed."""
    existed = name in _sessions
    await get_session(name)
    return not existed


async def close_session(name: str) -> bool:
    """Close and forget a named session; returns False if it didn't exist."""
    async with _sessions_lock:
        context = _sessions.pop(name, None)
    if context is None:
        return False
    try:
        await context.close()
    except Exception:
        pass
    logger.info("Closed browser session %r (%d/%d in use)", name, len(_sessions), MAX_SESSIONS)
    return True


def list_sessions() -> list[str]:
    """Names of the live sessions."""
    return sorted(_sessions)


# ---------------------------------------------------------------------------
#  Egress timezone derivation (fingerprint consistency behind a proxy)
# ---------------------------------------------------------------------------

_timezone_id: str | None = None
_timezone_resolved = False


def _proxy_url() -> str | None:
    """Proxy URL with credentials embedded, for urllib's ProxyHandler."""
    if not CAMOUFOX_PROXY_SERVER:
        return None
    if CAMOUFOX_PROXY_USERNAME and CAMOUFOX_PROXY_PASSWORD:
        parsed = urlparse(CAMOUFOX_PROXY_SERVER)
        netloc = f"{CAMOUFOX_PROXY_USERNAME}:{CAMOUFOX_PROXY_PASSWORD}@{parsed.netloc}"
        return parsed._replace(netloc=netloc).geturl()
    return CAMOUFOX_PROXY_SERVER


def _resolve_timezone_sync() -> str | None:
    """Query ip-api.com THROUGH the proxy for the exit IP's timezone."""
    import json

    proxy_url = _proxy_url()
    handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url} if proxy_url else None)
    opener = urllib.request.build_opener(handler)
    with opener.open("http://ip-api.com/json?fields=query,timezone", timeout=10) as resp:
        data = json.load(resp)
    return data.get("timezone") or None


async def _context_timezone() -> str | None:
    """Timezone for new contexts: CAMOUFOX_TIMEZONE override, else the proxy
    exit's timezone (resolved once through the proxy), else None (UTC)."""
    global _timezone_id, _timezone_resolved
    if _timezone_resolved:
        return _timezone_id
    if CAMOUFOX_TIMEZONE:
        _timezone_id, _timezone_resolved = CAMOUFOX_TIMEZONE, True
        logger.info("Context timezone override: %s", _timezone_id)
        return _timezone_id
    if not CAMOUFOX_PROXY_SERVER:
        _timezone_resolved = True
        return None
    try:
        _timezone_id = await asyncio.to_thread(_resolve_timezone_sync)
        logger.info("Proxy egress timezone resolved: %s", _timezone_id)
    except Exception as exc:
        logger.warning(
            "Could not resolve the proxy egress timezone (%s) — contexts will use the browser default", exc
        )
        _timezone_id = None
    _timezone_resolved = True
    return _timezone_id


def _is_public_http_url(url: str) -> bool:
    """Reject browser requests to local, private, or otherwise special hosts.

    Delegates to the shared SSRF guard (same policy as the bridge edge, plus
    inert browser schemes); DNS verdicts are cached there per host so a
    JS-heavy page's subresources don't hammer the resolver.
    """
    return ssrf.is_public_http_url(url)


async def _guard_request(route, request) -> None:
    """Abort private redirects and subresource requests inside the browser."""
    url = request.url
    if await asyncio.to_thread(_is_public_http_url, url):
        await route.continue_()
    else:
        logger.warning("Blocked browser request to non-public URL: %s", url)
        await route.abort("blockedbyclient")


async def _close_page(page) -> None:
    """Close a page, keeping named-session and shared contexts alive."""
    context = page.context
    try:
        await page.close()
    finally:
        if context is None or context in _sessions.values():
            return  # named session context — it must survive the request
        if ISOLATE_CONTEXTS:
            await context.close()


def _same_origin(left: str, right: str) -> bool:
    """Compare scheme, hostname, and effective port."""
    a, b = urlparse(left), urlparse(right)
    try:
        a_port = a.port or (443 if a.scheme == "https" else 80)
        b_port = b.port or (443 if b.scheme == "https" else 80)
    except ValueError:
        return False
    return (a.scheme.lower(), a.hostname.lower() if a.hostname else "", a_port) == (
        b.scheme.lower(), b.hostname.lower() if b.hostname else "", b_port
    )


async def _is_waf_challenge(page) -> bool:
    """Detect common WAF challenge pages cheaply.

    Avoids reading the full page body (innerText on large pages is expensive
    both to compute and to serialize over CDP): checks the title, known
    challenge selectors, and at most the first 4 KB of text.
    """
    try:
        title = (await page.title()).lower()
        if any(marker in title for marker in WAF_MARKERS):
            return True
        return bool(await page.evaluate("""(markers) => {
            const challengeSelectors = [
                '#challenge-running', '#challenge-form', '#cf-chl-widget',
                'iframe[src*="challenges.cloudflare.com"]',
                '[id*="cf-chl-"]', '[class*="cf-chl-"]',
                'div[data-translate*="checking your browser"]',
                '.g-recaptcha', 'form[action*="recaptcha"]',
            ];
            if (document.querySelector(challengeSelectors.join(','))) return true;
            const body = document.body;
            if (!body) return false;
            const text = body.textContent.slice(0, 4000).toLowerCase();
            return markers.some(m => text.includes(m));
        }""", list(WAF_MARKERS)))
    except PlaywrightError:
        return False


async def _wait_for_waf(page) -> None:
    """Give a browser challenge time to complete, then wait for navigation."""
    if not await _is_waf_challenge(page):
        return
    logger.info("WAF challenge detected at %s; waiting for browser verification", page.url)
    deadline = asyncio.get_running_loop().time() + WAF_WAIT
    while asyncio.get_running_loop().time() < deadline:
        await page.wait_for_timeout(1000)
        if not await _is_waf_challenge(page):
            logger.info("WAF challenge cleared at %s", page.url)
            return


async def fetch_page(url: str, session: str | None = None) -> dict[str, Any]:
    """Fetch a URL through the stealth browser.

    Args:
        url:      Target URL.
        session:  Optional named session (login persistence across calls).

    Returns:
        dict with keys: url, title, text, html, status
    """
    page = await _new_page(session)
    try:
        response = await page.goto(url, wait_until=NAV_WAIT, timeout=int(SCRAPE_TIMEOUT * 1000))
        await _wait_for_waf(page)

        title = await page.title()
        text = await page.inner_text("body")
        html = await page.content()
        # None when no navigation response is available (e.g. a client-side
        # redirect chain) — never fabricate a 200, callers filter on status.
        status = response.status if response else None

        result = {
            "url": page.url,
            "title": title,
            "text": text,
            "html": html,
            "status": status,
        }
        if await _is_waf_challenge(page):
            result["waf_challenge"] = True
        return result
    finally:
        await _close_page(page)


async def extract_page(url: str, session: str | None = None) -> dict[str, Any]:
    """Extract clean markdown + tables from a URL.

    Uses a readability-style extraction: removes nav, footer, script, style,
    and converts the main content to markdown.

    Args:
        url:      Target URL.
        session:  Optional named session (login persistence across calls).

    Returns:
        dict with keys: url, title, markdown, tables
    """
    page = await _new_page(session)
    try:
        await page.goto(url, wait_until=NAV_WAIT, timeout=int(SCRAPE_TIMEOUT * 1000))
        await _wait_for_waf(page)
        # domcontentloaded alone can race SPA hydration — give JS pages the
        # configured settle pause before extraction (same as crawl/SERP paths).
        if NAV_DELAY > 0:
            await page.wait_for_timeout(int(NAV_DELAY))

        title = await page.title()

        # Extract tables
        tables = await _extract_tables(page)

        # Extract main content as markdown
        markdown = await _extract_markdown(page)

        result = {
            "url": page.url,
            "title": title,
            "markdown": markdown,
            "tables": tables,
        }
        if await _is_waf_challenge(page):
            result["waf_challenge"] = True
        return result
    finally:
        await _close_page(page)


async def _extract_tables(page) -> list[dict[str, Any]]:
    """Extract all tables from the page as list-of-lists."""
    try:
        tables = await page.evaluate("""
            () => {
                const tables = [...document.querySelectorAll('table')];
                return tables.map(table => {
                    const rows = [...table.querySelectorAll('tr')];
                    return rows.map(row =>
                        [...row.querySelectorAll('th,td')].map(cell => cell.innerText.trim())
                    );
                });
            }
        """)
        return [{"rows": t} for t in tables[:20]]
    except Exception:
        return []


async def _extract_markdown(page) -> str:
    """Extract the main content as clean markdown (headings, links, lists, code)."""
    try:
        # Remove noise elements
        await page.evaluate("""
            () => {
                const remove = (sel) => document.querySelectorAll(sel)
                    .forEach(el => el.remove());
                remove('script, style, noscript, iframe, svg, nav, footer, header, aside');
                remove('[role="navigation"], [role="banner"], [role="contentinfo"]');
                remove('.ad, .ads, .advertisement, .sidebar, .menu, .cookie');
            }
        """)
    except Exception:
        pass

    try:
        # Grab the main content as HTML and convert it to real markdown
        html = await page.evaluate("""
            () => {
                const main = document.querySelector('main, article, [role="main"], #content, .content')
                    || document.body;
                return main ? main.innerHTML : document.body.innerHTML;
            }
        """)
        md = markdownify(f"<body>{html}</body>", heading_style="ATX", bullets="-")
        # Collapse excessive blank lines
        lines = [line.rstrip() for line in md.split("\n")]
        result = []
        prev_blank = False
        for line in lines:
            if not line.strip():
                if not prev_blank:
                    result.append("")
                prev_blank = True
            else:
                result.append(line)
                prev_blank = False
        return "\n".join(result).strip()[:50000]
    except Exception:
        # Fall back to plain text if conversion fails
        return await page.inner_text("body")


async def scrape(url: str, *, mode: str = "extract", session: str | None = None) -> dict[str, Any]:
    """Scrape a URL: extract clean markdown by default, or fetch raw HTML+text.

    Args:
        url:      Target URL.
        mode:     "extract" for clean markdown (default), "fetch" for raw HTML+text.
        session:  Optional named session (login persistence across calls).

    Returns:
        Extraction result (markdown + tables) or fetch result (html + text).
    """
    async with _page_slots:
        try:
            if mode == "fetch":
                return await fetch_page(url, session)
            return await extract_page(url, session)
        except PlaywrightError as exc:
            if _browser is None or not _browser.is_connected():
                await _reset_browser()
            logger.warning("Scrape failed for %s (mode=%s, session=%s): %s — falling back to fetch", url, mode, session or "-", exc)
            if mode != "fetch":
                try:
                    return await fetch_page(url, session)
                except Exception:
                    pass
            raise


async def crawl_site(url: str, depth: int = 2, max_pages: int = 50, session: str | None = None) -> dict[str, Any]:
    """Crawl a website BFS-style, returning pages and a sitemap.

    Args:
        url:       Root URL to crawl.
        depth:     Crawl depth (1 = just the given page).
        max_pages: Maximum pages to collect.
        session:   Optional named session (crawl behind a logged-in profile).

    Returns:
        dict with keys: pages[], sitemap[]
    """
    visited: set[str] = set()
    queued: set[str] = {url}
    pages: list[dict] = []
    sitemap: list[str] = []
    queue: deque[tuple[str, int]] = deque([(url, 0)])
    deadline = (
        asyncio.get_running_loop().time() + CRAWL_MAX_SECONDS if CRAWL_MAX_SECONDS > 0 else None
    )

    while queue and len(pages) < max_pages:
        if deadline is not None and asyncio.get_running_loop().time() >= deadline:
            logger.warning(
                "Crawl budget of %ss exhausted at %d/%d pages; returning partial results",
                CRAWL_MAX_SECONDS,
                len(pages),
                max_pages,
            )
            break
        current_url, current_depth = queue.popleft()

        if current_url in visited:
            continue
        visited.add(current_url)

        try:
            # Each page holds a slot so concurrent crawls/scrapes stay within
            # MAX_CONCURRENT_PAGES and cannot overwhelm the browser.
            async with _page_slots:
                page = await _new_page(session)
                try:
                    await page.goto(current_url, wait_until=NAV_WAIT, timeout=int(SCRAPE_TIMEOUT * 1000))
                    await page.wait_for_timeout(int(NAV_DELAY))

                    title = await page.title()
                    text = await page.inner_text("body")
                    pages.append({
                        "url": page.url,
                        "title": title,
                        "text": text[:2000],
                        "depth": current_depth,
                    })
                    sitemap.append(page.url)
                    logger.info("Crawled [%d/%d] %s", len(pages), max_pages, current_url)

                    if current_depth < depth - 1:
                        links = await page.evaluate("""
                            () => [...document.querySelectorAll('a[href]')]
                                .map(a => a.href)
                                .filter(href => href.startsWith('http'))
                        """)
                        # Filter in one worker-thread batch — same-origin plus
                        # shared-guard DNS checks for every link would otherwise
                        # run (blocking) on the event loop.
                        public_links = await asyncio.to_thread(
                            lambda: [
                                link
                                for link in links
                                if link not in queued and _same_origin(url, link) and _is_public_http_url(link)
                            ]
                        )
                        for link in public_links:
                            queued.add(link)
                            queue.append((link, current_depth + 1))
                finally:
                    await _close_page(page)
        except Exception as exc:
            logger.warning("Crawl failed for %s: %s", current_url, exc)

    return {"pages": pages, "sitemap": sitemap, "count": len(pages)}


# Per-engine circuit breaker for browser SERPs: after this many consecutive
# empty results an engine is skipped for the cooldown (Google serves its
# "enable JavaScript" shell to challenged clients — retrying on every query
# would just add its wait time to every search).
SERP_BREAKER_THRESHOLD = 2
SERP_BREAKER_COOLDOWN = 600.0
_serp_breaker: dict[str, tuple[int, float]] = {}  # engine -> (consecutive fails, skip-until monotonic)


def _serp_breaker_skip(engine: str) -> bool:
    """True if the engine recently produced empty SERPs too often."""
    _, skip_until = _serp_breaker.get(engine, (0, 0.0))
    return time.monotonic() < skip_until


def _serp_breaker_record(engine: str, ok: bool) -> None:
    fails, _ = _serp_breaker.get(engine, (0, 0.0))
    if ok:
        _serp_breaker.pop(engine, None)
        return
    fails += 1
    skip_until = time.monotonic() + SERP_BREAKER_COOLDOWN if fails >= SERP_BREAKER_THRESHOLD else 0.0
    _serp_breaker[engine] = (fails, skip_until)


async def search_web(query: str, count: int = 10, engines: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Web search through the stealth browser (real SERP, no API).

    Tries the given engines in order (default: DuckDuckGo only) and returns
    the first engine that yields results. Each engine gets a fresh page so
    one engine's challenge/consent page cannot poison the next attempt.
    Engines tripping the empty-results circuit breaker are skipped.

    Returns {"engine": <used>, "query": ..., "results": [{title,url,snippet}]}.
    On total failure the shape is kept, with the last engine's (empty) results
    and "engines_tried" listing what was attempted.
    """
    if engines is None:
        engines = ("duckduckgo",)
    parsers = {"google": _serp_google, "duckduckgo": _serp_ddg, "duckduckgo lite": _serp_ddg_lite}
    last: dict[str, Any] = {}
    tried: list[str] = []
    async with _page_slots:
        for engine in engines:
            parser = parsers.get(engine)
            if parser is None:
                raise ValueError(f"Unknown browser search engine: {engine}")
            if _serp_breaker_skip(engine):
                logger.info("Browser SERP %s skipped — circuit breaker open (recent empty results)", engine)
                continue
            tried.append(engine)
            page = await _new_page()
            try:
                results = await parser(page, query, count)
            except Exception as exc:
                logger.warning("Browser SERP %s failed for %r: %s", engine, query, exc)
                results = []
            finally:
                await _close_page(page)
            _serp_breaker_record(engine, bool(results))
            if results:
                return {"engine": engine, "query": query, "results": results[:count]}
            last = {"engine": engine, "query": query, "results": []}

        logger.warning(
            "Browser web search returned 0 results for %r across %s — "
            "SERP selectors may be stale or engines served challenge pages",
            query,
            tried,
        )
        return {**last, "engines_tried": tried}


async def _serp_google(page, query: str, count: int) -> list[dict[str, str]]:
    """Parse the Google SERP from a Camoufox page.

    Google renders results with JS after domcontentloaded (and serves an
    'enable JavaScript' retry shell to challenged clients), so wait for the
    first result heading to actually appear before parsing.
    """
    search_url = f"https://www.google.com/search?q={quote(query)}&num={count}&hl=en"
    await page.goto(search_url, wait_until=NAV_WAIT, timeout=int(SCRAPE_TIMEOUT * 1000))
    try:
        await page.wait_for_selector("a h3", timeout=8_000)
    except Exception:
        pass  # shell/challenge page — the evaluate below returns []
    await page.wait_for_timeout(int(NAV_DELAY))
    return await page.evaluate(
        """
        (count) => {
            const seen = new Set();
            const results = [];
            // h3 nodes inside result anchors; closest('a') carries the URL.
            for (const h3 of document.querySelectorAll('#search a h3, #rso a h3, a > h3')) {
                if (results.length >= count) break;
                const anchor = h3.closest('a');
                if (!anchor || !anchor.href) continue;
                const url = anchor.href;
                // Skip Google-internal links (search params, accounts, consent).
                if (!/^https?:\\/\\//.test(url) || url.includes('google.com/search')
                    || url.includes('google.com/url') || seen.has(url)) continue;
                seen.add(url);
                const block = h3.closest('div.g, div[data-hveid], li');
                const snippetEl = block && block.querySelector('div.VwiC3b, span.aCOpRe');
                results.push({
                    title: h3.innerText.trim(),
                    url,
                    snippet: snippetEl ? snippetEl.innerText.trim() : '',
                });
            }
            return results;
        }
        """,
        count,
    )


async def _serp_ddg(page, query: str, count: int) -> list[dict[str, str]]:
    """Parse the DuckDuckGo HTML SERP from a Camoufox page."""
    search_url = f"https://duckduckgo.com/html/?q={quote(query)}"
    await page.goto(search_url, wait_until=NAV_WAIT, timeout=int(SCRAPE_TIMEOUT * 1000))
    await page.wait_for_timeout(int(NAV_DELAY))
    return await page.evaluate(
        """
        (count) => {
            const results = [];
            const items = document.querySelectorAll(
                '.result, .web-result, .result--more, ' +
                '[data-testid="result"], article[data-testid="result"], li.result'
            );
            for (const item of items) {
                if (results.length >= count) break;
                const titleEl = item.querySelector('h2 a, .result__title a, .result__a');
                const snippetEl = item.querySelector('.result__snippet, .snippet');
                if (titleEl) {
                    results.push({
                        title: titleEl.innerText.trim(),
                        url: titleEl.href,
                        snippet: snippetEl ? snippetEl.innerText.trim() : '',
                    });
                }
            }
            return results;
        }
        """,
        count,
    )


async def _serp_ddg_lite(page, query: str, count: int) -> list[dict[str, str]]:
    """Parse the DuckDuckGo Lite SERP (lite.duckduckgo.com) — a minimal,
    JS-free table layout that is rarely challenged and trivial to parse."""
    search_url = f"https://lite.duckduckgo.com/lite/?q={quote(query)}"
    await page.goto(search_url, wait_until=NAV_WAIT, timeout=int(SCRAPE_TIMEOUT * 1000))
    await page.wait_for_timeout(int(NAV_DELAY))
    rows = await page.evaluate(
        """
        (count) => {
            const results = [];
            for (const a of document.querySelectorAll('a.result-link')) {
                if (results.length >= count) break;
                const tr = a.closest('tr');
                const snippetEl = tr && tr.nextElementSibling
                    && tr.nextElementSibling.querySelector('.result-snippet');
                results.push({
                    title: a.innerText.trim(),
                    url: a.href,
                    snippet: snippetEl ? snippetEl.innerText.trim() : '',
                });
            }
            return results;
        }
        """,
        count,
    )
    # Lite wraps some URLs in /l/?uddg=<encoded> redirects — unwrap them.
    return [{**r, "url": _unwrap_ddg_redirect(r["url"])} for r in rows]


def _unwrap_ddg_redirect(url: str) -> str:
    """Extract the real target from a DuckDuckGo /l/?uddg= redirect link."""
    if "duckduckgo.com/l/" not in url:
        return url
    target = parse_qs(urlparse(url).query).get("uddg", [None])[0]
    return target or url


async def health() -> bool:
    """Check whether the Camoufox Playwright server is reachable.

    The Playwright server has no HTTP endpoint, so health is a plain TCP
    connect to the websocket port.
    """
    try:
        return await asyncio.to_thread(_health_sync)
    except Exception:
        return False


def _health_sync() -> bool:
    """TCP health probe for the Camoufox Playwright websocket server."""
    parsed = urlparse(CAMOUFOX_WS_URL)
    host = parsed.hostname or "camoufox"
    port = parsed.port or 9222
    try:
        with socket.create_connection((host, port), timeout=5.0):
            return True
    except OSError:
        return False


async def shutdown() -> None:
    """Close the Playwright connection and all named sessions."""
    global _browser, _playwright_ctx
    _sessions.clear()
    if _browser is not None:
        try:
            await _browser.close()
        except Exception:
            pass
        _browser = None
    if _playwright_ctx is not None:
        try:
            await _playwright_ctx.stop()
        except Exception:
            pass
        _playwright_ctx = None
