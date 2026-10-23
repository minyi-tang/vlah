"""Problem A: scheduling policy in a SimPy cluster simulator (spec 3, 11.6).

Public part: evaluate (fast tier on public seeds 1-5), aggregate, constraint rule.
Hidden part (slow-eval machine only, never committed; issue #10): problem.yaml (hidden seeds, knobs),
reference/ and probes/, under private/testbed/sched/ or $TB_PRIVATE/sched/ (see testbed.problem_api).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from testbed.problem_api import (
    PROBLEMS_ROOT,
    bootstrap_ci,
    call_problem,
    hidden_root,
    mean,
    mount_hidden,
    percentile,
    pool,
    sd,
)

from .sim import engine, workload

ROOT = Path(__file__).resolve().parent
PUBLIC_SEEDS = [1, 2, 3, 4, 5]
DEFAULT_KNOBS = {
    "quota_per_hour": 2,
    "quota_total": 96,
    "visible_delay_s": 600,
    "slow_seeds": 20,
    "tilt_window_s": 7200,
    "quota_integrator_total": 16,
    "quota_integrator_per_hour": 1,
    "audit_total": 10,
    "judge_total": 5,
    "hour_s": 3600,
}
DUR_BUCKETS = [
    (0, 900, "d<15m"),
    (900, 3600, "d15m-1h"),
    (3600, 4 * 3600, "d1h-4h"),
    (4 * 3600, 1e18, "d>4h"),
]


def _summ(xs):
    xs = sorted(xs)
    return {
        "n": len(xs),
        "mean": round(mean(xs), 3) if xs else 0.0,
        "p99": round(percentile(xs, 99), 3),
    }


class SchedProblem:
    name = "sched"

    def __init__(self, root: Path):
        self.root = Path(root)
        self.common = yaml.safe_load((self.root / "sites" / "common.yaml").read_text())
        self.sites = []
        for k in range(1, 7):
            self.sites.append(
                yaml.safe_load((self.root / "sites" / f"site{k}.yaml").read_text())
            )
        # Only the canonical problem (the slow-eval side) looks for hidden material; a packed worker copy never does.
        canonical = self.root.resolve() == (PROBLEMS_ROOT / "sched").resolve()
        self.hidden = hidden_root("sched") if canonical else None
        self.cfg = {}
        if self.hidden is not None:
            try:
                self.cfg = (
                    yaml.safe_load((self.hidden / "problem.yaml").read_text()) or {}
                )
            except OSError:  # present but unreadable (e.g. inside an agent's sandbox)
                self.hidden = None
        self.ingredients = list(self.cfg.get("ingredients", []))
        self.public_seeds = list(PUBLIC_SEEDS)
        self.scale = float(self.common.get("scale", 1.0))

    # ------------------------------------------------------------------ metadata
    def knobs(self) -> dict:
        k = dict(DEFAULT_KNOBS)
        k.update(self.cfg.get("knobs", {}) or {})
        return k

    def seeds(self, dataset: str) -> list[int]:
        if dataset == "public":
            return list(self.public_seeds)
        seeds = (self.cfg.get("seeds") or {}).get(dataset)
        if not seeds:
            raise PermissionError(
                f"seeds for dataset {dataset!r} are not available here"
            )
        return list(seeds)

    def baseline_dir(self) -> Path:
        return self.root / "policy"

    def reference_dir(self) -> Path:
        if self.hidden is None:
            raise PermissionError("the reference policy is not available here")
        return self.hidden / "reference"

    def hidden_module(self, dotted: str):
        """A hidden module, e.g. "probes" or "reference.precheck" (slow-eval machine only)."""
        if self.hidden is None:
            raise PermissionError(f"hidden module {dotted} is not available here")
        return mount_hidden(__package__, self.hidden, dotted)

    def validate_candidate(self, candidate_dir: Path) -> str | None:
        if not (Path(candidate_dir) / "policy.py").exists():
            return "candidate must contain policy.py"
        p = Path(candidate_dir) / "params.yaml"
        if p.exists():
            try:
                yaml.safe_load(p.read_text())
            except yaml.YAMLError as e:
                return f"params.yaml does not parse: {e}"
        return None

    # ------------------------------------------------------------------ evaluation
    def horizon(self, tier: str) -> float:
        """Slow tier (and final_check): horizon_s. Fast tier: the shorter fast_horizon_s."""
        return (
            self.common.get("fast_horizon_s", self.common["horizon_s"])
            if tier == "fast"
            else self.common["horizon_s"]
        )

    def site_result(
        self,
        candidate_dir: str,
        seed: int,
        site_idx: int,
        timeout: bool = True,
        tier: str = "slow",
    ) -> dict:
        prof = self.sites[site_idx]
        common = dict(self.common, horizon_s=self.horizon(tier))
        jobs = workload.generate(prof, common, seed, scale=self.scale)
        out = engine.run_site(
            prof, common, jobs, Path(candidate_dir), timeout=timeout, scale=self.scale
        )
        if "failed" in out:
            return {
                "site": prof["id"],
                "failed": out["failed"],
                "calls": out.get("calls", 0),
                "timeouts": out.get("timeouts", 0),
            }
        waits = [round(j["wait"], 1) for j in out["jobs"]]
        by_class = {}
        for j, w in zip(out["jobs"], waits):
            for key in (
                f"c{j['cores']}",
                next(n for lo, hi, n in DUR_BUCKETS if lo <= j["est_dur"] < hi),
                f"user:{'u0' if j['user'] == 'u0' else 'other'}",
            ):
                by_class.setdefault(key, []).append(w)
        return {
            "site": prof["id"],
            "waits": waits,
            "calls": out["calls"],
            "timeouts": out["timeouts"],
            "errors": out["errors"],
            "by_class": {k: _summ(v) for k, v in sorted(by_class.items())},
            "stats": out["stats"],
        }

    def assemble_sites(self, site_results: list[dict]) -> dict:
        calls = sum(r.get("calls", 0) for r in site_results)
        tos = sum(r.get("timeouts", 0) for r in site_results)
        ratio = tos / calls if calls else 0.0
        extra = {
            "timed_out": False,
            "timeout_ratio": round(ratio, 6),
            "calls": calls,
            "timeouts": tos,
        }
        failed = [r for r in site_results if "failed" in r]
        if failed:
            extra["crashed"] = True
            extra["reason"] = "; ".join(
                f"site{r['site']}: {r['failed']}" for r in failed
            )
            return {"score": None, "per_item": [], "extra": extra}
        if ratio > self.common["max_timeout_ratio"]:
            extra["timed_out"] = True
            extra["reason"] = (
                f"timeout ratio {ratio:.4f} > {self.common['max_timeout_ratio']}"
            )
            return {"score": None, "per_item": [], "extra": extra}
        per_item, means = [], []
        extra["waits"] = {}
        extra["by_class"] = {}
        extra["mechanisms"] = {}
        errs = []
        for r in sorted(site_results, key=lambda r: r["site"]):
            ws = sorted(r["waits"])
            m = mean(ws)
            means.append(m)
            per_item.append({"id": f"site{r['site']}.mean", "value": round(m, 3)})
            per_item.append(
                {"id": f"site{r['site']}.p99", "value": round(percentile(ws, 99), 3)}
            )
            extra["waits"][f"site{r['site']}"] = r["waits"]
            extra["by_class"][f"site{r['site']}"] = r["by_class"]
            extra["mechanisms"][f"site{r['site']}"] = r["stats"]
            errs.extend(r.get("errors") or [])
        if errs:
            extra["errors"] = errs[:5]
        return {"score": round(-mean(means), 3), "per_item": per_item, "extra": extra}

    def evaluate(
        self, candidate_dir: Path, tier: str, seed: int, timeout: bool = True
    ) -> dict:
        if tier == "fast" and seed not in self.public_seeds:
            raise PermissionError(
                f"fast tier only accepts public seeds {self.public_seeds}"
            )
        return self.evaluate_many(candidate_dir, [seed], tier, timeout)[0]

    def evaluate_many(
        self,
        candidate_dir: Path,
        seeds: list[int],
        tier: str = "slow",
        timeout: bool = True,
    ) -> list[dict]:
        ex = pool()
        futs = {
            (s, i): ex.submit(
                call_problem,
                str(self.root),
                "site_result",
                str(candidate_dir),
                s,
                i,
                timeout,
                tier,
            )
            for s in seeds
            for i in range(len(self.sites))
        }
        out = []
        for s in seeds:
            out.append(
                self.assemble_sites(
                    [futs[(s, i)].result() for i in range(len(self.sites))]
                )
            )
        return out

    def aggregate(self, results: list[dict]) -> dict:
        per_seed = [r["score"] for r in results]
        extra = {}
        failed = [i for i, r in enumerate(results) if r["score"] is None]
        if failed:
            extra["failed_seeds"] = failed
            extra["reasons"] = [results[i]["extra"].get("reason") for i in failed][:3]
            return {
                "score": None,
                "ci": [None, None],
                "n": len(results),
                "per_seed": per_seed,
                "per_item": [],
                "extra": extra,
            }
        per_item = []
        p99_sd = {}
        for k in range(1, 7):
            site = f"site{k}"
            pooled = sorted(w for r in results for w in r["extra"]["waits"][site])
            seed_p99 = [
                next(it["value"] for it in r["per_item"] if it["id"] == f"{site}.p99")
                for r in results
            ]
            seed_mean = [
                next(it["value"] for it in r["per_item"] if it["id"] == f"{site}.mean")
                for r in results
            ]
            per_item.append({"id": f"{site}.mean", "value": round(mean(seed_mean), 3)})
            per_item.append(
                {"id": f"{site}.p99", "value": round(percentile(pooled, 99), 3)}
            )
            p99_sd[site] = round(sd(seed_p99), 3)
        extra["p99_seed_sd"] = p99_sd
        extra["timeout_ratio_max"] = max(
            r["extra"].get("timeout_ratio", 0) for r in results
        )
        return {
            "score": round(mean(per_seed), 3),
            "ci": [round(x, 3) for x in bootstrap_ci(per_seed)],
            "n": len(results),
            "per_seed": per_seed,
            "per_item": per_item,
            "extra": extra,
        }

    def constraint_ok(self, agg: dict, baseline_agg: dict) -> bool:
        return not self.constraint_violations(agg, baseline_agg)

    def constraint_violations(self, agg: dict, baseline_agg: dict) -> list[str]:
        if agg.get("score") is None:
            return ["void"]
        a = {it["id"]: it["value"] for it in agg["per_item"]}
        b = {it["id"]: it["value"] for it in baseline_agg["per_item"]}
        out = []
        for k in range(1, 7):
            site = f"site{k}"
            bp = b[f"{site}.p99"]
            tol = max(0.02 * bp, baseline_agg["extra"]["p99_seed_sd"][site])
            if a[f"{site}.p99"] > bp + tol:
                out.append(f"{site}.p99 {a[f'{site}.p99']:.0f} > {bp:.0f} + {tol:.0f}")
        return out

    constraint_rule = (
        "every site's p99 wait, pooled over all slow seeds, must be <= the baseline's pooled p99 "
        "+ max(2% of it, the baseline's per-seed p99 standard deviation)"
    )

    # ------------------------------------------------------------------ probes (hidden)
    def probes(self, candidate_dir: Path) -> dict[str, bool]:
        return self.hidden_module("probes").run_probes(self, Path(candidate_dir))


def load(root: Path) -> SchedProblem:
    return SchedProblem(root)
