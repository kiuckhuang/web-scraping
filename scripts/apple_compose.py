#!/usr/bin/env python3
"""Compose orchestrator for Apple `container` on macOS.

`make CONTAINER=container ...` routes Compose targets here. podman-compose.yml
stays the single source of truth: this script translates it into native
`container` CLI invocations, because Apple `container` ships no compose
implementation, no healthchecks and no restart policies:

- builds or pulls the image each service references
- creates the named volumes the enabled services reference
- starts services in `depends_on` order, replicating each compose healthcheck
  with `container exec` probes and waiting it out (`service_healthy`)
- `down` stops and deletes everything the stack created

Apple container DNS: containers resolve each other as `<name>.<dns-domain>`.
The script ensures `[dns] domain` is configured (default "test") and passes
every service name as a network alias. When the matching host-side resolver
entry is missing (one-time `sudo container system dns create <domain>`), the
script falls back to injecting each started container's IP into the dependent
services' environment, so `up` works without the admin step.

Known divergences from the podman runtime (documented in the README):
- all services share the `default` network — the internal/edge segmentation
  is not enforced
- `restart: unless-stopped`, `pids_limit` and `security_opt` are ignored

Subcommands mirror the subset of the compose CLI the Makefile uses:
build, up, down, logs, ps, exec, pull-image, prune.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = ROOT / "podman-compose.yml"

# Bare service names used as hostnames inside container URLs (SEARXNG_URL,
# CAMOUFOX_WS_URL, ...) never reach Apple container's embedded DNS service;
# they must be qualified with the configured DNS domain.
DNS_DOMAIN_DEFAULT = "test"
CONTAINER_CONFIG = Path.home() / ".config" / "container" / "config.toml"

PYAML_PIN = "PyYAML==6.0.4"  # matches bridge/pyproject.toml
BOOTSTRAP_VENV = ROOT / ".venv-compose"


def ensure_yaml() -> None:
    """Make PyYAML importable on the host python (PEP 668-safe).

    System python installs refuse `pip install --user`; instead bootstrap a
    dedicated venv once and re-exec this script under it.
    """
    try:
        import yaml  # noqa: F401, PLC0415
    except ImportError:
        pass
    else:
        return
    python = BOOTSTRAP_VENV / "bin" / "python"
    if not python.exists():
        print(f"bootstrapping {BOOTSTRAP_VENV.name} (PyYAML for compose parsing)...")
        subprocess.run([sys.executable, "-m", "venv", str(BOOTSTRAP_VENV)], check=True)
    subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", PYAML_PIN],
        check=True,
    )
    os.execv(str(python), [str(python), str(Path(__file__).resolve()), *sys.argv[1:]])


def import_yaml() -> Any:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - guarded by ensure_yaml()
        raise SystemExit("PyYAML missing; call ensure_yaml() first") from exc
    return yaml


# ---------------------------------------------------------------------------
# Environment handling
# ---------------------------------------------------------------------------


def env_values() -> dict[str, str]:
    """Merge .env with the process environment (process env wins, like compose)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from init import ENV_FILE, parse_env_file  # noqa: PLC0415

    values = parse_env_file(ENV_FILE) if ENV_FILE.exists() else {}
    values.update(os.environ)
    return values


_VAR_PATTERN = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<op>:?-)(?P<default>[^}]*))?\}")


def interpolate(text: str, values: dict[str, str]) -> str:
    """Substitute ${VAR}, ${VAR:-default} and ${VAR-default} (compose semantics)."""

    def sub(match: re.Match[str]) -> str:
        name, op, default = match.group("name"), match.group("op"), match.group("default")
        if name not in values:
            return default if op else ""
        if op == ":-" and values[name] == "":
            return default
        return values[name]

    placeholder = "\x00"
    return _VAR_PATTERN.sub(sub, text.replace("$$", placeholder)).replace(placeholder, "$")


# ---------------------------------------------------------------------------
# Compose model
# ---------------------------------------------------------------------------


@dataclass
class Service:
    name: str
    container_name: str
    image: str | None = None
    build: dict[str, Any] | None = None
    entrypoint: list[str] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    ports: list[str] = field(default_factory=list)
    volumes: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    healthcheck: dict[str, Any] | None = None
    mem_limit: str | None = None
    shm_size: str | None = None
    cap_add: list[str] = field(default_factory=list)
    cap_drop: list[str] = field(default_factory=list)
    profiles: list[str] = field(default_factory=list)


def parse_compose(path: Path = COMPOSE_FILE, domain: str = "") -> tuple[dict[str, Service], list[str]]:
    """Parse podman-compose.yml into services + declared named volumes.

    Environment values are interpolated against .env/process env; service
    hostnames are left bare here — `service_env()` qualifies them at start
    time once the addressing mode (DNS names vs. container IPs) is known.
    """
    yaml = import_yaml()
    raw = yaml.safe_load(path.read_text())
    base = env_values()
    service_names = set(raw.get("services", {}))

    values = dict(base)
    values["_service_names"] = service_names
    values["_addresses"] = _static_addresses(service_names, domain)

    services: dict[str, Service] = {}
    for name, spec in raw.get("services", {}).items():
        env_raw = spec.get("environment", {}) or {}
        if isinstance(env_raw, list):
            env_raw = dict(_split_env(item) for item in env_raw)
        services[name] = Service(
            name=name,
            container_name=spec.get("container_name", name),
            image=interpolate(spec["image"], values) if "image" in spec else None,
            build=spec.get("build"),
            entrypoint=_as_args(spec.get("entrypoint")),
            command=_as_args(spec.get("command")),
            environment={key: interpolate(str(val), values) for key, val in env_raw.items()},
            ports=[interpolate(str(p), values) for p in spec.get("ports", []) or []],
            volumes=[interpolate(str(v), values) for v in spec.get("volumes", []) or []],
            depends_on=list((spec.get("depends_on") or {}).keys()),
            healthcheck=spec.get("healthcheck"),
            mem_limit=interpolate(spec["mem_limit"], values) if spec.get("mem_limit") else None,
            shm_size=interpolate(spec["shm_size"], values) if spec.get("shm_size") else None,
            cap_add=[str(c) for c in spec.get("cap_add", []) or []],
            cap_drop=[str(c) for c in spec.get("cap_drop", []) or []],
            profiles=[str(p) for p in spec.get("profiles", []) or []],
        )
    return services, [interpolate(str(v), values) for v in (raw.get("volumes") or {})]


def _as_args(value: Any) -> list[str]:
    """Compose command/entrypoint: lists pass through, strings are shlex-split."""
    if value is None:
        return []
    if isinstance(value, str):
        return shlex.split(value)
    return [str(item) for item in value]


def _static_addresses(service_names: set[str], domain: str) -> dict[str, str]:
    """DNS-mode address map: every service resolves as `<name>.<domain>`."""
    if not domain:
        return {}
    return {name: f"{name}.{domain}" for name in service_names}


def _split_env(item: str) -> tuple[str, str]:
    key, _, value = item.partition("=")
    return key.strip(), value


_URL_PATTERN = re.compile(
    r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<userinfo>(?:[^@/]*@)?)(?P<host>[^@/:?#]*)(?P<rest>.*)$"
)


def qualify_url(value: str, values: dict[str, str]) -> str:
    """Resolve stack service hostnames in env values to reachable addresses.

    Two shapes are resolved, anything else passes through unchanged:
    - a URL whose host is a bare service name (`http://bridge:8000`)
    - a value that is exactly a service name (`VALKEY_HOST=valkey`)

    `_addresses` maps service name -> FQDN (DNS mode) or container IP
    (fallback mode); dotted hosts like `localhost` are never touched.
    """
    addresses = values.get("_addresses", {})
    names = values.get("_service_names", set())
    if not addresses:
        return value
    if value in names:
        return addresses.get(value, value)
    match = _URL_PATTERN.match(value)
    if not match:
        return value
    host = match.group("host")
    if not host or "." in host or host not in names:
        return value
    address = addresses.get(host)
    if not address:
        # No live address (e.g. a disabled, never-started service) — keep the
        # bare name; consumers treat it as unreachable via their own breakers.
        return value
    return f"{match.group('scheme')}{match.group('userinfo')}{address}{match.group('rest')}"


def service_env(svc: Service, values: dict[str, str]) -> dict[str, str]:
    """The service environment with intra-stack hostnames resolved."""
    context = dict(values)
    context.setdefault("_service_names", {svc.name})
    return {key: qualify_url(val, context) for key, val in svc.environment.items()}


def searxng_enabled(values: dict[str, str]) -> bool:
    return values.get("SEARXNG_ENABLED", "false").strip().strip("\"'").lower() not in (
        "", "0", "false", "no", "off",
    )


def enabled_services(services: dict[str, Service], values: dict[str, str]) -> list[Service]:
    """Profile-aware dependency-ordered service list (mirrors compose.py)."""
    enabled = searxng_enabled(values)
    selected = [
        svc for svc in services.values()
        if not svc.profiles or ("searxng" in svc.profiles and enabled)
    ]
    return order_services(selected)


def order_services(services: list[Service]) -> list[Service]:
    """Topological sort by depends_on; raises on unknown or cyclic deps."""
    by_name = {svc.name: svc for svc in services}
    ordered: list[Service] = []
    done: set[str] = set()
    pending = list(services)
    while pending:
        progressed = False
        remaining = []
        for svc in pending:
            missing = [dep for dep in svc.depends_on if dep not in by_name]
            if missing:
                raise SystemExit(
                    f"service {svc.name} depends on disabled/unknown services {missing} "
                    "(enable them, e.g. SEARXNG_ENABLED=true)"
                )
            if all(dep in done for dep in svc.depends_on):
                ordered.append(svc)
                done.add(svc.name)
                progressed = True
            else:
                remaining.append(svc)
        if not progressed:
            raise SystemExit(f"cyclic depends_on among: {[s.name for s in remaining]}")
        pending = remaining
    return ordered


# ---------------------------------------------------------------------------
# container CLI plumbing
# ---------------------------------------------------------------------------


def find_binary() -> str:
    """Locate the `container` CLI: env override, PATH, then the dev build."""
    override = os.environ.get("CONTAINER_BIN")
    if override:
        return override
    found = shutil.which("container")
    if found:
        return found
    for candidate in (
        Path.home() / "github/container/bin/container",
        Path.home() / "github/container/bin/release/container",
    ):
        if candidate.exists():
            return str(candidate)
    raise SystemExit(
        "the Apple `container` CLI was not found — install it "
        "(https://github.com/apple/container) or point CONTAINER_BIN at the binary"
    )


def run(binary: str, *args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [binary, *args], cwd=ROOT, check=False, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise SystemExit(f"`container {' '.join(args)}` failed: {detail}")
    return result


def ensure_service_started(binary: str) -> None:
    if run(binary, "ls", check=False, capture=True).returncode == 0:
        return
    print("starting the container system service...")
    run(binary, "system", "start")
    run(binary, "ls", capture=True)  # must succeed now


def read_dns_domain() -> str:
    if not CONTAINER_CONFIG.exists():
        return ""
    try:
        config = tomllib.loads(CONTAINER_CONFIG.read_text())
    except tomllib.TOMLDecodeError as exc:
        print(f"warning: cannot parse {CONTAINER_CONFIG}: {exc}")
        return ""
    return str((config.get("dns") or {}).get("domain", ""))


def ensure_dns_domain(binary: str, default: str = DNS_DOMAIN_DEFAULT) -> str:
    """Guarantee `[dns] domain` exists so containers can resolve each other."""
    domain = read_dns_domain()
    if domain:
        return domain
    CONTAINER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    existing = CONTAINER_CONFIG.read_text() if CONTAINER_CONFIG.exists() else ""
    backup = CONTAINER_CONFIG.with_suffix(".toml.bak")
    if existing:
        backup.write_text(existing)
        print(f"backed up {CONTAINER_CONFIG} to {backup}")
    CONTAINER_CONFIG.write_text(_inject_dns_domain(existing, default))
    print(f"configured container DNS domain \"{default}\" in {CONTAINER_CONFIG} (service restarts once)")
    run(binary, "system", "stop", check=False)
    run(binary, "system", "start")
    return default


def _inject_dns_domain(config: str, domain: str) -> str:
    """Insert `domain` under an existing [dns] table, else append a new one."""
    lines = config.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == "[dns]":
            return "\n".join([*lines[:index + 1], f'domain = "{domain}"', *lines[index + 1:]]) + "\n"
    text = config if not config or config.endswith("\n") else f"{config}\n"
    return f"{text}[dns]\ndomain = \"{domain}\"\n"


# ---------------------------------------------------------------------------
# Images, volumes, containers
# ---------------------------------------------------------------------------


def image_exists(binary: str, reference: str) -> bool:
    return run(binary, "image", "inspect", reference, check=False, capture=True).returncode == 0


def volume_exists(binary: str, name: str) -> bool:
    return run(binary, "volume", "inspect", name, check=False, capture=True).returncode == 0


def ensure_image(binary: str, svc: Service, values: dict[str, str], force_build: bool = False) -> None:
    if not svc.image:
        raise SystemExit(f"service {svc.name} declares neither image nor build")
    if svc.build and (force_build or not image_exists(binary, svc.image)):
        build_one(binary, svc, values)
        return
    if not svc.build and not image_exists(binary, svc.image):
        print(f"pulling {svc.image} ({svc.name})")
        run(binary, "image", "pull", svc.image)
    if not image_exists(binary, svc.image):
        raise SystemExit(f"image {svc.image} for {svc.name} is not available")


def build_one(binary: str, svc: Service, values: dict[str, str]) -> None:
    build = svc.build or {}
    context = ROOT / str(build.get("context", "."))
    argv = [binary, "build"]
    for key, val in (build.get("args") or {}).items():
        argv += ["--build-arg", f"{key}={interpolate(str(val), values)}"]
    dockerfile = build.get("dockerfile", "Dockerfile")
    if dockerfile and dockerfile != "Dockerfile":
        argv += ["-f", str(context / dockerfile)]
    argv += ["-t", svc.image or "", str(context)]
    print(f"building {svc.image} ({svc.name})")
    run(*argv)


def referenced_volumes(services: list[Service]) -> dict[str, str]:
    """Named volumes (not bind mounts) the given services mount, with an image."""
    volumes: dict[str, str] = {}
    for svc in services:
        for spec in svc.volumes:
            source = spec.split(":")[0]
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", source) and source not in volumes:
                volumes[source] = svc.image or ""
    return volumes


def volume_spec(spec: str) -> str:
    """Resolve compose volume specs: bare names are volumes, paths bind mounts."""
    parts = spec.split(":")
    if parts and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", parts[0]):
        source = Path(parts[0]).expanduser()
        if not source.is_absolute():
            source = ROOT / source
        parts[0] = str(source)
    return ":".join(parts)


def normalize_size(spec: str) -> str:
    """Compose sizes (256m, 4gb) -> container CLI sizes (256M, 4G)."""
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgt]?)(i?b?)", spec.strip(), re.IGNORECASE)
    if not match:
        return spec
    suffix = (match.group(2) or "b").upper()
    return f"{match.group(1)}{suffix}"


def run_argv(binary: str, svc: Service, env: dict[str, str]) -> list[str]:
    argv = [binary, "run", "-d", "--name", svc.container_name, "--network", f"default,alias={svc.name}"]
    if svc.mem_limit:
        argv += ["--memory", normalize_size(svc.mem_limit)]
    if svc.shm_size:
        argv += ["--shm-size", normalize_size(svc.shm_size)]
    for cap in svc.cap_drop:
        argv += ["--cap-drop", cap]
    for cap in svc.cap_add:
        argv += ["--cap-add", cap]
    # Apple named volumes start empty and root-owned — unlike podman, the
    # image's volume content and ownership are not seeded into them. The
    # valkey entrypoint chowns /data before dropping to the valkey user
    # (CAP_SETGID/SETUID alone), which needs CAP_CHOWN on this runtime.
    if svc.name == "valkey" and "CHOWN" not in svc.cap_add:
        argv += ["--cap-add", "CHOWN"]
    for key, val in env.items():
        argv += ["-e", f"{key}={val}"]
    for port in svc.ports:
        argv += ["-p", port]
    for vol in svc.volumes:
        argv += ["-v", volume_spec(vol)]
    if svc.entrypoint:
        argv += ["--entrypoint", svc.entrypoint[0]]
    argv += [svc.image or ""]
    argv += svc.entrypoint[1:] + svc.command
    return argv


def container_state(binary: str) -> dict[str, tuple[str, str]]:
    """Map container id -> (state, ipv4 address) over all containers."""
    result = run(binary, "ls", "--all", "--format", "json", check=False, capture=True)
    if result.returncode != 0 or not (result.stdout or "").strip():
        return {}
    states: dict[str, tuple[str, str]] = {}
    for item in json.loads(result.stdout):
        name = (item.get("configuration") or {}).get("id") or item.get("id") or ""
        status = item.get("status") or {}
        state = status.get("state") or item.get("state") or ""
        ip = ""
        networks = status.get("networks") or []
        if networks:
            ip = str(networks[0].get("ipv4Address", "")).split("/")[0]
        if name:
            states[name] = (state, ip)
    return states


def running_names(binary: str) -> list[str]:
    return [name for name, (state, _) in container_state(binary).items() if state == "running"]


def registered_dns_domains(binary: str) -> set[str]:
    """Domains registered on the host resolver (`sudo container system dns create`)."""
    result = run(binary, "system", "dns", "list", check=False, capture=True)
    if result.returncode != 0:
        return set()
    return _parse_dns_domains(result.stdout or "")


def _parse_dns_domains(output: str) -> set[str]:
    """Parse `container system dns list` table output (skips the DOMAIN header)."""
    domains = set()
    for line in output.splitlines()[1:]:
        name = line.strip().split()[0] if line.strip() else ""
        if name and name != "DOMAIN":
            domains.add(name)
    return domains


# ---------------------------------------------------------------------------
# Healthchecks
# ---------------------------------------------------------------------------


def health_argv(svc: Service) -> list[str]:
    """The compose healthcheck test, shaped for `container exec`."""
    test = (svc.healthcheck or {}).get("test") or []
    if isinstance(test, str):
        test = ["CMD-SHELL", test]
    if test and test[0] == "CMD-SHELL":
        return ["sh", "-c", str(test[1])]
    if test and test[0] == "CMD":
        test = test[1:]
    return [str(item) for item in test]


def health_deadline(svc: Service) -> float:
    """Worst-case seconds a healthy service may need (compose semantics)."""
    hc = svc.healthcheck or {}
    start_period = _duration(hc.get("start_period"), 0.0)
    interval = _duration(hc.get("interval"), 30.0)
    retries = int(hc.get("retries", 3))
    timeout = _duration(hc.get("timeout"), 5.0)
    return start_period + interval * retries + timeout + 10.0


def _duration(value: Any, default: float) -> float:
    if value is None:
        return default
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(s|m|h)?", str(value).strip(), re.IGNORECASE)
    if not match:
        return default
    factor = {"s": 1.0, "m": 60.0, "h": 3600.0}.get((match.group(2) or "s").lower(), 1.0)
    return float(match.group(1)) * factor


def wait_healthy(binary: str, svc: Service) -> bool:
    """Replicate the compose healthcheck with `container exec` probes."""
    if not svc.healthcheck:
        return True
    argv = health_argv(svc)
    deadline = time.monotonic() + health_deadline(svc)
    while time.monotonic() < deadline:
        if run(binary, "exec", svc.container_name, *argv, check=False, capture=True).returncode == 0:
            return True
        time.sleep(2.0)
    return False


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_up(args: argparse.Namespace) -> int:
    binary = find_binary()
    ensure_service_started(binary)
    domain = ensure_dns_domain(binary)
    services, _ = parse_compose(domain=domain)
    values = env_values()
    values["_service_names"] = set(services)

    # Addressing mode: DNS names when the host resolver knows the domain
    # (one-time `sudo container system dns create <domain>`), container IPs
    # otherwise. Either way, intra-stack env values resolve at start time.
    if domain in registered_dns_domains(binary):
        values["_addresses"] = {name: f"{name}.{domain}" for name in services}
        dns_mode, mode = True, f"DNS ({domain})"
    else:
        values["_addresses"] = {}
        dns_mode, mode = False, "container IPs"
        print(
            f"note: host resolver has no \"{domain}\" domain — using container IPs.\n"
            f"      For name-based addressing run once: sudo {binary} system dns create {domain}"
        )
    # Baked env holds container IPs in fallback mode; IPs change on every
    # system restart, so `up` recreates the containers for consistent
    # addressing. DNS mode keeps names stable and starts containers in place.
    recreate_all = not dns_mode

    selected = enabled_services(services, values)
    states = container_state(binary)
    if not searxng_enabled(values):
        for name in ("ws-searxng", "ws-valkey"):
            state, _ = states.get(name, ("", ""))
            if state == "running":
                print(f"SEARXNG_ENABLED=false — stopping {name}")
                run(binary, "stop", name, "-t", "10", check=False)
                states[name] = ("stopped", "")

    # IP mode recreates every container, and Apple volumes are single-attach:
    # delete the old containers BEFORE normalizing volume metadata, otherwise
    # the normalization helper cannot mount a volume that a running VM holds.
    if recreate_all or args.force:
        for svc in selected:
            if svc.container_name in states:
                print(f"recreating {svc.container_name} (--force)" if args.force
                      else f"recreating {svc.container_name} (fresh addressing)")
                run(binary, "delete", "--force", svc.container_name, check=False)
                states.pop(svc.container_name, None)

    ensure_volumes(binary, referenced_volumes(selected))

    for svc in selected:
        state, _ = states.get(svc.container_name, ("", ""))
        ensure_image(binary, svc, values)
        if state == "running":
            print(f"{svc.container_name} already running")
        elif state and dns_mode:
            print(f"starting existing {svc.container_name}")
            run(binary, "start", svc.container_name)
        else:
            print(f"starting {svc.container_name} ({svc.name})")
            run(*run_argv(binary, svc, service_env(svc, values)))
        # Dependents started later resolve this service by its live address
        # (IP mode only; DNS mode keeps the stable <name>.<domain> names).
        if not dns_mode:
            _, ip = container_state(binary).get(svc.container_name, ("", ""))
            values["_addresses"][svc.name] = ip or f"{svc.name}.{domain}"
        if not wait_healthy(binary, svc):
            print(f"warning: {svc.container_name} healthcheck did not pass; recent logs:")
            run(binary, "logs", "-n", "30", svc.container_name, check=False)
            return 1

    print(f"\nstack is up (addressing: {mode}):")
    for svc in selected:
        endpoints = ", ".join(svc.ports) if svc.ports else "internal only"
        print(f"  {svc.name:13} {endpoints}")
    _warn_if_publishing_blocked(binary, selected, domain)
    print(
        "\nnotes: containers do not auto-restart on this runtime "
        "(restart: unless-stopped is ignored); `make logs` tails them."
    )
    return 0


def _warn_if_publishing_blocked(binary: str, services: list[Service], domain: str) -> None:
    """Probe published ports; explain the macOS Local Network privacy block.

    The runtime helper (container-runtime-linux) dials the container IPs from
    the host; without Local Network permission every forwarded connection is
    accepted and then reset (apple/container#1702), while the containers
    themselves are perfectly reachable from a permitted process.
    """
    blocked = []
    for svc in services:
        for spec in svc.ports:
            host_ip, _, remainder = spec.partition(":")
            if not remainder:  # "8000:8000" — no host IP part
                host_ip, remainder = "", spec
            host_port = remainder.split("/")[0].split(":")[0]
            ok, detail = _probe_port(host_ip or "127.0.0.1", host_port)
            if not ok:
                blocked.append(f"{svc.container_name} 127.0.0.1:{host_port} ({detail})")
    if not blocked:
        return
    print(
        "\nwarning: published ports are not reachable from this Mac:\n  "
        + "\n  ".join(blocked)
        + "\n\nThis is the macOS Local Network privacy block (apple/container#1702): the\n"
        "runtime helper dials container IPs from the host and is being denied.\n"
        "Fix (one time, GUI):\n"
        "  1. System Settings > Privacy & Security > Local Network\n"
        "  2. enable \"container-runtime-linux\" (and \"container\" if listed)\n"
        f"  3. {binary} system stop && {binary} system start\n"
        "Containers keep running; no rebuild needed. Inter-container traffic is unaffected."
    )


def _probe_port(host: str, port: str, timeout: float = 3.0) -> tuple[bool, str]:
    """Send an HTTP probe; any response bytes count, a reset means blocked."""
    try:
        port_num = int(port)
    except ValueError:
        return True, ""  # non-numeric (shouldn't happen) — don't warn
    try:
        with socket.create_connection((host, port_num), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b"GET / HTTP/1.0\r\nHost: localhost\r\n\r\n")
            data = sock.recv(64)
            return (True, "") if data else (False, "no response")
    except ConnectionResetError:
        return False, "connection reset"
    except OSError as exc:
        return False, str(exc)


def ensure_volumes(binary: str, volumes: dict[str, str]) -> None:
    for name, image in volumes.items():
        if not volume_exists(binary, name):
            print(f"creating volume {name}")
            run(binary, "volume", "create", name)
        _normalize_volume(binary, name, image)


def volume_init_argv(binary: str, name: str, image: str) -> list[str]:
    """Normalize an Apple named volume's ext4 metadata.

    Apple named volumes are ext4 images, so they contain a `lost+found`
    directory that podman's directory-backed volumes do not. It is created
    mode 0700 and the service entrypoint chowns it to the service user
    (valkey does `find . ! -user valkey -exec chown ... +`), after which the
    next start cannot read it as root without DAC capabilities and aborts
    under `set -e`. Make it traversable for root once per `up`; harmless for
    every service image.
    """
    return [
        binary, "run", "--rm",
        # Override the image entrypoint: searxng's upstream entrypoint ignores
        # the command and launches the server, which would hang this helper.
        "--entrypoint", "/bin/sh",
        "--cap-add", "FOWNER", "--cap-add", "DAC_OVERRIDE",
        "-v", f"{name}:/vol", image,
        "-c", "chmod 755 /vol/lost+found 2>/dev/null || true",
    ]


def _normalize_volume(binary: str, name: str, image: str) -> None:
    # Expected to fail while another container still attaches the volume
    # (Apple volumes are single-attach); cmd_up deletes old containers first.
    if not image:
        return
    run(*volume_init_argv(binary, name, image), check=False, capture=True)


def cmd_down(args: argparse.Namespace) -> int:
    binary = find_binary()
    ensure_service_started(binary)
    services, volumes = parse_compose()
    states = container_state(binary)
    failed = False
    for svc in reversed(order_services(list(services.values()))):
        if svc.container_name not in states:
            continue
        print(f"stopping {svc.container_name}")
        run(binary, "stop", svc.container_name, "-t", "10", check=False)
        if run(binary, "delete", svc.container_name, check=False).returncode != 0:
            failed |= run(binary, "delete", "--force", svc.container_name, check=False).returncode != 0
    if args.volumes:
        for name in volumes:
            if volume_exists(binary, name):
                print(f"deleting volume {name}")
                run(binary, "volume", "delete", name, check=False)
    tail = "named volumes kept (pass --volumes to drop them)" if not args.volumes else "volumes deleted"
    print(f"stack is down ({tail})")
    return 1 if failed else 0


def cmd_build(args: argparse.Namespace) -> int:
    binary = find_binary()
    ensure_service_started(binary)
    services, _ = parse_compose()
    values = env_values()
    names = args.service or [svc.name for svc in services.values() if svc.build]
    if not names:
        raise SystemExit("no buildable services selected")
    for name in names:
        svc = services.get(name)
        if svc is None or not svc.build:
            raise SystemExit(f"service {name} has no build section")
        build_one(binary, svc, values)
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    binary = find_binary()
    ensure_service_started(binary)
    services, _ = parse_compose()
    by_container = {svc.container_name: svc.name for svc in services.values()}
    if args.service:
        unknown = [name for name in args.service if name not in services]
        if unknown:
            raise SystemExit(f"unknown service(s): {', '.join(unknown)}")
        names = [services[name].container_name for name in args.service]
    else:
        names = [name for name in running_names(binary) if name in by_container]
    if not names:
        print("no running stack containers to tail")
        return 0
    if not args.follow:
        failed = False
        for name in names:
            if len(names) > 1:
                print(f"==> {name} <==")
            result = run(binary, "logs", "-n", str(args.lines), name, check=False)
            failed |= result.returncode != 0
        return 1 if failed else 0
    return _follow(binary, names, args.lines)


def _follow(binary: str, names: list[str], lines: int) -> int:
    """Interleave `container logs --follow` output from several containers."""
    lines_by_name = {name: queue.Queue() for name in names}

    def pump(name: str, proc: subprocess.Popen) -> None:  # type: ignore[type-arg]
        for line in proc.stdout:  # type: ignore[union-attr]
            lines_by_name[name].put(line.rstrip("\n"))
        lines_by_name[name].put(None)

    procs = {
        name: subprocess.Popen(
            [binary, "logs", "--follow", "-n", str(lines), name],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        for name in names
    }
    threads = [threading.Thread(target=pump, args=(name, proc), daemon=True) for name, proc in procs.items()]
    for thread in threads:
        thread.start()
    try:
        while any(thread.is_alive() for thread in threads):
            for name, lines_queue in lines_by_name.items():
                try:
                    for _ in range(50):  # drain in bursts so one chatty service cannot starve others
                        line = lines_queue.get_nowait()
                        if line is None:
                            break
                        print(f"[{name}] {line}")
                except queue.Empty:
                    continue
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        for proc in procs.values():
            proc.terminate()
    return 0


def cmd_ps(args: argparse.Namespace) -> int:
    binary = find_binary()
    for name in running_names(binary):
        print(name)
    return 0


def cmd_exec(args: argparse.Namespace) -> int:
    binary = find_binary()
    services, _ = parse_compose()
    svc = services.get(args.service)
    if svc is None:
        raise SystemExit(f"unknown service {args.service}")
    argv = [binary, "exec"]
    if sys.stdin.isatty() and sys.stdout.isatty():
        argv.append("-t")
    argv += [svc.container_name, *args.args]
    return subprocess.run(argv, cwd=ROOT, check=False).returncode


def cmd_pull_image(args: argparse.Namespace) -> int:
    binary = find_binary()
    ensure_service_started(binary)
    run(binary, "image", "pull", args.reference)
    return 0


def cmd_pull(args: argparse.Namespace) -> int:
    """Pull the registry images referenced by the enabled services (compose `pull`)."""
    binary = find_binary()
    ensure_service_started(binary)
    services, _ = parse_compose()
    values = env_values()
    for svc in enabled_services(services, values):
        if svc.image and not svc.build:
            print(f"pulling {svc.image} ({svc.name})")
            run(binary, "image", "pull", svc.image)
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    binary = find_binary()
    ensure_service_started(binary)
    return run(binary, "image", "prune", check=False).returncode or 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    ensure_yaml()
    sys.stdout.reconfigure(line_buffering=True)  # interleave subprocess output correctly
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="build/pull images, create volumes, start and health-check services")
    up.add_argument("-d", "--detach", action="store_true", help="detached mode (the default; accepted for compose compatibility)")
    up.add_argument("--force", action="store_true", help="recreate existing containers")
    up.set_defaults(func=cmd_up)

    down = sub.add_parser("down", help="stop and delete the stack containers")
    down.add_argument("-v", "--volumes", action="store_true", help="also delete the named volumes")
    down.set_defaults(func=cmd_down)

    build = sub.add_parser("build", help="(re)build the service images")
    build.add_argument("service", nargs="*", help="services to build (default: all with a build section)")
    build.set_defaults(func=cmd_build)

    logs = sub.add_parser("logs", help="show container logs")
    logs.add_argument("-f", "--follow", action="store_true", help="follow log output")
    logs.add_argument("-n", "--lines", type=int, default=50, help="number of lines to show")
    logs.add_argument("service", nargs="*", help="service names (default: all running)")
    logs.set_defaults(func=cmd_logs)

    ps = sub.add_parser("ps", help="list running stack containers (names only)")
    ps.set_defaults(func=cmd_ps)

    ex = sub.add_parser("exec", help="run a command inside a service container")
    ex.add_argument("service", help="service name (e.g. bridge)")
    ex.add_argument("args", nargs=argparse.REMAINDER, help="command to run")
    ex.set_defaults(func=cmd_exec)

    pull = sub.add_parser("pull-image", help="pull an image reference")
    pull.add_argument("reference", help="image reference (e.g. docker.io/library/python:3.12-slim)")
    pull.set_defaults(func=cmd_pull_image)

    pull_cmd = sub.add_parser("pull", help="pull the registry images of the enabled services (compose `pull`)")
    pull_cmd.set_defaults(func=cmd_pull)

    prune = sub.add_parser("prune", help="prune dangling images")
    prune.set_defaults(func=cmd_prune)

    args = parser.parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
