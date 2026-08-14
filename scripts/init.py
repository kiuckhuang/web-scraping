#!/usr/bin/env python3
"""Create a local .env with host IDs and deployment secrets."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
TEMPLATE = ROOT / ".env.example"


def main() -> None:
    if ENV_FILE.exists():
        print(".env already exists - skipping. Delete it first to regenerate.")
        values = {}
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key in {"APP_UID", "APP_GID"}:
                values[key] = value
    else:
        values = {
            # Root cannot be recreated as appuser inside the images; use the
            # conventional unprivileged IDs when initialization runs as root.
            "APP_UID": str(os.getuid() or 1000),
            "APP_GID": str(os.getgid() or 1000),
            "SEARXNG_SECRET_KEY": secrets.token_hex(32),
            "MCP_API_KEY": secrets.token_urlsafe(32),
        }
        lines = TEMPLATE.read_text(encoding="utf-8").splitlines(keepends=True)
        for index, line in enumerate(lines):
            for key, value in values.items():
                if line.startswith(f"{key}="):
                    lines[index] = f"{key}={value}\n"
        ENV_FILE.write_text("".join(lines), encoding="utf-8")
        ENV_FILE.chmod(0o600)
        print(f"Created .env (UID={values['APP_UID']}, GID={values['APP_GID']}, secrets generated)")

if __name__ == "__main__":
    main()
