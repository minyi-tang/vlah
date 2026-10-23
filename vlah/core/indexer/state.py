"""The indexer's replayed state: claims, statuses, refutations, lineage (v2.2 D9, D34, D56).

Everything here follows from the merged files and events alone, applied in merge
sequence order, so a rebuild from the repository equals the incremental state.

Rules applied on replay:
- a status change must pass the D9 table (check_transition); a worker confirming a
  dead end must not be its author;
- D34 priority: a role cannot overturn a status set by a strictly higher-priority
  role; a human can overturn anything. A reinstated event, and a human lift out
  of refuted or quarantined, hand the claim back to the roles that normally advance
  it, so the higher role's own undo does not freeze the ladder;
- a role reinstates only what an equal or lower role refuted;
- refutation sources are counted before the transition check, so a second source
  that confirms an existing refutation is not dropped as "unchanged";
- a refutation cascades only when it comes from a human or the Critic or has two
  sources: to every claim that depends on the refuted one, and to the results of
  every candidate that descends from a refuted candidate;
- a "reinstated" event undoes a refutation and its cascade; the dead-end status
  "reinstated" is an ordinary D9 transition.
Events that break these rules are ignored and logged.
"""

import json
import logging
import re
from dataclasses import dataclass, field

import yaml

from vlah.core.indexer.api import ClaimState, StatusChange
from vlah.core.types.claims import Claim
from vlah.core.types.events import Event, ResultReady
from vlah.core.types.ids import WRITE_PRIORITY, ActorId, Role
from vlah.core.types.states import (
    INITIAL_STATUS,
    TRANSITION_RULES,
    Subject,
    check_transition,
)

logger = logging.getLogger(__name__)

AUTHORITATIVE_REFUTERS: frozenset[Role] = frozenset({"human", "critic"})
# Statuses a higher role imposes; leaving one is a lift that reopens the ladder.
LIFTED_STATUSES: frozenset[str] = frozenset({"refuted", "quarantined"})
SECOND_SOURCE_COUNT = 2
CANDIDATE_POINTER = re.compile(r"candidates/(?P<hash>[0-9a-f]+)/")
LINEAGE_FILE = re.compile(r"candidates/(?P<hash>[0-9a-f]+)/lineage\.json")
APPROACH_FILE = re.compile(r"approaches/(?P<id>[^/]+)\.md")
UNKNOWN_STATUS = "unknown"


@dataclass(kw_only=True)
class ClaimRecord:
    claim: Claim
    # The first merge sequence number at which the claim is on the blackboard; None
    # until an event merges after it, because a claim-only commit gets no number.
    stamp: int | None
    status: str
    set_by_role: Role | None = None
    history: list[StatusChange] = field(default_factory=list[StatusChange])
    refuters: dict[ActorId, Role] = field(default_factory=dict[ActorId, Role])
    status_before_refutation: str | None = None
    last_change: int | None = None


@dataclass(kw_only=True)
class ApproachRecord:
    approach_id: str
    status: str
    set_by_role: Role | None = None
    history: list[StatusChange] = field(default_factory=list[StatusChange])


def may_refute(subject: Subject, role: Role) -> bool:
    """Whether D9 lets role refute this kind of claim at all."""
    return role == "human" or any(
        rule.subject == subject and rule.to_kind == "refuted" and role in rule.roles
        for rule in TRANSITION_RULES
    )


def candidate_hashes_of(claim: Claim) -> tuple[str, ...]:
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
            if (match := CANDIDATE_POINTER.match(pointer)) is not None
        )
    )


def cited_claims_of(claim: Claim) -> tuple[str, ...]:
    """Claims this one builds on: its depends_on and its claim evidence."""
    return tuple(
        dict.fromkeys(
            [*claim.depends_on]
            + [evidence.claim for evidence in claim.evidence if evidence.claim]
        )
    )


@dataclass(kw_only=True)
class ReplayState:
    top_level: str | None = None
    claims: dict[str, ClaimRecord] = field(default_factory=dict[str, ClaimRecord])
    approaches: dict[str, ApproachRecord] = field(
        default_factory=dict[str, ApproachRecord]
    )
    parents: dict[str, tuple[str, ...]] = field(
        default_factory=dict[str, tuple[str, ...]]
    )
    result_claims_by_candidate: dict[str, list[str]] = field(
        default_factory=dict[str, list[str]]
    )
    applied_sequences: set[int] = field(default_factory=set[int])
    # Candidates judged at the top level, for the dead-end re-walk metric.
    top_level_measured: set[str] = field(default_factory=set[str])
    head: int = 0

    # Files

    def add_claim(self, claim: Claim, stamp: int | None) -> None:
        if claim.id in self.claims:
            return
        else:
            self.claims[claim.id] = ClaimRecord(
                claim=claim, stamp=stamp, status=claim.status, last_change=stamp
            )
            if claim.type == "result":
                for candidate in candidate_hashes_of(claim):
                    self.result_claims_by_candidate.setdefault(candidate, []).append(
                        claim.id
                    )
            else:
                pass

    def stamp_pending(self, sequence: int) -> None:
        """Claims merged without an event become visible at the next event's number."""
        for record in self.claims.values():
            if record.stamp is None:
                record.stamp = sequence
                record.last_change = sequence
            else:
                continue

    def add_file(self, path: str, content: bytes) -> None:
        lineage = LINEAGE_FILE.fullmatch(path)
        approach = APPROACH_FILE.fullmatch(path)
        if lineage is not None:
            data: object = json.loads(content)
            parents = data.get("parents") if isinstance(data, dict) else None
            self.parents[lineage.group("hash")] = tuple(
                str(parent) for parent in parents or []
            )
        elif approach is not None and approach.group("id") not in self.approaches:
            self.approaches[approach.group("id")] = ApproachRecord(
                approach_id=approach.group("id"),
                status=approach_status_of(content),
            )
        else:
            pass

    # Events

    def apply_event(self, event: Event) -> None:
        sequence = event.seq if event.seq is not None else self.head + 1
        if sequence in self.applied_sequences:
            return
        else:
            self.applied_sequences.add(sequence)
            self.head = max(self.head, sequence)
            if event.type == "claim_status":
                self.apply_claim_status(event, sequence)
            elif event.type == "reinstated":
                self.apply_reinstated(event, sequence)
            elif event.type == "approach_status":
                self.apply_approach_status(event, sequence)
            elif (
                isinstance(event.data, ResultReady)
                and event.data.level == self.top_level
            ):
                self.top_level_measured.add(event.data.candidate)
            else:
                pass

    def outranked(self, set_by: Role | None, role: Role) -> bool:
        """D34: only a role of equal or higher priority may overturn a status."""
        if set_by is None or role == "human":
            return False
        else:
            return WRITE_PRIORITY[set_by] < WRITE_PRIORITY[role]

    def apply_claim_status(self, event: Event, sequence: int) -> None:
        record = self.claims.get(event.target or "")
        new_status = event.to
        if record is None or new_status is None:
            logger.warning(f"claim_status {event.local_id} names no known claim")
            return
        else:
            subject = record.claim.type
            if new_status == "refuted" and record.status == "refuted":
                # A second source confirming the refutation (D34).
                if may_refute(subject, event.role):
                    record.refuters[event.actor] = event.role
                    record.last_change = sequence
                else:
                    logger.info(
                        f"ignored {event.local_id}: {event.role} may not refute"
                    )
                return
            else:
                decision = check_transition(
                    subject, record.status, new_status, event.role
                )
                if not decision.is_allowed:
                    logger.info(f"ignored {event.local_id}: {decision.reason}")
                elif self.outranked(record.set_by_role, event.role):
                    logger.info(
                        f"ignored {event.local_id}: {record.set_by_role} set "
                        f"{record.claim.id}, which outranks {event.role} (D34)"
                    )
                elif (
                    subject == "dead_end"
                    and new_status == "confirmed"
                    and event.role == "worker"
                    and event.actor == record.claim.author
                ):
                    logger.info(
                        f"ignored {event.local_id}: an author cannot confirm itself"
                    )
                else:
                    self.change_status(record, event, sequence, new_status)

    def change_status(
        self, record: ClaimRecord, event: Event, sequence: int, new_status: str
    ) -> None:
        is_lift = event.role == "human" and record.status in LIFTED_STATUSES
        if new_status == "refuted":
            record.status_before_refutation = record.status
            record.refuters[event.actor] = event.role
        elif record.status == "refuted":
            # Leaving refuted by any route drops its sources, or a later single
            # refutation would count as the second one.
            record.refuters.clear()
            record.status_before_refutation = None
        else:
            pass
        record.history.append(
            StatusChange(
                sequence=sequence,
                actor=event.actor,
                from_status=record.status,
                to_status=new_status,
            )
        )
        record.status = new_status
        record.set_by_role = None if is_lift else event.role
        record.last_change = sequence

    def apply_reinstated(self, event: Event, sequence: int) -> None:
        """Undo a refutation: back to the status it had, and no cascade from it."""
        record = self.claims.get(event.target or "")
        if record is None or record.status != "refuted":
            logger.info(f"ignored {event.local_id}: nothing refuted to reinstate")
        elif event.role not in AUTHORITATIVE_REFUTERS:
            logger.info(f"ignored {event.local_id}: {event.role} may not reinstate")
        elif any(
            self.outranked(refuter_role, event.role)
            for refuter_role in record.refuters.values()
        ):
            logger.info(
                f"ignored {event.local_id}: a role above {event.role} refuted "
                f"{record.claim.id} (D34)"
            )
        else:
            restored = (
                record.status_before_refutation or INITIAL_STATUS[record.claim.type]
            )
            record.history.append(
                StatusChange(
                    sequence=sequence,
                    actor=event.actor,
                    from_status=record.status,
                    to_status=restored,
                )
            )
            record.status = restored
            record.set_by_role = None
            record.refuters.clear()
            record.status_before_refutation = None
            record.last_change = sequence

    def apply_approach_status(self, event: Event, sequence: int) -> None:
        approach = self.approaches.get(event.target or "")
        if approach is None or event.to is None:
            logger.warning(f"approach_status {event.local_id} names no known approach")
        else:
            decision = check_transition(
                "approach", approach.status, event.to, event.role
            )
            if not decision.is_allowed:
                logger.info(f"ignored {event.local_id}: {decision.reason}")
            elif self.outranked(approach.set_by_role, event.role):
                logger.info(f"ignored {event.local_id}: outranked (D34)")
            else:
                approach.history.append(
                    StatusChange(
                        sequence=sequence,
                        actor=event.actor,
                        from_status=approach.status,
                        to_status=event.to,
                    )
                )
                approach.status = event.to
                approach.set_by_role = event.role

    # Queries

    def is_authoritatively_refuted(self, claim_id: str) -> bool:
        record = self.claims.get(claim_id)
        if record is None or record.status != "refuted":
            return False
        else:
            roles = set(record.refuters.values())
            return bool(roles & AUTHORITATIVE_REFUTERS) or (
                len(record.refuters) >= SECOND_SOURCE_COUNT
            )

    def is_refuted_candidate(self, candidate: str, seen: set[str]) -> bool:
        """A candidate whose result, or an ancestor's result, is refuted with authority."""
        if candidate in seen:
            return False
        else:
            seen.add(candidate)
            own = any(
                self.is_authoritatively_refuted(claim_id)
                for claim_id in self.result_claims_by_candidate.get(candidate, [])
            )
            return own or any(
                self.is_refuted_candidate(parent, seen)
                for parent in self.parents.get(candidate, ())
            )

    def is_cascaded_refuted(self, claim_id: str, seen: set[str] | None = None) -> bool:
        visited = seen if seen is not None else set[str]()
        record = self.claims.get(claim_id)
        if record is None or claim_id in visited:
            return False
        else:
            visited.add(claim_id)
            if self.is_authoritatively_refuted(claim_id):
                return True
            elif any(
                self.is_cascaded_refuted(dependency, visited)
                for dependency in record.claim.depends_on
            ):
                return True
            elif record.claim.type == "result":
                return any(
                    self.is_refuted_candidate(candidate, set())
                    for candidate in candidate_hashes_of(record.claim)
                )
            else:
                return False

    def claim_state(self, claim_id: str) -> ClaimState:
        record = self.claims.get(claim_id)
        if record is None:
            return ClaimState(
                claim_id=claim_id,
                status=UNKNOWN_STATUS,
                history=(),
                is_cascaded_refuted=False,
            )
        else:
            return ClaimState(
                claim_id=claim_id,
                status=record.status,
                history=tuple(record.history),
                is_cascaded_refuted=self.is_cascaded_refuted(claim_id),
            )

    def citations_of(self, claim_id: str) -> tuple[str, ...]:
        """Distinct claims that build on claim_id, by id: the same on a rebuild."""
        return tuple(
            other_id
            for other_id in sorted(self.claims)
            if claim_id in cited_claims_of(self.claims[other_id].claim)
        )

    def fingerprint(self) -> str:
        """Every replayed fact, in a stable order: equal states give equal strings."""
        return json.dumps(
            {
                "claims": {
                    claim_id: {
                        "stamp": record.stamp,
                        "last_change": record.last_change,
                        "status": record.status,
                        "set_by": record.set_by_role,
                        "history": [
                            [
                                change.sequence,
                                change.actor,
                                change.from_status,
                                change.to_status,
                            ]
                            for change in record.history
                        ],
                        "refuters": sorted(record.refuters),
                        "cascaded": self.is_cascaded_refuted(claim_id),
                    }
                    for claim_id, record in sorted(self.claims.items())
                },
                "approaches": {
                    approach_id: [record.status, len(record.history)]
                    for approach_id, record in sorted(self.approaches.items())
                },
                "parents": {
                    key: list(value) for key, value in sorted(self.parents.items())
                },
                "top_level_measured": sorted(self.top_level_measured),
            },
            sort_keys=True,
        )


def approach_status_of(content: bytes) -> str:
    """The status in an approach file's frontmatter; active when it gives none."""
    text = content.decode("utf-8", "replace")
    if not text.startswith("---"):
        return INITIAL_STATUS["approach"]
    else:
        frontmatter: object = yaml.safe_load(text.split("---")[1])
        status = frontmatter.get("status") if isinstance(frontmatter, dict) else None
        return str(status) if status else INITIAL_STATUS["approach"]
