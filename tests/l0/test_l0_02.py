"""L0-02 (v2.2 §14.1). Owner: W1-02 (#12).

Write one claim → merging produces a seq → the Indexer replays from zero twice, and the state is exactly the same both times (D56).
"""

from pathlib import Path

import pytest

from tests.fixtures.herd import make_toy_herd
from vlah.arms.config import PRESETS

pytestmark = [pytest.mark.l0, pytest.mark.indexer]


@pytest.mark.xfail(reason="not implemented yet; owned by W1-02 (#12)", strict=False)
def test_l0_02_replay_is_deterministic(tmp_path: Path) -> None:
    """Write one claim → merging produces a seq → the Indexer replays from zero twice, and the state is exactly the same both times (D56)."""
    # Owner: build the herd with your real component in place of its fake, script the
    # situation above, assert the outcome, and delete the xfail marker.
    make_toy_herd(tmp_path, PRESETS["S2"])
    raise NotImplementedError("W1-02 (#12) replaces this with the assertion")
