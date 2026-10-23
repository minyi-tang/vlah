"""Scaffolding fakes of the component contracts, just enough to run the smoke loop.

These are not reference implementations: they skip most rules on purpose. Each
Wave-1 component replaces one of them. make_fake_components returns them as
HerdComponents, whose fields are typed by the frozen contracts, so pyright proves
each fake satisfies its contract.
"""

import statistics
from collections.abc import Mapping
from dataclasses import dataclass, field

import yaml

from vlah.adapters.memory.store import MemoryVersionedStore
from vlah.arms.config import ArmConfig
from vlah.core.blackboard.api import (
    Blackboard,
    CommitListener,
    MergedCommit,
    PathFilter,
    Violation,
    WriteReceipt,
)
from vlah.core.budget.api import BudgetView
from vlah.core.eval.api import EvalRequest, EvalResponse, RemeasureRequest
from vlah.core.indexer.api import (
    ApproachState,
    ClaimState,
    Diff,
    Hit,
    IndexerMetrics,
    WorkingSet,
)
from vlah.core.queue.api import HandleLiveness
from vlah.core.types.candidates import CandidateManifest, candidate_hash, payload_files
from vlah.core.types.claims import Claim, EvidenceRef, parse_claim, render_claim
from vlah.core.types.clock import Clock
from vlah.core.types.events import Event, ResultReady, event_from_json, event_to_json
from vlah.core.types.ids import (
    ActorId,
    Role,
    format_claim_id,
    format_event_local_id,
    format_task_id,
)
from vlah.core.types.interrupts import Interrupt
from vlah.core.types.paths import may_create
from vlah.core.types.sources import Paired
from vlah.core.types.states import INITIAL_STATUS
from vlah.core.types.tasks import Lease, Task
from vlah.ports.signal_source import CandidateRef, SignalSource
from vlah.ports.store import MergeResult
from vlah.runtime.wiring import HerdComponents

PUSH_TOKEN = "fake-push-token"
EVAL_SERVICE = ActorId("eval_service")


def is_event_path(path: str) -> bool:
    return path.startswith("events/") and not path.startswith("events/_seq/")


def is_claim_path(path: str) -> bool:
    return path.startswith("claims/")


class FakeBlackboard:
    """Commits and merges through a MemoryVersionedStore; checks only paths and parsing."""

    def __init__(self, *, name: str) -> None:
        self.store = MemoryVersionedStore(name=name, push_token=PUSH_TOKEN)
        self.listeners: dict[int, CommitListener] = {}
        self.next_subscription_id = 1

    def check(
        self, actor: ActorId, files: Mapping[str, bytes]
    ) -> tuple[Violation, ...]:
        violations: list[Violation] = []
        for path, content in files.items():
            if not may_create(actor, path):
                violations.append(
                    Violation(
                        path=path,
                        rule="path_not_allowed",
                        message=f"{actor} may not write",
                    )
                )
            elif self.store.exists(path):
                violations.append(
                    Violation(path=path, rule="file_exists", message="exists")
                )
            elif is_claim_path(path):
                parse_claim(content.decode("utf-8"))
            elif is_event_path(path):
                event_from_json(content.decode("utf-8"))
            else:
                pass
        return tuple(violations)

    def merged_commit(self, merge_result: MergeResult) -> MergedCommit:
        events = tuple(
            event_from_json(self.store.read(path, None).decode("utf-8")).model_copy(
                update={"seq": sequence_number}
            )
            for path, sequence_number in sorted(
                merge_result.event_sequence_numbers.items(), key=lambda entry: entry[1]
            )
        )
        claims = tuple(
            parse_claim(self.store.read(path, None).decode("utf-8"))
            for path in merge_result.added_paths
            if is_claim_path(path)
        )
        return MergedCommit(
            commit_id=merge_result.commit_id,
            actor=merge_result.author,
            paths=merge_result.added_paths,
            events=events,
            claims=claims,
        )

    def write(
        self, actor: ActorId, files: Mapping[str, bytes], message: str
    ) -> WriteReceipt:
        violations = self.check(actor, files)
        if violations:
            return WriteReceipt(
                status="rejected",
                commit_id=None,
                sequence_numbers=(),
                violations=violations,
            )
        else:
            branch = f"w/{actor}"
            commit_id = self.store.commit(branch, files, actor, message, PUSH_TOKEN)
            merge_results = self.store.merge(branch)
            sequence_numbers: list[int] = []
            for merge_result in merge_results:
                sequence_numbers.extend(merge_result.event_sequence_numbers.values())
                merged = self.merged_commit(merge_result)
                for listener in list(self.listeners.values()):
                    listener(merged)
            return WriteReceipt(
                status="merged",
                commit_id=commit_id,
                sequence_numbers=tuple(sequence_numbers),
                violations=(),
            )

    def read(self, path: str, at_sequence: int | None) -> bytes:
        return self.store.read(path, at_sequence)

    def list_paths(self, prefix: str, at_sequence: int | None) -> tuple[str, ...]:
        return self.store.list_paths(prefix, at_sequence)

    def head_sequence(self) -> int:
        return self.store.head_sequence()

    def subscribe(self, listener: CommitListener) -> int:
        subscription_id = self.next_subscription_id
        self.next_subscription_id += 1
        self.listeners[subscription_id] = listener
        return subscription_id

    def unsubscribe(self, subscription_id: int) -> None:
        self.listeners.pop(subscription_id)

    def mirror(self, name: str, keep: PathFilter) -> Blackboard:
        raise NotImplementedError("the fake blackboard has no mirrors; see W1-01 (#11)")

    def clone_url(self) -> str:
        return self.store.clone_url(True)


@dataclass(kw_only=True)
class FakeIndexer:
    """Status from the latest claim_status event, substring search, empty diffs."""

    claims: dict[str, Claim] = field(default_factory=dict[str, Claim])
    statuses: dict[str, str] = field(default_factory=dict[str, str])
    head: int = 0

    def apply(self, merged_commit: MergedCommit) -> None:
        for claim in merged_commit.claims:
            self.claims[claim.id] = claim
            self.statuses[claim.id] = claim.status
        for event in merged_commit.events:
            self.head = max(self.head, event.seq or 0)
            if (
                event.type == "claim_status"
                and event.target is not None
                and event.to is not None
            ):
                self.statuses[event.target] = event.to
            else:
                pass

    def rebuild(self) -> None:
        pass

    def head_sequence(self) -> int:
        return self.head

    def claim_state(self, claim_id: str) -> ClaimState:
        return ClaimState(
            claim_id=claim_id,
            status=self.statuses[claim_id],
            history=(),
            is_cascaded_refuted=False,
        )

    def approach_state(self, approach_id: str) -> ApproachState:
        return ApproachState(approach_id=approach_id, status="active", history=())

    def search(
        self, query: str, limit: int, viewer: ActorId, arm: ArmConfig
    ) -> tuple[Hit, ...]:
        hits = [
            Hit(item_id=claim.id, kind="claim", similarity=1.0, summary=claim.body[:80])
            for claim in self.claims.values()
            if query.lower() in claim.body.lower()
        ]
        return tuple(hits[:limit])

    def similar_candidates(
        self, payload_text: str, limit: int, viewer: ActorId
    ) -> tuple[Hit, ...]:
        return ()

    def similar_dead_ends(
        self, payload_text: str, limit: int, viewer: ActorId
    ) -> tuple[Hit, ...]:
        return ()

    def diff_since(
        self,
        cursor: int,
        working_set: WorkingSet,
        viewer: ActorId,
        max_tokens: int,
        arm: ArmConfig,
    ) -> Diff:
        return Diff(items=(), cursor=cursor, omitted_count=0, rendered="")

    def reuse_count(self, claim_id: str) -> int:
        return 0

    def metrics(self) -> IndexerMetrics:
        return IndexerMetrics(dead_end_rewalk_count=0, insight_reuse_counts={})


@dataclass(kw_only=True)
class FakeTaskQueue:
    """for_agent tasks first, then priority, then first in; no slot balancing."""

    tasks: list[Task] = field(default_factory=list[Task])
    created_count: int = 0

    def put(self, task: Task) -> None:
        self.tasks.append(task)

    def claim_task(self, agent: ActorId) -> Task | None:
        ordered = sorted(
            enumerate(self.tasks),
            key=lambda entry: (
                entry[1].for_agent != agent,
                entry[1].priority,
                entry[0],
            ),
        )
        for index, task in ordered:
            if task.for_agent is None or task.for_agent == agent:
                return self.tasks.pop(index)
            else:
                pass
        return None

    def complete(self, task_id: str, agent: ActorId) -> None:
        pass

    def requeue(self, task_id: str, reason: str) -> None:
        pass

    def next_task_id(self) -> str:
        self.created_count += 1
        return format_task_id(self.created_count)

    def maintain(self, slots_per_approach: Mapping[str, int]) -> tuple[Task, ...]:
        added: list[Task] = []
        for approach, slot_count in sorted(slots_per_approach.items()):
            queued_count = sum(1 for task in self.tasks if task.approach == approach)
            for _ in range(slot_count - queued_count):
                task = Task(
                    id=self.next_task_id(),
                    approach=approach,
                    kind="advance",
                    goal=f"Advance the whole problem using approach {approach}",
                    created_by="queue",
                )
                self.put(task)
                added.append(task)
        return tuple(added)

    def on_result_ready(self, event: Event) -> Task:
        assert isinstance(event.data, ResultReady)
        task = Task(
            id=self.next_task_id(),
            approach=event.data.approach,
            kind="result_followup",
            goal=f"Decide the next step from the result of candidate {event.data.candidate}",
            refs=(event.data.claim_id, event.data.candidate),
            priority=0,
            for_agent=event.data.submitter,
            created_by="queue",
        )
        self.put(task)
        return task


@dataclass(kw_only=True)
class FakeLeases:
    leases: dict[ActorId, Lease] = field(default_factory=dict[ActorId, Lease])
    clock: Clock

    def acquire(self, agent: ActorId, task: Task, handle_id: str) -> Lease:
        now_seconds = self.clock.now_seconds()
        lease = Lease(
            agent=agent,
            approach=task.approach,
            task_id=task.id,
            handle_id=handle_id,
            target_claims=task.refs,
            heartbeat_ts=now_seconds,
            acquired_ts=now_seconds,
        )
        self.leases[agent] = lease
        return lease

    def beat(
        self, agent: ActorId, files_touched: tuple[str, ...], timestamp_seconds: float
    ) -> None:
        lease = self.leases.get(agent)
        if lease is not None:
            self.leases[agent] = lease.model_copy(
                update={
                    "heartbeat_ts": timestamp_seconds,
                    "files_touched": files_touched,
                }
            )
        else:
            pass

    def add_targets(
        self, agent: ActorId, claim_ids: tuple[str, ...], depends_on: tuple[str, ...]
    ) -> None:
        lease = self.leases[agent]
        self.leases[agent] = lease.model_copy(
            update={
                "target_claims": lease.target_claims + claim_ids,
                "depends_on": lease.depends_on + depends_on,
            }
        )

    def release(self, agent: ActorId) -> None:
        self.leases.pop(agent, None)

    def get(self, agent: ActorId) -> Lease | None:
        return self.leases.get(agent)

    def active(self) -> tuple[Lease, ...]:
        return tuple(self.leases.values())

    def reclaim_stale(
        self, now_seconds: float, liveness: HandleLiveness
    ) -> tuple[str, ...]:
        return ()


@dataclass(frozen=True, kw_only=True)
class PendingJob:
    job_id: str
    candidate_hash: str
    claim_id: str
    submitter: ActorId
    approach: str
    baseline: str


def require_merged(receipt: WriteReceipt) -> None:
    """Fakes must not hide a rejected write: the smoke loop has to catch it."""
    if receipt.status == "rejected":
        raise RuntimeError(f"blackboard rejected a write: {receipt.violations}")
    else:
        pass


class FakeEvalService:
    """Accepts every request, submits to one judge, and reports the result paired against zero.

    Like the real eval client (#14), it first writes the candidate to the blackboard as
    the submitting worker, then the result claim as the eval service.
    """

    def __init__(
        self, *, blackboard: Blackboard, source: SignalSource, clock: Clock
    ) -> None:
        self.blackboard = blackboard
        self.source = source
        self.clock = clock
        self.pending: list[PendingJob] = []
        self.written_count = 0

    def next_number(self) -> int:
        self.written_count += 1
        return self.written_count

    def request(self, request: EvalRequest, caller: ActorId) -> EvalResponse:
        baseline = request.baseline or "baseline@0"
        manifest_data: object = yaml.safe_load(
            (request.candidate_directory / "manifest.yaml").read_text(encoding="utf-8")
        )
        manifest = CandidateManifest.model_validate(manifest_data)
        hash_value = candidate_hash(request.candidate_directory, baseline)
        candidate_files = {
            f"candidates/{hash_value}/candidate/{relative_path}": file_path.read_bytes()
            for relative_path, file_path in payload_files(request.candidate_directory)
        }
        candidate_files[f"candidates/{hash_value}/manifest.yaml"] = (
            request.candidate_directory / "manifest.yaml"
        ).read_bytes()
        require_merged(
            self.blackboard.write(caller, candidate_files, f"candidate {hash_value}")
        )
        claim = Claim(
            id=format_claim_id(EVAL_SERVICE, self.next_number()),
            type="result",
            status=INITIAL_STATUS["result"],
            approach=manifest.approach,
            author=EVAL_SERVICE,
            evidence=(EvidenceRef(candidate=f"candidates/{hash_value}/result.json"),),
            body=request.rationale,
        )
        claim_path = f"claims/{EVAL_SERVICE}/{claim.id}.md"
        require_merged(
            self.blackboard.write(
                EVAL_SERVICE,
                {claim_path: render_claim(claim).encode("utf-8")},
                f"result claim for {hash_value}",
            )
        )
        job_id = self.source.submit(
            CandidateRef(
                candidate_hash=hash_value,
                payload_directory=request.candidate_directory,
                manifest=manifest,
            ),
            caller,
            baseline,
            "dev",
        )
        self.pending.append(
            PendingJob(
                job_id=job_id,
                candidate_hash=hash_value,
                claim_id=claim.id,
                submitter=caller,
                approach=manifest.approach,
                baseline=baseline,
            )
        )
        return EvalResponse(
            request_id=job_id, claim_id=claim.id, baseline=baseline, status="queued"
        )

    def remeasure(self, request: RemeasureRequest) -> str:
        raise NotImplementedError(
            "the fake eval service does not remeasure; see W1-04 (#14)"
        )

    def tick(self) -> tuple[Event, ...]:
        events: list[Event] = []
        still_pending: list[PendingJob] = []
        for job in self.pending:
            score = self.source.poll(job.job_id)
            if score is None:
                still_pending.append(job)
            else:
                per_seed = score.per_seed or (score.value or 0.0,)
                event = Event(
                    local_id=format_event_local_id(EVAL_SERVICE, self.next_number()),
                    ts=self.clock.now_seconds(),
                    actor=EVAL_SERVICE,
                    role="eval_service",
                    type="result_ready",
                    target=job.approach,
                    data=ResultReady(
                        candidate=job.candidate_hash,
                        claim_id=job.claim_id,
                        approach=job.approach,
                        submitter=job.submitter,
                        submitter_checkpoint=None,
                        baseline=job.baseline,
                        paired=Paired(
                            mean=statistics.fmean(per_seed),
                            se=0.0,
                            n_items=len(per_seed),
                            n_reps=1,
                        ),
                        per_item=f"candidates/{job.candidate_hash}/result.json",
                        score=score.value or 0.0,
                        level=self.source.declaration.id,
                    ),
                )
                event_path = f"events/{event.local_id}.json"
                require_merged(
                    self.blackboard.write(
                        EVAL_SERVICE,
                        {event_path: event_to_json(event).encode("utf-8")},
                        f"result_ready for {job.candidate_hash}",
                    )
                )
                events.append(event)
        self.pending = still_pending
        return tuple(events)


@dataclass(kw_only=True)
class RecordingBudget:
    """A BudgetMeter that only records, until W1-10 (#20) lands."""

    records: list[tuple[str, str | None, str, float]] = field(
        default_factory=list[tuple[str, str | None, str, float]]
    )

    def record(
        self, role: Role, approach: str | None, source: str, machine_hours: float
    ) -> None:
        self.records.append((role, approach, source, machine_hours))

    def spent(self) -> BudgetView:
        total = sum(record[3] for record in self.records)
        return BudgetView(
            total_machine_hours=total,
            machine_hours_by_role={},
            machine_hours_by_approach={},
            machine_hours_by_source={},
            fraction_of_limit=0.0,
        )


class FakeRouter:
    """Never interrupts anyone."""

    def on_commit(self, merged_commit: MergedCommit) -> tuple[Interrupt, ...]:
        return ()

    def check(self, agent: ActorId) -> Interrupt | None:
        return None

    def acknowledge(self, agent: ActorId, interrupt_id: str) -> None:
        pass

    def overdue(self, now_seconds: float) -> tuple[Interrupt, ...]:
        return ()


def make_fake_components(
    *, clock: Clock, source: SignalSource, blackboard: Blackboard
) -> HerdComponents:
    return HerdComponents(
        blackboard=blackboard,
        indexer=FakeIndexer(),
        task_queue=FakeTaskQueue(),
        leases=FakeLeases(clock=clock),
        eval_service=FakeEvalService(blackboard=blackboard, source=source, clock=clock),
        router=FakeRouter(),
    )
