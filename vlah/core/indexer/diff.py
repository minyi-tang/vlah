"""diff_since: what changed that matters to this agent, ranked and truncated (v2.2 D17, D18).

Items are claims stamped or changed after the cursor, minus the viewer's own. They
rank, first to last:
0. the working set: the lease's target claims and ids the agent recently searched;
1. results verified at some level in the agent's approach;
2. dead ends in the agent's approach;
3. insights from other approaches;
4. everything else.
Ties go to the most recent change, then the id, so a diff is deterministic for a
given state, cursor and working set. Lines are added while they fit in max_tokens
with the closing "N more, use search" line reserved.
"""

from dataclasses import dataclass

from vlah.core.indexer.api import Diff, DiffItem, WorkingSet
from vlah.core.indexer.state import ClaimRecord
from vlah.core.types.ids import ActorId
from vlah.core.types.states import VERIFIED_PREFIX
from vlah.core.types.tokens import count_tokens

SUMMARY_CHARACTERS = 160


@dataclass(frozen=True, kw_only=True)
class Candidate:
    rank: int
    record: ClaimRecord


def rank_of(record: ClaimRecord, working_set: WorkingSet) -> int:
    claim = record.claim
    same_approach = claim.approach == working_set.approach
    if claim.id in working_set.target_claims or claim.id in working_set.recent_ids:
        return 0
    elif (
        claim.type == "result"
        and same_approach
        and record.status.startswith(VERIFIED_PREFIX)
    ):
        return 1
    elif claim.type == "dead_end" and same_approach:
        return 2
    elif claim.type == "insight" and not same_approach:
        return 3
    else:
        return 4


def summary_of(record: ClaimRecord) -> str:
    first_line = (
        record.claim.body.strip().splitlines()[0] if record.claim.body.strip() else ""
    )
    return first_line[:SUMMARY_CHARACTERS]


def line_of(item: DiffItem) -> str:
    return f"- [{item.status}] {item.item_id} ({item.approach}): {item.summary}"


def more_line(omitted_count: int) -> str:
    return f"{omitted_count} more, use search"


def build_diff(
    records: tuple[ClaimRecord, ...],
    cursor: int,
    head: int,
    working_set: WorkingSet,
    viewer: ActorId,
    max_tokens: int,
    results_only: bool,
) -> Diff:
    changed = [
        Candidate(rank=rank_of(record, working_set), record=record)
        for record in records
        if record.last_change is not None
        and record.last_change > cursor
        and record.claim.author != viewer
        and (record.claim.type == "result" or not results_only)
    ]
    ordered = sorted(
        changed,
        key=lambda candidate: (
            candidate.rank,
            -(candidate.record.last_change or 0),
            candidate.record.claim.id,
        ),
    )
    items = [
        DiffItem(
            item_id=candidate.record.claim.id,
            kind="claim",
            approach=candidate.record.claim.approach,
            status=candidate.record.status,
            summary=summary_of(candidate.record),
        )
        for candidate in ordered
    ]
    kept: list[DiffItem] = []
    lines: list[str] = []
    for index, item in enumerate(items):
        line = line_of(item)
        remaining = len(items) - index - 1
        closing = more_line(remaining) if remaining else ""
        if count_tokens("\n".join([*lines, line, closing])) <= max_tokens:
            kept.append(item)
            lines.append(line)
        else:
            break
    omitted_count = len(items) - len(kept)
    if omitted_count:
        lines.append(more_line(omitted_count))
    else:
        pass
    return Diff(
        items=tuple(kept),
        cursor=max(cursor, head),
        omitted_count=omitted_count,
        rendered="\n".join(lines),
    )
