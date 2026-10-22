"""L0-14 (v2.2 §14.1). Owner: W1-05 (#15).

baseline_advanced's files_changed intersects the lease's files_touched → hard interrupt; no intersection → soft interrupt (§8 rule 6).
"""

import json
from pathlib import Path

import pytest

from tests.fixtures.runtime import (
    MemoryAssembly,
    WaitingRunner,
    random_for_tests,
    toy_problem,
)
from vlah.core.router.router import KeyValueRouter
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.core.types.tasks import Task
from vlah.runtime.herd import build_herd

pytestmark = [pytest.mark.l0, pytest.mark.router]

OVERLAPPING = ActorId("worker-01")
ELSEWHERE = ActorId("worker-02")


def baseline_advanced(files_changed: list[str]) -> bytes:
    return json.dumps(
        {
            "local_id": "integrator/00001",
            "ts": 0.0,
            "actor": "integrator",
            "role": "integrator",
            "type": "baseline_advanced",
            "data": {
                "baseline": 1,
                "parent_baseline": 0,
                "candidate": "9f3a",
                "files_changed": files_changed,
                "paired": {"mean": 0.1, "se": 0.01, "n_items": 5, "n_reps": 1},
                "guard_pending": False,
            },
        }
    ).encode()


def test_l0_14_rebase_interrupt_rule(tmp_path: Path) -> None:
    """baseline_advanced's files_changed intersects the lease's files_touched → hard interrupt; no intersection → soft interrupt (§8 rule 6)."""
    clock = FakeClock()
    run = build_herd(
        toy_problem(tmp_path),
        "S2",
        MemoryAssembly(clock=clock, root=tmp_path),
        "r-1",
        root=tmp_path,
        worker_count=2,
        duration_seconds=60.0,
        clock=clock,
        random=random_for_tests(),
        runner=WaitingRunner(),
    ).prepare()
    router = run.components.router
    assert isinstance(router, KeyValueRouter)
    leases = run.components.leases
    for agent, touched in ((OVERLAPPING, ("solver.py",)), (ELSEWHERE, ("notes.md",))):
        leases.acquire(
            agent,
            Task(
                id=f"t-{agent}",
                approach="a-01",
                kind="advance",
                goal="g",
                created_by="queue",
            ),
            f"handle-{agent}",
        )
        leases.beat(agent, touched, clock.now_seconds())
    run.components.blackboard.write(
        ActorId("integrator"),
        {"events/integrator/00001.json": baseline_advanced(["solver.py", "cache.py"])},
        "baseline 1",
    )
    hard = router.check(OVERLAPPING)
    assert hard is not None and hard.rule == 6 and hard.kind == "hard"
    assert "solver.py" in hard.reason and "cache.py" not in hard.reason
    assert router.check(ELSEWHERE) is None
    (soft,) = router.soft_notes(ELSEWHERE)
    assert soft.rule == 6 and soft.kind == "soft"
    run.supervisor.finish()
