"""L0-05a (v2.2 §14.1). Owner: W1-03 (#13).

result_ready → the follow-up task's for_agent is the submitter (D28).
"""

from pathlib import Path

import pytest

from tests.fixtures.herd import make_toy_herd
from vlah.arms.config import PRESETS

pytestmark = [pytest.mark.l0, pytest.mark.queue]


@pytest.mark.xfail(reason="not implemented yet; owned by W1-03 (#13)", strict=False)
def test_l0_05a_result_follow_up_goes_to_submitter(tmp_path: Path) -> None:
    """result_ready → the follow-up task's for_agent is the submitter (D28)."""
    # Owner: build the herd with your real component in place of its fake, script the
    # situation above, assert the outcome, and delete the xfail marker.
    make_toy_herd(tmp_path, PRESETS["S2"])
    raise NotImplementedError("W1-03 (#13) replaces this with the assertion")
