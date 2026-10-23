"""Three-way merge of candidate directories (rebase onto the current baseline).

Text files use ``git merge-file`` (diff3 semantics); YAML parameter files merge key by key.
A conflict anywhere means the whole rebase conflicts (-> compose task).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

from testbed.problem_api import iter_candidate_files

_MISSING = object()


class Conflict(Exception):
    pass


def _files(d: Path | None) -> dict[str, bytes]:
    if d is None or not Path(d).exists():
        return {}
    return {rel: p.read_bytes() for rel, p in iter_candidate_files(Path(d))}


def merge_yaml(base, ours, theirs, path=""):
    if theirs == base:
        return ours
    if ours == base or ours == theirs:
        return theirs
    if base is _MISSING and isinstance(ours, dict) and isinstance(theirs, dict):
        base = {}  # both sides added the same mapping: merge inside it
    if isinstance(base, dict) and isinstance(ours, dict) and isinstance(theirs, dict):
        out = {}
        for k in list(dict.fromkeys([*base, *ours, *theirs])):
            b, o, t = (
                base.get(k, _MISSING),
                ours.get(k, _MISSING),
                theirs.get(k, _MISSING),
            )
            v = merge_yaml(b, o, t, f"{path}.{k}")
            if v is not _MISSING:
                out[k] = v
        return out
    raise Conflict(f"yaml key {path or '<root>'}: both sides changed it differently")


def merge_text(base: bytes, ours: bytes, theirs: bytes, name: str) -> bytes:
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        for n, b in (("ours", ours), ("base", base), ("theirs", theirs)):
            (td / n).write_bytes(b)
        r = subprocess.run(
            [
                "git",
                "merge-file",
                "-p",
                "--diff3",
                str(td / "ours"),
                str(td / "base"),
                str(td / "theirs"),
            ],
            capture_output=True,
        )
        if r.returncode != 0:
            raise Conflict(f"{name}: text conflict")
        return r.stdout


def three_way(
    base_dir: Path, ours_dir: Path, theirs_dir: Path, out_dir: Path
) -> list[str]:
    """Write the rebased candidate into out_dir. Returns files that differ from ours (files_changed).
    Raises Conflict."""
    b, o, t = _files(base_dir), _files(ours_dir), _files(theirs_dir)
    merged: dict[str, bytes | None] = {}
    for rel in sorted(set(b) | set(o) | set(t)):
        bv, ov, tv = b.get(rel), o.get(rel), t.get(rel)
        if tv == bv:
            mv = ov
        elif ov == bv or ov == tv:
            mv = tv
        elif None in (bv, ov, tv):
            raise Conflict(f"{rel}: added/deleted on both sides")
        elif rel.endswith((".yaml", ".yml")):
            try:
                y = merge_yaml(
                    yaml.safe_load(bv) or {},
                    yaml.safe_load(ov) or {},
                    yaml.safe_load(tv) or {},
                )
            except yaml.YAMLError as e:
                raise Conflict(f"{rel}: unparsable yaml ({e})")
            mv = yaml.safe_dump(y, sort_keys=True).encode()
        else:
            mv = merge_text(bv, ov, tv, rel)
        merged[rel] = mv
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    changed = []
    for rel, data in merged.items():
        if data is None:
            if rel in o:
                changed.append(rel)
            continue
        p = out_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        if o.get(rel) != data:
            changed.append(rel)
    return changed


def _flat(y, prefix=""):
    if isinstance(y, dict):
        out = {}
        for k, v in y.items():
            out.update(_flat(v, f"{prefix}.{k}"))
        return out
    return {prefix: y}


def distance(a_dir: Path, b_dir: Path) -> int:
    """Edit distance between two candidates: differing lines (text) or differing leaf keys (YAML)."""
    import difflib

    a, b = _files(a_dir), _files(b_dir)
    total = 0
    for rel in set(a) | set(b):
        x, y = a.get(rel), b.get(rel)
        if x == y:
            continue
        if rel.endswith((".yaml", ".yml")):
            try:
                fx = _flat(yaml.safe_load(x) or {}) if x is not None else {}
                fy = _flat(yaml.safe_load(y) or {}) if y is not None else {}
                total += sum(
                    1
                    for k in set(fx) | set(fy)
                    if fx.get(k, _MISSING) != fy.get(k, _MISSING)
                )
                continue
            except yaml.YAMLError:
                pass
        xl = x.decode(errors="replace").splitlines() if x is not None else []
        yl = y.decode(errors="replace").splitlines() if y is not None else []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, xl, yl, autojunk=False
        ).get_opcodes():
            if tag != "equal":
                total += max(i2 - i1, j2 - j1)
    return total


def copy_tree(src: Path, dst: Path):
    dst = Path(dst)
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for rel, p in iter_candidate_files(Path(src)):
        q = dst / rel
        q.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, q)
