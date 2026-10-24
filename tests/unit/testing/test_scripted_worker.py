"""The scripted worker through the real tool server, eval client and blackboard (#127)."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fixtures.eval_harness import EvalHarness, make_eval_harness
from tests.fixtures.fakes import FakeLeases
from tests.fixtures.tools import (
    DiffingIndexer,
    RecordingRecorder,
    ScriptedRouter,
    serving,
)
from vlah.adapters.memory.store import MemoryObjectStore
from vlah.arms.config import PRESETS
from vlah.core.types.claims import parse_claim
from vlah.core.types.ids import ActorId
from vlah.core.types.params import Params
from vlah.testing.scripted_worker import (
    APPROACH_VARIABLE,
    MODULE,
    SOURCES_VARIABLE,
    TASK_KIND_VARIABLE,
    TASK_REFS_VARIABLE,
    TOOL_TOKEN_VARIABLE,
    TOOL_URL_VARIABLE,
    read_transcript,
)
from vlah.tools.herd_tools import HerdTools

pytestmark = pytest.mark.harness

WORKER = ActorId("worker-01")
ADVANCE_TASK = "t-00001"
FOLLOW_UP_TASK = "t-00002"
RUN_TIMEOUT_SECONDS = 60.0


def make_tools(eval_harness: EvalHarness) -> HerdTools:
    return HerdTools(
        blackboard=eval_harness.blackboard,
        objects=MemoryObjectStore(),
        indexer=DiffingIndexer(),
        eval_service=eval_harness.service,
        leases=FakeLeases(clock=eval_harness.clock),
        router=ScriptedRouter(),
        recorder=RecordingRecorder(),
        arm=PRESETS["S2"],
        params=Params(),
        clock=eval_harness.clock,
        run_id="r-scripted",
    )


def run_worker(
    tools: HerdTools,
    sandbox: Path,
    task_id: str,
    task_kind: str,
    task_refs: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """One session of the program, as a subprocess, with the environment a harness sets."""
    sandbox.mkdir(parents=True, exist_ok=True)
    with serving(tools) as server:
        token = server.register_agent(WORKER, sandbox, task_id)
        environment = {
            "PATH": os.environ.get("PATH", ""),
            TOOL_URL_VARIABLE: server.endpoint_for(WORKER).url,
            TOOL_TOKEN_VARIABLE: token,
            "VLAH_AGENT": WORKER,
            "VLAH_TASK": task_id,
            "VLAH_SANDBOX": str(sandbox),
            "VLAH_TRANSCRIPT": str(sandbox / ".vlah" / "transcript.json"),
            TASK_KIND_VARIABLE: task_kind,
            TASK_REFS_VARIABLE: ",".join(task_refs),
            APPROACH_VARIABLE: "a-01",
            SOURCES_VARIABLE: "slow",
        }
        return subprocess.run(
            [sys.executable, "-m", MODULE, "--script", "submit_one"],
            cwd=sandbox,
            env=environment,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT_SECONDS,
            check=False,
        )


def claim_files(eval_harness: EvalHarness) -> dict[str, bytes]:
    blackboard = eval_harness.blackboard
    return {
        path: blackboard.read(path, None)
        for path in blackboard.list_paths(f"claims/{WORKER}/", None)
    }


def test_submit_one_submits_a_candidate_and_records_the_follow_up(
    tmp_path: Path,
) -> None:
    eval_harness = make_eval_harness(tmp_path / "eval")
    tools = make_tools(eval_harness)
    sandbox = tmp_path / "sandbox"

    completed = run_worker(tools, sandbox, ADVANCE_TASK, "advance")
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert (sandbox / "candidates" / ADVANCE_TASK / "manifest.yaml").is_file()
    (result_claim_path,) = eval_harness.blackboard.list_paths(
        "claims/eval_service/", None
    )
    result_claim_id = parse_claim(
        eval_harness.blackboard.read(result_claim_path, None).decode()
    ).id
    claims = claim_files(eval_harness)
    (insight_path,) = claims
    insight = parse_claim(claims[insight_path].decode())
    assert insight.type == "insight"
    assert insight.evidence[0].claim == result_claim_id, "it cites the result claim"
    transcript = read_transcript(sandbox / ".vlah" / "transcript.json")
    assert transcript.tool_calls == ("eval_request", "repo_write")
    assert transcript.used_tokens > 0

    completed = run_worker(
        tools, sandbox, FOLLOW_UP_TASK, "result_followup", (result_claim_id,)
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    claims = claim_files(eval_harness)
    assert len(claims) == 2
    dead_end = parse_claim(claims[max(claims)].decode())
    assert dead_end.type == "dead_end"
    assert dead_end.evidence[0].claim == result_claim_id


def test_two_runs_with_the_same_inputs_write_the_same_claims(tmp_path: Path) -> None:
    """The H12 comparison (#132) needs the program to be deterministic: no clock,
    no randomness in anything it writes."""
    written: list[dict[str, bytes]] = []
    for name in ("first", "second"):
        eval_harness = make_eval_harness(tmp_path / name / "eval")
        completed = run_worker(
            make_tools(eval_harness),
            tmp_path / name / "sandbox",
            ADVANCE_TASK,
            "advance",
        )
        assert completed.returncode == 0, completed.stderr[-2000:]
        written.append(claim_files(eval_harness))
    assert written[0] == written[1]
    candidate_files = [
        sorted(
            (path.name, path.read_bytes())
            for path in (
                tmp_path / name / "sandbox" / "candidates" / ADVANCE_TASK
            ).iterdir()
        )
        for name in ("first", "second")
    ]
    assert candidate_files[0] == candidate_files[1]


def test_a_refused_claim_fails_the_session(tmp_path: Path) -> None:
    """A follow-up without its result claim cannot cite the candidate it tested, and
    the tool server refuses the dead end; the program must not swallow that."""
    eval_harness = make_eval_harness(tmp_path / "eval")
    completed = run_worker(
        make_tools(eval_harness),
        tmp_path / "sandbox",
        FOLLOW_UP_TASK,
        "result_followup",
        ("c-eval_service-99999",),
    )
    assert completed.returncode != 0
    assert "refused" in completed.stderr
    assert claim_files(eval_harness) == {}
    assert json.loads((tmp_path / "sandbox" / ".vlah" / "transcript.json").read_text())[
        "tool_calls"
    ] == ["repo_write"]
