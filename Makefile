# =============================================================================
#  Makefile — automated build, run, test for web-scraping stack
#
#  Quick start:
#    make init      # first run only — creates .env with your UID/GID
#    make           # start all services
#    make help      # show this help
# =============================================================================

CONTAINER := podman

.PHONY: all init build up down logs test rebuild clean help

all: up

help:
	@echo "Usage: make <target>"
	@echo ""
	@echo "Targets:"
	@echo "  init      — Create .env from .env.example with your host UID/GID"
	@echo "  build     — Build bridge and mcp images"
	@echo "  up        — Start all services (podman compose up -d)"
	@echo "  down      — Stop all services"
	@echo "  logs      — Follow logs from all services"
	@echo "  test      — Integration tests: health checks + API smoke tests"
	@echo "  rebuild   — Stop, clean caches, rebuild and start"
	@echo "  clean     — Stop, remove volumes, prune unused images"
	@echo ""
	@echo "Quick start:"
	@echo "  make init && make"

init:
	@if [ -f .env ]; then \
		echo ".env already exists — skipping. Delete it first to regenerate."; \
	else \
		cp .env.example .env; \
		sed -i "s/^APP_UID=.*/APP_UID=$$(id -u)/" .env; \
		sed -i "s/^APP_GID=.*/APP_GID=$$(id -g)/" .env; \
		sed -i "s/^SEARXNG_SECRET_KEY=.*/SEARXNG_SECRET_KEY=$$(openssl rand -hex 32)/" .env; \
		echo "Created .env (UID=$$(id -u), GID=$$(id -g), secret key generated)"; \
	fi
	@mkdir -p fortress-profile && chmod 777 fortress-profile && \
		echo "fortress-profile ready (chmod 777 for Fortress container user)"

build:
	$(CONTAINER) compose build

up:
	$(CONTAINER) compose up -d

down:
	$(CONTAINER) compose down

logs:
	$(CONTAINER) compose logs -f

test:
	@PASS=0; FAIL=0; \
	echo "=== Integration Tests ==="; \
	echo ""; \
	echo "[1/4] Bridge health (localhost:8000) ..."; \
	if curl -sf http://localhost:8000/health | python3 -m json.tool; then \
		PASS=$$((PASS+1)); echo "  PASS"; \
	else \
		FAIL=$$((FAIL+1)); echo "  FAIL"; \
	fi; \
	echo ""; \
	echo "[2/4] Bridge search endpoint ..."; \
	if curl -sf "http://localhost:8000/search?q=hello+world&max_results=1" | python3 -m json.tool; then \
		PASS=$$((PASS+1)); echo "  PASS"; \
	else \
		FAIL=$$((FAIL+1)); echo "  FAIL"; \
	fi; \
	echo ""; \
	echo "[3/4] MCP health (localhost:9100) ..."; \
	if curl -sf http://localhost:9100/health | python3 -m json.tool; then \
		PASS=$$((PASS+1)); echo "  PASS"; \
	else \
		FAIL=$$((FAIL+1)); echo "  FAIL (check MCP server code)"; \
	fi; \
	echo ""; \
	echo "[4/4] MCP tools list ..."; \
	MCP_SID=$$(curl -sf -D- -X POST http://localhost:9100/mcp \
		-H "Content-Type: application/json" \
		-d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-06","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}' 2>/dev/null \
		| grep -i '^mcp-session-id' | head -1 | tr -d '\r' | awk '{print $$2}'); \
	if [ -n "$$MCP_SID" ]; then \
		curl -sf -X POST http://localhost:9100/mcp \
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


rebuild: down
	rm -rf bridge/__pycache__ mcp/__pycache__
	$(MAKE) build up

clean: down
	$(CONTAINER) compose down -v
	$(CONTAINER) system prune -f
