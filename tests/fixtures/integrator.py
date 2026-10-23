"""The herd's Integrator over the real shared merger, with a scripted measurer.

A candidate is a full copy of the baseline version it targets plus changes, as a
worker's work directory is. Its content carries markers; the scripted measurer
scores a directory by the markers it finds, so a rebased or reverted baseline
measures as the sum of what it contains, with a separate table for the audit set
and optional penalties for combinations (interactions).
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from tests.fixtures.herd import make_store_blackboard
from tests.fixtures.worker import RecordingTaskQueue
from vlah.arms.config import PRESETS, ArmConfig
from vlah.core.blackboard.blackboard import StoreBlackboard
from vlah.core.eval.api import EvalRequest, EvalResponse, RemeasureRequest
from vlah.core.integrator.core_adapter import (
    CoreCalibration,
    Measurement,
    QuotaExhausted,
)
from vlah.core.integrator.opportunity_map import ScriptRunner, run_script
from vlah.core.integrator.service import HerdIntegrator
from vlah.core.types.claims import Claim, EvidenceRef, render_claim
from vlah.core.types.clock import FakeClock
from vlah.core.types.events import Event
from vlah.core.types.ids import ActorId
from vlah.core.types.params import GateSpec, Params

TOP_LEVEL = "slow"
EVAL = ActorId("eval_service")
HUMAN = ActorId("human")
WORKER_ONE = ActorId("worker-01")
WORKER_TWO = ActorId("worker-02")
SEEDS = 5
# baseline@0 scores this on every seed; a candidate's gain is added to every seed.
BASELINE_SCORE = 10.0
APPROACH = "a-01"
BASELINE_FILES = {"params.yaml": "window: 10\nmode: base\n", "notes.txt": "baseline\n"}
TICK_SECONDS = 60.0


def per_seed(gain: float, noise: tuple[float, ...] = ()) -> list[float]:
    return [
        BASELINE_SCORE + gain + (noise[seed] if seed < len(noise) else 0.0)
        for seed in range(SEEDS)
    ]


def directory_text(directory: Path) -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.name != "manifest.yaml"
    )


@dataclass(kw_only=True)
class ScriptedMeasurer:
    """Scores a directory by the markers in its files, on the daily or the audit set."""

    dev_gain_by_marker: dict[str, float] = field(default_factory=dict[str, float])
    audit_gain_by_marker: dict[str, float] = field(default_factory=dict[str, float])
    # A penalty applied when every marker of the key is present (an interaction).
    combined_gain_by_markers: dict[tuple[str, ...], float] = field(
        default_factory=dict[tuple[str, ...], float]
    )
    journal: list[str] = field(default_factory=list[str])
    submissions: list[tuple[str, int, str]] = field(
        default_factory=list[tuple[str, int, str]]
    )
    pending: dict[str, Measurement] = field(default_factory=dict[str, Measurement])
    audit_quota: int = 100
    count: int = 0

    def gain_of(self, directory: Path, dataset: str) -> float:
        return self.gain_of_text(directory_text(directory), dataset)

    def gain_of_text(self, text: str, dataset: str) -> float:
        table = (
            self.dev_gain_by_marker if dataset == "dev" else self.audit_gain_by_marker
        )
        gain = sum(gain for marker, gain in table.items() if marker in text)
        for markers, penalty in self.combined_gain_by_markers.items():
            if all(marker in text for marker in markers):
                gain += penalty
            else:
                pass
        return gain

    def submit(
        self,
        candidate_dir: Path,
        baseline: int,
        dataset: str,
        note: str,
        parents: list[str],
        exempt: bool,
        predicted: float,
    ) -> str:
        if dataset == "audit" and self.audit_quota <= 0:
            raise QuotaExhausted(None)
        else:
            if dataset == "audit":
                self.audit_quota -= 1
            else:
                pass
            self.count += 1
            ticket = f"m-{self.count:04d}"
            self.submissions.append((dataset, baseline, note))
            self.journal.append(f"{dataset} measurement of baseline@{baseline}")
            scores = per_seed(self.gain_of(candidate_dir, dataset))
            self.pending[ticket] = Measurement(
                ticket, f"content-{self.count}", scores, sum(scores) / SEEDS, True
            )
            return ticket

    def result(self, ticket: str) -> Measurement | None:
        return self.pending.get(ticket)


@dataclass(kw_only=True)
class RecordingEval:
    requests: list[RemeasureRequest] = field(default_factory=list[RemeasureRequest])

    def request(self, request: EvalRequest, caller: ActorId) -> EvalResponse:
        raise NotImplementedError("the Integrator never submits worker requests")

    def remeasure(self, request: RemeasureRequest) -> str:
        self.requests.append(request)
        return f"request-{len(self.requests):05d}"

    def tick(self) -> tuple[Event, ...]:
        return ()


@dataclass(frozen=True, kw_only=True)
class ScriptedVerdict:
    passed: bool
    output: str


@dataclass(kw_only=True)
class ScriptedGates:
    """A guard gate fails on any version whose files hold a failing marker."""

    failing_markers: set[str] = field(default_factory=set[str])
    journal: list[str] = field(default_factory=list[str])
    runs: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def __call__(self, gate: GateSpec, candidate_directory: Path) -> ScriptedVerdict:
        text = directory_text(candidate_directory)
        failed = any(marker in text for marker in self.failing_markers)
        self.runs.append((gate.name, candidate_directory.name))
        self.journal.append(f"gate {gate.name} on baseline@{candidate_directory.name}")
        return ScriptedVerdict(passed=not failed, output="guard ran")


@dataclass(kw_only=True)
class IntegratorHarness:
    integrator: HerdIntegrator
    blackboard: StoreBlackboard
    clock: FakeClock
    measurer: ScriptedMeasurer
    eval_service: RecordingEval
    task_queue: RecordingTaskQueue
    compose_queue: RecordingTaskQueue
    gates: ScriptedGates
    root: Path
    claim_count: int = 0
    claim_by_candidate: dict[str, str] = field(default_factory=dict[str, str])

    def events(self, event_type: str) -> list[Event]:
        return [
            event
            for event in self.integrator.records.events_written
            if event.type == event_type
        ]

    def files_of(self, k: int) -> dict[str, str]:
        """The files of baseline@k, to build a candidate on it."""
        directory = Path(self.integrator.core.versions[k].dir)
        return {
            path.relative_to(directory).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(directory.rglob("*"))
            if path.is_file() and path.name != "manifest.yaml"
        }

    def verify(
        self,
        submitter: ActorId,
        changes: dict[str, str],
        gain: float,
        audit_gain: float | None = None,
        noise: tuple[float, ...] = (),
        on_baseline: int = 0,
        approach: str = APPROACH,
        tags: tuple[str, ...] = (),
    ) -> str:
        """A candidate built on baseline@on_baseline with a verified top-level result.

        Each changed file's content is a marker worth gain on the daily set and
        audit_gain (gain by default) on the audit set, over the baseline it is
        built on. Its verified per-seed scores are what the scripted measurer
        would measure for its content, plus noise.
        """
        files = {**self.files_of(on_baseline), **changes}
        for content in changes.values():
            self.measurer.dev_gain_by_marker[content] = gain
            self.measurer.audit_gain_by_marker[content] = (
                gain if audit_gain is None else audit_gain
            )
        measured_gain = self.measurer.gain_of_text("\n".join(files.values()), "dev")
        self.claim_count += 1
        digest = hashlib.sha256(
            json.dumps([submitter, sorted(files.items()), on_baseline]).encode()
        )
        candidate = f"{digest.hexdigest()[:16]}{self.claim_count:04x}"
        target = f"baseline@{on_baseline}"
        manifest = {
            "kind": "patch",
            "target": target,
            "approach": approach,
            "author": submitter,
            "created_at": self.clock.now_seconds(),
        }
        receipt = self.blackboard.write(
            submitter,
            {
                f"candidates/{candidate}/manifest.yaml": yaml.safe_dump(
                    manifest
                ).encode(),
                **{
                    f"candidates/{candidate}/candidate/{path}": content.encode()
                    for path, content in files.items()
                },
            },
            "candidate",
        )
        assert receipt.status != "rejected", receipt.violations
        claim_id = f"c-eval_service-{self.claim_count:05d}"
        scores = per_seed(measured_gain, noise)
        result = {
            "candidate": candidate,
            "claim_id": claim_id,
            "submitter": submitter,
            "baseline": target,
            "levels": [
                {
                    "level": TOP_LEVEL,
                    "score": sum(scores) / SEEDS,
                    "n": SEEDS,
                    "per_seed": scores,
                    "paired": None,
                    "predicted": None,
                    "prediction_error": None,
                    "constraint_ok": True,
                    "status": f"verified:{TOP_LEVEL}",
                    "submitted_at_seconds": 0.0,
                    "is_cached": False,
                }
            ],
        }
        claim = Claim(
            id=claim_id,
            type="result",
            status="proposed",
            approach=approach,
            author=EVAL,
            tags=tags,
            evidence=(EvidenceRef(candidate=f"candidates/{candidate}/result.json"),),
            body=f"Result of {candidate[:12]}.",
        )
        receipt = self.blackboard.write(
            EVAL,
            {
                f"candidates/{candidate}/result.json": json.dumps(result).encode(),
                f"claims/eval_service/{claim_id}.md": render_claim(claim).encode(),
            },
            "result",
        )
        assert receipt.status != "rejected", receipt.violations
        self.claim_by_candidate[candidate] = claim_id
        self.integrator.on_verified(claim_id)
        return candidate

    def run(self, ticks: int = 6) -> list[Event]:
        written: list[Event] = []
        for _ in range(ticks):
            written.extend(self.integrator.tick())
            self.clock.advance(TICK_SECONDS)
        return written


def make_integrator(
    tmp_path: Path,
    arm: ArmConfig = PRESETS["S2"],
    params: Params | None = None,
    gates: tuple[GateSpec, ...] = (),
    sigma: float = 0.5,
    map_scripts: dict[str, bytes] | None = None,
    script_runner: ScriptRunner = run_script,
) -> IntegratorHarness:
    """map_scripts are written by the human first, at map/measurements/<id>/<script>."""
    clock = FakeClock()
    blackboard = make_store_blackboard(clock, name="integrator")
    blackboard.write(HUMAN, {"spec.md": b"# Spec\n", **(map_scripts or {})}, "spec")
    baseline_directory = tmp_path / "baseline0"
    baseline_directory.mkdir(parents=True)
    for name, content in BASELINE_FILES.items():
        (baseline_directory / name).write_text(content, encoding="utf-8")
    journal: list[str] = []
    measurer = ScriptedMeasurer(journal=journal)
    eval_service = RecordingEval()
    task_queue = RecordingTaskQueue()
    compose_queue = RecordingTaskQueue()
    scripted_gates = ScriptedGates(journal=journal)
    integrator = HerdIntegrator(
        blackboard=blackboard,
        eval_service=eval_service,
        task_queue=task_queue,
        compose_queue=compose_queue,
        gates=gates,
        gate_runner=scripted_gates,
        arm=arm,
        params=params if params is not None else Params(),
        clock=clock,
        top_level=TOP_LEVEL,
        work_directory=tmp_path / "integrator",
        baseline_directory=baseline_directory,
        calibration=CoreCalibration(
            per_seed=tuple(per_seed(0.0)),
            score=BASELINE_SCORE,
            audit_per_seed=tuple(per_seed(0.0)),
            sigma_dev=sigma,
            sigma_audit=sigma,
        ),
        measurer=measurer,
        script_runner=script_runner,
    )
    return IntegratorHarness(
        integrator=integrator,
        blackboard=blackboard,
        clock=clock,
        measurer=measurer,
        eval_service=eval_service,
        task_queue=task_queue,
        compose_queue=compose_queue,
        gates=scripted_gates,
        root=tmp_path,
    )
