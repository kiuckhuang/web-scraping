#!/usr/bin/env bash
# make doctor — diagnose common setup problems in the web-scrape stack.
# Exits non-zero if any check fails.
set -u

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

PASS=0
FAIL=0

ok() { printf '  OK    %s\n' "$1"; PASS=$((PASS + 1)); }
bad() { printf '  FAIL  %s\n' "$1"; FAIL=$((FAIL + 1)); }

echo "=== Environment ==="

CONTAINER_CMD=""
if command -v podman >/dev/null 2>&1; then
    CONTAINER_CMD="podman"
    ok "podman installed ($(podman --version))"
elif command -v docker >/dev/null 2>&1; then
    CONTAINER_CMD="docker"
    ok "docker installed ($(docker --version))"
else
    bad "container runtime (podman or docker) not found in PATH"
fi

if [ -f .env ]; then
    ok ".env exists"
else
    bad ".env missing — run 'make init' first"
fi

if [ -f .env ] && grep -q '^MCP_API_KEY=change-me' .env 2>/dev/null; then
    bad ".env MCP_API_KEY is still the placeholder — run 'make init' to generate one"
else
    ok "MCP_API_KEY looks set"
fi

echo ""
echo "=== Containers ==="

if [ -n "$CONTAINER_CMD" ]; then
    for svc in valkey searxng camoufox bridge mcp; do
        if $CONTAINER_CMD ps --format '{{.Names}}' | grep -q "ws-$svc"; then
            ok "ws-$svc running"
        else
            bad "ws-$svc not running (start with 'make up')"
        fi
    done
else
    bad "cannot inspect containers without podman or docker"
fi

echo ""
echo "=== Health ==="

BRIDGE_PORT=$(sed -n 's/^PORT_BRIDGE=//p' .env 2>/dev/null); BRIDGE_PORT=${BRIDGE_PORT:-8000}
MCP_PORT=$(sed -n 's/^PORT_MCP=//p' .env 2>/dev/null); MCP_PORT=${MCP_PORT:-9100}
SEARXNG_PORT=$(sed -n 's/^PORT_SEARXNG=//p' .env 2>/dev/null); SEARXNG_PORT=${SEARXNG_PORT:-8888}
CAMOUFOX_PORT=$(sed -n 's/^PORT_CAMOUFOX=//p' .env 2>/dev/null); CAMOUFOX_PORT=${CAMOUFOX_PORT:-9223}

if curl -sf "http://localhost:${BRIDGE_PORT}/health" >/dev/null 2>&1; then
    ok "bridge /health"
else
    bad "bridge /health unreachable (check 'podman compose logs bridge')"
fi

if curl -sf "http://localhost:${MCP_PORT}/health" >/dev/null 2>&1; then
    ok "mcp /health"
else
    bad "mcp /health unreachable (check 'podman compose logs mcp')"
fi

if curl -sf "http://localhost:${SEARXNG_PORT}/healthz" >/dev/null 2>&1; then
    ok "searxng /healthz"
else
    bad "searxng /healthz unreachable (check 'podman compose logs searxng')"
fi

# The Camoufox Playwright server has no HTTP endpoint — probe the TCP listener.
if timeout 3 bash -c "cat < /dev/null > /dev/tcp/localhost/${CAMOUFOX_PORT}" 2>/dev/null; then
    ok "camoufox WS endpoint (tcp/${CAMOUFOX_PORT})"
else
    bad "camoufox WS endpoint unreachable (check 'podman compose logs camoufox')"
fi

echo ""
echo "=== Result: $PASS passed, $FAIL failed ==="
[ "$FAIL" -eq 0 ]
