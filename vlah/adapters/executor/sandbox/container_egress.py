"""The egress proxy as a container: one listener per agent on a run's network (D75).

The Docker executor starts one of these per run, on the run's internal network and
on the bridge, so it is the workers' only way out. The program watches a directory
the executor mounts: each <agent>.json there asks for a listener on a port with a
policy and a generation number; the program runs an allowlist proxy for it, bound
on every interface of the container, and writes <agent>.ready holding the
generation once the port listens. A file with a new generation replaces the
listener. Denied connections go to <denied directory>/<agent>-denied.tsv, as the
process sandbox writes them.

Runs inside the worker image as:
    python -m vlah.adapters.executor.sandbox.container_egress --policies DIR --denied DIR
"""

import argparse
import logging
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from vlah.adapters.executor.sandbox.proxy import AllowlistProxy
from vlah.core.types.base import FrozenModel
from vlah.core.types.clock import Clock, SystemClock
from vlah.core.types.ids import ActorId
from vlah.core.types.params import EgressPolicy

logger = logging.getLogger(__name__)

MODULE = "vlah.adapters.executor.sandbox.container_egress"
REQUEST_SUFFIX = ".json"
READY_SUFFIX = ".ready"
DENIED_SUFFIX = "-denied.tsv"
ALL_INTERFACES = "0.0.0.0"
DEFAULT_POLL_SECONDS = 0.2
STOP_JOIN_SECONDS = 5.0


class ListenerRequest(FrozenModel):
    """What the executor writes for one agent; the generation tells a change apart."""

    agent: ActorId
    port: int
    generation: int
    policy: EgressPolicy


def request_path(directory: Path, agent: ActorId) -> Path:
    return directory / f"{agent}{REQUEST_SUFFIX}"


def ready_path(directory: Path, agent: ActorId) -> Path:
    return directory / f"{agent}{READY_SUFFIX}"


def denied_log_path(directory: Path, agent: ActorId) -> Path:
    return directory / f"{agent}{DENIED_SUFFIX}"


def write_request(directory: Path, request: ListenerRequest) -> None:
    """Write the file whole, then rename it, so the program never reads a half file."""
    directory.mkdir(parents=True, exist_ok=True)
    destination = request_path(directory, request.agent)
    partial = destination.with_suffix(".partial")
    partial.write_text(request.model_dump_json(), encoding="utf-8")
    os.replace(partial, destination)


def read_ready_generation(directory: Path, agent: ActorId) -> int | None:
    path = ready_path(directory, agent)
    if not path.is_file():
        return None
    else:
        text = path.read_text(encoding="utf-8").strip()
        return int(text) if text.isdigit() else None


@dataclass(kw_only=True)
class NetworkProxy(AllowlistProxy):
    """An allowlist proxy on a fixed port of a given interface, for other containers."""

    bind_host: str = ALL_INTERFACES

    def start(self) -> None:
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        tcp.bind((self.bind_host, self.port))
        tcp.listen(64)
        self.listeners.append(tcp)
        thread = threading.Thread(target=self.accept_loop, args=(tcp,), daemon=True)
        thread.start()
        self.threads.append(thread)

    def stop(self) -> None:
        """Release the port for the next generation: on Linux a close alone leaves a
        thread blocked in accept holding it, so the listener is shut down first."""
        self.stopping.set()
        for listener in self.listeners:
            try:
                listener.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        super().stop()
        for thread in self.threads:
            thread.join(STOP_JOIN_SECONDS)


@dataclass(kw_only=True)
class EgressContainerProgram:
    """Keeps one listener per request file; reconcile is one pass over the directory."""

    policies_directory: Path
    denied_directory: Path
    clock: Clock
    bind_host: str = ALL_INTERFACES
    running: dict[ActorId, tuple[int, NetworkProxy]] = field(
        default_factory=dict[ActorId, tuple[int, NetworkProxy]]
    )

    def reconcile(self) -> None:
        self.policies_directory.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.policies_directory.glob(f"*{REQUEST_SUFFIX}")):
            try:
                request = ListenerRequest.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, ValidationError, ValueError):
                logger.exception(f"{path} is not a listener request; skipped")
                continue
            current = self.running.get(request.agent)
            if current is not None and current[0] == request.generation:
                continue
            else:
                try:
                    self.replace(request, current[1] if current is not None else None)
                except OSError:
                    # One agent's port must not take the other agents' listeners down;
                    # the executor times out on the missing ready file and says so.
                    logger.exception(f"could not open {request.agent}'s listener")

    def replace(self, request: ListenerRequest, previous: NetworkProxy | None) -> None:
        if previous is not None:
            previous.stop()
        else:
            pass
        proxy = NetworkProxy(
            agent=request.agent,
            policy=request.policy,
            clock=self.clock,
            denied_log=denied_log_path(self.denied_directory, request.agent),
            port=request.port,
            bind_host=self.bind_host,
        )
        proxy.start()
        self.running[request.agent] = (request.generation, proxy)
        ready_path(self.policies_directory, request.agent).write_text(
            f"{request.generation}\n", encoding="utf-8"
        )
        logger.info(
            f"listening for {request.agent} on {self.bind_host}:{request.port} "
            f"(generation {request.generation}, {len(request.policy.allow)} rules)"
        )

    def serve_forever(self, poll_seconds: float) -> None:
        while True:
            self.reconcile()
            time.sleep(poll_seconds)

    def stop(self) -> None:
        for _, proxy in self.running.values():
            proxy.stop()
        self.running.clear()


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=f"python -m {MODULE}",
        description="The egress allowlist proxy for a run's worker containers.",
    )
    parser.add_argument("--policies", type=Path, required=True)
    parser.add_argument("--denied", type=Path, required=True)
    parser.add_argument("--bind", default=ALL_INTERFACES)
    parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    program = EgressContainerProgram(
        policies_directory=arguments.policies,
        denied_directory=arguments.denied,
        clock=SystemClock(),
        bind_host=arguments.bind,
    )
    program.serve_forever(arguments.poll_seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
else:
    pass
