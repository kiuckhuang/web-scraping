#!/usr/bin/env python3
"""Create or refresh the local .env with host IDs and deployment secrets.

New files use the compact .env.example. Existing files are updated in place:
only managed image pins change; missing everyday settings are appended.
Secrets, flags, custom overrides and comments are preserved. --compact is an
explicit migration that removes redundant advanced defaults after backing up
the original file; all feature flags and credential settings are retained.
--ensure leaves existing files untouched, as used by up/rebuild/update.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
TEMPLATE = ROOT / ".env.example"
ADVANCED_TEMPLATE = ROOT / ".env.advanced.example"
_ASSIGNMENT = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=)(.*)$")

# Secrets generated only when absent (first run, or a previous .env lacked them).
GENERATED = {
    "SEARXNG_SECRET_KEY": lambda: secrets.token_hex(32),
    "MCP_API_KEY": lambda: secrets.token_urlsafe(32),
}
# Pinned component versions: on an explicit `make init` the template value
# always wins so version bumps in .env.example reach existing deployments.
# API keys/secrets (GENERATED) and every other local setting keep their
# existing value. Keep this set in sync with the pinned-image policy
# (README env table); keys here must never be secrets.
VERSIONED = {
    "SEARXNG_CHANNEL",
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
        match = _ASSIGNMENT.match(line)
        if match:
            values[match[2]] = match[4]
    return values


def render(
    existing: dict[str, str],
    *, original: str | None = None,
) -> tuple[list[str], list[str], list[str], list[tuple[str, str, str]]]:
    """Render .env lines from the template, overlaying existing values.

    VERSIONED keys take the template value instead. Returns the lines, the
    secret keys that were newly generated, the template keys added on top of
    the existing file, and the version keys updated to the template value
    as (key, old, new) tuples.
    """
    template_text = TEMPLATE.read_text(encoding="utf-8")
    lines = template_text.splitlines(keepends=True)
    templated: set[str] = set()
    generated: list[str] = []
    added: list[str] = []
    updated: list[tuple[str, str, str]] = []
    for index, line in enumerate(lines):
        key, separator, template_value = line.partition("=")
        if not separator or key.lstrip().startswith("#") or not key.strip():
            continue
        key = key.strip()
        templated.add(key)
        if key in existing and key not in VERSIONED:
            lines[index] = f"{key}={existing[key]}\n"
        else:
            if key in GENERATED:
                lines[index] = f"{key}={GENERATED[key]()}\n"
                generated.append(key)
            elif key in HOST_IDS:
                lines[index] = f"{key}={HOST_IDS[key]()}\n"
            # else: keep the template default as-is
            if key in existing:
                if existing[key].strip() != template_value.strip():
                    updated.append((key, existing[key].strip(), template_value.strip()))
            else:
                added.append(key)

    if original is not None:
        # Preserve user formatting/comments and advanced settings in place.
        managed = parse_env_file(TEMPLATE)
        preserved = []
        for line in original.splitlines(keepends=True):
            match = _ASSIGNMENT.match(line)
            if match and match[2] in VERSIONED and match[2] in managed:
                comment = re.search(r"\s+#.*", match[4])
                suffix = comment[0] if comment else ""
                line = f"{match[1]}{match[2]}{match[3]}{managed[match[2]]}{suffix}\n"
            preserved.append(line)
        missing = [key for key in templated if key not in existing]
        if missing:
            if preserved and not preserved[-1].endswith("\n"):
                preserved[-1] += "\n"
            preserved.append("\n# Added by make init (existing settings above are preserved).\n")
            for line in lines:
                match = _ASSIGNMENT.match(line)
                if match and match[2] in missing:
                    preserved.append(line)
        return preserved, generated, added, updated

    # Keys added by hand that the current template does not carry are kept
    # verbatim instead of being dropped by the re-render.
    extras = [key for key in existing if key not in templated]
    if extras:
        lines.append(
            "\n# --- carried over from the previous .env (not in the current .env.example) ---\n"
        )
        lines.extend(f"{key}={existing[key]}\n" for key in extras)

    return lines, generated, added, updated


def compact_values(existing: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Remove only known redundant tuning defaults; keep explicit choices."""
    defaults = parse_env_file(ADVANCED_TEMPLATE)
    everyday = parse_env_file(TEMPLATE)
    kept = {}
    removed = []
    for key, value in existing.items():
        protected = key in everyday or key.endswith("_ENABLED") or "FALLBACK" in key or any(
            word in key for word in ("KEY", "SECRET", "TOKEN", "PASSWORD", "USERNAME")
        ) or key in ("HTTP_FASTPATH", "CAMOUFOX_ISOLATE_CONTEXTS", "CAMOUFOX_GEOIP") or defaults.get(key) in ("true", "false")
        if not protected and key in defaults and value == defaults[key]:
            removed.append(key)
        else:
            kept[key] = value
    return kept, removed


def write_env(content: str) -> None:
    """Atomic, private replacement so interrupted init cannot truncate secrets."""
    fd, name = tempfile.mkstemp(prefix=".env.init-", dir=ENV_FILE.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, ENV_FILE)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create .env from .env.example, preserving existing values.",
    )
    parser.add_argument(
        "--ensure",
        action="store_true",
        help="create .env only if missing; never modify an existing file",
    )
    parser.add_argument("--compact", action="store_true",
                        help="back up and simplify existing .env; remove only redundant advanced defaults")
    parser.add_argument("--dry-run", action="store_true",
                        help="report changes without writing files or printing secret values")
    args = parser.parse_args()
    if args.ensure and (args.compact or args.dry_run):
        parser.error("--ensure cannot be combined with --compact or --dry-run")

    if ENV_FILE.exists():
        if args.ensure:
            return  # .env already exists — the caller only needed existence
        existing = parse_env_file(ENV_FILE)
        original = ENV_FILE.read_text(encoding="utf-8")
        removed = []
        values = existing
        if args.compact:
            values, removed = compact_values(existing)
        lines, generated, added, updated = render(values, original=None if args.compact else original)
        content = "".join(lines)
        if content == ENV_FILE.read_text(encoding="utf-8"):
            print(f"env up to date ({len(existing)} keys, no changes)")
            return
        note = f"{len(values) - len(updated)} existing values preserved"
        if updated:
            note += f"; versions updated: {', '.join(key for key, _, _ in updated)}"
        if added:
            note += f"; +{len(added)} new from template"
        if generated:
            note += f"; generated: {', '.join(generated)}"
        if removed:
            note += f"; removed {len(removed)} redundant defaults"
        if args.dry_run:
            print(f"Would update .env ({note}); no files written")
        else:
            if args.compact:
                fd, name = tempfile.mkstemp(prefix=".env.backup-", dir=ENV_FILE.parent)
                backup = Path(name)
                with os.fdopen(fd, "w", encoding="utf-8") as file:
                    file.write(original)
                print(f"Backup saved: {backup.name}")
            write_env(content)
            print(f"Updated .env ({note})")
        for key, old, new in updated:
            print(f"  {key}: {old} -> {new}")
        return

    lines, _generated, _added, _updated = render({})
    if args.dry_run:
        print(f"Would create compact .env ({len(parse_env_file(TEMPLATE))} settings); no files written")
        return
    write_env("".join(lines))
    print(
        "Created .env "
        f"(UID={os.getuid() or 1000}, GID={os.getgid() or 1000}, secrets generated)"
    )


if __name__ == "__main__":
    main()
