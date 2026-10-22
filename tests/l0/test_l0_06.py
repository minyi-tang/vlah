"""L0-06 (v2.2 §14.1). Owner: W1-02 (#12).

A single-source refuted does not cascade; a Critic-sourced one cascades to depends_on, and reinstated undoes it (D34).
"""

from pathlib import Path

import pytest

from tests.fixtures.herd import make_toy_herd
from vlah.arms.config import PRESETS

pytestmark = [pytest.mark.l0, pytest.mark.indexer]


@pytest.mark.xfail(reason="not implemented yet; owned by W1-02 (#12)", strict=False)
def test_l0_06_refuted_cascade_rules(tmp_path: Path) -> None:
    """A single-source refuted does not cascade; a Critic-sourced one cascades to depends_on, and reinstated undoes it (D34)."""
    # Owner: build the herd with your real component in place of its fake, script the
    # situation above, assert the outcome, and delete the xfail marker.
    make_toy_herd(tmp_path, PRESETS["S2"])
    raise NotImplementedError("W1-02 (#12) replaces this with the assertion")
