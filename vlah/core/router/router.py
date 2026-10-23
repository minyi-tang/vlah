"""The router: which merged events must reach a worker now (v2.2 section 8, D17, D29, D60).

All state lives in the key-value store, so every router object over one store (the
tool server's, the supervisor's) sees the same interrupts, and a restart loses none:
- interrupt:<agent>: an envelope with the task the interrupts were written for and
  every unacknowledged hard interrupt, most urgent first; the first one is shown.
  check() reads only this key, and looks up the lease only when it finds one. Every
  change is one compare-and-set on the key, so an interrupt that lands meanwhile is
  never lost, and acknowledging the shown one shows the next.
- soft:<agent>: a sorted set of soft notes, each tagged with its task, for the next
  sync point.
- router:pending: agents with an unacknowledged hard interrupt, scored by when the
  shown interrupt started waiting, so a stream of new interrupts cannot postpone the
  executor's fallback (D60 layer 3).
- router:seen:<agent>:<rule>:<trigger>: one interrupt per agent, rule and event.
- router:latency and router:overdue: the H3 measurements.

An interrupt belongs to the task its lease held. While the agent holds no lease it
waits unseen (a restart moves the lease through that state); once the agent holds a
lease for another task it is discarded, since nobody could have acknowledged it.

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
class PendingInterrupt:
    """One unacknowledged hard interrupt; target is what its trigger was about."""

    interrupt: Interrupt
    priority: int
    rule_id: str
    target: str

    def to_json(self) -> dict[str, object]:
        return {
            "interrupt": self.interrupt.model_dump(mode="json"),
            "priority": self.priority,
            "rule_id": self.rule_id,
            "target": self.target,
        }

    def order(self) -> tuple[int, float, str]:
        return (self.priority, self.interrupt.written_ts, self.interrupt.id)


def parse_pending(data: object) -> PendingInterrupt:
    if not isinstance(data, dict):
        raise ValueError("a pending interrupt is not an object")
    else:
        return PendingInterrupt(
            interrupt=Interrupt.model_validate(data["interrupt"]),
            priority=int(data["priority"]),
            rule_id=str(data["rule_id"]),
            target=str(data["target"]),
        )


@dataclass(frozen=True, kw_only=True)
class Envelope:
    """The hard interrupts one agent has not acknowledged, most urgent first.

    fallback_from_ts is when the shown interrupt started waiting: the first write
    into an empty envelope, or the moment the previous one was acknowledged or went
    overdue. A new arrival never moves it.
    """

    task_id: str
    fallback_from_ts: float
    entries: tuple[PendingInterrupt, ...]

    @property
    def interrupt(self) -> Interrupt:
        return self.entries[0].interrupt

    def with_entry(self, entry: PendingInterrupt) -> "Envelope":
        entries = sorted((*self.entries, entry), key=PendingInterrupt.order)
        return Envelope(
            task_id=self.task_id,
            fallback_from_ts=self.fallback_from_ts,
            entries=tuple(entries),
        )

    def without(
        self, removed: tuple[PendingInterrupt, ...], now_seconds: float
    ) -> "Envelope | None":
        """The envelope with entries removed, or None when nothing is left.

        The fallback clock restarts when the shown entry leaves.
        """
        kept = tuple(entry for entry in self.entries if entry not in removed)
        if not kept:
            return None
        elif kept[0] == self.entries[0]:
            return Envelope(
                task_id=self.task_id,
                fallback_from_ts=self.fallback_from_ts,
                entries=kept,
            )
        else:
            return Envelope(
                task_id=self.task_id, fallback_from_ts=now_seconds, entries=kept
            )

    def to_bytes(self) -> bytes:
        return json.dumps(
            {
                "task_id": self.task_id,
                "fallback_from_ts": self.fallback_from_ts,
                "entries": [entry.to_json() for entry in self.entries],
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
                entries: object = data["entries"]
                if not isinstance(entries, list) or not entries:
                    raise ValueError("no entries")
                else:
                    return Envelope(
                        task_id=str(data["task_id"]),
                        fallback_from_ts=float(data["fallback_from_ts"]),
                        entries=tuple(parse_pending(entry) for entry in entries),
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


def trigger_target(trigger: Trigger) -> str:
    if trigger.event is not None:
        return trigger.event.target or ""
    elif trigger.claim is not None:
        return trigger.claim.id
    else:
        return ""


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
        """Apply one rule to one lease; a predicate that raises loses only this pair."""
        if trigger.type not in rule.events:
            return None
        else:
            try:
                is_hit = PREDICATES[rule.match](trigger, lease, self.indexer)
            except Exception:
                logger.exception(
                    f"rule {rule.id} failed on {trigger.key} for {lease.agent}; "
                    "routing continues without it"
                )
                return None
        if not is_hit:
            return None
        elif rule.kind == "revoke":
            assert rule.revokes is not None
            self.withdraw(lease, rule.revokes, trigger_target(trigger))
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
                assert rule.priority is not None
                self.write_hard(
                    lease.task_id,
                    PendingInterrupt(
                        interrupt=interrupt,
                        priority=rule.priority,
                        rule_id=rule.id,
                        target=trigger_target(trigger),
                    ),
                )
                return interrupt
            else:
                self.store.sorted_add(
                    soft_key(lease.agent),
                    json.dumps(
                        {
                            "task_id": lease.task_id,
                            "interrupt": interrupt.model_dump(mode="json"),
                        }
                    ),
                    interrupt.written_ts,
                )
                return interrupt

    def replace(
        self, agent: ActorId, raw: bytes | None, updated: Envelope | None
    ) -> bool:
        """Swap the agent's envelope with compare-and-set and keep router:pending in step.

        The store has no compare-and-delete, so an emptied envelope is the empty
        object. An interrupt written between the swap and the removal keeps its
        fallback: the key is read again after the removal.
        """
        key = interrupt_key(agent)
        if updated is not None:
            if self.store.compare_and_set(key, raw, updated.to_bytes(), None):
                self.store.sorted_add(PENDING_KEY, agent, updated.fallback_from_ts)
                return True
            else:
                return False
        elif self.store.compare_and_set(key, raw, EMPTY_ENVELOPE, None):
            self.store.sorted_remove(PENDING_KEY, agent)
            arrived = parse_envelope(self.store.get(key))
            if arrived is not None:
                self.store.sorted_add(PENDING_KEY, agent, arrived.fallback_from_ts)
            else:
                pass
            return True
        else:
            return False

    def write_hard(self, task_id: str, entry: PendingInterrupt) -> None:
        """Add to the agent's envelope; one left by another task is replaced."""
        agent = entry.interrupt.agent
        while True:
            raw = self.store.get(interrupt_key(agent))
            current = parse_envelope(raw)
            if current is None or current.task_id != task_id:
                updated = Envelope(
                    task_id=task_id,
                    fallback_from_ts=entry.interrupt.written_ts,
                    entries=(entry,),
                )
            else:
                updated = current.with_entry(entry)
            if self.replace(agent, raw, updated):
                return
            else:
                continue

    def withdraw(self, lease: Lease, rule_id: str, target: str) -> None:
        """Drop the pending interrupts a rule raised about target; the next one shows."""
        while True:
            raw = self.store.get(interrupt_key(lease.agent))
            current = parse_envelope(raw)
            if current is None or current.task_id != lease.task_id:
                return
            else:
                revoked = tuple(
                    entry
                    for entry in current.entries
                    if entry.rule_id == rule_id and entry.target == target
                )
                if not revoked:
                    return
                elif self.replace(
                    lease.agent,
                    raw,
                    current.without(revoked, self.clock.now_seconds()),
                ):
                    logger.info(
                        f"withdrew {[entry.interrupt.id for entry in revoked]} "
                        f"from {lease.agent}"
                    )
                    return
                else:
                    continue

    def check(self, agent: ActorId) -> Interrupt | None:
        """The shown interrupt: one store read, plus the lease only when there is one."""
        raw = self.store.get(interrupt_key(agent))
        envelope = parse_envelope(raw)
        if envelope is None:
            return None
        else:
            lease = self.leases.get(agent)
            if lease is None:
                return None
            elif lease.task_id == envelope.task_id:
                return envelope.interrupt
            else:
                self.discard(agent, raw, envelope)
                return None

    def discard(self, agent: ActorId, raw: bytes | None, stale: Envelope) -> None:
        """Clear an envelope left by a task the agent no longer holds."""
        if self.replace(agent, raw, None):
            logger.info(
                f"discarded {len(stale.entries)} interrupts {agent} could not "
                f"acknowledge: task {stale.task_id} ended"
            )
        else:
            pass

    def pending_count(self, agent: ActorId) -> int:
        """How many more hard interrupts wait behind the shown one."""
        envelope = parse_envelope(self.store.get(interrupt_key(agent)))
        return len(envelope.entries) - 1 if envelope is not None else 0

    def acknowledge(self, agent: ActorId, interrupt_id: str) -> None:
        """Clear the shown interrupt and show the next; any other id does nothing.

        The swap is retried while the shown interrupt is still the acknowledged one,
        so an arrival behind it cannot make a valid acknowledgement a no-op.
        """
        key = interrupt_key(agent)
        while True:
            raw = self.store.get(key)
            envelope = parse_envelope(raw)
            if envelope is None or envelope.interrupt.id != interrupt_id:
                logger.info(f"{agent} acknowledged {interrupt_id}, which is not shown")
                return
            else:
                now_seconds = self.clock.now_seconds()
                shown = envelope.entries[0]
                if self.replace(agent, raw, envelope.without((shown,), now_seconds)):
                    self.store.sorted_add(
                        LATENCY_KEY,
                        interrupt_id,
                        now_seconds - shown.interrupt.written_ts,
                    )
                    return
                else:
                    continue

    def overdue(self, now_seconds: float) -> tuple[Interrupt, ...]:
        """Hard interrupts unacknowledged past the timeout, each returned once.

        The worker loop then restarts the agent with the interrupt in its start
        pack, so the shown one is cleared here and the next one shows (D60 layer 3).
        An agent whose lease moved to another task has its interrupts discarded;
        one with no lease keeps them until it holds a lease again.
        """
        timeout = self.params.interrupt_ack_timeout_seconds
        due: list[Interrupt] = []
        for member, fallback_from_ts in self.store.sorted_range(PENDING_KEY):
            agent = ActorId(member)
            if now_seconds - fallback_from_ts < timeout:
                continue
            else:
                lease = self.leases.get(agent)
            if lease is None or not self.store.sorted_remove(PENDING_KEY, member):
                continue
            else:
                while True:
                    raw = self.store.get(interrupt_key(agent))
                    envelope = parse_envelope(raw)
                    if envelope is None:
                        break
                    elif envelope.task_id != lease.task_id:
                        self.discard(agent, raw, envelope)
                        break
                    else:
                        shown = envelope.entries[0]
                        if self.replace(
                            agent, raw, envelope.without((shown,), now_seconds)
                        ):
                            self.store.sorted_add(
                                OVERDUE_KEY, shown.interrupt.id, now_seconds
                            )
                            due.append(shown.interrupt)
                            break
                        else:
                            continue
        return tuple(due)

    def soft_notes(self, agent: ActorId) -> tuple[Interrupt, ...]:
        """Soft notes for the agent's current task; notes of earlier tasks are dropped.

        Not in the Router contract yet.
        """
        lease = self.leases.get(agent)
        if lease is None:
            return ()
        else:
            notes: list[Interrupt] = []
            for member, _ in self.store.sorted_range(soft_key(agent)):
                data: object = json.loads(member)
                if not isinstance(data, dict):
                    raise ValueError("a soft note is not an object")
                elif str(data["task_id"]) == lease.task_id:
                    notes.append(Interrupt.model_validate(data["interrupt"]))
                else:
                    self.store.sorted_remove(soft_key(agent), member)
            return tuple(notes)

    def stats(self) -> InterruptStats:
        return InterruptStats(
            latencies_seconds=tuple(
                latency for _, latency in self.store.sorted_range(LATENCY_KEY)
            ),
            overdue_count=len(self.store.sorted_range(OVERDUE_KEY)),
        )
