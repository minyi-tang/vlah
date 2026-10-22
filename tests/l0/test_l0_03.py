"""L0-03 (v2.2 §14.1). Owner: W1-04 (#14).

eval_request is two-phase: similar candidates exist → needs_confirmation; call again with the token → queued, and the result claim has already been created (D57).
"""

from pathlib import Path

import pytest

from tests.fixtures.herd import make_toy_herd
from vlah.arms.config import PRESETS

pytestmark = [pytest.mark.l0, pytest.mark.eval]


@pytest.mark.xfail(reason="not implemented yet; owned by W1-04 (#14)", strict=False)
def test_l0_03_eval_request_is_two_phase(tmp_path: Path) -> None:
    """eval_request is two-phase: similar candidates exist → needs_confirmation; call again with the token → queued, and the result claim has already been created (D57)."""
    # Owner: build the herd with your real component in place of its fake, script the
    # situation above, assert the outcome, and delete the xfail marker.
    make_toy_herd(tmp_path, PRESETS["S2"])
    raise NotImplementedError("W1-04 (#14) replaces this with the assertion")
