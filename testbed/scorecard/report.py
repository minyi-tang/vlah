"""Render scorecards: one run, or several runs (arms) side by side."""

from __future__ import annotations

import json

from testbed.scorecard.metrics import HINTS, METRICS, scorecard

MARK = {True: "PASS", False: "FAIL", None: "n/a"}


PERCENT = {"capture", "quota_discipline", "prediction_error"}


def _fmt(v, key=""):
    if v is None:
        return "-"
    if isinstance(v, dict):
        return " / ".join(f"{k.split('_')[0]} {_fmt(x, key)}" for k, x in v.items())
    if isinstance(v, (int, float)) and key in PERCENT:
        return f"{v:.0%}"
    if isinstance(v, float):
        return f"{v:.3g}"
    return str(v)


def render_one(events, fmt="text") -> str:
    sc = scorecard(events)
    if fmt == "json":
        return json.dumps(sc, indent=1, default=str)
    lines = [
        f"Scorecard  run={sc['run_id']}  arm={sc['arm']}  problem={sc['problem']}",
        "",
    ]
    w = max(len(lbl) for _, lbl, _ in METRICS)
    for key, label, _ in METRICS:
        m = sc["metrics"][key]
        hint = f"   -> look at: {HINTS[key]}" if m["ok"] is False else ""
        lines.append(
            f"  [{MARK[m['ok']]:4s}] {label:{w}s}  {_fmt(m['value'], key):>22s}   target {m['target']}{hint}"
        )
    lines.append("")
    lines.append("  diagnostics: " + json.dumps(sc["diagnostics"], default=str))
    if fmt == "md":
        rows = ["| metric | value | target | status |", "| --- | --- | --- | --- |"]
        for key, label, _ in METRICS:
            m = sc["metrics"][key]
            rows.append(
                f"| {label} | {_fmt(m['value'], key)} | {m['target']} | {MARK[m['ok']]} |"
            )
        return "\n".join(rows)
    return "\n".join(lines)


def render_many(runs: list[list[dict]], fmt="text") -> str:
    cards = [scorecard(ev) for ev in runs]
    if fmt == "json":
        return json.dumps(cards, indent=1, default=str)
    heads = [f"{c['arm']}:{c['run_id']}" for c in cards]
    rows = []
    for key, label, _ in METRICS:
        cells = []
        for c in cards:
            m = c["metrics"][key]
            cells.append(f"{_fmt(m['value'], key)} {MARK[m['ok']]}")
        rows.append((label, cells, cards[0]["metrics"][key]["target"]))
    for extra in ("final", "actor_best"):
        cells = []
        for c in cards:
            d = c["diagnostics"].get(extra)
            cells.append(
                "-"
                if not d
                else f"{d['score']} ({len(d.get('ingredients') or [])} ing)"
            )
        rows.append((extra, cells, ""))
    if fmt == "md":
        out = [
            "| metric | " + " | ".join(heads) + " | target |",
            "| --- |" + " --- |" * (len(heads) + 1),
        ]
        out += [
            f"| {lbl} | " + " | ".join(cells) + f" | {tgt} |"
            for lbl, cells, tgt in rows
        ]
        return "\n".join(out)
    w0 = max(len(r[0]) for r in rows)
    ws = [max(len(h), *(len(r[1][i]) for r in rows)) for i, h in enumerate(heads)]
    out = [
        " " * w0 + "  " + "  ".join(h.ljust(w) for h, w in zip(heads, ws)) + "  target"
    ]
    for lbl, cells, tgt in rows:
        out.append(
            lbl.ljust(w0)
            + "  "
            + "  ".join(c.ljust(w) for c, w in zip(cells, ws))
            + f"  {tgt}"
        )
    return "\n".join(out)
