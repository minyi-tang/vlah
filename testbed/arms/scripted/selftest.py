"""tb selftest: run the three scripted fake herds on the stub problem and check the scorecard
(spec 11.9). No LLM; a few seconds."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from testbed.arms.scripted.env import ScriptedEnv
from testbed.arms.scripted.scenarios import EXPECT, SCENARIOS, SELFTEST_KNOBS
from testbed.scorecard.metrics import scorecard
from testbed.scorecard.report import render_many


def run_scenario(name, workdir: Path, problem="stub"):
    fn, arm, n = SCENARIOS[name]
    env = ScriptedEnv(
        workdir / name,
        problem=problem,
        arm=arm,
        n=n,
        run_id=f"r-self-{name}",
        overrides=SELFTEST_KNOBS,
    )
    try:
        events = fn(env)
        out = workdir / f"{name}.events.jsonl"
        shutil.copy(env.events_path(), out)
        return events, out
    finally:
        env.close()


def check(name, events):
    sc = scorecard(events)
    bad = []
    for key, want in EXPECT[name].items():
        got = sc["metrics"][key]["ok"]
        if got is not want:
            bad.append(
                f"{name}.{key}: expected {'green' if want else 'red'}, got {got} ({sc['metrics'][key]})"
            )
    return sc, bad


def main(a) -> int:
    wd = Path(a.out) if a.out else Path(tempfile.mkdtemp(prefix="tb-selftest-"))
    wd.mkdir(parents=True, exist_ok=True)
    runs, problems = [], []
    for name in SCENARIOS:
        events, path = run_scenario(name, wd, a.problem)
        _, bad = check(name, events)
        problems += bad
        runs.append(events)
        print(f"{name:13s} {'OK' if not bad else 'MISMATCH'}   events: {path}")
    print()
    print(render_many(runs))
    for b in problems:
        print("  !! " + b)
    print(f"\nSELFTEST {'PASS' if not problems else 'FAIL'}")
    return 0 if not problems else 1
