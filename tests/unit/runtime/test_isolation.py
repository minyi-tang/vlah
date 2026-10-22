"""Physical isolation: what an S1 or a B1 worker's clone holds (D58, D67)."""

from pathlib import Path

import pytest
import yaml

from tests.fixtures.herd import make_store_blackboard
from vlah.arms.config import PRESETS, ArmName
from vlah.core.blackboard.layout import open_blackboard
from vlah.core.types.claims import Claim, EvidenceRef, render_claim
from vlah.core.types.clock import FakeClock
from vlah.core.types.ids import ActorId
from vlah.runtime.isolation import worker_view

pytestmark = pytest.mark.runtime

ME = ActorId("worker-01")
OTHER = ActorId("worker-02")


def claim_files(author: ActorId) -> dict[str, bytes]:
    claim = Claim(
        id=f"c-{author}-00001",
        type="insight",
        status="proposed",
        approach="a-01",
        author=author,
        evidence=(EvidenceRef(path="spec.md"),),
        body=f"{author}'s finding",
    )
    return {f"claims/{author}/{claim.id}.md": render_claim(claim).encode()}


def manifest(author: ActorId) -> bytes:
    return yaml.safe_dump(
        {
            "kind": "patch",
            "target": "baseline@0",
            "approach": "a-01",
            "author": author,
            "created_at": 0.0,
        }
    ).encode()


@pytest.mark.parametrize("store", ["memory", "git"])
@pytest.mark.parametrize("arm", ["S1", "B1", "S2"])
def test_a_workers_clone_holds_only_what_its_arm_shares(
    tmp_path: Path, arm: ArmName, store: str
) -> None:
    """On the git store the B1 view reads manifests from inside merge delivery."""
    blackboard = (
        make_store_blackboard(FakeClock(), name="iso")
        if store == "memory"
        else open_blackboard(tmp_path, "r-1", "blackboard")
    )
    blackboard.write(ActorId("human"), {"spec.md": b"spec"}, "spec")
    views = {
        agent: worker_view(blackboard, PRESETS[arm], "r-1", agent)
        for agent in (ME, OTHER)
    }
    for agent in (ME, OTHER):
        blackboard.write(agent, claim_files(agent), "claim")
        blackboard.write(
            agent, {f"candidates/{agent[-2:]}aa/manifest.yaml": manifest(agent)}, "c"
        )
    mine = set(views[ME].list_paths("", None))
    assert "spec.md" in mine and "claims/worker-01/c-worker-01-00001.md" in mine
    assert "candidates/01aa/manifest.yaml" in mine
    sees_their_claims = "claims/worker-02/c-worker-02-00001.md" in mine
    sees_their_candidates = "candidates/02aa/manifest.yaml" in mine
    assert sees_their_claims is (arm == "S2"), "only S2 shares claims"
    assert sees_their_candidates is (arm != "B1"), "S1 shares candidates and scores"
    assert (views[ME] is blackboard) is (arm == "S2"), "S2 clones the run's blackboard"
