"""MCP server (Streamable HTTP) — exposes search + scrape tools over HTTP.

Runs inside a container on port 9100. opencode connects as a remote MCP:

    { "mcp": { "web-scrape": { "type": "remote", "url": "http://localhost:9100/mcp" } } }

Calls the bridge REST API at http://bridge:8000 — no heavy deps, just httpx + mcp.
Uses the Streamable HTTP transport (the MCP standard since protocol version 2025-03-26),
which replaces the deprecated HTTP+SSE transport.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://bridge:8000")
HTTP_TIMEOUT = float(os.environ.get("BRIDGE_TIMEOUT", "120"))
MCP_PORT = int(os.environ.get("MCP_PORT", "9100"))

server = Server("web-scrape-bridge")


# ---------------------------------------------------------------------------
#  Bridge REST API client
# ---------------------------------------------------------------------------

async def _api_get(path: str, **params) -> dict:
    # Drop empty-string/None params so FastAPI's pattern validators don't reject them
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.get(f"{BRIDGE_URL}{path}", params=clean)
        resp.raise_for_status()
        return resp.json()


async def _api_post(path: str, body: dict) -> dict:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(f"{BRIDGE_URL}{path}", json=body)
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
#  MCP tools
# ---------------------------------------------------------------------------

TOOLS: list[Tool] = [
    Tool(
        name="search_web",
        description="Search the web via SearXNG (aggregates 70+ search engines: Google, Bing, DuckDuckGo, Brave, etc.). Returns titles, URLs, snippets, and metadata.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query string"},
                "categories": {"type": "string", "description": "Comma-separated categories: general, it, images, news, etc."},
                "language": {"type": "string", "description": "Language code (e.g. 'en', 'all')", "default": "en"},
                "max_results": {"type": "integer", "description": "Max results to return", "default": 10},
                "time_range": {"type": "string", "enum": ["day", "month", "year"], "description": "Time range filter"},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="scrape_url",
        description="Scrape a URL through the Fortress stealth Chromium browser. Bypasses Cloudflare, DataDome, PerimeterX, Akamai, and other bot detection. Returns clean markdown (extract mode) or raw HTML + text (fetch mode).",
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
        description="Search the web via SearXNG, then scrape each result URL through Fortress for full page content. This is the Exa-style combined endpoint: get search results with full page markdown. Results are scraped concurrently.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "categories": {"type": "string", "description": "SearXNG categories"},
                "language": {"type": "string", "default": "en"},
                "max_results": {"type": "integer", "description": "How many results to scrape (default 5)", "default": 5},
                "scrape_mode": {"type": "string", "enum": ["extract", "fetch"], "default": "extract"},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="crawl_site",
        description="Crawl a whole website via Fortress (auto-handles SPA/JavaScript and lazy-loading). Returns all discovered pages and a sitemap.",
        inputSchema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Root URL to crawl"},
                "depth": {"type": "integer", "description": "Crawl depth (1 = just the given page)", "default": 2},
                "max_pages": {"type": "integer", "description": "Max pages to collect", "default": 50},
            },
            "required": ["url"],
        },
    ),
    Tool(
        name="fortress_search",
        description="Web search through the Fortress stealth browser (real browser search, not SearXNG). Useful when SearXNG engines are rate-limited or you need SERP results that look fully human.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "count": {"type": "integer", "description": "Number of results", "default": 10},
            },
            "required": ["query"],
        },
    ),
]


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    logger.info("MCP tool called: %s args=%s", name, arguments)

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
            return [TextContent(type="text", text=_format_search_results(result))]

        elif name == "scrape_url":
            result = await _api_post("/scrape", {
                "url": arguments["url"],
                "mode": arguments.get("mode", "extract"),
            })
            return [TextContent(type="text", text=_format_scrape_result(result))]

        elif name == "search_and_scrape":
            result = await _api_post("/search_and_scrape", {
                "query": arguments["query"],
                "categories": arguments.get("categories"),
                "language": arguments.get("language", "en"),
                "max_results": arguments.get("max_results", 5),
                "scrape_mode": arguments.get("scrape_mode", "extract"),
            })
            return [TextContent(type="text", text=_format_combined_results(result))]

        elif name == "crawl_site":
            result = await _api_get(
                "/crawl",
                url=arguments["url"],
                depth=arguments.get("depth", 2),
                max_pages=arguments.get("max_pages", 50),
            )
            return [TextContent(type="text", text=_format_crawl_result(result))]

        elif name == "fortress_search":
            result = await _api_get(
                "/web_search",
                q=arguments["query"],
                count=arguments.get("count", 10),
            )
            return [TextContent(type="text", text=_format_search_results(result))]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except httpx.HTTPStatusError as exc:
        logger.exception("Tool %s HTTP error", name)
        return [TextContent(type="text", text=f"Error in {name}: {exc.response.status_code} {exc.response.text[:500]}")]
    except Exception as exc:
        logger.exception("Tool %s failed", name)
        return [TextContent(type="text", text=f"Error in {name}: {exc}")]


# ---------------------------------------------------------------------------
#  Formatting helpers
# ---------------------------------------------------------------------------

def _format_search_results(result: dict) -> str:
    lines = [f"## Search Results ({result.get('number_of_results', '?')} total)\n"]
    for i, r in enumerate(result.get("results", []), 1):
        lines.append(f"### {i}. {r.get('title', 'Untitled')}")
        lines.append(f"   URL: {r.get('url', '')}")
        if r.get("content"):
            lines.append(f"   {r['content'][:300]}")
        if r.get("engine"):
            lines.append(f"   Engine: {r['engine']}")
        lines.append("")
    return "\n".join(lines)


def _format_scrape_result(result: dict) -> str:
    lines = [f"## Scraped: {result.get('url', '')}\n"]
    if result.get("title"):
        lines.append(f"**Title:** {result['title']}\n")
    if result.get("markdown"):
        lines.append(result["markdown"][:5000])
    elif result.get("text"):
        lines.append(result["text"][:5000])
    if result.get("tables"):
        lines.append(f"\n\n**Tables:** {len(result['tables'])} table(s) extracted")
    return "\n".join(lines)


def _format_combined_results(result: dict) -> str:
    query = result.get("query", "")
    results = result.get("results", [])
    lines = [f'## Search + Scrape Results for: "{query}"\n']
    for i, r in enumerate(results, 1):
        lines.append(f"### {i}. {r.get('title', 'Untitled')}")
        lines.append(f"   URL: {r.get('url', '')}")
        content = r.get("content")
        if content and isinstance(content, dict) and content.get("markdown"):
            lines.append(f"   {content['markdown'][:500]}")
        elif r.get("scrape_error"):
            lines.append(f"   [scrape failed: {r['scrape_error']}]")
        lines.append("")
    return "\n".join(lines)


def _format_crawl_result(result: dict) -> str:
    pages = result.get("pages", [])
    lines = [f"## Crawl Results: {len(pages)} pages\n"]
    for p in pages[:20]:
        lines.append(f"- {p.get('url', '')}: {p.get('title', '')}")
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


_sessions: dict[str, Session] = {}


async def _send_json(scope, send, status: int, body: dict):
    """Send a plain JSON HTTP response (ASGI)."""
    payload = json.dumps(body).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json")],
    })
    await send({"type": "http.response.body", "body": payload})


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
            _sessions[sid] = Session(transport=transport, task=task, cm=cm)
            logger.info("MCP session created: %s", sid)
    else:
        session = _sessions.get(session_id)
        if session is None:
            await _send_json(scope, send, 404, {"error": "Session not found"})
            return
        await session.transport.handle_request(scope, receive, send)


async def handle_health(scope, receive, send):
    """Health check — returns bridge status and tool count."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{BRIDGE_URL}/health")
            bridge_status = "up" if resp.status_code == 200 else f"down ({resp.status_code})"
    except Exception:
        bridge_status = "unreachable"
    await _send_json(scope, send, 200, {"status": "ok", "bridge": bridge_status, "tools": len(TOOLS)})


# ---------------------------------------------------------------------------
#  ASGI app — minimal router (no Starlette dependency)
# ---------------------------------------------------------------------------

async def app(scope, receive, send):
    path = scope.get("path", "")
    method = scope.get("method", "")

    if path == "/mcp" and method in ("POST", "GET", "DELETE"):
        await handle_mcp(scope, receive, send)
    elif path == "/health" and method == "GET":
        await handle_health(scope, receive, send)
    else:
        await _send_json(scope, send, 404, {"error": "not found"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=MCP_PORT, log_level="info")
