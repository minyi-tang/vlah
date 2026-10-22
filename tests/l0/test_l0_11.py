"""L0-11 (v2.2 §14.1). Owner: W1-09 (#19).

Integrator: textual conflict → issues a compose task; no conflict but the baseline has moved → exactly one paired remeasure; gain not above 1 standard error → integration_failed, and the author gets a follow-up task; under the B1 configuration, workers do not receive baseline_advanced (D74, D58).
"""

from pathlib import Path

import pytest

from tests.fixtures.herd import make_toy_herd
from vlah.arms.config import PRESETS

pytestmark = [pytest.mark.l0, pytest.mark.integrator]


@pytest.mark.xfail(reason="not implemented yet; owned by W1-09 (#19)", strict=False)
def test_l0_11_integrator_merge_rules(tmp_path: Path) -> None:
    """Integrator: textual conflict → issues a compose task; no conflict but the baseline has moved → exactly one paired remeasure; gain not above 1 standard error → integration_failed, and the author gets a follow-up task; under the B1 configuration, workers do not receive baseline_advanced (D74, D58)."""
    # Owner: build the herd with your real component in place of its fake, script the
    # situation above, assert the outcome, and delete the xfail marker.
    make_toy_herd(tmp_path, PRESETS["S2"])
    raise NotImplementedError("W1-09 (#19) replaces this with the assertion")
