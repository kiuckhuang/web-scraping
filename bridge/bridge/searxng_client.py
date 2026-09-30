"""SearXNG search client — queries the SearXNG JSON API."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080")
DEFAULT_TIMEOUT = float(os.environ.get("SEARXNG_CLIENT_TIMEOUT", "18"))
BREAKER_THRESHOLD = int(os.environ.get("SEARXNG_BREAKER_THRESHOLD", "2"))
BREAKER_COOLDOWN = float(os.environ.get("SEARXNG_BREAKER_COOLDOWN", "60"))
_failures = 0
_skip_until = 0.0
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
    global _failures, _skip_until
    general = not categories or categories == "general"
    breaker_applies = general and not query.lstrip().startswith("!") and pageno == 1
    if breaker_applies and time.monotonic() < _skip_until:
        raise RuntimeError("SearXNG circuit breaker open after repeated unavailable searches")
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
    try:
        resp = await client.get(f"{SEARXNG_URL}/search", params=params)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        if breaker_applies:
            _record_failure()
        raise
    if breaker_applies:
        if data.get("results"):
            _failures, _skip_until = 0, 0.0
        elif data.get("unresponsive_engines"):
            _record_failure()

    results = data.get("results", [])[:max_results]
    data["results"] = results
    return data


def _record_failure() -> None:
    global _failures, _skip_until
    _failures += 1
    if BREAKER_THRESHOLD > 0 and _failures >= BREAKER_THRESHOLD:
        _skip_until = time.monotonic() + BREAKER_COOLDOWN


async def health() -> bool:
    """Check if SearXNG is up and responding."""
    try:
        client = _get_client()
        resp = await client.get(f"{SEARXNG_URL}/healthz")
        return resp.status_code == 200
    except Exception:
        return False
