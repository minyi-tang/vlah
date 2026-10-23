"""Append-only event log (spec 11.4). One JSON object per line, one file per run.

    {"ts": epoch_s, "run_id": ..., "arm": ..., "actor": ..., "type": ..., "data": {...}}

Writers only ever append (O_APPEND + an advisory lock). The scorecard reads nothing else.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

# type -> required data fields (POST /events accepts any type; these are checked when present)
SCHEMA: dict[str, list[str]] = {
    "calibration": ["baseline_slow", "reference_slow", "baseline_fast"],
    "fast_run": ["hash", "seed", "score", "baseline_score"],
    "submit": [
        "ticket",
        "hash",
        "predicted_score",
        "parents",
        "duplicate",
        "direction",
    ],
    "result": ["ticket", "hash", "score", "ci", "constraint_ok", "ingredients"],
    "quota_rejected": ["actor"],
    "blackboard_write": ["kind", "id", "refs"],
    "blackboard_read": ["ids"],
    "fork": ["from_actor", "to_actor", "reason"],
    "allocation": ["hash", "workers_before", "workers_after", "reason"],
    "abandon": ["hash", "reason"],
    "final": ["hash", "ticket"],
    "final_check": ["dataset", "score", "ci"],
    "actor_final": ["actor", "hash", "ticket"],
    # integrator (11.8.1 / design doc D74)
    "baseline_advanced": [
        "baseline",
        "parent_baseline",
        "candidate",
        "files_changed",
        "paired",
    ],
    "baseline_reverted": ["from_baseline", "new_baseline", "removed", "reason"],
    "integration_failed": ["candidate", "baseline", "reason"],
    "interaction_found": ["candidate", "baseline", "earlier", "remeasured"],
    "audit_checked": ["segment", "daily", "audit", "threshold", "flagged"],
    "compose_task": ["task_id", "candidates", "baseline"],
    "quarantined": ["candidate", "reason"],
    "integrator_frozen": ["baseline", "ticket", "hash"],
    "stale_target": ["candidate", "claimed", "effective"],
    # service housekeeping
    "anomaly": ["ticket", "score", "reference_score"],
    "run_end": [],
}

KINDS_BLACKBOARD = {"claim", "dead_end", "insight", "compose"}


class SchemaError(ValueError):
    pass


def validate(ev: dict) -> None:
    for k in ("ts", "run_id", "type", "data"):
        if k not in ev:
            raise SchemaError(f"event missing {k!r}: {ev}")
    req = SCHEMA.get(ev["type"])
    if req:
        missing = [f for f in req if f not in ev["data"]]
        if missing:
            raise SchemaError(f"{ev['type']} missing data fields {missing}")
    if ev["type"] == "blackboard_write":
        if ev["data"]["kind"] not in KINDS_BLACKBOARD:
            raise SchemaError(
                f"blackboard_write kind must be one of {sorted(KINDS_BLACKBOARD)}"
            )
        if ev["data"]["kind"] == "dead_end" and not ev["data"].get("hash"):
            raise SchemaError("dead_end requires hash")


class Recorder:
    def __init__(self, path: Path, run_id: str, arm: str | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.arm = arm

    def write(
        self, type_: str, data: dict, ts: float, actor: str | None = None, **extra
    ) -> dict:
        ev = {
            "ts": ts,
            "run_id": self.run_id,
            "arm": self.arm,
            "actor": actor,
            "type": type_,
            "data": data,
        }
        ev.update(extra)
        self.append(ev)
        return ev

    def append(self, ev: dict) -> None:
        validate(ev)
        line = (json.dumps(ev, sort_keys=True, default=_jsonable) + "\n").encode()
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, line)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _jsonable(o):
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, Path):
        return str(o)
    raise TypeError(type(o))


def read_events(path: Path) -> list[dict]:
    out = []
    with Path(path).open() as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def sorted_events(events: list[dict]) -> list[dict]:
    """Stable sort by ts (file order breaks ties)."""
    return [e for _, e in sorted(enumerate(events), key=lambda p: (p[1]["ts"], p[0]))]
