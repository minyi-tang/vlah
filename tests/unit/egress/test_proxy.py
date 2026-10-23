"""Layer 1: the allowlist proxy lets allowed hosts through and logs the rest (D75)."""

import socket
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fixtures.egress import (
    BIG_BODY,
    BIG_PATH,
    BLOCKED_HOST,
    WORKER,
    local_rule,
    serve_locally,
)
from vlah.adapters.executor.sandbox.proxy import AllowlistProxy
from vlah.core.types.clock import FakeClock
from vlah.core.types.params import EgressPolicy

pytestmark = pytest.mark.egress


@pytest.fixture
def opener_and_proxy(
    tmp_path: Path,
) -> Iterator[tuple[urllib.request.OpenerDirector, AllowlistProxy, int]]:
    server = serve_locally()
    proxy = AllowlistProxy(
        agent=WORKER,
        policy=EgressPolicy(allow=(local_rule(server.port),)),
        clock=FakeClock(current_seconds=42.0),
        denied_log=tmp_path / "denied.tsv",
    )
    proxy.start()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{proxy.port}"})
    )
    try:
        yield opener, proxy, server.port
    finally:
        proxy.stop()
        server.stop()


def test_an_allowed_host_goes_through_the_proxy(
    opener_and_proxy: tuple[urllib.request.OpenerDirector, AllowlistProxy, int],
) -> None:
    opener, proxy, port = opener_and_proxy
    with opener.open(f"http://127.0.0.1:{port}/hook/check", timeout=10) as response:
        assert response.read() == f"hello from {port}".encode()
    assert proxy.denied() == ()


def test_a_denied_host_gets_403_and_is_logged(
    opener_and_proxy: tuple[urllib.request.OpenerDirector, AllowlistProxy, int],
    tmp_path: Path,
) -> None:
    opener, proxy, _ = opener_and_proxy
    with pytest.raises(urllib.error.HTTPError) as refused:
        opener.open(f"http://{BLOCKED_HOST}/answers.txt", timeout=10)
    assert refused.value.code == 403
    (denied,) = proxy.denied()
    assert (denied.agent, denied.host, denied.port, denied.timestamp_seconds) == (
        WORKER,
        BLOCKED_HOST,
        80,
        42.0,
    )
    assert (tmp_path / "denied.tsv").read_text() == f"42.0\t{BLOCKED_HOST}\t80\n"


def connect_through(proxy_port: int, target: str) -> tuple[bytes, socket.socket]:
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=10)
    sock.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    reply = sock.recv(4096)
    return reply, sock


def test_connect_tunnels_to_an_allowed_port_and_refuses_the_rest(
    opener_and_proxy: tuple[urllib.request.OpenerDirector, AllowlistProxy, int],
) -> None:
    _, proxy, port = opener_and_proxy
    reply, tunnel = connect_through(proxy.port, f"127.0.0.1:{port}")
    assert reply.startswith(b"HTTP/1.1 200")
    with tunnel:
        tunnel.sendall(
            b"GET /tunnelled HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n"
        )
        body = b""
        while chunk := tunnel.recv(4096):
            body += chunk
    assert body.endswith(f"hello from {port}".encode())
    refused, sock = connect_through(proxy.port, f"{BLOCKED_HOST}:443")
    sock.close()
    assert refused.startswith(b"HTTP/1.1 403")
    assert [denied.port for denied in proxy.denied()] == [443]


def test_large_bodies_stream_through_both_paths(
    opener_and_proxy: tuple[urllib.request.OpenerDirector, AllowlistProxy, int],
) -> None:
    """A streamed model reply or a big MCP response must not be cut at a buffer."""
    opener, proxy, port = opener_and_proxy
    with opener.open(f"http://127.0.0.1:{port}{BIG_PATH}", timeout=60) as response:
        assert response.read() == BIG_BODY
    reply, tunnel = connect_through(proxy.port, f"127.0.0.1:{port}")
    assert reply.startswith(b"HTTP/1.1 200")
    with tunnel:
        tunnel.sendall(
            f"GET {BIG_PATH} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode()
        )
        tunnel.settimeout(60)
        raw = b""
        while chunk := tunnel.recv(1 << 20):
            raw += chunk
    assert raw.split(b"\r\n\r\n", 1)[1] == BIG_BODY


def test_a_malformed_target_is_a_400_not_a_hang(
    opener_and_proxy: tuple[urllib.request.OpenerDirector, AllowlistProxy, int],
) -> None:
    _, proxy, _ = opener_and_proxy
    sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
    with sock:
        sock.sendall(b"GET http://[not-a-host/ HTTP/1.1\r\nHost: x\r\n\r\n")
        assert sock.recv(4096).startswith(b"HTTP/1.1 400")
