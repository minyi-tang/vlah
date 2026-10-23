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
    scenario, arm, worker_count = SCENARIOS[name]
    env = ScriptedEnv(
        workdir / name,
        problem=problem,
        arm=arm,
        n=worker_count,
        run_id=f"r-self-{name}",
        overrides=SELFTEST_KNOBS,
    )
    try:
        events = scenario(env)
        events_copy = workdir / f"{name}.events.jsonl"
        shutil.copy(env.events_path(), events_copy)
        return events, events_copy
    finally:
        env.close()


def check(name, events):
    card = scorecard(events)
    mismatches = []
    for metric_key, want in EXPECT[name].items():
        got = card["metrics"][metric_key]["ok"]
        if got is not want:
            mismatches.append(
                f"{name}.{metric_key}: expected {'green' if want else 'red'}, got {got}"
                f" ({card['metrics'][metric_key]})"
            )
    return card, mismatches


def main(arguments) -> int:
    workdir = (
        Path(arguments.out)
        if arguments.out
        else Path(tempfile.mkdtemp(prefix="tb-selftest-"))
    )
    workdir.mkdir(parents=True, exist_ok=True)
    runs, mismatches = [], []
    for name in SCENARIOS:
        events, events_path = run_scenario(name, workdir, arguments.problem)
        _, scenario_mismatches = check(name, events)
        mismatches += scenario_mismatches
        runs.append(events)
        print(
            f"{name:13s} {'OK' if not scenario_mismatches else 'MISMATCH'}   events: {events_path}"
        )
    print()
    print(render_many(runs))
    for mismatch in mismatches:
        print("  !! " + mismatch)
    print(f"\nSELFTEST {'PASS' if not mismatches else 'FAIL'}")
    return 0 if not mismatches else 1
