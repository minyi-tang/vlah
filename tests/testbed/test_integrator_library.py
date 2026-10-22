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


def write(d: Path, ingredients) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    (d / "params.yaml").write_text(
        yaml.safe_dump({"ingredients": {k: True for k in ingredients}})
    )
    return d


def score_of(d: Path) -> float:
    prm = yaml.safe_load((d / "params.yaml").read_text()) or {}
    return -1000.0 + 100.0 * sum(
        1 for v in (prm.get("ingredients") or {}).values() if v
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
        s = score_of(Path(candidate_dir))
        self.results[ticket] = Measurement(ticket, f"h-{ticket}", [s, s], s, True)
        return ticket

    def result(self, ticket):
        return self.results.get(ticket)


def core(tmp_path, measurer, clock=lambda: 0.0):
    calibration = {
        "baseline_slow": {"per_seed": [-1000.0, -1000.0], "score": -1000.0},
        "baseline_audit": {"per_seed": [-1000.0, -1000.0], "score": -1000.0},
        "audit_sigma": {"dev": 0.0, "audit": 0.0},
    }
    return IntegratorCore(
        IntegratorConfig(),
        tmp_path / "wd",
        write(tmp_path / "base", []),
        calibration,
        clock,
        measurer,
    )


def verified(tmp_path, name, ingredients, target="baseline@0"):
    d = write(tmp_path / name, ingredients)
    s = score_of(d)
    return VerifiedCandidate(f"t-{name}", f"h-{name}", target, [s, s], s, d, actor=name)


def drain(c):
    out = []
    for _ in range(50):
        out += c.step()
        if c.task is None and not c.queue:
            break
    return out


def test_decisions_come_back_as_data_and_records_match_the_herd(tmp_path):
    m = FakeMeasurer()
    c = core(tmp_path, m)
    c.add(verified(tmp_path, "a", ["backfill"]))
    c.add(verified(tmp_path, "b", ["admission"]))
    decisions = drain(c)
    assert [d["type"] for d in decisions if d["type"] == "baseline_advanced"] == [
        "baseline_advanced"
    ] * 2
    assert [x[0] for x in m.calls].count(
        "dev"
    ) == 1  # exactly one paired re-measure (baseline moved)
    assert c.current().score == -800.0
    for k in range(len(c.versions)):
        BaselineRecord.model_validate(
            json.loads((c.wd / "baseline" / f"{k}.json").read_text())
        )
    assert (
        IntegratorConfig.from_json(IntegratorConfig().to_json()) == IntegratorConfig()
    )


def test_find_culprit_and_revert_primitives(tmp_path):
    m = FakeMeasurer()
    c = core(tmp_path, m)
    for name, ing in (("a", ["backfill"]), ("b", ["admission"]), ("c", ["locality"])):
        k = c.k
        c.add(verified(tmp_path, name, [*ALL[:k], *ing], target=f"baseline@{k}"))
        drain(c)
    assert c.k == 3
    bad = c.versions[2].added
    assert IntegratorCore.find_culprit(0, 3, lambda lo, hi: hi >= 2) == 2
    v = c.revert(1, 2, "guard:demo")
    assert bad in v.removed and bad not in v.candidates and v.k == 4
    decisions = drain(c)
    assert any(d["type"] == "baseline_reverted" for d in decisions)
    BaselineRecord.model_validate(
        json.loads((c.wd / "baseline" / "4.json").read_text())
    )


def test_compose_task_predicts_the_candidates_own_score(tmp_path):
    m = FakeMeasurer()
    c = core(tmp_path, m)
    first = write(tmp_path / "a", ["backfill"])
    (first / "knob.yaml").write_text("window: 10\n")
    c.add(
        VerifiedCandidate(
            "t-a", "h-a", "baseline@0", [-900.0, -900.0], -900.0, first, actor="a"
        )
    )
    drain(c)
    clash = write(tmp_path / "b", ["admission"])
    (clash / "knob.yaml").write_text("window: 20\n")
    c.add(
        VerifiedCandidate(
            "t-b", "h-b", "baseline@0", [-900.0, -900.0], -900.0, clash, actor="b"
        )
    )
    tasks = [d for d in drain(c) if d["type"] == "compose_task"]
    assert len(tasks) == 1 and tasks[0]["predicted"] == -900.0
