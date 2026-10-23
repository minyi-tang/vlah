"""A toy problem with scripted workers, for the smoke test and the L0 skeletons.

Each worker, on a regular task, writes a candidate, asks for one judge run and
records an insight. On a result follow-up it records what it saw. The fake judge
scores each worker's candidate on five seeds after a ten-minute latency.
"""

import dataclasses
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from random import Random

import yaml

from tests.fixtures.fakes import FakeBlackboard, RecordingBudget, make_fake_components
from vlah.adapters.memory.executor import (
    MemoryExecutor,
    ScriptedHarness,
    ScriptedProgram,
    ScriptedSession,
)
from vlah.adapters.memory.signal_source import FAKE_SCORE_FILE_NAME, FakeSlowSource
from vlah.adapters.memory.store import MemoryKeyValueStore, MemoryVersionedStore
from vlah.arms.config import ArmConfig
from vlah.core.blackboard.api import Blackboard
from vlah.core.blackboard.blackboard import StoreBlackboard
from vlah.core.eval.api import EvalRequest, EvalService
from vlah.core.eval.jobs import GateOutcome
from vlah.core.eval.service import LadderEvalService
from vlah.core.queue.queue import KeyValueLeases, KeyValueTaskQueue
from vlah.core.types.candidates import CandidateManifest
from vlah.core.types.claims import Claim, EvidenceRef, render_claim
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId, format_claim_id
from vlah.core.types.params import GateSpec, Params
from vlah.core.types.sources import SourceBudget, SourceDecl
from vlah.core.types.tasks import Task
from vlah.ports.harness import Workspace
from vlah.ports.signal_source import Dataset
from vlah.ports.store import VersionedStore
from vlah.runtime.wiring import SteppedHerd, build_stepped_herd

TOY_SOURCE = SourceDecl(
    id="toy_slow",
    mode="pull",
    latency="10m",
    budget=SourceBudget(per_day=48, allocated_by="human"),
    cost_machine_hours=1.0,
    trust=0.9,
    readable_by=("critic",),
)
TOY_APPROACH = "a-01"
WORK_SECONDS = 60.0


@dataclass(frozen=True, kw_only=True)
class SeenFollowUp:
    agent: ActorId
    task: Task


@dataclass(kw_only=True)
class ToyWorker:
    """The scripted program every toy worker runs; it records what it did."""

    blackboard: Blackboard
    eval_service: EvalService
    candidates_root: Path
    claim_counts: dict[ActorId, int] = field(default_factory=dict[ActorId, int])
    submitted_claim_ids: dict[ActorId, str] = field(default_factory=dict[ActorId, str])
    follow_ups: list[SeenFollowUp] = field(default_factory=list[SeenFollowUp])

    def write_claim(
        self, agent: ActorId, claim_type: str, body: str, evidence: EvidenceRef
    ) -> None:
        number = self.claim_counts.get(agent, 0) + 1
        self.claim_counts[agent] = number
        claim = Claim.model_validate(
            {
                "id": format_claim_id(agent, number),
                "type": claim_type,
                "status": "proposed",
                "approach": TOY_APPROACH,
                "author": agent,
                "evidence": [evidence.model_dump(exclude_none=True)],
                "body": body,
            }
        )
        receipt = self.blackboard.write(
            agent,
            {f"claims/{agent}/{claim.id}.md": render_claim(claim).encode("utf-8")},
            f"{claim_type} from {agent}",
        )
        if receipt.status == "rejected":
            raise RuntimeError(
                f"the toy worker's claim was rejected: {receipt.violations}"
            )
        else:
            pass

    def write_candidate(self, session: ScriptedSession, worker_number: int) -> Path:
        directory = self.candidates_root / session.agent / session.task.id
        directory.mkdir(parents=True)
        (directory / "policy.py").write_text(
            f"WORKER = {worker_number}\n", encoding="utf-8"
        )
        per_seed = [float(worker_number + seed) for seed in range(5)]
        (directory / FAKE_SCORE_FILE_NAME).write_text(
            json.dumps({"per_seed": per_seed}), encoding="utf-8"
        )
        manifest = CandidateManifest(
            kind="artifact",
            target="baseline@0",
            approach=TOY_APPROACH,
            author=session.agent,
            created_at=session.clock.now_seconds(),
        )
        (directory / "manifest.yaml").write_text(
            yaml.safe_dump(manifest.model_dump(mode="json")), encoding="utf-8"
        )
        session.files_touched.add("policy.py")
        return directory

    def __call__(self, session: ScriptedSession) -> Iterator[float]:
        worker_number = int(session.agent.rsplit("-", 1)[1])
        if session.task.kind == "advance":
            candidate_directory = self.write_candidate(session, worker_number)
            response = self.eval_service.request(
                EvalRequest(
                    candidate_directory=candidate_directory,
                    predictions={TOY_SOURCE.id: float(worker_number)},
                    rationale=f"worker {worker_number} raises every seed by {worker_number}",
                ),
                session.agent,
            )
            assert response.claim_id is not None
            self.submitted_claim_ids[session.agent] = response.claim_id
            yield WORK_SECONDS
            self.write_claim(
                session.agent,
                "insight",
                "Submitted one candidate and waited for the judge.",
                EvidenceRef(claim=response.claim_id),
            )
        elif session.task.kind == "result_followup":
            self.follow_ups.append(SeenFollowUp(agent=session.agent, task=session.task))
            self.write_claim(
                session.agent,
                "dead_end",
                "The toy candidate is done; nothing further to try.",
                EvidenceRef(claim=session.task.refs[0]),
            )
        else:
            raise ValueError(
                f"the toy worker has no plan for a {session.task.kind} task"
            )


@dataclass(frozen=True, kw_only=True)
class ToyHerd:
    herd: SteppedHerd
    source: FakeSlowSource
    worker: ToyWorker


TOY_PUSH_TOKEN = "toy-push-token"
TOY_SEED_COUNT = 5


def zero_baseline_scores(
    source_id: str, baseline: str, dataset: Dataset
) -> tuple[float, ...]:
    """The toy baseline scores zero on every seed, so a paired gain equals the score."""
    return (0.0,) * TOY_SEED_COUNT


def passing_gate(gate: GateSpec, candidate_directory: Path) -> GateOutcome:
    return GateOutcome(passed=True, output="")


class MemoryMirrorStores:
    def __call__(self, mirror_name: str) -> tuple[VersionedStore, str]:
        return (
            MemoryVersionedStore(name=mirror_name, push_token=TOY_PUSH_TOKEN),
            TOY_PUSH_TOKEN,
        )


def make_store_blackboard(clock: FakeClock, name: str = "toy") -> StoreBlackboard:
    """The real blackboard (W1-01) over the memory store, with all its write checks."""
    return StoreBlackboard(
        store=MemoryVersionedStore(name=name, push_token=TOY_PUSH_TOKEN),
        push_token=TOY_PUSH_TOKEN,
        objects=None,
        mirror_store_factory=MemoryMirrorStores(),
        clock=clock,
    )


def make_toy_herd(
    tmp_path: Path,
    arm: ArmConfig,
    worker_count: int = 2,
    use_real_components: bool = False,
) -> ToyHerd:
    """use_real_components swaps in every real component that exists so far, over
    in-memory stores: the blackboard (W1-01), the queue and leases (W1-03) and the
    eval client (W1-04)."""
    clock = FakeClock()
    source = FakeSlowSource(clock=clock, declaration=TOY_SOURCE)
    params = Params(slots_per_approach={TOY_APPROACH: worker_count})
    if use_real_components:
        blackboard: Blackboard = make_store_blackboard(clock)
        components = make_fake_components(
            clock=clock, source=source, blackboard=blackboard
        )
        key_value_store = MemoryKeyValueStore(clock=clock, namespace="toy")
        task_queue = KeyValueTaskQueue(store=key_value_store)
        components = dataclasses.replace(
            components,
            task_queue=task_queue,
            leases=KeyValueLeases(
                store=key_value_store,
                task_queue=task_queue,
                blackboard=blackboard,
                clock=clock,
                lease_ttl_seconds=params.lease_ttl_seconds,
            ),
            eval_service=LadderEvalService(
                blackboard=blackboard,
                sources=(source,),
                indexer=components.indexer,
                budget=RecordingBudget(),
                gates=(),
                gate_runner=passing_gate,
                baseline_scores=zero_baseline_scores,
                arm=arm,
                params=params,
                clock=clock,
                random=Random(0),
            ),
        )
    else:
        components = make_fake_components(
            clock=clock, source=source, blackboard=FakeBlackboard(name="toy")
        )
    worker = ToyWorker(
        blackboard=components.blackboard,
        eval_service=components.eval_service,
        candidates_root=tmp_path / "candidates",
    )
    programs: dict[str, ScriptedProgram] = {"toy": worker}
    herd = build_stepped_herd(
        arm=arm,
        params=params,
        clock=clock,
        executor=MemoryExecutor(
            clock=clock,
            programs=programs,
            heartbeat_interval_seconds=params.heartbeat_interval_seconds,
            sandbox_root=tmp_path / "sandboxes",
        ),
        harness=ScriptedHarness(default_program="toy"),
        components=components,
        worker_ids=tuple(
            ActorId(f"worker-{index:02d}") for index in range(1, worker_count + 1)
        ),
        workspace=Workspace(
            target_repository="memory://toy-target",
            target_ref="baseline@0",
            blackboard_clone_url=components.blackboard.clone_url(),
        ),
    )
    return ToyHerd(herd=herd, source=source, worker=worker)
