"""L0-16 (v2.2 §14.1). Owner: W1-09 (#19).

Spot check: a scripted overfitting candidate is flagged, reverted, and set to quarantined; in a clean scripted run, none of the 7 spot checks raises an alarm (D72).
"""

from pathlib import Path

import pytest

from tests.fixtures.herd import make_toy_herd
from vlah.arms.config import PRESETS

pytestmark = [pytest.mark.l0, pytest.mark.integrator]


@pytest.mark.xfail(reason="not implemented yet; owned by W1-09 (#19)", strict=False)
def test_l0_16_spot_check(tmp_path: Path) -> None:
    """Spot check: a scripted overfitting candidate is flagged, reverted, and set to quarantined; in a clean scripted run, none of the 7 spot checks raises an alarm (D72)."""
    # Owner: build the herd with your real component in place of its fake, script the
    # situation above, assert the outcome, and delete the xfail marker.
    make_toy_herd(tmp_path, PRESETS["S2"])
    raise NotImplementedError("W1-09 (#19) replaces this with the assertion")
