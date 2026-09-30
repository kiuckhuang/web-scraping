# Camoufox — anti-detect Firefox (patched at the C++ level), served over
# Playwright's websocket protocol; the stack's browser engine.
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
# mismatch), and camoufox 0.5.6 requires playwright <1.63.
ARG CAMOUFOX_PKG_VERSION=0.5.6
ARG CAMOUFOX_BROWSER_VERSION=156.0.1-beta.33
# camoufox installs the browser under XDG_CACHE_HOME (platformdirs) — a fixed
# shared path so the build-time fetch as root is visible to appuser at runtime.
ENV XDG_CACHE_HOME=/opt/camoufox-cache
# [geoip] enables GeoIP-consistent fingerprints when a proxy is configured
# (timezone/locale/lat-lon matched to the proxy's egress IP). The MaxMind DB
# is warmed here at build time — never downloaded at container start.
# The CLI can report pin/download errors with exit 0; assert the installed pin.
COPY install_browser.py /app/install_browser.py
RUN pip install --no-cache-dir \
        "camoufox[geoip]==${CAMOUFOX_PKG_VERSION}" \
        "playwright==1.62.0" \
    && EXPECTED_BROWSER_VERSION="${CAMOUFOX_BROWSER_VERSION}" python /app/install_browser.py \
    && python -c "from camoufox.geolocation import download_mmdb; download_mmdb()" \
    && camoufox_path="$(python -c 'from camoufox.pkgman import INSTALL_DIR; print(INSTALL_DIR)')" \
    && chmod -R a+rX "$camoufox_path" \
    && chown -R appuser:appuser "$camoufox_path"

# Preserve the CLI fetch path's extension warmup when using the exact installer.
RUN python -c "from camoufox.__main__ import maybe_download_addons, DefaultAddons; maybe_download_addons(list(DefaultAddons))" \
    && chown -R appuser:appuser /opt/camoufox-cache

# Runtime libraries for the patched Firefox binary. playwright install-deps
# resolves the correct package names for the detected Debian release.
RUN python -m playwright install-deps firefox \
    && rm -rf /var/lib/apt/lists/*

COPY --chown=appuser:appuser launcher.py /app/launcher.py

USER appuser

EXPOSE 9222

# Healthcheck: the Playwright server has no HTTP endpoint, so probe the TCP
# listener directly.
HEALTHCHECK --interval=60s --timeout=5s --retries=5 --start-period=20s \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 9222), timeout=3).close()"

CMD ["python", "launcher.py"]
