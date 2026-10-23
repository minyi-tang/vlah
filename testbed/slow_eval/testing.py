"""An in-process slow-eval service with a manual clock, for tests and scripted runs."""

from pathlib import Path

from fastapi.testclient import TestClient

from testbed.cli.client import SlowClient
from testbed.slow_eval.app import create_app
from testbed.slow_eval.core import ManualClock, SlowEval

SYSTEM_ACTORS = ("runner", "integrator", "composer")


class InProcessService:
    def __init__(self, directory: Path):
        self.clock = ManualClock()
        self.core = SlowEval(directory / "slow", clock=self.clock, inline=True)
        self.app = create_app(directory / "slow", core=self.core, publisher=False)
        self.http = TestClient(self.app)
        self.directory = directory

    def client(
        self, run_id: str, actor: str = "w1", key: str | None = None
    ) -> SlowClient:
        """A client for actor; testbed actors get the system key unless key="" is passed."""
        if key is None and actor in SYSTEM_ACTORS:
            key = self.core.system_key
        return SlowClient(None, run_id, actor, http=self.http, key=key or None)

    def new_run(
        self,
        run_id: str = "r1",
        arm: str = "herd",
        problem: str = "stub",
        n: int = 6,
        **overrides,
    ) -> SlowClient:
        runner = self.client(run_id, "runner")
        runner.create_run(arm, problem, n, overrides)
        return runner

    def close(self):
        self.core.close()
