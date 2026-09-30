"""DNS-pinned forward proxy for browser/HTTP egress.

Every HTTP destination and CONNECT tunnel is resolved afresh, restricted to
global addresses, and connected by IP. Optional upstream HTTP(S)/SOCKS5
proxies receive that validated IP rather than re-resolving the target name.
TLS inside CONNECT remains end-to-end (original hostname and fingerprint).
"""
from __future__ import annotations

import base64
import ipaddress
import logging
import os
import select
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import unquote, urlsplit, urlunsplit

from .ssrf import UrlBlockedError, resolve_public_addresses

logger = logging.getLogger(__name__)
TIMEOUT = float(os.environ.get("EGRESS_GUARD_TIMEOUT", "30"))


def _receive(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise OSError("Upstream proxy closed connection")
        data.extend(chunk)
    return bytes(data)


def _authority(address: str, port: int) -> str:
    return f"[{address}]:{port}" if ":" in address else f"{address}:{port}"


def _upstream_tunnel(address: str, port: int, proxy: str) -> socket.socket:
    if not proxy:
        return socket.create_connection((address, port), timeout=TIMEOUT)
    parsed = urlsplit(proxy)
    if parsed.scheme not in ("http", "https", "socks5", "socks5h") or not parsed.hostname:
        raise ValueError("Unsupported egress proxy scheme")
    sock = socket.create_connection((parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 1080
                                                                    if parsed.scheme.startswith("socks") else 80)),
                                    timeout=TIMEOUT)
    try:
        if parsed.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
        if parsed.scheme.startswith("socks"):
            username = unquote(parsed.username or "").encode()
            password = unquote(parsed.password or "").encode()
            sock.sendall(b"\x05\x02\x00\x02" if username else b"\x05\x01\x00")
            reply = _receive(sock, 2)
            if reply == b"\x05\x02" and username:
                if len(username) > 255 or len(password) > 255:
                    raise ValueError("Proxy credentials too long")
                sock.sendall(b"\x01" + bytes([len(username)]) + username + bytes([len(password)]) + password)
                if _receive(sock, 2) != b"\x01\x00":
                    raise OSError("SOCKS proxy authentication failed")
            elif reply != b"\x05\x00":
                raise OSError("SOCKS proxy rejected authentication")
            ip = ipaddress.ip_address(address)
            sock.sendall(b"\x05\x01\x00" + (b"\x01" if ip.version == 4 else b"\x04") + ip.packed
                         + port.to_bytes(2, "big"))
            reply = _receive(sock, 4)
            if reply[:2] != b"\x05\x00":
                raise OSError("SOCKS proxy rejected connection")
            size = {1: 4, 4: 16}.get(reply[3])
            if reply[3] == 3:
                size = _receive(sock, 1)[0]
            if size is None:
                raise OSError("Invalid SOCKS proxy response")
            _receive(sock, size + 2)
        else:
            authority = _authority(address, port)
            auth = ""
            if parsed.username:
                credentials = f"{unquote(parsed.username)}:{unquote(parsed.password or '')}".encode()
                auth = f"Proxy-Authorization: Basic {base64.b64encode(credentials).decode()}\r\n"
            sock.sendall(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n{auth}\r\n".encode())
            header = bytearray()
            while not header.endswith(b"\r\n\r\n") and len(header) < 16384:
                header.extend(_receive(sock, 1))
            status = header.split(b" ", 2)
            if not header.endswith(b"\r\n\r\n") or len(status) < 2 or status[1] != b"200":
                raise OSError("Upstream HTTP proxy rejected CONNECT")
        return sock
    except BaseException:
        sock.close()
        raise


def open_public_connection(host: str, port: int, proxy: str = "") -> socket.socket:
    addresses = resolve_public_addresses(host, port)
    last: OSError | None = None
    for address in addresses:
        try:
            return _upstream_tunnel(address, port, proxy)
        except OSError as exc:
            last = exc
    raise last or OSError("No public destination available")


def _relay(left: socket.socket, right: socket.socket) -> None:
    left.settimeout(TIMEOUT)
    right.settimeout(TIMEOUT)
    while True:
        ready, _, _ = select.select([left, right], [], [], TIMEOUT)
        if not ready:
            return
        for source in ready:
            data = source.recv(65536)
            if not data:
                return
            (right if source is left else left).sendall(data)


class GuardHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    rbufsize = 0  # Keep request bodies/tunnel bytes on the socket for relay.

    def log_message(self, format: str, *args) -> None:
        pass  # Never log credential-bearing request URLs.

    def do_CONNECT(self) -> None:
        self._forward(tunnel=True)

    def do_GET(self) -> None:
        self._forward(tunnel=False)

    do_POST = do_GET
    do_PUT = do_GET
    do_DELETE = do_GET
    do_HEAD = do_GET
    do_OPTIONS = do_GET
    do_PATCH = do_GET

    def _forward(self, *, tunnel: bool) -> None:
        self.close_connection = True
        remote = None
        started = False
        try:
            parsed = urlsplit("http://" + self.path if tunnel else self.path)
            if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Invalid proxy destination")
            port = parsed.port or 80
            remote = open_public_connection(parsed.hostname, port, cast(GuardServer, self.server).upstream_proxy)
            if tunnel:
                self.send_response(200, "Connection Established")
                self.end_headers()
            else:
                path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
                headers = [(k, v) for k, v in self.headers.items()
                           if k.lower() not in ("proxy-authorization", "proxy-connection", "connection", "host")]
                headers.extend([("Host", _authority(parsed.hostname, port)), ("Connection", "close")])
                request = f"{self.command} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers) + "\r\n"
                remote.sendall(request.encode("iso-8859-1"))
            started = True
            _relay(self.connection, remote)
        except (OSError, ValueError) as exc:
            if not started:
                self.send_error(403 if isinstance(exc, UrlBlockedError) else 502, "Egress destination rejected or unavailable")
        finally:
            if remote:
                remote.close()


class GuardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, upstream_proxy: str):
        self.upstream_proxy = upstream_proxy
        self._slots = threading.BoundedSemaphore(64)
        super().__init__(("0.0.0.0", port), GuardHandler)

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        request.settimeout(TIMEOUT)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def main() -> None:
    shared = os.environ.get("EGRESS_PROXY", "").strip()
    browser_proxy = os.environ.get("CAMOUFOX_PROXY_SERVER", "").strip() or shared
    if browser_proxy and os.environ.get("CAMOUFOX_PROXY_USERNAME"):
        from urllib.parse import quote
        parsed = urlsplit(browser_proxy)
        username = quote(os.environ["CAMOUFOX_PROXY_USERNAME"], safe="")
        password = quote(os.environ.get("CAMOUFOX_PROXY_PASSWORD", ""), safe="")
        browser_proxy = parsed._replace(netloc=f"{username}:{password}@{parsed.netloc}").geturl()
    browser = GuardServer(8081, browser_proxy)
    http = GuardServer(8082, os.environ.get("HTTP_FASTPATH_PROXY", "").strip() or shared)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    browser.serve_forever()


if __name__ == "__main__":
    main()
