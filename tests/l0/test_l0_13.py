"""L0-13 (v2.2 §14.1). Owner: W1-04 (#14).

result_ready carries baseline, paired gain, standard error, n_items, n_reps; n_reps does not count cache hits (D73).
"""

from pathlib import Path

import pytest

from tests.fixtures.herd import make_toy_herd
from vlah.arms.config import PRESETS

pytestmark = [pytest.mark.l0, pytest.mark.eval]


@pytest.mark.xfail(reason="not implemented yet; owned by W1-04 (#14)", strict=False)
def test_l0_13_result_ready_fields(tmp_path: Path) -> None:
    """result_ready carries baseline, paired gain, standard error, n_items, n_reps; n_reps does not count cache hits (D73)."""
    # Owner: build the herd with your real component in place of its fake, script the
    # situation above, assert the outcome, and delete the xfail marker.
    make_toy_herd(tmp_path, PRESETS["S2"])
    raise NotImplementedError("W1-04 (#14) replaces this with the assertion")
