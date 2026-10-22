"""L0-15 (v2.2 §14.1). Owner: W1-09 (#19).

A guard batch fails → bisect, write a new baseline version that excludes the culprit, and leave old records unchanged; the deliverable is never guard_pending (D74).
"""

from pathlib import Path

import pytest

from tests.fixtures.herd import make_toy_herd
from vlah.arms.config import PRESETS

pytestmark = [pytest.mark.l0, pytest.mark.integrator]


@pytest.mark.xfail(reason="not implemented yet; owned by W1-09 (#19)", strict=False)
def test_l0_15_guard_batch_bisects(tmp_path: Path) -> None:
    """A guard batch fails → bisect, write a new baseline version that excludes the culprit, and leave old records unchanged; the deliverable is never guard_pending (D74)."""
    # Owner: build the herd with your real component in place of its fake, script the
    # situation above, assert the outcome, and delete the xfail marker.
    make_toy_herd(tmp_path, PRESETS["S2"])
    raise NotImplementedError("W1-09 (#19) replaces this with the assertion")
