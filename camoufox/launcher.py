"""Launch Camoufox as a remote Playwright websocket server (ws-camoufox container).

`python -m camoufox server` takes no options, so this wrapper forwards container
environment into camoufox.server.launch_server. All extra kwargs are merged into
the config camoufox sends to Playwright's `firefox.launchServer()` (host, port,
wsPath, maxConnections, proxy are native launchServer options).

Outbound proxy (browser-wide): CAMOUFOX_PROXY_SERVER (+ _USERNAME/_PASSWORD/
_BYPASS), falling back to the stack-wide EGRESS_PROXY when unset. GeoIP-
consistent fingerprints (timezone/locale/lat-lon matched to the proxy's
egress IP) auto-enable when a proxy is set; CAMOUFOX_GEOIP=true/false
overrides. The GeoIP database is warmed at image build time — a missing DB
degrades to a no-geoip launch (logged), never a runtime download.

Known limitation: headless='virtual' (managed Xvfb) is only wired through the
Camoufox() wrapper classes, not launch_server — this server runs Firefox in
plain headless mode.
"""

import logging
import os
from pathlib import Path

from camoufox.server import launch_server

# The launcher is the container's main process — INFO logs (proxy/geoip state,
# fail-open retries) must be visible in `podman logs ws-camoufox`.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("camoufox-launcher")


def _proxy() -> dict[str, str] | None:
    """Build the Playwright proxy dict from CAMOUFOX_PROXY_* env; None if unset.

    CAMOUFOX_PROXY_SERVER wins; otherwise the stack-wide EGRESS_PROXY applies
    (one-line proxy setup for the whole stack).
    """
    server = (
        os.environ.get("CAMOUFOX_PROXY_SERVER", "").strip()
        or os.environ.get("EGRESS_PROXY", "").strip()
    )
    if not server:
        return None
    proxy = {"server": server}
    username = os.environ.get("CAMOUFOX_PROXY_USERNAME", "").strip()
    password = os.environ.get("CAMOUFOX_PROXY_PASSWORD", "").strip()
    if username:
        proxy["username"] = username
    if password:
        proxy["password"] = password
    bypass = os.environ.get("CAMOUFOX_PROXY_BYPASS", "").strip()
    if bypass:
        proxy["bypass"] = bypass
    return proxy


def _geoip_enabled(proxy: dict[str, str] | None) -> bool:
    """Decide whether to pass geoip=True (fingerprint matched to egress IP).

    CAMOUFOX_GEOIP: "auto" (default) enables it whenever a proxy is configured;
    "true"/"false" force it either way. A missing GeoIP database degrades to a
    logged no-geoip launch instead of failing the container.
    """
    raw = os.environ.get("CAMOUFOX_GEOIP", "auto").strip().lower()
    if raw in {"0", "false", "no"}:
        return False
    if raw not in {"1", "true", "yes", "auto", ""}:
        logger.warning("Invalid CAMOUFOX_GEOIP=%r — ignoring (use true/false/auto)", raw)
        return False
    if proxy is None and raw in {"", "auto"}:
        return False  # no proxy and not explicitly requested — host IP adds nothing
    try:
        from camoufox.geolocation import get_mmdb_path

        db = Path(get_mmdb_path("ipv4"))
        if not db.exists():
            logger.warning("GeoIP requested but the database is missing at %s — continuing without geoip", db)
            return False
    except Exception as exc:  # geoip extra missing or not importable
        logger.warning("GeoIP requested but unavailable (%s) — continuing without geoip", exc)
        return False
    return True


def main() -> None:
    proxy = _proxy()
    use_geoip = _geoip_enabled(proxy)
    kwargs: dict = dict(
        headless=True,
        host=os.environ.get("CAMOUFOX_HOST", "0.0.0.0"),
        port=int(os.environ.get("CAMOUFOX_PORT", "9222")),
        ws_path=os.environ.get("CAMOUFOX_WS_PATH", "browser").strip("/"),
        max_connections=int(os.environ.get("CAMOUFOX_MAX_CONNECTIONS", "8")),
    )
    if proxy:
        kwargs["proxy"] = proxy
        logger.info("Browser proxy enabled: %s (bypass: %s)", proxy["server"], proxy.get("bypass", "none"))
    if use_geoip:
        kwargs["geoip"] = True
    logger.info("Launching Camoufox server (proxy=%s, geoip=%s)", bool(proxy), use_geoip)
    try:
        launch_server(**kwargs)
    except Exception as exc:
        if not use_geoip:
            raise
        # The geoip lookup runs at launch through the proxy (public IP echo
        # services); a flaky lookup must not crash-loop the container — retry
        # once without geoip (fingerprint falls back to defaults; logged loudly).
        logger.warning("Launch with geoip failed (%s) — retrying without geoip", exc)
        kwargs.pop("geoip", None)
        launch_server(**kwargs)


if __name__ == "__main__":
    main()
