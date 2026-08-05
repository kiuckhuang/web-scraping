#!/usr/bin/env python3
"""Render the SearXNG settings template into a concrete settings.yml.

Placeholders like ${SEARXNG_REQUEST_TIMEOUT} in the template are replaced
with the matching environment variable. Every variable is defaulted in
searxng-entrypoint.sh, so a missing value falls back to a sane default.

Usage: render_settings.py <template> <output>
"""
import os
import sys

TOKENS = [
    "SEARXNG_REQUEST_TIMEOUT",
    "SEARXNG_MAX_REQUEST_TIMEOUT",
    "SEARXNG_BAN_TIME_ON_FAIL",
    "SEARXNG_MAX_BAN_TIME_ON_FAIL",
    "SEARXNG_SUSPEND_TOO_MANY",
]


def main() -> None:
    src, dst = sys.argv[1], sys.argv[2]
    with open(src, encoding="utf-8") as fh:
        data = fh.read()
    for token in TOKENS:
        data = data.replace("${%s}" % token, os.environ[token])
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(data)


if __name__ == "__main__":
    main()
