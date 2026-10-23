"""A fake slow judge on the injected clock (D61; like the testbed's stub problem, TB section 11.1).

A test controls a candidate's score by putting fake_score.json in its payload:
{"per_seed": [...], "audit_per_seed": [...], "constraint_ok": true}. Without it the
candidate scores zero on every seed.

Like the testbed's slow tier (TB section 11.3), the fake answers a repeat of a
candidate from its cache: the same actor submitting the same hash to the same
dataset gets a new job id whose result is the first job's, marked is_cached, visible
when the first one is.
"""

import statistics
from dataclasses import dataclass

from pydantic import Field

from vlah.core.types.base import FrozenModel
from vlah.core.types.clock import Clock
from vlah.core.types.ids import ActorId
from vlah.core.types.sources import ScoreResult, SourceDecl
from vlah.ports.signal_source import CandidateRef, Dataset

FAKE_SCORE_FILE_NAME = "fake_score.json"
DEFAULT_SEED_COUNT = 5


class FakeScore(FrozenModel):
    per_seed: tuple[float, ...] = Field(min_length=1)
    audit_per_seed: tuple[float, ...] | None = None
    constraint_ok: bool = True


@dataclass(frozen=True, kw_only=True)
class FakeJob:
    job_id: str
    candidate_hash: str
    actor: ActorId
    baseline: str
    dataset: Dataset
    visible_at_seconds: float
    result: ScoreResult


class FakeSlowSource:
    """Scores become visible only after the declared latency has passed."""

    def __init__(self, *, clock: Clock, declaration: SourceDecl) -> None:
        self.clock = clock
        self.source_declaration = declaration
        self.jobs: dict[str, FakeJob] = {}
        self.first_job_id_by_key: dict[tuple[ActorId, str, Dataset], str] = {}

    @property
    def declaration(self) -> SourceDecl:
        return self.source_declaration

    def score_for(self, candidate: CandidateRef, dataset: Dataset) -> ScoreResult:
        score_path = candidate.payload_directory / FAKE_SCORE_FILE_NAME
        if score_path.is_file():
            fake_score = FakeScore.model_validate_json(
                score_path.read_text(encoding="utf-8")
            )
        else:
            fake_score = FakeScore(per_seed=(0.0,) * DEFAULT_SEED_COUNT)
        if dataset == "audit" and fake_score.audit_per_seed is not None:
            per_seed = fake_score.audit_per_seed
        else:
            per_seed = fake_score.per_seed
        return ScoreResult(
            value=statistics.fmean(per_seed),
            n=len(per_seed),
            per_seed=per_seed,
            constraint_ok=fake_score.constraint_ok,
        )

    def submit(
        self,
        candidate: CandidateRef,
        actor: ActorId,
        baseline: str,
        dataset: Dataset,
    ) -> str:
        job_id = f"{self.source_declaration.id}-{len(self.jobs) + 1:05d}"
        cache_key = (actor, candidate.candidate_hash, dataset)
        first_job_id = self.first_job_id_by_key.setdefault(cache_key, job_id)
        if first_job_id == job_id:
            visible_at_seconds = (
                self.clock.now_seconds() + self.source_declaration.latency_seconds
            )
            result = self.score_for(candidate, dataset)
        else:
            first_job = self.jobs[first_job_id]
            visible_at_seconds = first_job.visible_at_seconds
            result = first_job.result.model_copy(update={"is_cached": True})
        self.jobs[job_id] = FakeJob(
            job_id=job_id,
            candidate_hash=candidate.candidate_hash,
            actor=actor,
            baseline=baseline,
            dataset=dataset,
            visible_at_seconds=visible_at_seconds,
            result=result,
        )
        return job_id

    def poll(self, job_id: str) -> ScoreResult | None:
        job = self.jobs[job_id]
        if self.clock.now_seconds() >= job.visible_at_seconds:
            return job.result
        else:
            return None
