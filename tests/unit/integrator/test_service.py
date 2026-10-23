"""The Integrator wrapper: FIFO, write-once records, interactions, the map and the config (D74, D76)."""

import math
from pathlib import Path

import pytest

from testbed.integrator.core import IntegratorCore
from testbed.problem_api import se
from tests.fixtures.integrator import WORKER_ONE, WORKER_TWO, make_integrator
from vlah.arms.config import PRESETS
from vlah.core.integrator.core_adapter import config_from_params
from vlah.core.integrator.opportunity_map import run_script
from vlah.core.integrator.service import HerdIntegrator
from vlah.core.types.events import BaselineAdvanced, MapUpdated, RemeasureReady
from vlah.core.types.params import Params

pytestmark = pytest.mark.integrator

NOISE = (0.1, -0.1, 0.1, -0.1, 0.0)
SCRIPT_PATH = "map/measurements/gap_by_cause/measure.py"


def naming_runner(script: Path, baseline_directory: Path) -> str:
    return f"{script.name} on baseline@{baseline_directory.name}"


def advanced_candidates(harness_events: list[BaselineAdvanced]) -> list[str]:
    return [payload.candidate for payload in harness_events]


def test_candidates_integrate_first_in_first_out(tmp_path: Path) -> None:
    harness = make_integrator(tmp_path)
    smaller = harness.verify(WORKER_TWO, {"b.txt": "B\n"}, gain=1.0)
    larger = harness.verify(WORKER_ONE, {"a.txt": "A\n"}, gain=2.0)
    harness.run()
    advanced = [
        event.data
        for event in harness.events("baseline_advanced")
        if isinstance(event.data, BaselineAdvanced)
    ]
    assert advanced_candidates(advanced) == [smaller, larger]
    assert [payload.baseline for payload in advanced] == [1, 2]
    assert harness.integrator.current().candidates == (smaller, larger)


def test_baseline_records_are_written_once_and_never_rewritten(tmp_path: Path) -> None:
    harness = make_integrator(tmp_path)
    harness.verify(WORKER_ONE, {"a.txt": "A\n"}, gain=2.0)
    harness.verify(WORKER_TWO, {"b.txt": "B\n"}, gain=1.0)
    harness.run()
    assert harness.blackboard.list_paths("baseline/", None) == (
        "baseline/0.json",
        "baseline/1.json",
        "baseline/2.json",
    )
    before = harness.blackboard.read("baseline/1.json", None)
    harness.integrator.records.write_baseline(
        harness.integrator.core.versions[1], guard_pending=True, gates={"lint": False}
    )
    assert harness.blackboard.read("baseline/1.json", None) == before


@pytest.mark.parametrize(
    ("factor", "is_found"), [(1.1, True), (0.9, False)], ids=["above", "below"]
)
def test_an_interaction_is_recorded_at_two_sigma_root_two(
    tmp_path: Path, factor: float, is_found: bool
) -> None:
    """earlier gain - remeasured gain > 2 * sqrt(se_earlier^2 + se_remeasured^2)."""
    harness = make_integrator(tmp_path)
    harness.verify(WORKER_ONE, {"a.txt": "A\n"}, gain=2.0)
    harness.verify(WORKER_TWO, {"b.txt": "B\n"}, gain=1.0, noise=NOISE)
    standard_error = se(list(NOISE))
    threshold = Params().interaction_sigma_multiple * math.sqrt(standard_error**2)
    harness.measurer.combined_gain_by_markers[("A\n", "B\n")] = -factor * threshold
    harness.run()
    assert len(harness.events("baseline_advanced")) == 2
    assert bool(harness.events("interaction_found")) is is_found


def test_map_outputs_are_written_per_baseline(tmp_path: Path) -> None:
    harness = make_integrator(
        tmp_path,
        map_scripts={SCRIPT_PATH: b"print('measure')\n"},
        script_runner=naming_runner,
    )
    assert harness.blackboard.list_paths("map/gap.md", None) == ("map/gap.md",)
    harness.verify(WORKER_ONE, {"a.txt": "A\n"}, gain=2.0)
    harness.run()
    outputs = [
        path
        for path in harness.blackboard.list_paths("map/measurements/", None)
        if path.endswith("output.txt")
    ]
    assert outputs == [
        "map/measurements/gap_by_cause/0/output.txt",
        "map/measurements/gap_by_cause/1/output.txt",
    ]
    assert b"measure.py on baseline@1" in harness.blackboard.read(outputs[1], None)
    updates = [
        event.data
        for event in harness.events("map_updated")
        if isinstance(event.data, MapUpdated)
    ]
    assert [update.baseline for update in updates] == [0, 1]
    assert updates[1].measurements == ("gap_by_cause",)


def test_b1_measures_the_map_only_on_the_initial_baseline(tmp_path: Path) -> None:
    harness = make_integrator(
        tmp_path,
        arm=PRESETS["B1"],
        map_scripts={SCRIPT_PATH: b"print('measure')\n"},
        script_runner=naming_runner,
    )
    harness.verify(WORKER_ONE, {"a.txt": "A\n"}, gain=2.0)
    harness.run()
    assert len(harness.events("baseline_advanced")) == 1
    assert len(harness.events("map_updated")) == 1
    assert harness.blackboard.list_paths("map/measurements/gap_by_cause/1/", None) == ()


def test_a_restarted_integrator_does_not_measure_the_map_again(tmp_path: Path) -> None:
    harness = make_integrator(
        tmp_path, map_scripts={SCRIPT_PATH: b""}, script_runner=naming_runner
    )
    gap_before = harness.blackboard.read("map/gap.md", None)
    restarted = HerdIntegrator(
        **{
            name: getattr(harness.integrator, name)
            for name in (
                "blackboard",
                "eval_service",
                "task_queue",
                "compose_queue",
                "gates",
                "gate_runner",
                "arm",
                "params",
                "clock",
                "top_level",
                "work_directory",
                "baseline_directory",
                "calibration",
                "measurer",
                "script_runner",
            )
        }
    )
    assert restarted.records.events_written == []
    assert restarted.records.event_count == 1, "numbering continues after the first"
    assert harness.blackboard.read("map/gap.md", None) == gap_before
    assert harness.blackboard.list_paths("map/measurements/gap_by_cause/", None) == (
        "map/measurements/gap_by_cause/0/output.txt",
        SCRIPT_PATH,
    )


def test_the_default_runner_runs_the_script_on_a_copy(tmp_path: Path) -> None:
    script = tmp_path / "measure.py"
    script.write_text(
        "import pathlib, sys\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "(root / 'scratch.txt').write_text('x')\n"
        "print(sorted(path.name for path in root.iterdir()))\n",
        encoding="utf-8",
    )
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "params.yaml").write_text("window: 10\n", encoding="utf-8")
    output = run_script(script, baseline)
    assert output == "['params.yaml', 'scratch.txt']\n"
    assert sorted(path.name for path in baseline.iterdir()) == ["params.yaml"]


def test_the_core_config_is_the_same_in_every_arm(tmp_path: Path) -> None:
    cores = [
        make_integrator(tmp_path / name, arm=PRESETS[name]).integrator.core
        for name in ("B1", "S1", "S2")
    ]
    assert all(isinstance(core, IntegratorCore) for core in cores)
    configs = [core.cfg for core in cores if isinstance(core, IntegratorCore)]
    assert configs[0] == configs[1] == configs[2] == config_from_params(Params())


def test_a_remeasure_answer_uses_the_events_own_scores(tmp_path: Path) -> None:
    """result.json is written once per hash, so a second measurement of the same
    content and target (an audit of a reverted baseline) is read from the event."""
    harness = make_integrator(tmp_path)
    harness.integrator.answer(
        RemeasureReady(
            request_id="request-7",
            candidate="ab" * 32,
            parent_candidate="baseline@2",
            claim_id="c-eval_service-00009",
            baseline="baseline@2",
            paired=None,
            score=11.0,
            level="slow",
            constraint_ok=True,
            per_seed=(11.0, 11.5, 10.5, 11.0, 11.0),
            dataset="audit",
        )
    )
    measurement = harness.integrator.eval_measurer.result("request-7")
    assert measurement is not None
    assert measurement.per_seed == [11.0, 11.5, 10.5, 11.0, 11.0]
    assert measurement.constraint_ok is True
    harness.integrator.answer(
        RemeasureReady(
            request_id="request-8",
            candidate="cd" * 32,
            parent_candidate="baseline@2",
            claim_id="c-eval_service-00010",
            baseline="baseline@2",
            paired=None,
            score=None,
            level="slow",
            constraint_ok=None,
        )
    )
    void = harness.integrator.eval_measurer.result("request-8")
    assert void is not None and void.constraint_ok is False and void.per_seed == []
