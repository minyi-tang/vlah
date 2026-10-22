"""The three scripted "fake herds" of spec 11.9. Each is a function of a ScriptedEnv; the score and
ingredients of a stub candidate are fixed by its params, so red/green depends only on behaviour."""

from __future__ import annotations

H = 3600.0
SELFTEST_KNOBS = {
    "slow_seeds": 2,
    "visible_delay_s": 1,
    "quota_per_hour": 100,
    "tilt_window_s": 10,
}


def _predict(true, rel_err):
    return true - rel_err * abs(true)


def _try(env, actor, d, target, rel_err, fast=True, **kw):
    """fast_run (optional) -> submit with a prediction off by rel_err -> wait for the result."""
    if fast:
        env.fast_run(actor, d, target)
    true = env.true_score(d)
    r = env.submit(actor, d, predicted=_predict(true, rel_err), target=target, **kw)
    env.sleep(2)
    return r, env.client(actor).status(r.get("of") or r["ticket"])


def perfect_herd(env):
    """6 actors; 5 ingredients touched in the first 25% of the run; trap once, then a dead_end;
    every submit preceded by a positive fast run; allocation right after the first big gain;
    predictions improve; final = 4 ingredients, no fairshare."""
    loose, tight = 0.30, 0.01
    env.advance_to(60)
    first = {}
    for actor, ing in [
        ("w1", "backfill"),
        ("w2", "admission"),
        ("w3", "locality"),
        ("w4", "packing"),
    ]:
        d = env.make([ing])
        r, st = _try(env, actor, d, "baseline@0", loose, direction=ing)
        first[actor] = (r, st)
        if actor == "w1":  # first big gain: tilt toward it
            env.herd_event(
                "w1",
                "blackboard_write",
                {
                    "kind": "insight",
                    "id": "ins-1",
                    "refs": [_hash(env, r)],
                    "text": "backfill: large gain at site 1",
                },
            )
            env.herd_event(
                "pi",
                "allocation",
                {
                    "hash": _hash(env, r),
                    "workers_before": 1,
                    "workers_after": 2,
                    "reason": "backfill: large gain at site 1",
                },
            )
    trap = env.make(["trap_sjf"])
    r, st = _try(env, "w6", trap, "baseline@0", loose, direction="sjf")
    assert st["result"]["constraint_ok"] is False
    env.herd_event(
        "w6",
        "blackboard_write",
        {
            "kind": "dead_end",
            "id": "de-1",
            "refs": [_hash(env, r)],
            "hash": _hash(env, r),
        },
    )
    env.advance_to(0.5 * H)
    # build on the pushed baseline: locality, then packing
    for actor, ing in [("w3", "locality"), ("w4", "packing")]:
        env.advance_to(env.now() + 600)
        k, bdir = env.base_for(actor)
        d = env.make([ing], base_dir=bdir)
        env.herd_event(actor, "blackboard_read", {"ids": ["ins-1", "de-1"]})
        _try(env, actor, d, f"baseline@{k}", tight, direction=ing, parents=[])
    # w5 checks the known conflict and records the trade-off instead of shipping it
    env.advance_to(2 * H)
    k, bdir = env.base_for("w5")
    d = env.make(["fairshare"], base_dir=bdir)
    r, st = _try(env, "w5", d, f"baseline@{k}", tight, direction="fairshare")
    env.herd_event(
        "w5",
        "blackboard_write",
        {
            "kind": "insight",
            "id": "ins-2",
            "refs": [_hash(env, r), env.latest.hash],
            "text": "backfill + strict fairshare cancel out; keep fairshare off",
        },
    )
    env.advance_to(8 * H)
    return env.finish()


def lone_wolf(env):
    """1 actor, two ingredients, submits the trap twice (the second time after seeing it fail)."""
    env.advance_to(60)
    _try(env, "solo", env.make(["backfill"]), "baseline@0", 0.2, direction="backfill")
    env.advance_to(0.5 * H)
    k, bdir = env.base_for("solo")
    _try(
        env,
        "solo",
        env.make(["trap_sjf"], base_dir=bdir),
        f"baseline@{k}",
        0.2,
        direction="sjf",
    )
    env.advance_to(1 * H)
    _try(
        env,
        "solo",
        env.make(["trap_sjf"], base_dir=bdir, aging_s=1),
        f"baseline@{k}",
        0.2,
        direction="sjf v2",
    )
    env.advance_to(3 * H)
    k, bdir = env.base_for("solo")
    _try(
        env,
        "solo",
        env.make(["admission"], base_dir=bdir),
        f"baseline@{k}",
        0.2,
        direction="admission",
    )
    env.advance_to(8 * H)
    return env.finish()


def quota_hog(env):
    """6 actors submit without fast runs and predict 0 every time."""
    env.advance_to(60)
    ings = ["backfill", "admission", "locality", "packing", "est_order", "fairshare"]
    for rnd in range(2):
        for i, ing in enumerate(ings):
            d = env.make([ing], tweak=rnd)
            env.submit(
                f"w{i + 1}", d, predicted=0.0, target="baseline@0", direction=ing
            )
            env.sleep(30)
    env.advance_to(4 * H)
    return env.finish()


def _hash(env, r):
    t = r.get("of") or r["ticket"]
    return env.core.ticket_row(env.run_id, t)["hash"]


SCENARIOS = {
    "perfect_herd": (perfect_herd, "herd", 6),
    "lone_wolf": (lone_wolf, "single", 1),
    "quota_hog": (quota_hog, "herd", 6),
}

# What the scorecard must say (spec 11.9). True = green, False = red; unlisted rows are free.
EXPECT = {
    "perfect_herd": {
        "breadth": True,
        "assembly": True,
        "capture": True,
        "dead_end_rehits": True,
        "conflict": True,
        "quota_discipline": True,
        "prediction_error": True,
        "tilt": True,
        "generalization": True,
    },
    "lone_wolf": {"breadth": False, "dead_end_rehits": False, "assembly": False},
    "quota_hog": {"quota_discipline": False, "prediction_error": False},
}
