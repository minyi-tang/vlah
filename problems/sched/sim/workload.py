"""Workload generator: (site profile, seed) -> job list. Deterministic, public.

Every dataset (public / dev / audit / judge) uses this same generator with different seeds;
hidden seeds live only on the slow-eval machine.
"""

import json
import math
import random
from pathlib import Path

_DAY = 86400.0


def _mean_duration(d):
    k = d["kind"]
    if k == "pareto":
        a, xm, cap = d["alpha"], d["min_s"], d["max_s"]
        # E[min(X, cap)] for Pareto(alpha, xm)
        return xm * a / (a - 1) - (xm**a) * (cap ** (1 - a)) / (a - 1)
    if k == "lognormal":
        return d["mean_s"]
    if k == "uniform":
        return (d["min_s"] + d["max_s"]) / 2
    raise ValueError(k)


def _draw_duration(rng, d):
    k = d["kind"]
    if k == "pareto":
        u = 1.0 - rng.random()
        return min(d["max_s"], d["min_s"] / (u ** (1.0 / d["alpha"])))
    if k == "lognormal":
        s = d["sigma_log"]
        mu = math.log(d["mean_s"]) - s * s / 2
        return rng.lognormvariate(mu, s)
    if k == "uniform":
        return rng.uniform(d["min_s"], d["max_s"])
    raise ValueError(k)


def _cores_dist(profile, common):
    dist = profile.get("cores") or common["cores_dist"]
    return sorted((int(c), float(p)) for c, p in dist.items())


def _e_core_seconds(profile, common):
    dist = _cores_dist(profile, common)
    e = sum(c * p for c, p in dist) * _mean_duration(profile["duration"])
    big = (profile.get("users") or {}).get("big_user")
    if big:
        bdist = sorted(
            (int(c), float(p)) for c, p in (big.get("cores") or {1: 1.0}).items()
        )
        eb = sum(c * p for c, p in bdist) * _mean_duration(
            big.get("duration", profile["duration"])
        )
        e = big["share"] * eb + (1 - big["share"]) * e
    return e


def arrival_rate(profile, common, scale=1.0):
    """Jobs per second at the base utilization: cores * util / E[cores * dur]."""
    machines = max(1, round(profile["machines"] * scale))
    total = machines * profile["cores_per_machine"]
    return total * profile["arrival"]["utilization"] / _e_core_seconds(profile, common)


def _rate_at(profile, base, t):
    arr = profile["arrival"]
    if arr["kind"] == "peaks":
        h = (t % _DAY) / 3600.0
        for p in arr["peaks_h"]:
            if p <= h < p + arr["peak_len_h"]:
                return base * arr["peak_factor"]
    return base


def generate(profile, common, seed, scale=1.0):
    site = profile["id"]
    rng = random.Random(f"sched:{seed}:{site}")
    horizon = common["horizon_s"]
    base = arrival_rate(profile, common, scale)
    dist = _cores_dist(profile, common)
    users_cfg = profile.get("users", {"n": 20})
    n_users = users_cfg["n"]
    big_cfg = users_cfg.get("big_user")
    big = big_cfg["share"] if big_cfg else users_cfg.get("big_user_share")
    big_dist = (
        sorted(
            (int(c), float(p)) for c, p in (big_cfg.get("cores") or {1: 1.0}).items()
        )
        if big_cfg
        else None
    )
    racks = profile.get("racks", 1)
    has_affinity = "rack_affinity" in profile.get("mechanisms", {})
    sigma = common["est_noise_sigma"]
    peak = profile["arrival"]["kind"] == "peaks"
    rmax = base * (profile["arrival"].get("peak_factor", 1) if peak else 1)

    jobs = []
    t = 0.0
    while True:
        t += rng.expovariate(rmax)
        if t >= horizon:
            break
        if peak and rng.random() > _rate_at(profile, base, t) / rmax:  # thinning
            continue
        is_big = big is not None and rng.random() < big
        jd = dist
        ddist = profile["duration"]
        if is_big and big_cfg:
            jd, ddist = big_dist, big_cfg.get("duration", ddist)
        u = rng.random()
        acc = 0.0
        cores = jd[-1][0]
        for c, p in jd:
            acc += p
            if u < acc:
                cores = c
                break
        dur = _draw_duration(rng, ddist)
        user = (
            "u0"
            if is_big
            else f"u{rng.randrange(1 if big is not None else 0, n_users)}"
        )
        est = dur * rng.lognormvariate(0.0, sigma)
        if has_affinity:
            rack_pref = rng.randrange(racks)
        elif racks > 1:
            # Every site with racks gives its jobs a preferred rack, so the field alone tells a
            # policy nothing about which site penalizes off-rack runs (A20). Drawn from its own
            # stream so the job stream above stays byte-identical to the single-site version.
            rack_pref = random.Random(f"rack:{seed}:{site}:{len(jobs)}").randrange(
                racks
            )
        else:
            rack_pref = None
        jobs.append(
            {
                "id": len(jobs),
                "site": site,
                "user": user,
                "submit_t": round(t, 3),
                "cores": cores,
                "true_dur": round(dur, 3),
                "est_dur": round(est, 3),
                "rack_pref": rack_pref,
            }
        )
    return jobs


def write_trace(jobs, path):
    """Write a job list as one JSON object per line (the public traces in the worker copy)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for job in jobs:
            f.write(json.dumps(job, separators=(",", ":")) + "\n")


def load_trace(path):
    """Read a job list written by write_trace."""
    with Path(path).open() as f:
        return [json.loads(line) for line in f if line.strip()]
