"""L0-10 (v2.2 §14.1). Owner: W1-11 (#21).

Arm switch table: under the B1 configuration, search and diff_since return empty, and routing rules 2 to 5 do not fire; S2 has everything on (D58).
"""

import json
from pathlib import Path

import pytest

from tests.fixtures.runtime import (
    MemoryAssembly,
    SwitchedRouter,
    WaitingRunner,
    random_for_tests,
    toy_problem,
)
from vlah.arms.config import ArmName
from vlah.core.types.claims import Claim, EvidenceRef, render_claim
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.core.types.tasks import Task
from vlah.runtime.herd import build_herd
from vlah.tools.herd_tools import HerdTools

pytestmark = [pytest.mark.l0, pytest.mark.runtime]

WORKER = ActorId("worker-01")
CLAIM_ID = "c-worker-01-00001"


@pytest.mark.parametrize(("arm", "is_on"), [("B1", False), ("S2", True)])
def test_l0_10_arm_switches(tmp_path: Path, arm: ArmName, is_on: bool) -> None:
    """Arm switch table: under the B1 configuration, search and diff_since return empty, and routing rules 2 to 5 do not fire; S2 has everything on (D58)."""
    clock = FakeClock()
    run = build_herd(
        toy_problem(tmp_path),
        arm,
        MemoryAssembly(clock=clock, root=tmp_path),
        f"r-{arm}",
        root=tmp_path,
        worker_count=1,
        duration_seconds=60.0,
        clock=clock,
        random=random_for_tests(),
        runner=WaitingRunner(),
    ).prepare()
    components = run.components
    tools = components.registry
    router = components.router
    assert isinstance(tools, HerdTools) and isinstance(router, SwitchedRouter)
    assert tools.arm is run.context.arm and router.arm is run.context.arm

    tools.register_agent(WORKER, tmp_path)
    claim = Claim(
        id=CLAIM_ID,
        type="insight",
        status="proposed",
        approach="a-01",
        author=WORKER,
        evidence=(EvidenceRef(path="spec.md"),),
        body="caching halves the misses",
    )
    tools.repo_write(WORKER, {f"claims/{WORKER}/{CLAIM_ID}.md": render_claim(claim)})
    hits = tools.search(WORKER, "caching", 5).result["hits"]
    assert bool(hits) is is_on, "search"
    (tmp_path / "candidate").mkdir()
    diff = tools.eval_request(
        WORKER, "candidate", {"slow": 1.0}, "why", (), None, None
    ).diff
    assert (diff is not None) is is_on, "diff_since at the eval_request sync point"

    components.leases.acquire(
        WORKER,
        Task(
            id="t-00001",
            approach="a-01",
            kind="advance",
            goal="g",
            refs=(CLAIM_ID,),
            created_by="queue",
        ),
        "handle-1",
    )
    refutation = {
        "local_id": "critic-1/00001",
        "ts": 0.0,
        "actor": "critic-1",
        "role": "critic",
        "type": "claim_status",
        "target": CLAIM_ID,
        "from": "proposed",
        "to": "refuted",
        "reason": "does not reproduce",
        "evidence": ["spec.md"],
    }
    components.blackboard.write(
        ActorId("critic-1"),
        {"events/critic-1/00001.json": json.dumps(refutation).encode()},
        "refute",
    )
    assert (router.check(WORKER) is not None) is is_on, "router rule 2"
    run.supervisor.finish()
