"""The herd's tools, as plain methods of the calling agent (v2.2 section 7, D17, D19, D31, D36).

These are the herd-side hooks (D60 layer 1):
- every call checks for a pending hard interrupt and attaches it;
- writes are first gated here (new paths, own prefix only), then by the blackboard;
- each claim is merged at once and added to the writer's lease;
- an eval request gets the sync-point diff;
- after repeated failures every reply carries the diff (S2 only).

The MCP SDK runs each tool call on a worker thread, so calls arrive concurrently.
- self.lock guards only this class's tables and is never held across I/O, so token
  lookups for MCP auth and the interrupt hook never wait behind another call;
- each agent's own lock serializes that agent's writes (claim ids) and its sync
  state; other agents never wait on it;
- recorder posts happen after every lock is released, and a recorder failure is
  logged, never returned: the write has already merged.

The MCP server in server.py only maps a bearer token to an agent and calls these.
"""

import re
import secrets
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import JsonValue

from vlah.arms.config import ArmConfig
from vlah.core.blackboard.api import Blackboard
from vlah.core.eval.api import EvalRequest, EvalService
from vlah.core.indexer.api import Hit, Indexer, WorkingSet
from vlah.core.queue.api import Leases
from vlah.core.router.api import Router
from vlah.core.types.base import FrozenModel
from vlah.core.types.claims import Claim, EvidenceRef, render_claim
from vlah.core.types.clock import Clock
from vlah.core.types.ids import ActorId, format_claim_id
from vlah.core.types.interrupts import Interrupt
from vlah.core.types.params import Params
from vlah.ports.store import ObjectStore
from vlah.tools.api import Recorder
from vlah.tools.evidence import (
    CLAIM_ID_PATTERN,
    candidate_hashes,
    evidence_pointers,
    parse_claims,
    write_event_data,
)
from vlah.tools.recording import PendingEvent, RecorderClient
from vlah.tools.scope import (
    PRIVATE_POINTER_PREFIX,
    may_read,
    resolve_candidate_directory,
    write_gate,
)

OBJECT_POINTER_PREFIX = "object://"
CLAIM_NUMBER_PATTERN = re.compile(r"c-.+-(?P<number>\d+)\.md")
RECENT_IDS_KEPT = 20
# Hits attached when a worker reaches the failure threshold (D19: "force a search").
RELATED_LIMIT = 5


class ToolReply(FrozenModel):
    """What every tool returns: its result, plus anything the herd pushes to the agent."""

    result: dict[str, JsonValue]
    interrupt: Interrupt | None = None
    diff: str | None = None


@dataclass(kw_only=True)
class AgentState:
    sandbox_root: Path
    task_id: str | None
    cursor: int = 0
    consecutive_failures: int = 0
    recent_ids: deque[str] = field(
        default_factory=lambda: deque(maxlen=RECENT_IDS_KEPT)
    )
    lock: threading.RLock = field(default_factory=threading.RLock)


def hit_json(hit: Hit) -> dict[str, JsonValue]:
    return {
        "id": hit.item_id,
        "kind": hit.kind,
        "similarity": hit.similarity,
        "summary": hit.summary,
    }


class HerdTools:
    def __init__(
        self,
        *,
        blackboard: Blackboard,
        objects: ObjectStore | None,
        indexer: Indexer,
        eval_service: EvalService,
        leases: Leases,
        router: Router,
        recorder: Recorder | None,
        arm: ArmConfig,
        params: Params,
        clock: Clock,
        run_id: str,
    ) -> None:
        self.blackboard = blackboard
        self.objects = objects
        self.indexer = indexer
        self.eval_service = eval_service
        self.leases = leases
        self.router = router
        self.recorder = recorder
        self.arm = arm
        self.params = params
        self.clock = clock
        self.run_id = run_id
        self.recording = RecorderClient(
            recorder=recorder, clock=clock, run_id=run_id, arm=arm
        )
        self.agents: dict[ActorId, AgentState] = {}
        self.agent_by_token: dict[str, ActorId] = {}
        self.token_by_agent: dict[ActorId, str] = {}
        self.claims_by_task: dict[tuple[ActorId, str | None], list[str]] = {}
        self.lock = threading.Lock()

    def register_agent(
        self, agent: ActorId, sandbox_root: Path, task_id: str | None = None
    ) -> str:
        """Give an agent a bearer token, and the sandbox its candidate paths resolve in.

        Tokens are credentials, so they come from secrets, never from the seeded
        random generator. task_id attributes claims written before the lease exists.
        """
        cursor = self.blackboard.head_sequence()
        with self.lock:
            token = self.token_by_agent.get(agent) or secrets.token_hex(16)
            self.token_by_agent[agent] = token
            self.agent_by_token[token] = agent
            self.agents[agent] = AgentState(
                sandbox_root=sandbox_root, task_id=task_id, cursor=cursor
            )
            return token

    def agent_for_token(self, token: str) -> ActorId | None:
        with self.lock:
            return self.agent_by_token.get(token)

    def state(self, agent: ActorId) -> AgentState:
        with self.lock:
            if agent not in self.agents:
                raise PermissionError(f"{agent} is not registered with the tool server")
            else:
                return self.agents[agent]

    def reply(
        self, agent: ActorId, result: dict[str, JsonValue], diff: str | None = None
    ) -> ToolReply:
        """Attach the pending hard interrupt, and the diff when failures call for it (D19)."""
        state = self.state(agent)
        with state.lock:
            if (
                diff is None
                and self.arm.failure_refresh
                and state.consecutive_failures
                >= self.params.consecutive_failures_before_refresh
            ):
                diff = self.sync_diff(agent)
            else:
                pass
        return ToolReply(result=result, interrupt=self.router.check(agent), diff=diff)

    def sync_diff(self, agent: ActorId) -> str:
        """The ranked diff since this agent last looked, truncated (D17, D18)."""
        state = self.state(agent)
        with state.lock:
            lease = self.leases.get(agent)
            working_set = WorkingSet(
                approach=lease.approach if lease is not None else "",
                target_claims=lease.target_claims if lease is not None else (),
                recent_ids=tuple(state.recent_ids),
            )
            diff = self.indexer.diff_since(
                state.cursor,
                working_set,
                agent,
                self.params.diff_truncate_tokens,
                self.arm,
            )
            state.cursor = diff.cursor
            return diff.rendered

    def repo_read(self, agent: ActorId, path: str) -> ToolReply:
        if not may_read(self.blackboard, self.arm, agent, path):
            return self.reply(
                agent, {"error": f"{path} is not readable in arm {self.arm.arm}"}
            )
        elif path.startswith(OBJECT_POINTER_PREFIX):
            if self.objects is None:
                return self.reply(agent, {"error": "this herd has no object store"})
            else:
                content = self.objects.get(path, agent)
                self.recording.post(
                    agent, [PendingEvent(type="blackboard_read", data={"ids": [path]})]
                )
                return self.reply(
                    agent, {"path": path, "content": content.decode("utf-8", "replace")}
                )
        else:
            try:
                content = self.blackboard.read(path, None)
            except FileNotFoundError:
                return self.reply(agent, {"error": f"{path} does not exist"})
            state = self.state(agent)
            with state.lock:
                state.recent_ids.append(path)
            self.recording.post(
                agent, [PendingEvent(type="blackboard_read", data={"ids": [path]})]
            )
            return self.reply(
                agent, {"path": path, "content": content.decode("utf-8", "replace")}
            )

    def write_files(
        self, agent: ActorId, files: dict[str, str]
    ) -> tuple[dict[str, JsonValue], list[PendingEvent]]:
        """With the agent's lock held: gate, merge and book a write."""
        gate_errors = [
            error
            for path in sorted(files)
            if (error := write_gate(self.arm, agent, path))
        ]
        claims, parse_errors = parse_claims(files)
        # Refused before the blackboard looks the pointer up, so the receipt never
        # tells a worker which private keys exist (D63).
        private_evidence = [
            f"{claim.id}: evidence {pointer} is private; workers never cite private data"
            for claim in claims
            for pointer in evidence_pointers(claim)
            if pointer.startswith(PRIVATE_POINTER_PREFIX)
        ]
        hashes_by_claim = {
            claim.id: candidate_hashes(self.blackboard, claim) for claim in claims
        }
        missing_hash = [
            claim.id
            for claim in claims
            if claim.type == "dead_end" and not hashes_by_claim[claim.id]
        ]
        if gate_errors or parse_errors or private_evidence:
            return {
                "error": "; ".join(gate_errors + parse_errors + private_evidence)
            }, []
        elif missing_hash:
            return {
                "error": f"dead ends {missing_hash} must cite the candidate they tested "
                "(evidence: candidate: candidates/<hash>/..., or the result claim) so "
                "nobody re-runs it"
            }, []
        else:
            receipt = self.blackboard.write(
                agent,
                {path: text.encode("utf-8") for path, text in files.items()},
                f"write by {agent}",
            )
            if receipt.status == "rejected":
                return {
                    "error": "; ".join(
                        f"{violation.path}: {violation.message}"
                        for violation in receipt.violations
                    )
                }, []
            else:
                self.book_claims(agent, claims)
                events = [
                    PendingEvent(
                        type="blackboard_write",
                        data=write_event_data(claim, hashes_by_claim[claim.id]),
                    )
                    for claim in claims
                ]
                return {
                    "written": [path for path in sorted(files)],
                    "sequence_numbers": [number for number in receipt.sequence_numbers],
                }, events

    def book_claims(self, agent: ActorId, claims: list[Claim]) -> None:
        """Add new claims to the lease (D55) and to the task's record (D4)."""
        if not claims:
            return
        else:
            lease = self.leases.get(agent)
            if lease is not None:
                self.leases.add_targets(
                    agent,
                    tuple(claim.id for claim in claims),
                    tuple(
                        dependency
                        for claim in claims
                        for dependency in claim.depends_on
                    ),
                )
                task_id = lease.task_id
            else:
                task_id = self.state(agent).task_id
            with self.lock:
                self.claims_by_task.setdefault((agent, task_id), []).extend(
                    claim.id for claim in claims
                )

    def repo_write(self, agent: ActorId, files: dict[str, str]) -> ToolReply:
        state = self.state(agent)
        with state.lock:
            result, events = self.write_files(agent, files)
        self.recording.post(agent, events)
        return self.reply(agent, result)

    def next_claim_id(self, agent: ActorId) -> str:
        numbers = [
            int(match.group("number"))
            for path in self.blackboard.list_paths(f"claims/{agent}/", None)
            if (match := CLAIM_NUMBER_PATTERN.fullmatch(Path(path).name)) is not None
        ]
        return format_claim_id(agent, max(numbers, default=0) + 1)

    def surprise(
        self, agent: ActorId, note: str, pointer: str, approach: str
    ) -> ToolReply:
        """Record something unexpected without needing a conclusion (D8)."""
        state = self.state(agent)
        with state.lock:
            claim = Claim(
                id=self.next_claim_id(agent),
                type="question",
                status="open",
                approach=approach,
                author=agent,
                evidence=(EvidenceRef(path=pointer),),
                surprise=True,
                body=note,
            )
            result, events = self.write_files(
                agent, {f"claims/{agent}/{claim.id}.md": render_claim(claim)}
            )
        self.recording.post(agent, events)
        return self.reply(agent, result)

    def search(self, agent: ActorId, query: str, limit: int) -> ToolReply:
        hits = self.indexer.search(query, limit, agent, self.arm)
        state = self.state(agent)
        with state.lock:
            state.recent_ids.extend(hit.item_id for hit in hits)
        self.recording.post(
            agent,
            [
                PendingEvent(
                    type="blackboard_read", data={"ids": [hit.item_id for hit in hits]}
                )
            ],
        )
        return self.reply(agent, {"hits": [hit_json(hit) for hit in hits]})

    def eval_request(
        self,
        agent: ActorId,
        candidate_dir: str,
        predictions: dict[str, float],
        rationale: str,
        parents: tuple[str, ...],
        confirm_token: str | None,
        baseline: str | None,
    ) -> ToolReply:
        directory = resolve_candidate_directory(
            self.state(agent).sandbox_root, candidate_dir
        )
        if isinstance(directory, str):
            return self.reply(agent, {"error": directory})
        else:
            diff = (
                self.sync_diff(agent)
                if self.arm.diff_since in ("on", "results_only")
                else None
            )
            response = self.eval_service.request(
                EvalRequest(
                    candidate_directory=directory,
                    parents=parents,
                    predictions=predictions,
                    rationale=rationale,
                    confirm_token=confirm_token,
                    baseline=baseline,
                ),
                agent,
            )
            state = self.state(agent)
            with state.lock:
                if response.status in ("gate_failed", "dedup_hit"):
                    state.consecutive_failures += 1
                elif response.status == "queued":
                    state.consecutive_failures = 0
                else:
                    pass
                reached_threshold = (
                    state.consecutive_failures
                    == self.params.consecutive_failures_before_refresh
                )
            result: dict[str, JsonValue] = response.model_dump(mode="json")
            if self.arm.failure_refresh and reached_threshold:
                related = self.indexer.search(rationale, RELATED_LIMIT, agent, self.arm)
                self.recording.post(
                    agent,
                    [
                        PendingEvent(
                            type="blackboard_read",
                            data={"ids": [hit.item_id for hit in related]},
                        )
                    ],
                )
                result["related"] = [hit_json(hit) for hit in related]
            else:
                pass
            return self.reply(agent, result, diff=diff)

    def hook_check(self, agent: ActorId) -> Interrupt | None:
        return self.router.check(agent)

    def hook_ack(self, agent: ActorId, interrupt_id: str) -> bool:
        """Acknowledge the interrupt pending for this agent; any other id is refused."""
        pending = self.router.check(agent)
        if pending is None or pending.id != interrupt_id:
            return False
        else:
            self.router.acknowledge(agent, interrupt_id)
            return True

    def claims_written(self, agent: ActorId, task_id: str) -> tuple[str, ...]:
        """Claims booked here, plus those the lease kept for the task (D4).

        The lease outlives this object, so a tool server rebuilt over the same leases
        still reports them. Only ids the agent authored count: the lease also carries
        the task's refs, which name other writers' claims and candidates.
        """
        with self.lock:
            booked = list(self.claims_by_task.get((agent, task_id), []))
        lease = self.leases.get(agent)
        if lease is not None and lease.task_id == task_id:
            booked.extend(
                claim_id
                for claim_id in lease.target_claims
                if (match := CLAIM_ID_PATTERN.fullmatch(claim_id)) is not None
                and match.group("author") == agent
            )
        else:
            pass
        return tuple(dict.fromkeys(booked))
