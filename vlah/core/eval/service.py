"""The eval client: the only component that spends judge runs (v2.2 section 9).

A request goes through these steps:
1. validate it (a manifest, a known baseline, no symbolic links, predictions for
   every level, a rationale; D37);
2. dedup by hash (D69, D73);
3. confirmation, when similar candidates or dead ends exist (D26);
4. write the candidate as the submitting worker;
5. pre-submit gates (D54);
6. write the result claim;
7. queue at the cheapest level.

A compose request is the Integrator's work (D74), so it skips steps 2 and 3 in
every arm and spends the Integrator's quota, like a remeasure; its result claim
is tagged compose so that a restart and the dedup table know it.

tick then walks each request up the ladder (ladder.py). Each measured level gets a
claim_status event: "verified:<level>" means validly measured, not improved, and
a gate or constraint failure means "refuted". One result_ready (or, for the
Integrator, one remeasure_ready) is written when the ladder ends.

Requests arrive on several threads while the runtime ticks:
- self.lock guards the job list, the dedup table and the confirm tokens, and is
  never held across gates, similarity search or judge calls;
- requests for the same candidate hash take that hash's lock, so a duplicate
  waits for the first to be queued and is then deduplicated;
- one tick runs at a time, outside self.lock.
"""

import threading
from random import Random

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
from vlah.core.eval.history import (
    COMPOSE_TAG,
    REMEASURE_TAG,
    measured_claims_by_hash,
    replay_recorded_runs,
    stored_level_score,
)
from vlah.core.eval.jobs import (
    BaselineScores,
    ConfirmToken,
    EvalJob,
    GateRunner,
    NewJob,
    load_manifest,
)
from vlah.core.eval.ladder import LadderRunner, mean_or_none
from vlah.core.eval.quota import QuotaTracker, RequestKind
from vlah.core.eval.records import EvalRecords
from vlah.core.eval.validation import CheckedRequest, check_candidate, request_error
from vlah.core.indexer.api import Indexer
from vlah.core.types.candidates import candidate_hash, payload_text
from vlah.core.types.claims import EvidenceRef
from vlah.core.types.clock import Clock
from vlah.core.types.events import DedupHit, Event
from vlah.core.types.ids import ActorId
from vlah.core.types.params import GateSpec, Params
from vlah.ports.signal_source import SignalSource

INTEGRATOR = ActorId("integrator")
CLAIM_TAGS_BY_KIND: dict[RequestKind, tuple[str, ...]] = {
    "worker": (),
    "remeasure": (REMEASURE_TAG,),
    "compose": (COMPOSE_TAG,),
}
SIMILAR_LIMIT = 5
CONFIRM_TOKEN_TTL_SECONDS = 3600.0


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
        quota = QuotaTracker(
            clock=clock, params=params, top_level_id=self.ladder[-1].declaration.id
        )
        replay_recorded_runs(blackboard, quota)
        self.runner = LadderRunner(
            records=self.records,
            quota=quota,
            budget=budget,
            gates=gates,
            gate_runner=gate_runner,
            baseline_scores=baseline_scores,
            clock=clock,
            random=random,
        )
        self.indexer = indexer
        self.baseline_scores = baseline_scores
        self.arm = arm
        self.params = params
        self.clock = clock
        self.random = random
        self.jobs: list[EvalJob] = []
        self.claims_by_hash: dict[str, str] = measured_claims_by_hash(blackboard)
        self.confirm_tokens: dict[str, ConfirmToken] = {}
        self.hash_locks: dict[str, threading.Lock] = {}
        self.request_count = 0
        self.lock = threading.Lock()
        self.tick_lock = threading.Lock()

    def next_request_id(self) -> str:
        with self.lock:
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

    def hash_lock(self, hash_value: str) -> threading.Lock:
        with self.lock:
            return self.hash_locks.setdefault(hash_value, threading.Lock())

    def request(self, request: EvalRequest, caller: ActorId) -> EvalResponse:
        checked = check_candidate(
            self.blackboard, request.candidate_directory, request.baseline
        )
        if isinstance(checked, str):
            return self.response(
                "invalid_request", request.baseline or "unknown", error=checked
            )
        else:
            error = request_error(
                self.blackboard,
                self.ladder,
                request.candidate_directory,
                request.predictions,
                request.rationale,
                request.parents,
            )
            if error is not None:
                return self.response("invalid_request", checked.baseline, error=error)
            else:
                hash_value = candidate_hash(
                    request.candidate_directory, checked.baseline
                )
                with self.hash_lock(hash_value):
                    if request.kind == "compose":
                        return self.queue_request(request, caller, checked, hash_value)
                    else:
                        return self.dedup_or_queue(request, caller, checked, hash_value)

    def dedup_or_queue(
        self,
        request: EvalRequest,
        caller: ActorId,
        checked: CheckedRequest,
        hash_value: str,
    ) -> EvalResponse:
        with self.lock:
            original_claim = self.claims_by_hash.get(hash_value)
        # Exact duplicates are caught before the similarity round-trip: an identical
        # payload is also maximally similar.
        if self.arm.hash_dedup and original_claim is not None:
            self.records.write_event(
                "dedup_hit",
                target=hash_value,
                data=DedupHit(candidate=hash_value, original_claim=original_claim),
            )
            return self.response("dedup_hit", checked.baseline, claim_id=original_claim)
        elif request.confirm_token is not None:
            if self.take_confirm_token(request.confirm_token, caller, hash_value):
                return self.queue_request(request, caller, checked, hash_value)
            else:
                return self.response(
                    "invalid_request",
                    checked.baseline,
                    error="the confirm token is not valid",
                )
        elif self.arm.needs_confirmation:
            similar = self.similar_items(
                caller, payload_text(request.candidate_directory)
            )
            if similar:
                return self.response(
                    "needs_confirmation",
                    checked.baseline,
                    similar=similar,
                    confirm_token=self.issue_confirm_token(caller, hash_value),
                )
            else:
                return self.queue_request(request, caller, checked, hash_value)
        else:
            return self.queue_request(request, caller, checked, hash_value)

    def issue_confirm_token(self, caller: ActorId, hash_value: str) -> str:
        with self.lock:
            token_value = f"{self.random.getrandbits(64):016x}"
            self.confirm_tokens[token_value] = ConfirmToken(
                caller=caller,
                candidate_hash=hash_value,
                expires_at_seconds=self.clock.now_seconds() + CONFIRM_TOKEN_TTL_SECONDS,
            )
            return token_value

    def take_confirm_token(
        self, token_value: str, caller: ActorId, hash_value: str
    ) -> bool:
        """Use up a token only for its owner and its candidate; a stranger cannot spend it."""
        with self.lock:
            token = self.confirm_tokens.get(token_value)
            if token is None:
                return False
            elif token.expires_at_seconds < self.clock.now_seconds():
                del self.confirm_tokens[token_value]
                return False
            elif token.caller != caller or token.candidate_hash != hash_value:
                return False
            else:
                del self.confirm_tokens[token_value]
                return True

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

    def queue_request(
        self,
        request: EvalRequest,
        caller: ActorId,
        checked: CheckedRequest,
        hash_value: str,
    ) -> EvalResponse:
        """Write the candidate, run the pre-submit gates, and queue it (with no lock held)."""
        self.records.write_candidate(caller, request.candidate_directory, hash_value)
        gate_failure = self.runner.failed_gate(
            "pre_submit", request.candidate_directory
        )
        if gate_failure is not None:
            gate_name, output = gate_failure
            self.records.write_gate_dead_end(
                checked.manifest.approach, hash_value, gate_name, output
            )
            return self.response(
                "gate_failed", checked.baseline, error=f"gate {gate_name} failed"
            )
        else:
            job = self.create_job(
                NewJob(
                    kind=request.kind,
                    caller=caller,
                    candidate_directory=request.candidate_directory,
                    manifest=checked.manifest,
                    baseline=checked.baseline,
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
                baseline=checked.baseline,
                status="queued",
            )

    def create_job(self, new_job: NewJob) -> EvalJob:
        claim_id = self.records.write_claim(
            "result",
            new_job.manifest.approach,
            EvidenceRef(candidate=f"candidates/{new_job.hash_value}/result.json"),
            new_job.rationale,
            tags=CLAIM_TAGS_BY_KIND[new_job.kind],
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
        with self.lock:
            self.jobs.append(job)
            # A remeasure never becomes the original a worker's duplicate points at.
            if new_job.kind == "worker":
                self.claims_by_hash.setdefault(new_job.hash_value, claim_id)
            else:
                pass
        return job

    def parent_score(self, parent: str, level_id: str, baseline: str) -> float | None:
        """The parent's last measured score at this level, else the baseline's mean (D74)."""
        with self.lock:
            measured = [
                result.score
                for job in self.jobs
                if job.candidate_hash == parent
                for result in job.level_results
                if result.level == level_id and result.score is not None
            ]
        if measured:
            return measured[-1]
        else:
            stored = stored_level_score(self.blackboard, parent, level_id)
            if stored is not None:
                return stored
            else:
                return mean_or_none(self.baseline_scores(level_id, baseline, "dev"))

    def remeasure(self, request: RemeasureRequest) -> str:
        manifest = load_manifest(request.candidate_directory)
        hash_value = candidate_hash(request.candidate_directory, request.baseline)
        self.records.write_candidate(
            INTEGRATOR, request.candidate_directory, hash_value
        )
        top_level_id = self.ladder[-1].declaration.id
        prediction = self.parent_score(
            request.parent_candidate, top_level_id, request.baseline
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
                predictions={} if prediction is None else {top_level_id: prediction},
                levels=(self.ladder[-1],),
                parent_candidate=request.parent_candidate,
                rationale=f"Integrator remeasure of {request.parent_candidate} on {request.baseline}.",
            )
        )
        return job.request_id

    def tick(self) -> tuple[Event, ...]:
        """Advance every job; one job's judge trouble never holds up the others."""
        with self.tick_lock:
            with self.lock:
                jobs = [job for job in self.jobs if not job.is_finished]
            events: list[Event] = []
            for job in jobs:
                events.extend(self.runner.advance(job))
                if job.releases_hash:
                    with self.lock:
                        if self.claims_by_hash.get(job.candidate_hash) == job.claim_id:
                            del self.claims_by_hash[job.candidate_hash]
                        else:
                            pass
                else:
                    pass
            return tuple(events)
