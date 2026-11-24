"""Shared SSRF guard — single source of truth for public-URL checks.

Used by the bridge edge validation (main.py) and by the in-browser request
guard (browser_client.py), so the two policies cannot drift apart. The edge
validator is strict (public http/https only); the browser guard additionally
allows inert schemes (data:, blob:, about:) that Chromium itself injects.

All DNS resolution runs in worker threads (asyncio.to_thread at call sites).
The per-host verdict is cached for SSRF_DNS_CACHE_TTL seconds because the
browser guard resolves DNS for *every subresource* of every scraped page —
without the cache a JS-heavy page hammers the resolver through the thread
pool. Cache races between worker threads are benign (worst case a duplicate
resolve); CPython dict operations are atomic under the GIL.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import time
from urllib.parse import urlparse

# TTL for cached DNS verdicts (seconds). 0 disables caching (tests).
DNS_CACHE_TTL = float(os.environ.get("SSRF_DNS_CACHE_TTL", "60"))
DNS_CACHE_MAX = 4096  # bound on cached hostnames

# Host names that are always local regardless of what DNS says.
LOCAL_HOSTS = frozenset({"localhost", "0.0.0.0", "127.0.0.1", "::1"})

# IPv6 transition prefixes that embed an IPv4 destination. ipaddress reports
# them as global, so the embedded IPv4 must be classified instead — otherwise a
# NAT64 address such as 64:ff9b::7f00:1 (127.0.0.1) would pass every SSRF layer.
_NAT64_PREFIX = ipaddress.ip_network("64:ff9b::/96")

# Tri-state DNS verdict per host -> (monotonic timestamp, verdict).
_dns_cache: dict[str, tuple[float, str]] = {}


class UrlBlockedError(ValueError):
    """A URL was rejected by the SSRF validator.

    Carries the HTTP status (400 for malformed URLs, 403 for blocked hosts)
    and the detail message so endpoint layers can map it 1:1.
    """

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def clear_dns_cache() -> None:
    """Reset cached DNS verdicts (used between tests)."""
    _dns_cache.clear()


def _embedded_ipv4(ip: ipaddress.IPv4Address | ipaddress.IPv6Address):
    """Map an IPv4-mapped/NAT64 IPv6 address back to the IPv4 it carries."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return ip.ipv4_mapped
        if ip in _NAT64_PREFIX:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for global unicast addresses after unwrapping IPv4 embedding."""
    ip = _embedded_ipv4(ip)
    return ip.is_global and not ip.is_multicast


def _resolve_verdict(host: str) -> str:
    """Resolve host and classify it: 'public', 'private', or 'unresolvable'.

    Unresolvable hosts are a distinct verdict (not 'private') so the edge
    validator can reject them with a clearer message; both are blocked.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return "unresolvable"
    if not infos:
        return "unresolvable"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not _is_public_ip(ip):
            return "private"
    return "public"


def resolve_public_addresses(host: str, port: int) -> list[str]:
    """Fresh resolution for a pinned connection, never a cached DNS verdict."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise UrlBlockedError(403, "Could not resolve host; access blocked") from exc
    addresses = []
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not _is_public_ip(address):
            raise UrlBlockedError(403, "Access to non-global addresses is blocked")
        # Keep the address as resolved (NAT64/mapped hosts must connect in
        # their original family); classification above used the embedded IPv4.
        text = str(address)
        if text not in addresses:
            addresses.append(text)
    if not addresses:
        raise UrlBlockedError(403, "Could not resolve host; access blocked")
    return addresses


def _prune_dns_cache() -> None:
    """Bound cache memory: drop expired entries, then evict the oldest half."""
    now = time.monotonic()
    for host, (ts, _) in list(_dns_cache.items()):
        if now - ts >= DNS_CACHE_TTL:
            _dns_cache.pop(host, None)
    if len(_dns_cache) >= DNS_CACHE_MAX:
        keep = sorted(_dns_cache.items(), key=lambda kv: kv[1][0])[len(_dns_cache) // 2:]
        _dns_cache.clear()
        _dns_cache.update(keep)


def dns_verdict(host: str) -> str:
    """Tri-state DNS verdict ('public' | 'private' | 'unresolvable'), cached."""
    if DNS_CACHE_TTL > 0:
        now = time.monotonic()
        hit = _dns_cache.get(host)
        if hit is not None and now - hit[0] < DNS_CACHE_TTL:
            return hit[1]
    verdict = _resolve_verdict(host)
    if DNS_CACHE_TTL > 0:
        if len(_dns_cache) >= DNS_CACHE_MAX:
            _prune_dns_cache()
        _dns_cache[host] = (time.monotonic(), verdict)
    return verdict


def is_public_http_url(url: str) -> bool:
    """True if a URL is safe to hand to the browser.

    http/https URLs must resolve to public addresses only. Inert browser
    schemes (data:, blob:, about:) are allowed — Chromium generates them
    internally and they carry no network authority.
    """
    try:
        parsed = urlparse(url)
    except ValueError:  # malformed IPv6/host brackets — treat as non-public
        return False
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https") or not parsed.hostname:
        return scheme in ("data", "blob", "about")
    if parsed.hostname.lower() in LOCAL_HOSTS:
        return False
    return dns_verdict(parsed.hostname) == "public"


def validate_public_url(url: str) -> str:
    """Edge validation for user-supplied URLs; returns the URL or raises.

    Raises UrlBlockedError(400) for non-http schemes and missing hosts,
    UrlBlockedError(403) for local/private/unresolvable hosts (unresolvable
    hosts are rejected outright rather than re-resolved later by the browser
    — DNS-rebinding defense).
    """
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise UrlBlockedError(400, "Malformed URL") from exc
    if parsed.scheme.lower() not in ("http", "https"):
        raise UrlBlockedError(400, f"Unsupported scheme: {parsed.scheme}")
    host = parsed.hostname or ""
    if not host:
        raise UrlBlockedError(400, "Missing host")
    if host.lower() in LOCAL_HOSTS:
        raise UrlBlockedError(403, "Access to internal addresses is blocked")
    verdict = dns_verdict(host)
    if verdict == "unresolvable":
        raise UrlBlockedError(403, "Could not resolve host; access blocked")
    if verdict != "public":
        raise UrlBlockedError(403, "Access to internal addresses is blocked")
    return url
