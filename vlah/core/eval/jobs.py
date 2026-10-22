"""What a queued eval request is, and the plug-in points of the eval client (v2.2 D54, D57, D73)."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import yaml

from vlah.core.eval.quota import RequestKind
from vlah.core.eval.records import LevelResult
from vlah.core.types.candidates import CandidateManifest
from vlah.core.types.ids import ActorId
from vlah.core.types.params import GateSpec
from vlah.core.types.sources import ScoreResult
from vlah.ports.signal_source import Dataset, SignalSource


@dataclass(frozen=True, kw_only=True)
class GateOutcome:
    passed: bool
    output: str


class GateRunner(Protocol):
    def __call__(self, gate: GateSpec, candidate_directory: Path) -> GateOutcome:
        """Run one gate against a candidate; real gates run inside the sandbox (W1-12)."""
        ...


class BaselineScores(Protocol):
    def __call__(
        self, source_id: str, baseline: str, dataset: Dataset
    ) -> tuple[float, ...] | None:
        """The baseline's per-seed scores at a level, or None if not known yet."""
        ...


@dataclass(frozen=True, kw_only=True)
class ConfirmToken:
    caller: ActorId
    candidate_hash: str
    expires_at_seconds: float


@dataclass(kw_only=True)
class EvalJob:
    request_id: str
    kind: RequestKind
    caller: ActorId
    candidate_hash: str
    candidate_directory: Path
    manifest: CandidateManifest
    baseline: str
    approach: str
    parent_candidate: str | None
    claim_id: str
    predictions: dict[str, float]
    levels: tuple[SignalSource, ...]
    level_index: int = 0
    job_id: str | None = None
    retry_at_seconds: float = 0.0
    held_score: ScoreResult | None = None
    status: str = "proposed"
    level_results: list[LevelResult] = field(default_factory=list[LevelResult])
    is_finished: bool = False


@dataclass(frozen=True, kw_only=True)
class NewJob:
    """Everything needed to queue one request, worker or remeasure."""

    kind: RequestKind
    caller: ActorId
    candidate_directory: Path
    manifest: CandidateManifest
    baseline: str
    hash_value: str
    parents: tuple[str, ...]
    predictions: dict[str, float]
    levels: tuple[SignalSource, ...]
    parent_candidate: str | None
    rationale: str


def load_manifest(candidate_directory: Path) -> CandidateManifest:
    """The candidate's own manifest.yaml."""
    manifest_data: object = yaml.safe_load(
        (candidate_directory / "manifest.yaml").read_text(encoding="utf-8")
    )
    return CandidateManifest.model_validate(manifest_data)
