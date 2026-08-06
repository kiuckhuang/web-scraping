"""FastAPI application — REST API for SearXNG search + Fortress scrape.

Endpoints:
  GET  /health              — check SearXNG + Fortress status
  GET  /search              — search the web via SearXNG
  POST /scrape              — scrape a URL via Fortress (stealth browser)
  POST /search_and_scrape   — search via SearXNG, then scrape top results
  GET  /crawl               — crawl a whole site via Fortress
  GET  /web_search          — web search via Fortress stealth browser (not SearXNG)
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import socket
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Literal
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .searxng_client import search as searxng_search, health as searxng_health, shutdown as searxng_shutdown
from .fortress_client import (
    scrape as fortress_scrape,
    crawl_site as fortress_crawl,
    search_web as fortress_web_search,
    health as fortress_health,
    shutdown as fortress_shutdown,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Scrape cache: repeated scrapes of the same URL within the TTL skip the
# (slow) browser round-trip. Big win for AI agents that re-visit pages.
BRIDGE_CACHE_TTL = float(os.environ.get("BRIDGE_CACHE_TTL", "300"))
BRIDGE_CACHE_MAX = int(os.environ.get("BRIDGE_CACHE_MAX", "100"))
BRIDGE_CACHE_MAX_BYTES = int(os.environ.get("BRIDGE_CACHE_MAX_BYTES", str(25 * 1024 * 1024)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Bridge starting up")
    yield
    logger.info("Bridge shutting down — closing SearXNG/Fortress sessions")
    await searxng_shutdown()
    await fortress_shutdown()


app = FastAPI(
    title="Web Scrape Bridge",
    description="SearXNG search + Tilion Fortress stealth scrape — a self-hosted alternative to Exa",
    version="0.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def request_id_middleware(request, call_next):
    """Tag every request with a short request ID and log it.

    The ID is echoed back via the X-Request-ID header so a failing agent call
    can be correlated across bridge/Fortress/SearXNG logs.
    """
    rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:8]
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    if request.url.path != "/health":  # keep healthchecks out of the logs
        logger.info("req=%s %s %s -> %s", rid, request.method, request.url.path, response.status_code)
    return response


# ---------------------------------------------------------------------------
#  Models
# ---------------------------------------------------------------------------

class ScrapeRequest(BaseModel):
    url: str = Field(..., max_length=8192, description="URL to scrape")
    mode: Literal["extract", "fetch"] = Field("extract", description='"extract" for clean markdown, "fetch" for raw HTML')


class SearchAndScrapeRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=1000, description="Search query")
    categories: str | None = Field(None, description="Comma-separated SearXNG categories")
    language: str = Field("en")
    max_results: int = Field(5, ge=1, le=50, description="How many results to scrape (default 5)")
    scrape_mode: Literal["extract", "fetch"] = Field("extract", description='"extract" or "fetch"')


# ---------------------------------------------------------------------------
#  URL validation — block private/internal networks (SSRF protection)
# ---------------------------------------------------------------------------

def _validate_public_url(url: str) -> str:
    """Reject URLs pointing to private/internal networks.

    Raises HTTPException(403) for SSRF attempts.

    Note on DNS rebinding: the host is resolved here and again by the browser
    when navigating. We reject unresolvable hosts outright so a host that
    fails validation-time resolution cannot slip through a later re-resolution.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail=f"Unsupported scheme: {parsed.scheme}")
    host = parsed.hostname or ""
    if not host:
        raise HTTPException(status_code=400, detail="Missing host")
    # Block obvious local names
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        raise HTTPException(status_code=403, detail="Access to internal addresses is blocked")
    # Resolve and block private ranges
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        # Unresolvable host — reject rather than let a later re-resolution
        # by the browser silently bypass the check (DNS-rebinding defense).
        raise HTTPException(status_code=403, detail="Could not resolve host; access blocked")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local:
            raise HTTPException(status_code=403, detail="Access to internal addresses is blocked")
    return url


def _is_public_url(url: str) -> bool:
    """True if the URL is allowed to be scraped (public http/https only)."""
    try:
        _validate_public_url(url)
        return True
    except HTTPException:
        return False


# ---------------------------------------------------------------------------
#  Scrape cache — TTL + size-bounded, keyed by (url, mode)
# ---------------------------------------------------------------------------

_cache: dict[tuple[str, str], tuple[float, dict[str, Any], int]] = {}
_cache_bytes = 0


async def _cache_get(url: str, mode: str) -> dict[str, Any] | None:
    global _cache_bytes
    item = _cache.get((url, mode))
    if item is None:
        return None
    if time.monotonic() - item[0] > BRIDGE_CACHE_TTL:
        _cache.pop((url, mode), None)
        _cache_bytes -= item[2]
        return None
    logger.info("Cache hit: %s (mode=%s)", url, mode)
    return item[1]


def _same_origin(left: str, right: str) -> bool:
    """Compare scheme, hostname, and effective port for crawl boundaries."""
    a, b = urlparse(left), urlparse(right)
    try:
        a_port = a.port or (443 if a.scheme == "https" else 80)
        b_port = b.port or (443 if b.scheme == "https" else 80)
    except ValueError:
        return False
    return (a.scheme.lower(), a.hostname.lower() if a.hostname else "", a_port) == (
        b.scheme.lower(), b.hostname.lower() if b.hostname else "", b_port
    )


def _cache_set(url: str, mode: str, value: dict[str, Any]) -> None:
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
    _cache[(url, mode)] = (time.monotonic(), value, size)
    _cache_bytes += size


# ---------------------------------------------------------------------------
#  Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health_check() -> dict[str, Any]:
    """Check the status of SearXNG and Fortress."""
    searxng_ok, fortress_ok = await asyncio.gather(
        searxng_health(),
        fortress_health(),
    )
    return {
        "status": "ok" if searxng_ok and fortress_ok else "degraded",
        "services": {
            "searxng": "up" if searxng_ok else "down",
            "fortress": "up" if fortress_ok else "down",
        },
    }


# ---------------------------------------------------------------------------
#  Search (SearXNG)
# ---------------------------------------------------------------------------

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
    """Search the web via SearXNG (70+ engines aggregated)."""
    try:
        return await searxng_search(
            q,
            categories=categories,
            language=language,
            pageno=pageno,
            time_range=time_range,
            safesearch=safesearch,
            max_results=max_results,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"SearXNG error: {exc}")


# ---------------------------------------------------------------------------
#  Scrape (Fortress)
# ---------------------------------------------------------------------------

@app.post("/scrape")
async def scrape(req: ScrapeRequest) -> dict[str, Any]:
    """Scrape a single URL through the Fortress stealth browser.

    Bypasses Cloudflare, DataDome, PerimeterX, Akamai, and other bot detection.
    Returns clean markdown (extract mode) or raw HTML + text (fetch mode).
    """
    _validate_public_url(req.url)
    cached = await _cache_get(req.url, req.mode)
    if cached is not None:
        return {**cached, "cached": True}
    try:
        content = await fortress_scrape(req.url, mode=req.mode)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Fortress scrape error: {exc}")
    _cache_set(req.url, req.mode, content)
    return content


# ---------------------------------------------------------------------------
#  Search + Scrape (the Exa-style combined endpoint)
# ---------------------------------------------------------------------------

@app.post("/search_and_scrape")
async def search_and_scrape(req: SearchAndScrapeRequest) -> dict[str, Any]:
    """Search via SearXNG, then scrape each result URL through Fortress.

    This is the primary "Exa-like" endpoint: get search results with full page content.
    Results are scraped concurrently for speed.
    """
    try:
        results = await searxng_search(
            req.query,
            categories=req.categories,
            language=req.language,
            max_results=req.max_results,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"SearXNG error: {exc}")

    urls = [r["url"] for r in results.get("results", []) if r.get("url")]
    if not urls:
        return {"query": req.query, "results": [], "scraped": []}

    async def scrape_one(result: dict) -> dict:
        url = result.get("url", "")
        # SSRF guard: never hand a private/internal URL to Fortress, even if a
        # search engine returned it.
        if not _is_public_url(url):
            return {**result, "content": None, "scrape_error": "blocked: internal or private address"}
        cached = await _cache_get(url, req.scrape_mode)
        if cached is not None:
            return {**result, "content": cached, "cached": True}
        try:
            content = await fortress_scrape(url, mode=req.scrape_mode)
            _cache_set(url, req.scrape_mode, content)
            return {**result, "content": content}
        except Exception as exc:
            return {**result, "content": None, "scrape_error": str(exc)}

    scraped = await asyncio.gather(*[scrape_one(r) for r in results["results"]])
    return {
        "query": req.query,
        "number_of_results": results.get("number_of_results", 0),
        "results": scraped,
    }


# ---------------------------------------------------------------------------
#  Crawl (Fortress)
# ---------------------------------------------------------------------------

@app.get("/crawl")
async def crawl(
    url: str = Query(..., max_length=8192, description="Root URL to crawl"),
    depth: int = Query(2, ge=1, le=5),
    max_pages: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """Crawl a whole site via Fortress (auto-handles SPA/JS + lazy-load)."""
    _validate_public_url(url)
    try:
        return await fortress_crawl(url, depth=depth, max_pages=max_pages)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Fortress crawl error: {exc}")


# ---------------------------------------------------------------------------
#  Web search via Fortress (stealth browser, not SearXNG)
# ---------------------------------------------------------------------------

@app.get("/web_search")
async def web_search(
    q: str = Query(..., min_length=1, max_length=1000, description="Search query"),
    count: int = Query(10, ge=1, le=30),
) -> dict[str, Any]:
    """Web search through the Fortress stealth browser (real browser, no SERP API)."""
    try:
        return await fortress_web_search(q, count=count)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Fortress search error: {exc}")


# ---------------------------------------------------------------------------
#  Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    host = os.environ.get("BRIDGE_HOST", "0.0.0.0")
    port = int(os.environ.get("BRIDGE_PORT", "8000"))
    uvicorn.run("bridge.main:app", host=host, port=port, reload=False)
