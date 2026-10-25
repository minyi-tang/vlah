"""Scripted subprocesses, public inputs and a live HTTP fixture for the herd-arm test."""

import socket
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI

from testbed.slow_eval.app import create_app
from testbed.slow_eval.core import ManualClock, SlowEval
from vlah.adapters.harness.egress import herd_tool_rules
from vlah.adapters.memory.executor import ScriptedHarness
from vlah.adapters.signal.testbed_http.client import SlowTierClient
from vlah.core.types.ids import ActorId
from vlah.core.types.params import EgressPolicy
from vlah.core.types.tasks import Task
from vlah.ports.executor import AgentSpec, StartPack
from vlah.ports.harness import HookConfig, ToolEndpoint, Workspace
from vlah.runtime.local_assembly import free_port
from vlah.runtime.testbed_problem import SLOW_ADAPTER

PACKED_PROBLEM = """from pathlib import Path

class PackedProblem:
    public_seeds = [1, 2, 3, 4, 5]
    def __init__(self, root):
        self.root = root
    def validate_candidate(self, directory):
        return None if (Path(directory) / "params.yaml").is_file() else "missing params"
    def evaluate(self, candidate_dir, tier, seed, timeout=True):
        raise RuntimeError("a herd worker must use the service, not the local simulator")

def load(root):
    return PackedProblem(root)
"""

WORKER_PROGRAM = """import asyncio, json, os, subprocess, sys, threading
from pathlib import Path
import httpx2
import yaml
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from vlah.core.types.claims import Claim, EvidenceRef, render_claim
from vlah.core.types.ids import ActorId

actor = os.environ["TB_ACTOR"]
assert "TB_SYSTEM_KEY" not in os.environ
candidate = Path("target/policy")
params = yaml.safe_load((candidate / "params.yaml").read_text())
if actor == "worker-01":
    params["ingredients"] = {"backfill": True}
else:
    params["bonus"] = {"fixture": 20.0}
(candidate / "params.yaml").write_text(yaml.safe_dump(params))
subprocess.run(
    [sys.executable, "-c", "from testbed.cli.main import main; main()",
     "fast", str(candidate), "--seed", "1", "--out", "fast.json"],
    check=True,
)
async def submit():
    async with httpx2.AsyncClient(
        headers={"Authorization": "Bearer " + os.environ["SCRIPT_TOOL_TOKEN"]}
    ) as http:
        async with streamable_http_client(
            os.environ["SCRIPT_TOOL_URL"], http_client=http
        ) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                read = await session.call_tool("repo_read", {"path": "spec.md"})
                assert not read.is_error
                claim = Claim(
                    id=f"c-{actor}-00001", type="insight", status="proposed",
                    approach="order", author=ActorId(actor),
                    evidence=(EvidenceRef(path="spec.md"),), body="Scripted fast check."
                )
                written = await session.call_tool("repo_write", {"files": {
                    f"claims/{actor}/{claim.id}.md": render_claim(claim)
                }})
                assert not written.is_error
                reply = await session.call_tool("eval_request", {
                    "candidate_dir": str(candidate), "predictions": {"slow": -900.0},
                    "rationale": "The fixture improves the baseline."
                })
                assert not reply.is_error, reply
                result = reply.structured_content["result"]
                if result["status"] == "needs_confirmation":
                    reply = await session.call_tool("eval_request", {
                        "candidate_dir": str(candidate), "predictions": {"slow": -900.0},
                        "rationale": "Confirm the fixture.",
                        "confirm_token": result["confirm_token"]
                    })
                    result = reply.structured_content["result"]
                assert result["status"] == "queued", result
                Path("submitted.json").write_text(json.dumps(result))
asyncio.run(submit())
Path(os.environ["VLAH_TRANSCRIPT"]).write_text(
    json.dumps({"used_tokens": 1, "window_tokens": 100000})
)
threading.Event().wait()
"""


class ReadyServer(uvicorn.Server):
    def __init__(self, app: FastAPI, port: int) -> None:
        super().__init__(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        )
        self.ready = threading.Event()

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        await super().startup(sockets)
        self.ready.set()


@dataclass(frozen=True, kw_only=True)
class RunningService:
    client: SlowTierClient
    core: SlowEval
    clock: ManualClock


@contextmanager
def running_service(root: Path) -> Iterator[RunningService]:
    clock = ManualClock()
    core = SlowEval(root, clock=clock, inline=True)
    app = create_app(root, core=core, publisher=False)
    server = ReadyServer(app, free_port())
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    client = SlowTierClient(
        base_url=f"http://127.0.0.1:{server.config.port}",
        run_id="r-scripted",
        system_key=core.system_key,
    )
    try:
        assert server.ready.wait(10)
        response = client.call(
            "POST",
            "/runs",
            {
                "run_id": client.run_id,
                "arm": "herd",
                "problem": "stub",
                "N": 2,
                "overrides": {
                    "visible_delay_s": 0,
                    "slow_seeds": 3,
                    "quota_per_hour": 100,
                },
            },
        )
        assert response.status == 200
        yield RunningService(client=client, core=core, clock=clock)
    finally:
        server.should_exit = True
        thread.join(10)
        assert not thread.is_alive()
        core.close()


def write_files(root: Path, files: dict[str, str]) -> Path:
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def make_template(root: Path) -> Path:
    return write_files(
        root,
        {
            "spec.md": "# Scripted testbed\nUse target/policy/ and problem/.\n",
            "charter/status.md": "aligned\n",
            "charter/params.yaml": "slots_per_approach: {order: 2}\n",
            "charter/constraints.md": (
                "# Fixture limits\n```yaml limits\n"
                "eval_machine_hours: 100\ndays: 1\nruns: {slow: 100}\n```\n"
            ),
            "charter/progress.md": "# Progress\n",
            "charter/sources.md": "# Sources\nslow is the scripted service.\n",
            "approaches/order.md": "---\nstatus: active\n---\nTry the fixture.\n",
            "eval/sources/slow.yaml": (
                "id: slow\nmode: pull\nlatency: 0s\n"
                "budget: {per_day: 100, allocated_by: human}\n"
                "cost_machine_hours: 0.01\ntrust: 1\nreadable_by: [critic]\n"
            ),
            "herd.yaml": yaml.safe_dump(
                {
                    "judges": {"slow": {"adapter_class": SLOW_ADAPTER}},
                    "executor": {"kind": "local", "settings": {}},
                    "harness": {
                        "kind": "copilot",
                        "settings": {"model": "scripted-fixture"},
                    },
                }
            ),
        },
    )


def make_packed(root: Path) -> Path:
    return write_files(
        root,
        {
            "__init__.py": "",
            "problem.py": PACKED_PROBLEM,
            "tools.md": "# Tools\nRun tb fast.\n",
            "baseline_scores.json": "{}\n",
            "policy/params.yaml": "ingredients: {}\n",
            "policy/policy.py": "def order(queue, state):\n    return queue\n",
        },
    )


class ScriptedCliHarness(ScriptedHarness):
    def build(
        self,
        agent: ActorId,
        task: Task,
        start_pack: StartPack,
        tool_endpoint: ToolEndpoint,
        hook_config: HookConfig,
        workspace: Workspace,
    ) -> AgentSpec:
        spec = super().build(
            agent, task, start_pack, tool_endpoint, hook_config, workspace
        )
        return spec.model_copy(
            update={
                "command": (sys.executable, "-c", WORKER_PROGRAM),
                "env": {
                    **spec.env,
                    "SCRIPT_TOOL_URL": tool_endpoint.url,
                    "SCRIPT_TOOL_TOKEN": tool_endpoint.bearer_token,
                },
                "egress": EgressPolicy(
                    allow=tuple(herd_tool_rules(tool_endpoint, hook_config))
                ),
            }
        )
