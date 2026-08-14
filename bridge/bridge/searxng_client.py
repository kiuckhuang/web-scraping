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
LANGUAGE_ALIASES = {
    "zh-hant": "zh-TW",
    "zh-hans": "zh-CN",
}

# Reused across requests — keeps connections pooled instead of re-opening
# a connection per search.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, headers=SEARXNG_HEADERS)
    return _client


async def shutdown() -> None:
    """Close the shared client (called on bridge shutdown)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


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
    language = LANGUAGE_ALIASES.get(language.lower(), language)
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

    client = _get_client()
    resp = await client.get(f"{SEARXNG_URL}/search", params=params)
    resp.raise_for_status()
    data = resp.json()

    # Some engines cannot provide dated results for all locales. Preserve
    # useful search output when a strict time filter produces no results.
    if time_range and not data.get("results"):
        fallback_params = {**params}
        fallback_params.pop("time_range", None)
        fallback_resp = await client.get(f"{SEARXNG_URL}/search", params=fallback_params)
        fallback_resp.raise_for_status()
        data = fallback_resp.json()
        data["time_range_fallback"] = True

    results = data.get("results", [])[:max_results]
    data["results"] = results
    return data


async def health() -> bool:
    """Check if SearXNG is up and responding."""
    try:
        client = _get_client()
        resp = await client.get(f"{SEARXNG_URL}/healthz")
        return resp.status_code == 200
    except Exception:
        return False
