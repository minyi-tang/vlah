"""Scorecard metrics (spec 11.5 is the authoritative definition). Each metric is a function of
one run's events and nothing else. A metric returns
    {"value": ..., "target": str, "ok": True | False | None, "detail": {...}}
where ok=None means "not applicable" (e.g. herd-only rows on the control arms).

Interpretation choices that 11.5 leaves open are listed in DECISIONS.md (search "scorecard:").
"""

from __future__ import annotations

import math
import statistics

from testbed.recorder.log import sorted_events

SYSTEM_ACTORS = {"runner", "integrator", "composer", "slow_eval"}
TRAP = "trap_sjf"
CONFLICT_PAIR = ("backfill", "fairshare")

TARGETS = {
    "sched": {"breadth": 5, "assembly": 3, "capture": 0.70},
    "stub": {"breadth": 5, "assembly": 3, "capture": 0.70},
    "harness": {"breadth": 4, "assembly": 3, "capture": 0.60},
}


class RunView:
    """Indexes one run's events."""

    def __init__(self, events: list[dict]):
        self.events = sorted_events(events)
        cals = [e for e in self.events if e["type"] == "calibration"]
        if not cals:
            raise ValueError("run has no calibration event")
        self.cal = cals[0]["data"]
        self.t0 = cals[0]["ts"]
        self.arm = cals[0].get("arm") or self.cal.get("arm")
        self.problem = self.cal.get("problem", "sched")
        self.knobs = self.cal.get("knobs", {})
        self.base_score = self.cal["baseline_slow"]["score"]
        self.ref_score = self.cal["reference_slow"]["score"]
        self.of = lambda t: [e for e in self.events if e["type"] == t]
        self.results = self.of("result")
        self.submits = self.of("submit")
        finals = self.of("final")
        self.final = finals[-1] if finals else None
        end = (
            self.final["ts"]
            if self.final
            else (self.events[-1]["ts"] if self.events else self.t0)
        )
        self.T = max(end - self.t0, 1e-9)
        self.ings_by_hash: dict[str, frozenset] = {}
        self.result_by_ticket: dict[str, dict] = {}
        for r in self.results:
            self.ings_by_hash.setdefault(
                r["data"]["hash"], frozenset(r["data"].get("ingredients") or [])
            )
            self.result_by_ticket[r["data"]["ticket"]] = r

    # ------------------------------------------------------------ filters
    @staticmethod
    def is_worker(e) -> bool:
        return (e.get("actor") or "anon") not in SYSTEM_ACTORS

    def worker_results(self):
        return [
            r
            for r in self.results
            if self.is_worker(r) and r["data"].get("dataset", "dev") == "dev"
        ]

    def worker_submits(self, include_duplicates=True):
        return [
            s
            for s in self.submits
            if self.is_worker(s)
            and s["data"].get("dataset", "dev") == "dev"
            and (include_duplicates or not s["data"].get("duplicate"))
        ]

    def final_result(self):
        if not self.final:
            return None
        r = self.result_by_ticket.get(self.final["data"].get("ticket"))
        if r is None:
            for x in reversed(self.results):
                if x["data"]["hash"] == self.final["data"].get("hash"):
                    return x
        return r

    def is_herd(self):
        return self.arm == "herd"


def _m(value, target, ok, **detail):
    return {"value": value, "target": target, "ok": ok, "detail": detail}


def _failed(r, base):
    d = r["data"]
    return (not d.get("constraint_ok")) or d.get("score") is None or d["score"] < base


# ---------------------------------------------------------------- the nine metrics


def breadth(v: RunView):
    tgt = TARGETS.get(v.problem, TARGETS["sched"])["breadth"]
    cut = v.t0 + 0.25 * v.T
    seen = set()
    for r in v.worker_results():
        if r["ts"] < cut:
            seen |= set(r["data"].get("ingredients") or [])
    return _m(
        len(seen),
        f">= {tgt}",
        len(seen) >= tgt,
        ingredients=sorted(seen),
        cutoff_s=round(0.25 * v.T, 1),
    )


def assembly(v: RunView):
    tgt = TARGETS.get(v.problem, TARGETS["sched"])["assembly"]
    fr = v.final_result()
    if fr is None:
        return _m(None, f">= {tgt}", False, reason="no final")
    ings = sorted(set(fr["data"].get("ingredients") or []) - {TRAP})
    return _m(
        len(ings),
        f">= {tgt}",
        len(ings) >= tgt,
        ingredients=ings,
        final_ticket=fr["data"]["ticket"],
    )


def capture(v: RunView):
    tgt = TARGETS.get(v.problem, TARGETS["sched"])["capture"]
    fr = v.final_result()
    if fr is None or fr["data"].get("score") is None:
        return _m(None, f">= {tgt:.0%}", False, reason="no scored final")
    denom = v.ref_score - v.base_score
    val = (fr["data"]["score"] - v.base_score) / denom if denom else float("nan")
    return _m(
        round(val, 4),
        f">= {tgt:.0%}",
        val >= tgt,
        final_score=fr["data"]["score"],
        baseline=v.base_score,
        reference=v.ref_score,
    )


def dead_end_rehits(v: RunView):
    """11.5, literally: for each failed result (constraint violated, void, or below the baseline),
    count the later submits with the same ingredient set (any actor, the same one, duplicates
    included); herd adds, per dead_end write, the later submits matching its hash's ingredients.
    """
    failed = [
        (r["ts"], frozenset(r["data"].get("ingredients") or []))
        for r in v.results
        if v.is_worker(r) and _failed(r, v.base_score)
    ]
    marks = list(failed)
    for w in v.of("blackboard_write"):
        if (
            w["data"].get("kind") == "dead_end"
            and w["data"].get("hash") in v.ings_by_hash
        ):
            marks.append((w["ts"], v.ings_by_hash[w["data"]["hash"]]))
    total, empty_total, distinct = 0, 0, []
    for s in v.worker_submits():
        ings = v.ings_by_hash.get(s["data"]["hash"])
        if ings is None:
            continue
        n = sum(1 for t, m in marks if t < s["ts"] and ings == m)
        if n:
            total += n
            empty_total += n if not ings else 0
            distinct.append(
                {
                    "ticket": s["data"]["ticket"],
                    "actor": s.get("actor"),
                    "ingredients": sorted(ings),
                }
            )
    return _m(
        total,
        "0",
        total == 0,
        distinct_submits=len(distinct),
        hits=distinct[:20],
        count_excluding_empty_set=total - empty_total,
        note="an empty ingredient set (no probe fired) is one bucket; see count_excluding_empty_set",
    )


def conflict_handling(v: RunView):
    fr = v.final_result()
    if fr is None:
        return _m(None, "no, or recorded", False, reason="no final")
    ings = set(fr["data"].get("ingredients") or [])
    if not set(CONFLICT_PAIR) <= ings:
        return _m("both off", "no, or recorded", True)
    for w in v.of("blackboard_write"):
        if w["data"].get("kind") in ("insight", "compose"):
            union = set()
            for h in w["data"].get("refs") or []:
                union |= set(v.ings_by_hash.get(h, ()))
            if set(CONFLICT_PAIR) <= union:
                return _m(
                    "both on, trade-off recorded",
                    "no, or recorded",
                    True,
                    record=w["data"].get("id"),
                )
    return _m("both on, no record", "no, or recorded", False)


def quota_discipline(v: RunView):
    fast = v.of("fast_run")
    subs = v.worker_submits()
    if not subs:
        return _m(None, ">= 80%", None, reason="no submits")
    good = 0
    for s in subs:
        h = s["data"]["hash"]
        if any(
            f["ts"] <= s["ts"]
            and f["data"].get("hash") == h
            and f["data"].get("score") is not None
            and f["data"].get("baseline_score") is not None
            and f["data"]["score"] > f["data"]["baseline_score"]
            for f in fast
        ):
            good += 1
    val = good / len(subs)
    return _m(
        round(val, 4),
        ">= 80%",
        val >= 0.8,
        submits=len(subs),
        with_positive_fast_run=good,
        duplicates_included=sum(1 for s in subs if s["data"].get("duplicate")),
    )


def prediction_error(v: RunView):
    pairs = []
    for s in v.worker_submits():
        r = v.result_by_ticket.get(s["data"].get("of") or s["data"]["ticket"])
        if r is None or r["data"].get("score") in (None, 0):
            continue
        sc = r["data"]["score"]
        pairs.append(abs(s["data"]["predicted_score"] - sc) / abs(sc))
    if len(pairs) < 4:
        return _m(
            None,
            "second half <= 0.8 x first half",
            None,
            reason=f"only {len(pairs)} scored submits",
        )
    h = len(pairs) // 2
    first, second = statistics.median(pairs[:h]), statistics.median(pairs[h:])
    ok = second <= 0.8 * first if first > 0 else second == 0
    return _m(
        {"first_half": round(first, 4), "second_half": round(second, 4)},
        "second half <= 0.8 x first half",
        ok,
        n=len(pairs),
    )


def tilt(v: RunView):
    if not v.is_herd():
        return _m(
            None,
            "allocation toward it within window",
            None,
            reason="not applicable to this arm",
        )
    window = v.knobs.get("tilt_window_s", 7200)
    best = v.base_score
    trig = None
    for r in v.worker_results():
        d = r["data"]
        if d.get("score") is None or not d.get("constraint_ok"):
            continue
        ci = d.get("ci") or [d["score"], d["score"]]
        width = (ci[1] - ci[0]) if None not in ci else 0.0
        if d["score"] > best and d["score"] - best >= width:
            trig = r
            break
        best = max(best, d["score"])
    if trig is None:
        return _m(
            None,
            "allocation toward it within window",
            None,
            reason="no result beat the best by >= 1 CI width",
        )
    ings = set(trig["data"].get("ingredients") or [])
    for a in v.of("allocation"):
        if trig["ts"] <= a["ts"] <= trig["ts"] + window:
            d = a["data"]
            if (
                set(v.ings_by_hash.get(d.get("hash"), ())) & ings
                and d["workers_after"] > d["workers_before"]
            ):
                return _m(
                    "yes",
                    "allocation toward it within window",
                    True,
                    trigger=trig["data"]["ticket"],
                    after_s=round(a["ts"] - trig["ts"], 1),
                )
    return _m(
        "no",
        "allocation toward it within window",
        False,
        trigger=trig["data"]["ticket"],
        window_s=window,
    )


def generalization(v: RunView):
    fr = v.final_result()
    checks = v.of("final_check")
    if (
        fr is None
        or not checks
        or fr["data"].get("score") is None
        or checks[-1]["data"].get("score") is None
    ):
        return _m(None, "< 1", False, reason="missing final or final_check")
    fc = checks[-1]["data"]
    if (
        fc.get("dataset") == "holdout"
    ):  # problem B: seen = final (dev), unseen = holdout
        seen, unseen = fr["data"]["score"], fc["score"]
    else:  # problem A: seen = public seeds, unseen = final (dev)
        seen, unseen = fc["score"], fr["data"]["score"]
    ci = fr["data"].get("ci") or [None, None]
    width = (ci[1] - ci[0]) if None not in ci else 0.0
    diff = seen - unseen
    val = (
        diff / width
        if width > 0
        else (0.0 if diff == 0 else math.copysign(math.inf, diff))
    )
    return _m(
        round(val, 4),
        "< 1",
        val < 1,
        seen=seen,
        unseen=unseen,
        ci_width=width,
        dataset=fc.get("dataset"),
    )


METRICS = [
    ("breadth", "Breadth", breadth),
    ("assembly", "Assembly", assembly),
    ("capture", "Capture", capture),
    ("dead_end_rehits", "Dead-end re-hits", dead_end_rehits),
    ("conflict", "Conflict handling", conflict_handling),
    ("quota_discipline", "Quota discipline", quota_discipline),
    ("prediction_error", "Prediction error", prediction_error),
    ("tilt", "Tilt", tilt),
    ("generalization", "Generalization", generalization),
]

# Where to look when a row is red (spec section 5, last row of the reading table).
HINTS = {
    "breadth": "PI prompt for splitting directions",
    "assembly": "compose tasks never triggered / integrator",
    "capture": "assembly + direction coverage",
    "dead_end_rehits": "blackboard reads or dead-end broadcast",
    "conflict": "insight/compose records for interacting ingredients",
    "quota_discipline": "pre-submit fast-tier check / prediction",
    "prediction_error": "prediction-before-submit not doing its job",
    "tilt": "allocator",
    "generalization": "fast-tier overfitting",
}


def diagnostics(v: RunView) -> dict:
    wr = v.worker_results()
    scored = [
        r
        for r in wr
        if r["data"].get("score") is not None and r["data"].get("constraint_ok")
    ]
    best = max(scored, key=lambda r: r["data"]["score"], default=None)
    of = v.of
    out = {
        "arm": v.arm,
        "problem": v.problem,
        "T_s": round(v.T, 1),
        "worker_submits": len(v.worker_submits(include_duplicates=False)),
        "duplicates": sum(1 for s in v.worker_submits() if s["data"].get("duplicate")),
        "quota_rejected": len(of("quota_rejected")),
        "actors": sorted({s.get("actor") for s in v.worker_submits()}),
        "baseline_advanced": len(of("baseline_advanced")),
        "integration_failed": len(of("integration_failed")),
        "compose_tasks": len(of("compose_task")),
        "baseline_reverted": len(of("baseline_reverted")),
        "audit_checks": len(of("audit_checked")),
        "audit_flags": sum(1 for a in of("audit_checked") if a["data"].get("flagged")),
        "anomalies": len(of("anomaly")),
        "system_dev_submits": {
            a: sum(
                1
                for s in v.submits
                if s.get("actor") == a and s["data"].get("dataset", "dev") == "dev"
            )
            for a in ("runner", "integrator", "composer")
        },
        "stale_targets": len(of("stale_target")),
        "agent_restarts": len(of("agent_restart")),
    }
    if best is not None:
        denom = v.ref_score - v.base_score
        out["actor_best"] = {
            "ticket": best["data"]["ticket"],
            "actor": best.get("actor"),
            "score": best["data"]["score"],
            "capture": (
                round((best["data"]["score"] - v.base_score) / denom, 4)
                if denom
                else None
            ),
            "ingredients": best["data"].get("ingredients"),
        }
    fr = v.final_result()
    if fr is not None:
        out["final"] = {
            "ticket": fr["data"]["ticket"],
            "score": fr["data"]["score"],
            "ingredients": fr["data"].get("ingredients"),
        }
    return out


def scorecard(events: list[dict]) -> dict:
    v = RunView(events)
    rows = {}
    for key, _label, fn in METRICS:
        try:
            rows[key] = fn(v)
        except Exception as e:  # a broken metric must not hide the others
            rows[key] = _m(None, "", False, error=f"{type(e).__name__}: {e}")
    run_id = v.events[0].get("run_id") if v.events else None
    return {
        "run_id": run_id,
        "arm": v.arm,
        "problem": v.problem,
        "metrics": rows,
        "diagnostics": diagnostics(v),
    }
