"""MCP server (Streamable HTTP) — exposes search + scrape tools over HTTP.

Runs inside a container on port 9100. opencode connects as a remote MCP:

    { "mcp": { "web-scrape": { "type": "remote", "url": "http://localhost:9100/mcp" } } }

Calls the bridge REST API at http://bridge:8000 — no heavy deps, just httpx + mcp.
Uses the Streamable HTTP transport (the MCP standard since protocol version 2025-03-26),
which replaces the deprecated HTTP+SSE transport.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import time
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from mcp.server import Server
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://bridge:8000")
HTTP_TIMEOUT = float(os.environ.get("BRIDGE_TIMEOUT", "120"))
MCP_PORT = int(os.environ.get("MCP_PORT", "9100"))
MCP_LISTEN_HOST = os.environ.get("MCP_LISTEN_HOST", "0.0.0.0").strip()
MCP_PUBLIC_BIND_HOST = os.environ.get("MCP_PUBLIC_BIND_HOST", "127.0.0.1").strip()
MCP_API_KEY = os.environ.get("MCP_API_KEY", "").strip()

# Token budget for tool results (snippet / single-scrape / combined-scrape chars)
MCP_SNIPPET_CHARS = int(os.environ.get("MCP_SNIPPET_CHARS", "300"))
MCP_CONTENT_CHARS = int(os.environ.get("MCP_CONTENT_CHARS", "5000"))
MCP_COMBINED_CHARS = int(os.environ.get("MCP_COMBINED_CHARS", "1200"))

# Security knobs
MCP_SESSION_TTL = float(os.environ.get("MCP_SESSION_TTL", "1800"))  # idle session lifetime (s)
MCP_RATE_LIMIT = int(os.environ.get("MCP_RATE_LIMIT", "120"))  # requests/minute/IP, 0 = unlimited
MCP_MAX_BODY = int(os.environ.get("MCP_MAX_BODY", "1048576"))  # max request body bytes
MCP_ALLOWED_ORIGIN = os.environ.get("MCP_ALLOWED_ORIGIN", "localhost").strip()


def _is_local_bind_host(host: str) -> bool:
    """Return whether a host-side bind is local-only."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

# Masked key for logging (never leak the full key)
_key_preview = f"{MCP_API_KEY[:4]}...{MCP_API_KEY[-4:]}" if len(MCP_API_KEY) > 8 else ("set" if MCP_API_KEY else "unset")

if not MCP_API_KEY:
    logger.warning(
        "MCP_API_KEY is NOT set — authentication is DISABLED. Anyone who can reach this "
        "server can use it. Set MCP_API_KEY in .env (make init generates one)."
    )
    if not _is_local_bind_host(MCP_PUBLIC_BIND_HOST):
        raise RuntimeError(
            "MCP_API_KEY is required when MCP_PUBLIC_BIND_HOST is not localhost or a loopback address"
        )

server = Server("web-scrape-bridge")


# ---------------------------------------------------------------------------
#  Bridge REST API client — shared connection pool (no per-call clients)
# ---------------------------------------------------------------------------

_client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)


async def _api_get(path: str, **params) -> dict:
    # Drop empty-string/None params so FastAPI's pattern validators don't reject them
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    resp = await _client.get(f"{BRIDGE_URL}{path}", params=clean)
    resp.raise_for_status()
    return resp.json()


async def _api_post(path: str, body: dict) -> dict:
    resp = await _client.post(f"{BRIDGE_URL}{path}", json=body)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
#  MCP tools
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="search_web",
        description="Search the web via the configured SearXNG engines. Returns titles, URLs, and snippets.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query string"},
                "categories": {"type": "string", "description": "Comma-separated categories: general, it, images, news, etc."},
                "language": {"type": "string", "description": "Language code (e.g. 'en', 'all')", "default": "en"},
                "max_results": {"type": "integer", "description": "Max results to return (1-50)", "default": 10},
                "time_range": {"type": "string", "enum": ["day", "week", "month", "year"], "description": "Time range filter — day, week, month, or year."},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="scrape_url",
        description="Scrape a URL via Fortress stealth browser (bypasses Cloudflare, DataDome, PerimeterX, Akamai). Clean markdown (extract) or raw HTML+text (fetch).",
        inputSchema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to scrape"},
                "mode": {"type": "string", "enum": ["extract", "fetch"], "description": "'extract' = clean markdown+tables, 'fetch' = raw HTML+text", "default": "extract"},
            },
            "required": ["url"],
        },
    ),
    Tool(
        name="search_and_scrape",
        description="Search via SearXNG, then scrape the top results for full page markdown (Exa-style combined endpoint).",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "categories": {"type": "string", "description": "SearXNG categories"},
                "language": {"type": "string", "default": "en"},
                "max_results": {"type": "integer", "description": "How many results to scrape (1-50, default 3)", "default": 3},
                "scrape_mode": {"type": "string", "enum": ["extract", "fetch"], "default": "extract", "description": "'extract' = clean markdown+tables, 'fetch' = raw HTML+text"},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="crawl_site",
        description="Crawl a whole website via Fortress (SPA/JS-aware, lazy-loading handled). Returns discovered pages and a sitemap.",
        inputSchema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Root URL to crawl"},
                "depth": {"type": "integer", "description": "Crawl depth (1 = just the given page)", "default": 2},
                "max_pages": {"type": "integer", "description": "Max pages to collect (1-200)", "default": 50},
            },
            "required": ["url"],
        },
    ),
    Tool(
        name="fortress_search",
        description="Web search through the Fortress stealth browser (real-browser SERP, not SearXNG). Use when SearXNG engines are rate-limited.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "count": {"type": "integer", "description": "Number of results (1-30)", "default": 10},
            },
            "required": ["query"],
        },
    ),
]


async def _list_tools_handler(_ctx, _params):
    return ListToolsResult(tools=TOOLS)


_URL_CREDS = re.compile(r"//([^/@:]+):([^/@]+)@")
_SENSITIVE_PARAM = re.compile(r"([?&](?:token|key|secret|auth|password|api_key|apikey|signature|sig)=)[^&#]*", re.IGNORECASE)


def _redact(value: str) -> str:
    """Mask embedded credentials in URLs before they hit the logs."""
    value = _URL_CREDS.sub(r"//\1:***@", value)
    return _SENSITIVE_PARAM.sub(r"\1***", value)


def _redact_args(arguments: dict) -> dict:
    return {k: (_redact(v) if isinstance(v, str) else v) for k, v in arguments.items()}


async def _call_tool_handler(_ctx, params: CallToolRequestParams) -> CallToolResult:
    name = params.name
    arguments = params.arguments or {}
    logger.info("MCP tool called: %s args=%s", name, _redact_args(arguments))

    try:
        if name == "search_web":
            result = await _api_get(
                "/search",
                q=arguments["query"],
                categories=arguments.get("categories", ""),
                language=arguments.get("language", "en"),
                max_results=arguments.get("max_results", 10),
                time_range=arguments.get("time_range", ""),
            )
            return CallToolResult(content=[TextContent(type="text", text=_format_search_results(result))])

        elif name == "scrape_url":
            result = await _api_post("/scrape", {
                "url": arguments["url"],
                "mode": arguments.get("mode", "extract"),
            })
            return CallToolResult(content=[TextContent(type="text", text=_format_scrape_result(result))])

        elif name == "search_and_scrape":
            result = await _api_post("/search_and_scrape", {
                "query": arguments["query"],
                "categories": arguments.get("categories"),
                "language": arguments.get("language", "en"),
                "max_results": arguments.get("max_results", 3),
                "scrape_mode": arguments.get("scrape_mode", "extract"),
            })
            return CallToolResult(content=[TextContent(type="text", text=_format_combined_results(result))])

        elif name == "crawl_site":
            result = await _api_get(
                "/crawl",
                url=arguments["url"],
                depth=arguments.get("depth", 2),
                max_pages=arguments.get("max_pages", 50),
            )
            return CallToolResult(content=[TextContent(type="text", text=_format_crawl_result(result))])

        elif name == "fortress_search":
            result = await _api_get(
                "/web_search",
                q=arguments["query"],
                count=arguments.get("count", 10),
            )
            return CallToolResult(content=[TextContent(type="text", text=_format_search_results(result))])

        else:
            return CallToolResult(content=[TextContent(type="text", text=f"Unknown tool: {name}")])

    except httpx.HTTPStatusError as exc:
        logger.warning(
            "Tool %s HTTP error: %s %s",
            name,
            exc.response.status_code,
            exc.response.text[:500],
        )
        return CallToolResult(
            content=[TextContent(type="text", text=f"Error in {name}: {exc.response.status_code} {exc.response.text[:500]}")],
            is_error=True,
        )
    except Exception as exc:
        logger.exception("Tool %s failed", name)
        return CallToolResult(content=[TextContent(type="text", text=f"Error in {name}: {exc}")], is_error=True)


server.add_request_handler("tools/list", PaginatedRequestParams, _list_tools_handler)
server.add_request_handler("tools/call", CallToolRequestParams, _call_tool_handler)


# ---------------------------------------------------------------------------
#  Auth — bearer token for remote connections, local subnet bypass
# ---------------------------------------------------------------------------


def _get_trusted_cidrs() -> set[str]:
    """Compute trusted networks: loopback + this container's own /24 subnets.

    Podman forwards host connections (localhost:9100) through the bridge gateway,
    which lives on the same subnet as the container. Trusting that subnet lets
    host-side tooling (make test, local AI agents) connect without a token,
    while LAN clients on a different subnet still must authenticate.
    """
    cidrs = {"127.0.0.0/8", "::1/128"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            octets = ip.split(".")
            cidrs.add(".".join(octets[:3]) + ".0/24")
    except Exception:
        pass
    return cidrs


_TRUSTED_CIDRS = _get_trusted_cidrs()


def _is_trusted(scope) -> bool:
    """True if the request originates from a trusted (local/podman-forwarded) address."""
    client = scope.get("client")
    if not client:
        return False
    host = client[0]
    if host in ("127.0.0.1", "::1", "localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in ipaddress.ip_network(cidr, strict=False) for cidr in _TRUSTED_CIDRS)


def _auth_ok(scope) -> bool:
    """Check bearer token. Always passes for trusted sources or when no key is set."""
    if not MCP_API_KEY:
        return True
    if _is_trusted(scope):
        return True
    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    auth = headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        return hmac.compare_digest(token, MCP_API_KEY)
    return False


# ---------------------------------------------------------------------------
#  Rate limiting — sliding window per client IP
# ---------------------------------------------------------------------------

_RATE_WINDOW: dict[str, list[float]] = {}


def _rate_limit_ok(scope) -> bool:
    """True if the request is within the per-IP rate limit (MCP_RATE_LIMIT/min)."""
    global _RATE_WINDOW
    if MCP_RATE_LIMIT <= 0:
        return True
    client = scope.get("client")
    ip = client[0] if client else "unknown"
    now = time.monotonic()
    hits = [t for t in _RATE_WINDOW.get(ip, []) if now - t < 60.0]
    if len(hits) >= MCP_RATE_LIMIT:
        _RATE_WINDOW[ip] = hits
        return False
    hits.append(now)
    _RATE_WINDOW[ip] = hits
    if len(_RATE_WINDOW) > 10000:  # bound memory: prune expired entries
        _RATE_WINDOW = {
            k: fresh
            for k, v in _RATE_WINDOW.items()
            if (fresh := [t for t in v if now - t < 60.0])
        }
    return True


# ---------------------------------------------------------------------------
#  Session lifecycle — expire idle sessions so they can't leak forever
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
#  Formatting helpers
# ---------------------------------------------------------------------------

def _format_search_results(result: dict) -> str:
    """Compact format: one markdown link line per result (fewer tokens than
    heading-per-result, and the URL stays clickable)."""
    results = result.get("results", [])
    total = result.get("number_of_results", len(results))
    lines = [f"## Search Results ({total} total)", ""]
    for i, r in enumerate(results, 1):
        title = r.get("title", "Untitled")
        url = r.get("url", "")
        snippet = (r.get("content") or "").strip()
        if snippet:
            snippet = snippet[:MCP_SNIPPET_CHARS]
        line = f"{i}. [{title}]({url})"
        if snippet:
            line += f" — {snippet}"
        if r.get("engine"):
            line += f" (engine: {r['engine']})"
        lines.append(line)
    return "\n".join(lines)


def _format_scrape_result(result: dict) -> str:
    lines = [f"## Scraped: {result.get('url', '')}", ""]
    if result.get("title"):
        lines.append(f"**Title:** {result['title']}")
        lines.append("")
    body = result.get("markdown") or result.get("text") or ""
    if body:
        lines.append(body[:MCP_CONTENT_CHARS])
    if result.get("tables"):
        lines.append(f"\n**Tables:** {len(result['tables'])} table(s) extracted")
    return "\n".join(lines)


def _format_combined_results(result: dict) -> str:
    query = result.get("query", "")
    results = result.get("results", [])
    lines = [f'## Search + Scrape Results for: "{query}"', ""]
    for i, r in enumerate(results, 1):
        title = r.get("title", "Untitled")
        url = r.get("url", "")
        lines.append(f"{i}. [{title}]({url})")
        content = r.get("content")
        if isinstance(content, dict):
            # extract mode -> markdown, fetch mode -> text/html
            body = content.get("markdown") or content.get("text") or ""
            if body:
                lines.append(f"   {body[:MCP_COMBINED_CHARS]}")
        elif r.get("scrape_error"):
            lines.append(f"   [scrape failed: {r['scrape_error']}]")
        lines.append("")
    return "\n".join(lines)


def _format_crawl_result(result: dict) -> str:
    pages = result.get("pages", [])
    lines = [f"## Crawl Results: {len(pages)} pages", ""]
    for p in pages[:20]:
        lines.append(f"- [{p.get('title', '')}]({p.get('url', '')})")
    if len(pages) > 20:
        lines.append(f"\n... and {len(pages) - 20} more pages")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  Streamable HTTP transport — session management
# ---------------------------------------------------------------------------

@dataclass
class Session:
    """Holds a live MCP session: transport, background server task, and context manager."""
    transport: StreamableHTTPServerTransport
    task: asyncio.Task
    cm: Any  # the connect() async context manager
    last_seen: float  # time.monotonic() of the most recent request


_sessions: dict[str, Session] = {}
_cleanup_task: asyncio.Task | None = None


async def _sweep_sessions() -> None:
    """Close and remove sessions idle for longer than MCP_SESSION_TTL."""
    now = time.monotonic()
    expired = [sid for sid, s in _sessions.items() if now - s.last_seen > MCP_SESSION_TTL]
    for sid in expired:
        session = _sessions.pop(sid, None)
        if session is None:
            continue
        logger.info("MCP session expired after %ss idle: %s", MCP_SESSION_TTL, sid)
        session.task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(session.task), timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        try:
            await session.cm.__aexit__(None, None, None)
        except Exception:
            pass


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(max(MCP_SESSION_TTL / 2, 30))
        try:
            await _sweep_sessions()
        except Exception:
            logger.exception("Session sweep failed")


def _ensure_cleanup() -> None:
    """Start the background session-sweep task once."""
    global _cleanup_task
    if _cleanup_task is None or _cleanup_task.done():
        _cleanup_task = asyncio.create_task(_cleanup_loop())


async def _send_json(scope, send, status: int, body: dict):
    """Send a plain JSON HTTP response (ASGI)."""
    payload = json.dumps(body).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({"type": "http.response.body", "body": payload})


async def _read_body_limited(receive) -> list[dict] | None:
    """Buffer a request body while enforcing the limit for chunked requests."""
    messages: list[dict] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            messages.append(message)
            return messages
        if message["type"] != "http.request":
            messages.append(message)
            continue
        size += len(message.get("body", b""))
        if size > MCP_MAX_BODY:
            return None
        messages.append(message)
        if not message.get("more_body", False):
            return messages


def _replay_body(messages: list[dict]):
    """Return an ASGI receive callable that replays a buffered body."""
    index = 0

    async def receive():
        nonlocal index
        if index < len(messages):
            message = messages[index]
            index += 1
            return message
        return {"type": "http.request", "body": b"", "more_body": False}

    return receive


async def handle_mcp(scope, receive, send):
    """ASGI handler for the single /mcp endpoint (POST, GET, DELETE).

    Uses the MCP Streamable HTTP transport (protocol version 2025-03-26+),
    which replaces the deprecated HTTP+SSE transport.
    """
    headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
    session_id = headers.get("mcp-session-id")

    if not session_id:
        # New session — create transport, connect server, handle initialize request
        transport = StreamableHTTPServerTransport(
            mcp_session_id=uuid.uuid4().hex,
            is_json_response_enabled=True,
        )
        # Enter the connect() context manager and keep it open for the session lifetime
        cm = transport.connect()
        read_stream, write_stream = await cm.__aenter__()
        # Start the MCP server as a background task — it runs for the session lifetime
        task = asyncio.create_task(
            server.run(read_stream, write_stream, server.create_initialization_options())
        )
        # Handle the initialize request (this sends the response back via HTTP)
        await transport.handle_request(scope, receive, send)
        sid = transport.mcp_session_id or ""
        if sid:
            _sessions[sid] = Session(transport=transport, task=task, cm=cm, last_seen=time.monotonic())
            logger.info("MCP session created: %s", sid)
    else:
        session = _sessions.get(session_id)
        if session is None:
            await _send_json(scope, send, 404, {"error": "Session not found or expired"})
            return
        session.last_seen = time.monotonic()
        await session.transport.handle_request(scope, receive, send)


async def handle_health(scope, receive, send):
    """Health check — returns bridge status and tool count."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{BRIDGE_URL}/health")
            if resp.status_code == 200:
                bridge_status = "up" if resp.json().get("status") == "ok" else "degraded"
            else:
                bridge_status = f"down ({resp.status_code})"
    except Exception:
        bridge_status = "unreachable"
    status = 200 if bridge_status == "up" else 503
    await _send_json(scope, send, status, {"status": "ok" if status == 200 else "degraded", "bridge": bridge_status, "tools": len(TOOLS)})


# ---------------------------------------------------------------------------
#  CORS — allow browser-based MCP clients (llama.cpp web UI, etc.)
#  Restrict the allowed origin via MCP_ALLOWED_ORIGIN if needed.
# ---------------------------------------------------------------------------

_CORS_BASE_HEADERS = [
    (b"access-control-allow-methods", b"GET, POST, DELETE, OPTIONS"),
    (b"access-control-allow-headers", b"Content-Type, Accept, Authorization, Mcp-Session-Id, MCP-Protocol-Version"),
    (b"access-control-expose-headers", b"Mcp-Session-Id"),
    (b"access-control-max-age", b"86400"),
]


def _cors_origin(scope) -> str | None:
    """Return the request origin when it matches the configured CORS policy."""
    origin = next((value.decode() for key, value in scope.get("headers", []) if key.lower() == b"origin"), "")
    if not origin or not MCP_ALLOWED_ORIGIN:
        return None
    if MCP_ALLOWED_ORIGIN.lower() == "localhost":
        parsed = urlparse(origin)
        if parsed.scheme in {"http", "https"} and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
            return origin
        return None
    allowed = {item.strip() for item in MCP_ALLOWED_ORIGIN.split(",")}
    return origin if origin in allowed else None


def _cors_headers(scope) -> list[tuple[bytes, bytes]]:
    headers = list(_CORS_BASE_HEADERS)
    origin = _cors_origin(scope)
    if origin:
        headers.insert(0, (b"access-control-allow-origin", origin.encode()))
        headers.append((b"vary", b"Origin"))
    return headers


def _is_cors_preflight(scope) -> bool:
    return scope.get("method") == "OPTIONS"


async def _send_cors_preflight(scope, send):
    await send({
        "type": "http.response.start",
        "status": 204,
        "headers": _cors_headers(scope),
    })
    await send({"type": "http.response.body", "body": b""})


def _wrap_send_with_cors(scope, send):
    """Wrap the ASGI send callable to inject CORS headers into response start."""
    original_send = send
    cors_added = False

    async def send_with_cors(message):
        nonlocal cors_added
        if message["type"] == "http.response.start" and not cors_added:
            headers = list(message.get("headers", []))
            # Add CORS headers if not already present
            existing = {k.decode().lower() for k, _ in headers}
            for k, v in _cors_headers(scope):
                if k.decode().lower() not in existing:
                    headers.append((k, v))
            message["headers"] = headers
            cors_added = True
        await original_send(message)

    return send_with_cors


# ---------------------------------------------------------------------------
#  ASGI app — minimal router (no Starlette dependency)
# ---------------------------------------------------------------------------

async def app(scope, receive, send):
    path = scope.get("path", "")
    method = scope.get("method", "")
    rid = uuid.uuid4().hex[:8]
    client = scope.get("client")
    headers_in = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}

    # Handle CORS preflight
    if method == "OPTIONS":
        await _send_cors_preflight(scope, send)
        return

    # Wrap send to inject CORS headers into all responses
    send_with_cors = _wrap_send_with_cors(scope, send)

    if path == "/mcp" and method in ("POST", "GET", "DELETE"):
        _ensure_cleanup()
        if not _rate_limit_ok(scope):
            logger.warning("req=%s rate limited: %s %s from %s", rid, method, path, client)
            await _send_json(scope, send_with_cors, 429, {"error": "rate limit exceeded", "request_id": rid})
            return
        content_length = headers_in.get("content-length")
        if method == "POST" and content_length and int(content_length) > MCP_MAX_BODY:
            await _send_json(scope, send_with_cors, 413, {"error": "request body too large", "request_id": rid})
            return
        if not _auth_ok(scope):
            logger.warning("req=%s unauthorized: %s %s from %s", rid, method, path, client)
            await _send_json(scope, send_with_cors, 401, {"error": "unauthorized", "request_id": rid})
            return
        if method == "POST":
            body_messages = await _read_body_limited(receive)
            if body_messages is None:
                await _send_json(scope, send_with_cors, 413, {"error": "request body too large", "request_id": rid})
                return
            receive = _replay_body(body_messages)
        logger.info("req=%s %s %s from %s", rid, method, path, client)
        await handle_mcp(scope, receive, send_with_cors)
    elif path == "/health" and method == "GET":
        await handle_health(scope, receive, send_with_cors)
    else:
        await _send_json(scope, send_with_cors, 404, {"error": "not found"})


if __name__ == "__main__":
    import uvicorn
    auth_mode = "open" if not MCP_API_KEY else f"bearer-token for untrusted sources (trusted: {', '.join(sorted(_TRUSTED_CIDRS))})"
    logger.info("MCP server starting on %s:%d (auth: %s)", MCP_LISTEN_HOST, MCP_PORT, auth_mode)
    uvicorn.run(app, host=MCP_LISTEN_HOST, port=MCP_PORT, log_level="info")
