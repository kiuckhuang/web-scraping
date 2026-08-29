# AGENTS.md — web-scraping stack

Guidance for coding agents working in this repo. Read the README first for the
architecture (SearXNG + Camoufox + Bridge + MCP over Podman Compose); this file
covers the conventions and non-obvious decisions you must not regress.

## Commands

- `make init && make up && make test` — full setup + verification.
- `make test-unit` — unit tests. Runs **inside the containers** when the stack
  is up (tests the deployed images), otherwise in a host `.venv` (tests the
  working tree, same as CI). After editing Python code without rebuilding,
  either `make rebuild` first or run tests with the stack down.
- `ruff check .` — lint (config in root `pyproject.toml`; line-length 120,
  rules E/W/F/I/UP, E501 ignored).
- CI (`.github/workflows/ci.yml`) = ruff + host-venv unit tests + compose
  config validation + image builds. Keep local behavior aligned with CI.

## Testing conventions

- All unit tests are **network-free**. Mock DNS with
  `monkeypatch.setattr(socket, "getaddrinfo", ...)` (see `bridge/tests`).
- Bridge tests import `bridge.main` / `bridge.browser_client`; mcp tests
  insert `mcp/` into `sys.path` and import `server` directly (the mcp service
  is a single module, not a package).
- Mutate module-level config in tests via `monkeypatch.setattr`, never by
  assigning module globals with try/finally.
- Never assert deployment-dependent config values (e.g. `BROWSER_ENGINE`) —
  container tests run against the deployed image, whose `.env` may differ from
  CI. Assert the wiring (`value == module.ATTR`) or monkeypatch explicitly.
- Async code is driven with explicit `asyncio.run(...)` in tests (no
  pytest-asyncio dependency).

## Non-obvious design decisions (do not regress)

1. **Pinned dependencies with CVE comments** — `starlette`, `pyjwt`,
   `uvicorn` are pinned beyond what upstream lower bounds require. Keep the
   pins and their rationale comments; when bumping, update the comment.
2. **Pinned image tags** — `searxng/searxng` and `valkey` are pinned
   deliberately (no floating `:latest`); the camoufox image pins both the
   camoufox package and the browser build as Dockerfile ARGs. Bump via
   `SEARXNG_CHANNEL` in `.env.example` **and** the compose default **and** the
   README env table — all three must stay in sync.
3. **Camoufox is the only browser engine** (since the fortress-last tag;
   Fortress upstream stopped shipping builds 2026-07-15). Do not reintroduce
   a second engine or an engine-selection env var without a real need — the
   client is `bridge/bridge/browser_client.py`, connecting over Playwright's
   websocket protocol (`firefox.connect`), NOT CDP. The Chromium CDP
   Host-header workaround is gone with CDP — don't reinvent it.
4. **Playwright client/server version parity (Camoufox)** — Playwright's
   remote protocol enforces *minor*-version parity (the server rejects
   mismatched clients with HTTP 428), and the camoufox image's driver is
   pinned by camoufox 0.5.5 to playwright 1.60.x. `bridge/pyproject.toml`
   therefore pins `playwright==1.60.0`. Bump the bridge pin and the camoufox
   Dockerfile pin **together**, and only once Camoufox upstream supports the
   new minor.
5. **SSRF defense in depth** — URLs are validated at the bridge edge
   (`_validate_public_url`), again per search-result URL, and again inside the
   browser (`_guard_request` intercepts redirects/subresources). Unresolvable
   hosts are *rejected*, not passed through (DNS-rebinding defense). Any new
   endpoint that fetches a URL must go through the same validator.
6. **MCP auth model** — localhost/podman-forwarded subnets bypass bearer auth
   (`_get_trusted_cidrs`); a non-loopback `MCP_BIND_HOST` without
   `MCP_API_KEY` is a hard startup error. Keep it that way.
7. **SearXNG settings are rendered at container start** from
   `searxng/settings.template.yml` + env overrides (`render_settings.py`,
   `searxng-entrypoint.sh`). The `/etc/searxng/settings.yml` mount is a
   *placeholder* for the upstream entrypoint's existence check; the real config
   is `/tmp/searxng-settings.yml` via `SEARXNG_SETTINGS_PATH`.
8. **Log redaction** — MCP logs tool args through `_redact_args()` (masks
   `user:pass@` and `?token=`/`?key=` etc.). Route any new logged user input
   through it.
9. **Browser profile is ephemeral by design** — Playwright's `launchServer`
   cannot serve a persistent context over websocket, so there is no profile
   volume. Login persistence across scrape calls is provided by named
   sessions (`bridge/bridge/browser_client.py` + `POST /sessions`), which are
   bounded by `CAMOUFOX_MAX_SESSIONS` and die with the browser connection.
10. **Proxy + GeoIP** — browser egress is configured via `CAMOUFOX_PROXY_*`
    env in the ws-camoufox container (never per-scrape). The GeoIP database
    is warmed at image build time (`camoufox[geoip]` + `download_mmdb()` in
    the Dockerfile) — never download it at container start. `CAMOUFOX_GEOIP`
    auto-enables with a proxy; the launcher retries once without geoip if the
    launch-time IP lookup fails, so a flaky lookup cannot crash-loop.

## Style

- Python 3.12 target, `from __future__ import annotations`, type hints
  throughout, stdlib-first (no Starlette in mcp — it's a raw ASGI app).
- Environment-variable knobs for anything operational; document each in
  `.env.example` **and** the README env table.
- Secrets are generated by `scripts/init.py` (`make init`); never commit `.env`.
