"""What the eval client reads back from the blackboard: after a restart, and for remeasures.

Its own bookkeeping lives in memory; the blackboard is the record it can rebuild
from (D13). Only finished worker results are rebuilt into the dedup table, so a
candidate still in flight when the client stopped can be submitted again. The quota
is rebuilt from every result.json: each level the judge ran is charged again at the
time it was submitted. A resubmitted in-flight candidate comes back as a cache hit
from a judge that caches, so it is not charged twice.
"""

from vlah.core.blackboard.api import Blackboard
from vlah.core.eval.quota import QuotaTracker, RequestKind
from vlah.core.eval.records import EVAL_SERVICE, ResultFile
from vlah.core.types.claims import parse_claim

REMEASURE_TAG = "remeasure"
COMPOSE_TAG = "compose"
# Neither is ever the original a worker's duplicate points at.
INTEGRATOR_TAGS = (REMEASURE_TAG, COMPOSE_TAG)


def stored_result(blackboard: Blackboard, hash_value: str) -> ResultFile | None:
    path = f"candidates/{hash_value}/result.json"
    if path not in blackboard.list_paths(path, None):
        return None
    else:
        return ResultFile.model_validate_json(blackboard.read(path, None))


def has_measured_result(blackboard: Blackboard, hash_value: str) -> bool:
    result = stored_result(blackboard, hash_value)
    return result is not None and any(
        level.score is not None for level in result.levels
    )


def stored_level_score(
    blackboard: Blackboard, hash_value: str, level_id: str
) -> float | None:
    """The last measured score of a candidate at one level, from its result.json."""
    result = stored_result(blackboard, hash_value)
    scores = [
        level.score
        for level in (result.levels if result is not None else ())
        if level.level == level_id and level.score is not None
    ]
    return scores[-1] if scores else None


def measured_claims_by_hash(blackboard: Blackboard) -> dict[str, str]:
    """The dedup table after a restart: worker candidates with a measured result."""
    claims: dict[str, str] = {}
    for path in blackboard.list_paths(f"claims/{EVAL_SERVICE}/", None):
        claim = parse_claim(blackboard.read(path, None).decode("utf-8"))
        pointer = claim.evidence[0].candidate
        if (
            claim.type != "result"
            or any(tag in claim.tags for tag in INTEGRATOR_TAGS)
            or pointer is None
        ):
            continue
        else:
            hash_value = pointer.split("/")[1]
            if hash_value not in claims and has_measured_result(blackboard, hash_value):
                claims[hash_value] = claim.id
            else:
                pass
    return claims


def replay_recorded_runs(blackboard: Blackboard, quota: QuotaTracker) -> None:
    """Charge the quota for every judge run a result.json records, the Integrator's included.

    A hash has one result.json, so a duplicate that ran again without dedup (B1) is
    counted once, and a run after a judge failure not at all: the first result.json
    has no entry for the failed level, and the rerun's is never written.
    """
    for path in blackboard.list_paths("candidates/", None):
        # A payload file may be called result.json too; only the top-level one counts.
        if path.split("/")[1:] != [path.split("/")[1], "result.json"]:
            continue
        else:
            result = ResultFile.model_validate_json(blackboard.read(path, None))
            claim_path = f"claims/{EVAL_SERVICE}/{result.claim_id}.md"
            claim = parse_claim(blackboard.read(claim_path, None).decode("utf-8"))
            if REMEASURE_TAG in claim.tags:
                kind: RequestKind = "remeasure"
            elif COMPOSE_TAG in claim.tags:
                kind = "compose"
            else:
                kind = "worker"
            for level in result.levels:
                if level.is_cached:
                    continue
                else:
                    quota.record(
                        kind,
                        result.submitter,
                        level.level,
                        level.submitted_at_seconds,
                        level.dataset,
                    )
