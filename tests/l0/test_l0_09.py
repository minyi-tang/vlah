"""L0-09 (v2.2 §14.1). Owner: W1-10 (#20).

Budget meter accumulates to the cap → stop event (D52).
"""

from pathlib import Path

import pytest

from tests.fixtures.herd import make_toy_herd
from vlah.arms.config import PRESETS

pytestmark = [pytest.mark.l0, pytest.mark.budget]


@pytest.mark.xfail(reason="not implemented yet; owned by W1-10 (#20)", strict=False)
def test_l0_09_budget_cap_stops(tmp_path: Path) -> None:
    """Budget meter accumulates to the cap → stop event (D52)."""
    # Owner: build the herd with your real component in place of its fake, script the
    # situation above, assert the outcome, and delete the xfail marker.
    make_toy_herd(tmp_path, PRESETS["S2"])
    raise NotImplementedError("W1-10 (#20) replaces this with the assertion")
