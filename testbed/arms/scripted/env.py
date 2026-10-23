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
        self.wd = Path(workdir or tempfile.mkdtemp(prefix="tb-scripted-"))
        self.clock = ManualClock()
        self.core = SlowEval(self.wd / "slow", clock=self.clock, inline=True)
        self.http = TestClient(
            create_app(self.wd / "slow", core=self.core, publisher=False)
        )
        self.run_id, self.arm, self.problem_name = run_id, arm, problem
        ov = dict(overrides or {})
        if arm == "isolated":
            ov.setdefault("per_actor_share", True)
        self.runner = SlowClient(
            None, run_id, "runner", http=self.http, key=self.core.system_key
        )
        self.runner.create_run(arm, problem, n, ov)
        self.t0 = self.clock.now()
        self.problem = load_problem(problem)
        self.knobs = self.core.knobs(run_id)
        self.latest = None
        self.integ = Integrator(
            self.runner,
            self.wd / "integrator",
            self.problem.baseline_dir(),
            self.clock.now,
            config=config or IntegratorConfig(),
            composer=composer,
            on_advance=self._on_advance if arm in PUSH_ARMS else None,
        )
        self.latest = self.integ.versions[0]
        self._n = 0

    # ---------------------------------------------------------------- time
    def now(self):
        return self.clock.now() - self.t0

    def advance_to(self, t_rel: float):
        target = self.t0 + t_rel
        delay = max(1.0, float(self.knobs.get("visible_delay_s", 1)))
        for _ in range(100_000):
            self.integ.step()
            now = self.clock.now()
            if now >= target:
                break
            due = self.integ.due(now)
            if due == "now":
                nxt = now + 0.001
            elif due == "wait":
                nxt = now + delay
            elif isinstance(due, (int, float)):
                nxt = due
            else:
                nxt = target
            pending = self.core.db.execute(
                "SELECT MIN(visible_at) FROM tickets WHERE run_id=? AND published=0 AND dup_of IS NULL",
                (self.run_id,),
            ).fetchone()[0]
            if pending is not None and pending > now:
                nxt = min(nxt, pending)
            self.clock.t = min(max(nxt, now + 0.001), target)
        self.integ.step()

    def sleep(self, dt):
        self.advance_to(self.now() + dt)

    # ---------------------------------------------------------------- agents' tools
    def _on_advance(self, v):
        self.latest = v

    def client(self, actor):
        return SlowClient(None, self.run_id, actor, http=self.http)

    def base_for(self, actor):
        """What the agent sees as its baseline: pushed versions for single/herd, baseline@0 otherwise."""
        v = self.latest if self.arm in PUSH_ARMS else self.integ.versions[0]
        return v.k, Path(v.dir)

    def make(self, add=(), base_dir=None, **params) -> Path:
        self._n += 1
        d = self.wd / "work" / f"c{self._n}"
        copy_tree(base_dir or self.problem.baseline_dir(), d)
        p = d / "params.yaml"
        prm = yaml.safe_load(p.read_text()) if p.exists() else {}
        prm = prm or {}
        ings = dict(prm.get("ingredients") or {})
        for a in add:
            ings[a] = True
        prm["ingredients"] = ings
        prm.update(params)
        p.write_text(yaml.safe_dump(prm, sort_keys=True))
        return d

    def fast_run(self, actor, d: Path, target: str, seed=1) -> dict:
        res = self.problem.evaluate(d, "fast", seed)
        base = self.problem.evaluate(self.problem.baseline_dir(), "fast", seed)["score"]
        h = candidate_hash(d, target)
        self.client(actor).post_events(
            [
                {
                    "type": "fast_run",
                    "data": {
                        "hash": h,
                        "seed": seed,
                        "score": res["score"],
                        "baseline_score": base,
                    },
                }
            ]
        )
        return {"hash": h, "score": res["score"], "baseline_score": base}

    def submit(self, actor, d: Path, predicted: float, target: str, **kw):
        return self.client(actor).submit_dir(
            d, predicted=predicted, target=target, **kw
        )

    def herd_event(self, actor, type_, data):
        self.client(actor).post_events([{"type": type_, "data": data}])

    def true_score(self, d: Path) -> float:
        return self.problem.aggregate(
            self.problem.evaluate_many(
                d, self.problem.seeds("dev")[: self.knobs["slow_seeds"]]
            )
        )["score"]

    # ---------------------------------------------------------------- end of run (11.8)
    def finish(self, actor_finals=()):
        delay = max(1.0, float(self.knobs.get("visible_delay_s", 1)))
        for actor, ticket in actor_finals:
            self.client(actor).final(ticket=ticket)
        v = self.integ.finalize(wait=lambda: self.clock.advance(delay))
        self.runner.final(ticket=v.ticket)
        self.runner.final_check(v.ticket)
        return self.events()

    def events(self):
        return read_events(self.core.data / "runs" / self.run_id / "events.jsonl")

    def events_path(self) -> Path:
        return self.core.data / "runs" / self.run_id / "events.jsonl"

    def close(self):
        self.core.close()
