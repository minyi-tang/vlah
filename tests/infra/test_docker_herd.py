"""Two scripted Docker workers complete one follow-up per result in S2.

Tasks retain submitter attribution even when a peer claims them during a lease gap.
"""

import subprocess
import sys
import time
from collections import Counter
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
from vlah.core.worker.loop import run_task
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


def completed_follow_ups(blackboard: Blackboard) -> list[Claim]:
    return [
        claim
        for agent in WORKERS
        for claim in claims_by(blackboard, agent)
        if claim.type == "dead_end" and "stub" not in claim.tags
    ]


def follow_ups_done(blackboard: Blackboard) -> bool:
    """Each distinct result has exactly one non-stub answer, by any worker (D28, D55)."""
    result_claim_ids = [
        event.data.claim_id
        for event in merged_events(blackboard)
        if event.type == "result_ready" and isinstance(event.data, ResultReady)
    ]
    return (
        len(result_claim_ids) == len(WORKERS)
        and len(set(result_claim_ids)) == len(result_claim_ids)
        and Counter(
            claim.evidence[0].claim for claim in completed_follow_ups(blackboard)
        )
        == Counter(result_claim_ids)
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


@pytest.mark.parametrize("lease_gap", [False, True], ids=["supervised", "lease-gap"])
def test_two_scripted_workers_in_containers_run_the_herd(
    tmp_path: Path, worker_image: str, lease_gap: bool
) -> None:
    """A submitter's lease gap must not make its peer's valid follow-up look missing."""
    root = containerized_problem(tmp_path, worker_image)
    problem = load_problem(root)
    assembly = LocalAssembly(
        docker=DockerCli(), executor_poll_seconds=0.5, waiter_seconds=1.0
    )
    run_id = f"{RUN_ID}-lease-gap" if lease_gap else RUN_ID
    herd = build_herd(
        problem,
        "S2",
        assembly,
        run_id,
        root=root / ".herd" / "runs",
        worker_count=2,
        duration_seconds=HERD_TIMEOUT_SECONDS,
        clock=SystemClock(),
        random=Random(0),
        poll_seconds=1.0,
    )
    run = herd.prepare() if lease_gap else herd.start()
    blackboard = run.components.blackboard
    try:
        deadline = time.monotonic() + HERD_TIMEOUT_SECONDS
        if lease_gap:
            slots = {slot.agent: slot for slot in run.supervisor.slots}
            cursors = {agent: 0 for agent in WORKERS}
            queue = run.components.task_queue
            claimant = WORKERS[1]
            for submitter in WORKERS:
                queue.maintain({"a-01": 1})
                advance = queue.claim_task(submitter)
                assert advance is not None and advance.kind == "advance"
                cursors[submitter] = run_task(
                    submitter, advance, slots[submitter].deps, cursors[submitter]
                )
                assert run.components.leases.get(submitter) is None

                ready: list[ResultReady] = []
                while time.monotonic() < deadline and not ready:
                    run.components.eval_service.tick()
                    ready = [
                        event.data
                        for event in merged_events(blackboard)
                        if event.type == "result_ready"
                        and isinstance(event.data, ResultReady)
                        and event.data.submitter == submitter
                    ]
                    if not ready:
                        time.sleep(1.0)
                    else:
                        pass
                assert len(ready) == 1
                assert not follow_ups_done(blackboard), "a follow-up is still missing"
                follow_up = queue.claim_task(claimant)
                assert follow_up is not None and follow_up.kind == "result_followup"
                assert follow_up.priority == 0
                assert follow_up.for_agent == submitter
                assert follow_up.refs == (ready[0].claim_id, ready[0].candidate)
                cursors[claimant] = run_task(
                    claimant, follow_up, slots[claimant].deps, cursors[claimant]
                )
                assert run.components.leases.get(claimant) is None
                assert follow_ups_done(blackboard) == (submitter == WORKERS[-1])
            assert [claim.author for claim in completed_follow_ups(blackboard)] == [
                claimant,
                claimant,
            ]
        else:
            while time.monotonic() < deadline and not follow_ups_done(blackboard):
                time.sleep(1.0)
    finally:
        try:
            run.stop()
        finally:
            assembly.close()
    assert leftovers(run_id) == [], "no container outlives the run"
    assert follow_ups_done(blackboard), "every result has one completed follow-up"

    events = merged_events(blackboard)
    results = [event for event in events if event.type == "result_ready"]
    assert len(results) == 2
    follow_ups = completed_follow_ups(blackboard)
    assert len(follow_ups) == len(results)
    submitters: list[ActorId] = []
    for event in results:
        assert isinstance(event.data, ResultReady)
        assert event.data.level == "rubric"
        assert event.data.paired is not None and event.data.paired.n_items == 5
        submitters.append(event.data.submitter)
        follow_up = [
            claim
            for claim in follow_ups
            if claim.evidence[0].claim == event.data.claim_id
        ]
        assert len(follow_up) == 1, "each result has exactly one evidence-linked answer"
    assert sorted(submitters) == sorted(WORKERS)
