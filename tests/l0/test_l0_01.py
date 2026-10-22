"""L0-01 (v2.2 §14.1). Owner: W1-03 (#13).

claim_task selection order: for_agent first, then priority, then the approach with the largest target_slots gap (D55).
"""

from pathlib import Path

import pytest

from tests.fixtures.herd import make_toy_herd
from vlah.arms.config import PRESETS

pytestmark = [pytest.mark.l0, pytest.mark.queue]


@pytest.mark.xfail(reason="not implemented yet; owned by W1-03 (#13)", strict=False)
def test_l0_01_claim_task_order(tmp_path: Path) -> None:
    """claim_task selection order: for_agent first, then priority, then the approach with the largest target_slots gap (D55)."""
    # Owner: build the herd with your real component in place of its fake, script the
    # situation above, assert the outcome, and delete the xfail marker.
    make_toy_herd(tmp_path, PRESETS["S2"])
    raise NotImplementedError("W1-03 (#13) replaces this with the assertion")
