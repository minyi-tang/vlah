"""What the wrapper writes as the actor integrator: events, baseline records, tasks (D74).

Every core decision becomes one herd event with the payload from
vlah.core.types.events, written to the run's blackboard; B1 workers' mirrors never
carry baseline/ or events/integrator/, so no separate sink is needed. Each completed
version becomes baseline/<k>.json, written once. A failed integration gives its
author a follow-up task in the arm's queue, a conflict gives the compose pool a
task in the compose queue, and a reverted candidate's result claim is quarantined
with a claim_status event.

Both tasks are queued after the integration_failed event and named by its number,
which never repeats: a restarted Integrator never reuses a task id, and a crash
between the event and the task loses the task instead of queueing it twice.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from vlah.core.blackboard.api import Blackboard
from vlah.core.integrator.core_adapter import Decision, Version
from vlah.core.queue.api import TaskQueue
from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.clock import Clock
from vlah.core.types.events import Event, event_to_json
from vlah.core.types.ids import ActorId, format_event_local_id
from vlah.core.types.tasks import Task

logger = logging.getLogger(__name__)

INTEGRATOR = ActorId("integrator")
ROLE = "integrator"
TASK_ID_PREFIX = "t-integrator"
FOLLOW_UP_PRIORITY = 1
RUN_ENDED = "run_ended"
SHORT_HASH = 12
# The decision fields that make up each event payload (vlah.core.types.events).
PAYLOAD_FIELDS: dict[str, tuple[str, ...]] = {
    "baseline_advanced": (
        "baseline",
        "parent_baseline",
        "candidate",
        "files_changed",
        "paired",
        "guard_pending",
    ),
    "integration_failed": ("candidate", "baseline", "paired", "reason"),
    "interaction_found": (
        "candidate",
        "baseline",
        "earlier",
        "remeasured",
        "overlaps_with",
    ),
    "baseline_reverted": ("from_baseline", "new_baseline", "removed", "reason"),
    "audit_checked": ("segment", "daily", "audit", "threshold", "flagged"),
}
LOGGED_ONLY = frozenset({"stale_target", "integrator_frozen"})


def strings_of(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    else:
        return ()


@dataclass(frozen=True, kw_only=True)
class CandidateFacts:
    """What the wrapper remembers about a verified candidate, by its hash."""

    claim_id: str
    approach: str
    is_compose: bool


@dataclass(kw_only=True)
class IntegratorRecords:
    sink: Blackboard
    task_queue: TaskQueue
    compose_queue: TaskQueue
    clock: Clock
    top_level: str
    # True when guard gates exist: every merge waits for its batch (D74).
    guard_pending_on_advance: bool
    facts: dict[str, CandidateFacts] = field(default_factory=dict[str, CandidateFacts])
    event_count: int = 0
    # A conflict's compose_task decision, by candidate, until its integration_failed.
    compose_decisions: dict[str, Decision] = field(default_factory=dict[str, Decision])
    events_written: list[Event] = field(default_factory=list[Event])

    def start(self) -> None:
        """Continue the event numbering after a restart, from the highest number."""
        self.event_count = max(
            (
                int(PurePosixPath(path).stem)
                for path in self.sink.list_paths(f"events/{INTEGRATOR}/", None)
            ),
            default=0,
        )

    def remember(
        self, candidate: str, claim_id: str, approach: str, is_compose: bool
    ) -> None:
        self.facts[candidate] = CandidateFacts(
            claim_id=claim_id, approach=approach, is_compose=is_compose
        )

    def write_event(
        self,
        event_type: str,
        *,
        target: str | None = None,
        data: dict[str, object] | None = None,
        from_status: str | None = None,
        to_status: str | None = None,
        reason: str | None = None,
        evidence: tuple[str, ...] = (),
    ) -> Event:
        self.event_count += 1
        local_id = format_event_local_id(INTEGRATOR, self.event_count)
        event = Event.model_validate(
            {
                "local_id": local_id,
                "ts": self.clock.now_seconds(),
                "actor": INTEGRATOR,
                "role": ROLE,
                "type": event_type,
                "target": target,
                "from": from_status,
                "to": to_status,
                "reason": reason,
                "evidence": list(evidence),
                "data": data,
            }
        )
        receipt = self.sink.write(
            INTEGRATOR,
            {f"events/{local_id}.json": event_to_json(event).encode("utf-8")},
            f"{event_type} by the Integrator",
        )
        if receipt.status == "rejected":
            logger.error(f"the blackboard rejected {event_type}: {receipt.violations}")
        else:
            self.events_written.append(event)
        return event

    def write_baseline(
        self, version: Version, guard_pending: bool, gates: dict[str, bool]
    ) -> None:
        """baseline/<k>.json, once; a version's record never changes afterwards."""
        path = f"baseline/{version.k}.json"
        if self.sink.list_paths(path, None):
            return
        else:
            record = BaselineRecord.model_validate(
                {**version.record(), "guard_pending": guard_pending, "gates": gates}
            )
            receipt = self.sink.write(
                INTEGRATOR,
                {path: record.model_dump_json(indent=2).encode("utf-8")},
                f"baseline@{version.k}",
            )
            if receipt.status == "rejected":
                logger.error(f"the blackboard rejected {path}: {receipt.violations}")
            else:
                pass

    def payload(self, decision: Decision, kind: str) -> dict[str, object]:
        fields = {name: decision.get(name) for name in PAYLOAD_FIELDS[kind]}
        if kind == "baseline_advanced":
            fields["guard_pending"] = self.guard_pending_on_advance
        else:
            pass
        return fields

    def on_failed(self, decision: Decision, is_closing: bool) -> None:
        """integration_failed, a conflict's compose task, and a follow-up task for the
        author while the run goes on."""
        candidate = str(decision["candidate"])
        reason = str(decision["reason"])
        self.write_event(
            "integration_failed",
            target=candidate,
            reason=reason,
            data=self.payload(decision, "integration_failed"),
        )
        task_id = f"{TASK_ID_PREFIX}-{self.event_count:05d}"
        compose_decision = self.compose_decisions.pop(candidate, None)
        if compose_decision is not None:
            self.queue_compose(compose_decision, f"{task_id}-compose")
        else:
            pass
        facts = self.facts[candidate]
        actor = decision.get("actor")
        if is_closing or reason == RUN_ENDED or not isinstance(actor, str):
            logger.info(f"no follow-up for {candidate[:SHORT_HASH]}: the run is ending")
        elif facts.is_compose:
            logger.info(
                f"no follow-up for composed {candidate[:SHORT_HASH]}: the compose "
                "agent has no line of work to continue"
            )
        else:
            note = str(decision.get("note") or "")
            self.task_queue.put(
                Task(
                    id=task_id,
                    approach=facts.approach,
                    kind="integration_failed_followup",
                    goal=(
                        f"Your candidate {candidate[:SHORT_HASH]} was not merged into "
                        f"baseline@{decision.get('baseline')}: {reason}. {note} "
                        "Decide whether to rework it on the current baseline or "
                        "record a dead end."
                    ).replace("  ", " "),
                    refs=(facts.claim_id, candidate),
                    priority=FOLLOW_UP_PRIORITY,
                    for_agent=ActorId(actor),
                    created_by="integrator",
                )
            )

    def on_compose(self, decision: Decision) -> None:
        """A rebase conflict; its task waits for the integration_failed that follows."""
        self.compose_decisions[strings_of(decision.get("candidates"))[0]] = decision

    def queue_compose(self, decision: Decision, task_id: str) -> None:
        """A task for the compose pool outside every arm (D74)."""
        candidates = strings_of(decision.get("candidates"))
        conflicting = candidates[0]
        self.compose_queue.put(
            Task(
                id=task_id,
                approach=self.facts[conflicting].approach,
                kind="rebase_compose",
                goal=(
                    f"Compose candidate {conflicting[:SHORT_HASH]} with "
                    f"baseline@{decision.get('baseline')}: {decision.get('why')}. "
                    "Produce one candidate that keeps both sides' intent and submit "
                    f"it for evaluation against {decision.get('target')}."
                ),
                refs=candidates,
                seed_context=json.dumps(
                    {
                        name: decision.get(name)
                        for name in (
                            "task_id",
                            "base_dir",
                            "ours_dir",
                            "theirs_dir",
                            "target",
                            "predicted",
                            "why",
                        )
                    },
                    sort_keys=True,
                ),
                priority=FOLLOW_UP_PRIORITY,
                pool="external",
                created_by="integrator",
            )
        )

    def on_quarantined(self, decision: Decision) -> None:
        """A reverted candidate's result claim: verified -> quarantined (D72)."""
        candidate = str(decision["candidate"])
        cause = str(decision.get("reason"))
        self.write_event(
            "claim_status",
            target=self.facts[candidate].claim_id,
            from_status=f"verified:{self.top_level}",
            to_status="quarantined",
            reason=f"reverted from the baseline by the {cause} check (D72, D74)",
            evidence=(f"candidates/{candidate}/result.json",),
        )

    def record(self, decision: Decision, is_closing: bool) -> None:
        """One core decision to the blackboard and the queues."""
        kind = decision.get("type")
        if not isinstance(kind, str):
            logger.warning(f"a core decision without a type: {decision}")
        elif kind == "integration_failed":
            self.on_failed(decision, is_closing)
        elif kind == "compose_task":
            self.on_compose(decision)
        elif kind == "quarantined":
            self.on_quarantined(decision)
        elif kind in PAYLOAD_FIELDS:
            candidate = decision.get("candidate")
            self.write_event(
                kind,
                target=candidate if isinstance(candidate, str) else None,
                data=self.payload(decision, kind),
            )
        elif kind in LOGGED_ONLY:
            logger.info(f"integrator {kind}: {decision}")
        else:
            logger.warning(f"unknown core decision {kind}: {decision}")
