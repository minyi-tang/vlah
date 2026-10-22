"""The start pack by arm follows the D58 table; arms differ only in what they share."""

import json
from pathlib import Path

import pytest
import yaml

from tests.fixtures.worker import TASK, WORKER, make_worker_harness
from vlah.arms.config import PRESETS, ArmName
from vlah.core.types.claims import Claim, EvidenceRef, render_claim
from vlah.core.types.ids import ActorId
from vlah.core.worker.start_pack import build_start_pack

pytestmark = pytest.mark.harness

OTHER = ActorId("worker-02")
HEADINGS_BY_ARM: dict[ArmName, list[str]] = {
    "B0": ["Your task", "spec.md", "charter/constraints.md", "Your notes"],
    "B1": ["Your task", "spec.md", "charter/constraints.md", "Your claims"],
    "S1": [
        "Your task",
        "spec.md",
        "charter/constraints.md",
        "Your claims",
        "Every candidate's scores",
    ],
    "S2": [
        "Your task",
        "spec.md",
        "charter/constraints.md",
        "Digest of a-01",
        "What changed since you last looked",
    ],
}


def claim_file(author: ActorId, body: str) -> dict[str, bytes]:
    claim = Claim(
        id=f"c-{author}-00001",
        type="insight",
        status="proposed",
        approach="a-01",
        author=author,
        evidence=(EvidenceRef(path="spec.md"),),
        body=body,
    )
    return {f"claims/{author}/{claim.id}.md": render_claim(claim).encode()}


@pytest.mark.parametrize("arm", ["B0", "B1", "S1", "S2"])
def test_each_arm_gets_its_start_pack(tmp_path: Path, arm: ArmName) -> None:
    worker = make_worker_harness(tmp_path, PRESETS[arm], {}, "none")
    blackboard = worker.blackboard
    human = ActorId("human")
    blackboard.write(human, {"charter/constraints.md": b"Stay under 1 GB."}, "c")
    blackboard.write(WORKER, claim_file(WORKER, "My own finding."), "mine")
    blackboard.write(OTHER, claim_file(OTHER, "Someone else's finding."), "theirs")
    manifest = {"kind": "patch", "target": "baseline@0", "approach": "a-01"}
    blackboard.write(
        OTHER,
        {
            "candidates/abc123/manifest.yaml": yaml.safe_dump(
                {**manifest, "author": OTHER, "created_at": 0.0}
            ).encode()
        },
        "candidate",
    )
    result = {
        "candidate": "abc123",
        "levels": [{"level": "slow", "score": 0.42, "n": 5, "status": "verified:slow"}],
    }
    blackboard.write(
        ActorId("eval_service"),
        {"candidates/abc123/result.json": json.dumps(result).encode()},
        "result",
    )
    blackboard.write(ActorId("librarian-1"), {"digests/a-01.md": b"Digest text."}, "d")

    start_pack, cursor = build_start_pack(WORKER, TASK, worker.deps.context, 3)
    context = start_pack.files["CONTEXT.md"]
    headings = [line[3:] for line in context.splitlines() if line.startswith("## ")]
    assert headings == HEADINGS_BY_ARM[arm]
    assert "Make the solver faster." in context and "Stay under 1 GB." in context
    assert "Someone else's finding." not in context, "no arm pastes others' claims"
    assert ("My own finding." in context) is (arm != "S2")
    assert ("| abc123 | a-01 | slow | 0.42 | 5 | verified:slow |" in context) is (
        arm == "S1"
    )
    assert ("news since 3" in context and "Digest text." in context) is (arm == "S2")
    assert cursor == (10 if arm == "S2" else 3), "only S2 moves the diff cursor"
