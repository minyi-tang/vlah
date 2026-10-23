"""Automatic corroboration: an insight cited by two results verified at the top level (D9).

The indexer writes one claim_status event, proposed to corroborated, as the actor
indexer. It never writes it twice: it checks the replayed status first, and a
duplicate that slips through after a restart is rejected on replay as "unchanged".
A write the blackboard rejects is reported so the indexer tries again at its next
merge.
"""

import json
import logging

from vlah.core.blackboard.api import Blackboard
from vlah.core.indexer.state import ReplayState
from vlah.core.types.clock import Clock
from vlah.core.types.ids import ActorId, format_event_local_id
from vlah.core.types.states import VERIFIED_PREFIX

logger = logging.getLogger(__name__)

INDEXER = ActorId("indexer")
CORROBORATING_RESULTS = 2


def corroborating_results(
    state: ReplayState, insight_id: str, top_level: str | None
) -> int:
    """Results verified at the top level that build on the insight."""
    if top_level is None:
        return 0
    else:
        verified = f"{VERIFIED_PREFIX}{top_level}"
        return sum(
            1
            for citing in state.citations_of(insight_id)
            if (record := state.claims.get(citing)) is not None
            and record.claim.type == "result"
            and record.status == verified
        )


def write_corroboration(
    blackboard: Blackboard, clock: Clock, insight_id: str, evidence: tuple[str, ...]
) -> bool:
    """Write the event; False when the blackboard rejected it."""
    number = len(blackboard.list_paths(f"events/{INDEXER}/", None)) + 1
    local_id = format_event_local_id(INDEXER, number)
    event = {
        "local_id": local_id,
        "ts": clock.now_seconds(),
        "actor": INDEXER,
        "role": "indexer",
        "type": "claim_status",
        "target": insight_id,
        "from": "proposed",
        "to": "corroborated",
        "reason": "cited by two results verified at the top level (D9)",
        "evidence": list(evidence),
    }
    receipt = blackboard.write(
        INDEXER,
        {f"events/{local_id}.json": json.dumps(event).encode("utf-8")},
        f"corroborate {insight_id}",
    )
    if receipt.status == "rejected":
        logger.error(f"could not corroborate {insight_id}: {receipt.violations}")
        return False
    else:
        return True
