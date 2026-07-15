"""MCP server — exposes search + scrape as Model Context Protocol tools.

Run standalone:
    python -m bridge.mcp_server

Or add to an MCP client config:
    { "mcpServers": { "web-scrape": { "command": "python", "args": ["-m", "bridge.mcp_server"] } } }

Tools exposed:
    search_web        — search via SearXNG (70+ engines)
    scrape_url        — scrape a URL via Fortress stealth browser
    search_and_scrape — search + scrape top results (Exa-style)
    crawl_site        — crawl a whole site
    fortress_search   — web search via Fortress stealth browser (not SearXNG)
"""

from __future__ import annotations

import asyncio
import logging

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .searxng_client import search as searxng_search
from .fortress_client import (
    scrape as fortress_scrape,
    crawl_site as fortress_crawl,
    search_web as fortress_web_search,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

server = Server("web-scrape-bridge")

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
            result = await searxng_search(
                arguments["query"],
                categories=arguments.get("categories"),
                language=arguments.get("language", "en"),
                max_results=arguments.get("max_results", 10),
                time_range=arguments.get("time_range"),
            )
            return [TextContent(type="text", text=_format_search_results(result))]

        elif name == "scrape_url":
            result = await fortress_scrape(arguments["url"], mode=arguments.get("mode", "extract"))
            return [TextContent(type="text", text=_format_scrape_result(result))]

        elif name == "search_and_scrape":
            search_data = await searxng_search(
                arguments["query"],
                categories=arguments.get("categories"),
                language=arguments.get("language", "en"),
                max_results=arguments.get("max_results", 5),
            )
            urls = [r["url"] for r in search_data.get("results", []) if r.get("url")]

            async def scrape_one(r: dict) -> dict:
                try:
                    content = await fortress_scrape(r["url"], mode=arguments.get("scrape_mode", "extract"))
                    return {**r, "content": content}
                except Exception as exc:
                    return {**r, "content": None, "scrape_error": str(exc)}

            scraped = await asyncio.gather(*[scrape_one(r) for r in search_data.get("results", [])]) if urls else []
            return [TextContent(type="text", text=_format_combined_results(arguments["query"], scraped))]

        elif name == "crawl_site":
            result = await fortress_crawl(
                arguments["url"],
                depth=arguments.get("depth", 2),
                max_pages=arguments.get("max_pages", 50),
            )
            return [TextContent(type="text", text=_format_crawl_result(result))]

        elif name == "fortress_search":
            result = await fortress_web_search(
                arguments["query"],
                count=arguments.get("count", 10),
            )
            return [TextContent(type="text", text=_format_search_results(result))]

        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

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


def _format_combined_results(query: str, results: list[dict]) -> str:
    lines = [f"## Search + Scrape Results for: \"{query}\"\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"### {i}. {r.get('title', 'Untitled')}")
        lines.append(f"   URL: {r.get('url', '')}")
        if r.get("content") and r["content"].get("markdown"):
            lines.append(f"   {r['content']['markdown'][:500]}")
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
#  Entrypoint
# ---------------------------------------------------------------------------

async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
