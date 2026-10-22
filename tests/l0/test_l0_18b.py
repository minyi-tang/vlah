"""L0-18b (v2.2 §14.1). Owner: W1-07 (#17).

The process is killed with kill -9 → the sidecar writes a stub claim on its behalf (D77).
"""

from pathlib import Path

import pytest

from tests.fixtures.herd import make_toy_herd
from vlah.arms.config import PRESETS

pytestmark = [pytest.mark.l0, pytest.mark.executor]


@pytest.mark.xfail(reason="not implemented yet; owned by W1-07 (#17)", strict=False)
def test_l0_18b_stub_claim_on_kill(tmp_path: Path) -> None:
    """The process is killed with kill -9 → the sidecar writes a stub claim on its behalf (D77)."""
    # Owner: build the herd with your real component in place of its fake, script the
    # situation above, assert the outcome, and delete the xfail marker.
    make_toy_herd(tmp_path, PRESETS["S2"])
    raise NotImplementedError("W1-07 (#17) replaces this with the assertion")
