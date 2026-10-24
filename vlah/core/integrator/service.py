"""The herd's Integrator: the shared merger wrapped in blackboard events and tasks (D74, D79).

on_verified feeds the core first in, first out, as result claims reach the top
level. tick lets the core act: it reads the remeasure_ready events the eval client
wrote since the last tick and hands them to the measurer, then steps the core,
records its decisions, runs a guard batch when one is due and reruns the map after
an advance. closeout follows D79: no new candidates, the guard catch-up on pending
baselines, the core's final spot check, then the frozen deliverable.

One re-entrant lock covers the core: on_verified runs on whichever thread delivers
a merge, tick and closeout on the supervisor's.
"""

import logging
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from vlah.arms.config import ArmConfig
from vlah.core.blackboard.api import Blackboard
from vlah.core.eval.api import EvalService
from vlah.core.integrator.core_adapter import (
    CoreAdapter,
    CoreCalibration,
    Decision,
    Measurement,
    Measurer,
    VerifiedCandidate,
    Version,
    Waiter,
    build_core,
)
from vlah.core.integrator.guard import GuardBatches, GuardGateRunner
from vlah.core.integrator.measurer import EvalMeasurer
from vlah.core.integrator.opportunity_map import (
    OpportunityMap,
    ScriptRunner,
    run_script,
)
from vlah.core.integrator.records import IntegratorRecords
from vlah.core.queue.api import TaskQueue
from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.candidates import CandidateManifest
from vlah.core.types.claims import parse_claim
from vlah.core.types.clock import Clock
from vlah.core.types.events import Event, RemeasureReady, event_from_json
from vlah.core.types.params import GateSpec, Params

logger = logging.getLogger(__name__)

STEPS_PER_TICK = 8
DRAIN_STEPS = 10_000
VERIFIED_PREFIX = "verified:"
# The eval client tags the result claim of a compose candidate (D74).
COMPOSE_TAG = "compose"
EVAL_SERVICE = "eval_service"


def no_wait() -> None:
    """The waiter for tests, whose measurer answers at once."""
    return


class LevelRecord(BaseModel):
    """One level of candidates/<hash>/result.json, as the Integrator reads it."""

    model_config = ConfigDict(extra="ignore")

    level: str
    score: float | None
    per_seed: tuple[float, ...]
    status: str

    @property
    def is_verified(self) -> bool:
        return self.score is not None and self.status.startswith(VERIFIED_PREFIX)


class ResultRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    candidate: str
    submitter: str
    levels: tuple[LevelRecord, ...]

    def at(self, level: str) -> LevelRecord | None:
        return next((item for item in self.levels if item.level == level), None)


@dataclass(kw_only=True)
class HerdIntegrator:
    blackboard: Blackboard
    eval_service: EvalService
    task_queue: TaskQueue
    compose_queue: TaskQueue
    gates: tuple[GateSpec, ...]
    gate_runner: GuardGateRunner
    arm: ArmConfig
    params: Params
    clock: Clock
    top_level: str
    work_directory: Path
    baseline_directory: Path
    calibration: CoreCalibration
    # The core's measurer; None means the eval client, through EvalMeasurer.
    measurer: Measurer | None = None
    script_runner: ScriptRunner = run_script
    waiter: Waiter = no_wait
    lock: threading.RLock = field(default_factory=threading.RLock)
    event_cursor: int = 0
    # Verified claims not yet handed to the core, in verification order.
    pending_claims: list[str] = field(default_factory=list[str])
    deliverable: BaselineRecord | None = None
    records: IntegratorRecords = field(init=False)
    guard: GuardBatches = field(init=False)
    eval_measurer: EvalMeasurer = field(init=False)
    core: CoreAdapter = field(init=False)
    map: OpportunityMap = field(init=False)

    def __post_init__(self) -> None:
        self.guard = GuardBatches(
            gates=self.gates,
            gate_runner=self.gate_runner,
            every_merges=self.params.guard_every_merges,
        )
        self.records = IntegratorRecords(
            sink=self.blackboard,
            task_queue=self.task_queue,
            compose_queue=self.compose_queue,
            clock=self.clock,
            top_level=self.top_level,
            guard_pending_on_advance=self.guard.enabled,
        )
        self.records.start()
        self.eval_measurer = EvalMeasurer(
            eval_service=self.eval_service, clock=self.clock
        )
        self.core = build_core(
            params=self.params,
            work_directory=self.work_directory,
            baseline_directory=self.baseline_directory,
            calibration=self.calibration,
            clock=self.clock,
            measurer=self.measurer if self.measurer is not None else self.eval_measurer,
            on_advance=self.on_version_complete,
        )
        self.map = OpportunityMap(
            blackboard=self.blackboard,
            records=self.records,
            arm=self.arm,
            work_directory=self.work_directory,
            runner=self.script_runner,
        )
        self.event_cursor = self.blackboard.head_sequence()
        self.on_version_complete(self.core.versions[0])
        self.map.run(0, Path(self.core.versions[0].dir))

    # The core's callback: a version is complete

    def on_version_complete(self, version: Version) -> None:
        self.guard.mark(version)
        if self.guard.enabled and version.added is None and version.k > 0:
            # A revert's content is final now: its gates go into its record.
            self.guard.run_gates(version)
        else:
            pass
        self.records.write_baseline(
            version,
            guard_pending=self.guard.is_pending(version.k),
            gates=self.guard.outcomes.get(version.k, {}),
        )

    # Feeding the core

    def result_of(self, candidate: str) -> ResultRecord | None:
        path = f"candidates/{candidate}/result.json"
        if not self.blackboard.list_paths(path, None):
            return None
        else:
            return ResultRecord.model_validate_json(self.blackboard.read(path, None))

    def materialize(self, candidate: str) -> Path:
        """The candidate's payload as a directory the core can merge."""
        directory = self.work_directory / "cands" / candidate
        if directory.exists():
            return directory
        else:
            prefix = f"candidates/{candidate}/candidate/"
            for path in self.blackboard.list_paths(prefix, None):
                target = directory / path.removeprefix(prefix)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(self.blackboard.read(path, None))
            directory.mkdir(parents=True, exist_ok=True)
            return directory

    def on_verified(self, result_claim_id: str) -> None:
        """Queue a result claim verified at the top level, first in, first out.

        The eval client writes the claim_status event before the candidate's
        result.json, so the claim is only remembered here and read at the next tick.
        """
        with self.lock:
            self.pending_claims.append(result_claim_id)

    def admit_pending(self) -> None:
        """Feed the core the verified claims whose result.json has arrived, in order."""
        while self.pending_claims and self.admit(self.pending_claims[0]):
            self.pending_claims.pop(0)

    def admit(self, result_claim_id: str) -> bool:
        """True once the claim is queued or dismissed; False while its result is missing."""
        claim_path = f"claims/{EVAL_SERVICE}/{result_claim_id}.md"
        if not self.blackboard.list_paths(claim_path, None):
            logger.warning(f"{result_claim_id} is not a result claim on the blackboard")
            return True
        else:
            claim = parse_claim(self.blackboard.read(claim_path, None).decode("utf-8"))
            pointers = [item.candidate for item in claim.evidence if item.candidate]
            candidate = pointers[0].split("/")[1] if pointers else None
            result = self.result_of(candidate) if candidate is not None else None
            level = result.at(self.top_level) if result is not None else None
            if candidate is None:
                logger.warning(f"{result_claim_id} points at no candidate")
                return True
            elif result is None:
                return False
            elif level is None or level.score is None or not level.is_verified:
                logger.info(
                    f"{result_claim_id} has no verified {self.top_level} result"
                )
                return True
            else:
                manifest_data: object = yaml.safe_load(
                    self.blackboard.read(f"candidates/{candidate}/manifest.yaml", None)
                )
                manifest = CandidateManifest.model_validate(manifest_data)
                self.records.remember(
                    candidate,
                    result_claim_id,
                    manifest.approach,
                    is_compose=COMPOSE_TAG in claim.tags,
                )
                self.eval_measurer.approach_by_hash[candidate] = manifest.approach
                self.core.add(
                    VerifiedCandidate(
                        result_claim_id,
                        candidate,
                        manifest.target,
                        list(level.per_seed),
                        level.score,
                        self.materialize(candidate),
                        result.submitter,
                    )
                )
                return True

    # Measurements coming back

    def ingest_remeasures(self) -> None:
        """Hand every remeasure_ready event since the last tick to the measurer."""
        head = self.blackboard.head_sequence()
        for sequence in range(self.event_cursor + 1, head + 1):
            path = self.blackboard.read(f"events/_seq/{sequence}", None).decode()
            event = event_from_json(self.blackboard.read(path, None).decode())
            if isinstance(event.data, RemeasureReady):
                self.answer(event.data)
            else:
                pass
        self.event_cursor = head

    def answer(self, ready: RemeasureReady) -> None:
        """The event's own per-seed scores; result.json is written once per hash,
        so an event without them (written before #98) falls back to the record.

        A void answer is asked again. If the judge still cannot score it, a dev
        measurement fails its candidate as before, but an audit measurement
        answers with the version's own daily scores: the spot check then finds no
        gap and passes with an error in the log, because a judge failure must
        never revert a merge (D72).
        """
        if ready.score is None and self.eval_measurer.resubmit(ready.request_id):
            return
        else:
            pass
        if ready.per_seed:
            per_seed = list(ready.per_seed)
        else:
            result = self.result_of(ready.candidate)
            level = result.at(ready.level) if result is not None else None
            per_seed = list(level.per_seed) if level is not None else []
        submitted = self.eval_measurer.submission(ready.request_id)
        if (
            ready.score is None
            and submitted is not None
            and submitted.dataset == "audit"
        ):
            version = self.core.versions[submitted.baseline]
            logger.error(
                f"the judge could not score baseline@{version.k} on the audit set; "
                "its spot check is skipped, not flagged"
            )
            per_seed = list(version.per_seed)
            score = version.score
            constraint_ok = True
        else:
            score = ready.score
            constraint_ok = score is not None and ready.constraint_ok is not False
        self.eval_measurer.answer(
            Measurement(
                ready.request_id,
                ready.candidate,
                per_seed,
                score,
                constraint_ok,
            )
        )

    # Stepping

    def handle(self, decisions: list[Decision], is_closing: bool) -> None:
        for decision in decisions:
            self.records.record(decision, is_closing)
            baseline = decision.get("baseline")
            if decision.get("type") == "baseline_advanced" and isinstance(
                baseline, int
            ):
                self.after_advance(baseline)
            else:
                pass

    def after_advance(self, k: int) -> None:
        self.run_guard(is_closing=False)
        if self.map.updates_after_advances():
            self.map.run(self.core.k, Path(self.core.current().dir))
        else:
            pass

    def run_guard(self, is_closing: bool) -> None:
        """One guard step when a batch is due and no measurement is pending.

        A revert may need a measurement of the new head; the next tick, once it
        has arrived, checks the head again, so no judge run blocks the supervisor.
        """
        if self.guard.is_due() and self.core.due(self.clock.now_seconds()) != "wait":
            self.guard_step(is_closing)
        else:
            pass

    def guard_step(self, is_closing: bool) -> None:
        """One guard check; a revert's decisions leave the core on the next step."""
        if self.guard.check(self.core) is not None:
            self.handle(self.core.step(), is_closing)
        else:
            pass

    def step_core(self, is_closing: bool) -> None:
        for _ in range(STEPS_PER_TICK):
            due = self.core.due(self.clock.now_seconds())
            if due == "now":
                self.handle(self.core.step(), is_closing)
            elif due == "wait":
                self.handle(self.core.step(), is_closing)
                return
            else:
                return

    def drain(self, is_closing: bool) -> None:
        """Let a pending measurement finish, waiting between polls."""
        for _ in range(DRAIN_STEPS):
            if self.core.due(self.clock.now_seconds()) == "wait":
                self.handle(self.core.step(), is_closing)
                if self.core.due(self.clock.now_seconds()) == "wait":
                    self.wait_for_measurement()
                else:
                    pass
            else:
                return
        logger.error("a measurement never arrived; the Integrator stops waiting")

    def tick(self) -> tuple[Event, ...]:
        with self.lock:
            before = len(self.records.events_written)
            self.ingest_remeasures()
            self.admit_pending()
            self.run_guard(is_closing=False)
            self.step_core(is_closing=False)
            return tuple(self.records.events_written[before:])

    def record_of(self, version: Version, guard_pending: bool) -> BaselineRecord:
        return BaselineRecord.model_validate(
            {
                **version.record(),
                "guard_pending": guard_pending,
                "gates": self.guard.outcomes.get(version.k, {}),
            }
        )

    def current(self) -> BaselineRecord:
        with self.lock:
            version = self.core.current()
            return self.record_of(version, self.guard.is_pending(version.k))

    # Closeout (D79)

    def wait_for_measurement(self) -> None:
        self.waiter()
        self.ingest_remeasures()

    def guard_catch_up(self) -> None:
        """Re-run the gates for everything merged since the last guarded version."""
        while self.guard.is_unguarded(self.core):
            self.guard_step(is_closing=True)
            self.drain(is_closing=True)

    def closeout(self) -> BaselineRecord:
        """Stop accepting; guard catch-up; the final spot check; freeze the deliverable."""
        with self.lock:
            if self.deliverable is not None:
                return self.deliverable
            else:
                self.ingest_remeasures()
                self.admit_pending()
                # Stop accepting first: a guard revert steps the core, which would
                # otherwise integrate a queued candidate during the catch-up.
                self.core.closed = True
                self.drain(is_closing=True)
                self.guard_catch_up()
                version, decisions = self.core.closeout(self.wait_for_measurement)
                self.handle(decisions, is_closing=True)
                self.guard_catch_up()
                final = self.guard.deliverable(self.core)
                if final.k != version.k:
                    logger.warning(
                        f"the deliverable is baseline@{final.k}, not the core's "
                        f"baseline@{version.k}: the guard gates decided"
                    )
                else:
                    pass
                self.deliverable = self.record_of(final, guard_pending=False)
                return self.deliverable

    def close(self) -> None:
        shutil.rmtree(self.work_directory / "cands", ignore_errors=True)
