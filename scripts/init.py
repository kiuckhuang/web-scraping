#!/usr/bin/env python3
"""Create or refresh the local .env with host IDs and deployment secrets.

`make init` (explicit) re-renders .env from the current .env.example and
overlays every value already present, so template updates (new keys,
refreshed comments) are picked up without losing your configuration —
including MCP_API_KEY, which remote clients hold. Only keys missing from
the existing file are generated. With --ensure (used by the up/rebuild/
update targets) an existing .env is left untouched; a missing one is
created. Delete .env for a full secret rotation (this invalidates the MCP
bearer token configured in remote clients).
"""

from __future__ import annotations

import argparse
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


def render(existing: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    """Render .env lines from the template, overlaying existing values.

    Returns the lines, the secret keys that were newly generated, and the
    template keys added on top of the existing file.
    """
    lines = TEMPLATE.read_text(encoding="utf-8").splitlines(keepends=True)
    templated: set[str] = set()
    generated: list[str] = []
    added: list[str] = []
    for index, line in enumerate(lines):
        key, separator, _ = line.partition("=")
        if not separator or key.lstrip().startswith("#") or not key.strip():
            continue
        key = key.strip()
        templated.add(key)
        if key in existing:
            lines[index] = f"{key}={existing[key]}\n"
        else:
            if key in GENERATED:
                lines[index] = f"{key}={GENERATED[key]()}\n"
                generated.append(key)
            elif key in HOST_IDS:
                lines[index] = f"{key}={HOST_IDS[key]()}\n"
            # else: keep the template default as-is
            added.append(key)

    # Keys added by hand that the current template does not carry are kept
    # verbatim instead of being dropped by the re-render.
    extras = [key for key in existing if key not in templated]
    if extras:
        lines.append(
            "\n# --- carried over from the previous .env (not in the current .env.example) ---\n"
        )
        lines.extend(f"{key}={existing[key]}\n" for key in extras)

    return lines, generated, added


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create .env from .env.example, preserving existing values.",
    )
    parser.add_argument(
        "--ensure",
        action="store_true",
        help="create .env only if missing; never modify an existing file",
    )
    args = parser.parse_args()

    if ENV_FILE.exists():
        if args.ensure:
            return  # .env already exists — the caller only needed existence
        existing = parse_env_file(ENV_FILE)
        lines, generated, added = render(existing)
        content = "".join(lines)
        if content == ENV_FILE.read_text(encoding="utf-8"):
            print(f"env up to date ({len(existing)} keys, no changes)")
            return
        ENV_FILE.write_text(content, encoding="utf-8")
        ENV_FILE.chmod(0o600)
        note = f"{len(existing)} existing keys preserved"
        if added:
            note += f"; +{len(added)} new from template"
        if generated:
            note += f"; generated: {', '.join(generated)}"
        print(f"Updated .env from .env.example ({note})")
        return

    lines, _generated, _added = render({})
    ENV_FILE.write_text("".join(lines), encoding="utf-8")
    ENV_FILE.chmod(0o600)
    print(
        "Created .env "
        f"(UID={os.getuid() or 1000}, GID={os.getgid() or 1000}, secrets generated)"
    )


if __name__ == "__main__":
    main()
