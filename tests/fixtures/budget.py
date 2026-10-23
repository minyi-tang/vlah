"""The real budget meter, stop rules and metrics over memory stores, for their tests."""

import json
from dataclasses import dataclass, field

from tests.fixtures.herd import make_store_blackboard
from tests.fixtures.runtime import FakeIntegrator
from vlah.adapters.memory.model import MemoryModel
from vlah.adapters.memory.store import MemoryKeyValueStore, MemoryVectorIndex
from vlah.core.blackboard.blackboard import StoreBlackboard
from vlah.core.budget.meter import KeyValueBudgetMeter
from vlah.core.indexer.indexer import StoreIndexer
from vlah.core.metrics.standing import HerdMetrics
from vlah.core.stop.rules import BudgetStopRules, EscalationPolicy
from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.core.types.params import Limits, Params, Target

TOP_LEVEL = "slow"
CHEAP_LEVEL = "fast"
HUMAN = ActorId("human")
EVAL = ActorId("eval_service")
INTEGRATOR = ActorId("integrator")
LIMITS = Limits(eval_machine_hours=10.0, days=1.0, runs={TOP_LEVEL: 4})


@dataclass(kw_only=True)
class BudgetHarness:
    clock: FakeClock
    store: MemoryKeyValueStore
    blackboard: StoreBlackboard
    meter: KeyValueBudgetMeter
    integrator: FakeIntegrator
    stop_rules: BudgetStopRules
    metrics: HerdMetrics
    baseline_count: int = field(default=0)

    def events_of_type(self, event_type: str) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        for path in self.blackboard.list_paths("events/ci/", None):
            data: object = json.loads(self.blackboard.read(path, None))
            if isinstance(data, dict) and data.get("type") == event_type:
                found.append({str(key): value for key, value in data.items()})
            else:
                continue
        return found

    def result(
        self,
        candidate: str,
        submitter: ActorId,
        approach: str,
        level: str,
        score: float,
        predicted: float,
    ) -> None:
        """A result.json as the eval client writes it, with its manifest."""
        manifest = (
            f"kind: patch\ntarget: baseline@0\napproach: {approach}\n"
            f"author: {submitter}\ncreated_at: 0.0\n"
        ).encode()
        self.blackboard.write(
            submitter, {f"candidates/{candidate}/manifest.yaml": manifest}, "c"
        )
        result = {
            "candidate": candidate,
            "claim_id": "c-eval_service-00001",
            "submitter": submitter,
            "baseline": "baseline@0",
            "levels": [
                {
                    "level": level,
                    "score": score,
                    "n": 5,
                    "per_seed": [score] * 5,
                    "paired": None,
                    "predicted": predicted,
                    "prediction_error": abs(predicted - score),
                    "constraint_ok": True,
                    "status": f"verified:{level}",
                    "submitted_at_seconds": 0.0,
                    "is_cached": False,
                }
            ],
        }
        receipt = self.blackboard.write(
            EVAL,
            {f"candidates/{candidate}/result.json": json.dumps(result).encode()},
            "r",
        )
        assert receipt.status != "rejected", receipt.violations

    def advance_baseline(self, candidate: str) -> None:
        self.baseline_count += 1
        self.integrator.baseline = BaselineRecord(
            k=self.baseline_count,
            parent=self.baseline_count - 1,
            candidates=(candidate,),
            added=candidate,
            created_ts=self.clock.now_seconds(),
        )


def make_budget(
    limits: Limits = LIMITS,
    target: Target | None = None,
    policy: EscalationPolicy | None = None,
) -> BudgetHarness:
    clock = FakeClock()
    store = MemoryKeyValueStore(clock=clock, namespace="budget-test")
    blackboard = make_store_blackboard(clock, name="budget")
    blackboard.write(HUMAN, {"spec.md": b"# Spec\n"}, "spec")
    meter = KeyValueBudgetMeter(store=store, limits=limits)
    integrator = FakeIntegrator(params=Params())
    stop_rules = BudgetStopRules(
        store=store,
        blackboard=blackboard,
        meter=meter,
        integrator=integrator,
        limits=limits,
        target=target,
        policy=policy or EscalationPolicy(),
    )
    indexer = StoreIndexer(
        blackboard=blackboard,
        vectors=MemoryVectorIndex(),
        model=MemoryModel(),
        params=Params(),
        clock=clock,
        top_level=TOP_LEVEL,
    )
    blackboard.subscribe(indexer.apply)
    metrics = HerdMetrics(
        blackboard=blackboard,
        indexer=indexer,
        budget=meter,
        router=None,
        top_level=TOP_LEVEL,
    )
    return BudgetHarness(
        clock=clock,
        store=store,
        blackboard=blackboard,
        meter=meter,
        integrator=integrator,
        stop_rules=stop_rules,
        metrics=metrics,
    )
