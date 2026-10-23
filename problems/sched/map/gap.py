"""Opportunity map, first measurement script (design doc D76). Public.

Reads the per-site / per-class breakdown that `tb fast <dir> --out r.json` writes and compares it
with the baseline on the same public seed (baseline_scores.json). Writes gap.md: where the waiting
time is, split by site, job size, estimated duration and user, plus the mechanism counters
(cold starts, off-rack placements, overloaded time).

    tb fast work --seed 1 --out r.json
    python problem/map/gap.py r.json [--out gap.md]
"""

import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _row(name, cand, base):
    d = cand["mean"] - base["mean"] if base else 0.0
    b = f"{base['mean']:9.1f} {base['p99']:9.0f}" if base else " " * 19
    return f"| {name:14s} | {cand['n']:6d} | {cand['mean']:9.1f} | {cand['p99']:9.0f} | {b.split()[0] if base else '':>9s} | {d:+9.1f} |"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("result")
    ap.add_argument("--out", default="gap.md")
    a = ap.parse_args()
    r = json.loads(Path(a.result).read_text())
    seed = str(r["seed"])
    base_all = json.loads((HERE.parent / "baseline_scores.json").read_text())
    base = base_all.get("fast_detail", {}).get(seed, {})
    lines = [
        f"# gap.md  (seed {seed}; score {r['score']} vs baseline {r['baseline_score']})",
        "",
    ]
    by = r.get("extra", {}).get("by_class", {})
    mech = r.get("extra", {}).get("mechanisms", {})
    bby = base.get("by_class", {})
    bmech = base.get("mechanisms", {})
    items = {it["id"]: it["value"] for it in r.get("per_item", [])}
    bitems = {it["id"]: it["value"] for it in base.get("per_item", [])}
    lines += [
        "## Sites",
        "",
        "| site | mean | p99 | baseline mean | baseline p99 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for k in range(1, 7):
        s = f"site{k}"
        lines.append(
            f"| {s} | {items.get(s + '.mean', 0):.1f} | {items.get(s + '.p99', 0):.0f} | "
            f"{bitems.get(s + '.mean', 0):.1f} | {bitems.get(s + '.p99', 0):.0f} |"
        )
    for s in sorted(by):
        lines += [
            "",
            f"## {s}",
            "",
            f"mechanisms: {mech.get(s)}  (baseline: {bmech.get(s)})",
            "",
            "| class | n | mean | p99 | base mean | delta |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for cls, c in by[s].items():
            lines.append(_row(cls, c, bby.get(s, {}).get(cls)))
    Path(a.out).write_text("\n".join(lines) + "\n")
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
