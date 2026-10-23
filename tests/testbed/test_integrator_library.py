"""testbed.integrator used as a library, the way the herd's Integrator wraps it (issue #10, 3d)."""

import json
from pathlib import Path

import yaml

from testbed.integrator import (
    IntegratorConfig,
    IntegratorCore,
    Measurement,
    VerifiedCandidate,
)
from vlah.core.types.baseline import BaselineRecord

ALL = ["backfill", "admission", "locality"]


def write_candidate(candidate_dir: Path, ingredients) -> Path:
    candidate_dir.mkdir(parents=True, exist_ok=True)
    (candidate_dir / "params.yaml").write_text(
        yaml.safe_dump(
            {"ingredients": {ingredient: True for ingredient in ingredients}}
        )
    )
    return candidate_dir


def score_of(candidate_dir: Path) -> float:
    params_yaml = yaml.safe_load((candidate_dir / "params.yaml").read_text()) or {}
    return -1000.0 + 100.0 * sum(
        1 for enabled in (params_yaml.get("ingredients") or {}).values() if enabled
    )


class FakeMeasurer:
    """Measures synchronously; the herd's version goes through its EvalService."""

    def __init__(self):
        self.calls = []
        self.results = {}

    def submit(
        self, candidate_dir, baseline, dataset, note, parents, exempt, predicted
    ):
        ticket = f"m{len(self.calls)}"
        self.calls.append((dataset, baseline, note))
        score = score_of(Path(candidate_dir))
        self.results[ticket] = Measurement(
            ticket, f"h-{ticket}", [score, score], score, True
        )
        return ticket

    def result(self, ticket):
        return self.results.get(ticket)


def make_core(tmp_path, measurer, clock=lambda: 0.0):
    calibration = {
        "baseline_slow": {"per_seed": [-1000.0, -1000.0], "score": -1000.0},
        "baseline_audit": {"per_seed": [-1000.0, -1000.0], "score": -1000.0},
        "audit_sigma": {"dev": 0.0, "audit": 0.0},
    }
    return IntegratorCore(
        IntegratorConfig(),
        tmp_path / "wd",
        write_candidate(tmp_path / "base", []),
        calibration,
        clock,
        measurer,
    )


def verified(tmp_path, name, ingredients, target="baseline@0"):
    candidate_dir = write_candidate(tmp_path / name, ingredients)
    score = score_of(candidate_dir)
    return VerifiedCandidate(
        f"t-{name}",
        f"h-{name}",
        target,
        [score, score],
        score,
        candidate_dir,
        actor=name,
    )


def drain(core):
    decisions = []
    for _ in range(50):
        decisions += core.step()
        if core.task is None and not core.queue:
            break
    return decisions


def test_decisions_come_back_as_data_and_records_match_the_herd(tmp_path):
    measurer = FakeMeasurer()
    core = make_core(tmp_path, measurer)
    core.add(verified(tmp_path, "a", ["backfill"]))
    core.add(verified(tmp_path, "b", ["admission"]))
    decisions = drain(core)
    assert [
        decision["type"]
        for decision in decisions
        if decision["type"] == "baseline_advanced"
    ] == ["baseline_advanced"] * 2
    assert [call[0] for call in measurer.calls].count(
        "dev"
    ) == 1  # exactly one paired re-measure (baseline moved)
    assert core.current().score == -800.0
    for k in range(len(core.versions)):
        BaselineRecord.model_validate(
            json.loads((core.workdir / "baseline" / f"{k}.json").read_text())
        )
    assert (
        IntegratorConfig.from_json(IntegratorConfig().to_json()) == IntegratorConfig()
    )


def test_find_culprit_and_revert_primitives(tmp_path):
    measurer = FakeMeasurer()
    core = make_core(tmp_path, measurer)
    for name, ingredients in (
        ("a", ["backfill"]),
        ("b", ["admission"]),
        ("c", ["locality"]),
    ):
        k = core.k
        core.add(
            verified(tmp_path, name, [*ALL[:k], *ingredients], target=f"baseline@{k}")
        )
        drain(core)
    assert core.k == 3
    bad_hash = core.versions[2].added
    assert IntegratorCore.find_culprit(0, 3, lambda low_k, high_k: high_k >= 2) == 2
    version = core.revert(1, 2, "guard:demo")
    assert (
        bad_hash in version.removed
        and bad_hash not in version.candidates
        and version.k == 4
    )
    decisions = drain(core)
    assert any(decision["type"] == "baseline_reverted" for decision in decisions)
    BaselineRecord.model_validate(
        json.loads((core.workdir / "baseline" / "4.json").read_text())
    )


def test_compose_task_predicts_the_candidates_own_score(tmp_path):
    measurer = FakeMeasurer()
    core = make_core(tmp_path, measurer)
    first = write_candidate(tmp_path / "a", ["backfill"])
    (first / "knob.yaml").write_text("window: 10\n")
    core.add(
        VerifiedCandidate(
            "t-a", "h-a", "baseline@0", [-900.0, -900.0], -900.0, first, actor="a"
        )
    )
    drain(core)
    clash = write_candidate(tmp_path / "b", ["admission"])
    (clash / "knob.yaml").write_text("window: 20\n")
    core.add(
        VerifiedCandidate(
            "t-b", "h-b", "baseline@0", [-900.0, -900.0], -900.0, clash, actor="b"
        )
    )
    tasks = [decision for decision in drain(core) if decision["type"] == "compose_task"]
    assert len(tasks) == 1 and tasks[0]["predicted"] == -900.0
