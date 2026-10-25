"""A real eval client on the real blackboard, with scripted judges, gates and similarity."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from random import Random

import yaml

from tests.fixtures.fakes import FakeIndexer, RecordingBudget
from tests.fixtures.herd import make_store_blackboard
from vlah.adapters.memory.signal_source import FAKE_SCORE_FILE_NAME, FakeSlowSource
from vlah.arms.config import PRESETS, ArmConfig
from vlah.core.blackboard.api import Blackboard
from vlah.core.eval.api import EvalRequest, EvalRequestKind
from vlah.core.eval.jobs import BaselineScores, GateOutcome
from vlah.core.eval.service import LadderEvalService
from vlah.core.indexer.api import Hit
from vlah.core.types.candidates import CandidateManifest
from vlah.core.types.clock import FakeClock
from vlah.core.types.events import Event, event_from_json
from vlah.core.types.ids import ActorId
from vlah.core.types.params import GateSpec, Params
from vlah.core.types.sources import SourceBudget, SourceDecl
from vlah.ports.signal_source import Dataset

WORKER = ActorId("worker-07")
SEED_COUNT = 5


def source_declaration(
    source_id: str, cost: float, latency: str, n_for_ci: int = 1, per_day: int = 48
) -> SourceDecl:
    return SourceDecl.model_validate(
        {
            "id": source_id,
            "mode": "pull",
            "latency": latency,
            "budget": SourceBudget(per_day=per_day, allocated_by="human"),
            "cost_machine_hours": cost,
            "trust": 0.9,
            "readable_by": ["critic"],
            "noise": {"n_for_ci": n_for_ci},
        }
    )


@dataclass(kw_only=True)
class ScriptedSimilarity(FakeIndexer):
    candidate_hits: tuple[Hit, ...] = ()
    dead_end_hits: tuple[Hit, ...] = ()

    def similar_candidates(
        self, payload_text: str, limit: int, viewer: ActorId
    ) -> tuple[Hit, ...]:
        return self.candidate_hits

    def similar_dead_ends(
        self, payload_text: str, limit: int, viewer: ActorId
    ) -> tuple[Hit, ...]:
        return self.dead_end_hits


@dataclass(kw_only=True)
class ScriptedGates:
    failing: set[str] = field(default_factory=set[str])

    def __call__(self, gate: GateSpec, candidate_directory: Path) -> GateOutcome:
        return GateOutcome(
            passed=gate.name not in self.failing, output=f"{gate.name} ran"
        )


def zero_baseline(source_id: str, baseline: str, dataset: Dataset) -> tuple[float, ...]:
    return (0.0,) * SEED_COUNT


@dataclass(kw_only=True)
class EvalHarness:
    clock: FakeClock
    blackboard: Blackboard
    sources: tuple[FakeSlowSource, ...]
    indexer: ScriptedSimilarity
    budget: RecordingBudget
    gates: ScriptedGates
    service: LadderEvalService
    root: Path
    candidate_count: int = 0

    def candidate(
        self, gain: float, name: str | None = None, constraint_ok: bool = True
    ) -> Path:
        """A candidate directory whose fake score is gain on every seed."""
        self.candidate_count += 1
        directory = self.root / (name or f"candidate-{self.candidate_count}")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "policy.py").write_text(f"GAIN = {gain}\n", encoding="utf-8")
        (directory / FAKE_SCORE_FILE_NAME).write_text(
            json.dumps(
                {"per_seed": [gain] * SEED_COUNT, "constraint_ok": constraint_ok}
            ),
            encoding="utf-8",
        )
        manifest = CandidateManifest(
            kind="artifact",
            target="baseline@0",
            approach="a-01",
            author=WORKER,
            created_at=0.0,
        )
        (directory / "manifest.yaml").write_text(
            yaml.safe_dump(manifest.model_dump(mode="json")), encoding="utf-8"
        )
        return directory

    def request(
        self, directory: Path, **fields: str | EvalRequestKind | None
    ) -> EvalRequest:
        return EvalRequest.model_validate(
            {
                "candidate_directory": directory,
                "predictions": {source.declaration.id: 1.0 for source in self.sources},
                "rationale": "it should help",
                **fields,
            }
        )

    def run(self, seconds: float, steps: int = 4) -> list[Event]:
        """Tick the service while the fake clock moves past the judges' latency."""
        events: list[Event] = []
        for _ in range(steps):
            events.extend(self.service.tick())
            self.clock.advance(seconds / steps)
            events.extend(self.service.tick())
        return events

    def events_on_blackboard(self) -> list[Event]:
        return [
            event_from_json(
                self.blackboard.read(
                    self.blackboard.read(f"events/_seq/{sequence}", None).decode(), None
                ).decode()
            )
            for sequence in range(1, self.blackboard.head_sequence() + 1)
        ]


def make_eval_harness(
    root: Path,
    arm: ArmConfig = PRESETS["S2"],
    declarations: tuple[SourceDecl, ...] = (source_declaration("slow", 1.0, "10m"),),
    gates: tuple[GateSpec, ...] = (),
    blackboard: Blackboard | None = None,
    baseline_scores: BaselineScores = zero_baseline,
    params: Params | None = None,
) -> EvalHarness:
    """A fresh eval client; over an existing blackboard, it is one restarted."""
    clock = FakeClock()
    if blackboard is None:
        blackboard = make_store_blackboard(clock, name="eval")
    else:
        pass
    sources = tuple(
        FakeSlowSource(clock=clock, declaration=declaration)
        for declaration in declarations
    )
    indexer = ScriptedSimilarity()
    budget = RecordingBudget()
    scripted_gates = ScriptedGates()
    service = LadderEvalService(
        blackboard=blackboard,
        sources=sources,
        indexer=indexer,
        budget=budget,
        gates=gates,
        gate_runner=scripted_gates,
        baseline_scores=baseline_scores,
        arm=arm,
        params=params if params is not None else Params(),
        clock=clock,
        random=Random(0),
    )
    return EvalHarness(
        clock=clock,
        blackboard=blackboard,
        sources=sources,
        indexer=indexer,
        budget=budget,
        gates=scripted_gates,
        service=service,
        root=root,
    )
