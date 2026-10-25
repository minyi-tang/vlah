"""The LLM-free herd in containers (#127, #128): two scripted workers, each in its
own container, the real tool server and git blackboard on the host, the arm S2;
the smoke test's assertions hold."""

import subprocess
import sys
import time
from pathlib import Path
from random import Random

import pytest
import yaml

from vlah.adapters.executor.docker.client import DockerCli
from vlah.adapters.executor.docker.egress import run_label
from vlah.core.blackboard.api import Blackboard
from vlah.core.problem.loader import load_problem
from vlah.core.types.claims import Claim, parse_claim
from vlah.core.types.clock import SystemClock
from vlah.core.types.events import Event, ResultReady, event_from_json
from vlah.core.types.ids import ActorId
from vlah.runtime.herd import build_herd
from vlah.runtime.local_assembly import LocalAssembly

pytestmark = [
    pytest.mark.infra,
    pytest.mark.executor,
    pytest.mark.docker,
    pytest.mark.timeout(900),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP = REPO_ROOT / "examples" / "prompt_optimization" / "setup.py"
RUN_ID = "r-docker-herd"
WORKERS = (ActorId("worker-01"), ActorId("worker-02"))
HERD_TIMEOUT_SECONDS = 600.0
# The scripted worker's candidate carries its own per-seed scores for a fake judge;
# baseline@0 has none and scores zero on every seed.
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


def containerized_problem(tmp_path: Path, image: str) -> Path:
    """The prompt-optimization example with the fake-score judge, Docker and scripted workers."""
    root = tmp_path / "prompt-opt"
    subprocess.run([sys.executable, str(SETUP), str(root)], check=True)
    (root / "private" / "judge.py").write_text(FAKE_SCORE_JUDGE, encoding="utf-8")
    herd_path = root / "herd.yaml"
    config: object = yaml.safe_load(herd_path.read_text(encoding="utf-8"))
    assert isinstance(config, dict)
    config["executor"] = {
        "kind": "docker",
        "settings": {
            "target_repository": str(root / "target"),
            "target_ref": "main",
            "image": image,
            "resources": {"cpus": 1, "memory": "1g"},
        },
    }
    config["harness"] = {"kind": "scripted", "settings": {"script": "submit_one"}}
    herd_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return root


def claims_by(blackboard: Blackboard, agent: ActorId) -> list[Claim]:
    return [
        parse_claim(blackboard.read(path, None).decode("utf-8"))
        for path in blackboard.list_paths(f"claims/{agent}/", None)
    ]


def follow_ups_done(blackboard: Blackboard) -> bool:
    """Each worker has recorded the dead end that answers its own result (D28)."""
    return all(
        any(claim.type == "dead_end" and "stub" not in claim.tags for claim in claims)
        for claims in (claims_by(blackboard, agent) for agent in WORKERS)
    )


def merged_events(blackboard: Blackboard) -> list[Event]:
    """Every event by sequence number, 1 to the head; a gap raises on the read."""
    return [
        event_from_json(blackboard.read(pointer, None).decode("utf-8"))
        for pointer in [
            blackboard.read(f"events/_seq/{sequence}", None).decode("utf-8")
            for sequence in range(1, blackboard.head_sequence() + 1)
        ]
    ]


def leftovers(run_id: str) -> list[str]:
    return subprocess.run(
        ["docker", "ps", "--all", "--quiet", "--filter", f"label={run_label(run_id)}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()


def test_two_scripted_workers_in_containers_run_the_herd(
    tmp_path: Path, worker_image: str
) -> None:
    root = containerized_problem(tmp_path, worker_image)
    problem = load_problem(root)
    assembly = LocalAssembly(
        docker=DockerCli(), executor_poll_seconds=0.5, waiter_seconds=1.0
    )
    run = build_herd(
        problem,
        "S2",
        assembly,
        RUN_ID,
        root=root / ".herd" / "runs",
        worker_count=2,
        duration_seconds=HERD_TIMEOUT_SECONDS,
        clock=SystemClock(),
        random=Random(0),
        poll_seconds=1.0,
    ).start()
    blackboard = run.components.blackboard
    try:
        deadline = time.monotonic() + HERD_TIMEOUT_SECONDS
        while time.monotonic() < deadline and not follow_ups_done(blackboard):
            time.sleep(1.0)
        run.stop()
    finally:
        assembly.close()
    assert leftovers(RUN_ID) == [], "no container outlives the run"

    events = merged_events(blackboard)
    results = [event for event in events if event.type == "result_ready"]
    assert len(results) == 2
    submitters: list[ActorId] = []
    for event in results:
        assert isinstance(event.data, ResultReady)
        assert event.data.level == "rubric"
        assert event.data.paired is not None and event.data.paired.n_items == 5
        submitters.append(event.data.submitter)
        follow_up = [
            claim
            for claim in claims_by(blackboard, event.data.submitter)
            if claim.type == "dead_end"
            and claim.evidence[0].claim == event.data.claim_id
        ]
        assert len(follow_up) == 1, "the result went back to its submitter (D28)"
    assert sorted(submitters) == sorted(WORKERS)
