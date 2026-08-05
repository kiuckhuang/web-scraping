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
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .searxng_client import search as searxng_search, health as searxng_health
from .fortress_client import (
    scrape as fortress_scrape,
    crawl_site as fortress_crawl,
    search_web as fortress_web_search,
    health as fortress_health,
    shutdown as fortress_shutdown,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Bridge starting up")
    yield
    logger.info("Bridge shutting down — closing Fortress session")
    await fortress_shutdown()


app = FastAPI(
    title="Web Scrape Bridge",
    description="SearXNG search + Tilion Fortress stealth scrape — a self-hosted alternative to Exa",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
#  Models
# ---------------------------------------------------------------------------

class ScrapeRequest(BaseModel):
    url: str = Field(..., description="URL to scrape")
    mode: str = Field("extract", description='"extract" for clean markdown, "fetch" for raw HTML')


class SearchAndScrapeRequest(BaseModel):
    query: str = Field(..., description="Search query")
    categories: str | None = Field(None, description="Comma-separated SearXNG categories")
    language: str = Field("en")
    max_results: int = Field(5, description="How many results to scrape (default 5)")
    scrape_mode: str = Field("extract", description='"extract" or "fetch"')


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
    q: str = Query(..., description="Search query"),
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
    try:
        return await fortress_scrape(req.url, mode=req.mode)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Fortress scrape error: {exc}")


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
        try:
            content = await fortress_scrape(url, mode=req.scrape_mode)
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
    url: str = Query(..., description="Root URL to crawl"),
    depth: int = Query(2, ge=1, le=5),
    max_pages: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """Crawl a whole site via Fortress (auto-handles SPA/JS + lazy-load)."""
    try:
        return await fortress_crawl(url, depth=depth, max_pages=max_pages)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Fortress crawl error: {exc}")


# ---------------------------------------------------------------------------
#  Web search via Fortress (stealth browser, not SearXNG)
# ---------------------------------------------------------------------------

@app.get("/web_search")
async def web_search(
    q: str = Query(..., description="Search query"),
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
