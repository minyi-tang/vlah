"""The eval client: the only component that spends judge runs (v2.2 section 9).

A request goes through these steps:
1. validate it (predictions for every level, a rationale; D37);
2. dedup by hash (D69, D73);
3. confirmation, when similar candidates or dead ends exist (D26);
4. write the candidate as the submitting worker;
5. pre-submit gates (D54);
6. write the result claim;
7. queue at the cheapest level.

tick then walks each request up the ladder. Each measured level gets a
claim_status event: "verified:<level>" means validly measured, not improved, and
a gate or constraint failure means "refuted". One result_ready (or, for the
Integrator, one remeasure_ready) is written when the ladder ends.
"""

import logging
from pathlib import Path
from random import Random
from typing import Literal

from vlah.arms.config import ArmConfig
from vlah.core.blackboard.api import Blackboard
from vlah.core.budget.api import BudgetMeter
from vlah.core.eval.api import (
    EvalRequest,
    EvalResponse,
    EvalStatus,
    RemeasureRequest,
    SimilarItem,
)
from vlah.core.eval.jobs import (
    BaselineScores,
    ConfirmToken,
    EvalJob,
    GateRunner,
    NewJob,
    load_manifest,
)
from vlah.core.eval.pairing import is_promoted, pair_per_seed
from vlah.core.eval.quota import QuotaTracker
from vlah.core.eval.records import EvalRecords, LevelResult, ResultFile
from vlah.core.indexer.api import Indexer
from vlah.core.types.candidates import candidate_hash, payload_files, payload_text
from vlah.core.types.claims import EvidenceRef
from vlah.core.types.clock import Clock
from vlah.core.types.events import DedupHit, Event, RemeasureReady, ResultReady
from vlah.core.types.ids import ActorId, role_of_actor
from vlah.core.types.params import GateSpec, Params
from vlah.core.types.sources import Paired, ScoreResult
from vlah.ports.errors import JudgeBusyError
from vlah.ports.signal_source import CandidateRef, SignalSource

logger = logging.getLogger(__name__)

INTEGRATOR = ActorId("integrator")
MAX_CANDIDATE_BYTES = 5 * 1024 * 1024
SIMILAR_LIMIT = 5
CONFIRM_TOKEN_TTL_SECONDS = 3600.0
CONSTRAINT_GATE = "constraint"


class LadderEvalService:
    def __init__(
        self,
        *,
        blackboard: Blackboard,
        sources: tuple[SignalSource, ...],
        indexer: Indexer,
        budget: BudgetMeter,
        gates: tuple[GateSpec, ...],
        gate_runner: GateRunner,
        baseline_scores: BaselineScores,
        arm: ArmConfig,
        params: Params,
        clock: Clock,
        random: Random,
    ) -> None:
        pull_sources = [
            source for source in sources if source.declaration.mode == "pull"
        ]
        if not pull_sources:
            raise ValueError("the eval client needs at least one pull judge")
        else:
            self.ladder = tuple(
                sorted(
                    pull_sources,
                    key=lambda source: (
                        source.declaration.cost_machine_hours,
                        source.declaration.latency_seconds,
                    ),
                )
            )
        self.blackboard = blackboard
        self.records = EvalRecords(blackboard=blackboard, clock=clock)
        self.quota = QuotaTracker(
            clock=clock, params=params, top_level_id=self.ladder[-1].declaration.id
        )
        self.indexer = indexer
        self.budget = budget
        self.gates = gates
        self.gate_runner = gate_runner
        self.baseline_scores = baseline_scores
        self.arm = arm
        self.params = params
        self.clock = clock
        self.random = random
        self.jobs: list[EvalJob] = []
        self.jobs_by_hash: dict[str, EvalJob] = {}
        self.confirm_tokens: dict[str, ConfirmToken] = {}
        self.request_count = 0

    def current_baseline(self) -> str:
        records = self.blackboard.list_paths("baseline/", None)
        numbers = [
            int(Path(path).stem) for path in records if Path(path).stem.isdigit()
        ]
        return f"baseline@{max(numbers, default=0)}"

    def next_request_id(self) -> str:
        self.request_count += 1
        return f"request-{self.request_count:05d}"

    def response(
        self,
        status: EvalStatus,
        baseline: str,
        *,
        claim_id: str | None = None,
        error: str | None = None,
        similar: tuple[SimilarItem, ...] = (),
        confirm_token: str | None = None,
    ) -> EvalResponse:
        return EvalResponse(
            request_id=self.next_request_id(),
            claim_id=claim_id,
            baseline=baseline,
            status=status,
            similar=similar,
            confirm_token=confirm_token,
            error=error,
        )

    def validation_error(self, request: EvalRequest) -> str | None:
        missing_levels = [
            source.declaration.id
            for source in self.ladder
            if source.declaration.id not in request.predictions
        ]
        payload_bytes = sum(
            file_path.stat().st_size
            for relative_path, file_path in payload_files(request.candidate_directory)
        )
        unknown_parents = [
            parent
            for parent in request.parents
            if not self.blackboard.list_paths(f"candidates/{parent}/", None)
        ]
        if not (request.candidate_directory / "manifest.yaml").is_file():
            return "the candidate directory has no manifest.yaml"
        elif missing_levels:
            return f"predictions are required for every level: missing {missing_levels} (D37)"
        elif not request.rationale.strip():
            return "a rationale is required (D37)"
        elif payload_bytes > MAX_CANDIDATE_BYTES:
            return f"the candidate is {payload_bytes} bytes; the limit is {MAX_CANDIDATE_BYTES}"
        elif unknown_parents:
            return f"unknown parent candidates {unknown_parents}"
        else:
            return None

    def request(self, request: EvalRequest, caller: ActorId) -> EvalResponse:
        baseline = request.baseline or self.current_baseline()
        error = self.validation_error(request)
        if error is not None:
            return self.response("invalid_request", baseline, error=error)
        else:
            hash_value = candidate_hash(request.candidate_directory, baseline)
            original = self.jobs_by_hash.get(hash_value)
            # Exact duplicates are caught before the similarity round-trip: an identical
            # payload is also maximally similar.
            if self.arm.hash_dedup and original is not None:
                self.records.write_event(
                    "dedup_hit",
                    target=hash_value,
                    data=DedupHit(
                        candidate=hash_value, original_claim=original.claim_id
                    ),
                )
                return self.response("dedup_hit", baseline, claim_id=original.claim_id)
            else:
                return self.confirm_and_queue(request, caller, baseline, hash_value)

    def similar_items(self, caller: ActorId, text: str) -> tuple[SimilarItem, ...]:
        threshold = self.params.similarity_threshold
        items = [
            SimilarItem(
                item_id=hit.item_id,
                kind="candidate",
                similarity=hit.similarity,
                summary=hit.summary,
            )
            for hit in self.indexer.similar_candidates(text, SIMILAR_LIMIT, caller)
            if hit.similarity >= threshold
        ]
        if self.arm.dead_end_presearch:
            items.extend(
                SimilarItem(
                    item_id=hit.item_id,
                    kind="dead_end",
                    similarity=hit.similarity,
                    summary=hit.summary,
                )
                for hit in self.indexer.similar_dead_ends(text, SIMILAR_LIMIT, caller)
                if hit.similarity >= threshold
            )
        else:
            pass
        return tuple(items)

    def confirm_and_queue(
        self, request: EvalRequest, caller: ActorId, baseline: str, hash_value: str
    ) -> EvalResponse:
        if request.confirm_token is not None:
            token = self.confirm_tokens.pop(request.confirm_token, None)
            if (
                token is None
                or token.caller != caller
                or token.candidate_hash != hash_value
                or token.expires_at_seconds < self.clock.now_seconds()
            ):
                return self.response(
                    "invalid_request", baseline, error="the confirm token is not valid"
                )
            else:
                return self.queue_request(request, caller, baseline, hash_value)
        elif self.arm.needs_confirmation:
            similar = self.similar_items(
                caller, payload_text(request.candidate_directory)
            )
            if similar:
                token_value = f"{self.random.getrandbits(64):016x}"
                self.confirm_tokens[token_value] = ConfirmToken(
                    caller=caller,
                    candidate_hash=hash_value,
                    expires_at_seconds=self.clock.now_seconds()
                    + CONFIRM_TOKEN_TTL_SECONDS,
                )
                return self.response(
                    "needs_confirmation",
                    baseline,
                    similar=similar,
                    confirm_token=token_value,
                )
            else:
                return self.queue_request(request, caller, baseline, hash_value)
        else:
            return self.queue_request(request, caller, baseline, hash_value)

    def failed_gate(
        self, stage: Literal["pre_submit", "post_result"], candidate_directory: Path
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

    def queue_request(
        self, request: EvalRequest, caller: ActorId, baseline: str, hash_value: str
    ) -> EvalResponse:
        manifest = load_manifest(request.candidate_directory)
        self.records.write_candidate(caller, request.candidate_directory, hash_value)
        gate_failure = self.failed_gate("pre_submit", request.candidate_directory)
        if gate_failure is not None:
            gate_name, output = gate_failure
            self.records.write_gate_dead_end(
                manifest.approach, hash_value, gate_name, output
            )
            return self.response(
                "gate_failed", baseline, error=f"gate {gate_name} failed"
            )
        else:
            job = self.create_job(
                NewJob(
                    kind="worker",
                    caller=caller,
                    candidate_directory=request.candidate_directory,
                    manifest=manifest,
                    baseline=baseline,
                    hash_value=hash_value,
                    parents=request.parents,
                    predictions=dict(request.predictions),
                    levels=self.ladder,
                    parent_candidate=None,
                    rationale=request.rationale,
                )
            )
            return EvalResponse(
                request_id=job.request_id,
                claim_id=job.claim_id,
                baseline=baseline,
                status="queued",
            )

    def create_job(self, new_job: NewJob) -> EvalJob:
        claim_id = self.records.write_claim(
            "result",
            new_job.manifest.approach,
            EvidenceRef(candidate=f"candidates/{new_job.hash_value}/result.json"),
            new_job.rationale,
        )
        self.records.write_lineage(new_job.hash_value, new_job.parents)
        job = EvalJob(
            request_id=self.next_request_id(),
            kind=new_job.kind,
            caller=new_job.caller,
            candidate_hash=new_job.hash_value,
            candidate_directory=new_job.candidate_directory,
            manifest=new_job.manifest,
            baseline=new_job.baseline,
            approach=new_job.manifest.approach,
            parent_candidate=new_job.parent_candidate,
            claim_id=claim_id,
            predictions=new_job.predictions,
            levels=new_job.levels,
        )
        self.jobs.append(job)
        self.jobs_by_hash[new_job.hash_value] = job
        return job

    def remeasure(self, request: RemeasureRequest) -> str:
        manifest = load_manifest(request.candidate_directory)
        hash_value = candidate_hash(request.candidate_directory, request.baseline)
        self.records.write_candidate(
            INTEGRATOR, request.candidate_directory, hash_value
        )
        job = self.create_job(
            NewJob(
                kind="remeasure",
                caller=INTEGRATOR,
                candidate_directory=request.candidate_directory,
                manifest=manifest,
                baseline=request.baseline,
                hash_value=hash_value,
                parents=(request.parent_candidate,),
                predictions={},
                levels=(self.ladder[-1],),
                parent_candidate=request.parent_candidate,
                rationale=f"Integrator remeasure of {request.parent_candidate} on {request.baseline}.",
            )
        )
        return job.request_id

    def submit(self, job: EvalJob) -> None:
        level = job.levels[job.level_index]
        if job.retry_at_seconds > self.clock.now_seconds() or not self.quota.allows(
            job.kind, job.caller, level.declaration
        ):
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
            try:
                job.job_id = level.submit(candidate, job.caller, job.baseline, "dev")
            except JudgeBusyError as busy:
                job.retry_at_seconds = busy.retry_at_seconds
                return
            self.quota.record(job.kind, job.caller, level.declaration)
            self.budget.record(
                role_of_actor(job.caller),
                job.approach,
                level.declaration.id,
                level.declaration.cost_machine_hours,
            )

    def tick(self) -> tuple[Event, ...]:
        events: list[Event] = []
        for job in [job for job in self.jobs if not job.is_finished]:
            if job.held_score is not None:
                events.extend(self.score_level(job))
            elif job.job_id is None:
                self.submit(job)
            else:
                score = job.levels[job.level_index].poll(job.job_id)
                if score is not None:
                    job.held_score = score
                    events.extend(self.score_level(job))
                else:
                    pass
        return tuple(events)

    def score_level(self, job: EvalJob) -> list[Event]:
        """Pair a level's score with the baseline, run post-result gates, and move on."""
        level = job.levels[job.level_index]
        score = job.held_score
        assert score is not None
        baseline_per_seed = self.baseline_scores(
            level.declaration.id, job.baseline, "dev"
        )
        if baseline_per_seed is None:
            logger.info(
                f"holding {job.candidate_hash}: no scores for {job.baseline} yet"
            )
            return []
        else:
            paired = pair_per_seed(score.per_seed, baseline_per_seed, 1)
            post_failure = self.failed_gate("post_result", job.candidate_directory)
            if score.constraint_ok is False:
                post_failure = (
                    CONSTRAINT_GATE,
                    "the judge reported constraint_ok = false",
                )
            else:
                pass
            predicted = job.predictions.get(level.declaration.id)
            if predicted is not None and score.value is not None:
                prediction_error = abs(predicted - score.value)
            else:
                prediction_error = None
            if post_failure is not None:
                new_status = "refuted"
                self.records.write_gate_dead_end(
                    job.approach, job.candidate_hash, post_failure[0], post_failure[1]
                )
            else:
                new_status = f"verified:{level.declaration.id}"
            events = [
                self.records.write_event(
                    "claim_status",
                    target=job.claim_id,
                    from_status=job.status,
                    to_status=new_status,
                    source=level.declaration.id,
                )
            ]
            job.status = new_status
            job.level_results.append(
                LevelResult(
                    level=level.declaration.id,
                    score=score.value,
                    n=score.n,
                    per_seed=score.per_seed,
                    paired=paired,
                    predicted=predicted,
                    prediction_error=prediction_error,
                    constraint_ok=score.constraint_ok,
                    status=new_status,
                )
            )
            job.held_score = None
            job.job_id = None
            is_last_level = job.level_index == len(job.levels) - 1
            if (
                post_failure is None
                and not is_last_level
                and is_promoted(level.declaration, paired)
            ):
                job.level_index += 1
            else:
                events.append(self.finish(job, paired, score, level.declaration.id))
            return events

    def finish(
        self, job: EvalJob, paired: Paired, score: ScoreResult, level_id: str
    ) -> Event:
        job.is_finished = True
        self.records.write_result(
            ResultFile(
                candidate=job.candidate_hash,
                claim_id=job.claim_id,
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
                    score=score.value or 0.0,
                    level=level_id,
                    constraint_ok=score.constraint_ok,
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
                    score=score.value or 0.0,
                    level=level_id,
                    constraint_ok=score.constraint_ok,
                ),
            )
