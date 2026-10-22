"""Stub problem for the scripted self-tests (spec 11.1, 11.9). No simulation: the score is a
fixed function of the ingredients a candidate declares.

Candidate = a directory with params.yaml:
    ingredients: {backfill: true, ...}     # declared ingredients (the integrator merges these key-wise)
    dev_only_bonus: 300                    # optional: extra score on dev seeds only (an overfit candidate)
    public_only_bonus: 0                   # optional: extra score on public seeds only (fast-tier overfit)
    crash: false                           # optional: evaluation fails (score None)
    bonus: {name: 10, ...}                 # optional: generic named improvements (summed; integrator tests)
Ingredients may also be listed in manifest.yaml under `ingredients` (union with params.yaml).

score = -1000 + 100 * (number of declared ingredients)
        - 200 if both backfill and fairshare are on (the designed conflict: together worse than either alone)
        + optional bonuses + optional deterministic noise (stub.yaml noise_sd, default 0)
constraint_ok = false when trap_sjf is declared. reference = the five ingredients on.
"""

from __future__ import annotations

import hashlib
import random
from pathlib import Path

import yaml

from testbed.problem_api import STATE_DIR, bootstrap_ci, content_hash, mean

ROOT = Path(__file__).resolve().parent
ALL = [
    "backfill",
    "admission",
    "locality",
    "packing",
    "est_order",
    "trap_sjf",
    "fairshare",
]


def _declared(candidate_dir: Path) -> tuple[set, dict]:
    p = Path(candidate_dir) / "params.yaml"
    prm = yaml.safe_load(p.read_text()) if p.exists() else {}
    prm = prm or {}
    ings = {k for k, v in (prm.get("ingredients") or {}).items() if v}
    m = Path(candidate_dir) / "manifest.yaml"
    if m.exists():
        man = yaml.safe_load(m.read_text()) or {}
        ings |= set(man.get("ingredients") or [])
    return {i for i in ings if i in ALL}, prm


class StubProblem:
    name = "stub"

    def __init__(self, root: Path):
        self.root = Path(root)
        # Nothing here is secret: the stub's seeds and "reference" are a formula, so they are committed
        # as stub.yaml and generated (reference/ would trip the hidden-material guard, issue #10).
        self.cfg = yaml.safe_load((self.root / "stub.yaml").read_text())
        self.ingredients = list(self.cfg.get("ingredients", ALL))
        self.public_seeds = [1, 2, 3, 4, 5]
        self.noise_sd = float(self.cfg.get("noise_sd", 0.0))

    def knobs(self):
        return dict(self.cfg.get("knobs", {}))

    def seeds(self, dataset):
        if dataset == "public":
            return list(self.public_seeds)
        return list(self.cfg["seeds"][dataset])

    def baseline_dir(self):
        return self.root / "policy"

    def reference_dir(self):
        d = STATE_DIR / "stub" / "five-ingredients"
        if not (d / "params.yaml").exists():
            d.mkdir(parents=True, exist_ok=True)
            five = ["backfill", "admission", "locality", "packing", "est_order"]
            (d / "params.yaml").write_text(
                yaml.safe_dump(
                    {"ingredients": {k: (k in five) for k in ALL}}, sort_keys=True
                )
            )
        return d

    def validate_candidate(self, candidate_dir):
        p = Path(candidate_dir) / "params.yaml"
        if not p.exists():
            return "candidate must contain params.yaml"
        try:
            yaml.safe_load(p.read_text())
        except yaml.YAMLError as e:
            return str(e)
        return None

    def _dataset_of(self, seed):
        for ds in ("dev", "audit", "judge"):
            if seed in (self.cfg.get("seeds") or {}).get(ds, []):
                return ds
        return "public"

    def evaluate(self, candidate_dir, tier, seed, timeout=True):
        if tier == "fast" and seed not in self.public_seeds:
            raise PermissionError("fast tier only accepts public seeds")
        ings, prm = _declared(candidate_dir)
        if prm.get("crash"):
            return {
                "score": None,
                "per_item": [],
                "extra": {"crashed": True, "reason": "declared crash"},
            }
        score = -1000.0 + 100.0 * len(ings)
        if {"backfill", "fairshare"} <= ings:
            score -= 200.0
        score += sum(float(x) for x in (prm.get("bonus") or {}).values())
        ds = self._dataset_of(seed)
        if ds == "dev":
            score += float(prm.get("dev_only_bonus", 0))
        if ds == "public":
            score += float(prm.get("public_only_bonus", 0))
        if self.noise_sd:
            h = hashlib.sha256(
                f"{seed}:{content_hash(Path(candidate_dir))}".encode()
            ).hexdigest()
            score += random.Random(h).gauss(0, self.noise_sd)
        return {
            "score": round(score, 6),
            "per_item": [{"id": "total", "value": round(score, 6)}],
            "extra": {
                "ingredients": sorted(ings),
                "dataset": ds,
                "trap": "trap_sjf" in ings,
            },
        }

    def evaluate_many(self, candidate_dir, seeds, tier="slow", timeout=True):
        return [self.evaluate(candidate_dir, tier, s, timeout) for s in seeds]

    def aggregate(self, results):
        per_seed = [r["score"] for r in results]
        if any(s is None for s in per_seed):
            return {
                "score": None,
                "ci": [None, None],
                "n": len(results),
                "per_seed": per_seed,
                "per_item": [],
                "extra": {
                    "failed_seeds": [i for i, s in enumerate(per_seed) if s is None]
                },
            }
        trap = any(r["extra"].get("trap") for r in results)
        return {
            "score": round(mean(per_seed), 6),
            "ci": [round(x, 6) for x in bootstrap_ci(per_seed)],
            "n": len(results),
            "per_seed": per_seed,
            "per_item": [{"id": "total", "value": round(mean(per_seed), 6)}],
            "extra": {"trap": trap},
        }

    def constraint_ok(self, agg, baseline_agg):
        return agg.get("score") is not None and not agg["extra"].get("trap")

    def constraint_violations(self, agg, baseline_agg):
        return (
            []
            if self.constraint_ok(agg, baseline_agg)
            else ["trap_sjf declared" if agg.get("score") is not None else "void"]
        )

    constraint_rule = "stub: candidates declaring trap_sjf violate the constraint"

    def probes(self, candidate_dir):
        ings, _ = _declared(candidate_dir)
        return {k: (k in ings) for k in self.ingredients}


def load(root: Path) -> StubProblem:
    return StubProblem(root)
