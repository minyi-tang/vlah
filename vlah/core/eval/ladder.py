"""Walking one job up the judge ladder: submit, poll, score, finish (v2.2 D54, D73, D74).

A level ends one of three ways:
- measured: the run is paired with the baseline seed by seed; the claim becomes
  verified at that level, or refuted when a constraint or post-result gate fails;
- void: the judge ran the candidate but returned no score, for example a seed that
  crashed or timed out (TB section 11.3); the claim is refuted and the ready event
  carries no score;
- judge failure: the judge could not run it at all; the claim keeps its status, the
  ready event carries no score and constraint_ok None, and the hash is released
  so the worker may submit again.

A transient judge error (a network failure, a 5xx, a status call that failed) is
retried with exponential backoff and jitter; a ValueError from the judge, such as
a refused submission, is permanent.

Quota is charged when a level is submitted and given back when the judge says it
answered from its cache; the budget's machine hours are charged when a result
arrives, and only for a run the judge made (TB section 11.3, D73).
"""

import logging
import statistics
from dataclasses import dataclass
from pathlib import Path
from random import Random

from vlah.core.budget.api import BudgetMeter
from vlah.core.eval.jobs import BaselineScores, EvalJob, GateRunner
from vlah.core.eval.pairing import is_promoted, pair_per_seed
from vlah.core.eval.quota import QuotaTracker
from vlah.core.eval.records import EvalRecords, LevelResult, ResultFile
from vlah.core.types.clock import Clock
from vlah.core.types.events import Event, RemeasureReady, ResultReady
from vlah.core.types.ids import role_of_actor
from vlah.core.types.params import GateSpec
from vlah.core.types.sources import Paired, ScoreResult
from vlah.ports.errors import JudgeBusyError
from vlah.ports.signal_source import CandidateRef

logger = logging.getLogger(__name__)

CONSTRAINT_GATE = "constraint"
VOID_GATE = "void"
MAX_JUDGE_RETRIES = 5
RETRY_BASE_SECONDS = 30.0


@dataclass(frozen=True, kw_only=True)
class LevelEnd:
    """How a level ended, for the claim status and the ready event."""

    paired: Paired | None
    failure: tuple[str, str] | None


class LadderRunner:
    """Advances jobs; only tick calls it, one job at a time."""

    def __init__(
        self,
        *,
        records: EvalRecords,
        quota: QuotaTracker,
        budget: BudgetMeter,
        gates: tuple[GateSpec, ...],
        gate_runner: GateRunner,
        baseline_scores: BaselineScores,
        clock: Clock,
        random: Random,
    ) -> None:
        self.records = records
        self.quota = quota
        self.budget = budget
        self.gates = gates
        self.gate_runner = gate_runner
        self.baseline_scores = baseline_scores
        self.clock = clock
        self.random = random

    def failed_gate(
        self, stage: str, candidate_directory: Path
    ) -> tuple[str, str] | None:
        for gate in self.gates:
            if stage not in gate.when:
                continue
            else:
                outcome = self.gate_runner(gate, candidate_directory)
                if not outcome.passed:
                    return gate.name, outcome.output
                else:
                    continue
        return None

    def advance(self, job: EvalJob) -> list[Event]:
        """One step for one job; a judge error is handled here, never raised to tick."""
        if job.retry_at_seconds > self.clock.now_seconds():
            return []
        else:
            try:
                return self.step(job)
            except JudgeBusyError as busy:
                job.retry_at_seconds = busy.retry_at_seconds
                return []
            except ValueError as error:
                logger.exception(f"the judge failed {job.request_id} for good")
                return self.judge_failure(job, f"{type(error).__name__}: {error}")
            except Exception as error:
                return self.retry_later(job, f"{type(error).__name__}: {error}")

    def step(self, job: EvalJob) -> list[Event]:
        if job.held_score is not None:
            return self.score_level(job)
        elif job.job_id is None:
            self.submit(job)
            return []
        else:
            score = job.levels[job.level_index].poll(job.job_id)
            job.error_count = 0
            if score is not None:
                job.held_score = score
                return self.score_level(job)
            else:
                return []

    def retry_later(self, job: EvalJob, reason: str) -> list[Event]:
        job.error_count += 1
        if job.error_count > MAX_JUDGE_RETRIES:
            return self.judge_failure(
                job, f"{MAX_JUDGE_RETRIES} retries failed; last: {reason}"
            )
        else:
            backoff = RETRY_BASE_SECONDS * 2 ** (job.error_count - 1)
            jitter = self.random.uniform(0.5, 1.5)
            job.retry_at_seconds = self.clock.now_seconds() + backoff * jitter
            logger.warning(
                f"judge error on {job.request_id} (attempt {job.error_count}); "
                f"retrying in {backoff * jitter:.0f} s: {reason}"
            )
            return []

    def submit(self, job: EvalJob) -> None:
        level = job.levels[job.level_index]
        if not self.quota.allows(job.kind, job.caller, level.declaration, job.dataset):
            return
        else:
            manifest = job.manifest.model_copy(
                update={"predicted_score": job.predictions.get(level.declaration.id)}
            )
            candidate = CandidateRef(
                candidate_hash=job.candidate_hash,
                payload_directory=job.candidate_directory,
                manifest=manifest,
            )
            job.job_id = level.submit(candidate, job.caller, job.baseline, job.dataset)
            job.submitted_at_seconds = self.clock.now_seconds()
            job.error_count = 0
            self.quota.record(
                job.kind,
                job.caller,
                level.declaration.id,
                job.submitted_at_seconds,
                job.dataset,
            )

    def level_end(self, job: EvalJob, score: ScoreResult) -> LevelEnd | None:
        """Pair and check the level; None while the baseline's scores are not known.

        An audit measurement (D72) is never held: when the baseline's audit scores
        are unknown its paired result is None, and the Integrator pairs the
        per-seed scores itself.
        """
        if score.value is None:
            reasons = score.extra.get("reasons") or score.extra.get("error") or "none"
            return LevelEnd(
                paired=None,
                failure=(
                    VOID_GATE,
                    f"the judge returned no score (reasons: {reasons})",
                ),
            )
        else:
            level_id = job.levels[job.level_index].declaration.id
            baseline_per_seed = self.baseline_scores(
                level_id, job.baseline, job.dataset
            )
            if baseline_per_seed is None and job.dataset == "dev":
                logger.info(
                    f"holding {job.candidate_hash}: no scores for {job.baseline} yet"
                )
                return None
            else:
                paired = (
                    None
                    if baseline_per_seed is None
                    else pair_per_seed(
                        score.per_seed, baseline_per_seed, 0 if score.is_cached else 1
                    )
                )
                if score.constraint_ok is False:
                    failure: tuple[str, str] | None = (
                        CONSTRAINT_GATE,
                        "the judge reported constraint_ok = false",
                    )
                else:
                    failure = self.failed_gate("post_result", job.candidate_directory)
                return LevelEnd(paired=paired, failure=failure)

    def score_level(self, job: EvalJob) -> list[Event]:
        """Record the level, then promote the job or finish it."""
        level = job.levels[job.level_index]
        score = job.held_score
        assert score is not None
        end = self.level_end(job, score)
        if end is None:
            return []
        else:
            assert job.submitted_at_seconds is not None
            if score.is_cached:
                self.quota.refund(
                    job.kind,
                    job.caller,
                    level.declaration.id,
                    job.submitted_at_seconds,
                    job.dataset,
                )
            else:
                self.budget.record(
                    role_of_actor(job.caller),
                    job.approach,
                    level.declaration.id,
                    level.declaration.cost_machine_hours,
                )
            events: list[Event] = []
            if end.failure is not None:
                new_status = "refuted"
                events.append(
                    self.records.write_gate_dead_end(
                        job.approach,
                        job.candidate_hash,
                        end.failure[0],
                        end.failure[1],
                    )
                )
            else:
                new_status = f"verified:{level.declaration.id}"
            events.append(
                self.records.write_event(
                    "claim_status",
                    target=job.claim_id,
                    from_status=job.status,
                    to_status=new_status,
                    source=level.declaration.id,
                )
            )
            job.status = new_status
            predicted = job.predictions.get(level.declaration.id)
            job.level_results.append(
                LevelResult(
                    level=level.declaration.id,
                    score=score.value,
                    n=score.n,
                    per_seed=score.per_seed,
                    paired=end.paired,
                    predicted=predicted,
                    prediction_error=(
                        abs(predicted - score.value)
                        if predicted is not None and score.value is not None
                        else None
                    ),
                    constraint_ok=score.constraint_ok,
                    status=new_status,
                    submitted_at_seconds=job.submitted_at_seconds,
                    is_cached=score.is_cached,
                    dataset=job.dataset,
                )
            )
            job.held_score = None
            job.job_id = None
            job.submitted_at_seconds = None
            is_last_level = job.level_index == len(job.levels) - 1
            if (
                end.failure is None
                and end.paired is not None
                and not is_last_level
                and is_promoted(level.declaration, end.paired)
            ):
                job.level_index += 1
            else:
                constraint_ok = (
                    False if end.failure is not None else score.constraint_ok
                )
                events.append(
                    self.finish(
                        job,
                        end.paired,
                        score.value,
                        level.declaration.id,
                        constraint_ok,
                    )
                )
            return events

    def judge_failure(self, job: EvalJob, reason: str) -> list[Event]:
        """The judge could not run the candidate; the worker may submit it again."""
        logger.warning(f"judge failure on {job.request_id}: {reason}")
        job.releases_hash = True
        return [
            self.finish(
                job, None, None, job.levels[job.level_index].declaration.id, None
            )
        ]

    def finish(
        self,
        job: EvalJob,
        paired: Paired | None,
        score: float | None,
        level_id: str,
        constraint_ok: bool | None,
    ) -> Event:
        job.is_finished = True
        self.records.write_result(
            ResultFile(
                candidate=job.candidate_hash,
                claim_id=job.claim_id,
                submitter=job.caller,
                baseline=job.baseline,
                levels=tuple(job.level_results),
            )
        )
        if job.kind == "remeasure":
            assert job.parent_candidate is not None
            return self.records.write_event(
                "remeasure_ready",
                target=job.candidate_hash,
                data=RemeasureReady(
                    request_id=job.request_id,
                    candidate=job.candidate_hash,
                    parent_candidate=job.parent_candidate,
                    claim_id=job.claim_id,
                    baseline=job.baseline,
                    paired=paired,
                    score=score,
                    level=level_id,
                    constraint_ok=constraint_ok,
                    per_seed=(
                        job.level_results[-1].per_seed if job.level_results else ()
                    ),
                    dataset=job.dataset,
                ),
            )
        else:
            return self.records.write_event(
                "result_ready",
                target=job.claim_id,
                data=ResultReady(
                    candidate=job.candidate_hash,
                    claim_id=job.claim_id,
                    approach=job.approach,
                    submitter=job.caller,
                    submitter_checkpoint=None,
                    baseline=job.baseline,
                    paired=paired,
                    per_item=f"candidates/{job.candidate_hash}/result.json",
                    score=score,
                    level=level_id,
                    constraint_ok=constraint_ok,
                ),
            )


def mean_or_none(values: tuple[float, ...] | None) -> float | None:
    if not values:
        return None
    else:
        return statistics.fmean(values)
