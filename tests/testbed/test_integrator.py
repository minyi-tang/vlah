"""Spec 11.8.1 integrator tests (scripted, no LLM) on the stub problem."""

from pathlib import Path

import pytest
import yaml

from testbed.arms.runner import integrator_config
from testbed.arms.scripted.env import ScriptedEnv
from testbed.arms.scripted.scenarios import SELFTEST_KNOBS
from testbed.integrator.core import IntegratorConfig
from testbed.integrator.merge import Conflict, copy_tree, merge_yaml

HOUR_SECONDS = 3600


def make_env(workdir, arm="isolated", worker_count=3, composer=None, **knob_overrides):
    return ScriptedEnv(
        workdir,
        arm=arm,
        n=worker_count,
        run_id="r-int",
        overrides=dict(SELFTEST_KNOBS, **knob_overrides),
        composer=composer,
    )


def events_of(env, event_type):
    return [event for event in env.events() if event["type"] == event_type]


def remeasures(env):
    return [
        event
        for event in events_of(env, "submit")
        if event["actor"] == "integrator" and event["data"]["dataset"] == "dev"
    ]


def test_three_independent_candidates_make_three_baselines(tmp_path):
    env = make_env(tmp_path)
    env.advance_to(60)
    for actor, ingredient in [
        ("w1", "backfill"),
        ("w2", "admission"),
        ("w3", "locality"),
    ]:
        env.submit(actor, env.make([ingredient]), predicted=-900, target="baseline@0")
    env.advance_to(3 * HOUR_SECONDS)
    advanced = events_of(env, "baseline_advanced")
    assert [event["data"]["baseline"] for event in advanced] == [1, 2, 3]
    assert env.integrator.versions[-1].score == -700
    for k in range(4):
        assert (env.integrator.wd / "baseline" / f"{k}.json").exists()
    # isolated agents never see the moving baseline
    assert env.base_for("w1")[0] == 0
    env.close()


def test_moved_baseline_costs_exactly_one_remeasure(tmp_path):
    env = make_env(tmp_path)
    env.advance_to(60)
    env.submit("w1", env.make(["backfill"]), predicted=-900, target="baseline@0")
    env.submit("w2", env.make(["admission"]), predicted=-900, target="baseline@0")
    env.advance_to(HOUR_SECONDS / 2)
    assert len(events_of(env, "baseline_advanced")) == 2
    assert len(remeasures(env)) == 1
    env.close()


def test_text_or_key_conflict_spawns_compose_task(tmp_path):
    calls = []

    def composer(task):  # scripted "agent": keep ours, add the admission switch
        calls.append(task)
        composed_dir = Path(task["ours_dir"]).parent.parent / "composed"
        copy_tree(Path(task["ours_dir"]), composed_dir)
        params_yaml = yaml.safe_load((composed_dir / "params.yaml").read_text())
        params_yaml["ingredients"]["admission"] = True
        (composed_dir / "params.yaml").write_text(yaml.safe_dump(params_yaml))
        return composed_dir

    env = make_env(tmp_path, composer=composer)
    env.advance_to(60)
    env.submit(
        "w1", env.make(["backfill"], window=10), predicted=-900, target="baseline@0"
    )
    env.submit(
        "w2", env.make(["admission"], window=20), predicted=-900, target="baseline@0"
    )
    env.advance_to(2 * HOUR_SECONDS)
    failures = events_of(env, "integration_failed")
    assert [failure["data"]["reason"] for failure in failures] == ["conflict"]
    assert len(events_of(env, "compose_task")) == 1 and len(calls) == 1
    composed = [
        submit for submit in events_of(env, "submit") if submit["actor"] == "composer"
    ]
    assert len(composed) == 1 and composed[0]["data"]["target"] == "baseline@1"
    assert [
        event["data"]["baseline"] for event in events_of(env, "baseline_advanced")
    ] == [1, 2]
    env.close()


def test_gain_below_one_se_is_not_merged(tmp_path):
    env = make_env(tmp_path)
    env.advance_to(60)
    env.submit("w1", env.make([], harmless=1), predicted=-1000, target="baseline@0")
    env.advance_to(600)
    assert events_of(env, "baseline_advanced") == []
    assert [
        failure["data"]["reason"] for failure in events_of(env, "integration_failed")
    ] == ["below_1se"]
    env.close()


def test_dev_only_candidate_is_caught_by_audit_and_reverted(tmp_path):
    env = make_env(tmp_path, arm="herd")
    env.advance_to(60)
    env.submit("w1", env.make(["backfill"]), predicted=-900, target="baseline@0")
    env.advance_to(120)
    base_k, base_dir = env.base_for("w2")
    assert base_k == 1
    bad = env.make(["admission"], base_dir=base_dir, dev_only_bonus=300)
    env.submit("w2", bad, predicted=-500, target="baseline@1")
    env.advance_to(HOUR_SECONDS)
    audits = events_of(env, "audit_checked")
    assert audits and audits[0]["data"]["flagged"] is True
    reverts = events_of(env, "baseline_reverted")
    assert len(reverts) == 1 and reverts[0]["data"]["new_baseline"] == 3
    bad_hash = [
        event
        for event in events_of(env, "baseline_advanced")
        if event["data"]["baseline"] == 2
    ][0]["data"]["candidate"]
    assert reverts[0]["data"]["removed"] == [bad_hash]
    assert [event["data"]["candidate"] for event in events_of(env, "quarantined")] == [
        bad_hash
    ]
    version = env.integrator.versions[-1]
    assert (
        version.score == env.integrator.versions[1].score
        and bad_hash not in version.candidates
    )
    env.close()


def test_clean_run_seven_audits_no_alarm(tmp_path):
    env = make_env(tmp_path, arm="herd")
    env.advance_to(60)
    for i in range(14):
        base_k, base_dir = env.base_for("w1")
        params_yaml = yaml.safe_load((base_dir / "params.yaml").read_text())
        bonus = dict(params_yaml.get("bonus") or {}, **{f"b{i}": 10})
        env.submit(
            "w1",
            env.make([], base_dir=base_dir, bonus=bonus),
            predicted=-900,
            target=f"baseline@{base_k}",
        )
        env.sleep(30)
    env.advance_to(2 * HOUR_SECONDS)
    audits = events_of(env, "audit_checked")
    assert len(events_of(env, "baseline_advanced")) == 14
    assert len(audits) == 7 and not any(audit["data"]["flagged"] for audit in audits)
    env.close()


def test_integrator_identical_across_arms(tmp_path):
    configs = [integrator_config(arm) for arm in ("single", "isolated", "herd")]
    assert configs[0] == configs[1] == configs[2] and isinstance(
        configs[0], IntegratorConfig
    )
    knobs = []
    for arm in ("single", "isolated", "herd"):
        env = ScriptedEnv(
            tmp_path / arm, arm=arm, n=3, run_id=f"r-{arm}", overrides=SELFTEST_KNOBS
        )
        knobs.append(
            {
                knob: value
                for knob, value in env.knobs.items()
                if "integrator" in knob or knob.startswith("audit")
            }
        )
        env.close()
    assert knobs[0] == knobs[1] == knobs[2]


def test_final_is_last_baseline_and_actor_final_is_separate(tmp_path):
    env = make_env(tmp_path, arm="single", worker_count=1)
    env.advance_to(60)
    receipt = env.submit(
        "solo", env.make(["backfill"]), predicted=-900, target="baseline@0"
    )
    env.advance_to(600)
    events = env.finish(actor_finals=[("solo", receipt["ticket"])])
    finals = [event for event in events if event["type"] == "final"]
    assert (
        len(finals) == 1 and finals[0]["data"]["ticket"] == env.integrator.frozen.ticket
    )
    assert len([event for event in events if event["type"] == "actor_final"]) == 1
    assert [
        event["data"]["dataset"] for event in events if event["type"] == "final_check"
    ] == ["public"]
    env.close()


def test_stale_target_label_is_detected_and_rebased(tmp_path):
    """A candidate built on baseline@0 but labelled with the pushed baseline@1 must not drop merge 1."""
    env = make_env(tmp_path, arm="herd")
    env.advance_to(60)
    old = env.make(["admission", "locality"])  # built from baseline@0 ...
    env.submit("w1", env.make(["backfill"]), predicted=-900, target="baseline@0")
    env.advance_to(120)
    assert env.base_for("w2")[0] == 1
    env.submit(
        "w2", old, predicted=-800, target="baseline@1"
    )  # ... but labelled baseline@1
    env.advance_to(2 * HOUR_SECONDS)
    stale = events_of(env, "stale_target")
    assert (
        len(stale) == 1
        and stale[0]["data"]["claimed"] == 1
        and stale[0]["data"]["effective"] == 0
    )
    version = env.integrator.versions[-1]
    assert version.score == -700  # backfill + admission + locality, nothing lost
    env.close()


def test_compose_waits_for_its_quota_slot(tmp_path):
    def composer(task):
        composed_dir = (
            Path(task["ours_dir"]).parent.parent / f"composed-{task['task_id']}"
        )
        copy_tree(Path(task["ours_dir"]), composed_dir)
        params_yaml = yaml.safe_load((composed_dir / "params.yaml").read_text())
        params_yaml["ingredients"]["locality"] = True
        (composed_dir / "params.yaml").write_text(yaml.safe_dump(params_yaml))
        return composed_dir

    env = make_env(tmp_path, composer=composer)
    env.advance_to(60)
    env.submit(
        "w1", env.make(["backfill"], window=10), predicted=-900, target="baseline@0"
    )
    env.submit(
        "w2", env.make(["admission"]), predicted=-900, target="baseline@0"
    )  # uses the hourly re-measure
    env.submit(
        "w3", env.make(["locality"], window=20), predicted=-900, target="baseline@0"
    )  # conflicts with w1
    env.advance_to(3 * HOUR_SECONDS)
    rejected = [
        event
        for event in events_of(env, "quota_rejected")
        if event["actor"] == "composer"
    ]
    composed = [
        submit for submit in events_of(env, "submit") if submit["actor"] == "composer"
    ]
    assert len(composed) == 1 and len(rejected) <= 1
    assert env.integrator.versions[-1].score == -700
    env.close()


def test_yaml_merge_rules():
    assert merge_yaml({}, {"b": {"x": 1}}, {"b": {"x": 1, "y": 2}}) == {
        "b": {"x": 1, "y": 2}
    }
    assert merge_yaml({"a": 1}, {"a": 1, "k": 2}, {"a": 3}) == {"a": 3, "k": 2}
    with pytest.raises(Conflict):
        merge_yaml({"a": 1}, {"a": 2}, {"a": 3})


def test_quarantined_content_is_never_merged_again(tmp_path):
    env = make_env(tmp_path, arm="herd")
    env.advance_to(60)
    env.submit("w1", env.make(["backfill"]), predicted=-900, target="baseline@0")
    env.advance_to(120)
    bad = env.make(["admission"], base_dir=env.base_for("w2")[1], dev_only_bonus=300)
    env.submit("w2", bad, predicted=-500, target="baseline@1")
    env.advance_to(HOUR_SECONDS)
    assert len(events_of(env, "baseline_reverted")) == 1
    base_k = env.base_for("w2")[0]
    env.submit(
        "w2", bad, predicted=-500, target=f"baseline@{base_k}"
    )  # same files, new label
    env.advance_to(3 * HOUR_SECONDS)
    assert events_of(env, "integration_failed")[-1]["data"]["reason"] == "quarantined"
    env.close()


def test_editing_a_pushed_change_is_not_called_stale(tmp_path):
    """A herd worker who changes a value the pushed baseline introduced is building on that baseline."""
    env = make_env(tmp_path, arm="herd")
    env.advance_to(60)
    env.submit(
        "w1", env.make(["backfill"], knob=1), predicted=-900, target="baseline@0"
    )
    env.advance_to(120)
    base_k, base_dir = env.base_for("w2")
    env.submit(
        "w2",
        env.make(["admission"], base_dir=base_dir, knob=2),
        predicted=-800,
        target=f"baseline@{base_k}",
    )
    env.advance_to(HOUR_SECONDS)
    assert events_of(env, "stale_target") == []
    assert [
        event["data"]["baseline"] for event in events_of(env, "baseline_advanced")
    ] == [1, 2]
    env.close()


def test_author_keeps_editing_after_a_merge_without_compose(tmp_path):
    """The single-arm default: work/ still says baseline@0 after the agent's own change was merged."""
    env = make_env(tmp_path, arm="single", worker_count=1)
    env.advance_to(60)
    first = env.make(["backfill"])
    env.submit("solo", first, predicted=-900, target="baseline@0")
    env.advance_to(120)
    assert env.base_for("solo")[0] == 1
    second = env.make(
        ["backfill", "admission"]
    )  # keeps editing the same line of work, label unchanged
    env.submit("solo", second, predicted=-800, target="baseline@0")
    env.advance_to(2 * HOUR_SECONDS)
    assert events_of(env, "compose_task") == []
    stale = events_of(env, "stale_target")
    assert (
        len(stale) == 1
        and stale[0]["data"]["claimed"] == 0
        and stale[0]["data"]["effective"] == 1
    )
    assert env.integrator.versions[-1].score == -800
    env.close()
