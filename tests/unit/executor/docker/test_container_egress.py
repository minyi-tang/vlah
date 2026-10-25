"""The egress proxy program: a listener per request file, replaced by generation (D75)."""

import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.fixtures.egress import BLOCKED_HOST, serve_locally
from tests.fixtures.tools import free_port
from vlah.adapters.executor.sandbox.container_egress import (
    EgressContainerProgram,
    ListenerRequest,
    denied_log_path,
    read_ready_generation,
    write_request,
)
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.core.types.params import EgressPolicy, HostRule

pytestmark = [pytest.mark.egress, pytest.mark.docker]

WORKER = ActorId("worker-01")


def fetch_through(proxy_port: int, url: str) -> str:
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{proxy_port}"})
    )
    try:
        with opener.open(url, timeout=5) as response:
            return response.read().decode()
    except urllib.error.HTTPError as error:
        return f"status {error.code}"


def test_a_request_file_opens_a_listener_and_a_new_generation_replaces_it(
    tmp_path: Path,
) -> None:
    allowed = serve_locally()
    clock = FakeClock(current_seconds=50.0)
    program = EgressContainerProgram(
        policies_directory=tmp_path / "egress",
        denied_directory=tmp_path,
        clock=clock,
        bind_host="127.0.0.1",
    )
    policy = EgressPolicy(
        allow=(HostRule(host="127.0.0.1", ports=(allowed.port,), purpose="tools"),)
    )
    first_port, second_port = free_port(), free_port()
    try:
        program.reconcile()
        assert read_ready_generation(program.policies_directory, WORKER) is None
        write_request(
            program.policies_directory,
            ListenerRequest(agent=WORKER, port=first_port, generation=1, policy=policy),
        )
        program.reconcile()
        assert read_ready_generation(program.policies_directory, WORKER) == 1
        assert fetch_through(
            first_port, f"http://127.0.0.1:{allowed.port}/ok"
        ).startswith("hello")
        assert fetch_through(first_port, f"http://{BLOCKED_HOST}/x") == "status 403"
        assert denied_log_path(tmp_path, WORKER).read_text() == (
            f"50.0\t{BLOCKED_HOST}\t80\n"
        )
        program.reconcile()
        assert len(program.running) == 1, "the same generation is left alone"

        # A session restarted on the same port: the first listener must let go of it.
        write_request(
            program.policies_directory,
            ListenerRequest(agent=WORKER, port=first_port, generation=2, policy=policy),
        )
        program.reconcile()
        assert read_ready_generation(program.policies_directory, WORKER) == 2
        assert fetch_through(
            first_port, f"http://127.0.0.1:{allowed.port}/ok"
        ).startswith("hello")

        write_request(
            program.policies_directory,
            ListenerRequest(
                agent=WORKER, port=second_port, generation=3, policy=policy
            ),
        )
        program.reconcile()
        assert read_ready_generation(program.policies_directory, WORKER) == 3
        assert fetch_through(
            second_port, f"http://127.0.0.1:{allowed.port}/ok"
        ).startswith("hello")
        with pytest.raises(urllib.error.URLError):
            fetch_through(first_port, f"http://127.0.0.1:{allowed.port}/ok")
    finally:
        program.stop()
        allowed.stop()
