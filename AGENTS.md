# AGENTS.md — web-scraping stack

Guidance for coding agents working in this repo. Read the README first for the
architecture (SearXNG + Fortress + Bridge + MCP over Podman Compose); this file
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
- Bridge tests import `bridge.main` / `bridge.fortress_client`; mcp tests
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

1. **Chromium Host-header workaround** — Chromium's DevTools server rejects
   non-IP Host headers, so `fortress_client._resolve_cdp_url()` resolves the
   container name to an IP. It must re-resolve **on every reconnect and health
   check**, not only at import time: a recreated Fortress container gets a new
   IP, and a cached address silently breaks scraping until bridge restart.
2. **Pinned dependencies with CVE comments** — `starlette`, `pyjwt`,
   `uvicorn` are pinned beyond what upstream lower bounds require. Keep the
   pins and their rationale comments; when bumping, update the comment.
3. **Pinned image tags** — `searxng/searxng`, `tilion/fortress`, `valkey`,
   `alpine` are pinned deliberately (no floating `:latest`). Bump via
   `SEARXNG_CHANNEL` / `FORTRESS_CHANNEL` in `.env.example` **and** the compose
   default **and** the README env table — all three must stay in sync.
4. **Fortress is unmaintained upstream** (no build since 2026-07; current
   Chrome stable is newer). See README "Fortress maintenance status" before
   assuming tag 150 is current. Do not silently switch engines — the SSRF
   guard, WAF-wait logic, and `_guard_request` routing are Fortress-shaped.
   Stage 1 of the exit path is implemented: `BROWSER_ENGINE` (fortress |
   camoufox) selects the engine in `fortress_client.py`, and an opt-in
   `camoufox` compose service (profiles-gated) runs alongside Fortress. The
   SSRF route guard and the scrape/crawl/search layers are engine-agnostic
   Playwright API — keep them that way.
5. **Playwright client/server version parity (Camoufox)** — Playwright's
   remote protocol enforces *minor*-version parity (the server rejects
   mismatched clients with HTTP 428), and the camoufox image's driver is
   pinned by camoufox 0.5.5 to playwright 1.60.x. `bridge/pyproject.toml`
   therefore pins `playwright==1.60.0`. Bump the bridge pin and the camoufox
   Dockerfile pin **together**, and only once Camoufox upstream supports the
   new minor. The Fortress CDP path is version-agnostic.
6. **SSRF defense in depth** — URLs are validated at the bridge edge
   (`_validate_public_url`), again per search-result URL, and again inside the
   browser (`_guard_request` intercepts redirects/subresources). Unresolvable
   hosts are *rejected*, not passed through (DNS-rebinding defense). Any new
   endpoint that fetches a URL must go through the same validator.
7. **MCP auth model** — localhost/podman-forwarded subnets bypass bearer auth
   (`_get_trusted_cidrs`); a non-loopback `MCP_BIND_HOST` without
   `MCP_API_KEY` is a hard startup error. Keep it that way.
8. **SearXNG settings are rendered at container start** from
   `searxng/settings.template.yml` + env overrides (`render_settings.py`,
   `searxng-entrypoint.sh`). The `/etc/searxng/settings.yml` mount is a
   *placeholder* for the upstream entrypoint's existence check; the real config
   is `/tmp/searxng-settings.yml` via `SEARXNG_SETTINGS_PATH`.
9. **Log redaction** — MCP logs tool args through `_redact_args()` (masks
   `user:pass@` and `?token=`/`?key=` etc.). Route any new logged user input
   through it.
10. **uBlock Origin Lite** is downloaded by `scripts/init-ublock.sh` with a
    pinned version + SHA-256. Never download unverified blobs at container
    start. (Camoufox bundles uBlock Origin in its browser build; the init
    container is Fortress-specific.)

## Style

- Python 3.12 target, `from __future__ import annotations`, type hints
  throughout, stdlib-first (no Starlette in mcp — it's a raw ASGI app).
- Environment-variable knobs for anything operational; document each in
  `.env.example` **and** the README env table.
- Secrets are generated by `scripts/init.py` (`make init`); never commit `.env`.
