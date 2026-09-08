#!/usr/bin/env python3
"""Create or refresh the local .env with host IDs and deployment secrets.

init re-renders .env from the current .env.example and overlays every value
already present in an existing .env, so template updates (new keys, refreshed
comments) are picked up without losing your configuration — including
MCP_API_KEY, which remote clients hold. Only keys missing from the existing
file are generated. Delete .env first only for a full secret rotation (this
invalidates the MCP bearer token configured in remote clients).
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
TEMPLATE = ROOT / ".env.example"

# Secrets generated only when absent (first run, or a previous .env lacked them).
GENERATED = {
    "SEARXNG_SECRET_KEY": lambda: secrets.token_hex(32),
    "MCP_API_KEY": lambda: secrets.token_urlsafe(32),
}
# Host identity: root cannot be recreated as appuser inside the images, so run
# with the conventional unprivileged IDs when init executes as root.
HOST_IDS = {
    "APP_UID": lambda: str(os.getuid() or 1000),
    "APP_GID": lambda: str(os.getgid() or 1000),
}


def parse_env_file(path: Path) -> dict[str, str]:
    """Return KEY -> VALUE for a flat .env file; comments and blanks are skipped."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and not key.lstrip().startswith("#") and key.strip():
            values[key.strip()] = value
    return values


def render(existing: dict[str, str]) -> tuple[list[str], list[str]]:
    """Render .env lines from the template, overlaying existing values.

    Returns the lines and the list of secret keys that were newly generated.
    """
    lines = TEMPLATE.read_text(encoding="utf-8").splitlines(keepends=True)
    templated: set[str] = set()
    generated: list[str] = []
    for index, line in enumerate(lines):
        key, separator, _ = line.partition("=")
        if not separator or key.lstrip().startswith("#") or not key.strip():
            continue
        key = key.strip()
        templated.add(key)
        if key in existing:
            lines[index] = f"{key}={existing[key]}\n"
        elif key in GENERATED:
            lines[index] = f"{key}={GENERATED[key]()}\n"
            generated.append(key)
        elif key in HOST_IDS:
            lines[index] = f"{key}={HOST_IDS[key]()}\n"

    # Keys added by hand that the current template does not carry are kept
    # verbatim instead of being dropped by the re-render.
    extras = [key for key in existing if key not in templated]
    if extras:
        lines.append(
            "\n# --- carried over from the previous .env (not in the current .env.example) ---\n"
        )
        lines.extend(f"{key}={existing[key]}\n" for key in extras)

    return lines, generated


def main() -> None:
    existing = parse_env_file(ENV_FILE) if ENV_FILE.exists() else {}
    lines, generated = render(existing)
    ENV_FILE.write_text("".join(lines), encoding="utf-8")
    ENV_FILE.chmod(0o600)
    if existing:
        note = f"{len(existing)} existing keys preserved"
        if generated:
            note += f"; generated: {', '.join(generated)}"
        print(f"Updated .env from .env.example ({note})")
    else:
        print(
            "Created .env "
            f"(UID={os.getuid() or 1000}, GID={os.getgid() or 1000}, secrets generated)"
        )


if __name__ == "__main__":
    main()
