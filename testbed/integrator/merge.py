"""Three-way merge of candidate directories (rebase onto the current baseline).

Text files use git merge-file (diff3 semantics); YAML parameter files merge key by key.
A conflict anywhere means the whole rebase conflicts (-> compose task).
"""

from __future__ import annotations

import difflib
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

from testbed.problem_api import copy_writable_file, iter_candidate_files

MISSING = object()


class Conflict(Exception):
    pass


def read_candidate_files(candidate_dir: Path | None) -> dict[str, bytes]:
    if candidate_dir is None or not Path(candidate_dir).exists():
        return {}
    return {
        relative_path: path.read_bytes()
        for relative_path, path in iter_candidate_files(Path(candidate_dir))
    }


def merge_yaml(base, ours, theirs, key_path=""):
    if theirs == base:
        return ours
    if ours == base or ours == theirs:
        return theirs
    if base is MISSING and isinstance(ours, dict) and isinstance(theirs, dict):
        base = {}  # both sides added the same mapping: merge inside it
    if isinstance(base, dict) and isinstance(ours, dict) and isinstance(theirs, dict):
        merged = {}
        for yaml_key in list(dict.fromkeys([*base, *ours, *theirs])):
            base_value, ours_value, theirs_value = (
                base.get(yaml_key, MISSING),
                ours.get(yaml_key, MISSING),
                theirs.get(yaml_key, MISSING),
            )
            merged_value = merge_yaml(
                base_value, ours_value, theirs_value, f"{key_path}.{yaml_key}"
            )
            if merged_value is not MISSING:
                merged[yaml_key] = merged_value
        return merged
    raise Conflict(
        f"yaml key {key_path or '<root>'}: both sides changed it differently"
    )


def merge_text(base: bytes, ours: bytes, theirs: bytes, name: str) -> bytes:
    with tempfile.TemporaryDirectory() as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        for side, content in (("ours", ours), ("base", base), ("theirs", theirs)):
            (temp_dir / side).write_bytes(content)
        merge_run = subprocess.run(
            [
                "git",
                "merge-file",
                "-p",
                "--diff3",
                str(temp_dir / "ours"),
                str(temp_dir / "base"),
                str(temp_dir / "theirs"),
            ],
            capture_output=True,
        )
        if merge_run.returncode != 0:
            raise Conflict(f"{name}: text conflict")
        return merge_run.stdout


def three_way(
    base_dir: Path, ours_dir: Path, theirs_dir: Path, out_dir: Path
) -> list[str]:
    """Write the rebased candidate into out_dir. Returns files that differ from ours (files_changed).
    Raises Conflict."""
    base_files, ours_files, theirs_files = (
        read_candidate_files(base_dir),
        read_candidate_files(ours_dir),
        read_candidate_files(theirs_dir),
    )
    merged: dict[str, bytes | None] = {}
    for relative_path in sorted(set(base_files) | set(ours_files) | set(theirs_files)):
        base_bytes, ours_bytes, theirs_bytes = (
            base_files.get(relative_path),
            ours_files.get(relative_path),
            theirs_files.get(relative_path),
        )
        if theirs_bytes == base_bytes:
            merged_bytes = ours_bytes
        elif ours_bytes == base_bytes or ours_bytes == theirs_bytes:
            merged_bytes = theirs_bytes
        elif None in (base_bytes, ours_bytes, theirs_bytes):
            raise Conflict(f"{relative_path}: added/deleted on both sides")
        elif relative_path.endswith((".yaml", ".yml")):
            try:
                merged_yaml = merge_yaml(
                    yaml.safe_load(base_bytes) or {},
                    yaml.safe_load(ours_bytes) or {},
                    yaml.safe_load(theirs_bytes) or {},
                )
            except yaml.YAMLError as error:
                raise Conflict(f"{relative_path}: unparsable yaml ({error})")
            merged_bytes = yaml.safe_dump(merged_yaml, sort_keys=True).encode()
        else:
            merged_bytes = merge_text(
                base_bytes, ours_bytes, theirs_bytes, relative_path
            )
        merged[relative_path] = merged_bytes
    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    changed_files = []
    for relative_path, file_bytes in merged.items():
        if file_bytes is None:
            if relative_path in ours_files:
                changed_files.append(relative_path)
            continue
        out_path = out_dir / relative_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(file_bytes)
        if ours_files.get(relative_path) != file_bytes:
            changed_files.append(relative_path)
    return changed_files


def flatten_yaml(value, prefix=""):
    if isinstance(value, dict):
        leaves = {}
        for yaml_key, child in value.items():
            leaves.update(flatten_yaml(child, f"{prefix}.{yaml_key}"))
        return leaves
    return {prefix: value}


def distance(left_dir: Path, right_dir: Path) -> int:
    """Edit distance between two candidates: differing lines (text) or differing leaf keys (YAML)."""
    left_files, right_files = read_candidate_files(left_dir), read_candidate_files(
        right_dir
    )
    total = 0
    for relative_path in set(left_files) | set(right_files):
        left_bytes, right_bytes = left_files.get(relative_path), right_files.get(
            relative_path
        )
        if left_bytes == right_bytes:
            continue
        if relative_path.endswith((".yaml", ".yml")):
            try:
                left_leaves = (
                    flatten_yaml(yaml.safe_load(left_bytes) or {})
                    if left_bytes is not None
                    else {}
                )
                right_leaves = (
                    flatten_yaml(yaml.safe_load(right_bytes) or {})
                    if right_bytes is not None
                    else {}
                )
                total += sum(
                    1
                    for leaf_key in set(left_leaves) | set(right_leaves)
                    if left_leaves.get(leaf_key, MISSING)
                    != right_leaves.get(leaf_key, MISSING)
                )
                continue
            except yaml.YAMLError:
                pass
        left_lines = (
            left_bytes.decode(errors="replace").splitlines()
            if left_bytes is not None
            else []
        )
        right_lines = (
            right_bytes.decode(errors="replace").splitlines()
            if right_bytes is not None
            else []
        )
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            None, left_lines, right_lines, autojunk=False
        ).get_opcodes():
            if tag != "equal":
                total += max(i2 - i1, j2 - j1)
    return total


def copy_tree(source_dir: Path, destination_dir: Path):
    destination_dir = Path(destination_dir)
    if destination_dir.exists():
        shutil.rmtree(destination_dir)
    destination_dir.mkdir(parents=True)
    for relative_path, path in iter_candidate_files(Path(source_dir)):
        destination = destination_dir / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        copy_writable_file(path, destination)
