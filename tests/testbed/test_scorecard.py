"""Spec 11.9: the three scripted fake herds produce the expected red/green pattern."""

import pytest

from testbed.arms.scripted.scenarios import EXPECT
from testbed.arms.scripted.selftest import check, run_scenario
from testbed.recorder.log import Recorder, SchemaError, read_events
from testbed.scorecard.report import render_many, render_one


@pytest.mark.parametrize("name", list(EXPECT))
def test_scripted_scorecards(name, tmp_path):
    events, path = run_scenario(name, tmp_path)
    sc, bad = check(name, events)
    assert not bad, bad
    assert render_one(read_events(path))


def test_render_many(tmp_path):
    runs = [run_scenario(n, tmp_path)[0] for n in ("perfect_herd", "lone_wolf")]
    out = render_many(runs, fmt="md")
    assert "Breadth" in out and out.count("|") > 20


def test_recorder_is_append_only_and_validates(tmp_path):
    r = Recorder(tmp_path / "e.jsonl", "r1", "herd")
    r.write(
        "fast_run",
        {"hash": "h", "seed": 1, "score": 1.0, "baseline_score": 0.0},
        ts=1.0,
        actor="w1",
    )
    with pytest.raises(SchemaError):
        r.write(
            "blackboard_write", {"kind": "dead_end", "id": "x", "refs": []}, ts=2.0
        )  # dead_end needs hash
    with pytest.raises(SchemaError):
        r.write("submit", {"ticket": "t"}, ts=3.0)
    r.write(
        "anything_new", {"free": "form"}, ts=4.0
    )  # unknown types are accepted (POST /events takes any type)
    assert [e["type"] for e in read_events(tmp_path / "e.jsonl")] == [
        "fast_run",
        "anything_new",
    ]
