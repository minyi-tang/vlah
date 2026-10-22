"""L0-08 (v2.2 §14.1). Owner: W1-03 (#13).

lease TTL expires → the task goes back to the queue once and only once (D32).
"""

from pathlib import Path

import pytest

from tests.fixtures.herd import make_toy_herd
from vlah.arms.config import PRESETS

pytestmark = [pytest.mark.l0, pytest.mark.queue]


@pytest.mark.xfail(reason="not implemented yet; owned by W1-03 (#13)", strict=False)
def test_l0_08_stale_lease_requeues_once(tmp_path: Path) -> None:
    """lease TTL expires → the task goes back to the queue once and only once (D32)."""
    # Owner: build the herd with your real component in place of its fake, script the
    # situation above, assert the outcome, and delete the xfail marker.
    make_toy_herd(tmp_path, PRESETS["S2"])
    raise NotImplementedError("W1-03 (#13) replaces this with the assertion")
