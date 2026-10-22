"""L0-07 (v2.2 §14.1). Owner: W1-04 (#14).

gate fails → not counted in the score; writes dead_end reason=gate:<name> (D54).
"""

from pathlib import Path

import pytest

from tests.fixtures.herd import make_toy_herd
from vlah.arms.config import PRESETS

pytestmark = [pytest.mark.l0, pytest.mark.eval]


@pytest.mark.xfail(reason="not implemented yet; owned by W1-04 (#14)", strict=False)
def test_l0_07_gate_failure_records_dead_end(tmp_path: Path) -> None:
    """gate fails → not counted in the score; writes dead_end reason=gate:<name> (D54)."""
    # Owner: build the herd with your real component in place of its fake, script the
    # situation above, assert the outcome, and delete the xfail marker.
    make_toy_herd(tmp_path, PRESETS["S2"])
    raise NotImplementedError("W1-04 (#14) replaces this with the assertion")
