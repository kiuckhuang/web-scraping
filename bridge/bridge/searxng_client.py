"""SearXNG search client — queries the SearXNG JSON API."""

from __future__ import annotations

import os
from typing import Any

import httpx

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080")
DEFAULT_TIMEOUT = 15.0
SEARXNG_HEADERS = {
    "X-Real-IP": "127.0.0.1",
    "X-Forwarded-For": "127.0.0.1",
}


async def search(
    query: str,
    *,
    categories: str | None = None,
    language: str = "en",
    pageno: int = 1,
    time_range: str | None = None,
    safesearch: int = 0,
    max_results: int = 10,
) -> dict[str, Any]:
    """Query SearXNG and return parsed JSON results.

    Args:
        query:        Search query string.
        categories:   Comma-separated categories (e.g. "general,it,images").
        language:     Language code (e.g. "en", "all").
        pageno:       Page number (1-based).
        time_range:   "day", "month", "year", or None.
        safesearch:   0=off, 1=moderate, 2=strict.
        max_results:  Truncate to this many results.

    Returns:
        SearXNG JSON response dict with keys:
        - query, number_of_results, results[], unresponsive_engines[]
    """
    params: dict[str, Any] = {
        "q": query,
        "format": "json",
        "language": language,
        "pageno": pageno,
        "safesearch": safesearch,
    }
    if categories:
        params["categories"] = categories
    if time_range:
        params["time_range"] = time_range

    async with httpx.AsyncClient(
        timeout=DEFAULT_TIMEOUT,
        headers=SEARXNG_HEADERS,
    ) as client:
        resp = await client.get(f"{SEARXNG_URL}/search", params=params)
        resp.raise_for_status()
        data = resp.json()

    results = data.get("results", [])[:max_results]
    data["results"] = results
    return data


async def health() -> bool:
    """Check if SearXNG is up and responding."""
    try:
        async with httpx.AsyncClient(timeout=5.0, headers=SEARXNG_HEADERS) as client:
            resp = await client.get(f"{SEARXNG_URL}/healthz")
            return resp.status_code == 200
    except Exception:
        return False
