"""Layer 2 on Linux: the worker runs in its own user, network and mount namespaces.

What it guarantees: the namespace has only a loopback interface, so no process in
it can reach any host, whatever it does with proxy variables. The one way out is
the proxy: this launcher runs inside the namespace, listens on 127.0.0.1:<proxy
port> there, and forwards each connection to the proxy's Unix socket on the host,
which the filesystem still shares. Protected directories are bind-mounted
read-only in the mount namespace, so the blackboard's bare repositories cannot be
written. Needs unprivileged user namespaces (unshare -r from util-linux) and the
mount binary; wrap checks both at construction.

Run as: python -m vlah.adapters.executor.sandbox.linux_namespace --proxy-socket S
        --proxy-port P [--protected DIR ...] -- argv...
The outer process execs unshare, which re-runs this module with --inside.
"""

import argparse
import fcntl
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from vlah.adapters.executor.sandbox.proxy import pump

MODULE = "vlah.adapters.executor.sandbox.linux_namespace"
SIOCSIFFLAGS = 0x8914
SIOCGIFFLAGS = 0x8913
IFF_UP = 0x1
LOOPBACK = b"lo"
# A child killed by signal N exits the launcher with 128 + N, as shells do.
SIGNAL_EXIT_BASE = 128


def unshare_path() -> str | None:
    return shutil.which("unshare")


def namespaces_available() -> bool:
    """Whether this host lets an unprivileged user make a network namespace."""
    executable = unshare_path()
    if executable is None or shutil.which("mount") is None:
        return False
    else:
        probe = subprocess.run(
            [executable, "-r", "-n", "-m", "true"], capture_output=True, check=False
        )
        return probe.returncode == 0


def bring_loopback_up() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        request = struct.pack("16sH14s", LOOPBACK, 0, b"\0" * 14)
        flags = struct.unpack("16sH14s", fcntl.ioctl(sock, SIOCGIFFLAGS, request))[1]
        fcntl.ioctl(
            sock,
            SIOCSIFFLAGS,
            struct.pack("16sH14s", LOOPBACK, flags | IFF_UP, b"\0" * 14),
        )


def protect(directories: Sequence[Path]) -> None:
    """Bind-mount each directory read-only; root in the user namespace may do this."""
    for directory in directories:
        if not directory.is_dir():
            continue
        else:
            subprocess.run(
                ["mount", "--bind", str(directory), str(directory)], check=True
            )
            subprocess.run(
                ["mount", "-o", "remount,bind,ro", str(directory), str(directory)],
                check=True,
            )


def bridge(proxy_port: int, proxy_socket: Path) -> None:
    """Inside the namespace: 127.0.0.1:proxy_port -> the host proxy's Unix socket."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", proxy_port))
    listener.listen(64)

    def serve(client: socket.socket) -> None:
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.connect(str(proxy_socket))
            pump(client, upstream)
        except OSError:
            pass
        finally:
            client.close()
            upstream.close()

    def accept_loop() -> None:
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=serve, args=(client,), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()


def outer_argv(
    proxy_socket: Path,
    proxy_port: int,
    protected: Sequence[Path],
    argv: Sequence[str],
    python_executable: str,
) -> tuple[str, ...]:
    executable = unshare_path()
    if executable is None:
        raise RuntimeError("unshare is not available")
    else:
        return (
            executable,
            "-r",
            "-n",
            "-m",
            "--",
            python_executable,
            "-m",
            MODULE,
            "--inside",
            "--proxy-socket",
            str(proxy_socket),
            "--proxy-port",
            str(proxy_port),
            *(
                argument
                for directory in protected
                for argument in ("--protected", str(directory))
            ),
            "--",
            *argv,
        )


def main(arguments: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog=MODULE)
    parser.add_argument("--inside", action="store_true")
    parser.add_argument("--proxy-socket", required=True)
    parser.add_argument("--proxy-port", type=int, required=True)
    parser.add_argument("--protected", action="append", default=[])
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    options = parser.parse_args(arguments)
    command = [part for part in options.argv if part != "--"] if options.argv else []
    if not command:
        parser.error("no command to run")
    elif not options.inside:
        os.execv(
            unshare_path() or "unshare",
            list(
                outer_argv(
                    Path(options.proxy_socket),
                    options.proxy_port,
                    [Path(item) for item in options.protected],
                    command,
                    sys.executable,
                )
            ),
        )
    else:
        # The executor signals the whole process group. The agent gets the
        # SIGTERM and its grace period; this launcher must outlive it to report
        # how it ended, so it ignores the signal itself.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        bring_loopback_up()
        protect([Path(item) for item in options.protected])
        bridge(options.proxy_port, Path(options.proxy_socket))
        completed = subprocess.run(command, check=False)
        code = completed.returncode
        return SIGNAL_EXIT_BASE - code if code < 0 else code
    return 0
