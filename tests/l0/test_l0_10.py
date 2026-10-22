"""L0-10 (v2.2 §14.1). Owner: W1-11 (#21).

Arm switch table: under the B1 configuration, search and diff_since return empty, and routing rules 2 to 5 do not fire; S2 has everything on (D58).
"""

from pathlib import Path

import pytest

from tests.fixtures.herd import make_toy_herd
from vlah.arms.config import PRESETS

pytestmark = [pytest.mark.l0, pytest.mark.runtime]


@pytest.mark.xfail(reason="not implemented yet; owned by W1-11 (#21)", strict=False)
def test_l0_10_arm_switches(tmp_path: Path) -> None:
    """Arm switch table: under the B1 configuration, search and diff_since return empty, and routing rules 2 to 5 do not fire; S2 has everything on (D58)."""
    # Owner: build the herd with your real component in place of its fake, script the
    # situation above, assert the outcome, and delete the xfail marker.
    make_toy_herd(tmp_path, PRESETS["S2"])
    raise NotImplementedError("W1-11 (#21) replaces this with the assertion")
