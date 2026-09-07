"""Jina AI Reader/Search client — optional last-resort cloud transports.

When JINA_ENABLED=true (default false), Jina AI's hosted APIs become FINAL
fallbacks that only fire after every self-hosted transport failed:

  search → s.jina.ai   only after SearXNG / browser SERPs returned nothing
  scrape → r.jina.ai   only after the Camoufox browser could not fetch the
                       page (typically WAF / anti-bot hard blocks)

Auth and cost (verified 2026-09 against the live API):
  - Both endpoints take ``Authorization: Bearer <JINA_API_KEY>``.
  - s.jina.ai REQUIRES a key (anonymous requests get 401 40103) and bills a
    fixed >=10k tokens per request — search fallback is deliberately opt-in.
  - r.jina.ai works keyless at 20 RPM/IP; with a key 500 RPM.
  - 429 bodies carry ``retryAfter`` — surfaced to callers, never retried here
    (automatic retries would burn quota without improving the outcome).

The endpoints are overridable (``JINA_READER_URL`` / ``JINA_SEARCH_URL``) so
the EU-residency hosts (eu.r.jina.ai / eu.s.jina.ai) or a self-hosted OSS
reader can be used without code changes.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: str = "true") -> bool:
    """Mirror of main._env_flag (duplicated to avoid a circular import)."""
    return os.environ.get(name, default).strip().lower() not in ("", "0", "false", "no", "off")


# Master switch — default OFF: a paid third-party API must never be contacted
# implicitly. JINA_SEARCH_FALLBACK / JINA_SCRAPE_FALLBACK imply it.
JINA_ENABLED = _env_flag("JINA_ENABLED", "false")
JINA_API_KEY = os.environ.get("JINA_API_KEY", "").strip()
JINA_TIMEOUT = float(os.environ.get("JINA_TIMEOUT", "60"))
JINA_READER_URL = os.environ.get("JINA_READER_URL", "https://r.jina.ai").strip().rstrip("/")
JINA_SEARCH_URL = os.environ.get("JINA_SEARCH_URL", "https://s.jina.ai").strip().rstrip("/")
# Egress proxy for the Jina API calls themselves (empty = inherit the
# stack-wide EGRESS_PROXY, else direct) — same fallback rule as the fast path.
JINA_PROXY = (
    os.environ.get("JINA_PROXY", "").strip()
    or os.environ.get("EGRESS_PROXY", "").strip()
)
# Last-resort chain participation (each implies JINA_ENABLED).
JINA_SEARCH_FALLBACK = JINA_ENABLED and _env_flag("JINA_SEARCH_FALLBACK")
JINA_SCRAPE_FALLBACK = JINA_ENABLED and _env_flag("JINA_SCRAPE_FALLBACK")

# s.jina.ai validates num/count in 0..20; larger values are a 400.
MAX_SEARCH_RESULTS = 20
# Search results carry the full page read; only a snippet goes into the
# SearXNG-shaped result list (the full markdown rides along as jina_pages).
_SNIPPET_CHARS = 400
# Parity with the HTTP fast path's markdown cap.
MAX_MARKDOWN_CHARS = 50_000

# Reused across requests — keeps connections pooled instead of re-opening a
# TLS connection (and re-paying the handshake) per call.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        headers = {"Authorization": f"Bearer {JINA_API_KEY}"} if JINA_API_KEY else {}
        _client = httpx.AsyncClient(
            timeout=JINA_TIMEOUT,
            headers=headers,
            proxy=JINA_PROXY or None,
        )
    return _client


async def shutdown() -> None:
    """Close the shared client (called on bridge shutdown)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def status() -> str:
    """Configured state for /health — informational, never degrades the stack."""
    if not JINA_ENABLED:
        return "off"
    return "ready" if JINA_API_KEY else "anonymous"


class JinaError(Exception):
    """Jina API failure, carrying the HTTP status the bridge should return."""

    def __init__(self, message: str, *, bridge_status: int = 502, retry_after: float | None = None):
        super().__init__(message)
        self.bridge_status = bridge_status
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
#  Request seam (monkeypatched in tests) + error-envelope decoding
# ---------------------------------------------------------------------------

async def _request_json(
    method: str, url: str, *, json_body: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """One Jina API call returning (http_status, parsed json body).

    This is the only place that touches the network; tests monkeypatch it.
    """
    resp = await _get_client().request(method, url, json=json_body, headers=headers or {})
    try:
        data: dict[str, Any] = resp.json()
    except ValueError:
        data = {"message": (resp.text or "")[:500]}
    return resp.status_code, data


def _raise_for_error(status_code: int, body: dict[str, Any]) -> None:
    """Decode Jina's error envelope into a JinaError.

    Success is HTTP 2xx with the envelope code 200; everything else carries
    ``{"code": <http>, "status": <5-digit>, "name", "message", "retryAfter"?}``
    (e.g. 401/40103 AuthenticationRequired, 429/42903 RateLimitTriggered).
    """
    if 200 <= status_code < 300 and body.get("code", 200) == 200:
        return
    message = body.get("message") or body.get("name") or f"HTTP {status_code}"
    jina_status = body.get("status")
    retry_after = body.get("retryAfter")
    if status_code == 401 or jina_status in (40102, 40103):
        raise JinaError(f"Jina auth failed: {message} — check JINA_API_KEY")
    if status_code == 429 or jina_status == 42903:
        raise JinaError(
            f"Jina rate limit exceeded: {message}",
            bridge_status=429,
            retry_after=float(retry_after) if retry_after is not None else None,
        )
    if status_code == 402 or jina_status in (40202, 40203, 40904):
        raise JinaError(f"Jina quota/billing: {message}")
    raise JinaError(f"Jina API error {status_code}: {message}")


# ---------------------------------------------------------------------------
#  Search (s.jina.ai) — SearXNG-shaped results + full page reads
# ---------------------------------------------------------------------------

async def search(
    query: str,
    *,
    max_results: int = 5,
    sites: list[str] | None = None,
) -> dict[str, Any]:
    """Search via s.jina.ai; returns the SearXNG result shape plus jina_pages.

    Jina performs a full r.jina.ai read of each result server-side. The
    SearXNG-shaped ``results`` carry only snippets; the full page markdown
    rides along under ``jina_pages`` (consumed by /search_and_scrape, which
    reuses it instead of re-scraping; /search strips it before responding).
    Raises JinaError — the caller decides whether that degrades the response.
    """
    if not JINA_ENABLED:
        raise JinaError("Jina integration is disabled (JINA_ENABLED=false)", bridge_status=503)
    if not JINA_API_KEY:
        raise JinaError(
            "s.jina.ai requires an API key — set JINA_API_KEY in .env (https://jina.ai/?sui=apikey)",
            bridge_status=503,
        )
    body: dict[str, Any] = {
        "q": query,
        "num": max(1, min(int(max_results), MAX_SEARCH_RESULTS)),
    }
    headers = {"Accept": "application/json"}
    if sites:
        body["site"] = sites
    status_code, data = await _request_json("POST", f"{JINA_SEARCH_URL}/", json_body=body, headers=headers)
    _raise_for_error(status_code, data)

    results: list[dict[str, Any]] = []
    pages: dict[str, dict[str, str]] = {}
    for item in data.get("data") or []:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        title = (item.get("title") or "Untitled").strip()
        description = (item.get("description") or "").strip()
        content = (item.get("content") or "").strip()
        results.append({
            "title": title,
            "url": url,
            "content": description or content[:_SNIPPET_CHARS],
            "engine": "jina",
        })
        if content:
            pages[url] = {"title": title, "markdown": content}
    response: dict[str, Any] = {
        "query": query,
        "number_of_results": len(results),
        "results": results,
        "engine": "jina",
    }
    if pages:
        response["jina_pages"] = pages
    return response


# ---------------------------------------------------------------------------
#  Read (r.jina.ai) — scrape-shaped result for one URL
# ---------------------------------------------------------------------------

async def scrape(url: str, *, mode: str = "extract") -> dict[str, Any]:
    """Fetch a URL through r.jina.ai; returns the standard scrape shape.

    extract → readability markdown (Jina's default rendering; closest to the
    browser/fast-path extract modes). fetch → raw HTML + text. The caller
    tags the result with ``transport: "jina"`` (mirrors the fast path).
    """
    if not JINA_ENABLED:
        raise JinaError("Jina integration is disabled (JINA_ENABLED=false)", bridge_status=503)
    headers = {"Accept": "application/json"}
    if mode == "fetch":
        headers["X-Respond-With"] = "html"
    status_code, data = await _request_json("GET", f"{JINA_READER_URL}/{url}", headers=headers)
    _raise_for_error(status_code, data)

    page = data.get("data") or {}
    title = (page.get("title") or "").strip()
    http_status = page.get("httpStatus")
    status = http_status if isinstance(http_status, int) else 200
    if mode == "fetch":
        html = page.get("html") or ""
        text = (page.get("text") or page.get("content") or "").strip()
        if not text and html:
            text = BeautifulSoup(html, "html.parser").get_text("\n")
        return {
            "url": url,
            "title": title,
            "text": text,
            "html": html or (page.get("content") or ""),
            "status": status,
        }
    markdown = (page.get("content") or "").strip()[:MAX_MARKDOWN_CHARS]
    return {
        "url": url,
        "title": title,
        "markdown": markdown,
        "tables": [],  # tables are inline in the GFM markdown
        "status": status,
    }
