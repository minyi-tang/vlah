"""Render scorecards: one run, or several runs (arms) side by side."""

from __future__ import annotations

import json

from testbed.scorecard.metrics import HINTS, METRICS, scorecard

MARK = {True: "PASS", False: "FAIL", None: "n/a"}


PERCENT = {"capture", "quota_discipline", "prediction_error"}


def format_value(value, metric_key=""):
    if value is None:
        return "-"
    if isinstance(value, dict):
        return " / ".join(
            f"{part.split('_')[0]} {format_value(part_value, metric_key)}"
            for part, part_value in value.items()
        )
    if isinstance(value, (int, float)) and metric_key in PERCENT:
        return f"{value:.0%}"
    if isinstance(value, float):
        return f"{value:.3g}"
    return str(value)


def render_one(events, fmt="text") -> str:
    card = scorecard(events)
    if fmt == "json":
        return json.dumps(card, indent=1, default=str)
    lines = [
        f"Scorecard  run={card['run_id']}  arm={card['arm']}  problem={card['problem']}",
        "",
    ]
    label_width = max(len(label) for _, label, _ in METRICS)
    for metric_key, label, _ in METRICS:
        row = card["metrics"][metric_key]
        hint = f"   -> look at: {HINTS[metric_key]}" if row["ok"] is False else ""
        lines.append(
            f"  [{MARK[row['ok']]:4s}] {label:{label_width}s}  "
            f"{format_value(row['value'], metric_key):>22s}   target {row['target']}{hint}"
        )
    lines.append("")
    lines.append("  diagnostics: " + json.dumps(card["diagnostics"], default=str))
    if fmt == "md":
        rows = ["| metric | value | target | status |", "| --- | --- | --- | --- |"]
        for metric_key, label, _ in METRICS:
            row = card["metrics"][metric_key]
            rows.append(
                f"| {label} | {format_value(row['value'], metric_key)} | {row['target']} | {MARK[row['ok']]} |"
            )
        return "\n".join(rows)
    return "\n".join(lines)


def render_many(runs: list[list[dict]], fmt="text") -> str:
    cards = [scorecard(events) for events in runs]
    if fmt == "json":
        return json.dumps(cards, indent=1, default=str)
    headings = [f"{card['arm']}:{card['run_id']}" for card in cards]
    rows = []
    for metric_key, label, _ in METRICS:
        cells = []
        for card in cards:
            row = card["metrics"][metric_key]
            cells.append(f"{format_value(row['value'], metric_key)} {MARK[row['ok']]}")
        rows.append((label, cells, cards[0]["metrics"][metric_key]["target"]))
    for extra_row in ("final", "actor_best"):
        cells = []
        for card in cards:
            summary = card["diagnostics"].get(extra_row)
            cells.append(
                "-"
                if not summary
                else f"{summary['score']} ({len(summary.get('ingredients') or [])} ing)"
            )
        rows.append((extra_row, cells, ""))
    if fmt == "md":
        lines = [
            "| metric | " + " | ".join(headings) + " | target |",
            "| --- |" + " --- |" * (len(headings) + 1),
        ]
        lines += [
            f"| {label} | " + " | ".join(cells) + f" | {target} |"
            for label, cells, target in rows
        ]
        return "\n".join(lines)
    label_width = max(len(row[0]) for row in rows)
    column_widths = [
        max(len(heading), *(len(row[1][i]) for row in rows))
        for i, heading in enumerate(headings)
    ]
    lines = [
        " " * label_width
        + "  "
        + "  ".join(
            heading.ljust(width) for heading, width in zip(headings, column_widths)
        )
        + "  target"
    ]
    for label, cells, target in rows:
        lines.append(
            label.ljust(label_width)
            + "  "
            + "  ".join(cell.ljust(width) for cell, width in zip(cells, column_widths))
            + f"  {target}"
        )
    return "\n".join(lines)
