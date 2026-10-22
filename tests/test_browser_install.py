"""Build installer must select the exact release and reject silent pin drift."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.mark.parametrize("actual", ["158.0-beta.40", "152.0.4-beta.31"])
def test_exact_browser_install(monkeypatch, actual):
    path = Path(__file__).resolve().parents[1] / "camoufox/install_browser.py"
    spec = importlib.util.spec_from_file_location("browser_installer", path)
    assert spec and spec.loader
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    calls = []
    selected = []
    cli = ModuleType("camoufox.__main__")
    package = ModuleType("camoufox.pkgman")

    class Update:
        def __init__(self, **kwargs):
            selected.append(kwargs["selected_version"])

        def update(self, **kwargs):
            assert kwargs == {"i_know_what_im_doing": True}

    cli.CamoufoxUpdate = Update
    cli.load_repo_cache = lambda: {"repos": [{"name": "Official", "versions": [
        {"version": "158.0", "build": "beta.40", "url": "https://example.com/browser.zip", "is_prerelease": True},
    ]}]}
    package.AvailableVersion = lambda **kwargs: kwargs
    package.Version = lambda *args: args
    package.RepoConfig = SimpleNamespace(find_by_name=lambda name: "official")
    package.installed_verstr = lambda: actual
    monkeypatch.setitem(sys.modules, "camoufox", ModuleType("camoufox"))
    monkeypatch.setitem(sys.modules, "camoufox.__main__", cli)
    monkeypatch.setitem(sys.modules, "camoufox.pkgman", package)
    monkeypatch.setattr(installer.subprocess, "run", lambda command, **kwargs: calls.append(command))
    monkeypatch.setenv("EXPECTED_BROWSER_VERSION", "158.0-beta.40")
    if actual != "158.0-beta.40":
        with pytest.raises(RuntimeError, match="pin mismatch"):
            installer.main()
    else:
        installer.main()
    assert calls[0][-1] == "sync"
    assert calls[1][-1] == "official/prerelease/158.0-beta.40"
    assert selected[0]["version"] == ("beta.40", "158.0")
