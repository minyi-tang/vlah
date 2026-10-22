"""L0-13 (v2.2 §14.1). Owner: W1-04 (#14).

result_ready carries baseline, paired gain, standard error, n_items, n_reps; n_reps does not count cache hits (D73).
"""

from pathlib import Path

import pytest

from tests.fixtures.eval_harness import WORKER, make_eval_harness
from vlah.core.types.events import ResultReady

pytestmark = [pytest.mark.l0, pytest.mark.eval]


def test_l0_13_result_ready_fields(tmp_path: Path) -> None:
    """result_ready carries baseline, paired gain, standard error, n_items, n_reps; n_reps does not count cache hits (D73)."""
    harness = make_eval_harness(tmp_path)
    harness.service.request(harness.request(harness.candidate(2.0)), WORKER)
    events = harness.run(seconds=3600)
    (result,) = [event.data for event in events if isinstance(event.data, ResultReady)]
    assert result.baseline == "baseline@0"
    assert result.paired.mean == 2.0, "candidate minus baseline, seed by seed"
    assert result.paired.se == 0.0
    assert result.paired.n_items == 5
    assert (
        result.paired.n_reps == 1
    ), "the stored baseline is a cache hit and does not count"
