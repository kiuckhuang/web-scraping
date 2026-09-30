#!/usr/bin/env python3
"""Opt-in live search diagnostics: process health is not search availability."""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def summarize(data: dict, elapsed: float, source: str) -> dict:
    return {
        "source": source, "seconds": round(elapsed, 2), "results": len(data.get("results", [])),
        "provider": data.get("provider", source), "fallback_used": data.get("fallback_used", False),
        "cached": data.get("cached", False), "unresponsive_engines": data.get("unresponsive_engines", []),
        "attempts": data.get("attempts", []), "top_urls": [r.get("url") for r in data.get("results", [])[:3]],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", action="append", help="Repeat for an opt-in query benchmark")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    config = {}
    env = Path(__file__).resolve().parents[1] / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            key, separator, value = line.partition("=")
            if separator and key in ("PORT_BRIDGE", "PORT_SEARXNG"):
                config[key] = value.strip()
    failed = False
    for query in args.query or ["python asyncio documentation"]:
        print(f"Query: {query}")
        for source, port in (("searxng", config.get("PORT_SEARXNG", "8888")),
                             ("bridge", config.get("PORT_BRIDGE", "8000"))):
            params = {"q": query, "format": "json"} if source == "searxng" else {"q": query, "max_results": 5}
            url = f"http://127.0.0.1:{port}/search?{urllib.parse.urlencode(params)}"
            start = time.monotonic()
            try:
                with urllib.request.urlopen(url, timeout=args.timeout) as response:
                    data = json.load(response)
                print(json.dumps(summarize(data, time.monotonic() - start, source), ensure_ascii=False))
                if source == "bridge" and not data.get("results"):
                    failed = True
            except (OSError, ValueError) as exc:
                print(json.dumps({"source": source, "error": type(exc).__name__,
                                  "seconds": round(time.monotonic() - start, 2)}))
                if source == "bridge":
                    failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
