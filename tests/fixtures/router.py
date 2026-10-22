"""The real router over the in-memory key-value store, with leases and events to route."""

from dataclasses import dataclass, field

from tests.fixtures.fakes import FakeIndexer, FakeLeases
from vlah.adapters.memory.store import MemoryKeyValueStore
from vlah.arms.config import ArmConfig
from vlah.core.blackboard.api import MergedCommit
from vlah.core.indexer.api import ClaimState
from vlah.core.router.router import KeyValueRouter
from vlah.core.router.rules import Rule
from vlah.core.types.claims import Claim, EvidenceRef
from vlah.core.types.clock import FakeClock
from vlah.core.types.events import Event
from vlah.core.types.ids import ActorId
from vlah.core.types.params import Params
from vlah.core.types.tasks import Task

WORKER = ActorId("worker-01")
OTHER = ActorId("worker-02")
APPROACH = "a-01"
PAIRED = {"mean": 0.1, "se": 0.01, "n_items": 5, "n_reps": 1}


@dataclass(kw_only=True)
class ScriptedStates(FakeIndexer):
    """claim_state reports the cascaded refutations the test lists (D34)."""

    cascaded_refuted: set[str] = field(default_factory=set[str])

    def claim_state(self, claim_id: str) -> ClaimState:
        return ClaimState(
            claim_id=claim_id,
            status="refuted" if claim_id in self.cascaded_refuted else "proposed",
            history=(),
            is_cascaded_refuted=claim_id in self.cascaded_refuted,
        )


def event(
    seq: int,
    type: str,
    actor: str = "eval_service",
    role: str = "eval_service",
    **fields: object,
) -> Event:
    return Event.model_validate(
        {
            "seq": seq,
            "local_id": f"{actor}/{seq:05d}",
            "ts": 0.0,
            "actor": actor,
            "role": role,
            "type": type,
            **fields,
        }
    )


def result_ready(seq: int, submitter: ActorId) -> Event:
    return event(
        seq,
        "result_ready",
        data={
            "candidate": "9f3a",
            "claim_id": "c-eval_service-00001",
            "approach": APPROACH,
            "submitter": submitter,
            "submitter_checkpoint": None,
            "baseline": "baseline@0",
            "paired": PAIRED,
            "per_item": "",
            "score": 1.0,
            "level": "slow",
        },
    )


def claim_status(
    seq: int, target: str, to: str, actor: str = "critic-1", role: str = "critic"
) -> Event:
    return event(
        seq,
        "claim_status",
        actor=actor,
        role=role,
        target=target,
        to=to,
        reason="checked",
        evidence=["spec.md"],
    )


def approach_status(seq: int, approach: str, to: str) -> Event:
    return event(
        seq,
        "approach_status",
        actor="planner",
        role="planner",
        target=approach,
        to=to,
        reason="decision d-1",
    )


def baseline_advanced(seq: int, files_changed: tuple[str, ...]) -> Event:
    return event(
        seq,
        "baseline_advanced",
        actor="integrator",
        role="integrator",
        data={
            "baseline": 4,
            "parent_baseline": 3,
            "candidate": "9f3a",
            "files_changed": list(files_changed),
            "paired": PAIRED,
            "guard_pending": False,
        },
    )


def human_claim(approach: str) -> Claim:
    return Claim(
        id="c-human-00001",
        type="insight",
        status="proposed",
        approach=approach,
        author=ActorId("human"),
        evidence=(EvidenceRef(path="spec.md"),),
        body="Try the cache-oblivious layout.",
    )


def commit(*events: Event, claims: tuple[Claim, ...] = ()) -> MergedCommit:
    return MergedCommit(
        commit_id=f"commit-{events[0].seq if events else 'claims'}",
        actor=ActorId("eval_service"),
        paths=(),
        events=events,
        claims=claims,
    )


@dataclass(kw_only=True)
class RouterHarness:
    router: KeyValueRouter
    store: MemoryKeyValueStore
    leases: FakeLeases
    indexer: ScriptedStates
    clock: FakeClock

    def lease(
        self,
        agent: ActorId = WORKER,
        approach: str = APPROACH,
        target_claims: tuple[str, ...] = (),
        depends_on: tuple[str, ...] = (),
        files_touched: tuple[str, ...] = (),
    ) -> None:
        task = Task(
            id=f"t-{agent}",
            approach=approach,
            kind="advance",
            goal="g",
            refs=target_claims,
            created_by="queue",
        )
        self.leases.acquire(agent, task, f"handle-{agent}")
        self.leases.add_targets(agent, (), depends_on)
        self.leases.beat(agent, files_touched, self.clock.now_seconds())


def make_router(
    arm: ArmConfig, rules: tuple[Rule, ...] | None = None, params: Params | None = None
) -> RouterHarness:
    clock = FakeClock()
    store = MemoryKeyValueStore(clock=clock, namespace="router-test")
    leases = FakeLeases(clock=clock)
    indexer = ScriptedStates()
    router = KeyValueRouter(
        store=store,
        leases=leases,
        indexer=indexer,
        arm=arm,
        params=params or Params(),
        clock=clock,
        rules=rules,
    )
    return RouterHarness(
        router=router, store=store, leases=leases, indexer=indexer, clock=clock
    )
