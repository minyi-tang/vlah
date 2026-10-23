"""tb pack-problem: build the worker copy of a problem (spec 11.1). Hidden material never ships."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from testbed.problem_api import load_problem, resolve_problem_dir

HIDDEN_DIRS = {"hidden", "reference", "probes", "planted", "__pycache__"}
HIDDEN_FILES = {"problem.yaml", "test_hidden.py", "pi_prompt.md"}
HIDDEN_PATHS = {"tasks/holdout"}


def _excluded(rel: Path) -> bool:
    parts = rel.parts
    if (
        any(p in HIDDEN_DIRS for p in parts)
        or rel.name in HIDDEN_FILES
        or rel.suffix == ".pyc"
    ):
        return True
    s = rel.as_posix()
    return any(s == h or s.startswith(h + "/") for h in HIDDEN_PATHS)


def pack_problem(name: str, out: Path) -> Path:
    src = resolve_problem_dir(name)
    prob = load_problem(name)
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        if _excluded(rel) or not p.is_file():
            continue
        q = out / rel
        q.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, q)
    fast, detail = {}, {}
    for s in prob.public_seeds:
        r = prob.evaluate(prob.baseline_dir(), "fast", s)
        fast[str(s)] = r["score"]
        # Per-site and per-class waits only: the mechanism counters stay with the operator (A17).
        detail[str(s)] = {
            "per_item": r["per_item"],
            "by_class": r["extra"].get("by_class"),
        }
    (out / "baseline_scores.json").write_text(
        json.dumps({"fast": fast, "fast_detail": detail}, indent=1)
    )
    return out


def leaked(out: Path) -> list[str]:
    """Paths in a packed copy that must not be there (used by tests and tb run)."""
    bad = []
    for p in Path(out).rglob("*"):
        rel = p.relative_to(out)
        if _excluded(rel) and "__pycache__" not in rel.parts:
            bad.append(rel.as_posix())
    return bad
