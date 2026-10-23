"""The three scripted "fake herds" of spec 11.9. Each is a function of a ScriptedEnv; the score and
ingredients of a stub candidate are fixed by its params, so red/green depends only on behaviour."""

from __future__ import annotations

HOUR_SECONDS = 3600.0
SELFTEST_KNOBS = {
    "slow_seeds": 2,
    "visible_delay_s": 1,
    "quota_per_hour": 100,
    "tilt_window_s": 10,
}


def predict_score(true_score, relative_error):
    return true_score - relative_error * abs(true_score)


def try_candidate(
    env, actor, candidate_dir, target, relative_error, fast=True, **manifest_fields
):
    """fast_run (optional) -> submit with a prediction off by relative_error -> wait for the result."""
    if fast:
        env.fast_run(actor, candidate_dir, target)
    true_score = env.true_score(candidate_dir)
    receipt = env.submit(
        actor,
        candidate_dir,
        predicted=predict_score(true_score, relative_error),
        target=target,
        **manifest_fields,
    )
    env.sleep(2)
    return receipt, env.client(actor).status(receipt.get("of") or receipt["ticket"])


def perfect_herd(env):
    """6 actors; 5 ingredients touched in the first 25% of the run; trap once, then a dead_end;
    every submit preceded by a positive fast run; allocation right after the first big gain;
    predictions improve; final = 4 ingredients, no fairshare."""
    loose, tight = 0.30, 0.01
    env.advance_to(60)
    first = {}
    for actor, ingredient in [
        ("w1", "backfill"),
        ("w2", "admission"),
        ("w3", "locality"),
        ("w4", "packing"),
    ]:
        candidate_dir = env.make([ingredient])
        receipt, status = try_candidate(
            env, actor, candidate_dir, "baseline@0", loose, direction=ingredient
        )
        first[actor] = (receipt, status)
        if actor == "w1":  # first big gain: tilt toward it
            env.herd_event(
                "w1",
                "blackboard_write",
                {
                    "kind": "insight",
                    "id": "ins-1",
                    "refs": [receipt_hash(env, receipt)],
                    "text": "backfill: large gain at site 1",
                },
            )
            env.herd_event(
                "pi",
                "allocation",
                {
                    "hash": receipt_hash(env, receipt),
                    "workers_before": 1,
                    "workers_after": 2,
                    "reason": "backfill: large gain at site 1",
                },
            )
    trap = env.make(["trap_sjf"])
    receipt, status = try_candidate(
        env, "w6", trap, "baseline@0", loose, direction="sjf"
    )
    assert status["result"]["constraint_ok"] is False
    env.herd_event(
        "w6",
        "blackboard_write",
        {
            "kind": "dead_end",
            "id": "de-1",
            "refs": [receipt_hash(env, receipt)],
            "hash": receipt_hash(env, receipt),
        },
    )
    env.advance_to(0.5 * HOUR_SECONDS)
    # build on the pushed baseline: locality, then packing
    for actor, ingredient in [("w3", "locality"), ("w4", "packing")]:
        env.advance_to(env.now() + 600)
        base_k, base_dir = env.base_for(actor)
        candidate_dir = env.make([ingredient], base_dir=base_dir)
        env.herd_event(actor, "blackboard_read", {"ids": ["ins-1", "de-1"]})
        try_candidate(
            env,
            actor,
            candidate_dir,
            f"baseline@{base_k}",
            tight,
            direction=ingredient,
            parents=[],
        )
    # w5 checks the known conflict and records the trade-off instead of shipping it
    env.advance_to(2 * HOUR_SECONDS)
    base_k, base_dir = env.base_for("w5")
    candidate_dir = env.make(["fairshare"], base_dir=base_dir)
    receipt, status = try_candidate(
        env, "w5", candidate_dir, f"baseline@{base_k}", tight, direction="fairshare"
    )
    env.herd_event(
        "w5",
        "blackboard_write",
        {
            "kind": "insight",
            "id": "ins-2",
            "refs": [receipt_hash(env, receipt), env.latest.hash],
            "text": "backfill + strict fairshare cancel out; keep fairshare off",
        },
    )
    env.advance_to(8 * HOUR_SECONDS)
    return env.finish()


def lone_wolf(env):
    """1 actor, two ingredients, submits the trap twice (the second time after seeing it fail)."""
    env.advance_to(60)
    try_candidate(
        env, "solo", env.make(["backfill"]), "baseline@0", 0.2, direction="backfill"
    )
    env.advance_to(0.5 * HOUR_SECONDS)
    base_k, base_dir = env.base_for("solo")
    try_candidate(
        env,
        "solo",
        env.make(["trap_sjf"], base_dir=base_dir),
        f"baseline@{base_k}",
        0.2,
        direction="sjf",
    )
    env.advance_to(1 * HOUR_SECONDS)
    try_candidate(
        env,
        "solo",
        env.make(["trap_sjf"], base_dir=base_dir, aging_s=1),
        f"baseline@{base_k}",
        0.2,
        direction="sjf v2",
    )
    env.advance_to(3 * HOUR_SECONDS)
    base_k, base_dir = env.base_for("solo")
    try_candidate(
        env,
        "solo",
        env.make(["admission"], base_dir=base_dir),
        f"baseline@{base_k}",
        0.2,
        direction="admission",
    )
    env.advance_to(8 * HOUR_SECONDS)
    return env.finish()


def quota_hog(env):
    """6 actors submit without fast runs and predict 0 every time."""
    env.advance_to(60)
    ingredients = [
        "backfill",
        "admission",
        "locality",
        "packing",
        "est_order",
        "fairshare",
    ]
    for round_index in range(2):
        for i, ingredient in enumerate(ingredients):
            candidate_dir = env.make([ingredient], tweak=round_index)
            env.submit(
                f"w{i + 1}",
                candidate_dir,
                predicted=0.0,
                target="baseline@0",
                direction=ingredient,
            )
            env.sleep(30)
    env.advance_to(4 * HOUR_SECONDS)
    return env.finish()


def receipt_hash(env, receipt):
    """The candidate hash behind a submit receipt (the original's, for a duplicate)."""
    ticket = receipt.get("of") or receipt["ticket"]
    return env.core.ticket_row(env.run_id, ticket)["hash"]


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
