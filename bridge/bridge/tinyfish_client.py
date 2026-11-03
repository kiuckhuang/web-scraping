"""TinyFish (tinyfish.ai) search + fetch client — FREE-tier cloud transports.

Mirrors jina_client: TINYFISH_ENABLED=false by default. When enabled, TinyFish's
hosted free-tier APIs become fallbacks that only fire after every self-hosted
transport failed:

  search → GET  api.search.tinyfish.ai?query=…   only after SearXNG / browser
                                                  SERPs returned nothing
  scrape → POST api.fetch.tinyfish.ai            only after the Camoufox
                                                  browser could not fetch the
                                                  page (WAF/anti-bot blocks)

FREE-TIER ONLY: TinyFish Search (12,000 queries/day, 30/min) and Fetch
(1,000 URLs/day, 150/min) are free on every account. The wallet-metered
surfaces (agent automation, browser sessions, datasets, deep research,
monitors) are deliberately NOT implemented here — this client has no methods
for them, so the stack can never spend wallet credits implicitly.

Auth: the REST API takes the key from https://agent.tinyfish.ai/api-keys in
the ``X-API-Key`` header (NOT Bearer — that is the MCP endpoint's scheme).
There is no anonymous tier; the bridge refuses to call out without a key.
Requests are never retried here (automatic retries would burn the free daily
allowance); 429s surface the upstream ``Retry-After`` header to the caller.

Base URLs are overridable (``TINYFISH_SEARCH_URL`` / ``TINYFISH_FETCH_URL``).
Search takes no result-count parameter — the API returns a fixed page of
results, sliced to ``max_results`` client-side. Fetch rejects private IPs /
localhost / cloud-metadata endpoints server-side; the bridge validates URLs
with the same SSRF guard as /scrape before submitting them.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: str = "true") -> bool:
    """Mirror of main._env_flag (duplicated to avoid a circular import)."""
    return os.environ.get(name, default).strip().lower() not in ("", "0", "false", "no", "off")


# Master switch — default OFF: a third-party cloud API must never be contacted
# implicitly, even a free one. TINYFISH_SEARCH_FALLBACK / TINYFISH_SCRAPE_FALLBACK
# imply it.
TINYFISH_ENABLED = _env_flag("TINYFISH_ENABLED", "false")
TINYFISH_API_KEY = os.environ.get("TINYFISH_API_KEY", "").strip()
TINYFISH_TIMEOUT = float(os.environ.get("TINYFISH_TIMEOUT", "60"))
TINYFISH_SEARCH_URL = os.environ.get("TINYFISH_SEARCH_URL", "https://api.search.tinyfish.ai").strip().rstrip("/")
TINYFISH_FETCH_URL = os.environ.get("TINYFISH_FETCH_URL", "https://api.fetch.tinyfish.ai").strip().rstrip("/")
# Egress proxy for the TinyFish API calls themselves (empty = inherit the
# stack-wide EGRESS_PROXY, else direct) — same fallback rule as the fast path.
TINYFISH_PROXY = (
    os.environ.get("TINYFISH_PROXY", "").strip()
    or os.environ.get("EGRESS_PROXY", "").strip()
)
# Fallback-chain participation (each implies TINYFISH_ENABLED).
TINYFISH_SEARCH_FALLBACK = TINYFISH_ENABLED and _env_flag("TINYFISH_SEARCH_FALLBACK")
TINYFISH_SCRAPE_FALLBACK = TINYFISH_ENABLED and _env_flag("TINYFISH_SCRAPE_FALLBACK")

# The Search API has no result-count parameter — it returns a fixed page of
# results per query. The bridge slices to max_results client-side; the MCP
# tool schema caps at 20 like the sibling providers.
MAX_SEARCH_RESULTS = 20
# Only a snippet goes into the SearXNG-shaped result list (parity with the
# jina/exa/ceramic stages).
_SNIPPET_CHARS = 400
# Parity with the HTTP fast path's markdown cap.
MAX_MARKDOWN_CHARS = 50_000
# The Fetch API caps a request at 10 URLs; the bridge scrapes one URL per call.
MAX_FETCH_URLS = 10

# Reused across requests — keeps connections pooled instead of re-opening a
# TLS connection (and re-paying the handshake) per call.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        headers = {"X-API-Key": TINYFISH_API_KEY} if TINYFISH_API_KEY else {}
        _client = httpx.AsyncClient(
            timeout=TINYFISH_TIMEOUT,
            headers=headers,
            proxy=TINYFISH_PROXY or None,
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
    if not TINYFISH_ENABLED:
        return "off"
    return "ready" if TINYFISH_API_KEY else "unconfigured"


class TinyFishError(Exception):
    """TinyFish API failure, carrying the HTTP status the bridge should return."""

    def __init__(self, message: str, *, bridge_status: int = 502, retry_after: float | None = None):
        super().__init__(message)
        self.bridge_status = bridge_status
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
#  Request seam (monkeypatched in tests) + error-envelope decoding
# ---------------------------------------------------------------------------

async def _request_json(
    method: str, url: str, *, json_body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    """One TinyFish API call returning (http_status, parsed json body, headers).

    Headers are surfaced because — unlike Jina (retryAfter in the body) and
    Ceramic (retry_after_seconds in the body) — a TinyFish 429 carries its
    ``Retry-After`` as an HTTP header. This is the only place that touches
    the network; tests monkeypatch it.
    """
    resp = await _get_client().request(method, url, json=json_body)
    try:
        data: dict[str, Any] = resp.json()
    except ValueError:
        data = {"error": {"message": (resp.text or "")[:500]}}
    return resp.status_code, data, dict(resp.headers)


def _raise_for_error(status_code: int, body: dict[str, Any], headers: dict[str, str]) -> None:
    """Decode TinyFish's error envelope into a TinyFishError.

    API-level errors come back as non-2xx with
    ``{"error": {"code", "message", "details"?}}`` (401 MISSING/INVALID_API_KEY,
    400 INVALID_INPUT, 402 INSUFFICIENT_CREDITS for wallet accounts, 403 for
    legacy credit accounts, 429 rate limit).
    """
    if 200 <= status_code < 300:
        return
    error = body.get("error") or {}
    code = str(error.get("code") or "")
    message = str(error.get("message") or "") or f"HTTP {status_code}"
    if status_code == 401 or code in ("MISSING_API_KEY", "INVALID_API_KEY"):
        raise TinyFishError(f"TinyFish auth failed: {message} — check TINYFISH_API_KEY")
    if status_code == 429:
        retry_after = headers.get("retry-after")
        raise TinyFishError(
            f"TinyFish rate limit exceeded: {message}",
            bridge_status=429,
            retry_after=float(retry_after) if retry_after is not None else None,
        )
    if status_code == 402 or code == "INSUFFICIENT_CREDITS":
        raise TinyFishError(f"TinyFish wallet/billing: {message}")
    if status_code == 403:
        raise TinyFishError(f"TinyFish quota/billing: {message}")
    raise TinyFishError(f"TinyFish API error {status_code} ({code or 'unknown'}): {message}")


def _require_ready() -> None:
    if not TINYFISH_ENABLED:
        raise TinyFishError("TinyFish integration is disabled (TINYFISH_ENABLED=false)", bridge_status=503)
    if not TINYFISH_API_KEY:
        raise TinyFishError(
            "api.search.tinyfish.ai requires an API key — set TINYFISH_API_KEY in .env (https://agent.tinyfish.ai/api-keys)",
            bridge_status=503,
        )


# ---------------------------------------------------------------------------
#  Search (GET api.search.tinyfish.ai) — SearXNG-shaped results
# ---------------------------------------------------------------------------

async def search(query: str, *, max_results: int = 5) -> dict[str, Any]:
    """Search via TinyFish's free Search API; returns the SearXNG result shape.

    The API takes a keyword query and returns title/url/snippet per hit,
    mapped to the SearXNG ``content`` field. There is no result-count
    parameter — the API returns a fixed page of results, sliced here to
    ``max_results``. Raises TinyFishError — the caller decides whether that
    degrades the response.
    """
    _require_ready()
    status_code, data, headers = await _request_json(
        "GET", f"{TINYFISH_SEARCH_URL}?query={quote(query, safe='')}"
    )
    _raise_for_error(status_code, data, headers)

    results: list[dict[str, Any]] = []
    for item in data.get("results") or []:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        results.append({
            "title": (item.get("title") or "Untitled").strip(),
            "url": url,
            "content": (item.get("snippet") or "").strip()[:_SNIPPET_CHARS],
            "engine": "tinyfish",
        })
        if len(results) >= max(1, min(int(max_results), MAX_SEARCH_RESULTS)):
            break
    return {
        "query": query,
        "number_of_results": len(results),
        "results": results,
        "engine": "tinyfish",
    }


# ---------------------------------------------------------------------------
#  Fetch (POST api.fetch.tinyfish.ai) — scrape-shaped result for one URL
# ---------------------------------------------------------------------------

async def scrape(url: str, *, mode: str = "extract") -> dict[str, Any]:
    """Fetch a URL through TinyFish's free Fetch API; returns the standard scrape shape.

    extract → clean markdown (TinyFish's default output; closest to the
    browser/fast-path extract modes). fetch → semantic HTML + derived text.
    The page is fetched in TinyFish's cloud, so pages whose WAF defeats both
    our IP and our browser can still come through. The caller tags the result
    with ``transport: "tinyfish"`` (mirrors the fast path). Per-URL failures
    arrive as ``errors[]`` beside a 200 — an empty result set raises.
    """
    _require_ready()
    body: dict[str, Any] = {
        "urls": [url],
        "format": "html" if mode == "fetch" else "markdown",
    }
    status_code, data, headers = await _request_json("POST", TINYFISH_FETCH_URL, json_body=body)
    _raise_for_error(status_code, data, headers)

    if not data.get("results"):
        errors = data.get("errors") or []
        detail = "; ".join(str(e.get("error") or e) for e in errors[:3]) if errors else "no content returned"
        raise TinyFishError(f"TinyFish fetch returned no content for {url}: {detail}")
    page = (data.get("results") or [{}])[0]
    title = (page.get("title") or "").strip()
    if mode == "fetch":
        html = (page.get("text") or "").strip()
        text = BeautifulSoup(html, "html.parser").get_text("\n") if html else ""
        return {
            "url": url,
            "title": title,
            "text": text,
            "html": html,
            "status": 200,
        }
    markdown = (page.get("text") or "").strip()[:MAX_MARKDOWN_CHARS]
    return {
        "url": url,
        "title": title,
        "markdown": markdown,
        "tables": [],  # tables are inline in the markdown
        "status": 200,
    }
