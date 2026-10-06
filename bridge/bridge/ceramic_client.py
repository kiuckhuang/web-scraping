"""Ceramic (ceramic.ai) search client — optional last-resort cloud transport.

Mirrors exa_client: CERAMIC_ENABLED=false by default. When enabled, Ceramic's
hosted Search API (https://api.ceramic.ai/search) becomes a FINAL fallback
that only fires after every self-hosted transport failed — and after the Jina
and Exa fallbacks, which keep their positions directly before it (see
AGENTS.md):

  search → POST /search   only after SearXNG / browser SERPs / s.jina.ai /
                          api.exa.ai returned nothing

Ceramic is search-only — the API surface has no contents/reader endpoint, so
it does not join the scrape chain.

Auth and cost: credit-metered API (free tier on sign-up), key from
https://platform.ceramic.ai/keys sent as a Bearer token. There is no
anonymous tier — the bridge refuses to call out without a key. Requests are
never retried here (automatic retries would burn paid quota); 429s surface
the upstream retry_after_seconds to the caller.

Base URL is overridable (``CERAMIC_BASE_URL``) for corporate gateways. This
client implements only the one call the bridge needs; nothing is vendored
from the official SDK (docs.ceramic.ai).
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
# implicitly. CERAMIC_SEARCH_FALLBACK implies it.
CERAMIC_ENABLED = _env_flag("CERAMIC_ENABLED", "false")
CERAMIC_API_KEY = os.environ.get("CERAMIC_API_KEY", "").strip()
CERAMIC_TIMEOUT = float(os.environ.get("CERAMIC_TIMEOUT", "60"))
CERAMIC_BASE_URL = os.environ.get("CERAMIC_BASE_URL", "https://api.ceramic.ai").strip().rstrip("/")
# Egress proxy for the Ceramic API calls themselves (empty = inherit the
# stack-wide EGRESS_PROXY, else direct) — same fallback rule as the fast path.
CERAMIC_PROXY = (
    os.environ.get("CERAMIC_PROXY", "").strip()
    or os.environ.get("EGRESS_PROXY", "").strip()
)
# Last-resort chain participation (implies CERAMIC_ENABLED).
CERAMIC_SEARCH_FALLBACK = CERAMIC_ENABLED and _env_flag("CERAMIC_SEARCH_FALLBACK")

# The API caps maxResults at 20 (matching the REST query bound and the MCP
# tool schema) — plenty for a fallback stage.
MAX_SEARCH_RESULTS = 20
# Only a snippet goes into the SearXNG-shaped result list (parity with the
# jina/exa stages).
_SNIPPET_CHARS = 400
# Ceramic's minimum maxDescriptionLength; we only keep _SNIPPET_CHARS anyway,
# and the description is billed content — ask for the smallest slice allowed.
_DESCRIPTION_CHARS = int(os.environ.get("CERAMIC_DESCRIPTION_CHARS", "1000"))

# Reused across requests — keeps connections pooled instead of re-opening a
# TLS connection (and re-paying the handshake) per call.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        headers = {"Authorization": f"Bearer {CERAMIC_API_KEY}"} if CERAMIC_API_KEY else {}
        _client = httpx.AsyncClient(
            timeout=CERAMIC_TIMEOUT,
            headers=headers,
            proxy=CERAMIC_PROXY or None,
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
    if not CERAMIC_ENABLED:
        return "off"
    return "ready" if CERAMIC_API_KEY else "unconfigured"


class CeramicError(Exception):
    """Ceramic API failure, carrying the HTTP status the bridge should return."""

    def __init__(self, message: str, *, bridge_status: int = 502, retry_after: float | None = None):
        super().__init__(message)
        self.bridge_status = bridge_status
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
#  Request seam (monkeypatched in tests) + error-envelope decoding
# ---------------------------------------------------------------------------

async def _request_json(method: str, url: str, *, json_body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    """One Ceramic API call returning (http_status, parsed json body).

    This is the only place that touches the network; tests monkeypatch it.
    """
    resp = await _get_client().request(method, url, json=json_body)
    try:
        data: dict[str, Any] = resp.json()
    except ValueError:
        data = {"detail": (resp.text or "")[:500]}
    return resp.status_code, data


def _raise_for_error(status_code: int, body: dict[str, Any]) -> None:
    """Decode Ceramic's RFC-7807-ish error envelope into a CeramicError.

    Errors come back as ``{"title", "status", "detail", "requestId", "code"}``
    (401 invalid key, 402 credits exhausted, 422 bad parameter, 429 rate
    limit with ``retry_after_seconds``).
    """
    if 200 <= status_code < 300:
        return
    message = str(body.get("detail") or body.get("title") or "") or f"HTTP {status_code}"
    retry_after = body.get("retry_after_seconds")
    if status_code == 401:
        raise CeramicError(f"Ceramic auth failed: {message} — check CERAMIC_API_KEY")
    if status_code == 429:
        raise CeramicError(
            f"Ceramic rate limit exceeded: {message}",
            bridge_status=429,
            retry_after=float(retry_after) if retry_after is not None else None,
        )
    if status_code == 402:
        raise CeramicError(f"Ceramic quota/billing: {message}")
    raise CeramicError(f"Ceramic API error {status_code}: {message}")


# ---------------------------------------------------------------------------
#  Search (POST /search)
# ---------------------------------------------------------------------------

async def search(query: str, *, max_results: int = 5) -> dict[str, Any]:
    """Search via Ceramic /search; returns the SearXNG result shape.

    The API takes a keyword query (1-50 words), returns title/url/description
    per hit — descriptions are page-content snippets, mapped to the SearXNG
    ``content`` field. Raises CeramicError — the caller decides whether that
    degrades the response.
    """
    if not CERAMIC_ENABLED:
        raise CeramicError("Ceramic integration is disabled (CERAMIC_ENABLED=false)", bridge_status=503)
    if not CERAMIC_API_KEY:
        raise CeramicError(
            "api.ceramic.ai requires an API key — set CERAMIC_API_KEY in .env (https://platform.ceramic.ai/keys)",
            bridge_status=503,
        )
    body: dict[str, Any] = {
        "query": query,
        "maxResults": max(1, min(int(max_results), MAX_SEARCH_RESULTS)),
        "maxDescriptionLength": _DESCRIPTION_CHARS,
    }
    status_code, data = await _request_json("POST", f"{CERAMIC_BASE_URL}/search", json_body=body)
    _raise_for_error(status_code, data)

    results: list[dict[str, Any]] = []
    for item in (data.get("result") or {}).get("results") or []:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        results.append({
            "title": (item.get("title") or "Untitled").strip(),
            "url": url,
            "content": (item.get("description") or "").strip()[:_SNIPPET_CHARS],
            "engine": "ceramic",
        })
    return {
        "query": query,
        "number_of_results": len(results),
        "results": results,
        "engine": "ceramic",
    }
