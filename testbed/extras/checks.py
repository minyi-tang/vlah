"""tb extras: quick validations beyond the spec, all without an LLM.

  probes-robust   Do the ingredient detectors judge behaviour, not code? Independently written
                  policies (testbed/extras/alt_policies) must be detected as exactly what they do.
  composability   All 32 subsets of the five ingredients (+ the conflict pair): how much does the best
                  single / pair / triple capture? Pairwise interactions. If one ingredient already
                  captures most of the gain, "assembly" cannot separate the arms.
  fidelity        Is the fast tier a usable proxy for the slow tier? Rank correlation between public
                  single-seed scores and slow scores; does the trap look like a win on the fast tier?
  audit-noise     False-alarm rate of the integrator's audit rule (2.5 sigma sqrt2) on honest merges,
                  using real problem-A noise on dev vs audit seeds.
  oracle          Idealized scripted arms on the real problem, the real slow-eval rules and the real
                  integrator: how fast can single / isolated / herd reach 70% capture if every worker
                  already knows one ingredient? This is the ceiling the isolated control gets from the
                  shared integrator alone.

Results go to var/extras/<name>.md and stdout.
"""

from __future__ import annotations

import itertools
import json
import math
import shutil
import tempfile
from pathlib import Path

from testbed.problem_api import STATE_DIR, content_hash, load_problem, mean, sd

OUT = STATE_DIR / "extras"
FIVE = ["backfill", "admission", "locality", "packing", "est_order"]
ALT_DIR = Path(__file__).resolve().parent / "alt_policies"
ALT_EXPECTED = {
    "backfill_skip": {"backfill"},
    "admission_global": {"admission"},
    "locality_rack": {"locality"},
    "packing_recent": {"packing"},
    "est_order_aged": {"est_order"},
    "sjf_pure": {"est_order", "trap_sjf"},
    "est_order_aged2h": {
        "est_order",
        "trap_sjf",
    },  # spec probe window is 2 h; see fidelity for its slow-tier constraint
    "fairshare_jobs": {"fairshare"},
    "noise": set(),
}


def _variants(prob):
    return prob.hidden_module("reference.variants")


class Memo:
    """(content hash, seed, tier) -> per-seed result summary, persisted across extras."""

    def __init__(self, prob):
        self.prob = prob
        sites = Path(prob.root) / "sites"
        self.fp = content_hash(sites)[:12] if sites.exists() else ""
        self.path = OUT / f"cache-{self.fp}.json"
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def scores(self, d: Path, seeds, tier="slow"):
        h = content_hash(d)
        need = [s for s in seeds if f"{h}:{s}:{tier}" not in self.data]
        if need:
            for s, r in zip(need, self.prob.evaluate_many(d, need, tier)):
                self.data[f"{h}:{s}:{tier}"] = {
                    "score": r["score"],
                    "per_item": r["per_item"],
                    "waits_p99": {
                        k: v
                        for k, v in [(it["id"], it["value"]) for it in r["per_item"]]
                        if k.endswith("p99")
                    },
                }
            OUT.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data))
        return [self.data[f"{h}:{s}:{tier}"] for s in seeds]


def _cand(prob, ings, root: Path):
    name = "+".join(sorted(ings)) or "baseline"
    d = root / name
    if not d.exists():
        if not ings:
            shutil.copytree(prob.baseline_dir(), d)
        else:
            _variants(prob).make_candidate(sorted(ings), dest=d)
    return d


def _alt(prob, name, root: Path):
    d = root / f"alt-{name}"
    if not d.exists():
        d.mkdir(parents=True)
        shutil.copy2(ALT_DIR / f"{name}.py", d / "policy.py")
        (d / "params.yaml").write_text("{}\n")
    return d


def _spearman(xs, ys):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        for pos, i in enumerate(order):
            r[i] = pos
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = mean(rx), mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else float("nan")


# ------------------------------------------------------------------------ checks


def probes_robust(prob, root, n, out):
    rows, ok_all = [], True
    for name, want in ALT_EXPECTED.items():
        got = {k for k, v in prob.probes(_alt(prob, name, root)).items() if v}
        ok = got == want
        ok_all &= ok
        rows.append(
            f"| {name} | {sorted(want)} | {sorted(got)} | {'ok' if ok else 'MISMATCH'} |"
        )
    out(
        [
            "# probes-robust",
            "",
            "| policy | expected | detected | |",
            "| --- | --- | --- | --- |",
        ]
        + rows
        + ["", f"RESULT: {'PASS' if ok_all else 'FAIL'}"]
    )
    return ok_all


def composability(prob, root, n, out):
    memo = Memo(prob)
    seeds = prob.seeds("dev")[:n]
    res = {}
    for k in range(0, 6):
        for sub in itertools.combinations(FIVE, k):
            res[frozenset(sub)] = [
                r["score"] for r in memo.scores(_cand(prob, sub, root), seeds)
            ]
    extra = {
        "backfill+fairshare": {"backfill", "fairshare"},
        "fairshare": {"fairshare"},
    }
    for name, ings in extra.items():
        res[frozenset(ings)] = [
            r["score"] for r in memo.scores(_cand(prob, ings, root), seeds)
        ]
    base = res[frozenset()]
    full = res[frozenset(FIVE)]
    gap = mean(full) - mean(base)

    def cap(s):
        return (mean(res[s]) - mean(base)) / gap

    lines = [
        "# composability",
        "",
        f"{n} dev seeds; capture = (subset - baseline) / (all five - baseline)",
        "",
    ]
    best = {}
    for k in range(1, 6):
        subs = [s for s in res if len(s) == k and s <= set(FIVE)]
        b = max(subs, key=cap)
        best[k] = (sorted(b), cap(b))
        lines.append(f"best {k}: {sorted(b)}  capture {cap(b):.0%}")
    singles = {i: mean(res[frozenset([i])]) - mean(base) for i in FIVE}
    lines += [
        "",
        f"sum of single gains / full gain = {sum(singles.values()) / gap:.2f} (1 = additive)",
        "",
        "pairwise interaction = gain(a+b) - gain(a) - gain(b), in units of its paired s.e.",
        "",
        "| pair | interaction | z |",
        "| --- | --- | --- |",
    ]
    for a, b in itertools.combinations(FIVE, 2):
        ab = res[frozenset([a, b])]
        d = [
            x - y - z + w
            for x, y, z, w in zip(ab, res[frozenset([a])], res[frozenset([b])], base)
        ]
        z = mean(d) / (sd(d) / math.sqrt(len(d))) if sd(d) > 0 else float("inf")
        lines.append(f"| {a}+{b} | {mean(d):+.1f} | {z:+.1f} |")
    bf, fs, bffs = (
        mean(res[frozenset(x)])
        for x in (["backfill"], ["fairshare"], ["backfill", "fairshare"])
    )
    lines += [
        "",
        f"conflict pair: backfill {bf:.1f}, fairshare {fs:.1f}, both {bffs:.1f}  "
        f"-> both worse than backfill alone: {bffs < bf}",
    ]
    lines += ["", "| subset | score | capture |", "| --- | --- | --- |"]
    for s in sorted((s for s in res if s <= set(FIVE)), key=cap, reverse=True):
        lines.append(
            f"| {'+'.join(sorted(s)) or 'baseline'} | {mean(res[s]):.1f} | {cap(s):.0%} |"
        )
    ok = best[1][1] < 0.7 and bffs < bf
    lines += [
        "",
        f"RESULT: {'PASS' if ok else 'WARN'}  (pass = no single ingredient reaches 70% capture and the conflict holds)",
    ]
    out(lines)
    return ok


def fidelity(prob, root, n, out):
    memo = Memo(prob)
    dev = prob.seeds("dev")[:n]
    cands = {"baseline": _cand(prob, [], root), "reference": _cand(prob, FIVE, root)}
    for i in FIVE + ["trap_sjf", "fairshare"]:
        cands[i] = _cand(prob, [i], root)
    for a, b in [
        ("backfill", "admission"),
        ("locality", "est_order"),
        ("backfill", "fairshare"),
    ]:
        cands[f"{a}+{b}"] = _cand(prob, [a, b], root)
    for name in ALT_EXPECTED:
        cands[f"alt-{name}"] = _alt(prob, name, root)
    slow, fast = {}, {}
    for name, d in cands.items():
        slow[name] = (
            mean(r["score"] for r in memo.scores(d, dev) if r["score"] is not None)
            if all(r["score"] is not None for r in memo.scores(d, dev))
            else None
        )
        fast[name] = [r["score"] for r in memo.scores(d, prob.public_seeds, "fast")]
    names = [k for k in cands if slow[k] is not None and None not in fast[k]]
    rho_mean = _spearman([mean(fast[k]) for k in names], [slow[k] for k in names])
    rho_single = [
        _spearman([fast[k][i] for k in names], [slow[k] for k in names])
        for i in range(len(prob.public_seeds))
    ]
    base_fast = fast["baseline"]
    trap_wins = sum(
        1 for a, b in zip(fast["trap_sjf"], base_fast) if a is not None and a > b
    )
    bp = memo.scores(cands["baseline"], prob.public_seeds, "fast")
    tp = memo.scores(cands["trap_sjf"], prob.public_seeds, "fast")
    visible = 0
    for b, t in zip(bp, tp):
        if any(t["waits_p99"][k] > 1.02 * b["waits_p99"][k] for k in b["waits_p99"]):
            visible += 1
    lines = [
        "# fidelity (fast tier vs slow tier)",
        "",
        f"{len(names)} candidates; slow = mean of {n} dev seeds; fast = public seeds {prob.public_seeds}",
        "",
        f"Spearman(mean of 5 public seeds, slow) = {rho_mean:.2f}",
        "Spearman(single public seed, slow)   = "
        + ", ".join(f"{x:.2f}" for x in rho_single),
        f"trap_sjf beats baseline on the fast tier on {trap_wins}/{len(base_fast)} public seeds",
        f"trap_sjf shows a p99 regression (>2%) at some site on {visible}/{len(base_fast)} public seeds",
        "",
        "| candidate | slow | fast seeds 1..5 |",
        "| --- | --- | --- |",
    ]
    for k in names:
        lines.append(
            f"| {k} | {slow[k]:.1f} | " + ", ".join(f"{x:.0f}" for x in fast[k]) + " |"
        )
    lines += [
        "",
        "constraint on the slow tier (dev seeds) for the trap-like candidates:",
    ]
    base_agg = prob.aggregate(prob.evaluate_many(cands["baseline"], dev, "slow"))
    for k in (
        "trap_sjf",
        "alt-sjf_pure",
        "alt-est_order_aged2h",
        "alt-est_order_aged",
        "est_order",
    ):
        agg = prob.aggregate(prob.evaluate_many(cands[k], dev, "slow"))
        v = prob.constraint_violations(agg, base_agg)
        lines.append(f"  {k:22s} {'ok' if not v else 'VIOLATES ' + '; '.join(v)}")
    ok = rho_mean >= 0.8 and trap_wins >= 3
    lines += [
        "",
        f"RESULT: {'PASS' if ok else 'WARN'}  (pass = fast tier ranks well on average and the trap looks attractive on it)",
    ]
    out(lines)
    return ok


def audit_noise(prob, root, n, out):
    memo = Memo(prob)
    dev, aud = prob.seeds("dev")[:n], prob.seeds("audit")[:n]

    def ps(s, seeds):
        return [r["score"] for r in memo.scores(_cand(prob, s, root), seeds)]

    base_d, ref_d = ps([], dev), ps(FIVE, dev)
    base_a, ref_a = ps([], aud), ps(FIVE, aud)
    sig_d = sd([a - b for a, b in zip(ref_d, base_d)]) / math.sqrt(n)
    sig_a = sd([a - b for a, b in zip(ref_a, base_a)]) / math.sqrt(n)
    thr = 2.5 * math.sqrt(sig_d**2 + sig_a**2)
    flagged, total, worst = 0, 0, 0.0
    for k in range(0, 4):
        for s in itertools.combinations(FIVE, k):
            for t in itertools.combinations(
                [x for x in FIVE if x not in s], 2
            ):  # 2-merge segments
                j, kk = list(s), list(s) + list(t)
                dd = mean(ps(kk, dev)) - mean(ps(j, dev))
                da = mean(ps(kk, aud)) - mean(ps(j, aud))
                gap = abs(dd - da)
                worst = max(worst, gap / thr if thr else 0)
                flagged += gap > thr
                total += 1
    lines = [
        "# audit-noise",
        "",
        f"{n} dev + {n} audit seeds; sigma_dev {sig_d:.2f}, sigma_audit {sig_a:.2f}, "
        f"threshold 2.5*sqrt(sd^2+sa^2) = {thr:.2f}",
        "",
        f"honest 2-merge segments flagged: {flagged}/{total}  (worst gap = {worst:.2f} x threshold)",
        "",
        f"RESULT: {'PASS' if flagged == 0 else 'WARN'}",
    ]
    out(lines)
    return flagged == 0


def oracle(prob, root, n, out):
    from testbed.arms.scripted.env import ScriptedEnv

    H = 3600.0
    knobs = {
        "slow_seeds": n,
        "visible_delay_s": 600,
        "quota_per_hour": 2,
        "quota_total": 96,
        "tilt_window_s": 7200,
    }
    rows = []
    for arm, n_workers in (("single", 1), ("isolated", 5), ("herd", 5)):
        env = ScriptedEnv(
            root / f"oracle-{arm}",
            problem=prob.name,
            arm=arm,
            n=n_workers,
            run_id=f"r-oracle-{arm}",
            overrides=knobs,
        )
        cal = env.core.calibration(env.run_id)
        b, r = cal["baseline_slow"]["score"], cal["reference_slow"]["score"]
        hit70 = None
        actors = ["solo"] if arm == "single" else [f"w{i + 1}" for i in range(5)]
        todo = list(FIVE)
        t = 60.0
        env.advance_to(t)
        if arm == "isolated":
            for actor, ing in zip(actors, todo):
                d = _variants(prob).make_candidate(
                    [ing], dest=root / f"oracle-{arm}" / "work" / ing
                )
                env.submit(actor, d, predicted=b, target="baseline@0", direction=ing)
            todo = []
        while env.now() < 24 * H:
            if todo:
                k, bdir = env.base_for(actors[0])
                ing = todo.pop(0)
                actor = actors[(len(FIVE) - len(todo) - 1) % len(actors)]
                d = root / f"oracle-{arm}" / "work" / f"{ing}@{k}"
                import yaml

                prm = yaml.safe_load((bdir / "params.yaml").read_text()) or {}
                if (
                    "ingredients" not in prm
                ):  # baseline@0 is the production policy, not the switchable one
                    _variants(prob).make_candidate([ing], dest=d)
                else:
                    shutil.copytree(bdir, d, dirs_exist_ok=True)
                    prm["ingredients"][ing] = True
                    (d / "params.yaml").write_text(yaml.safe_dump(prm))
                try:
                    env.submit(
                        actor, d, predicted=b, target=f"baseline@{k}", direction=ing
                    )
                except Exception:
                    todo.insert(0, ing)
                env.advance_to(env.now() + 900)
            else:
                env.advance_to(env.now() + 1800)
            v = env.integ.versions[-1]
            if hit70 is None and v.score is not None and (v.score - b) / (r - b) >= 0.7:
                hit70 = env.now() / H
        v = env.integ.versions[-1]
        cap = (v.score - b) / (r - b) if v.score is not None else float("nan")
        rows.append(
            f"| {arm} | {n_workers} | {hit70 if hit70 is None else round(hit70, 1)} | {cap:.0%} | "
            f"{env.integ.stats['merged']} | {env.integ.stats['remeasures']} |"
        )
        env.close()
    out(
        [
            "# oracle arms (every worker already knows one ingredient; reference implementations)",
            "",
            f"slow tier with {n} seeds, 10-minute results, 2 per hour; integrator re-measures 1 per hour, 16 total",
            "",
            "| arm | workers | hours to 70% capture | capture at 24 h | merges | integrator re-measures |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        + rows
        + [
            "",
            "Read: if isolated reaches the same capture as herd, the shared integrator alone delivers assembly;",
            "herd can then only win where workers fail to find or compose ingredients by themselves.",
            "",
            "RESULT: INFO",
        ]
    )
    return True


CHECKS = {
    "probes-robust": probes_robust,
    "composability": composability,
    "fidelity": fidelity,
    "audit-noise": audit_noise,
    "oracle": oracle,
}


def main(a) -> int:
    prob = load_problem(getattr(a, "problem", "sched"))
    which = a.which or list(CHECKS)
    root = Path(tempfile.mkdtemp(prefix="tb-extras-"))
    OUT.mkdir(parents=True, exist_ok=True)
    status = 0
    for name in which:
        buf = []

        def out(lines, _buf=buf):
            _buf.extend(lines)
            print("\n".join(lines) + "\n", flush=True)

        ok = CHECKS[name](prob, root, a.seeds, out)
        (OUT / f"{name}.md").write_text("\n".join(buf) + "\n")
        status |= 0 if ok else 1
    print(f"reports in {OUT}")
    return status
