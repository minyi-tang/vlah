"""The router: which merged events must reach a worker now (v2.2 section 8, D17, D29, D60).

All state lives in the key-value store, so every router object over one store (the
tool server's, the supervisor's) sees the same interrupts, and a restart loses none:
- interrupt:<agent>: the pending hard interrupt, as an envelope with its priority,
  how many others arrived while it was pending, and when the oldest of them was
  written. check() reads only this key. Acknowledging swaps it for an empty
  envelope with compare-and-set, so an interrupt that lands meanwhile is never lost.
- soft:<agent>: a sorted set of soft notes, for the next sync point.
- router:pending: agents with an unacknowledged hard interrupt, scored by when the
  oldest one was written, so a stream of new interrupts cannot postpone the
  executor's fallback (D60 layer 3).
- router:seen:<agent>:<rule>:<trigger>: one interrupt per agent, rule and event.
- router:latency and router:overdue: the H3 measurements.

on_commit runs inside the blackboard's merge delivery, check and acknowledge on the
interrupt hook's thread, and overdue on the supervisor's; all of them are short.
"""

import json
import logging
from dataclasses import dataclass

from pydantic import ValidationError

from vlah.arms.config import ArmConfig
from vlah.core.blackboard.api import MergedCommit
from vlah.core.indexer.api import Indexer
from vlah.core.queue.api import Leases
from vlah.core.router.rules import (
    CLAIM_TRIGGER,
    PREDICATES,
    Rule,
    Trigger,
    default_rules,
    render_reason,
)
from vlah.core.types.clock import Clock
from vlah.core.types.ids import ActorId
from vlah.core.types.interrupts import Interrupt
from vlah.core.types.params import Params
from vlah.core.types.tasks import Lease
from vlah.ports.store import KeyValueStore

logger = logging.getLogger(__name__)

PENDING_KEY = "router:pending"
LATENCY_KEY = "router:latency"
OVERDUE_KEY = "router:overdue"
EMPTY_ENVELOPE = b"{}"


def interrupt_key(agent: ActorId) -> str:
    return f"interrupt:{agent}"


def soft_key(agent: ActorId) -> str:
    return f"soft:{agent}"


@dataclass(frozen=True, kw_only=True)
class Envelope:
    interrupt: Interrupt
    priority: int
    pending_count: int
    oldest_written_ts: float

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "interrupt": self.interrupt.model_dump(mode="json"),
                "priority": self.priority,
                "pending_count": self.pending_count,
                "oldest_written_ts": self.oldest_written_ts,
            }
        ).encode("utf-8")


def parse_envelope(raw: bytes | None) -> Envelope | None:
    if raw is None or raw == EMPTY_ENVELOPE:
        return None
    else:
        try:
            data: object = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("not an object")
            else:
                return Envelope(
                    interrupt=Interrupt.model_validate(data["interrupt"]),
                    priority=int(data["priority"]),
                    pending_count=int(data["pending_count"]),
                    oldest_written_ts=float(data["oldest_written_ts"]),
                )
        except (KeyError, TypeError, ValueError, ValidationError):
            logger.exception("an interrupt record does not parse; treating it as empty")
            return None


@dataclass(frozen=True, kw_only=True)
class InterruptStats:
    """H3: write-to-acknowledge latencies, and how many interrupts went overdue."""

    latencies_seconds: tuple[float, ...]
    overdue_count: int

    @property
    def overdue_fraction(self) -> float:
        total = len(self.latencies_seconds) + self.overdue_count
        return self.overdue_count / total if total else 0.0


def triggers_of(merged_commit: MergedCommit) -> tuple[Trigger, ...]:
    events = tuple(
        Trigger(
            type=event.type,
            key=str(event.seq) if event.seq is not None else event.local_id,
            event=event,
            claim=None,
        )
        for event in merged_commit.events
    )
    claims = tuple(
        Trigger(
            type=CLAIM_TRIGGER,
            key=f"{merged_commit.commit_id}-{claim.id}",
            event=None,
            claim=claim,
        )
        for claim in merged_commit.claims
    )
    return events + claims


class KeyValueRouter:
    def __init__(
        self,
        *,
        store: KeyValueStore,
        leases: Leases,
        indexer: Indexer,
        arm: ArmConfig,
        params: Params,
        clock: Clock,
        rules: tuple[Rule, ...] | None = None,
    ) -> None:
        self.store = store
        self.leases = leases
        self.indexer = indexer
        self.arm = arm
        self.params = params
        self.clock = clock
        self.rules = tuple(
            rule for rule in (rules or default_rules()) if rule.is_enabled(arm)
        )

    def on_commit(self, merged_commit: MergedCommit) -> tuple[Interrupt, ...]:
        """Evaluate every enabled rule against every active lease (one scan per commit)."""
        triggers = triggers_of(merged_commit)
        if not triggers:
            return ()
        else:
            raised: list[Interrupt] = []
            for lease in self.leases.active():
                for trigger in triggers:
                    for rule in self.rules:
                        interrupt = self.match(rule, trigger, lease)
                        if interrupt is not None:
                            raised.append(interrupt)
                        else:
                            continue
            return tuple(raised)

    def match(self, rule: Rule, trigger: Trigger, lease: Lease) -> Interrupt | None:
        if trigger.type not in rule.events:
            return None
        elif not PREDICATES[rule.match](trigger, lease, self.indexer):
            return None
        else:
            interrupt = Interrupt(
                id=f"i-{lease.agent}-{rule.id}-{trigger.key}",
                agent=lease.agent,
                kind=rule.kind,
                rule=rule.rule,
                reason=render_reason(rule, trigger, lease),
                refs=tuple(trigger.event.evidence) if trigger.event is not None else (),
                written_ts=self.clock.now_seconds(),
            )
            seen_key = f"router:seen:{lease.agent}:{rule.id}:{trigger.key}"
            if not self.store.compare_and_set(seen_key, None, b"1", None):
                return None
            elif rule.kind == "hard":
                self.write_hard(interrupt, rule.priority)
                return interrupt
            else:
                self.store.sorted_add(
                    soft_key(lease.agent),
                    interrupt.model_dump_json(),
                    interrupt.written_ts,
                )
                return interrupt

    def write_hard(self, interrupt: Interrupt, priority: int) -> None:
        """Keep the most urgent pending interrupt and count the rest."""
        key = interrupt_key(interrupt.agent)
        while True:
            raw = self.store.get(key)
            current = parse_envelope(raw)
            if current is None:
                updated = Envelope(
                    interrupt=interrupt,
                    priority=priority,
                    pending_count=0,
                    oldest_written_ts=interrupt.written_ts,
                )
            elif priority < current.priority:
                updated = Envelope(
                    interrupt=interrupt,
                    priority=priority,
                    pending_count=current.pending_count + 1,
                    oldest_written_ts=current.oldest_written_ts,
                )
            else:
                updated = Envelope(
                    interrupt=current.interrupt,
                    priority=current.priority,
                    pending_count=current.pending_count + 1,
                    oldest_written_ts=current.oldest_written_ts,
                )
            if self.store.compare_and_set(key, raw, updated.to_bytes(), None):
                self.store.sorted_add(
                    PENDING_KEY, interrupt.agent, updated.oldest_written_ts
                )
                return
            else:
                continue

    def check(self, agent: ActorId) -> Interrupt | None:
        envelope = parse_envelope(self.store.get(interrupt_key(agent)))
        return envelope.interrupt if envelope is not None else None

    def pending_count(self, agent: ActorId) -> int:
        """How many more hard interrupts arrived while the shown one was pending."""
        envelope = parse_envelope(self.store.get(interrupt_key(agent)))
        return envelope.pending_count if envelope is not None else 0

    def acknowledge(self, agent: ActorId, interrupt_id: str) -> None:
        """Clear the pending interrupt if it is the one shown; any other id does nothing."""
        key = interrupt_key(agent)
        raw = self.store.get(key)
        envelope = parse_envelope(raw)
        if envelope is None or envelope.interrupt.id != interrupt_id:
            logger.info(f"{agent} acknowledged {interrupt_id}, which is not pending")
        elif self.store.compare_and_set(key, raw, EMPTY_ENVELOPE, None):
            self.store.sorted_remove(PENDING_KEY, agent)
            self.store.sorted_add(
                LATENCY_KEY,
                interrupt_id,
                self.clock.now_seconds() - envelope.interrupt.written_ts,
            )
            # An interrupt written between the swap and the removal keeps its fallback.
            arrived = parse_envelope(self.store.get(key))
            if arrived is not None:
                self.store.sorted_add(PENDING_KEY, agent, arrived.oldest_written_ts)
            else:
                pass
        else:
            logger.info(
                f"{agent}'s interrupt changed while {interrupt_id} was acknowledged"
            )

    def overdue(self, now_seconds: float) -> tuple[Interrupt, ...]:
        """Hard interrupts unacknowledged past the timeout, each returned once.

        The worker loop then restarts the agent with the interrupt in its start
        pack, so the pending record is cleared here (D60 layer 3).
        """
        timeout = self.params.interrupt_ack_timeout_seconds
        due: list[Interrupt] = []
        for member, oldest_written_ts in self.store.sorted_range(PENDING_KEY):
            agent = ActorId(member)
            if now_seconds - oldest_written_ts < timeout:
                continue
            elif not self.store.sorted_remove(PENDING_KEY, member):
                continue
            else:
                key = interrupt_key(agent)
                raw = self.store.get(key)
                envelope = parse_envelope(raw)
                if envelope is not None and self.store.compare_and_set(
                    key, raw, EMPTY_ENVELOPE, None
                ):
                    self.store.sorted_add(
                        OVERDUE_KEY, envelope.interrupt.id, now_seconds
                    )
                    due.append(envelope.interrupt)
                else:
                    pass
        return tuple(due)

    def soft_notes(self, agent: ActorId) -> tuple[Interrupt, ...]:
        """Soft notes for the next sync point. Not in the Router contract yet."""
        return tuple(
            Interrupt.model_validate_json(member)
            for member, _ in self.store.sorted_range(soft_key(agent))
        )

    def stats(self) -> InterruptStats:
        return InterruptStats(
            latencies_seconds=tuple(
                latency for _, latency in self.store.sorted_range(LATENCY_KEY)
            ),
            overdue_count=len(self.store.sorted_range(OVERDUE_KEY)),
        )
