"""L0-17 (v2.2 §14.1). Owner: W1-12 (#22).

A worker sandbox accesses a host outside the allowlist → denied (D75).
"""

from pathlib import Path

import pytest

from tests.fixtures.herd import make_toy_herd
from vlah.arms.config import PRESETS

pytestmark = [pytest.mark.l0, pytest.mark.egress]


@pytest.mark.xfail(reason="not implemented yet; owned by W1-12 (#22)", strict=False)
def test_l0_17_egress_allowlist(tmp_path: Path) -> None:
    """A worker sandbox accesses a host outside the allowlist → denied (D75)."""
    # Owner: build the herd with your real component in place of its fake, script the
    # situation above, assert the outcome, and delete the xfail marker.
    make_toy_herd(tmp_path, PRESETS["S2"])
    raise NotImplementedError("W1-12 (#22) replaces this with the assertion")
