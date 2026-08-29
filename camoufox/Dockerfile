# Camoufox — anti-detect Firefox (patched at the C++ level), served over
# Playwright's websocket protocol. Opt-in alternative to Fortress (stage 1 of
# the migration path; see README "Alternative browser engine").
FROM python:3.12-slim

ARG APP_UID=1000
ARG APP_GID=1000

RUN test "${APP_UID}" -ne 0 && test "${APP_GID}" -ne 0 && \
    groupadd -g ${APP_GID} appuser && \
    useradd -u ${APP_UID} -g ${APP_GID} -m appuser

WORKDIR /app

# Upgrade the base image's pip (same rationale as the bridge/mcp images).
RUN pip install --no-cache-dir -U pip

# Pin the camoufox python package AND the browser build so image builds are
# reproducible: camoufox set <specifier> + fetch download exactly one release
# from the project's GitHub releases at build time — never at container start.
# playwright is pinned to the same minor as bridge/pyproject.toml: Playwright's
# remote protocol enforces client/server minor-version parity (HTTP 428 on
# mismatch), and camoufox 0.5.5 requires playwright <1.61.
ARG CAMOUFOX_PKG_VERSION=0.5.5
ARG CAMOUFOX_BROWSER_VERSION=152.0.4-beta.29
# camoufox installs the browser under XDG_CACHE_HOME (platformdirs) — a fixed
# shared path so the build-time fetch as root is visible to appuser at runtime.
ENV XDG_CACHE_HOME=/opt/camoufox-cache
RUN pip install --no-cache-dir \
        "camoufox==${CAMOUFOX_PKG_VERSION}" \
        "playwright==1.60.0" \
    && python -m camoufox set "official/stable/${CAMOUFOX_BROWSER_VERSION}" \
    && python -m camoufox fetch \
    && camoufox_path="$(python -c 'from camoufox.pkgman import INSTALL_DIR; print(INSTALL_DIR)')" \
    && chmod -R a+rX "$camoufox_path" \
    && chown -R appuser:appuser "$camoufox_path"

# Runtime libraries for the patched Firefox binary. playwright install-deps
# resolves the correct package names for the detected Debian release.
RUN python -m playwright install-deps firefox \
    && rm -rf /var/lib/apt/lists/*

COPY --chown=appuser:appuser launcher.py /app/launcher.py

USER appuser

EXPOSE 9222

# Healthcheck: the Playwright server has no HTTP endpoint, so probe the TCP
# listener directly.
HEALTHCHECK --interval=15s --timeout=5s --retries=5 --start-period=20s \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 9222), timeout=3).close()"

CMD ["python", "launcher.py"]
