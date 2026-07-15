# Web Scrape Stack — SearXNG + Tilion Fortress

A self-hosted, Podman-based search-and-scrape stack that combines:

- **SearXNG** — privacy metasearch engine aggregating 70+ search engines (Google, Bing, DuckDuckGo, Brave, etc.) with a JSON API
- **Tilion Fortress** — stealth Chromium engine that bypasses Cloudflare, DataDome, PerimeterX, Akamai, and other bot detection
- **Bridge** — a FastAPI + MCP service that orchestrates both into a unified API (like Exa, but self-hosted and free)

```
┌─────────────────────────────────────────────────────┐
│                   Your App / AI Agent                │
│              (REST API or MCP tools)                 │
└──────────────┬──────────────────────┬───────────────┘
               │                      │
        REST ( :8000 )          MCP HTTP ( :9100 )
               │                      │
┌──────────────▼──────────────────────▼───────────────┐
│                    Bridge Service                     │
│  /search            → SearXNG JSON API                │
│  /scrape            → Fortress stealth browser        │
│  /search_and_scrape → SearXNG + Fortress (Exa-style)  │
│  /crawl             → Fortress site crawler           │
│  /web_search        → Fortress browser search         │
└───────┬──────────────────────┬───────────────┘
        │                              │
┌───────▼──────────┐         ┌────────▼────────┐
│     SearXNG      │         │    Fortress     │
│   ( :8888 )      │         │   ( :9222 CDP)  │
│  70+ engines     │         │  stealth Chrome │
└───────┬──────────┘         └─────────────────┘
        │
┌───────▼──────────┐
│     Valkey       │
│  (rate limiting)  │
└──────────────────┘
```

## Quick Start

### Prerequisites

- [Podman](https://podman.io/) 4.x+ with `podman compose` (or `podman-compose`)
- ~2 GB RAM (SearXNG ~512 MB, Fortress ~850 MB, Bridge ~128 MB)

### 1. Configure

```bash
cp .env.example .env
# Generate a secret key for SearXNG
echo "SEARXNG_SECRET_KEY=$(openssl rand -hex 32)" >> .env
```

### 2. Launch

```bash
podman compose up -d
```

This starts five containers:

| Service    | Port  | Purpose                                      |
|------------|-------|----------------------------------------------|
| SearXNG    | 8888  | Metasearch web UI + JSON API                 |
| Valkey     | —     | Redis-compatible cache for SearXNG           |
| Fortress   | 9222  | Stealth Chromium (CDP endpoint)              |
| Bridge     | 8000  | Unified REST API (FastAPI)                   |
| MCP        | 9100  | SSE MCP server for AI agents                 |

### 3. Verify

```bash
# Check all services
curl http://localhost:8000/health

# Search the web (SearXNG)
curl 'http://localhost:8000/search?q=podman+tutorial&max_results=3'

# Scrape a bot-protected page (Fortress)
curl -X POST http://localhost:8000/scrape \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://example.com", "mode": "extract"}'

# Search + scrape in one call (Exa-style)
curl -X POST http://localhost:8000/search_and_scrape \
  -H 'Content-Type: application/json' \
  -d '{"query": "rust async programming", "max_results": 3}'
```

Interactive API docs at `http://localhost:8000/docs`.

## REST API

### `GET /search`

Search the web via SearXNG (70+ engines aggregated).

| Parameter     | Type   | Default | Description                          |
|---------------|--------|---------|--------------------------------------|
| `q`           | string | —       | Search query (required)              |
| `categories`  | string | —       | Comma-separated: `general,it,images` |
| `language`    | string | `en`    | Language code                        |
| `pageno`      | int    | `1`     | Page number                          |
| `time_range`  | string | —       | `day`, `month`, or `year`            |
| `safesearch`  | int    | `0`     | 0=off, 1=moderate, 2=strict          |
| `max_results` | int    | `10`    | Truncate to N results                |

### `POST /scrape`

Scrape a single URL through the Fortress stealth browser. Bypasses Cloudflare, DataDome, PerimeterX, Akamai.

```json
{"url": "https://protected-site.com", "mode": "extract"}
```

- `mode: "extract"` — clean markdown + tables (default)
- `mode: "fetch"` — raw HTML + text

### `POST /search_and_scrape`

Search via SearXNG, then scrape each result URL through Fortress concurrently. This is the primary Exa-style endpoint.

```json
{"query": "best practices for kubernetes security", "max_results": 5, "scrape_mode": "extract"}
```

### `GET /crawl`

Crawl a whole website via Fortress (auto-handles SPA/JS + lazy-load).

| Parameter  | Type | Default | Description           |
|------------|------|---------|-----------------------|
| `url`      | str  | —       | Root URL (required)   |
| `depth`    | int  | `2`     | Crawl depth (1–5)     |
| `max_pages`| int  | `50`    | Max pages (1–200)     |

### `GET /web_search`

Web search through the Fortress stealth browser (real browser search, not SearXNG). Useful when SearXNG engines are rate-limited.

### `GET /health`

Check status of SearXNG and Fortress.

## MCP Server (for AI Agents)

The stack includes a dedicated MCP container (`ws-mcp`) that exposes the search and scrape tools over the MCP Streamable HTTP transport on port 9100. It calls the bridge REST API internally — no local Python or modules required. opencode, Claude Desktop, Cursor, and any MCP-compatible client can connect to it as a remote MCP server.

| Service | Port  | Purpose                                      |
|---------|-------|----------------------------------------------|
| MCP     | 9100  | MCP server (Streamable HTTP) for AI agents   |

### Add to opencode

Copy `opencode.jsonc.example` to `opencode.jsonc` (or merge into your existing config):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "web-scrape": {
      "type": "remote",
      "url": "http://localhost:9100/mcp",
      "enabled": true
    }
  }
}
```

### Add to Claude Desktop

```json
{
  "mcpServers": {
    "web-scrape": {
      "url": "http://localhost:9100/mcp"
    }
  }
}
```

### MCP Tools

| Tool                 | Description                                              |
|----------------------|----------------------------------------------------------|
| `search_web`         | Search via SearXNG (70+ engines)                         |
| `scrape_url`         | Scrape a URL via Fortress stealth browser                |
| `search_and_scrape`  | Search + scrape top results (Exa-style combined)         |
| `crawl_site`         | Crawl a whole site via Fortress                          |
| `fortress_search`    | Web search via Fortress stealth browser (not SearXNG)    |

## Using Fortress Directly (CDP)

The Fortress container exposes a CDP endpoint on port 9222. You can connect your own Playwright, Puppeteer, or browser-use automation directly:

```python
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://localhost:9222")
    page = browser.new_page()
    page.goto("https://bot.sannysoft.com")
    page.screenshot(path="stealth-check.png")
```

```javascript
import { chromium } from "playwright";
const browser = await chromium.connectOverCDP("http://localhost:9222");
const page = await browser.newPage();
await page.goto("https://bot.sannysoft.com");
await page.screenshot({ path: "stealth-check.png" });
```

## Configuration

### Environment Variables

| Variable                | Default                  | Description                          |
|-------------------------|--------------------------|--------------------------------------|
| `SEARXNG_SECRET_KEY`    | (required)               | SearXNG session encryption key       |
| `SEARXNG_URL`           | `http://searxng:8080`    | SearXNG URL (container-internal)     |
| `FORTRESS_CDP_URL`      | `http://fortress:9222`   | Fortress CDP endpoint                |
| `BRIDGE_HOST`           | `0.0.0.0`                | Bridge listen host                   |
| `BRIDGE_PORT`           | `8000`                   | Bridge listen port                   |
| `FORTRESS_TIMEOUT`      | `60`                     | Scrape timeout (seconds)             |
| `TILION_PROXY`          | —                        | Residential proxy for Fortress       |
| `TILION_REGION`         | —                        | Egress region hint (`us`, `eu`, etc.)|
| `CAPTCHA_API_KEY`       | —                        | 2captcha/capsolver key for CAPTCHAs  |
| `CAPTCHA_PROVIDER`      | `2captcha`               | `2captcha` or `anticaptcha` or `capsolver` |

### SearXNG

Edit `searxng/settings.yml` to:
- Enable/disable specific search engines
- Change default language or safe search level
- Adjust rate limiting
- Add outgoing proxies

Restart after changes: `podman compose restart searxng`

### Fortress Proxy (for hard targets)

If sites block your datacenter IP, route Fortress through a residential proxy:

```bash
# In .env
TILION_PROXY=http://user:pass@residential-proxy:port
TILION_REGION=us
```

Then verify egress: `curl http://localhost:8000/health` and check the Fortress status.

## Project Structure

```
web-scraping/
├── podman-compose.yml          # 5 services: valkey, searxng, fortress, bridge, mcp
├── .env.example                # environment variable template
├── opencode.jsonc.example      # MCP config template (copy to opencode.jsonc)
├── searxng/
│   ├── settings.yml            # SearXNG config (JSON API enabled, 70+ engines)
│   └── limiter.toml            # rate limiter config
├── bridge/
│   ├── Dockerfile              # Python 3.12 + FastAPI + Playwright
│   ├── pyproject.toml          # dependencies
│   └── bridge/
│       ├── __init__.py
│       ├── main.py             # FastAPI REST API
│       ├── searxng_client.py   # SearXNG JSON API client
│       └── fortress_client.py  # Tilion Fortress CDP client (Playwright over CDP)
└── mcp/
    ├── Dockerfile              # Python 3.12 + mcp + httpx (lightweight)
    ├── requirements.txt        # mcp, httpx, starlette, uvicorn
    └── server.py               # SSE MCP server (calls bridge REST API)
```

## Commands

```bash
# Start everything
podman compose up -d

# View logs
podman compose logs -f bridge
podman compose logs -f mcp
podman compose logs -f searxng
podman compose logs -f fortress

# Stop
podman compose down

# Rebuild the bridge after code changes
podman compose up -d --build bridge

# Rebuild the MCP server after code changes
podman compose up -d --build mcp

# Update SearXNG
podman compose pull searxng && podman compose up -d searxng

# Update Fortress
podman compose pull fortress && podman compose up -d fortress
```

## How It Works

1. **Search**: The bridge sends a query to SearXNG's `/search?format=json` endpoint. SearXNG aggregates results from 70+ engines (Google, Bing, DuckDuckGo, Brave, etc.) and returns JSON with titles, URLs, and snippets.

2. **Scrape**: The bridge connects to the Fortress container over CDP (`http://fortress:9222`). Fortress is a recompiled Chromium that corrects the browser fingerprint in C++ (canvas, WebGL, audio, fonts, navigator — 34 patches), so bot detectors (Cloudflare, DataDome, PerimeterX, Akamai) read it as a normal Chrome install. The `tilion` Python library drives the browser to fetch/extract pages.

3. **Search + Scrape**: The Exa-style combined endpoint searches via SearXNG, then scrapes each result URL through Fortress concurrently — giving you search results with full page content in one call.

## Troubleshooting

### SearXNG returns 403 on JSON API

Ensure `json` is in the `search.formats` list in `searxng/settings.yml` (it is by default in this config). Restart: `podman compose restart searxng`.

### Fortress container won't start

The Fortress image is ~300 MB. On first pull it takes a while. Check: `podman compose logs fortress`. If you see sandbox errors, the `--no-sandbox` flag is already set in the compose file.

### Scraping still blocked

~90% of remaining blocks are IP reputation, not fingerprint. Use a residential proxy (`TILION_PROXY` env var). For CAPTCHA-protected sites, set `CAPTCHA_API_KEY`.

### Bridge can't connect to Fortress

Verify the Fortress CDP endpoint: `curl http://localhost:9222/json/version`. It should return JSON with Chrome version info. If not, check `podman compose logs fortress`.

### Port conflicts

Change the host port mappings in `podman-compose.yml`:
```yaml
ports:
  - "9088:8080"   # SearXNG
  - "9223:9222"   # Fortress
  - "9000:8000"   # Bridge
```

## License

This stack combines:
- **SearXNG** — AGPL-3.0
- **Tilion Fortress** — BSD-3-Clause
- **Bridge** — MIT
