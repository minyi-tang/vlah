"""L0-13 (v2.2 §14.1). Owner: W1-04 (#14).

result_ready carries baseline, paired gain, standard error, n_items, n_reps; n_reps does not count cache hits (D73).
"""

from pathlib import Path

import pytest

from tests.fixtures.eval_harness import WORKER, make_eval_harness
from vlah.arms.config import PRESETS
from vlah.core.types.events import ResultReady

pytestmark = [pytest.mark.l0, pytest.mark.eval]


def test_l0_13_result_ready_fields(tmp_path: Path) -> None:
    """result_ready carries baseline, paired gain, standard error, n_items, n_reps; n_reps does not count cache hits (D73)."""
    # B1 has no hash dedup, so the repeat reaches the judge, which answers from its cache.
    harness = make_eval_harness(tmp_path, arm=PRESETS["B1"])
    directory = harness.candidate(2.0)
    harness.service.request(harness.request(directory), WORKER)
    harness.service.request(harness.request(directory), WORKER)
    events = harness.run(seconds=3600)
    result, repeat = [
        event.data for event in events if isinstance(event.data, ResultReady)
    ]
    assert result.baseline == "baseline@0"
    assert result.paired is not None, "a measured result is paired"
    assert result.paired.mean == 2.0, "candidate minus baseline, seed by seed"
    assert result.paired.se == 0.0
    assert result.paired.n_items == 5
    assert (
        result.paired.n_reps == 1
    ), "the stored baseline is a cache hit and does not count"
    assert repeat.paired is not None
    assert repeat.paired.mean == 2.0, "a cache hit is still a measurement"
    assert repeat.paired.n_reps == 0, "the judge did not run the repeat"
