"""Network-free tests for the Apple container compose adapter."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def adapter():
    return load("apple_compose", "scripts/apple_compose.py")


def svc(adapter, name: str, **kwargs):
    defaults = dict(name=name, container_name=f"ws-{name}")
    defaults.update(kwargs)
    return adapter.Service(**defaults)


# ---------------------------------------------------------------------------
# interpolate


def test_interpolate_compose_semantics(adapter):
    values = {"SET": "v", "EMPTY": ""}
    assert adapter.interpolate("${SET}", values) == "v"
    assert adapter.interpolate("${MISSING}", values) == ""
    assert adapter.interpolate("${MISSING:-d}", values) == "d"
    assert adapter.interpolate("${EMPTY:-d}", values) == "d"
    assert adapter.interpolate("${EMPTY-d}", values) == ""
    assert adapter.interpolate("${MISSING-d}", values) == "d"
    assert adapter.interpolate("$${SET}", values) == "${SET}"
    assert adapter.interpolate("a${SET}b", values) == "avb"


# ---------------------------------------------------------------------------
# qualify_url: intra-stack hostnames -> live addresses


def test_qualify_url_resolves_bare_service_hosts(adapter):
    values = {
        "_service_names": {"bridge", "camoufox"},
        "_addresses": {"bridge": "bridge.test", "camoufox": "192.168.64.6"},
    }
    assert adapter.qualify_url("http://bridge:8000", values) == "http://bridge.test:8000"
    assert adapter.qualify_url("ws://camoufox:9222/browser", values) == "ws://192.168.64.6:9222/browser"
    assert adapter.qualify_url("http://user:pass@bridge/x", values) == "http://user:pass@bridge.test/x"


def test_qualify_url_resolves_bare_service_name_values(adapter):
    values = {"_service_names": {"valkey"}, "_addresses": {"valkey": "192.168.64.4"}}
    assert adapter.qualify_url("valkey", values) == "192.168.64.4"
    assert adapter.qualify_url("browser", values) == "browser"


def test_qualify_url_leaves_other_values_alone(adapter):
    values = {"_service_names": {"bridge"}, "_addresses": {"bridge": "bridge.test"}}
    assert adapter.qualify_url("http://localhost:8000", values) == "http://localhost:8000"
    assert adapter.qualify_url("http://egress-guard.test:8081", values) == "http://egress-guard.test:8081"
    assert adapter.qualify_url("http://unknown:1", values) == "http://unknown:1"
    assert adapter.qualify_url("UTC", values) == "UTC"
    assert adapter.qualify_url("INFO", values) == "INFO"


def test_qualify_url_without_addresses_is_noop(adapter):
    values = {"_service_names": {"bridge"}, "_addresses": {}}
    assert adapter.qualify_url("http://bridge:8000", values) == "http://bridge:8000"


def test_service_env_qualifies_against_live_addresses(adapter):
    service = svc(adapter, "bridge", environment={"SEARXNG_URL": "http://searxng:8080", "TZ": "UTC"})
    values = {"_service_names": {"searxng", "bridge"}, "_addresses": {"searxng": "searxng.test"}}
    assert adapter.service_env(service, values) == {"SEARXNG_URL": "http://searxng.test:8080", "TZ": "UTC"}


# ---------------------------------------------------------------------------
# compose file translation


@pytest.fixture()
def parsed(adapter, monkeypatch):
    monkeypatch.setattr(adapter, "env_values", lambda: {"APP_UID": "501", "APP_GID": "20"})
    services, volumes = adapter.parse_compose(domain="test")
    names = set(services)
    values = {"_service_names": names, "_addresses": adapter._static_addresses(names, "test")}
    return adapter, services, volumes, values


def test_parse_compose_env_stays_raw_until_service_env(parsed):
    """parse_compose interpolates but leaves addressing to service_env()."""
    adapter, services, _, values = parsed
    assert services["bridge"].environment["SEARXNG_URL"] == "http://searxng:8080"
    assert services["camoufox"].environment["CAMOUFOX_PROXY_SERVER"] == "http://egress-guard:8081"
    resolved = {name: adapter.service_env(svc, values) for name, svc in services.items()}
    assert resolved["bridge"]["SEARXNG_URL"] == "http://searxng.test:8080"
    assert resolved["bridge"]["CAMOUFOX_WS_URL"] == "ws://camoufox.test:9222/browser"
    assert resolved["bridge"]["HTTP_FASTPATH_PROXY"] == "http://egress-guard.test:8082"
    assert resolved["camoufox"]["CAMOUFOX_PROXY_SERVER"] == "http://egress-guard.test:8081"
    assert resolved["mcp"]["BRIDGE_URL"] == "http://bridge.test:8000"
    assert resolved["searxng"]["VALKEY_HOST"] == "valkey.test"


def test_parse_compose_dns_and_ip_modes(adapter):
    """DNS mode qualifies names; IP fallback mode injects started containers' IPs."""
    services, _ = adapter.parse_compose(domain="test")
    names = set(services)
    dns_values = {"_service_names": names, "_addresses": adapter._static_addresses(names, "test")}
    ip_values = {"_service_names": names, "_addresses": {"camoufox": "192.168.64.6"}}
    bridge_dns = adapter.service_env(services["bridge"], dns_values)
    assert bridge_dns["CAMOUFOX_WS_URL"] == "ws://camoufox.test:9222/browser"
    bridge_ip = adapter.service_env(services["bridge"], ip_values)
    assert bridge_ip["CAMOUFOX_WS_URL"] == "ws://192.168.64.6:9222/browser"


def test_parse_compose_interpolates_ports_sizes_and_pins(parsed):
    adapter, services, volumes, _ = parsed
    assert services["bridge"].ports == ["127.0.0.1:8000:8000"]
    assert services["camoufox"].mem_limit == "4g"
    assert services["camoufox"].shm_size == "4gb"
    assert services["searxng"].image.startswith("docker.io/searxng/searxng:")
    assert set(volumes) == {"valkey-data", "searxng-data"}


def test_parse_compose_preserves_capabilities(parsed):
    _, services, _, _ = parsed
    assert services["camoufox"].cap_drop == ["ALL"]
    assert services["camoufox"].cap_add == ["SYS_CHROOT"]
    assert services["bridge"].cap_drop == ["ALL"]
    assert services["bridge"].cap_add == []


def test_parse_compose_dependency_graph(parsed):
    _, services, _, _ = parsed
    assert services["bridge"].depends_on == ["camoufox"]
    assert services["mcp"].depends_on == ["bridge"]
    assert services["camoufox"].depends_on == ["egress-guard"]
    assert services["searxng"].depends_on == ["valkey"]


# ---------------------------------------------------------------------------
# ordering and profiles


def test_order_services_topological(adapter):
    a = svc(adapter, "a")
    b = svc(adapter, "b", depends_on=["a"])
    c = svc(adapter, "c", depends_on=["b", "a"])
    assert [s.name for s in adapter.order_services([c, b, a])] == ["a", "b", "c"]


def test_order_services_rejects_unknown_and_cycles(adapter):
    with pytest.raises(SystemExit, match="depends on disabled"):
        adapter.order_services([svc(adapter, "a", depends_on=["ghost"])])
    x = svc(adapter, "x", depends_on=["y"])
    y = svc(adapter, "y", depends_on=["x"])
    with pytest.raises(SystemExit, match="cyclic"):
        adapter.order_services([x, y])


def test_enabled_services_honors_searxng_profile(adapter, monkeypatch):
    monkeypatch.setattr(adapter, "env_values", lambda: {})
    services, _ = adapter.parse_compose()
    values_off = {"SEARXNG_ENABLED": "false"}
    values_on = {"SEARXNG_ENABLED": "true"}

    off = [s.name for s in adapter.enabled_services(services, values_off)]
    on = [s.name for s in adapter.enabled_services(services, values_on)]
    assert "searxng" not in off and "valkey" not in off
    assert "searxng" in on and "valkey" in on
    assert off == ["egress-guard", "camoufox", "bridge", "mcp"]
    assert on == ["egress-guard", "valkey", "searxng", "camoufox", "bridge", "mcp"]


# ---------------------------------------------------------------------------
# run argv construction


def test_run_argv_camoufox(adapter):
    camoufox = svc(
        adapter, "camoufox",
        image="localhost/ws-camoufox:local",
        mem_limit="4g", shm_size="4gb",
        cap_drop=["ALL"], cap_add=["SYS_CHROOT"],
        environment={"TZ": "UTC", "CAMOUFOX_PORT": "9222"},
        ports=["127.0.0.1:9223:9222"],
    )
    argv = adapter.run_argv("/bin/container", camoufox, {"TZ": "UTC"})
    assert argv[:4] == ["/bin/container", "run", "-d", "--name"]
    assert "ws-camoufox" in argv
    assert argv[argv.index("--network") + 1] == "default,alias=camoufox"
    assert argv[argv.index("--memory") + 1] == "4G"
    assert argv[argv.index("--shm-size") + 1] == "4G"
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--cap-add") + 1] == "SYS_CHROOT"
    assert argv[argv.index("-p") + 1] == "127.0.0.1:9223:9222"
    assert argv[argv.index("-e") + 1] == "TZ=UTC"
    assert argv[-1] == "localhost/ws-camoufox:local"


def test_run_argv_entrypoint_and_command(adapter):
    searxng = svc(
        adapter, "searxng", image="docker.io/searxng/searxng:latest",
        entrypoint=["/bin/sh", "/usr/local/bin/ws-searxng-entrypoint.sh"],
        command=["valkey-server", "--save", "30 1"],
    )
    argv = adapter.run_argv("/bin/container", searxng, {})
    assert argv[argv.index("--entrypoint") + 1] == "/bin/sh"
    assert argv[argv.index("docker.io/searxng/searxng:latest") + 1:] == [
        "/usr/local/bin/ws-searxng-entrypoint.sh", "valkey-server", "--save", "30 1",
    ]


def test_as_args_splits_string_commands_like_compose(adapter):
    assert adapter._as_args("valkey-server --save 30 1 --loglevel warning") == [
        "valkey-server", "--save", "30", "1", "--loglevel", "warning",
    ]
    assert adapter._as_args(["a", "b"]) == ["a", "b"]
    assert adapter._as_args(None) == []


# ---------------------------------------------------------------------------
# volume specs, sizes, healthchecks, DNS list parsing


def test_volume_spec_resolution(adapter):
    assert adapter.volume_spec("valkey-data:/data") == "valkey-data:/data"
    assert adapter.volume_spec("valkey-data:/data:ro") == "valkey-data:/data:ro"
    bind = adapter.volume_spec("./searxng/limiter.toml:/etc/searxng/limiter.toml:ro")
    assert bind == f"{ROOT}/searxng/limiter.toml:/etc/searxng/limiter.toml:ro"
    assert adapter.volume_spec("/abs/path:/target").startswith("/abs/path:/target")


def test_normalize_size(adapter):
    assert adapter.normalize_size("256m") == "256M"
    assert adapter.normalize_size("1g") == "1G"
    assert adapter.normalize_size("4gb") == "4G"
    assert adapter.normalize_size("512") == "512B"
    assert adapter.normalize_size("weird") == "weird"


def test_health_argv_cmd_and_shell(adapter):
    guard = svc(adapter, "egress-guard", healthcheck={"test": ["CMD", "valkey-cli", "ping"]})
    assert adapter.health_argv(guard) == ["valkey-cli", "ping"]
    bridge = svc(adapter, "bridge", healthcheck={"test": ["CMD-SHELL", "curl -sf http://localhost/health"]})
    assert adapter.health_argv(bridge) == ["sh", "-c", "curl -sf http://localhost/health"]
    bare = svc(adapter, "x", healthcheck={"test": "python -c 'ok()'"})
    assert adapter.health_argv(bare) == ["sh", "-c", "python -c 'ok()'"]


def test_health_deadline_uses_compose_fields(adapter):
    fast = svc(adapter, "fast", healthcheck={"interval": "5s", "retries": 2, "timeout": "1s"})
    slow = svc(adapter, "slow", healthcheck={
        "interval": "60s", "retries": 5, "timeout": "5s", "start_period": "20s",
    })
    assert 10 < adapter.health_deadline(fast) < 30
    assert adapter.health_deadline(slow) >= 300


def test_parse_dns_domains_table(adapter):
    assert adapter._parse_dns_domains("DOMAIN\n") == set()
    assert adapter._parse_dns_domains("DOMAIN\ntest\n") == {"test"}
    assert adapter._parse_dns_domains("DOMAIN\ntest  \nws.local\n") == {"test", "ws.local"}
