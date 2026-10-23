"""Shared helpers for the testbed tests (no third-party imports, so CI's root-only jobs can load them)."""

import os
from pathlib import Path

import yaml

os.environ.setdefault("TB_WORKERS", "8")

STUB_INGREDIENTS = [
    "backfill",
    "admission",
    "locality",
    "packing",
    "est_order",
    "trap_sjf",
    "fairshare",
]


def stub_candidate(directory: Path, ingredients=(), **params) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    params_yaml = {"ingredients": {k: (k in ingredients) for k in STUB_INGREDIENTS}}
    params_yaml.update(params)
    (directory / "params.yaml").write_text(yaml.safe_dump(params_yaml, sort_keys=True))
    return directory
