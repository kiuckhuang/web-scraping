"""Exa (exa.ai) search + contents client — optional last-resort cloud transports.

Mirrors jina_client: EXA_ENABLED=false by default. When enabled, Exa's hosted
API (https://api.exa.ai) becomes a FINAL fallback that only fires after every
self-hosted transport failed — and after Jina's fallbacks, which keep their
position directly before it (they are the pinned last resort; see AGENTS.md):

  search → POST /search     only after SearXNG / browser SERPs / s.jina.ai
                            returned nothing
  scrape → POST /contents   only after the Camoufox browser (and the Jina
                            reader) could not fetch the page

Auth and cost: the REST API requires a key from https://dashboard.exa.ai/api-keys,
sent as ``x-api-key`` (exa-js SDK parity). There is NO anonymous tier — unlike
r.jina.ai, a keyless call fails 401 before spending anything, and the bridge
refuses to call out at all without a key. Requests are never retried here
(automatic retries would burn paid quota without improving the outcome);
429s surface the upstream message to the caller.

The base URL is overridable (``EXA_BASE_URL``) for corporate gateways. This
client implements only the two calls the bridge needs (search with page text,
and contents for a single URL) — the full request/response surface lives in
the official SDK (github.com/exa-labs/exa-js) and docs.exa.ai; nothing is
vendored here.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: str = "true") -> bool:
    """Mirror of main._env_flag (duplicated to avoid a circular import)."""
    return os.environ.get(name, default).strip().lower() not in ("", "0", "false", "no", "off")


# Master switch — default OFF: a paid third-party API must never be contacted
# implicitly. EXA_SEARCH_FALLBACK / EXA_SCRAPE_FALLBACK imply it.
EXA_ENABLED = _env_flag("EXA_ENABLED", "false")
EXA_API_KEY = os.environ.get("EXA_API_KEY", "").strip()
EXA_TIMEOUT = float(os.environ.get("EXA_TIMEOUT", "60"))
EXA_BASE_URL = os.environ.get("EXA_BASE_URL", "https://api.exa.ai").strip().rstrip("/")
# Egress proxy for the Exa API calls themselves (empty = inherit the
# stack-wide EGRESS_PROXY, else direct) — same fallback rule as the fast path.
EXA_PROXY = (
    os.environ.get("EXA_PROXY", "").strip()
    or os.environ.get("EGRESS_PROXY", "").strip()
)
# Last-resort chain participation (each implies EXA_ENABLED).
EXA_SEARCH_FALLBACK = EXA_ENABLED and _env_flag("EXA_SEARCH_FALLBACK")
EXA_SCRAPE_FALLBACK = EXA_ENABLED and _env_flag("EXA_SCRAPE_FALLBACK")

# Kept at 20 (matching the REST query bound and the MCP tool schema) — plenty
# for a fallback stage, and safe against undocumented per-category caps.
MAX_SEARCH_RESULTS = 20
# Search results carry the page text; only a snippet goes into the SearXNG-shaped
# result list (the full text rides along as exa_pages, like jina_pages).
_SNIPPET_CHARS = 400
# Parity with the HTTP fast path's markdown cap.
MAX_MARKDOWN_CHARS = 50_000
# Page-text characters per result requested from the API (both /search contents
# and /contents) — matches exa-js's own default.
MAX_CHARACTERS = int(os.environ.get("EXA_MAX_CHARACTERS", "10000"))

# Reused across requests — keeps connections pooled instead of re-opening a
# TLS connection (and re-paying the handshake) per call.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        headers = {"x-api-key": EXA_API_KEY} if EXA_API_KEY else {}
        _client = httpx.AsyncClient(
            timeout=EXA_TIMEOUT,
            headers=headers,
            proxy=EXA_PROXY or None,
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
    if not EXA_ENABLED:
        return "off"
    return "ready" if EXA_API_KEY else "unconfigured"


class ExaError(Exception):
    """Exa API failure, carrying the HTTP status the bridge should return."""

    def __init__(self, message: str, *, bridge_status: int = 502, retry_after: float | None = None):
        super().__init__(message)
        self.bridge_status = bridge_status
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
#  Request seam (monkeypatched in tests) + error-envelope decoding
# ---------------------------------------------------------------------------

async def _request_json(method: str, url: str, *, json_body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    """One Exa API call returning (http_status, parsed json body).

    This is the only place that touches the network; tests monkeypatch it.
    """
    resp = await _get_client().request(method, url, json=json_body)
    try:
        data: dict[str, Any] = resp.json()
    except ValueError:
        data = {"message": (resp.text or "")[:500]}
    return resp.status_code, data


def _raise_for_error(status_code: int, body: dict[str, Any]) -> None:
    """Decode Exa's error envelope into an ExaError.

    The API returns HTTP error codes with either the current structured
    envelope ``{"error": {"type", "code", "message", "detail"}, "requestId"}``
    or the older flat ``{"requestId", "message"}`` shape — both decoded here
    (401 invalid key, 402 out of credits, 429 rate limit).
    """
    if 200 <= status_code < 300:
        return
    nested = body.get("error")
    message = ""
    if isinstance(nested, dict):
        message = str(nested.get("message") or nested.get("type") or "")
    message = message or str(body.get("message") or "") or f"HTTP {status_code}"
    retry_after = body.get("retryAfter")
    if status_code == 401:
        raise ExaError(f"Exa auth failed: {message} — check EXA_API_KEY")
    if status_code == 429:
        raise ExaError(
            f"Exa rate limit exceeded: {message}",
            bridge_status=429,
            retry_after=float(retry_after) if retry_after is not None else None,
        )
    if status_code == 402:
        raise ExaError(f"Exa quota/billing: {message}")
    raise ExaError(f"Exa API error {status_code}: {message}")


# ---------------------------------------------------------------------------
#  Search (POST /search) — SearXNG-shaped results + full page texts
# ---------------------------------------------------------------------------

async def search(
    query: str,
    *,
    max_results: int = 5,
    category: str | None = None,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
) -> dict[str, Any]:
    """Search via Exa /search; returns the SearXNG result shape plus exa_pages.

    Each result's cleaned page text is fetched in the same request (contents)
    — the SearXNG-shaped ``results`` carry only snippets, the full text rides
    along under ``exa_pages`` (consumed by /search_and_scrape, which reuses it
    instead of re-scraping; /search strips it before responding). Raises
    ExaError — the caller decides whether that degrades the response.
    """
    if not EXA_ENABLED:
        raise ExaError("Exa integration is disabled (EXA_ENABLED=false)", bridge_status=503)
    if not EXA_API_KEY:
        raise ExaError(
            "api.exa.ai requires an API key — set EXA_API_KEY in .env (https://dashboard.exa.ai/api-keys)",
            bridge_status=503,
        )
    body: dict[str, Any] = {
        "query": query,
        "numResults": max(1, min(int(max_results), MAX_SEARCH_RESULTS)),
        "contents": {"text": {"maxCharacters": MAX_CHARACTERS}},
    }
    if category:
        body["category"] = category
    if include_domains:
        body["includeDomains"] = list(include_domains)
    if exclude_domains:
        body["excludeDomains"] = list(exclude_domains)
    status_code, data = await _request_json("POST", f"{EXA_BASE_URL}/search", json_body=body)
    _raise_for_error(status_code, data)

    results: list[dict[str, Any]] = []
    pages: dict[str, dict[str, str]] = {}
    for item in data.get("results") or []:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        title = (item.get("title") or "Untitled").strip()
        text = (item.get("text") or "").strip()
        results.append({
            "title": title,
            "url": url,
            "content": text[:_SNIPPET_CHARS],
            "engine": "exa",
        })
        if text:
            pages[url] = {"title": title, "markdown": text}
    response: dict[str, Any] = {
        "query": query,
        "number_of_results": len(results),
        "results": results,
        "engine": "exa",
    }
    if pages:
        response["exa_pages"] = pages
    return response


# ---------------------------------------------------------------------------
#  Contents (POST /contents) — scrape-shaped result for one URL
# ---------------------------------------------------------------------------

async def scrape(url: str, *, mode: str = "extract") -> dict[str, Any]:
    """Fetch a URL through Exa /contents; returns the standard scrape shape.

    Exa returns cleaned page text (its "clean markdown"), never raw HTML —
    extract mode maps it to the markdown shape; fetch mode degrades to the
    same cleaned text (``html`` stays empty). The caller tags the result with
    ``transport: "exa"`` (mirrors the fast path).
    """
    if not EXA_ENABLED:
        raise ExaError("Exa integration is disabled (EXA_ENABLED=false)", bridge_status=503)
    status_code, data = await _request_json(
        "POST",
        f"{EXA_BASE_URL}/contents",
        json_body={"urls": [url], "text": {"maxCharacters": MAX_CHARACTERS}},
    )
    _raise_for_error(status_code, data)

    results = data.get("results") or []
    if not results:
        # Per-URL failures come back in "statuses" (e.g. unsupported content,
        # upstream http errors) rather than as an HTTP error — surface the tag.
        errors = [s for s in (data.get("statuses") or []) if s.get("status") == "error"]
        detail = ""
        if errors:
            err = errors[0].get("error") or {}
            detail = str(err.get("tag") or "") if isinstance(err, dict) else str(err)
        raise ExaError(f"Exa contents returned no result{f': {detail}' if detail else ''}")
    page = results[0]
    title = (page.get("title") or "").strip()
    text = (page.get("text") or "").strip()
    if mode == "fetch":
        return {
            "url": url,
            "title": title,
            "text": text,
            "html": "",  # Exa never returns raw HTML — cleaned text only
            "status": 200,
        }
    markdown = text[:MAX_MARKDOWN_CHARS]
    return {
        "url": url,
        "title": title,
        "markdown": markdown,
        "tables": [],  # tables are inline in the cleaned text
        "status": 200,
    }
