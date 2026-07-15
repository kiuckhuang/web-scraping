"""Tilion Fortress scraping client — stealth Chromium over CDP.

Connects to the Fortress container's CDP endpoint (http://fortress:9222) and
provides high-level operations using Playwright:

  - fetch_page:    get raw HTML + title + text of any URL (bypasses bot detection)
  - extract_page:  get clean markdown + tables from any URL
  - scrape:        convenience wrapper that tries extract first, falls back to fetch
  - crawl_site:    BFS crawl of a whole site
  - search_web:    web search through the stealth browser
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any
from urllib.parse import urlparse

import httpx
from playwright.async_api import async_playwright, Browser, Error as PlaywrightError

logger = logging.getLogger(__name__)

FORTRESS_CDP_URL = os.environ.get("FORTRESS_CDP_URL", "http://fortress:9222")
SCRAPE_TIMEOUT = float(os.environ.get("FORTRESS_TIMEOUT", "60"))
NAV_WAIT = os.environ.get("FORTRESS_NAV_WAIT", "domcontentloaded")

_browser: Browser | None = None
_playwright_ctx: Any = None
_lock = asyncio.Lock()


async def _get_browser() -> Browser:
    """Lazily connect to the Fortress CDP endpoint and reuse the connection."""
    global _browser, _playwright_ctx
    if _browser is None or not _browser.is_connected():
        async with _lock:
            if _browser is None or not _browser.is_connected():
                logger.info("Connecting to Fortress CDP at %s", FORTRESS_CDP_URL)
                _playwright_ctx = await async_playwright().start()
                _browser = await _playwright_ctx.chromium.connect_over_cdp(FORTRESS_CDP_URL)
                logger.info("Connected to Fortress CDP")
    return _browser


async def _new_page():
    """Get a new page (tab) on the Fortress browser."""
    browser = await _get_browser()
    context = await browser.new_context()
    page = await context.new_page()
    return page, context


async def fetch_page(url: str) -> dict[str, Any]:
    """Fetch a URL through the stealth browser.

    Returns:
        dict with keys: url, title, text, html, status
    """
    page, context = await _new_page()
    try:
        response = await page.goto(url, wait_until=NAV_WAIT, timeout=int(SCRAPE_TIMEOUT * 1000))
        await page.wait_for_timeout(1000)

        title = await page.title()
        text = await page.inner_text("body")
        html = await page.content()
        status = response.status if response else 200

        return {
            "url": page.url,
            "title": title,
            "text": text,
            "html": html,
            "status": status,
        }
    finally:
        await context.close()


async def extract_page(url: str) -> dict[str, Any]:
    """Extract clean markdown + tables from a URL.

    Uses a readability-style extraction: removes nav, footer, script, style,
    and converts the main content to markdown.

    Returns:
        dict with keys: url, title, markdown, tables
    """
    page, context = await _new_page()
    try:
        await page.goto(url, wait_until=NAV_WAIT, timeout=int(SCRAPE_TIMEOUT * 1000))
        await page.wait_for_timeout(1500)

        title = await page.title()

        # Extract tables
        tables = await _extract_tables(page)

        # Extract main content as markdown
        markdown = await _extract_markdown(page)

        return {
            "url": page.url,
            "title": title,
            "markdown": markdown,
            "tables": tables,
        }
    finally:
        await context.close()


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
    """Extract the main content as clean markdown."""
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
        # Get the main content text
        text = await page.evaluate("""
            () => {
                const main = document.querySelector('main, article, [role="main"], #content, .content')
                    || document.body;
                return main ? main.innerText : document.body.innerText;
            }
        """)
        # Basic cleanup: collapse excessive whitespace
        lines = [line.rstrip() for line in text.split("\n")]
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
        return await page.inner_text("body")


async def scrape(url: str, *, mode: str = "extract") -> dict[str, Any]:
    """Scrape a URL: extract clean markdown by default, or fetch raw HTML+text.

    Args:
        url:   Target URL.
        mode:  "extract" for clean markdown (default), "fetch" for raw HTML+text.

    Returns:
        Extraction result (markdown + tables) or fetch result (html + text).
    """
    try:
        if mode == "fetch":
            return await fetch_page(url)
        return await extract_page(url)
    except PlaywrightError as exc:
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
    parsed_root = urlparse(url)
    root_domain = f"{parsed_root.scheme}://{parsed_root.netloc}"

    visited: set[str] = set()
    pages: list[dict] = []
    sitemap: list[str] = []
    queue: list[tuple[str, int]] = [(url, 0)]

    while queue and len(pages) < max_pages:
        current_url, current_depth = queue.pop(0)

        if current_url in visited:
            continue
        visited.add(current_url)

        try:
            page, context = await _new_page()
            try:
                await page.goto(current_url, wait_until=NAV_WAIT, timeout=int(SCRAPE_TIMEOUT * 1000))
                await page.wait_for_timeout(800)

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
                    for link in links:
                        if link not in visited and link.startswith(root_domain):
                            queue.append((link, current_depth + 1))
            finally:
                await context.close()
        except Exception as exc:
            logger.warning("Crawl failed for %s: %s", current_url, exc)

    return {"pages": pages, "sitemap": sitemap, "count": len(pages)}


async def search_web(query: str, count: int = 10) -> dict[str, Any]:
    """Web search through the Fortress stealth browser.

    Navigates to DuckDuckGo, extracts organic results.
    """
    page, context = await _new_page()
    try:
        search_url = f"https://duckduckgo.com/html/?q={query.replace(' ', '+')}"
        await page.goto(search_url, wait_until=NAV_WAIT, timeout=int(SCRAPE_TIMEOUT * 1000))
        await page.wait_for_timeout(1500)

        results = await page.evaluate("""
            (count) => {
                const results = [];
                const items = document.querySelectorAll('.result, .web-result, [data-testid="result"]');
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

        return {
            "engine": "duckduckgo",
            "query": query,
            "results": results[:count],
        }
    finally:
        await context.close()


async def health() -> bool:
    """Check if the Fortress CDP endpoint is alive."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{FORTRESS_CDP_URL}/json/version")
            return resp.status_code == 200
    except Exception:
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
