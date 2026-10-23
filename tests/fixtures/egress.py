"""Local HTTP servers and a sandbox over a fake clock, for the egress tests."""

import http.server
import json
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from vlah.adapters.executor.sandbox.api import HerdHosts
from vlah.adapters.executor.sandbox.policy import effective_policy
from vlah.adapters.executor.sandbox.sandbox import ProcessSandbox, detect_layer_two
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.core.types.params import EgressPolicy, HostRule

WORKER = ActorId("worker-01")
CHILD_TIMEOUT_SECONDS = 60.0
# A name no resolver answers, so a denial can only come from the allowlist.
BLOCKED_HOST = "blocked.example.invalid"
PUBLIC_ADDRESS = "1.1.1.1"
# Larger than any socket buffer, so a proxy that mishandles back-pressure drops it.
BIG_PATH = "/big"
BIG_BODY = bytes(range(256)) * (8 * 1024 * 1024 // 256)


class RecordingHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        server = self.server
        assert isinstance(server, RecordingServer)
        server.paths.append(self.path)
        if self.path == BIG_PATH:
            body = BIG_BODY
        else:
            body = f"hello from {server.server_address[1]}".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *arguments: object) -> None:
        pass


class RecordingServer(http.server.ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), RecordingHandler)
        self.paths: list[str] = []

    @property
    def port(self) -> int:
        return int(self.server_address[1])


@dataclass(kw_only=True)
class LocalServer:
    server: RecordingServer
    thread: threading.Thread

    @property
    def port(self) -> int:
        return self.server.port

    @property
    def paths(self) -> list[str]:
        return self.server.paths

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def serve_locally() -> LocalServer:
    server = RecordingServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return LocalServer(server=server, thread=thread)


def local_rule(port: int) -> HostRule:
    return HostRule(host="127.0.0.1", ports=(port,), purpose="tools")


def herd_hosts(tool_port: int, slow_eval_port: int | None = None) -> HerdHosts:
    return HerdHosts(
        tool_server_host="127.0.0.1",
        tool_server_port=tool_port,
        slow_eval_host=f"127.0.0.1:{slow_eval_port}" if slow_eval_port else None,
        model_api_host="api.example.com",
    )


def policy_for(tool_port: int, slow_eval_port: int | None = None) -> EgressPolicy:
    return effective_policy(EgressPolicy(), herd_hosts(tool_port, slow_eval_port))


@dataclass(kw_only=True)
class SandboxHarness:
    sandbox: ProcessSandbox
    clock: FakeClock
    protected: Path
    children: list[subprocess.CompletedProcess[str]] = field(
        default_factory=list[subprocess.CompletedProcess[str]]
    )

    def run_child(
        self, program: str, policy: EgressPolicy, agent: ActorId = WORKER
    ) -> dict[str, object]:
        """Run a Python program under wrap(); it prints one JSON object on its last line."""
        wrapped = self.sandbox.wrap(agent, (sys.executable, "-c", program), {}, policy)
        completed = subprocess.run(
            list(wrapped.argv),
            env={"PATH": "/usr/bin:/bin", **wrapped.env},
            capture_output=True,
            text=True,
            timeout=CHILD_TIMEOUT_SECONDS,
            check=False,
        )
        self.children.append(completed)
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        assert lines, f"no output; stderr: {completed.stderr[-2000:]}"
        result: object = json.loads(lines[-1])
        assert isinstance(result, dict)
        return {str(key): value for key, value in result.items()}

    def close(self) -> None:
        self.sandbox.close()


def make_sandbox(
    tmp_path: Path, allow_unenforced: bool = False, layer_two: str | None = None
) -> SandboxHarness:
    clock = FakeClock(current_seconds=1000.0)
    protected = tmp_path / "runs"
    protected.mkdir(parents=True, exist_ok=True)
    sandbox = ProcessSandbox(
        clock=clock,
        work_directory=tmp_path / "sandbox",
        protected_directories=(protected,),
        allow_unenforced=allow_unenforced,
        layer_two=layer_two if layer_two is not None else detect_layer_two(),
    )
    return SandboxHarness(sandbox=sandbox, clock=clock, protected=protected)


# What a sandboxed child runs: every attempt reports how it ended, as JSON.
PROBE_PROGRAM = """
import json, os, socket, sys, urllib.request
report = {}
def attempt(name, action):
    try:
        report[name] = action()
    except Exception as error:
        report[name] = f"blocked: {type(error).__name__}"
def fetch(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read().decode()
def raw(host, port):
    sock = socket.create_connection((host, port), timeout=5)
    sock.close()
    return "connected"
def write(path):
    with open(path, "w") as handle:
        handle.write("x")
    return "written"
attempt("allowed_via_proxy", lambda: fetch(os.environ["ALLOWED_URL"]))
attempt("blocked_name_via_proxy", lambda: fetch(os.environ["BLOCKED_URL"]))
attempt("raw_other_local_port", lambda: raw("127.0.0.1", int(os.environ["OTHER_PORT"])))
attempt("raw_public_address", lambda: raw(os.environ["PUBLIC_ADDRESS"], 443))
attempt("write_protected", lambda: write(os.environ["PROTECTED_FILE"]))
attempt("write_elsewhere", lambda: write(os.environ["FREE_FILE"]))
print(json.dumps(report))
"""
