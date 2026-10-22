"""L0-19 (v2.2 §14.1). Owner: W1-11 (#21).

Arm fairness: the Integrator configuration is identical item by item across the four arms; whoever executes compose tasks is not in any arm's worker pool (D58). In this repo the Integrator settings live in Params, so the runtime must give every arm the same Params.
"""

from pathlib import Path

import pytest

from tests.fixtures.herd import make_toy_herd
from vlah.arms.config import PRESETS

pytestmark = [pytest.mark.l0, pytest.mark.runtime]


@pytest.mark.xfail(reason="not implemented yet; owned by W1-11 (#21)", strict=False)
def test_l0_19_arm_fairness(tmp_path: Path) -> None:
    """Arm fairness: the Integrator configuration is identical item by item across the four arms; whoever executes compose tasks is not in any arm's worker pool (D58). In this repo the Integrator settings live in Params, so the runtime must give every arm the same Params."""
    # Owner: build the herd with your real component in place of its fake, script the
    # situation above, assert the outcome, and delete the xfail marker.
    make_toy_herd(tmp_path, PRESETS["S2"])
    raise NotImplementedError("W1-11 (#21) replaces this with the assertion")
