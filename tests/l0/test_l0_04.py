"""L0-04 (v2.2 §14.1). Owner: W1-04 (#14).

A second submission with the same baseline and the same payload → dedup_hit, and the quota is not decremented; changing only the baseline identifier changes the hash (D69, D73).
"""

from pathlib import Path

import pytest

from tests.fixtures.eval_harness import WORKER, make_eval_harness
from vlah.core.types.candidates import candidate_hash

pytestmark = [pytest.mark.l0, pytest.mark.eval]


def test_l0_04_dedup_and_baseline_hash(tmp_path: Path) -> None:
    """A second submission with the same baseline and the same payload → dedup_hit, and the quota is not decremented; changing only the baseline identifier changes the hash (D69, D73)."""
    harness = make_eval_harness(tmp_path)
    directory = harness.candidate(1.0)
    first = harness.service.request(harness.request(directory), WORKER)
    harness.run(seconds=3600)
    jobs_before = len(harness.sources[0].jobs)
    charges_before = len(harness.budget.records)

    again = harness.service.request(harness.request(directory), WORKER)
    harness.run(seconds=3600)
    assert again.status == "dedup_hit"
    assert again.claim_id == first.claim_id
    assert len(harness.sources[0].jobs) == jobs_before, "no judge run for a duplicate"
    assert len(harness.budget.records) == charges_before, "no quota for a duplicate"
    assert candidate_hash(directory, "baseline@0") != candidate_hash(
        directory, "baseline@1"
    )
