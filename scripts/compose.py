#!/usr/bin/env python3
"""Run Compose with the optional SearXNG profile selected from .env."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from init import ENV_FILE, ROOT, parse_env_file


def searxng_enabled(values: dict[str, str]) -> bool:
    return values.get("SEARXNG_ENABLED", "false").strip().strip("\"'").lower() not in (
        "", "0", "false", "no", "off",
    )


def compose_command(runtime: str, args: list[str], enabled: bool) -> list[str]:
    # Explicit -f: podman forwards options to its compose provider
    # (docker-compose or podman-compose), and neither auto-detects the repo's
    # non-default podman-compose.yml filename.
    return [runtime, "compose", "-f", "podman-compose.yml",
            *(["--profile", "searxng"] if enabled else []), *args]


def detect_runtime() -> str:
    """podman on Linux (and CI); Apple `container` on macOS when installed."""
    if sys.platform != "darwin":
        return "podman"
    try:
        from apple_compose import find_binary
    except ImportError:
        return "podman"
    try:
        find_binary()
    except SystemExit:
        return "podman"
    return "container"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", default="auto")
    parser.add_argument("args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    runtime = detect_runtime() if args.runtime == "auto" else args.runtime
    if runtime == "container":
        # Apple container has no compose implementation; scripts/apple_compose.py
        # translates podman-compose.yml into native `container` invocations.
        adapter = Path(__file__).resolve().parent / "apple_compose.py"
        return subprocess.run([sys.executable, str(adapter), *args.args], cwd=ROOT).returncode
    values = parse_env_file(ENV_FILE) if ENV_FILE.exists() else {}
    enabled = searxng_enabled({**values, **os.environ})
    # down cleans every service even after the flag was changed to false.
    all_profiles = enabled or (args.args and args.args[0] == "down")
    if not enabled and args.args and args.args[0] == "up":
        # Stop previously enabled providers without deleting their volumes.
        running = subprocess.run([runtime, "ps", "--format", "{{.Names}}"],
                                 cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
        old = [name for name in ("ws-searxng", "ws-valkey") if name in running]
        if old:
            subprocess.run([runtime, "stop", *old], cwd=ROOT, check=True)
    return subprocess.run(compose_command(runtime, args.args, bool(all_profiles)), cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
