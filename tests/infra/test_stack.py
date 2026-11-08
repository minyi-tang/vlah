"""The LLM-free herd on the stack (#129): two scripted workers in containers, the
blackboard over HTTP with per-worker tokens, Redis as the store, the arm B1 so every
worker has its own view; the smoke test's assertions hold and the views are separate."""

import subprocess
import sys
import time
from pathlib import Path
from random import Random

import pytest
import redis
import yaml

from vlah.adapters.executor.docker.client import DockerCli
from vlah.adapters.executor.docker.egress import run_label
from vlah.adapters.store.git_http.server import clone_environment
from vlah.core.blackboard.api import Blackboard
from vlah.core.problem.loader import load_problem
from vlah.core.types.claims import Claim, parse_claim
from vlah.core.types.clock import SystemClock
from vlah.core.types.events import Event, event_from_json
from vlah.core.types.ids import ActorId
from vlah.runtime.herd import build_herd
from vlah.runtime.stack import run_command, stack_down, stack_up
from vlah.runtime.stack_assembly import STACK_REDIS_URL, GitViewPublisher, StackAssembly

pytestmark = [
    pytest.mark.infra,
    pytest.mark.runtime,
    pytest.mark.docker,
    pytest.mark.timeout(1200),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP = REPO_ROOT / "examples" / "prompt_optimization" / "setup.py"
RUN_ID = "r-stack-herd"
WORKERS = (ActorId("worker-01"), ActorId("worker-02"))
HERD_TIMEOUT_SECONDS = 600.0
GIT_TIMEOUT_SECONDS = 60.0
FAKE_SCORE_JUDGE = '''\
"""A judge for the scripted worker's candidates: the scores in fake_score.json."""
import json
import sys
from pathlib import Path

score_file = Path(sys.argv[1]) / "fake_score.json"
if score_file.is_file():
    per_seed = json.loads(score_file.read_text())["per_seed"]
else:
    per_seed = [0.0] * 5
print(json.dumps({"score": sum(per_seed) / len(per_seed), "per_seed": per_seed}))
'''


def stack_problem(tmp_path: Path, image: str) -> Path:
    """The prompt-optimization example with the fake-score judge and scripted workers;
    the executor and the store are left to the stack's defaults."""
    root = tmp_path / "prompt-opt"
    subprocess.run([sys.executable, str(SETUP), str(root)], check=True)
    (root / "private" / "judge.py").write_text(FAKE_SCORE_JUDGE, encoding="utf-8")
    herd_path = root / "herd.yaml"
    config: object = yaml.safe_load(herd_path.read_text(encoding="utf-8"))
    assert isinstance(config, dict)
    config["executor"]["settings"]["image"] = image
    config["executor"]["settings"]["resources"] = {"cpus": 1, "memory": "1g"}
    config["harness"] = {"kind": "scripted", "settings": {"script": "submit_one"}}
    herd_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return root


def claims_by(blackboard: Blackboard, agent: ActorId) -> list[Claim]:
    return [
        parse_claim(blackboard.read(path, None).decode("utf-8"))
        for path in blackboard.list_paths(f"claims/{agent}/", None)
    ]


def follow_ups_done(blackboard: Blackboard) -> bool:
    return all(
        any(claim.type == "dead_end" and "stub" not in claim.tags for claim in claims)
        for claims in (claims_by(blackboard, agent) for agent in WORKERS)
    )


def merged_events(blackboard: Blackboard) -> list[Event]:
    return [
        event_from_json(blackboard.read(pointer, None).decode("utf-8"))
        for pointer in [
            blackboard.read(f"events/_seq/{sequence}", None).decode("utf-8")
            for sequence in range(1, blackboard.head_sequence() + 1)
        ]
    ]


def git_ls_remote(url: str, token: str | None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "ls-remote", url],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "GIT_TERMINAL_PROMPT": "0",
            **(clone_environment(token) if token else {}),
        },
    )


def leftovers(run_id: str) -> list[str]:
    return subprocess.run(
        ["docker", "ps", "--all", "--quiet", "--filter", f"label={run_label(run_id)}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()


@pytest.fixture(scope="module")
def stack_up_for_the_module() -> None:
    assert stack_up(run_command, sys.stdout) == 0, "vlah stack up failed"


def test_two_scripted_workers_run_the_herd_on_the_stack(
    tmp_path: Path, worker_image: str, stack_up_for_the_module: None
) -> None:
    root = stack_problem(tmp_path, worker_image)
    problem = load_problem(root)
    assembly = StackAssembly(
        docker=DockerCli(), executor_poll_seconds=0.5, waiter_seconds=1.0
    )
    stack_down(run_command, sys.stdout, run_id=RUN_ID, remove_volumes=False)
    run = build_herd(
        problem,
        "B1",
        assembly,
        RUN_ID,
        root=root / ".herd" / "runs",
        worker_count=2,
        duration_seconds=HERD_TIMEOUT_SECONDS,
        clock=SystemClock(),
        random=Random(0),
        poll_seconds=1.0,
    ).start()
    components = run.components
    blackboard = components.blackboard
    try:
        deadline = time.monotonic() + HERD_TIMEOUT_SECONDS
        while time.monotonic() < deadline and not follow_ups_done(blackboard):
            time.sleep(1.0)
        publisher = components.view_publisher
        assert isinstance(publisher, GitViewPublisher)
        access = {agent: publisher.access_by_agent[agent] for agent in WORKERS}
        assert all(value.url.startswith("http://") for value in access.values())
        own = git_ls_remote(access[WORKERS[0]].url, access[WORKERS[0]].token)
        assert own.returncode == 0, own.stderr
        neighbour = git_ls_remote(access[WORKERS[1]].url, access[WORKERS[0]].token)
        assert (
            neighbour.returncode != 0 and "403" in neighbour.stderr
        ), "a B1 view leaked"
        anonymous = git_ls_remote(access[WORKERS[0]].url, None)
        assert anonymous.returncode != 0
        run.stop()
    finally:
        assembly.close()
    assert follow_ups_done(blackboard), "both workers answered their results"
    # Reading every pointer from 1 to the head raises on a gap (D56 over the network).
    assert len(merged_events(blackboard)) == blackboard.head_sequence()
    client = redis.Redis.from_url(STACK_REDIS_URL)
    try:
        assert client.keys(f"{RUN_ID}:*"), "the run's state is in Redis"
    finally:
        client.close()
    assert leftovers(RUN_ID) == [], "no container outlives the run"
