#!/usr/bin/env python3
"""Render the SearXNG settings template into a concrete settings.yml.

Placeholders like ${SEARXNG_REQUEST_TIMEOUT} in the template are replaced
with the matching environment variable. Every variable is defaulted in
searxng-entrypoint.sh, so a missing value falls back to a sane default.

Usage: render_settings.py <template> <output>
"""
import os
import sys

DEFAULTS = {
    "SEARXNG_BASE_URL": "http://localhost:8888/",
    "SEARXNG_REQUEST_TIMEOUT": "10",
    "SEARXNG_MAX_REQUEST_TIMEOUT": "15",
    "SEARXNG_BAN_TIME_ON_FAIL": "5",
    "SEARXNG_MAX_BAN_TIME_ON_FAIL": "120",
    "SEARXNG_SUSPEND_TOO_MANY": "180",
}


def _outgoing_proxy_block() -> str:
    """Build the `outgoing.proxies` YAML block from SEARXNG_OUTGOING_PROXY.

    Empty/unset keeps SearXNG on direct connections (the token in the
    template renders to a comment); a value like http://10.8.8.1:8088
    routes every engine request through that proxy (httpx `all://` form).
    """
    proxy = (os.environ.get("SEARXNG_OUTGOING_PROXY", "") or "").strip()
    if not proxy:
        return (
            "  # SEARXNG_OUTGOING_PROXY is unset — engines connect directly.\n"
            "  # Set it (e.g. http://10.8.8.1:8088) to route engine requests\n"
            "  # through an outbound proxy.\n"
        )
    return (
        "  # Engine requests routed through SEARXNG_OUTGOING_PROXY\n"
        "  proxies:\n"
        "    all://:\n"
        f"      - {proxy}\n"
    )


def main() -> None:
    src, dst = sys.argv[1], sys.argv[2]
    with open(src, encoding="utf-8") as fh:
        data = fh.read()
    os.environ.setdefault("SEARXNG_OUTGOING_PROXY", "")
    os.environ["SEARXNG_OUTGOING_PROXY_BLOCK"] = _outgoing_proxy_block()
    for token, default in DEFAULTS.items():
        val = os.environ.get(token, default) or default
        data = data.replace(f"${{{token}}}", val)
    data = data.replace("${SEARXNG_OUTGOING_PROXY_BLOCK}", os.environ["SEARXNG_OUTGOING_PROXY_BLOCK"])
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(data)


if __name__ == "__main__":
    main()
