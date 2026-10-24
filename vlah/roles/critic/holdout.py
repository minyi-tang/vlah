"""The Critic's two holdouts (v2.2 D34, D66, D72).

holdout_run, the spot-check split, during the run. A result verified at the top
level whose paired gain is a jump (above zero and above JUMP_STANDARD_ERRORS of its
standard error) is measured once more on the audit dataset through the eval client,
and the Integrator gets its claim only afterwards. When the daily gain exceeds the
audit gain by more than D72's noise band, the Critic refutes the claim, and the
claim never reaches the Integrator. Every other verified claim goes on at once.
The rechecks draw on the Critic's share of holdout_run_cap (critic_recheck_cap);
the Integrator runs with the rest (integrator_params), so its reserved checks stay
whole.

A recheck needs the audit scores of the result's baseline: the eval client pairs
baseline@0 itself, and a later baseline is known once the Integrator has measured
it on the audit set (an audit remeasure_ready whose parent_candidate is its own
baseline). A result on any other baseline goes on unchecked.

holdout_verdict, the verdict split, after the run. It stays sealed until the Critic
has stopped; then the deliverable is scored verdict_repeat_count times, once per
run, and the mean is recorded as the run's holdout score. The eval client's
RemeasureRequest cannot name the verdict split at all, so no role reaches it during
the run.
"""

import logging
import math
import statistics
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from vlah.arms.config import ArmConfig
from vlah.core.blackboard.api import Blackboard
from vlah.core.eval.api import EvalService, RemeasureRequest
from vlah.core.integrator.api import IntegratorService
from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.clock import Clock
from vlah.core.types.events import (
    AuditChecked,
    Event,
    EventPayload,
    RemeasureReady,
    ResultReady,
    event_from_json,
    event_to_json,
)
from vlah.core.types.ids import ActorId, format_event_local_id
from vlah.core.types.params import Params
from vlah.core.types.sources import Paired, ScoreResult
from vlah.ports.signal_source import Dataset

logger = logging.getLogger(__name__)

CRITIC = ActorId("critic-01")
# [proposed] in #121: a gain this many standard errors above zero is a jump.
JUMP_STANDARD_ERRORS = 3.0
BASELINE_ZERO = "baseline@0"
BASELINE_PREFIX = "baseline@"
VERIFIED_PREFIX = "verified:"
# Float slack when a gap is compared with its threshold, as in the shared merger.
GAP_TOLERANCE = 1e-9


def critic_recheck_cap(params: Params, arm: ArmConfig) -> int:
    """The Critic's share of holdout_run_cap: half the checks the Integrator does not
    reserve, the odd one included."""
    if arm.critic_holdout:
        unreserved = max(
            params.holdout_run_cap - params.spot_check_reserved_for_bisection, 0
        )
        return unreserved - unreserved // 2
    else:
        return 0


def integrator_params(params: Params, arm: ArmConfig) -> Params:
    """The Integrator's Params: its spot checks get the cap less the Critic's share."""
    return params.model_copy(
        update={
            "holdout_run_cap": params.holdout_run_cap - critic_recheck_cap(params, arm)
        }
    )


def is_jump(paired: Paired) -> bool:
    return paired.mean > 0 and paired.mean > JUMP_STANDARD_ERRORS * paired.se


def paired_gain(
    candidate_per_seed: tuple[float, ...], baseline_per_seed: tuple[float, ...]
) -> Paired | None:
    """Candidate minus baseline, seed by seed; None when the seeds do not line up."""
    if not candidate_per_seed or len(candidate_per_seed) != len(baseline_per_seed):
        return None
    else:
        differences = [
            candidate - baseline
            for candidate, baseline in zip(
                candidate_per_seed, baseline_per_seed, strict=True
            )
        ]
        return Paired(
            mean=statistics.fmean(differences),
            se=(
                statistics.stdev(differences) / math.sqrt(len(differences))
                if len(differences) >= 2
                else 0.0
            ),
            n_items=len(differences),
            n_reps=1,
        )


class VerdictJudge(Protocol):
    def measure_directory(
        self, directory: Path, dataset: Dataset, baseline: str
    ) -> ScoreResult:
        """Score a directory now, outside the run's queue (the top-level judge)."""
        ...


class DeliverableDirectory(Protocol):
    def __call__(self, deliverable: BaselineRecord) -> Path:
        """The files of the frozen deliverable, as the Integrator built them."""
        ...


class HoldoutRecorder(Protocol):
    def record_holdout(self, score: float) -> None: ...


@dataclass(kw_only=True)
class SealedVerdict:
    """The deliverable on the verdict split, repeat_count runs, used once (D66, D72)."""

    judge: VerdictJudge
    repeat_count: int
    deliverable_directory: DeliverableDirectory
    metrics: HoldoutRecorder
    is_used: bool = False

    def score(self, deliverable: BaselineRecord) -> float | None:
        """The mean of the scored runs; a void run is not run again (n is fixed)."""
        if self.is_used:
            raise RuntimeError("the verdict split is used once per run (D72)")
        else:
            self.is_used = True
        directory = self.deliverable_directory(deliverable)
        baseline = f"{BASELINE_PREFIX}{deliverable.k}"
        scores = [
            self.judge.measure_directory(directory, "judge", baseline).value
            for _ in range(self.repeat_count)
        ]
        measured = [score for score in scores if score is not None]
        if not measured:
            logger.error(
                f"the judge scored none of {self.repeat_count} verdict runs of "
                f"{baseline}; the run has no holdout score"
            )
            return None
        else:
            mean_score = statistics.fmean(measured)
            logger.info(
                f"verdict on {baseline}: {mean_score:.4f}, the mean of "
                f"{len(measured)} of {self.repeat_count} runs: {scores}"
            )
            self.metrics.record_holdout(mean_score)
            return mean_score


@dataclass(frozen=True, kw_only=True)
class Recheck:
    result_claim_id: str
    result: ResultReady


@dataclass(kw_only=True)
class HoldoutCritic:
    """Jump alerts on holdout_run and the sealed verdict, for one run.

    on_verified is called on whichever thread merges the claim_status event; tick,
    stop and score_verdict on the supervisor's. integrator is where a claim goes on.
    """

    blackboard: Blackboard
    eval_service: EvalService
    integrator: IntegratorService
    arm: ArmConfig
    params: Params
    clock: Clock
    top_level: str
    work_directory: Path
    # None where the verdict is scored elsewhere: the testbed runner scores it there.
    verdict: SealedVerdict | None = None
    actor: ActorId = CRITIC
    lock: threading.RLock = field(default_factory=threading.RLock)
    event_cursor: int = 0
    pending_claims: list[str] = field(default_factory=list[str])
    results_by_claim: dict[str, ResultReady] = field(
        default_factory=dict[str, ResultReady]
    )
    rechecks_by_request: dict[str, Recheck] = field(default_factory=dict[str, Recheck])
    audit_per_seed_by_baseline: dict[str, tuple[float, ...]] = field(
        default_factory=dict[str, tuple[float, ...]]
    )
    recheck_cap: int = field(init=False)
    recheck_count: int = field(init=False)
    event_count: int = field(init=False)
    is_stopped: bool = False

    def __post_init__(self) -> None:
        self.recheck_cap = critic_recheck_cap(self.params, self.arm)
        own_events = [
            event_from_json(self.blackboard.read(path, None).decode())
            for path in self.blackboard.list_paths(f"events/{self.actor}/", None)
        ]
        self.event_count = len(own_events)
        # A restarted Critic keeps its earlier rechecks inside its share.
        self.recheck_count = sum(
            1 for event in own_events if event.type == "audit_checked"
        )

    def on_verified(self, result_claim_id: str) -> None:
        """A result claim verified at the top level; looked at on the next tick."""
        with self.lock:
            self.pending_claims.append(result_claim_id)

    def tick(self) -> tuple[Event, ...]:
        with self.lock:
            if self.is_stopped:
                return ()
            else:
                written: list[Event] = []
                for recheck, ready_event in self.read_new_events():
                    written.extend(self.decide(recheck, ready_event))
                while self.pending_claims:
                    result = self.results_by_claim.get(self.pending_claims[0])
                    if result is None:
                        # Its result_ready merges after the claim_status event.
                        break
                    else:
                        self.consider(self.pending_claims[0], result)
                        self.pending_claims.pop(0)
                return tuple(written)

    def read_new_events(self) -> list[tuple[Recheck, Event]]:
        """Top-level results, the answers to rechecks, and baselines' audit scores."""
        answers: list[tuple[Recheck, Event]] = []
        head = self.blackboard.head_sequence()
        for sequence in range(self.event_cursor + 1, head + 1):
            path = self.blackboard.read(f"events/_seq/{sequence}", None).decode()
            event = event_from_json(self.blackboard.read(path, None).decode())
            data = event.data
            if isinstance(data, ResultReady) and data.level == self.top_level:
                self.results_by_claim[data.claim_id] = data
            elif isinstance(data, RemeasureReady) and data.dataset == "audit":
                recheck = self.rechecks_by_request.pop(data.request_id, None)
                if recheck is not None:
                    answers.append((recheck, event))
                elif data.parent_candidate == data.baseline and data.per_seed:
                    self.audit_per_seed_by_baseline[data.baseline] = data.per_seed
                else:
                    pass
            else:
                pass
        self.event_cursor = head
        return answers

    def consider(self, result_claim_id: str, result: ResultReady) -> None:
        """Recheck a jump while the share lasts; pass everything else on."""
        if not self.arm.critic_holdout or result.paired is None:
            self.integrator.on_verified(result_claim_id)
        elif not is_jump(result.paired):
            self.integrator.on_verified(result_claim_id)
        elif self.recheck_count >= self.recheck_cap:
            logger.info(
                f"no recheck left for the jump of {result.candidate[:12]} "
                f"({self.recheck_count} of {self.recheck_cap} used)"
            )
            self.integrator.on_verified(result_claim_id)
        elif (
            result.baseline != BASELINE_ZERO
            and result.baseline not in self.audit_per_seed_by_baseline
        ):
            logger.info(
                f"no audit scores for {result.baseline} yet; the jump of "
                f"{result.candidate[:12]} goes on unchecked"
            )
            self.integrator.on_verified(result_claim_id)
        else:
            request_id = self.eval_service.remeasure(
                RemeasureRequest(
                    candidate_directory=self.materialize(result.candidate),
                    baseline=result.baseline,
                    parent_candidate=result.candidate,
                    approach=result.approach,
                    dataset="audit",
                )
            )
            self.recheck_count += 1
            self.rechecks_by_request[request_id] = Recheck(
                result_claim_id=result_claim_id, result=result
            )
            logger.info(
                f"jump alert: {result.candidate[:12]} gained "
                f"{result.paired.mean:.4f} (se {result.paired.se:.4f}) on "
                f"{result.baseline}; rechecking it on the audit set "
                f"({self.recheck_count} of {self.recheck_cap})"
            )

    def materialize(self, candidate: str) -> Path:
        """The candidate's payload and manifest as a directory the eval client reads."""
        directory = self.work_directory / candidate
        prefix = f"candidates/{candidate}/candidate/"
        for path in self.blackboard.list_paths(prefix, None):
            target = directory / path.removeprefix(prefix)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.blackboard.read(path, None))
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "manifest.yaml").write_bytes(
            self.blackboard.read(f"candidates/{candidate}/manifest.yaml", None)
        )
        return directory

    def audit_gain(self, ready: RemeasureReady) -> Paired | None:
        if ready.score is None:
            return None
        elif ready.paired is not None:
            return ready.paired
        else:
            return paired_gain(
                ready.per_seed, self.audit_per_seed_by_baseline.get(ready.baseline, ())
            )

    def decide(self, recheck: Recheck, ready_event: Event) -> list[Event]:
        """Refute the claim when the gain does not hold; else pass it on."""
        ready = ready_event.data
        daily = recheck.result.paired
        assert isinstance(ready, RemeasureReady) and daily is not None
        audit = self.audit_gain(ready)
        evidence = f"events/{ready_event.local_id}.json"
        if audit is None:
            logger.warning(
                f"the recheck of {ready.parent_candidate[:12]} has no paired audit "
                f"score ({evidence}); its claim goes on"
            )
            self.integrator.on_verified(recheck.result_claim_id)
            return []
        else:
            threshold = self.params.spot_check_sigma_multiple * math.sqrt(
                daily.se**2 + audit.se**2
            )
            gap = daily.mean - audit.mean
            holds = (
                gap <= threshold + GAP_TOLERANCE and ready.constraint_ok is not False
            )
            baseline_number = int(ready.baseline.removeprefix(BASELINE_PREFIX))
            checked = self.write_event(
                "audit_checked",
                target=recheck.result_claim_id,
                source=self.top_level,
                evidence=(evidence,),
                data=AuditChecked(
                    segment=(baseline_number, baseline_number),
                    daily=daily,
                    audit=audit,
                    threshold=threshold,
                    flagged=not holds,
                ),
            )
            if holds:
                self.integrator.on_verified(recheck.result_claim_id)
                return [checked]
            else:
                refuted = self.write_event(
                    "claim_status",
                    target=recheck.result_claim_id,
                    from_status=f"{VERIFIED_PREFIX}{self.top_level}",
                    to_status="refuted",
                    reason=(
                        f"the daily gain {daily.mean:.4f} does not hold on the audit "
                        f"set: {audit.mean:.4f}, a gap of {gap:.4f} over the "
                        f"threshold {threshold:.4f}"
                        + (
                            "; the judge reported constraint_ok = false"
                            if ready.constraint_ok is False
                            else ""
                        )
                    ),
                    evidence=(f"events/{checked.local_id}.json", evidence),
                )
                logger.warning(f"refuted {recheck.result_claim_id}: {refuted.reason}")
                return [checked, refuted]

    def write_event(
        self,
        event_type: Literal["audit_checked", "claim_status"],
        *,
        target: str,
        evidence: tuple[str, ...],
        data: EventPayload | None = None,
        from_status: str | None = None,
        to_status: str | None = None,
        source: str | None = None,
        reason: str | None = None,
    ) -> Event:
        self.event_count += 1
        event = Event.model_validate(
            {
                "local_id": format_event_local_id(self.actor, self.event_count),
                "ts": self.clock.now_seconds(),
                "actor": self.actor,
                "role": "critic",
                "type": event_type,
                "target": target,
                "from": from_status,
                "to": to_status,
                "source": source,
                "reason": reason,
                "evidence": evidence,
                "data": None if data is None else data.model_dump(),
            }
        )
        receipt = self.blackboard.write(
            self.actor,
            {f"events/{event.local_id}.json": event_to_json(event).encode("utf-8")},
            f"{event_type} {target}",
        )
        if receipt.status == "rejected":
            raise RuntimeError(
                f"the blackboard rejected {event.local_id}: {receipt.violations}"
            )
        else:
            return event

    def stop(self) -> None:
        """The run is over: no more rechecks, and every held claim goes on, so the
        Integrator's closeout records it as ended with the run (D79)."""
        with self.lock:
            self.is_stopped = True
            held = [
                *self.pending_claims,
                *(
                    recheck.result_claim_id
                    for recheck in self.rechecks_by_request.values()
                ),
            ]
            self.pending_claims.clear()
            self.rechecks_by_request.clear()
        for result_claim_id in held:
            self.integrator.on_verified(result_claim_id)

    def score_verdict(self, deliverable: BaselineRecord) -> float | None:
        """The sealed verdict on the frozen deliverable; refused while the run goes on."""
        with self.lock:
            if not self.is_stopped:
                raise RuntimeError(
                    "the verdict split is sealed until the run has stopped (D66)"
                )
            elif self.verdict is None:
                logger.info("the verdict is scored outside this run (testbed)")
                return None
            else:
                return self.verdict.score(deliverable)
