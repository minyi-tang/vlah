"""Public replay observations, with separate worker, session and artifact identities."""

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml
from pydantic import JsonValue

from vlah.core.blackboard.api import Blackboard, MergedCommit
from vlah.core.indexer.api import Indexer
from vlah.core.types.base import FrozenModel
from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.candidates import CandidateManifest
from vlah.core.types.claims import Claim, parse_claim
from vlah.core.types.clock import Clock
from vlah.core.types.events import (
    BaselineAdvanced,
    BaselineReverted,
    Event,
    ResultReady,
    event_from_json,
    event_to_json,
)
from vlah.core.types.tasks import Task
from vlah.runtime.api import Report, RunStatus


@dataclass
class TraceState:
    at: float
    status: str
    sequence: int | None


@dataclass
class TraceNode:
    id: str
    kind: str
    title: str
    detail: str
    actor: str
    approach: str
    at: float
    order: int
    source: str
    states: list[TraceState] = field(default_factory=list)


@dataclass
class TraceEdge:
    source: str
    target: str
    relation: str
    at: float
    evidence: str


@dataclass
class TraceEvent:
    at: float
    sequence: int | None
    kind: str
    actor: str
    target: str
    detail: str
    canonical: dict[str, JsonValue] | None = None


@dataclass
class RunObservation:
    at: float
    status: dict[str, JsonValue]


@dataclass
class TraceSession:
    id: str
    actor: str
    task: str
    kind: str
    goal: str
    start: float
    end: float | None = None


class CandidateLineage(FrozenModel):
    parents: tuple[str, ...]


def reference_id(reference: str) -> str:
    if reference.startswith(("c-", "baseline@", "task:", "candidate:")):
        return reference
    else:
        return f"candidate:{reference}"


@dataclass
class TraceRecorder:
    blackboard: Blackboard
    indexer: Indexer
    clock: Clock
    nodes: dict[str, TraceNode] = field(default_factory=dict)
    edges: dict[tuple[str, str, str], TraceEdge] = field(default_factory=dict)
    events: list[TraceEvent] = field(default_factory=list)
    sessions: list[TraceSession] = field(default_factory=list)
    active: dict[str, TraceSession] = field(default_factory=dict)
    provenance: str = (
        "Public snapshot and subsequent VLAH merge observations. Session state is unavailable unless explicitly observed."
    )
    name: str = "A shared search"
    origin_seconds: float = field(init=False)
    output_path: Path | None = None
    worker_ids: tuple[str, ...] = ()
    compose_actor: str = ""
    subscription: int | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)
    seen_events: set[str] = field(default_factory=set)
    run_states: list[RunObservation] = field(default_factory=list)
    report_data: dict[str, JsonValue] | None = None
    report_at: float | None = None
    report_error: str | None = None

    def __post_init__(self) -> None:
        self.origin_seconds = self.clock.now_seconds()

    def now_seconds(self) -> float:
        return self.clock.now_seconds() - self.origin_seconds

    def record_status(self, status: RunStatus) -> None:
        with self.lock:
            payload: dict[str, JsonValue] = asdict(status)
            if not self.run_states or self.run_states[-1].status != payload:
                self.run_states.append(RunObservation(self.now_seconds(), payload))
                self.publish()
            else:
                pass

    def record_report(self, report: Report) -> None:
        with self.lock:
            self.report_data = {
                "run_id": report.run_id,
                "arm": report.arm,
                "deliverable": report.deliverable.model_dump(mode="json"),
                "baselines": [
                    baseline.model_dump(mode="json") for baseline in report.baselines
                ],
                "metrics": asdict(report.metrics),
            }
            self.report_error = None
            self.report_at = self.now_seconds()
            self.publish()

    def start(
        self, output_path: Path, workers: tuple[str, ...], compose_actor: str
    ) -> None:
        """Attach before workers start; reject a concurrently changing initial snapshot."""
        with self.lock:
            if self.subscription is not None:
                raise RuntimeError("This recorder is already attached.")
            else:
                head = self.blackboard.head_sequence()
                self.subscription = self.blackboard.subscribe(self)
                all_paths = self.blackboard.list_paths("", None)
                paths = tuple(
                    path
                    for path in all_paths
                    if (path.startswith("claims/") and path.endswith(".md"))
                    or (path.startswith("baseline/") and path.endswith(".json"))
                    or (
                        path.startswith("candidates/")
                        and path.endswith(("/manifest.yaml", "/lineage.json"))
                    )
                )
                claims = tuple(
                    parse_claim(self.blackboard.read(path, None).decode())
                    for path in paths
                    if path.startswith("claims/")
                )
                events = tuple(
                    event_from_json(
                        self.blackboard.read(
                            self.blackboard.read(
                                f"events/_seq/{sequence}", None
                            ).decode(),
                            None,
                        ).decode()
                    ).model_copy(update={"seq": sequence})
                    for sequence in range(1, head + 1)
                )
                self.observe(paths, claims, events)
                if (
                    self.blackboard.head_sequence() != head
                    or self.blackboard.list_paths("", None) != all_paths
                ):
                    self.blackboard.unsubscribe(self.subscription)
                    self.subscription = None
                    raise RuntimeError(
                        "Blackboard changed during attachment; attach before starting workers."
                    )
                else:
                    self.output_path = output_path
                    self.worker_ids = workers
                    self.compose_actor = compose_actor
                    self.publish()

    def stop(self) -> None:
        with self.lock:
            if self.subscription is not None:
                self.blackboard.unsubscribe(self.subscription)
                self.subscription = None
            else:
                pass
            self.publish()
            self.output_path = None

    def publish(self) -> None:
        if self.output_path is None:
            return
        else:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.output_path.with_name(f".{self.output_path.name}.tmp")
            temporary.write_text(
                self.export(
                    self.worker_ids, max(0.001, self.now_seconds()), self.compose_actor
                )
                + "\n"
            )
            temporary.replace(self.output_path)

    def add_node(
        self,
        identity: str,
        kind: str,
        title: str,
        detail: str,
        actor: str,
        approach: str,
        source: str,
        status: str,
    ) -> None:
        if identity not in self.nodes:
            self.nodes[identity] = TraceNode(
                identity,
                kind,
                title,
                detail,
                actor,
                approach,
                self.now_seconds(),
                len(self.nodes),
                source,
                [TraceState(self.now_seconds(), status, None)],
            )
        else:
            pass

    def edge(self, source: str, target: str, relation: str, evidence: str) -> None:
        self.edges.setdefault(
            (source, target, relation),
            TraceEdge(source, target, relation, self.now_seconds(), evidence),
        )

    def state(self, identity: str, status: str, sequence: int | None) -> None:
        node = self.nodes.get(identity)
        if node is not None and node.states[-1].status != status:
            node.states.append(TraceState(self.now_seconds(), status, sequence))
        else:
            pass

    def begin(self, actor: str, task: Task, session_id: str) -> None:
        with self.lock:
            identity = f"task:{task.id}"
            self.add_node(
                identity,
                "task",
                task.kind.replace("_", " "),
                task.goal,
                actor,
                task.approach,
                f"Task {task.id}; pool={task.pool}",
                "running",
            )
            self.state(identity, "running", None)
            for reference in task.refs:
                source = reference_id(reference)
                self.edge(
                    source, identity, "task reference", f"Task.refs ({task.kind})"
                )
            session = TraceSession(
                session_id, actor, identity, task.kind, task.goal, self.now_seconds()
            )
            self.sessions.append(session)
            self.active[actor] = session
            self.publish()

    def finish(self, actor: str) -> None:
        with self.lock:
            session = self.active.pop(actor)
            session.end = self.now_seconds()
            self.state(session.task, "session ended", None)
            self.publish()

    def output(self, actor: str, identity: str) -> None:
        session = self.active.get(actor)
        if session is not None:
            self.edge(
                session.task, identity, "produced", f"Observed session {session.id}"
            )
        else:
            pass

    def __call__(self, merged_commit: MergedCommit) -> None:
        with self.lock:
            self.observe(
                merged_commit.paths, merged_commit.claims, merged_commit.events
            )
            self.publish()

    def observe(
        self,
        paths: tuple[str, ...],
        claims: tuple[Claim, ...],
        incoming_events: tuple[Event, ...],
    ) -> None:
        for claim in claims:
            path = f"claims/{claim.author}/{claim.id}.md"
            self.add_node(
                claim.id,
                "claim",
                claim.type.replace("_", " "),
                claim.body,
                claim.author,
                claim.approach,
                path,
                claim.status,
            )
            self.output(claim.author, claim.id)
            for dependency in claim.depends_on:
                self.edge(dependency, claim.id, "depends on", f"{path}: depends_on")
            for evidence in claim.evidence:
                if evidence.claim:
                    self.edge(
                        evidence.claim, claim.id, "cites", f"{path}: evidence.claim"
                    )
                elif evidence.candidate:
                    candidate = evidence.candidate.split("/")[1]
                    self.edge(
                        f"candidate:{candidate}",
                        claim.id,
                        "evidence",
                        f"{path}: evidence.candidate",
                    )
                else:
                    pass
        for path in sorted(
            paths, key=lambda path: (path.startswith("baseline/"), path)
        ):
            if path.startswith("candidates/") and path.endswith("/manifest.yaml"):
                manifest = CandidateManifest.model_validate(
                    yaml.safe_load(self.blackboard.read(path, None))
                )
                identity = f"candidate:{path.split('/')[1]}"
                self.add_node(
                    identity,
                    "candidate",
                    f"Candidate {path.split('/')[1][:8]}",
                    manifest.note or f"Candidate against {manifest.target}.",
                    manifest.author,
                    manifest.approach,
                    path,
                    "recorded",
                )
                self.output(manifest.author, identity)
                self.edge(
                    manifest.target, identity, "targets baseline", f"{path}: target"
                )
                for parent in manifest.parents:
                    self.edge(
                        reference_id(parent),
                        identity,
                        "candidate parent",
                        f"{path}: parents",
                    )
            elif path.startswith("candidates/") and path.endswith("/lineage.json"):
                lineage = CandidateLineage.model_validate_json(
                    self.blackboard.read(path, None)
                )
                for parent in lineage.parents:
                    self.edge(
                        reference_id(parent),
                        f"candidate:{path.split('/')[1]}",
                        "candidate parent",
                        f"{path}: parents",
                    )
            elif path.startswith("baseline/") and path.endswith(".json"):
                baseline = BaselineRecord.model_validate_json(
                    self.blackboard.read(path, None)
                )
                identity = f"baseline@{baseline.k}"
                self.add_node(
                    identity,
                    "baseline",
                    identity,
                    f"{len(baseline.candidates)} accepted candidates; n={len(baseline.per_seed)}.",
                    "integrator",
                    "shared",
                    path,
                    "guard pending" if baseline.guard_pending else "accepted",
                )
                if baseline.parent is not None:
                    self.edge(
                        f"baseline@{baseline.parent}",
                        identity,
                        "baseline parent",
                        f"{path}: parent",
                    )
                else:
                    pass
                if baseline.added:
                    self.edge(
                        f"candidate:{baseline.added}",
                        identity,
                        "integrated",
                        f"{path}: added",
                    )
                    self.state(f"candidate:{baseline.added}", "integrated", None)
                else:
                    pass
            else:
                pass
        for event in incoming_events:
            if event.local_id in self.seen_events:
                continue
            else:
                self.seen_events.add(event.local_id)
            detail = (
                event.data.model_dump_json()
                if event.data is not None
                else event.reason or event.type.replace("_", " ")
            )
            target = event.target or ""
            if isinstance(event.data, ResultReady):
                detail = event.data.model_dump_json()
                target = f"candidate:{event.data.candidate}"
                self.state(target, "result ready", event.seq)
            elif isinstance(event.data, BaselineAdvanced):
                target = f"candidate:{event.data.candidate}"
                detail = event.data.model_dump_json()
                self.state(target, "integrated", event.seq)
            elif isinstance(event.data, BaselineReverted):
                target = f"baseline@{event.data.from_baseline}"
                detail = event.data.model_dump_json()
                self.state(target, "reverted", event.seq)
                for candidate in event.data.removed:
                    self.state(
                        f"candidate:{candidate}", "removed by rollback", event.seq
                    )
            elif event.type == "integration_failed" and event.target:
                target = f"candidate:{event.target}"
                self.state(target, "integration failed", event.seq)
            else:
                pass
            self.events.append(
                TraceEvent(
                    self.now_seconds(),
                    event.seq,
                    event.type,
                    event.actor,
                    target,
                    detail,
                    json.loads(event_to_json(event)),
                )
            )
        for node in self.nodes.values():
            if node.kind == "claim":
                current = self.indexer.claim_state(node.id)
                self.state(
                    node.id,
                    (
                        "refuted (dependency)"
                        if current.is_cascaded_refuted
                        else current.status
                    ),
                    incoming_events[-1].seq if incoming_events else None,
                )
            else:
                pass

    def export(
        self, workers: tuple[str, ...], duration_seconds: float, compose_actor: str
    ) -> str:
        return json.dumps(
            {
                "version": 1,
                "name": self.name,
                "provenance": self.provenance,
                "duration_seconds": duration_seconds,
                "workers": workers,
                "compose_actor": compose_actor,
                "nodes": [asdict(node) for node in self.nodes.values()],
                "edges": [asdict(edge) for edge in self.edges.values()],
                "events": [asdict(event) for event in self.events],
                "sessions": [asdict(session) for session in self.sessions],
                "run_states": [asdict(state) for state in self.run_states],
                "report": self.report_data,
                "report_at": self.report_at,
                "report_error": self.report_error,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
