"""L0-12 (v2.2 §14.1). Owner: W1-06 (#16).

The worker role reads private:// → denied; a worker pushes to the repository → denied (D63, D68; the in-memory adapters must simulate permissions too).
"""

from pathlib import Path

import pytest

from tests.fixtures.herd import make_toy_herd
from vlah.arms.config import PRESETS

pytestmark = [pytest.mark.l0, pytest.mark.tools]


@pytest.mark.xfail(reason="not implemented yet; owned by W1-06 (#16)", strict=False)
def test_l0_12_worker_permissions(tmp_path: Path) -> None:
    """The worker role reads private:// → denied; a worker pushes to the repository → denied (D63, D68; the in-memory adapters must simulate permissions too)."""
    # Owner: build the herd with your real component in place of its fake, script the
    # situation above, assert the outcome, and delete the xfail marker.
    make_toy_herd(tmp_path, PRESETS["S2"])
    raise NotImplementedError("W1-06 (#16) replaces this with the assertion")
