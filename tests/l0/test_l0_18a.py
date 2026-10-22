"""L0-18a (v2.2 §14.1). Owner: W1-08 (#18).

Context usage goes over 70% → write a claim and start a new session with the start pack (D77).
"""

from pathlib import Path

import pytest

from tests.fixtures.herd import make_toy_herd
from vlah.arms.config import PRESETS

pytestmark = [pytest.mark.l0, pytest.mark.harness]


@pytest.mark.xfail(reason="not implemented yet; owned by W1-08 (#18)", strict=False)
def test_l0_18a_context_restart(tmp_path: Path) -> None:
    """Context usage goes over 70% → write a claim and start a new session with the start pack (D77)."""
    # Owner: build the herd with your real component in place of its fake, script the
    # situation above, assert the outcome, and delete the xfail marker.
    make_toy_herd(tmp_path, PRESETS["S2"])
    raise NotImplementedError("W1-08 (#18) replaces this with the assertion")
