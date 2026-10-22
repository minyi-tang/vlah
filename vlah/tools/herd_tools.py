"""The herd's tools, as plain methods of the calling agent (v2.2 section 7, D17, D19, D31, D36).

These are the herd-side hooks (D60 layer 1):
- every call checks for a pending hard interrupt and attaches it;
- writes are first gated here (new paths, own prefix only), then by the blackboard;
- each claim is merged at once and added to the writer's lease;
- an eval request gets the sync-point diff;
- after repeated failures every reply carries the diff (S2 only).

The MCP SDK runs each tool call on a worker thread, so calls arrive concurrently.
One lock guards this class's own state. It is released while the eval service
runs its gates, so a slow gate never holds up another worker's calls, and the
interrupt hook takes no lock at all.

The MCP server in server.py only maps a bearer token to an agent and calls these.
"""

import re
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from random import Random

import yaml
from pydantic import JsonValue

from vlah.arms.config import ArmConfig
from vlah.core.blackboard.api import Blackboard
from vlah.core.eval.api import EvalRequest, EvalService
from vlah.core.indexer.api import Hit, Indexer, WorkingSet
from vlah.core.queue.api import Leases
from vlah.core.router.api import Router
from vlah.core.types.base import FrozenModel
from vlah.core.types.candidates import CandidateManifest
from vlah.core.types.claims import Claim, EvidenceRef, parse_claim, render_claim
from vlah.core.types.clock import Clock
from vlah.core.types.ids import ActorId, format_claim_id
from vlah.core.types.interrupts import Interrupt
from vlah.core.types.params import Params
from vlah.ports.store import ObjectStore
from vlah.tools.api import Recorder, RecorderEvent

PRIVATE_POINTER_PREFIX = "private://"
OBJECT_POINTER_PREFIX = "object://"
OWN_PREFIXES = ("claims", "events", "traces", "checkpoints")
MAP_SCRIPT_PATTERN = re.compile(r"map/measurements/[^/]+/[^/]+")
CANDIDATE_POINTER_PATTERN = re.compile(r"candidates/(?P<hash>[0-9a-f]+)/")
CLAIM_NUMBER_PATTERN = re.compile(r"c-.+-(?P<number>\d+)\.md")
RECENT_IDS_KEPT = 20
# Hits attached when a worker reaches the failure threshold (D19: "force a search").
RELATED_LIMIT = 5
# A claim that records composing two candidates carries this tag (TB section 11.5,
# conflict handling).
COMPOSE_TAG = "compose"
RECORDER_KIND_BY_CLAIM_TYPE = {"dead_end": "dead_end", "insight": "insight"}


class ToolReply(FrozenModel):
    """What every tool returns: its result, plus anything the herd pushes to the agent."""

    result: dict[str, JsonValue]
    interrupt: Interrupt | None = None
    diff: str | None = None


@dataclass(kw_only=True)
class AgentState:
    sandbox_root: Path
    cursor: int = 0
    consecutive_failures: int = 0
    recent_ids: deque[str] = field(
        default_factory=lambda: deque(maxlen=RECENT_IDS_KEPT)
    )


def hit_json(hit: Hit) -> dict[str, JsonValue]:
    return {
        "id": hit.item_id,
        "kind": hit.kind,
        "similarity": hit.similarity,
        "summary": hit.summary,
    }


def candidate_hashes(claim: Claim) -> tuple[str, ...]:
    pointers = [
        pointer
        for evidence in claim.evidence
        for pointer in (evidence.candidate, evidence.path, evidence.trace)
        if pointer is not None
    ]
    return tuple(
        dict.fromkeys(
            match.group("hash")
            for pointer in pointers
            if (match := CANDIDATE_POINTER_PATTERN.match(pointer)) is not None
        )
    )


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
        random: Random,
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
        self.random = random
        self.agents: dict[ActorId, AgentState] = {}
        self.agent_by_token: dict[str, ActorId] = {}
        self.token_by_agent: dict[ActorId, str] = {}
        self.claims_by_task: dict[tuple[ActorId, str | None], list[str]] = {}
        self.lock = threading.RLock()

    def register_agent(self, agent: ActorId, sandbox_root: Path) -> str:
        """Give an agent a bearer token, and the sandbox its candidate paths resolve in."""
        with self.lock:
            token = (
                self.token_by_agent.get(agent) or f"{self.random.getrandbits(128):032x}"
            )
            self.token_by_agent[agent] = token
            self.agent_by_token[token] = agent
            self.agents[agent] = AgentState(
                sandbox_root=sandbox_root, cursor=self.blackboard.head_sequence()
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
        with self.lock:
            state = self.state(agent)
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
        with self.lock:
            state = self.state(agent)
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

    def record(
        self, agent: ActorId, event_type: str, data: dict[str, JsonValue]
    ) -> None:
        if self.recorder is not None:
            self.recorder.post(
                (
                    RecorderEvent.model_validate(
                        {
                            "ts": self.clock.now_seconds(),
                            "run_id": self.run_id,
                            "arm": self.arm.arm,
                            "actor": agent,
                            "type": event_type,
                            "data": data,
                        }
                    ),
                )
            )
        else:
            pass

    def is_own_path(self, agent: ActorId, path: str) -> bool:
        segments = path.split("/")
        return (
            len(segments) >= 3 and segments[0] in OWN_PREFIXES and segments[1] == agent
        )

    def submitted_by(self, agent: ActorId, path: str) -> bool:
        """Whether path is inside a candidate this agent submitted (B1 sees its own results)."""
        match = CANDIDATE_POINTER_PATTERN.match(path)
        if match is None:
            return False
        else:
            manifest_path = f"candidates/{match.group('hash')}/manifest.yaml"
            if manifest_path not in self.blackboard.list_paths(manifest_path, None):
                return False
            else:
                manifest_data: object = yaml.safe_load(
                    self.blackboard.read(manifest_path, None)
                )
                return CandidateManifest.model_validate(manifest_data).author == agent

    def may_read(self, agent: ActorId, path: str) -> bool:
        """repo_read scope by arm (D58); private data is never readable by a worker (D63)."""
        scope = self.arm.repo_read_scope
        is_shared_problem_file = path == "spec.md" or path.startswith("approaches/")
        if path.startswith(PRIVATE_POINTER_PREFIX):
            return False
        elif scope == "all":
            return True
        elif is_shared_problem_file or self.is_own_path(agent, path):
            return True
        elif scope == "spec_approaches_own_candidates":
            return path.startswith("candidates/") or path.startswith(
                "events/eval_service/"
            )
        else:
            return self.submitted_by(agent, path)

    def repo_read(self, agent: ActorId, path: str) -> ToolReply:
        if not self.may_read(agent, path):
            return self.reply(
                agent, {"error": f"{path} is not readable in arm {self.arm.arm}"}
            )
        elif path.startswith(OBJECT_POINTER_PREFIX):
            if self.objects is None:
                return self.reply(agent, {"error": "this herd has no object store"})
            else:
                content = self.objects.get(path, agent)
                self.record(agent, "blackboard_read", {"ids": [path]})
                return self.reply(
                    agent, {"path": path, "content": content.decode("utf-8", "replace")}
                )
        else:
            try:
                content = self.blackboard.read(path, None)
            except FileNotFoundError:
                return self.reply(agent, {"error": f"{path} does not exist"})
            with self.lock:
                self.state(agent).recent_ids.append(path)
            self.record(agent, "blackboard_read", {"ids": [path]})
            return self.reply(
                agent, {"path": path, "content": content.decode("utf-8", "replace")}
            )

    def write_gate(self, agent: ActorId, path: str) -> str | None:
        """The tool server's own gate, before the blackboard's (D31)."""
        if self.is_own_path(agent, path):
            return None
        elif MAP_SCRIPT_PATTERN.fullmatch(path) and self.arm.map == "full":
            return None
        else:
            return f"{path}: workers create files only under their own prefix (claims/{agent}/ ...)"

    def repo_write(self, agent: ActorId, files: dict[str, str]) -> ToolReply:
        """Gate, merge and book a write as one step, so claim ids and leases stay consistent."""
        with self.lock:
            return self.write_locked(agent, files)

    def write_locked(self, agent: ActorId, files: dict[str, str]) -> ToolReply:
        gate_errors = [
            error for path in sorted(files) if (error := self.write_gate(agent, path))
        ]
        claims = {
            path: parse_claim(text)
            for path, text in files.items()
            if path.startswith("claims/")
        }
        missing_hash = [
            claim.id
            for claim in claims.values()
            if claim.type == "dead_end" and not candidate_hashes(claim)
        ]
        if gate_errors:
            return self.reply(agent, {"error": "; ".join(gate_errors)})
        elif missing_hash:
            return self.reply(
                agent,
                {
                    "error": f"dead ends {missing_hash} must cite the candidate they tested "
                    "(evidence: candidate: candidates/<hash>/...) so nobody re-runs it"
                },
            )
        else:
            receipt = self.blackboard.write(
                agent,
                {path: text.encode("utf-8") for path, text in files.items()},
                f"write by {agent}",
            )
            if receipt.status == "rejected":
                return self.reply(
                    agent,
                    {
                        "error": "; ".join(
                            f"{v.path}: {v.message}" for v in receipt.violations
                        )
                    },
                )
            else:
                self.after_claims_written(agent, tuple(claims.values()))
                return self.reply(
                    agent,
                    {
                        "written": [path for path in sorted(files)],
                        "sequence_numbers": [
                            number for number in receipt.sequence_numbers
                        ],
                    },
                )

    def after_claims_written(self, agent: ActorId, claims: tuple[Claim, ...]) -> None:
        if not claims:
            return
        else:
            lease = self.leases.get(agent)
            task_id = lease.task_id if lease is not None else None
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
            else:
                pass
            self.claims_by_task.setdefault((agent, task_id), []).extend(
                claim.id for claim in claims
            )
            for claim in claims:
                hashes = candidate_hashes(claim)
                if COMPOSE_TAG in claim.tags:
                    kind = "compose"
                else:
                    kind = RECORDER_KIND_BY_CLAIM_TYPE.get(claim.type, "claim")
                data: dict[str, JsonValue] = {
                    "kind": kind,
                    "id": claim.id,
                    "refs": list(hashes),
                }
                if claim.type == "dead_end":
                    data["hash"] = hashes[0]
                else:
                    pass
                self.record(agent, "blackboard_write", data)

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
        with self.lock:
            return self.surprise_locked(agent, note, pointer, approach)

    def surprise_locked(
        self, agent: ActorId, note: str, pointer: str, approach: str
    ) -> ToolReply:
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
        return self.write_locked(
            agent, {f"claims/{agent}/{claim.id}.md": render_claim(claim)}
        )

    def search(self, agent: ActorId, query: str, limit: int) -> ToolReply:
        hits = self.indexer.search(query, limit, agent, self.arm)
        with self.lock:
            self.state(agent).recent_ids.extend(hit.item_id for hit in hits)
        self.record(agent, "blackboard_read", {"ids": [hit.item_id for hit in hits]})
        return self.reply(agent, {"hits": [hit_json(hit) for hit in hits]})

    def resolve_candidate_directory(
        self, agent: ActorId, candidate_dir: str
    ) -> Path | None:
        root = self.state(agent).sandbox_root.resolve()
        directory = (root / candidate_dir).resolve()
        if directory.is_relative_to(root) and directory.is_dir():
            return directory
        else:
            return None

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
        directory = self.resolve_candidate_directory(agent, candidate_dir)
        if directory is None:
            return self.reply(
                agent, {"error": f"{candidate_dir} is not a directory in your sandbox"}
            )
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
            with self.lock:
                state = self.state(agent)
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
                self.record(
                    agent, "blackboard_read", {"ids": [hit.item_id for hit in related]}
                )
                result["related"] = [hit_json(hit) for hit in related]
            else:
                pass
            return self.reply(agent, result, diff=diff)

    def hook_check(self, agent: ActorId) -> Interrupt | None:
        return self.router.check(agent)

    def hook_ack(self, agent: ActorId, interrupt_id: str) -> None:
        self.router.acknowledge(agent, interrupt_id)

    def claims_written(self, agent: ActorId, task_id: str) -> tuple[str, ...]:
        with self.lock:
            return tuple(self.claims_by_task.get((agent, task_id), []))
