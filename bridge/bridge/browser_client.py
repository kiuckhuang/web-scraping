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
from collections import deque
from typing import Any
from urllib.parse import quote, urlparse

from markdownify import markdownify
from playwright.async_api import Browser, Error as PlaywrightError, async_playwright

from . import ssrf

logger = logging.getLogger(__name__)

ENGINE = "camoufox"
CAMOUFOX_WS_URL = os.environ.get("CAMOUFOX_WS_URL", "ws://camoufox:9222/browser")
SCRAPE_TIMEOUT = float(os.environ.get("CAMOUFOX_TIMEOUT", "60"))
NAV_WAIT = os.environ.get("CAMOUFOX_NAV_WAIT", "domcontentloaded")
NAV_DELAY = float(os.environ.get("CAMOUFOX_NAV_DELAY", "400"))
MAX_CONCURRENT_PAGES = int(os.environ.get("CAMOUFOX_MAX_CONCURRENT_PAGES", "3"))
WAF_WAIT = float(os.environ.get("CAMOUFOX_WAF_WAIT", "15"))
ISOLATE_CONTEXTS = os.environ.get("CAMOUFOX_ISOLATE_CONTEXTS", "true").lower() not in {"0", "false", "no"}
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


async def _reset_browser() -> None:
    """Drop a broken connection so the next request reconnects cleanly."""
    global _browser, _playwright_ctx
    browser, playwright_ctx = _browser, _playwright_ctx
    _browser = None
    _playwright_ctx = None
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


async def _new_page():
    """Get a page with isolated storage and browser-side SSRF filtering."""
    browser = await _get_browser()
    if ISOLATE_CONTEXTS:
        context = await browser.new_context()
    else:
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
    page = await context.new_page()
    await page.route("**/*", _guard_request)
    return page


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
    """Close a page and its isolated context."""
    context = page.context
    try:
        await page.close()
    finally:
        if ISOLATE_CONTEXTS and context is not None:
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


async def fetch_page(url: str) -> dict[str, Any]:
    """Fetch a URL through the stealth browser.

    Returns:
        dict with keys: url, title, text, html, status
    """
    page = await _new_page()
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


async def extract_page(url: str) -> dict[str, Any]:
    """Extract clean markdown + tables from a URL.

    Uses a readability-style extraction: removes nav, footer, script, style,
    and converts the main content to markdown.

    Returns:
        dict with keys: url, title, markdown, tables
    """
    page = await _new_page()
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


async def scrape(url: str, *, mode: str = "extract") -> dict[str, Any]:
    """Scrape a URL: extract clean markdown by default, or fetch raw HTML+text.

    Args:
        url:   Target URL.
        mode:  "extract" for clean markdown (default), "fetch" for raw HTML+text.

    Returns:
        Extraction result (markdown + tables) or fetch result (html + text).
    """
    async with _page_slots:
        try:
            if mode == "fetch":
                return await fetch_page(url)
            return await extract_page(url)
        except PlaywrightError as exc:
            if _browser is None or not _browser.is_connected():
                await _reset_browser()
            logger.warning("Scrape failed for %s (mode=%s): %s — falling back to fetch", url, mode, exc)
            if mode != "fetch":
                try:
                    return await fetch_page(url)
                except Exception:
                    pass
            raise


async def crawl_site(url: str, depth: int = 2, max_pages: int = 50) -> dict[str, Any]:
    """Crawl a website BFS-style, returning pages and a sitemap.

    Args:
        url:       Root URL to crawl.
        depth:     Crawl depth (1 = just the given page).
        max_pages: Maximum pages to collect.

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
                page = await _new_page()
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


async def search_web(query: str, count: int = 10) -> dict[str, Any]:
    """Web search through the stealth browser.

    Navigates to DuckDuckGo, extracts organic results.
    """
    async with _page_slots:
        page = await _new_page()
        try:
            search_url = f"https://duckduckgo.com/html/?q={quote(query)}"
            await page.goto(search_url, wait_until=NAV_WAIT, timeout=int(SCRAPE_TIMEOUT * 1000))
            await page.wait_for_timeout(int(NAV_DELAY))

            results = await page.evaluate("""
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
            """, count)

            if not results:
                # Zero results usually means DuckDuckGo changed its markup and
                # the selectors above no longer match (or a challenge page was
                # served). Log it so operators can distinguish selector rot
                # from a genuine no-results query.
                logger.warning(
                    "Browser web search returned 0 results for %r — "
                    "DuckDuckGo HTML selectors may be stale",
                    query,
                )

            return {
                "engine": "duckduckgo",
                "query": query,
                "results": results[:count],
            }
        finally:
            await _close_page(page)


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
    """Close the Playwright connection."""
    global _browser, _playwright_ctx
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
