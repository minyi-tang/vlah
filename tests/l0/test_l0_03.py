"""L0-03 (v2.2 §14.1). Owner: W1-04 (#14).

eval_request is two-phase: similar candidates exist → needs_confirmation; call again with the token → queued, and the result claim has already been created (D57).
"""

from pathlib import Path

import pytest

from tests.fixtures.eval_harness import WORKER, make_eval_harness
from vlah.core.indexer.api import Hit

pytestmark = [pytest.mark.l0, pytest.mark.eval]


def test_l0_03_eval_request_is_two_phase(tmp_path: Path) -> None:
    """eval_request is two-phase: similar candidates exist → needs_confirmation; call again with the token → queued, and the result claim has already been created (D57)."""
    harness = make_eval_harness(tmp_path)
    harness.indexer.candidate_hits = (
        Hit(item_id="c-similar", kind="candidate", similarity=0.95, summary="close"),
    )
    directory = harness.candidate(1.0)
    first = harness.service.request(harness.request(directory), WORKER)
    assert first.status == "needs_confirmation"
    assert [item.item_id for item in first.similar] == ["c-similar"]
    assert first.claim_id is None

    second = harness.service.request(
        harness.request(directory, confirm_token=first.confirm_token), WORKER
    )
    assert second.status == "queued"
    assert second.claim_id is not None
    claim_path = f"claims/eval_service/{second.claim_id}.md"
    assert claim_path in harness.blackboard.list_paths(
        "claims/", None
    ), "the result claim exists as soon as the request is queued"
