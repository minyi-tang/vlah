"""L0-14 (v2.2 §14.1). Owner: W1-05 (#15).

baseline_advanced's files_changed intersects the lease's files_touched → hard interrupt; no intersection → soft interrupt (§8 rule 6).
"""

from pathlib import Path

import pytest

from tests.fixtures.herd import make_toy_herd
from vlah.arms.config import PRESETS

pytestmark = [pytest.mark.l0, pytest.mark.router]


@pytest.mark.xfail(reason="not implemented yet; owned by W1-05 (#15)", strict=False)
def test_l0_14_rebase_interrupt_rule(tmp_path: Path) -> None:
    """baseline_advanced's files_changed intersects the lease's files_touched → hard interrupt; no intersection → soft interrupt (§8 rule 6)."""
    # Owner: build the herd with your real component in place of its fake, script the
    # situation above, assert the outcome, and delete the xfail marker.
    make_toy_herd(tmp_path, PRESETS["S2"])
    raise NotImplementedError("W1-05 (#15) replaces this with the assertion")
