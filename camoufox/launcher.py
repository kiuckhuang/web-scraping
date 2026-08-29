"""Launch Camoufox as a remote Playwright websocket server (ws-camoufox container).

`python -m camoufox server` takes no options, so this wrapper forwards container
environment into camoufox.server.launch_server. All extra kwargs are merged into
the config camoufox sends to Playwright's `firefox.launchServer()` (host, port,
wsPath, maxConnections are native launchServer options).

Known limitation: headless='virtual' (managed Xvfb) is only wired through the
Camoufox() wrapper classes, not launch_server — this server runs Firefox in
plain headless mode, like the Fortress engine today.
"""

import os

from camoufox.server import launch_server


def main() -> None:
    launch_server(
        headless=True,
        host=os.environ.get("CAMOUFOX_HOST", "0.0.0.0"),
        port=int(os.environ.get("CAMOUFOX_PORT", "9222")),
        ws_path=os.environ.get("CAMOUFOX_WS_PATH", "browser").strip("/"),
        max_connections=int(os.environ.get("CAMOUFOX_MAX_CONNECTIONS", "8")),
    )


if __name__ == "__main__":
    main()
