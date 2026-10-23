"""L0-19 (v2.2 §14.1). Owner: W1-11 (#21).

Arm fairness: the Integrator configuration is identical item by item across the four arms; whoever executes compose tasks is not in any arm's worker pool (D58). In this repo the Integrator settings live in Params, so the runtime must give every arm the same Params.
"""

from pathlib import Path

import pytest

from tests.fixtures.runtime import (
    FakeIntegrator,
    MemoryAssembly,
    WaitingRunner,
    random_for_tests,
    toy_problem,
)
from vlah.arms.config import ArmName
from vlah.core.types.clock import FakeClock
from vlah.runtime.compose import COMPOSE_AGENT
from vlah.runtime.herd import build_herd

pytestmark = [pytest.mark.l0, pytest.mark.runtime]

ARMS: tuple[ArmName, ...] = ("B0", "B1", "S1", "S2")


def test_l0_19_arm_fairness(tmp_path: Path) -> None:
    """Arm fairness: the Integrator configuration is identical item by item across the four arms; whoever executes compose tasks is not in any arm's worker pool (D58). In this repo the Integrator settings live in Params, so the runtime must give every arm the same Params."""
    problem = toy_problem(tmp_path)
    clock = FakeClock()
    integrator_settings = []
    for arm in ARMS:
        # B0 is one agent (D58); the sharing arms get a pool.
        worker_count = 1 if arm == "B0" else 3
        assembly = MemoryAssembly(clock=clock, root=tmp_path)
        run = build_herd(
            problem,
            arm,
            assembly,
            f"r-{arm}",
            root=tmp_path,
            worker_count=worker_count,
            duration_seconds=60.0,
            clock=clock,
            random=random_for_tests(),
            runner=WaitingRunner(),
        ).prepare()
        (context,) = assembly.contexts
        assert context.params is problem.params and context.arm.arm == arm
        integrator = run.components.integrator
        assert isinstance(integrator, FakeIntegrator)
        integrator_settings.append(integrator.params.model_dump())
        pool = [slot for slot in run.supervisor.slots if not slot.is_compose]
        (compose,) = [slot for slot in run.supervisor.slots if slot.is_compose]
        assert len(pool) == worker_count
        assert COMPOSE_AGENT not in [slot.agent for slot in pool]
        assert compose.agent == COMPOSE_AGENT
        assert compose.deps.task_queue is run.components.compose_queue
        assert all(slot.deps.task_queue is run.components.task_queue for slot in pool)
        assert (
            compose.deps.start_packs is not None
        ), "the compose agent gets no arm context"
    assert all(settings == integrator_settings[0] for settings in integrator_settings)
