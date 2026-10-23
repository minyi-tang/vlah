"""Spec 11.8.1 integrator tests (scripted, no LLM) on the stub problem."""

from pathlib import Path

import yaml

from testbed.arms.scripted.env import ScriptedEnv
from testbed.arms.scripted.scenarios import SELFTEST_KNOBS
from testbed.integrator.core import IntegratorConfig
from testbed.integrator.merge import copy_tree

H = 3600


def mk(tmp, arm="isolated", n=3, composer=None, **kw):
    return ScriptedEnv(
        tmp,
        arm=arm,
        n=n,
        run_id="r-int",
        overrides=dict(SELFTEST_KNOBS, **kw),
        composer=composer,
    )


def ev(env, t):
    return [e for e in env.events() if e["type"] == t]


def remeasures(env):
    return [
        e
        for e in ev(env, "submit")
        if e["actor"] == "integrator" and e["data"]["dataset"] == "dev"
    ]


def test_three_independent_candidates_make_three_baselines(tmp_path):
    env = mk(tmp_path)
    env.advance_to(60)
    for actor, ing in [("w1", "backfill"), ("w2", "admission"), ("w3", "locality")]:
        env.submit(actor, env.make([ing]), predicted=-900, target="baseline@0")
    env.advance_to(3 * H)
    adv = ev(env, "baseline_advanced")
    assert [a["data"]["baseline"] for a in adv] == [1, 2, 3]
    assert env.integ.versions[-1].score == -700
    for k in range(4):
        assert (env.integ.wd / "baseline" / f"{k}.json").exists()
    # isolated agents never see the moving baseline
    assert env.base_for("w1")[0] == 0
    env.close()


def test_moved_baseline_costs_exactly_one_remeasure(tmp_path):
    env = mk(tmp_path)
    env.advance_to(60)
    env.submit("w1", env.make(["backfill"]), predicted=-900, target="baseline@0")
    env.submit("w2", env.make(["admission"]), predicted=-900, target="baseline@0")
    env.advance_to(H / 2)
    assert len(ev(env, "baseline_advanced")) == 2
    assert len(remeasures(env)) == 1
    env.close()


def test_text_or_key_conflict_spawns_compose_task(tmp_path):
    calls = []

    def composer(task):  # scripted "agent": keep ours, add the admission switch
        calls.append(task)
        out = Path(task["ours_dir"]).parent.parent / "composed"
        copy_tree(Path(task["ours_dir"]), out)
        prm = yaml.safe_load((out / "params.yaml").read_text())
        prm["ingredients"]["admission"] = True
        (out / "params.yaml").write_text(yaml.safe_dump(prm))
        return out

    env = mk(tmp_path, composer=composer)
    env.advance_to(60)
    env.submit(
        "w1", env.make(["backfill"], window=10), predicted=-900, target="baseline@0"
    )
    env.submit(
        "w2", env.make(["admission"], window=20), predicted=-900, target="baseline@0"
    )
    env.advance_to(2 * H)
    fails = ev(env, "integration_failed")
    assert [f["data"]["reason"] for f in fails] == ["conflict"]
    assert len(ev(env, "compose_task")) == 1 and len(calls) == 1
    composed = [s for s in ev(env, "submit") if s["actor"] == "composer"]
    assert len(composed) == 1 and composed[0]["data"]["target"] == "baseline@1"
    assert [a["data"]["baseline"] for a in ev(env, "baseline_advanced")] == [1, 2]
    env.close()


def test_gain_below_one_se_is_not_merged(tmp_path):
    env = mk(tmp_path)
    env.advance_to(60)
    env.submit("w1", env.make([], harmless=1), predicted=-1000, target="baseline@0")
    env.advance_to(600)
    assert ev(env, "baseline_advanced") == []
    assert [f["data"]["reason"] for f in ev(env, "integration_failed")] == ["below_1se"]
    env.close()


def test_dev_only_candidate_is_caught_by_audit_and_reverted(tmp_path):
    env = mk(tmp_path, arm="herd")
    env.advance_to(60)
    env.submit("w1", env.make(["backfill"]), predicted=-900, target="baseline@0")
    env.advance_to(120)
    k, bdir = env.base_for("w2")
    assert k == 1
    bad = env.make(["admission"], base_dir=bdir, dev_only_bonus=300)
    env.submit("w2", bad, predicted=-500, target="baseline@1")
    env.advance_to(H)
    audits = ev(env, "audit_checked")
    assert audits and audits[0]["data"]["flagged"] is True
    rev = ev(env, "baseline_reverted")
    assert len(rev) == 1 and rev[0]["data"]["new_baseline"] == 3
    bad_hash = [a for a in ev(env, "baseline_advanced") if a["data"]["baseline"] == 2][
        0
    ]["data"]["candidate"]
    assert rev[0]["data"]["removed"] == [bad_hash]
    assert [q["data"]["candidate"] for q in ev(env, "quarantined")] == [bad_hash]
    v = env.integ.versions[-1]
    assert v.score == env.integ.versions[1].score and bad_hash not in v.candidates
    env.close()


def test_clean_run_seven_audits_no_alarm(tmp_path):
    env = mk(tmp_path, arm="herd")
    env.advance_to(60)
    for i in range(14):
        k, bdir = env.base_for("w1")
        prm = yaml.safe_load((bdir / "params.yaml").read_text())
        bonus = dict(prm.get("bonus") or {}, **{f"b{i}": 10})
        env.submit(
            "w1",
            env.make([], base_dir=bdir, bonus=bonus),
            predicted=-900,
            target=f"baseline@{k}",
        )
        env.sleep(30)
    env.advance_to(2 * H)
    audits = ev(env, "audit_checked")
    assert len(ev(env, "baseline_advanced")) == 14
    assert len(audits) == 7 and not any(a["data"]["flagged"] for a in audits)
    env.close()


def test_integrator_identical_across_arms(tmp_path):
    from testbed.arms.runner import integrator_config

    cfgs = [integrator_config(arm) for arm in ("single", "isolated", "herd")]
    assert cfgs[0] == cfgs[1] == cfgs[2] and isinstance(cfgs[0], IntegratorConfig)
    knobs = []
    for arm in ("single", "isolated", "herd"):
        env = ScriptedEnv(
            tmp_path / arm, arm=arm, n=3, run_id=f"r-{arm}", overrides=SELFTEST_KNOBS
        )
        knobs.append(
            {
                k: v
                for k, v in env.knobs.items()
                if "integrator" in k or k.startswith("audit")
            }
        )
        env.close()
    assert knobs[0] == knobs[1] == knobs[2]


def test_final_is_last_baseline_and_actor_final_is_separate(tmp_path):
    env = mk(tmp_path, arm="single", n=1)
    env.advance_to(60)
    r = env.submit("solo", env.make(["backfill"]), predicted=-900, target="baseline@0")
    env.advance_to(600)
    events = env.finish(actor_finals=[("solo", r["ticket"])])
    finals = [e for e in events if e["type"] == "final"]
    assert len(finals) == 1 and finals[0]["data"]["ticket"] == env.integ.frozen.ticket
    assert len([e for e in events if e["type"] == "actor_final"]) == 1
    assert [e["data"]["dataset"] for e in events if e["type"] == "final_check"] == [
        "public"
    ]
    env.close()


def test_stale_target_label_is_detected_and_rebased(tmp_path):
    """A candidate built on baseline@0 but labelled with the pushed baseline@1 must not drop merge 1."""
    env = mk(tmp_path, arm="herd")
    env.advance_to(60)
    old = env.make(["admission", "locality"])  # built from baseline@0 ...
    env.submit("w1", env.make(["backfill"]), predicted=-900, target="baseline@0")
    env.advance_to(120)
    assert env.base_for("w2")[0] == 1
    env.submit(
        "w2", old, predicted=-800, target="baseline@1"
    )  # ... but labelled baseline@1
    env.advance_to(2 * H)
    stale = ev(env, "stale_target")
    assert (
        len(stale) == 1
        and stale[0]["data"]["claimed"] == 1
        and stale[0]["data"]["effective"] == 0
    )
    v = env.integ.versions[-1]
    assert v.score == -700  # backfill + admission + locality, nothing lost
    env.close()


def test_compose_waits_for_its_quota_slot(tmp_path):
    def composer(task):
        out = Path(task["ours_dir"]).parent.parent / f"composed-{task['task_id']}"
        copy_tree(Path(task["ours_dir"]), out)
        prm = yaml.safe_load((out / "params.yaml").read_text())
        prm["ingredients"]["locality"] = True
        (out / "params.yaml").write_text(yaml.safe_dump(prm))
        return out

    env = mk(tmp_path, composer=composer)
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
    env.advance_to(3 * H)
    rejected = [e for e in ev(env, "quota_rejected") if e["actor"] == "composer"]
    composed = [s for s in ev(env, "submit") if s["actor"] == "composer"]
    assert len(composed) == 1 and len(rejected) <= 1
    assert env.integ.versions[-1].score == -700
    env.close()


def test_yaml_merge_rules():
    import pytest

    from testbed.integrator.merge import Conflict, merge_yaml

    assert merge_yaml({}, {"b": {"x": 1}}, {"b": {"x": 1, "y": 2}}) == {
        "b": {"x": 1, "y": 2}
    }
    assert merge_yaml({"a": 1}, {"a": 1, "k": 2}, {"a": 3}) == {"a": 3, "k": 2}
    with pytest.raises(Conflict):
        merge_yaml({"a": 1}, {"a": 2}, {"a": 3})


def test_quarantined_content_is_never_merged_again(tmp_path):
    env = mk(tmp_path, arm="herd")
    env.advance_to(60)
    env.submit("w1", env.make(["backfill"]), predicted=-900, target="baseline@0")
    env.advance_to(120)
    bad = env.make(["admission"], base_dir=env.base_for("w2")[1], dev_only_bonus=300)
    env.submit("w2", bad, predicted=-500, target="baseline@1")
    env.advance_to(H)
    assert len(ev(env, "baseline_reverted")) == 1
    k = env.base_for("w2")[0]
    env.submit(
        "w2", bad, predicted=-500, target=f"baseline@{k}"
    )  # same files, new label
    env.advance_to(3 * H)
    assert ev(env, "integration_failed")[-1]["data"]["reason"] == "quarantined"
    env.close()


def test_editing_a_pushed_change_is_not_called_stale(tmp_path):
    """A herd worker who changes a value the pushed baseline introduced is building on that baseline."""
    env = mk(tmp_path, arm="herd")
    env.advance_to(60)
    env.submit(
        "w1", env.make(["backfill"], knob=1), predicted=-900, target="baseline@0"
    )
    env.advance_to(120)
    k, bdir = env.base_for("w2")
    env.submit(
        "w2",
        env.make(["admission"], base_dir=bdir, knob=2),
        predicted=-800,
        target=f"baseline@{k}",
    )
    env.advance_to(H)
    assert ev(env, "stale_target") == []
    assert [a["data"]["baseline"] for a in ev(env, "baseline_advanced")] == [1, 2]
    env.close()


def test_author_keeps_editing_after_a_merge_without_compose(tmp_path):
    """The single-arm default: work/ still says baseline@0 after the agent's own change was merged."""
    env = mk(tmp_path, arm="single", n=1)
    env.advance_to(60)
    first = env.make(["backfill"])
    env.submit("solo", first, predicted=-900, target="baseline@0")
    env.advance_to(120)
    assert env.base_for("solo")[0] == 1
    second = env.make(
        ["backfill", "admission"]
    )  # keeps editing the same line of work, label unchanged
    env.submit("solo", second, predicted=-800, target="baseline@0")
    env.advance_to(2 * H)
    assert ev(env, "compose_task") == []
    stale = ev(env, "stale_target")
    assert (
        len(stale) == 1
        and stale[0]["data"]["claimed"] == 0
        and stale[0]["data"]["effective"] == 1
    )
    assert env.integ.versions[-1].score == -800
    env.close()
