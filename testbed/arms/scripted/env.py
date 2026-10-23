"""In-process arm environment for scripted (no-LLM) agents: the real slow-eval service and the
real integrator, driven by a manual clock so hours of simulated time run in seconds."""

from __future__ import annotations

import tempfile
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from testbed.cli.client import SlowClient
from testbed.integrator.core import IntegratorConfig
from testbed.integrator.driver import Integrator
from testbed.integrator.merge import copy_tree
from testbed.problem_api import candidate_hash, load_problem
from testbed.recorder.log import read_events
from testbed.slow_eval.app import create_app
from testbed.slow_eval.core import ManualClock, SlowEval

PUSH_ARMS = {"single", "herd"}  # 11.8.1: the one difference between arms
MAX_ADVANCE_STEPS = 100_000
MIN_CLOCK_STEP_SECONDS = 0.001


class ScriptedEnv:
    def __init__(
        self,
        workdir: Path | None = None,
        problem="stub",
        arm="herd",
        n=6,
        run_id="r-scripted",
        overrides=None,
        composer=None,
        config: IntegratorConfig | None = None,
    ):
        self.workdir = Path(workdir or tempfile.mkdtemp(prefix="tb-scripted-"))
        self.clock = ManualClock()
        self.core = SlowEval(self.workdir / "slow", clock=self.clock, inline=True)
        self.http = TestClient(
            create_app(self.workdir / "slow", core=self.core, publisher=False)
        )
        self.run_id, self.arm, self.problem_name = run_id, arm, problem
        knob_overrides = dict(overrides or {})
        if arm == "isolated":
            knob_overrides.setdefault("per_actor_share", True)
        self.runner = SlowClient(
            None, run_id, "runner", http=self.http, key=self.core.system_key
        )
        self.runner.create_run(arm, problem, n, knob_overrides)
        self.started_at = self.clock.now()
        self.problem = load_problem(problem)
        self.knobs = self.core.knobs(run_id)
        self.latest = None
        self.integrator = Integrator(
            self.runner,
            self.workdir / "integrator",
            self.problem.baseline_dir(),
            self.clock.now,
            config=config or IntegratorConfig(),
            composer=composer,
            on_advance=self.on_advance if arm in PUSH_ARMS else None,
        )
        self.latest = self.integrator.versions[0]
        self.candidate_count = 0

    def now(self):
        """Seconds since the run started."""
        return self.clock.now() - self.started_at

    def advance_to(self, run_seconds: float):
        target_at = self.started_at + run_seconds
        visible_delay_seconds = max(1.0, float(self.knobs.get("visible_delay_s", 1)))
        for _ in range(MAX_ADVANCE_STEPS):
            self.integrator.step()
            now = self.clock.now()
            if now >= target_at:
                break
            due = self.integrator.due(now)
            if due == "now":
                next_at = now + MIN_CLOCK_STEP_SECONDS
            elif due == "wait":
                next_at = now + visible_delay_seconds
            elif isinstance(due, (int, float)):
                next_at = due
            else:
                next_at = target_at
            next_visible_at = self.core.db.execute(
                "SELECT MIN(visible_at) FROM tickets WHERE run_id=? AND published=0 AND dup_of IS NULL",
                (self.run_id,),
            ).fetchone()[0]
            if next_visible_at is not None and next_visible_at > now:
                next_at = min(next_at, next_visible_at)
            self.clock.t = min(max(next_at, now + MIN_CLOCK_STEP_SECONDS), target_at)
        self.integrator.step()

    def sleep(self, seconds):
        self.advance_to(self.now() + seconds)

    def on_advance(self, version):
        self.latest = version

    def client(self, actor):
        return SlowClient(None, self.run_id, actor, http=self.http)

    def base_for(self, actor):
        """What the agent sees as its baseline: pushed versions for single/herd, baseline@0 otherwise."""
        version = self.latest if self.arm in PUSH_ARMS else self.integrator.versions[0]
        return version.k, Path(version.dir)

    def make(self, ingredients=(), base_dir=None, **params) -> Path:
        self.candidate_count += 1
        candidate_dir = self.workdir / "work" / f"c{self.candidate_count}"
        copy_tree(base_dir or self.problem.baseline_dir(), candidate_dir)
        params_path = candidate_dir / "params.yaml"
        params_yaml = (
            yaml.safe_load(params_path.read_text()) if params_path.exists() else {}
        )
        params_yaml = params_yaml or {}
        ingredient_flags = dict(params_yaml.get("ingredients") or {})
        for ingredient in ingredients:
            ingredient_flags[ingredient] = True
        params_yaml["ingredients"] = ingredient_flags
        params_yaml.update(params)
        params_path.write_text(yaml.safe_dump(params_yaml, sort_keys=True))
        return candidate_dir

    def fast_run(self, actor, candidate_dir: Path, target: str, seed=1) -> dict:
        result = self.problem.evaluate(candidate_dir, "fast", seed)
        baseline_score = self.problem.evaluate(
            self.problem.baseline_dir(), "fast", seed
        )["score"]
        submitted_hash = candidate_hash(candidate_dir, target)
        self.client(actor).post_events(
            [
                {
                    "type": "fast_run",
                    "data": {
                        "hash": submitted_hash,
                        "seed": seed,
                        "score": result["score"],
                        "baseline_score": baseline_score,
                    },
                }
            ]
        )
        return {
            "hash": submitted_hash,
            "score": result["score"],
            "baseline_score": baseline_score,
        }

    def submit(
        self,
        actor,
        candidate_dir: Path,
        predicted: float,
        target: str,
        **manifest_fields,
    ):
        return self.client(actor).submit_dir(
            candidate_dir, predicted=predicted, target=target, **manifest_fields
        )

    def herd_event(self, actor, event_type, data):
        self.client(actor).post_events([{"type": event_type, "data": data}])

    def true_score(self, candidate_dir: Path) -> float:
        return self.problem.aggregate(
            self.problem.evaluate_many(
                candidate_dir, self.problem.seeds("dev")[: self.knobs["slow_seeds"]]
            )
        )["score"]

    def finish(self, actor_finals=()):
        """End of run (11.8): actor finals, the integrator's closeout, the runner's final and final_check."""
        visible_delay_seconds = max(1.0, float(self.knobs.get("visible_delay_s", 1)))
        for actor, ticket in actor_finals:
            self.client(actor).final(ticket=ticket)
        version = self.integrator.finalize(
            wait=lambda: self.clock.advance(visible_delay_seconds)
        )
        self.runner.final(ticket=version.ticket)
        self.runner.final_check(version.ticket)
        return self.events()

    def events(self):
        return read_events(self.events_path())

    def events_path(self) -> Path:
        return self.core.data / "runs" / self.run_id / "events.jsonl"

    def close(self):
        self.core.close()
