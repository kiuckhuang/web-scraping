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
    monkeypatch.setenv("SEARXNG_SECRET_KEY", "test-secret-key")
    monkeypatch.setattr(sys, "argv", ["render_settings", str(ROOT / "searxng/settings.template.yml"), str(target)])
    renderer.main()
    data = yaml.safe_load(target.read_text())
    assert data["outgoing"]["request_timeout"] == 7
    assert data["outgoing"]["proxies"]["all://"] == ["http://specific.example:8080"]
    assert data["server"]["secret_key"] == "test-secret-key"
    assert data["server"]["image_proxy"] is False
    assert "${" not in target.read_text()


def test_bing_is_registered_but_disabled_by_default():
    """SEARCH_FALLBACK_BING needs a configured engine, not just a bang."""
    text = (ROOT / "searxng/settings.template.yml").read_text()
    assert "\n      - bing\n" in text
    assert "\n  - name: bing\n    disabled: true\n" in text


def test_search_diagnostic_retains_engine_error_reasons():
    diagnostic = load("search_diagnostic", "scripts/search_diagnostic.py")
    out = diagnostic.summarize({"results": [], "unresponsive_engines": [["google", "CAPTCHA"]]}, 1.234, "searxng")
    assert out["results"] == 0
    assert out["seconds"] == 1.23
    assert out["unresponsive_engines"] == [["google", "CAPTCHA"]]


def test_renderer_rejects_invalid_timeout(monkeypatch, tmp_path):
    renderer = load("render_settings", "searxng/render_settings.py")
    monkeypatch.setenv("SEARXNG_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("SEARXNG_REQUEST_TIMEOUT", "not-a-number")
    monkeypatch.setattr(sys, "argv", ["render_settings", str(ROOT / "searxng/settings.template.yml"), str(tmp_path / "out")])
    with pytest.raises(ValueError):
        renderer.main()


@pytest.mark.parametrize("secret", [None, "", "change-me-to-a-random-64-char-hex-string"])
def test_renderer_rejects_missing_or_placeholder_secret(monkeypatch, tmp_path, secret):
    renderer = load("render_settings", "searxng/render_settings.py")
    if secret is None:
        monkeypatch.delenv("SEARXNG_SECRET_KEY", raising=False)
    else:
        monkeypatch.setenv("SEARXNG_SECRET_KEY", secret)
    monkeypatch.setattr(sys, "argv", ["render_settings", str(ROOT / "searxng/settings.template.yml"), str(tmp_path / "out")])
    with pytest.raises(ValueError, match="SEARXNG_SECRET_KEY"):
        renderer.main()


def test_camoufox_keeps_only_sys_chroot_capability():
    """Firefox's content sandbox chroot helper dies (SIGSEGV per renderer) without CAP_SYS_CHROOT."""
    compose = yaml.safe_load((ROOT / "podman-compose.yml").read_text())
    camoufox = compose["services"]["camoufox"]
    assert camoufox["cap_drop"] == ["ALL"]
    assert camoufox["cap_add"] == ["SYS_CHROOT"]
    assert "no-new-privileges:true" in camoufox["security_opt"]


def test_compose_egress_guard_hostnames_stay_parameterized():
    """scripts/apple_compose.py DNS-qualifies the egress-guard host via this variable.

    Bare `egress-guard` hostnames would never reach Apple container's embedded
    DNS (single-label names are not routed); the podman default must stay
    unqualified. See AGENTS.md design decision 18.
    """
    compose = yaml.safe_load((ROOT / "podman-compose.yml").read_text())
    qualified = "${EGRESS_GUARD_HOST:-egress-guard}"
    assert f"CAMOUFOX_PROXY_SERVER=http://{qualified}:8081" in compose["services"]["camoufox"]["environment"]
    bridge_env = compose["services"]["bridge"]["environment"]
    assert f"HTTP_FASTPATH_PROXY=http://{qualified}:8082" in bridge_env
    assert f"JINA_PROXY=${{JINA_PROXY:-http://{qualified}:8082}}" in bridge_env
    text = (ROOT / "podman-compose.yml").read_text()
    assert "http://egress-guard:" not in text, "hardcoded egress-guard hostnames regressed"


def test_compose_profile_selected_by_enable_flag(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    compose = load("compose_wrapper", "scripts/compose.py")
    assert not compose.searxng_enabled({})
    assert not compose.searxng_enabled({"SEARXNG_ENABLED": "false"})
    assert compose.searxng_enabled({"SEARXNG_ENABLED": "true"})
    assert compose.compose_command("podman", ["up", "-d"], False) == [
        "podman", "compose", "-f", "podman-compose.yml", "up", "-d",
    ]
    assert compose.compose_command("podman", ["up", "-d"], True) == [
        "podman", "compose", "-f", "podman-compose.yml", "--profile", "searxng", "up", "-d",
    ]


def test_compose_detect_runtime(monkeypatch):
    """Linux defaults to podman; macOS prefers Apple container when installed."""
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    compose = load("compose_wrapper", "scripts/compose.py")
    monkeypatch.setattr(compose.sys, "platform", "linux")
    assert compose.detect_runtime() == "podman"

    monkeypatch.setattr(compose.sys, "platform", "darwin")
    monkeypatch.setitem(compose.sys.modules, "apple_compose", _FakeAdapter(monkeypatch, found=True))
    assert compose.detect_runtime() == "container"
    monkeypatch.setitem(compose.sys.modules, "apple_compose", _FakeAdapter(monkeypatch, found=False))
    assert compose.detect_runtime() == "podman"


class _FakeAdapter:
    """Stands in for scripts/apple_compose.py in detect_runtime tests."""

    def __init__(self, monkeypatch, found: bool):
        if found:
            self.find_binary = lambda: "/usr/local/bin/container"
        else:
            def missing():
                raise SystemExit("not installed")
            self.find_binary = missing


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
    monkeypatch.setattr(sys, "argv", ["compose.py", "--runtime", "podman", "up", "-d"])
    assert compose.main() == 0
    assert calls[1] == ["podman", "stop", "ws-searxng", "ws-valkey"]
    assert calls[2] == ["podman", "compose", "-f", "podman-compose.yml", "up", "-d"]
