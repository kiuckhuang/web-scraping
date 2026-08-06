FROM python:3.12-slim

ARG APP_UID=1000
ARG APP_GID=1000

RUN groupadd -g ${APP_GID} appuser && \
    useradd -u ${APP_UID} -g ${APP_GID} -m appuser

WORKDIR /app

# System deps: ca-certificates + curl for healthchecks, plus
# libs that Playwright's CDP client may need at runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies (dev extras include pytest for `make test-unit`)
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]"

# NOTE: We do NOT run `playwright install` — the bridge connects to the
# Fortress container over CDP (http://fortress:9222), so it only needs
# the Playwright Python client, not local browser binaries.

# Copy the bridge application
COPY bridge/ ./bridge/
COPY tests/ ./bridge/tests/

RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Healthcheck: hit the FastAPI /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=15s \
    CMD curl -sf http://localhost:8000/health || exit 1

CMD ["uvicorn", "bridge.main:app", "--host", "0.0.0.0", "--port", "8000"]
