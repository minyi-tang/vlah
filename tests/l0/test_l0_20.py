"""L0-20 (v2.2 §14.1). Owner: W1-09 (#19).

Closeout order: budget exhausted → stop accepting → guard_pending catch-up check → final spot check → freeze the deliverable; the deliverable is not guard_pending (D74, D79).
"""

from pathlib import Path

import pytest

from tests.fixtures.herd import make_toy_herd
from vlah.arms.config import PRESETS

pytestmark = [pytest.mark.l0, pytest.mark.integrator]


@pytest.mark.xfail(reason="not implemented yet; owned by W1-09 (#19)", strict=False)
def test_l0_20_closeout_order(tmp_path: Path) -> None:
    """Closeout order: budget exhausted → stop accepting → guard_pending catch-up check → final spot check → freeze the deliverable; the deliverable is not guard_pending (D74, D79)."""
    # Owner: build the herd with your real component in place of its fake, script the
    # situation above, assert the outcome, and delete the xfail marker.
    make_toy_herd(tmp_path, PRESETS["S2"])
    raise NotImplementedError("W1-09 (#19) replaces this with the assertion")
