"""L0-05b (v2.2 §14.1). Owner: W1-05 (#15).

When the submitter holds a lease, result_ready writes interrupt:<agent> (D28).
"""

from pathlib import Path

import pytest

from tests.fixtures.herd import make_toy_herd
from vlah.arms.config import PRESETS

pytestmark = [pytest.mark.l0, pytest.mark.router]


@pytest.mark.xfail(reason="not implemented yet; owned by W1-05 (#15)", strict=False)
def test_l0_05b_result_interrupts_a_leased_submitter(tmp_path: Path) -> None:
    """When the submitter holds a lease, result_ready writes interrupt:<agent> (D28)."""
    # Owner: build the herd with your real component in place of its fake, script the
    # situation above, assert the outcome, and delete the xfail marker.
    make_toy_herd(tmp_path, PRESETS["S2"])
    raise NotImplementedError("W1-05 (#15) replaces this with the assertion")
