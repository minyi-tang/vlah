"""Layer 1: a local forward proxy that lets only the policy's hosts and ports through.

One proxy per agent, so every denied connection is that agent's. It speaks HTTP
CONNECT (what HTTPS clients use) and plain absolute-URI HTTP (what the MCP client
and the interrupt hook use for the tool server). It listens on 127.0.0.1 and,
when asked, on a Unix socket too, which the Linux namespace launcher bridges from
inside the sandbox, where the host's loopback does not exist.

A denied destination gets 403 and is recorded with the clock's time. The proxy
resolves names itself, so the policy is applied to the name the client asked for.
"""

import logging
import os
import select
import socket
import threading
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from vlah.adapters.executor.sandbox.api import DeniedConnection
from vlah.adapters.executor.sandbox.policy import is_allowed
from vlah.core.types.clock import Clock
from vlah.core.types.ids import ActorId
from vlah.core.types.params import EgressPolicy

logger = logging.getLogger(__name__)

HEADER_LIMIT_BYTES = 64 * 1024
IDLE_TIMEOUT_SECONDS = 600.0
CONNECT_TIMEOUT_SECONDS = 10.0
CHUNK_BYTES = 64 * 1024
DENIED_REPLY = (
    b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nConnection: close\r\n"
    b"Content-Length: {length}\r\n\r\n"
)
ESTABLISHED_REPLY = b"HTTP/1.1 200 Connection Established\r\n\r\n"
BAD_REQUEST_REPLY = b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n"
HOP_HEADERS = frozenset(
    {"proxy-connection", "connection", "keep-alive", "proxy-authorization"}
)


@dataclass(frozen=True, kw_only=True)
class Request:
    method: str
    host: str
    port: int
    forward_line: bytes
    headers: list[bytes]
    body_start: bytes


def parse_request(raw: bytes) -> Request | None:
    head, _, body_start = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    parts = lines[0].split(b" ")
    if len(parts) != 3:
        return None
    else:
        method, target, version = (part.decode("latin-1") for part in parts)
        headers = [line for line in lines[1:] if line]
        if method.upper() == "CONNECT":
            host, _, port_text = target.rpartition(":")
            if not host or not port_text.isdigit():
                return None
            else:
                return Request(
                    method="CONNECT",
                    host=host.strip("[]"),
                    port=int(port_text),
                    forward_line=b"",
                    headers=headers,
                    body_start=body_start,
                )
        else:
            url = urlsplit(target)
            if not url.hostname:
                return None
            else:
                path = (url.path or "/") + (f"?{url.query}" if url.query else "")
                return Request(
                    method=method,
                    host=url.hostname,
                    port=url.port or (443 if url.scheme == "https" else 80),
                    forward_line=f"{method} {path} {version}\r\n".encode("latin-1"),
                    headers=headers,
                    body_start=body_start,
                )


def pump(left: socket.socket, right: socket.socket) -> None:
    """Copy bytes both ways until either side closes or goes idle.

    The sockets stay blocking: select says which side has bytes to read, and
    sendall then waits for the other side's buffer instead of failing when it is
    full, which a large streamed body would otherwise do within one send.
    """
    sockets = [left, right]
    while True:
        readable, _, failed = select.select(sockets, [], sockets, IDLE_TIMEOUT_SECONDS)
        if failed or not readable:
            return
        else:
            for sock in readable:
                other = right if sock is left else left
                try:
                    data = sock.recv(CHUNK_BYTES)
                except OSError:
                    return
                if not data:
                    return
                else:
                    try:
                        other.sendall(data)
                    except OSError:
                        return


@dataclass(kw_only=True)
class AllowlistProxy:
    agent: ActorId
    policy: EgressPolicy
    clock: Clock
    unix_socket_path: Path | None = None
    denied_log: Path | None = None
    port: int = 0
    listeners: list[socket.socket] = field(default_factory=list[socket.socket])
    denied_connections: list[DeniedConnection] = field(
        default_factory=list[DeniedConnection]
    )
    lock: threading.Lock = field(default_factory=threading.Lock)
    stopping: threading.Event = field(default_factory=threading.Event)
    threads: list[threading.Thread] = field(default_factory=list[threading.Thread])

    def start(self) -> None:
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tcp.bind(("127.0.0.1", 0))
        tcp.listen(64)
        self.port = tcp.getsockname()[1]
        self.listeners.append(tcp)
        if self.unix_socket_path is not None:
            self.unix_socket_path.parent.mkdir(parents=True, exist_ok=True)
            if self.unix_socket_path.exists():
                self.unix_socket_path.unlink()
            else:
                pass
            unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            unix.bind(str(self.unix_socket_path))
            unix.listen(64)
            self.listeners.append(unix)
        else:
            pass
        for listener in self.listeners:
            thread = threading.Thread(
                target=self.accept_loop, args=(listener,), daemon=True
            )
            thread.start()
            self.threads.append(thread)

    def stop(self) -> None:
        self.stopping.set()
        for listener in self.listeners:
            try:
                listener.close()
            except OSError:
                pass
        if self.unix_socket_path is not None and self.unix_socket_path.exists():
            self.unix_socket_path.unlink()
        else:
            pass

    def accept_loop(self, listener: socket.socket) -> None:
        while not self.stopping.is_set():
            try:
                client, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(client,), daemon=True).start()

    def read_head(self, client: socket.socket) -> bytes | None:
        client.settimeout(CONNECT_TIMEOUT_SECONDS)
        raw = b""
        while b"\r\n\r\n" not in raw and len(raw) < HEADER_LIMIT_BYTES:
            try:
                chunk = client.recv(4096)
            except OSError:
                return None
            if not chunk:
                return None
            else:
                raw += chunk
        return raw if b"\r\n\r\n" in raw else None

    def deny(self, client: socket.socket, host: str, port: int) -> None:
        denied = DeniedConnection(
            timestamp_seconds=self.clock.now_seconds(),
            agent=self.agent,
            host=host,
            port=port,
        )
        with self.lock:
            self.denied_connections.append(denied)
        if self.denied_log is not None:
            with self.denied_log.open("a", encoding="utf-8") as log:
                log.write(f"{denied.timestamp_seconds}\t{host}\t{port}\n")
        else:
            pass
        logger.warning(f"{self.agent} tried {host}:{port}, which the allowlist denies")
        body = f"{host}:{port} is outside the egress allowlist\n".encode("utf-8")
        reply = DENIED_REPLY.replace(b"{length}", str(len(body)).encode("ascii"))
        try:
            client.sendall(reply + body)
        except OSError:
            pass

    def serve(self, client: socket.socket) -> None:
        try:
            raw = self.read_head(client)
            try:
                request = parse_request(raw) if raw is not None else None
            except ValueError:
                request = None
            if request is None:
                client.sendall(BAD_REQUEST_REPLY)
            elif not is_allowed(self.policy, request.host, request.port):
                self.deny(client, request.host, request.port)
            else:
                self.forward(client, request)
        except OSError:
            pass
        finally:
            try:
                client.close()
            except OSError:
                pass

    def forward(self, client: socket.socket, request: Request) -> None:
        try:
            upstream = socket.create_connection(
                (request.host, request.port), timeout=CONNECT_TIMEOUT_SECONDS
            )
        except OSError as error:
            logger.info(f"{request.host}:{request.port} unreachable: {error}")
            client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            return
        with upstream:
            if request.method == "CONNECT":
                client.sendall(ESTABLISHED_REPLY)
                if request.body_start:
                    upstream.sendall(request.body_start)
                else:
                    pass
            else:
                kept = [
                    line
                    for line in request.headers
                    if line.split(b":", 1)[0].strip().lower() not in HOP_HEADERS
                ]
                head = b"\r\n".join([request.forward_line.rstrip(b"\r\n"), *kept])
                upstream.sendall(
                    head + b"\r\nConnection: close\r\n\r\n" + request.body_start
                )
            client.settimeout(None)
            upstream.settimeout(None)
            pump(client, upstream)

    def denied(self) -> tuple[DeniedConnection, ...]:
        with self.lock:
            return tuple(self.denied_connections)


def proxy_environment(port: int, no_proxy: str) -> dict[str, str]:
    """What a sandboxed process needs to send its traffic through the proxy."""
    url = f"http://127.0.0.1:{port}"
    return {
        "HTTP_PROXY": url,
        "HTTPS_PROXY": url,
        "http_proxy": url,
        "https_proxy": url,
        "NO_PROXY": no_proxy,
        "no_proxy": no_proxy,
    }


def unix_socket_path_for(directory: Path, agent: ActorId) -> Path:
    """Unix socket paths are short-limited; keep them under the run's own directory."""
    return (
        directory / f"{agent}.sock"
        if len(str(directory)) < 80
        else Path(f"/tmp/vlah-{os.getpid()}-{agent}.sock")
    )
