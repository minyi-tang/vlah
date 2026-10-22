"""L0-05b (v2.2 §14.1). Owner: W1-05 (#15).

When the submitter holds a lease, result_ready writes interrupt:<agent> (D28).
"""

from pathlib import Path

import pytest

from tests.fixtures.runtime import (
    MemoryAssembly,
    WaitingRunner,
    random_for_tests,
    result_ready_event,
    toy_problem,
)
from vlah.core.router.router import KeyValueRouter, interrupt_key
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.core.types.tasks import Task
from vlah.runtime.herd import build_herd

pytestmark = [pytest.mark.l0, pytest.mark.router]

SUBMITTER = ActorId("worker-01")
UNLEASED = ActorId("worker-02")


def test_l0_05b_result_interrupts_a_leased_submitter(tmp_path: Path) -> None:
    """When the submitter holds a lease, result_ready writes interrupt:<agent> (D28)."""
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
    run.components.leases.acquire(
        SUBMITTER,
        Task(id="t-1", approach="a-01", kind="advance", goal="g", created_by="queue"),
        "handle-1",
    )
    blackboard = run.components.blackboard
    eval_service = ActorId("eval_service")
    for number, submitter in ((1, SUBMITTER), (2, UNLEASED)):
        blackboard.write(
            eval_service,
            {
                f"events/eval_service/{number:05d}.json": result_ready_event(
                    submitter, number
                ).encode()
            },
            "result",
        )
    assert router.store.get(interrupt_key(SUBMITTER)) is not None
    interrupt = router.check(SUBMITTER)
    assert interrupt is not None and interrupt.rule == 1 and interrupt.kind == "hard"
    assert router.store.get(interrupt_key(UNLEASED)) is None, "no lease, no interrupt"
    run.supervisor.finish()
