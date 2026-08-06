# Web Scrape Stack: [SearXNG](https://github.com/searxng/searxng) + [Tilion Fortress](https://github.com/tiliondev/fortress)

A self-hosted, Podman-based search-and-scrape stack that combines:

- **[SearXNG](https://github.com/searxng/searxng)** — privacy metasearch engine configured with a focused set of search engines, including Google, Bing, DuckDuckGo, Wikipedia, GitHub, Stack Overflow, Reddit, and news sources
- **[Tilion Fortress](https://github.com/tiliondev/fortress)** — stealth [Chromium](https://www.chromium.org/) engine that bypasses Cloudflare, DataDome, PerimeterX, Akamai, and other bot detection
- **Bridge** — a [FastAPI](https://fastapi.tiangolo.com/) + [Model Context Protocol](https://modelcontextprotocol.io/) service that orchestrates both into a unified API (like [Exa](https://exa.ai/), but self-hosted and free)


## Quick Start

### Prerequisites

- [Podman](https://podman.io/) 4.x+ with `podman compose` (or [podman-compose](https://github.com/containers/podman-compose))
- ~2 GB RAM (SearXNG ~512 MB, Fortress ~850 MB, Bridge ~128 MB)

```bash
make init && make up && make test
```

That's it — generates secrets, starts all services, and verifies everything works. Then point your AI agent at the MCP server (see [MCP Server](#mcp-server-for-ai-agents) below).

### Architecture

```mermaid
flowchart TB
    subgraph external["External (other machines)"]
        client["Your app or AI agent"]
    end
    subgraph edge["edge network (exposed)"]
        mcp["MCP HTTP\n127.0.0.1:9100/mcp"]
        bridge["Bridge\nFastAPI orchestration\n127.0.0.1:8000"]
    end
    subgraph internal["internal network (localhost only)"]
        searx["SearXNG\nJSON search API\n127.0.0.1:8888"]
        fortress["Tilion Fortress\nChromium over CDP\n127.0.0.1:9222"]
        valkey["Valkey\ncache and limiter"]
    end

    client --> mcp
    mcp --> bridge
    bridge -->|search| searx
    bridge -->|scrape, crawl, browser search| fortress
    searx --> valkey
```

> **Security model:** Core services and MCP bind to `127.0.0.1` on the host by default. Set `MCP_BIND_HOST` to a non-loopback address only when remote access is intentional; a non-empty `MCP_API_KEY` is required in that mode. MCP is on the `edge` network only; it can talk to Bridge but cannot reach SearXNG, Fortress, or Valkey directly.

### Request Flow

```mermaid
sequenceDiagram
    participant A as App or AI agent
    participant M as MCP server
    participant B as Bridge
    participant S as SearXNG
    participant F as Fortress

    A->>M: search_web or search_and_scrape
    M->>B: HTTP request
    B->>S: /search?format=json
    S-->>B: ranked URLs and snippets
    opt search_and_scrape
        par each result URL
            B->>F: CDP page navigation
            F-->>B: HTML or extracted content
        end
    end
    B-->>M: JSON response
    M-->>A: MCP tool result
```

### Services

| Service    | Port (bind)  | Purpose                                      |
|------------|--------------|----------------------------------------------|
| [SearXNG](https://github.com/searxng/searxng) | 8888 (127.0.0.1) | Metasearch web UI + JSON API             |
| [Valkey](https://github.com/valkey-io/valkey) | —     | Redis-compatible cache for SearXNG           |
| [Fortress](https://github.com/tiliondev/fortress) | 9222 (127.0.0.1) | Stealth Chromium (CDP endpoint)       |
| Bridge     | 8000 (127.0.0.1) | Unified REST API ([FastAPI](https://fastapi.tiangolo.com/)) |
| MCP        | 9100 (127.0.0.1 by default) | Streamable HTTP server for AI agents ([MCP](https://modelcontextprotocol.io/)) |

Interactive API docs at `http://localhost:8000/docs`.

### Manual Testing (troubleshooting)

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

## REST API

### `GET /search`

Search the web via the configured [SearXNG](https://github.com/searxng/searxng) engines.

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

Scrape a single URL through the [Fortress](https://github.com/tiliondev/fortress) stealth browser. Bypasses [Cloudflare](https://www.cloudflare.com/), [DataDome](https://datadome.co/), [PerimeterX](https://www.perimeterx.com/), and [Akamai](https://www.akamai.com/).

```json
{"url": "https://protected-site.com", "mode": "extract"}
```

- `mode: "extract"` — clean markdown + tables (default)
- `mode: "fetch"` — raw HTML + text

### `POST /search_and_scrape`

Search via [SearXNG](https://github.com/searxng/searxng), then scrape each result URL through [Fortress](https://github.com/tiliondev/fortress) concurrently. This is the primary Exa-style endpoint.

```json
{"query": "best practices for kubernetes security", "max_results": 5, "scrape_mode": "extract"}
```

### `GET /crawl`

Crawl a whole website via [Fortress](https://github.com/tiliondev/fortress) (auto-handles SPA/JS + lazy-load).

| Parameter  | Type | Default | Description           |
|------------|------|---------|-----------------------|
| `url`      | str  | —       | Root URL (required)   |
| `depth`    | int  | `2`     | Crawl depth (1–5)     |
| `max_pages`| int  | `50`    | Max pages (1–200)     |

### `GET /web_search`

Web search through the [Fortress](https://github.com/tiliondev/fortress) stealth browser (real browser search, not SearXNG). Useful when SearXNG engines are rate-limited.

### `GET /health`

Check status of SearXNG and Fortress.

## MCP Server (for AI Agents)

The stack includes a dedicated MCP container (`ws-mcp`) that exposes the search and scrape tools over the [MCP](https://modelcontextprotocol.io/) Streamable HTTP transport on port 9100. It calls the bridge REST API internally — no local Python or modules required. [OpenCode](https://opencode.ai/), [Claude Desktop](https://claude.ai/download), [Cursor](https://www.cursor.com/), and any MCP-compatible client can connect to it as a remote MCP server.

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

For a **remote** connection (different machine), add the `Authorization` header:

```jsonc
{
  "mcp": {
    "web-scrape": {
      "type": "remote",
      "url": "http://YOUR_HOST:9100/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_API_KEY"
      }
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

For a **remote** connection, include the token:

```json
{
  "mcpServers": {
    "web-scrape": {
      "url": "http://YOUR_HOST:9100/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_API_KEY"
      }
    }
  }
}
```

> **Auth model:** Localhost (and podman-forwarded host connections) bypass auth automatically based on the container's trusted subnet. Remote clients must send `Authorization: Bearer <MCP_API_KEY>`. Set the key in `.env` (`MCP_API_KEY`) — `make init` generates one automatically.

### MCP Tools

| Tool                 | Description                                              |
|----------------------|----------------------------------------------------------|
| `search_web`         | Search via the configured [SearXNG](https://github.com/searxng/searxng) engines |
| `scrape_url`         | Scrape a URL via [Fortress](https://github.com/tiliondev/fortress) stealth browser |
| `search_and_scrape`  | Search + scrape top results (Exa-style combined)         |
| `crawl_site`         | Crawl a whole site via [Fortress](https://github.com/tiliondev/fortress) |
| `fortress_search`    | Web search via [Fortress](https://github.com/tiliondev/fortress) stealth browser (not SearXNG) |

## Using Fortress Directly (CDP)

The [Fortress](https://github.com/tiliondev/fortress) container exposes a CDP endpoint on port 9222. You can connect your own [Playwright](https://playwright.dev/), [Puppeteer](https://pptr.dev/), or [browser-use](https://github.com/browser-use/browser-use) automation directly:

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
| `SEARXNG_SECRET_KEY`    | (auto-generated)         | SearXNG session encryption key       |
| `SEARXNG_CHANNEL`       | `2026.8.4-c63835bd2`    | SearXNG image tag (change deliberately when updating) |
| `SEARXNG_URL`           | `http://searxng:8080`    | SearXNG URL (container-internal)     |
| `SEARXNG_REQUEST_TIMEOUT` | `10`                   | Outgoing request timeout (s) per engine |
| `SEARXNG_MAX_REQUEST_TIMEOUT` | `15`              | Max allowed request timeout (s)     |
| `SEARXNG_BAN_TIME_ON_FAIL` | `5`                   | Engine ban duration (s) after a failed request |
| `SEARXNG_MAX_BAN_TIME_ON_FAIL` | `120`            | Upper cap on the engine ban (s)     |
| `SEARXNG_SUSPEND_TOO_MANY` | `180`                | How long an engine is suspended (s) after a 429 / rate-limit |
| `SEARXNG_FORCE_OWNERSHIP` | `true`                | Force SearXNG file ownership on start |
| `SEARXNG_UWSGI_THREADS` | `4`                    | SearXNG worker thread count          |
| `FORTRESS_CDP_URL`      | `http://fortress:9222`   | Fortress CDP endpoint                |
| `FORTRESS_CHANNEL`      | `latest`                 | Fortress image channel (`stable` or `latest`) |
| `FORTRESS_TZ`           | host `TZ`                | Browser timezone override             |
| `FORTRESS_LANG`         | host `LANG`              | Browser language override             |
| `FORTRESS_PROFILE_DIR`  | `./fortress-profile`     | Persistent host directory for Chromium profile |
| `FORTRESS_SHM_SIZE`     | `2gb`                    | Fortress shared memory size |
| `FORTRESS_NAV_DELAY`    | `400`                    | Post-navigation pause (ms) for JS/SERP pages before extraction |
| `BRIDGE_HOST`           | `0.0.0.0`                | Bridge listen address (internal container) |
| `BRIDGE_PORT`           | `8000`                   | Bridge listen port (internal container) |
| `BRIDGE_CACHE_TTL`      | `300`                    | Scrape cache TTL (s) — repeat scrapes of the same URL skip the browser |
| `BRIDGE_CACHE_MAX`      | `100`                    | Max pages held in the scrape cache |
| `BRIDGE_CACHE_MAX_BYTES` | `26214400`              | Max serialized scrape-cache size (25 MiB) |
| `FORTRESS_TIMEOUT`       | `60`                    | Browser navigation timeout (s) |
| `FORTRESS_NAV_WAIT`      | `domcontentloaded`      | Browser navigation wait condition |
| `FORTRESS_MAX_CONCURRENT_PAGES` | `3`              | Maximum concurrent browser pages |
| `FORTRESS_WAF_WAIT`      | `15`                    | Maximum WAF challenge wait (s) |
| `FORTRESS_ISOLATE_CONTEXTS` | `true`                | Isolate cookies/storage for each request |
| `BRIDGE_URL`            | `http://bridge:8000`     | Bridge URL used by MCP (container-internal) |
| `PORT_SEARXNG`          | `8888`                   | Host port for SearXNG                |
| `PORT_FORTRESS`         | `9222`                   | Host port for Fortress CDP           |
| `PORT_BRIDGE`           | `8000`                   | Host port for Bridge REST API        |
| `PORT_MCP`              | `9100`                   | Host port for MCP server             |
| `MCP_API_KEY`           | (auto-generated)         | Bearer token for remote MCP clients (localhost bypasses auth) |
| `MCP_BIND_HOST`         | `127.0.0.1`              | Host address published for MCP; non-loopback requires `MCP_API_KEY` |
| `MCP_SESSION_TTL`       | `1800`                   | Idle MCP session lifetime (s) before expiry |
| `MCP_RATE_LIMIT`        | `120`                    | Max MCP requests/min per client IP (`0` = unlimited) |
| `MCP_MAX_BODY`          | `1048576`                | Max MCP request body size (bytes)    |
| `MCP_ALLOWED_ORIGIN`    | `localhost`              | `localhost` allows loopback hosts on any port; exact origin or empty also supported |
| `MCP_SNIPPET_CHARS`     | `300`                    | Search-result snippet length (chars) in MCP tool output |
| `MCP_CONTENT_CHARS`     | `5000`                   | Single-page scrape length (chars) in MCP tool output |
| `MCP_COMBINED_CHARS`    | `1200`                   | Per-result length (chars) in `search_and_scrape` MCP output |
| `APP_UID`               | `1000`                   | Host user UID for bridge/mcp containers |
| `APP_GID`               | `1000`                   | Host user GID for bridge/mcp containers |

### [SearXNG](https://docs.searxng.org/) Configuration

Settings are rendered from `searxng/settings.template.yml` at container start,
pulling the engine-tuning values above from `.env`. Edit the template to:
- Enable/disable specific search engines
- Change default language or safe search level
- Add outgoing proxies

The rate-limiting / engine-suspension knobs are exposed as environment
variables (see the table above) — e.g. to fail over faster after a 429:

```bash
# In .env
SEARXNG_SUSPEND_TOO_MANY=60
```

Apply changes: `podman compose up -d --force-recreate searxng` (or `make up`)

### Fortress Locale and Profile

Fortress inherits the compose process' `TZ` and `LANG` values by default. Set
`FORTRESS_TZ` or `FORTRESS_LANG` in `.env` to override them. If the host timezone is
not exported as `TZ`, set `FORTRESS_TZ` explicitly. The browser profile is persisted in
`FORTRESS_PROFILE_DIR`, allowing cookies and other profile state to survive restarts:

```bash
FORTRESS_CHANNEL=latest
FORTRESS_TZ=Asia/Hong_Kong
FORTRESS_LANG=zh-HK
FORTRESS_PROFILE_DIR=./fortress-profile
podman compose up -d
```

Stop the stack before copying or reusing the profile directory elsewhere. The bridge
uses isolated browser contexts by default. The profile is mounted at both
`/tmp/tilion-profile` (the upstream documented path) and
`/tmp/tillion-profile` (used by current image builds) for compatibility.

If the profile directory is not writable under rootless Podman, set
`FORTRESS_PROFILE_DIR` to a directory writable by the container user. Avoid making
the profile world-writable.

## Security

### Network segmentation

The stack is split into two bridge networks:

| Network   | Services                | Exposure                        |
|-----------|-------------------------|---------------------------------|
| `internal`| Valkey, SearXNG, Fortress | `127.0.0.1` only (localhost)  |
| `edge`    | Bridge, MCP             | Bridge `127.0.0.1`, MCP `127.0.0.1` by default |

- **Core services** (SearXNG, Fortress, Valkey) bind to `127.0.0.1` — accessible only from the host, not the LAN.
- **MCP** is published on localhost by default. Set `MCP_BIND_HOST` to a non-loopback address for remote clients; a non-empty `MCP_API_KEY` is mandatory then.
- MCP sits on `edge` only. It can talk to Bridge but **cannot** reach SearXNG, Fortress, or Valkey directly — compromise of MCP does not expose the core services.

### Least privilege

- Bridge and MCP run as non-root (`appuser`).
- Valkey and SearXNG drop all capabilities except `SETGID`/`SETUID`/`CHOWN`.
- SearXNG is configured with `public_instance: false` and `limiter: false` (private, internal-only).

### MCP hardening

- **Idle session expiry** — MCP sessions are garbage-collected after `MCP_SESSION_TTL` seconds of inactivity (default 30 min), so abandoned client connections cannot leak memory or background tasks forever.
- **Per-IP rate limiting** — `MCP_RATE_LIMIT` (default 120/min per IP) throttles abusive clients; excess requests get `429`. Set `0` to disable.
- **Request body limit** — requests larger than `MCP_MAX_BODY` (default 1 MB) are rejected with `413`.
- **Local-first authentication** — localhost clients may use the trusted local path. A non-loopback `MCP_BIND_HOST` requires a non-empty `MCP_API_KEY`; `make init` generates one.
- **Auth failures are logged** — every rejected request logs the client address, so "why can't my client connect" is answerable from `podman compose logs mcp`.
- **Log redaction** — tool arguments with embedded credentials (`user:pass@`, `?token=`, `?key=`...) are masked in logs.
- **Configurable CORS** — `MCP_ALLOWED_ORIGIN=localhost` allows browser clients from `localhost`, `127.0.0.1`, and `::1` on any port. Set an exact origin or empty to disable CORS.
- **Request IDs** — every MCP request gets a short `req=...` log tag (also echoed in error bodies), and the bridge logs one per REST call with an `X-Request-ID` response header, so a failing agent call can be traced across the stack.
- **Known limitation: no TLS.** The MCP endpoint serves plain HTTP (a valid trusted certificate requires operational setup, which this project deliberately avoids). For exposure beyond a trusted LAN, terminate TLS in front of `:9100` with a reverse proxy (e.g. Caddy or Traefik with automatic HTTPS) and send the bearer token over that connection.

### SSRF protection

The bridge validates all URLs passed to `/scrape`, `/crawl`, and `/search_and_scrape` — requests to private/internal networks are rejected with `403`, and hosts that cannot be resolved at validation time are rejected outright. Browser redirects and subresource requests are checked again, while crawls remain same-origin and public-only.

### Project Structure

```
web-scraping/
├── Makefile                    # build, run, test targets
├── podman-compose.yml          # 5 services: valkey, searxng, fortress, bridge, mcp
├── .env.example                # environment variable template
├── opencode.jsonc.example      # MCP config template (copy to opencode.jsonc)
├── scripts/
│   ├── doctor.sh               # make doctor — setup diagnostics
│   └── init.py                 # portable .env initialization
├── .github/workflows/ci.yml    # unit tests on every push
├── searxng/
│   ├── settings.template.yml   # SearXNG config template (focused engine set)
│   ├── render_settings.py      # renders template -> settings.yml from .env
│   └── limiter.toml            # rate limiter config
├── searxng-entrypoint.sh       # renders settings, then runs upstream entrypoint
├── bridge/
│   ├── Dockerfile              # Python 3.12 + FastAPI + Playwright
│   ├── pyproject.toml          # dependencies
│   ├── tests/                  # unit tests (make test-unit / CI)
│   └── bridge/
│       ├── __init__.py
│       ├── main.py             # FastAPI REST API
│       ├── searxng_client.py   # SearXNG JSON API client
│       └── fortress_client.py  # Tilion Fortress CDP client (Playwright over CDP)
└── mcp/
    ├── Dockerfile              # Python 3.12 + mcp + httpx (lightweight)
    ├── requirements.txt        # mcp, httpx, starlette, uvicorn
    ├── tests/                  # unit tests (make test-unit / CI)
    └── server.py               # Streamable HTTP MCP server (calls bridge REST API)
```

## Commands

### Makefile (recommended)

```bash
make init      # Create .env with UID/GID and secret key
make up        # Start all services
make build     # Build images
make test      # Unit + integration tests
make test-unit # Unit tests only (pytest inside bridge/mcp containers)
make test-scrape # Scrape smoke test through the bridge (example.com)
make doctor    # Diagnose common setup problems
make logs      # Follow logs
make rebuild   # Stop, rebuild, start
make update    # Pull latest images, rebuild custom images, restart
make down      # Stop services
make clean     # Stop, remove volumes, prune images
make help      # Show all targets (also runs with no argument)
```

### Podman Compose (manual)

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

1. **Search**: The bridge sends a query to [SearXNG](https://github.com/searxng/searxng)'s `/search?format=json` endpoint. The configured engines return JSON with titles, URLs, and snippets.

2. **Scrape**: The bridge connects to [Fortress](https://github.com/tiliondev/fortress) over CDP (`http://fortress:9222`). Fortress is a recompiled [Chromium](https://www.chromium.org/) that corrects the browser fingerprint in C++ (canvas, WebGL, audio, fonts, navigator — 34 patches), so bot detectors ([Cloudflare](https://www.cloudflare.com/), [DataDome](https://datadome.co/), [PerimeterX](https://www.perimeterx.com/), [Akamai](https://www.akamai.com/)) read it as a normal Chrome install. The [Playwright](https://playwright.dev/) client drives the browser to fetch/extract pages.

3. **Search + Scrape**: The Exa-style combined endpoint searches via [SearXNG](https://github.com/searxng/searxng), then scrapes each result URL through [Fortress](https://github.com/tiliondev/fortress) concurrently — giving you search results with full page content in one call.

## Troubleshooting

### SearXNG returns 403 on JSON API

Ensure `json` is in the `search.formats` list in `searxng/settings.template.yml` (it is by default in this config). Recreate: `podman compose up -d --force-recreate searxng`.

### MCP client gets 401

The server logs each rejected request with the client address (`podman compose logs mcp`). Check that:
1. Your client sends `Authorization: Bearer <MCP_API_KEY>` (see `.env`).
2. The client isn't on a different subnet than the server — only localhost and the podman-forwarded subnet bypass auth.
3. `make init` was run, so the key isn't the placeholder.

### MCP client gets 429

Per-IP rate limit exceeded (`MCP_RATE_LIMIT`, default 120/min). Raise it in `.env` and recreate the mcp container, or check whether something is hammering the endpoint.

### MCP client gets "Session not found or expired"

Sessions are dropped after `MCP_SESSION_TTL` seconds of inactivity (default 30 min) or a server restart. MCP clients reconnect automatically with a new session — if yours doesn't, restart it or reduce the TTL.

### Correlating a failing call across logs

Both the bridge and the MCP server tag every request with a short ID:
- bridge logs `req=abc12345 GET /search -> 502` and echoes `X-Request-ID: abc12345` in the response.
- mcp logs `req=...` on each incoming request and includes `request_id` in error bodies.

Grep both services for the same ID to trace a failure: `podman compose logs bridge mcp | grep abc12345`.

### Scrape results look stale

The bridge caches scrape results for `BRIDGE_CACHE_TTL` (default 300 s, up to `BRIDGE_CACHE_MAX` pages) — a repeated scrape of the same URL within the TTL is served from memory and flagged with `"cached": true`. Set `BRIDGE_CACHE_TTL=0` and recreate the bridge to disable caching.

### Fortress container won't start

The [Fortress](https://github.com/tiliondev/fortress) image is ~300 MB. On first pull it takes a while. Check: `podman compose logs fortress`. If you see sandbox errors, the `--no-sandbox` flag is already set in the compose file.

### Scraping still blocked

Remaining blocks are commonly caused by target-site policy or IP reputation. The stack does not provide a proxy or CAPTCHA-solver integration; configure those outside this project if required.

### Bridge can't connect to Fortress

Verify the [Fortress](https://github.com/tiliondev/fortress) CDP endpoint: `curl http://localhost:9222/json/version`. It should return JSON with Chrome version info. If not, check `podman compose logs fortress`.

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
