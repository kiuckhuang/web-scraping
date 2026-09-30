"""Initialization must never replace deployment choices or rotate keys."""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def init(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("env_init", ROOT / "scripts/init.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ENV_FILE", tmp_path / ".env")
    return module


def run(init, monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["init.py", *args])
    init.main()


def test_new_file_is_compact_private_and_idempotent(init, monkeypatch):
    run(init, monkeypatch)
    values = init.parse_env_file(init.ENV_FILE)
    assert set(values) == set(init.parse_env_file(init.TEMPLATE))
    assert len(values) <= 15
    assert "BRIDGE_CACHE_TTL" not in values
    assert "change-me" not in values["MCP_API_KEY"]
    assert init.ENV_FILE.stat().st_mode & 0o777 == 0o600
    before = init.ENV_FILE.read_bytes()
    run(init, monkeypatch)
    assert init.ENV_FILE.read_bytes() == before


def test_existing_keys_flags_blanks_comments_and_overrides_preserved(init, monkeypatch):
    original = ('# My local config\nMCP_API_KEY="my-secret" # keep\nEXA_API_KEY=\nEXA_ENABLED=true\n'
                'SEARXNG_ENABLED=false\nBRIDGE_CACHE_TTL=777\nCUSTOM_SETTING=hello\n'
                'SEARXNG_CHANNEL=old-tag\n')
    init.ENV_FILE.write_text(original)
    run(init, monkeypatch)
    out = init.ENV_FILE.read_text()
    for line in original.splitlines():
        if not line.startswith("SEARXNG_CHANNEL="):
            assert line in out
    assert "SEARXNG_CHANNEL=2026.9.21-49064747a" in out
    assert "SEARXNG_CHANNEL=old-tag" not in out


def test_ensure_does_not_touch_existing_file(init, monkeypatch):
    init.ENV_FILE.write_text("MCP_API_KEY=keep\nSEARXNG_CHANNEL=old\n")
    original = init.ENV_FILE.read_bytes()
    run(init, monkeypatch, "--ensure")
    assert init.ENV_FILE.read_bytes() == original


def test_version_refresh_preserves_inline_comment(init, monkeypatch):
    init.ENV_FILE.write_text('export SEARXNG_CHANNEL="old" # managed pin\n')
    run(init, monkeypatch)
    assert 'export SEARXNG_CHANNEL=2026.9.21-49064747a # managed pin' in init.ENV_FILE.read_text()


def test_compaction_backs_up_and_preserves_explicit_choices(init, monkeypatch):
    original = ('MCP_API_KEY=keep\nEXA_API_KEY=keep-exa\nEXA_ENABLED=true\n'
                'SEARXNG_ENABLED=false\nSEARCH_FALLBACK_BROWSER=true\nHTTP_FASTPATH=true\n'
                'CAMOUFOX_ISOLATE_CONTEXTS=true\nBRIDGE_CACHE_TTL=300\nBRIDGE_CACHE_MAX=42\n'
                'CUSTOM_SETTING=hello\n')
    init.ENV_FILE.write_text(original)
    run(init, monkeypatch, "--compact")
    values = init.parse_env_file(init.ENV_FILE)
    assert "BRIDGE_CACHE_TTL" not in values
    for key, value in {"MCP_API_KEY": "keep", "EXA_API_KEY": "keep-exa", "EXA_ENABLED": "true",
                       "SEARXNG_ENABLED": "false", "SEARCH_FALLBACK_BROWSER": "true", "HTTP_FASTPATH": "true",
                       "CAMOUFOX_ISOLATE_CONTEXTS": "true", "BRIDGE_CACHE_MAX": "42", "CUSTOM_SETTING": "hello"}.items():
        assert values[key] == value
    backups = list(init.ENV_FILE.parent.glob(".env.backup-*"))
    assert len(backups) == 1 and backups[0].read_text() == original
    assert backups[0].stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("existing", [False, True])
def test_dry_run_never_writes_or_prints_secrets(init, monkeypatch, capsys, existing):
    if existing:
        init.ENV_FILE.write_text("MCP_API_KEY=super-secret\nBRIDGE_CACHE_TTL=300\n")
    before = init.ENV_FILE.read_bytes() if existing else None
    run(init, monkeypatch, "--compact", "--dry-run")
    assert "super-secret" not in capsys.readouterr().out
    assert not list(init.ENV_FILE.parent.glob(".env.backup-*"))
    assert (init.ENV_FILE.read_bytes() if init.ENV_FILE.exists() else None) == before


def test_runtime_defaults_match_removable_template_defaults(init):
    """Compaction must not silently change Compose values by removing a key."""
    compose = (ROOT / "podman-compose.yml").read_text()
    defaults = dict(re.findall(r"\$\{([A-Z_]+):-([^}]*)\}", compose))
    advanced = init.parse_env_file(init.ADVANCED_TEMPLATE)
    _, removed = init.compact_values(advanced)
    for key in removed:
        assert key in defaults, f"No Compose default for removable {key}"
        assert defaults[key] == advanced[key], f"Default drift for {key}"


def test_image_pins_match_both_templates_and_compose(init):
    compact = init.parse_env_file(init.TEMPLATE)
    advanced = init.parse_env_file(init.ADVANCED_TEMPLATE)
    for key in init.VERSIONED:
        assert compact[key] == advanced[key]
        assert f"${{{key}:-{compact[key]}}}" in (ROOT / "podman-compose.yml").read_text()
