# =============================================================================
#  Makefile — automated build, run, test for web-scraping stack
#
#  Quick start:
#    make init      # first run only — creates .env with your UID/GID
#    make up        # start all services
#    make           # show this help
# =============================================================================

CONTAINER := podman

.PHONY: all init build up down logs test test-unit test-scrape doctor rebuild clean update help

all: help

help:
	@echo "Usage: make <target>"
	@echo ""
	@echo "Targets:"
	@echo "  init      — Create .env from .env.example with your host UID/GID"
	@echo "  build     — Build bridge and mcp images"
	@echo "  up        — Start all services (podman compose up -d)"
	@echo "  down      — Stop all services"
	@echo "  logs      — Follow logs from all services"
	@echo "  test      — Unit tests + integration tests (health checks + API smoke tests)"
	@echo "  test-unit — Unit tests only (runs pytest in bridge + mcp containers)"
	@echo "  test-scrape — Scrape smoke test through the bridge (example.com)"
	@echo "  doctor    — Diagnose common setup problems"
	@echo "  rebuild   — Stop, clean caches, rebuild and start"
	@echo "  update    — Pull latest images, rebuild custom images, restart"
	@echo "  clean     — Stop, remove volumes, prune unused images"
	@echo ""
	@echo "Quick start:"
	@echo "  make init && make up"

init:
	@python3 scripts/init.py
	@echo "environment ready (Fortress profile uses the managed fortress-profile volume)"

build:
	$(CONTAINER) compose build

up: init
	$(CONTAINER) compose up -d

down:
	$(CONTAINER) compose down

logs:
	$(CONTAINER) compose logs -f

test: test-unit
	@BRIDGE_PORT=$$(sed -n 's/^PORT_BRIDGE=//p' .env); \
	MCP_PORT=$$(sed -n 's/^PORT_MCP=//p' .env); \
	BRIDGE_PORT=$${BRIDGE_PORT:-8000}; \
	MCP_PORT=$${MCP_PORT:-9100}; \
	ready=0; \
	for attempt in 1 2 3 4 5 6 7 8 9 10; do \
		if curl -sf "http://localhost:$$BRIDGE_PORT/health" >/dev/null 2>&1 && curl -sf "http://localhost:$$MCP_PORT/health" >/dev/null 2>&1; then ready=1; break; fi; \
		sleep 2; \
	done; \
	if [ $$ready -ne 1 ]; then echo "Services did not become ready"; exit 1; fi; \
	PASS=0; FAIL=0; \
	echo "=== Integration Tests ==="; \
	echo "  bridge at localhost:$$BRIDGE_PORT, mcp at localhost:$$MCP_PORT"; \
	echo ""; \
	echo "[1/4] Bridge health (localhost:$$BRIDGE_PORT) ..."; \
	if curl -sf "http://localhost:$$BRIDGE_PORT/health" | python3 -m json.tool; then \
		PASS=$$((PASS+1)); echo "  PASS"; \
	else \
		FAIL=$$((FAIL+1)); echo "  FAIL"; \
	fi; \
	echo ""; \
	echo "[2/4] Bridge search endpoint ..."; \
	if curl -sf "http://localhost:$$BRIDGE_PORT/search?q=hello+world&max_results=1" | python3 -m json.tool; then \
		PASS=$$((PASS+1)); echo "  PASS"; \
	else \
		FAIL=$$((FAIL+1)); echo "  FAIL"; \
	fi; \
	echo ""; \
	echo "[3/4] MCP health (localhost:$$MCP_PORT) ..."; \
	if curl -sf "http://localhost:$$MCP_PORT/health" | python3 -m json.tool; then \
		PASS=$$((PASS+1)); echo "  PASS"; \
	else \
		FAIL=$$((FAIL+1)); echo "  FAIL (check MCP server code)"; \
	fi; \
	echo ""; \
	echo "[4/4] MCP tools list ..."; \
	MCP_SID=$$(curl -sf -D- -X POST "http://localhost:$$MCP_PORT/mcp" \
		-H "Content-Type: application/json" \
		-d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-06","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}' 2>/dev/null \
		| grep -i '^mcp-session-id' | head -1 | tr -d '\r' | awk '{print $$2}'); \
	if [ -n "$$MCP_SID" ]; then \
		curl -sf -X POST "http://localhost:$$MCP_PORT/mcp" \
			-H "Content-Type: application/json" \
			-H "Mcp-Session-Id: $$MCP_SID" \
			-d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
			| python3 -m json.tool && PASS=$$((PASS+1)) && echo "  PASS" || { FAIL=$$((FAIL+1)); echo "  FAIL"; }; \
	else \
		FAIL=$$((FAIL+1)); echo "  FAIL (could not initialize MCP session)"; \
	fi; \
	echo ""; \
	echo "=== Results: $$PASS passed, $$FAIL failed ==="; \
	[ $$FAIL -eq 0 ]


test-unit:
	@echo "=== Unit Tests (bridge + mcp) ==="
	@podman compose exec -T bridge python -m pytest -q bridge/tests || { echo "  FAILED: bridge unit tests"; exit 1; }
	@podman compose exec -T mcp python -m pytest -q tests || { echo "  FAILED: mcp unit tests"; exit 1; }
	@echo "  All unit tests passed"

test-scrape:
	@BRIDGE_PORT=$$(sed -n 's/^PORT_BRIDGE=//p' .env); \
	BRIDGE_PORT=$${BRIDGE_PORT:-8000}; \
	echo "=== Scrape smoke test (example.com via Fortress) ==="; \
	curl -sf -X POST "http://localhost:$$BRIDGE_PORT/scrape" \
		-H 'Content-Type: application/json' \
		-d '{"url": "https://example.com", "mode": "extract"}' \
		| python3 -m json.tool || { echo "  FAILED (check 'podman compose logs bridge fortress')"; exit 1; }

doctor:
	@./scripts/doctor.sh


rebuild: init down
	rm -rf bridge/__pycache__ mcp/__pycache__
	$(MAKE) build up

update: init down
	$(CONTAINER) compose pull
	$(MAKE) build up

clean: down
	$(CONTAINER) compose down -v
	$(CONTAINER) system prune -f
