"""Rebuilding the Integrator from the blackboard after a restart (D13, D74).

The shared core keeps its versions in memory and has no load function, so a
restarted wrapper replays them through the core itself, in order: each merge of a
baseline/<k>.json from its candidate's files, each revert from the merges it
removed. A ReplayMeasurer answers the core's measurements with the scores the
record holds, so the replay spends no judge run and the merge rules stay the
core's. A replayed version that differs from its record stops the restart: the
run must not go on from a baseline it cannot rebuild.

What the records do not say comes from the events: the spot-check counters from
the Integrator's audit_checked and baseline_reverted, the audit-set scores from
the remeasure_ready that measured a baseline itself, and the candidates still to
integrate from the result claims verified at the top level that no decision
settled and nobody refuted. A candidate failed only because the run ended is
integrated again, since the run goes on. The Critic's rechecks are its own: their
audit_checked and remeasure_ready count towards its share, not the Integrator's.
"""

import statistics
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from vlah.core.blackboard.api import Blackboard
from vlah.core.integrator.core_adapter import (
    CoreAdapter,
    Measurement,
    VerifiedCandidate,
    Version,
)
from vlah.core.integrator.guard import GuardBatches
from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.events import (
    AuditChecked,
    BaselineAdvanced,
    BaselineReverted,
    Event,
    IntegrationFailed,
    RemeasureReady,
    event_from_json,
)
from vlah.core.types.ids import ActorId

INTEGRATOR = ActorId("integrator")
VERIFIED_PREFIX = "verified:"
REFUTED = "refuted"
RUN_ENDED = "run_ended"
REPLAY_REASON = "replay after a restart"


def events_in_order(blackboard: Blackboard) -> Iterator[Event]:
    """Every merged event, in sequence order."""
    for sequence in range(1, blackboard.head_sequence() + 1):
        path = blackboard.read(f"events/_seq/{sequence}", None).decode()
        yield event_from_json(blackboard.read(path, None).decode())


def baseline_records(blackboard: Blackboard) -> list[BaselineRecord]:
    records = [
        BaselineRecord.model_validate_json(blackboard.read(path, None))
        for path in blackboard.list_paths("baseline/", None)
        if path.endswith(".json")
    ]
    return sorted(records, key=lambda record: record.k)


@dataclass(frozen=True, kw_only=True)
class IntegratorHistory:
    """What earlier processes' events say beyond the baseline records."""

    # Result claims verified at the top level, in the order they were verified.
    verified_claims: tuple[str, ...]
    # Claims refuted after that, by the Critic's recheck (D66).
    refuted_claims: frozenset[str]
    # Candidates merged, or failed for any reason but the end of the run.
    decided_candidates: frozenset[str]
    last_audited_k: int
    audited_versions: frozenset[int]
    audit_per_seed_by_version: dict[int, tuple[float, ...]]


def read_history(blackboard: Blackboard, top_level: str) -> IntegratorHistory:
    verified_claims: list[str] = []
    refuted_claims: set[str] = set()
    decided_candidates: set[str] = set()
    last_audited_k = 0
    audited_versions: set[int] = set()
    audit_per_seed_by_version: dict[int, tuple[float, ...]] = {}
    for event in events_in_order(blackboard):
        data = event.data
        if event.type == "claim_status" and event.target is not None:
            if (
                event.to == f"{VERIFIED_PREFIX}{top_level}"
                and event.target not in verified_claims
            ):
                verified_claims.append(event.target)
            elif event.to == REFUTED:
                refuted_claims.add(event.target)
            else:
                pass
        elif (
            isinstance(data, RemeasureReady)
            and data.dataset == "audit"
            and data.parent_candidate == data.baseline
        ):
            version_k = int(data.baseline.removeprefix("baseline@"))
            audited_versions.add(version_k)
            if data.score is not None and data.per_seed:
                audit_per_seed_by_version[version_k] = data.per_seed
            else:
                pass
        elif event.actor != INTEGRATOR:
            pass
        elif isinstance(data, BaselineAdvanced):
            decided_candidates.add(data.candidate)
        elif isinstance(data, IntegrationFailed) and data.reason != RUN_ENDED:
            decided_candidates.add(data.candidate)
        elif isinstance(data, AuditChecked):
            last_audited_k = data.segment[1]
            audited_versions.update(data.segment)
        elif isinstance(data, BaselineReverted):
            last_audited_k = data.new_baseline
        else:
            pass
    return IntegratorHistory(
        verified_claims=tuple(verified_claims),
        refuted_claims=frozenset(refuted_claims),
        decided_candidates=frozenset(decided_candidates),
        last_audited_k=last_audited_k,
        audited_versions=frozenset(audited_versions),
        audit_per_seed_by_version=audit_per_seed_by_version,
    )


@dataclass(kw_only=True)
class ReplayMeasurer:
    """The core's Measurer during a replay: the replayed record's scores, at once.

    submit takes the core's Measurer arguments and needs none of them: no judge runs.
    """

    record: BaselineRecord | None = None
    ticket_count: int = 0

    def submit(
        self,
        candidate_dir: Path,
        baseline: int,
        dataset: str,
        note: str,
        parents: list[str],
        exempt: bool,
        predicted: float,
    ) -> str:
        self.ticket_count += 1
        return f"replay-{self.ticket_count}"

    def result(self, ticket: str) -> Measurement | None:
        if self.record is None:
            return None
        else:
            per_seed = list(self.record.per_seed)
            score = statistics.fmean(per_seed) if per_seed else None
            return Measurement(ticket, None, per_seed, score, True)


def contribution(version: Version) -> str:
    """The candidate the core names when it reverts this version's merge."""
    if version.added is not None:
        return str(version.added)
    elif version.candidates:
        return str(version.candidates[-1])
    else:
        return ""


def reverted_segment(
    versions: list[Version], removed: tuple[str, ...]
) -> tuple[int, int]:
    """The (low, high] a revert took out, from the candidates it removed.

    removed starts with what versions low+1..high contributed, in order; the rest
    are later merges that conflicted with the replay. Spot checks and the guard
    revert merges, so low+1 is the merge that added removed[0]. Only a guard
    bisection can land on a revert, which is then found by its last candidate.
    """
    merges = [
        index
        for index in range(1, len(versions))
        if versions[index].added == removed[0]
    ]
    first = (
        merges[-1]
        if merges
        else max(
            index
            for index in range(1, len(versions))
            if contribution(versions[index]) == removed[0]
        )
    )
    high = first
    while (
        high + 1 < len(versions)
        and high + 1 - first < len(removed)
        and contribution(versions[high + 1]) == removed[high + 1 - first]
    ):
        high += 1
    return first - 1, high


def replay_versions(
    core: CoreAdapter,
    records: list[BaselineRecord],
    verified_by_hash: Mapping[str, VerifiedCandidate],
    now_seconds: float,
) -> None:
    """Rebuild versions 1..K through the core, each checked against its record."""
    live_measurer = core.measurer
    measurer = ReplayMeasurer()
    core.measurer = measurer
    core.versions[0].per_seed = list(records[0].per_seed)
    for record in records[1:]:
        measurer.record = record
        if record.added is not None:
            core.integrate(verified_by_hash[record.added])
        elif record.removed:
            low, high = reverted_segment(core.versions, record.removed)
            core.revert(low, high, REPLAY_REASON)
        else:
            raise RuntimeError(
                f"baseline/{record.k}.json records neither a merge nor a revert"
            )
        while core.due(now_seconds) == "wait":
            core.step()
        version = core.current()
        if version.k != record.k or tuple(version.candidates) != record.candidates:
            raise RuntimeError(
                f"replaying baseline@{record.k} gave baseline@{version.k} holding "
                f"{version.candidates}, not {list(record.candidates)}; the run "
                "cannot resume from a baseline it cannot rebuild"
            )
        else:
            version.per_seed = list(record.per_seed)
            version.created_ts = record.created_ts
    core.measurer = live_measurer
    core.pending_decisions = []


def restore_spot_checks(
    core: CoreAdapter, history: IntegratorHistory, has_calibrated_audit: bool
) -> None:
    """The counters the core keeps for spot checks (D72), from the events.

    A version counts as audit-measured when a spot check named it or an audit
    remeasure measured it; baseline@0 does not when the calibration gave its
    audit scores. Over-counting only leaves fewer spot checks.
    """
    for version_k, per_seed in history.audit_per_seed_by_version.items():
        if version_k < len(core.versions):
            core.versions[version_k].audit_per_seed = list(per_seed)
        else:
            pass
    core.last_audited_k = history.last_audited_k
    core.merges_since_audit = sum(
        1
        for version in core.versions[history.last_audited_k + 1 :]
        if version.added is not None
    )
    measured = set(history.audited_versions)
    if has_calibrated_audit:
        measured.discard(0)
    else:
        pass
    core.audits_used = len(measured)


def restore_guard(guard: GuardBatches, core: CoreAdapter) -> None:
    """The guard batch from the recorded gate outcomes (D74).

    Settling a batch writes nothing, so the last guarded version is the newest whose
    record holds passing gates, else baseline@0; every merge after it is checked
    again.
    """
    if guard.enabled:
        passed = [
            k
            for k, outcomes in guard.outcomes.items()
            if outcomes and all(outcomes.values())
        ]
        guard.last_guarded = max(passed, default=0)
        guard.pending = {
            version.k
            for version in core.versions[guard.last_guarded + 1 :]
            if version.added is not None
        }
        guard.merges_since = len(guard.pending)
    else:
        pass
