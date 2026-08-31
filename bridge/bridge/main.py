"""FastAPI application — REST API for SearXNG search + stealth-browser scrape.

Endpoints:
  GET  /health              — check SearXNG + browser engine status
  GET  /search              — search the web via SearXNG (bing + stealth-browser fallbacks)
  POST /scrape              — scrape a URL via the stealth browser (Camoufox)
  POST /search_and_scrape   — search via SearXNG (with fallbacks), then scrape top results
  GET  /crawl               — crawl a whole site via the stealth browser
  GET  /web_search          — web search via the stealth browser (not SearXNG)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field

from . import ssrf
from .browser_client import (
    ENGINE,
    SessionLimitError,
    close_session as browser_close_session,
    crawl_site as browser_crawl,
    create_session as browser_create_session,
    health as browser_health,
    list_sessions as browser_list_sessions,
    scrape as browser_scrape,
    search_web as browser_web_search,
    shutdown as browser_shutdown,
)
from .searxng_client import (
    health as searxng_health,
    search as searxng_search,
    shutdown as searxng_shutdown,
)

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Scrape cache: repeated scrapes of the same URL within the TTL skip the
# (slow) browser round-trip. Big win for AI agents that re-visit pages.
BRIDGE_CACHE_TTL = float(os.environ.get("BRIDGE_CACHE_TTL", "300"))
BRIDGE_CACHE_MAX = int(os.environ.get("BRIDGE_CACHE_MAX", "100"))
BRIDGE_CACHE_MAX_BYTES = int(os.environ.get("BRIDGE_CACHE_MAX_BYTES", str(25 * 1024 * 1024)))

# Search fallbacks — SearXNG's google/duckduckgo can be bot-challenged into
# returning zero results. When the merged search comes back empty, retry with
# SearXNG's (disabled-by-default) bing engine, then with the stealth browser
# scraping real SERPs (google, then duckduckgo). Each stage is env-gated.
def _env_flag(name: str, default: str = "true") -> bool:
    return os.environ.get(name, default).strip().lower() not in ("", "0", "false", "no", "off")


SEARCH_FALLBACK_BING = _env_flag("SEARCH_FALLBACK_BING")
SEARCH_FALLBACK_BROWSER = _env_flag("SEARCH_FALLBACK_BROWSER")
BROWSER_SEARCH_ENGINES: tuple[str, ...] = ("google", "duckduckgo")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Bridge starting up")
    yield
    logger.info("Bridge shutting down — closing SearXNG/browser sessions")
    await searxng_shutdown()
    await browser_shutdown()


app = FastAPI(
    title="Web Scrape Bridge",
    description="SearXNG search + Camoufox stealth scrape — a self-hosted alternative to Exa",
    version="0.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def request_id_middleware(request, call_next):
    """Tag every request with a short request ID and log it.

    The ID is echoed back via the X-Request-ID header so a failing agent call
    can be correlated across bridge/browser/SearXNG logs. A client-supplied
    ID is honored only if it is short and printable — it goes back in a
    response header and into log lines, so it must not be attack surface.
    """
    supplied = request.headers.get("X-Request-ID") or ""
    rid = supplied if _REQUEST_ID_RE.fullmatch(supplied) else uuid.uuid4().hex[:8]
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    if request.url.path != "/health":  # keep healthchecks out of the logs
        logger.info("req=%s %s %s -> %s", rid, request.method, request.url.path, response.status_code)
    return response


# Client-supplied request IDs: 1-64 chars of [A-Za-z0-9._-]; anything else
# (oversized, whitespace, control characters) is replaced with a fresh ID.
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


# ---------------------------------------------------------------------------
#  Models
# ---------------------------------------------------------------------------

_SESSION_NAME_PATTERN = r"^[A-Za-z0-9._-]{1,64}$"


class ScrapeRequest(BaseModel):
    url: str = Field(..., max_length=8192, description="URL to scrape")
    mode: Literal["extract", "fetch"] = Field("extract", description='"extract" for clean markdown, "fetch" for raw HTML')
    session: str | None = Field(None, max_length=64, pattern=_SESSION_NAME_PATTERN, description="Named session for login persistence (create via POST /sessions)")


class SearchAndScrapeRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=1000, description="Search query")
    categories: str | None = Field(None, description="Comma-separated SearXNG categories")
    language: str = Field("en")
    max_results: int = Field(5, ge=1, le=50, description="How many results to scrape (default 5)")
    scrape_mode: Literal["extract", "fetch"] = Field("extract", description='"extract" or "fetch"')
    session: str | None = Field(None, max_length=64, pattern=_SESSION_NAME_PATTERN, description="Named session for login persistence")


class SessionRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, pattern=_SESSION_NAME_PATTERN, description="Session name")


# ---------------------------------------------------------------------------
#  URL validation — block private/internal networks (SSRF protection)
#  (core logic lives in bridge.bridge.ssrf, shared with the browser guard)
# ---------------------------------------------------------------------------

def _validate_public_url(url: str) -> str:
    """Reject URLs pointing to private/internal networks.

    Raises HTTPException(403) for SSRF attempts, HTTPException(400) for
    malformed URLs. Blocking DNS resolution runs in a worker thread via
    asyncio.to_thread at the call sites — a slow resolver must not stall
    the event loop.
    """
    try:
        return ssrf.validate_public_url(url)
    except ssrf.UrlBlockedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


def _is_public_url(url: str) -> bool:
    """True if the URL is allowed to be scraped (public http/https only)."""
    try:
        ssrf.validate_public_url(url)
        return True
    except ssrf.UrlBlockedError:
        return False


# ---------------------------------------------------------------------------
#  Scrape cache — TTL + size-bounded, keyed by (url, mode)
# ---------------------------------------------------------------------------

_cache: dict[tuple[str, str], tuple[float, dict[str, Any], int]] = {}
_cache_bytes = 0


async def _cache_get(url: str, mode: str, session: str | None = None) -> dict[str, Any] | None:
    global _cache_bytes
    key = (url, mode, session or "")
    item = _cache.get(key)
    if item is None:
        return None
    if time.monotonic() - item[0] > BRIDGE_CACHE_TTL:
        _cache.pop(key, None)
        _cache_bytes -= item[2]
        return None
    logger.info("Cache hit: %s (mode=%s, session=%s)", url, mode, session or "-")
    return item[1]


def _cache_set(url: str, mode: str, value: dict[str, Any], session: str | None = None) -> None:
    global _cache_bytes
    size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    if size > BRIDGE_CACHE_MAX_BYTES:
        return
    existing = _cache.pop((url, mode), None)
    if existing is not None:
        _cache_bytes -= existing[2]
    while _cache and (
        len(_cache) >= BRIDGE_CACHE_MAX or _cache_bytes + size > BRIDGE_CACHE_MAX_BYTES
    ):
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache_bytes -= _cache.pop(oldest)[2]
    _cache[(url, mode, session or "")] = (time.monotonic(), value, size)
    _cache_bytes += size


def _cacheable(content: dict[str, Any]) -> bool:
    """WAF challenges and HTTP error pages must not be cached: a challenge can
    clear on retry and an error may be transient — serve them once, uncached."""
    if content.get("waf_challenge"):
        return False
    status = content.get("status")
    return not (isinstance(status, int) and status >= 400)


# ---------------------------------------------------------------------------
#  Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health_check() -> dict[str, Any]:
    """Check the status of SearXNG and the configured browser engine."""
    searxng_ok, browser_ok = await asyncio.gather(
        searxng_health(),
        browser_health(),
    )
    return {
        "status": "ok" if searxng_ok and browser_ok else "degraded",
        "engine": ENGINE,
        "services": {
            "searxng": "up" if searxng_ok else "down",
            "browser": "up" if browser_ok else "down",
        },
    }


# ---------------------------------------------------------------------------
#  Search (SearXNG, with bing + stealth-browser fallbacks)
# ---------------------------------------------------------------------------

def _fallback_allowed(categories: str | None, pageno: int, query: str) -> bool:
    """Fallbacks apply to plain first-page general web searches only.

    - categories must be unset or include "general" (browser SERPs are web
      searches — they cannot serve images/news/it categories).
    - only page 1 (SERP fallbacks fetch page 1; deeper pages stay SearXNG-only).
    - an explicit "!bang" in the query means the caller is directing engines
      themselves — never second-guess that.
    """
    if query.strip().startswith("!") or pageno != 1:
        return False
    if not categories:
        return True
    return "general" in [c.strip().lower() for c in categories.split(",")]


async def _search_with_fallbacks(
    q: str,
    *,
    categories: str | None,
    language: str,
    pageno: int,
    time_range: str | None,
    safesearch: int,
    max_results: int,
) -> dict[str, Any]:
    """SearXNG merged search, degrading gracefully when it yields nothing.

    Chain: SearXNG (google w=1.2 → duckduckgo → …) → SearXNG "!bing" forced
    query → Camoufox SERP (google, then duckduckgo). Each stage runs only if
    the previous one returned zero results; stage errors are logged and
    skipped. A response served by a fallback carries a "fallback" field
    ("bing" / "browser:google" / "browser:duckduckgo").
    """
    primary_error: Exception | None = None
    primary: dict[str, Any] | None = None
    try:
        primary = await searxng_search(
            q,
            categories=categories,
            language=language,
            pageno=pageno,
            time_range=time_range,
            safesearch=safesearch,
            max_results=max_results,
        )
        if primary.get("results"):
            return primary
    except Exception as exc:
        primary_error = exc
        logger.warning("SearXNG search failed for %r: %s", q, exc)

    # Gated off (or already past page 1) → no fallback: return the empty
    # primary as-is, or surface the failure if SearXNG itself errored.
    if not _fallback_allowed(categories, pageno, q):
        if primary is not None:
            return primary
        raise HTTPException(status_code=502, detail=f"SearXNG error: {primary_error}")

    unresponsive = (primary or {}).get("unresponsive_engines", [])

    # Stage 2 — SearXNG bing (registered but disabled; the bang activates it
    # for this query only, so normal searches never see bing results).
    if SEARCH_FALLBACK_BING:
        try:
            bing = await searxng_search(
                f"!bing {q}",
                categories=categories,
                language=language,
                safesearch=safesearch,
                max_results=max_results,
            )
            if bing.get("results"):
                bing["query"] = q
                bing["fallback"] = "bing"
                # Surface the *primary* merge's unresponsive engines — that is
                # the gap the client needs to know about, not the bing query's.
                bing["unresponsive_engines"] = unresponsive
                logger.info("Search fallback served %d results via bing for %r", len(bing["results"]), q)
                return bing
        except Exception as exc:
            logger.warning("Bing fallback failed for %r: %s", q, exc)

    # Stage 3 — stealth browser scraping real SERPs (google, then ddg).
    if SEARCH_FALLBACK_BROWSER:
        try:
            web = await browser_web_search(q, count=max_results, engines=BROWSER_SEARCH_ENGINES)
            results = [
                {
                    "title": r.get("title", "Untitled"),
                    "url": r.get("url", ""),
                    "content": (r.get("snippet") or "").strip(),
                    "engine": web.get("engine", "browser"),
                }
                for r in web.get("results", [])
                if r.get("url")
            ]
            if results:
                logger.info("Search fallback served %d results via browser:%s for %r", len(results), web.get("engine"), q)
                return {
                    "query": q,
                    "number_of_results": len(results),
                    "results": results[:max_results],
                    "unresponsive_engines": unresponsive,
                    "fallback": f"browser:{web.get('engine')}",
                }
        except Exception as exc:
            logger.warning("Browser search fallback failed for %r: %s", q, exc)

    if primary is not None:
        return primary
    raise HTTPException(status_code=502, detail=f"All search backends failed; last error: {primary_error}")


@app.get("/search")
async def search(
    q: str = Query(..., min_length=1, max_length=1000, description="Search query"),
    categories: str | None = Query(None),
    language: str = Query("en"),
    pageno: int = Query(1, ge=1),
    time_range: str | None = Query(None, description="Time range: day, week, month, year"),
    safesearch: int = Query(0, ge=0, le=2),
    max_results: int = Query(10, ge=1, le=50),
) -> dict[str, Any]:
    """Search the web via SearXNG, falling back to bing and then to stealth-
    browser SERPs (google/duckduckgo) when the merged search returns nothing."""
    return await _search_with_fallbacks(
        q,
        categories=categories,
        language=language,
        pageno=pageno,
        time_range=time_range,
        safesearch=safesearch,
        max_results=max_results,
    )


# ---------------------------------------------------------------------------
#  Scrape (stealth browser)
# ---------------------------------------------------------------------------

@app.post("/scrape")
async def scrape(req: ScrapeRequest) -> dict[str, Any]:
    """Scrape a single URL through the stealth browser.

    Bypasses Cloudflare, DataDome, PerimeterX, Akamai, and other bot detection.
    Returns clean markdown (extract mode) or raw HTML + text (fetch mode).
    """
    await asyncio.to_thread(_validate_public_url, req.url)
    cached = await _cache_get(req.url, req.mode, req.session)
    if cached is not None:
        return {**cached, "cached": True}
    try:
        content = await browser_scrape(req.url, mode=req.mode, session=req.session)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Browser scrape error: {exc}")
    if _cacheable(content):
        _cache_set(req.url, req.mode, content, req.session)
    return content


# ---------------------------------------------------------------------------
#  Search + Scrape (the Exa-style combined endpoint)
# ---------------------------------------------------------------------------

@app.post("/search_and_scrape")
async def search_and_scrape(req: SearchAndScrapeRequest) -> dict[str, Any]:
    """Search via SearXNG (with fallbacks), then scrape each result URL through
    the stealth browser.

    This is the primary "Exa-like" endpoint: get search results with full page content.
    Results are scraped concurrently for speed.
    """
    try:
        results = await _search_with_fallbacks(
            req.query,
            categories=req.categories,
            language=req.language,
            pageno=1,
            time_range=None,
            safesearch=0,
            max_results=req.max_results,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Search error: {exc}")

    url_results = [r for r in results.get("results", []) if r.get("url")]
    if not url_results:
        return {"query": req.query, "results": [], "scraped": [], "fallback": results.get("fallback")}

    async def scrape_one(result: dict) -> dict:
        url = result.get("url", "")
        # SSRF guard: never hand a private/internal URL to the browser, even if a
        # search engine returned it. DNS runs in a worker thread — with up to
        # 50 results gathered concurrently, on-loop resolution would serialize
        # and stall the whole event loop.
        if not await asyncio.to_thread(_is_public_url, url):
            return {**result, "content": None, "scrape_error": "blocked: internal or private address"}
        cached = await _cache_get(url, req.scrape_mode, req.session)
        if cached is not None:
            return {**result, "content": cached, "cached": True}
        try:
            content = await browser_scrape(url, mode=req.scrape_mode, session=req.session)
            if _cacheable(content):
                _cache_set(url, req.scrape_mode, content, req.session)
            return {**result, "content": content}
        except Exception as exc:
            return {**result, "content": None, "scrape_error": str(exc)}

    scraped = await asyncio.gather(*[scrape_one(r) for r in url_results])
    return {
        "query": req.query,
        "number_of_results": results.get("number_of_results", 0),
        "results": scraped,
        "fallback": results.get("fallback"),
    }


# ---------------------------------------------------------------------------
#  Crawl (stealth browser)
# ---------------------------------------------------------------------------

@app.get("/crawl")
async def crawl(
    url: str = Query(..., max_length=8192, description="Root URL to crawl"),
    depth: int = Query(2, ge=1, le=5),
    max_pages: int = Query(50, ge=1, le=200),
    session: str | None = Query(None, max_length=64, pattern=_SESSION_NAME_PATTERN, description="Named session for login persistence"),
) -> dict[str, Any]:
    """Crawl a whole site via the stealth browser (auto-handles SPA/JS + lazy-load)."""
    await asyncio.to_thread(_validate_public_url, url)
    try:
        return await browser_crawl(url, depth=depth, max_pages=max_pages, session=session)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Browser crawl error: {exc}")


# ---------------------------------------------------------------------------
#  Named sessions (login persistence)
# ---------------------------------------------------------------------------

@app.post("/sessions")
async def create_session(req: SessionRequest) -> dict[str, Any]:
    """Create (or return) a named long-lived browser context.

    Scrapes that pass the same `session` name reuse this context, so cookies
    and localStorage persist across calls (e.g. log in once, scrape many
    authenticated pages). Sessions live until DELETE /sessions/{name} or a
    bridge/browser restart. Bounded by CAMOUFOX_MAX_SESSIONS.
    """
    try:
        created = await browser_create_session(req.name)
    except SessionLimitError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Browser session error: {exc}")
    return {"name": req.name, "created": created}


@app.get("/sessions")
async def list_sessions() -> dict[str, Any]:
    """List the names of live browser sessions."""
    return {"sessions": browser_list_sessions()}


@app.delete("/sessions/{name}")
async def delete_session(name: str = Path(..., pattern=_SESSION_NAME_PATTERN, description="Session name")) -> dict[str, Any]:
    """Close and forget a named browser session."""
    deleted = await browser_close_session(name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Session not found: {name}")
    return {"name": name, "deleted": True}


# ---------------------------------------------------------------------------
#  Web search via the stealth browser (not SearXNG)
# ---------------------------------------------------------------------------

@app.get("/web_search")
async def web_search(
    q: str = Query(..., min_length=1, max_length=1000, description="Search query"),
    count: int = Query(10, ge=1, le=30),
) -> dict[str, Any]:
    """Web search through the stealth browser (real browser, no SERP API)."""
    try:
        return await browser_web_search(q, count=count)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Browser search error: {exc}")


# ---------------------------------------------------------------------------
#  Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    host = os.environ.get("BRIDGE_HOST", "0.0.0.0")
    port = int(os.environ.get("BRIDGE_PORT", "8000"))
    uvicorn.run("bridge.main:app", host=host, port=port, reload=False)
