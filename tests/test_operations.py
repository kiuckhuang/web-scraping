"""Network-free renderer and operational diagnostic tests."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_render_settings_produces_yaml_and_proxy_override(monkeypatch, tmp_path):
    renderer = load("render_settings", "searxng/render_settings.py")
    target = tmp_path / "settings.yml"
    monkeypatch.setenv("EGRESS_PROXY", "http://shared.example:8080")
    monkeypatch.setenv("SEARXNG_OUTGOING_PROXY", "http://specific.example:8080")
    monkeypatch.setenv("SEARXNG_REQUEST_TIMEOUT", "7")
    monkeypatch.setattr(sys, "argv", ["render_settings", str(ROOT / "searxng/settings.template.yml"), str(target)])
    renderer.main()
    data = yaml.safe_load(target.read_text())
    assert data["outgoing"]["request_timeout"] == 7
    assert data["outgoing"]["proxies"]["all://"] == ["http://specific.example:8080"]
    assert "${" not in target.read_text()


def test_search_diagnostic_retains_engine_error_reasons():
    diagnostic = load("search_diagnostic", "scripts/search_diagnostic.py")
    out = diagnostic.summarize({"results": [], "unresponsive_engines": [["google", "CAPTCHA"]]}, 1.234, "searxng")
    assert out["results"] == 0
    assert out["seconds"] == 1.23
    assert out["unresponsive_engines"] == [["google", "CAPTCHA"]]


def test_renderer_rejects_invalid_timeout(monkeypatch, tmp_path):
    renderer = load("render_settings", "searxng/render_settings.py")
    monkeypatch.setenv("SEARXNG_REQUEST_TIMEOUT", "not-a-number")
    monkeypatch.setattr(sys, "argv", ["render_settings", str(ROOT / "searxng/settings.template.yml"), str(tmp_path / "out")])
    with pytest.raises(ValueError):
        renderer.main()


def test_compose_profile_selected_by_enable_flag(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    compose = load("compose_wrapper", "scripts/compose.py")
    assert not compose.searxng_enabled({})
    assert not compose.searxng_enabled({"SEARXNG_ENABLED": "false"})
    assert compose.searxng_enabled({"SEARXNG_ENABLED": "true"})
    assert compose.compose_command("podman", ["up", "-d"], False) == ["podman", "compose", "up", "-d"]
    assert compose.compose_command("podman", ["up", "-d"], True) == ["podman", "compose", "--profile", "searxng", "up", "-d"]


def test_compose_disabled_up_stops_old_optional_containers(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    compose = load("compose_wrapper", "scripts/compose.py")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout="ws-bridge\nws-searxng\nws-valkey\n", returncode=0)

    monkeypatch.setenv("SEARXNG_ENABLED", "false")
    monkeypatch.setattr(compose.subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["compose.py", "up", "-d"])
    assert compose.main() == 0
    assert calls[1] == ["podman", "stop", "ws-searxng", "ws-valkey"]
    assert calls[2] == ["podman", "compose", "up", "-d"]
