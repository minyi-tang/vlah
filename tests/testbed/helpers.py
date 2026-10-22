"""Shared helpers for the testbed tests: stub candidates and an in-process slow-eval service."""

import os
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from testbed.cli.client import SlowClient
from testbed.slow_eval.app import create_app
from testbed.slow_eval.core import ManualClock, SlowEval

os.environ.setdefault("TB_WORKERS", "8")


def stub_candidate(d: Path, ingredients=(), **params) -> Path:
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    allk = [
        "backfill",
        "admission",
        "locality",
        "packing",
        "est_order",
        "trap_sjf",
        "fairshare",
    ]
    prm = {"ingredients": {k: (k in ingredients) for k in allk}}
    prm.update(params)
    (d / "params.yaml").write_text(yaml.safe_dump(prm, sort_keys=True))
    return d


class Svc:
    def __init__(self, tmp: Path):
        self.clock = ManualClock()
        self.core = SlowEval(tmp / "slow", clock=self.clock, inline=True)
        self.app = create_app(tmp / "slow", core=self.core, publisher=False)
        self.http = TestClient(self.app)
        self.tmp = tmp

    def client(self, run_id, actor="w1", key=None):
        if key is None and actor in ("runner", "integrator", "composer"):
            key = self.core.system_key
        return SlowClient(None, run_id, actor, http=self.http, key=key or None)

    def new_run(self, run_id="r1", arm="herd", problem="stub", n=6, **overrides):
        c = self.client(run_id, "runner")
        c.create_run(arm, problem, n, overrides)
        return c
