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
BREADTH_WINDOW_FRACTION = (
    0.25  # 11.5: ingredients touched in the first quarter of the run
)
QUOTA_DISCIPLINE_TARGET = 0.8
PREDICTION_IMPROVEMENT_RATIO = (
    0.8  # second-half median error <= 0.8 x first-half median error
)
MIN_SCORED_SUBMITS_FOR_PREDICTION = 4
DEFAULT_TILT_WINDOW_SECONDS = 7200

TARGETS = {
    "sched": {"breadth": 5, "assembly": 3, "capture": 0.70},
    "stub": {"breadth": 5, "assembly": 3, "capture": 0.70},
    "harness": {"breadth": 4, "assembly": 3, "capture": 0.60},
}


class RunView:
    """Indexes one run's events."""

    def __init__(self, events: list[dict]):
        self.events = sorted_events(events)
        calibrations = [
            event for event in self.events if event["type"] == "calibration"
        ]
        if not calibrations:
            raise ValueError("run has no calibration event")
        self.calibration = calibrations[0]["data"]
        self.started_at = calibrations[0]["ts"]
        self.arm = calibrations[0].get("arm") or self.calibration.get("arm")
        self.problem = self.calibration.get("problem", "sched")
        self.knobs = self.calibration.get("knobs", {})
        self.baseline_score = self.calibration["baseline_slow"]["score"]
        self.reference_score = self.calibration["reference_slow"]["score"]
        self.results = self.of("result")
        self.submits = self.of("submit")
        finals = self.of("final")
        self.final = finals[-1] if finals else None
        ended_at = (
            self.final["ts"]
            if self.final
            else (self.events[-1]["ts"] if self.events else self.started_at)
        )
        self.duration_seconds = max(ended_at - self.started_at, 1e-9)
        self.ingredients_by_hash: dict[str, frozenset] = {}
        self.result_by_ticket: dict[str, dict] = {}
        for result in self.results:
            self.ingredients_by_hash.setdefault(
                result["data"]["hash"],
                frozenset(result["data"].get("ingredients") or []),
            )
            self.result_by_ticket[result["data"]["ticket"]] = result

    def of(self, event_type: str) -> list[dict]:
        return [event for event in self.events if event["type"] == event_type]

    @staticmethod
    def is_worker(event) -> bool:
        return (event.get("actor") or "anon") not in SYSTEM_ACTORS

    def worker_results(self):
        return [
            result
            for result in self.results
            if self.is_worker(result) and result["data"].get("dataset", "dev") == "dev"
        ]

    def worker_submits(self, include_duplicates=True):
        return [
            submit
            for submit in self.submits
            if self.is_worker(submit)
            and submit["data"].get("dataset", "dev") == "dev"
            and (include_duplicates or not submit["data"].get("duplicate"))
        ]

    def final_result(self):
        if not self.final:
            return None
        result = self.result_by_ticket.get(self.final["data"].get("ticket"))
        if result is None:
            for candidate_result in reversed(self.results):
                if candidate_result["data"]["hash"] == self.final["data"].get("hash"):
                    return candidate_result
        return result

    def is_herd(self):
        return self.arm == "herd"


def metric(value, target, ok, **detail):
    return {"value": value, "target": target, "ok": ok, "detail": detail}


def is_failed(result, baseline_score):
    data = result["data"]
    return (
        (not data.get("constraint_ok"))
        or data.get("score") is None
        or data["score"] < baseline_score
    )


def breadth(run: RunView):
    target = TARGETS.get(run.problem, TARGETS["sched"])["breadth"]
    cutoff_at = run.started_at + BREADTH_WINDOW_FRACTION * run.duration_seconds
    seen = set()
    for result in run.worker_results():
        if result["ts"] < cutoff_at:
            seen |= set(result["data"].get("ingredients") or [])
    return metric(
        len(seen),
        f">= {target}",
        len(seen) >= target,
        ingredients=sorted(seen),
        cutoff_s=round(BREADTH_WINDOW_FRACTION * run.duration_seconds, 1),
    )


def assembly(run: RunView):
    target = TARGETS.get(run.problem, TARGETS["sched"])["assembly"]
    final_result = run.final_result()
    if final_result is None:
        return metric(None, f">= {target}", False, reason="no final")
    ingredients = sorted(set(final_result["data"].get("ingredients") or []) - {TRAP})
    return metric(
        len(ingredients),
        f">= {target}",
        len(ingredients) >= target,
        ingredients=ingredients,
        final_ticket=final_result["data"]["ticket"],
    )


def capture(run: RunView):
    target = TARGETS.get(run.problem, TARGETS["sched"])["capture"]
    final_result = run.final_result()
    if final_result is None or final_result["data"].get("score") is None:
        return metric(None, f">= {target:.0%}", False, reason="no scored final")
    gain_span = run.reference_score - run.baseline_score
    value = (
        (final_result["data"]["score"] - run.baseline_score) / gain_span
        if gain_span
        else float("nan")
    )
    return metric(
        round(value, 4),
        f">= {target:.0%}",
        value >= target,
        final_score=final_result["data"]["score"],
        baseline=run.baseline_score,
        reference=run.reference_score,
    )


def dead_end_rehits(run: RunView):
    """11.5, literally: for each failed result (constraint violated, void, or below the baseline),
    count the later submits with the same ingredient set (any actor, the same one, duplicates
    included); herd adds, per dead_end write, the later submits matching its hash's ingredients.
    """
    failed = [
        (result["ts"], frozenset(result["data"].get("ingredients") or []))
        for result in run.results
        if run.is_worker(result) and is_failed(result, run.baseline_score)
    ]
    marks = list(failed)
    for write in run.of("blackboard_write"):
        if (
            write["data"].get("kind") == "dead_end"
            and write["data"].get("hash") in run.ingredients_by_hash
        ):
            marks.append((write["ts"], run.ingredients_by_hash[write["data"]["hash"]]))
    total, empty_total, distinct = 0, 0, []
    for submit in run.worker_submits():
        ingredients = run.ingredients_by_hash.get(submit["data"]["hash"])
        if ingredients is None:
            continue
        rehit_count = sum(
            1
            for marked_at, marked_ingredients in marks
            if marked_at < submit["ts"] and ingredients == marked_ingredients
        )
        if rehit_count:
            total += rehit_count
            empty_total += rehit_count if not ingredients else 0
            distinct.append(
                {
                    "ticket": submit["data"]["ticket"],
                    "actor": submit.get("actor"),
                    "ingredients": sorted(ingredients),
                }
            )
    return metric(
        total,
        "0",
        total == 0,
        distinct_submits=len(distinct),
        hits=distinct[:20],
        count_excluding_empty_set=total - empty_total,
        note="an empty ingredient set (no probe fired) is one bucket; see count_excluding_empty_set",
    )


def conflict_handling(run: RunView):
    final_result = run.final_result()
    if final_result is None:
        return metric(None, "no, or recorded", False, reason="no final")
    ingredients = set(final_result["data"].get("ingredients") or [])
    if not set(CONFLICT_PAIR) <= ingredients:
        return metric("both off", "no, or recorded", True)
    for write in run.of("blackboard_write"):
        if write["data"].get("kind") in ("insight", "compose"):
            union = set()
            for referenced_hash in write["data"].get("refs") or []:
                union |= set(run.ingredients_by_hash.get(referenced_hash, ()))
            if set(CONFLICT_PAIR) <= union:
                return metric(
                    "both on, trade-off recorded",
                    "no, or recorded",
                    True,
                    record=write["data"].get("id"),
                )
    return metric("both on, no record", "no, or recorded", False)


def quota_discipline(run: RunView):
    fast_runs = run.of("fast_run")
    submits = run.worker_submits()
    if not submits:
        return metric(None, ">= 80%", None, reason="no submits")
    with_positive_fast_run = 0
    for submit in submits:
        submitted_hash = submit["data"]["hash"]
        if any(
            fast_run["ts"] <= submit["ts"]
            and fast_run["data"].get("hash") == submitted_hash
            and fast_run["data"].get("score") is not None
            and fast_run["data"].get("baseline_score") is not None
            and fast_run["data"]["score"] > fast_run["data"]["baseline_score"]
            for fast_run in fast_runs
        ):
            with_positive_fast_run += 1
    value = with_positive_fast_run / len(submits)
    return metric(
        round(value, 4),
        ">= 80%",
        value >= QUOTA_DISCIPLINE_TARGET,
        submits=len(submits),
        with_positive_fast_run=with_positive_fast_run,
        duplicates_included=sum(
            1 for submit in submits if submit["data"].get("duplicate")
        ),
    )


def prediction_error(run: RunView):
    relative_errors = []
    for submit in run.worker_submits():
        result = run.result_by_ticket.get(
            submit["data"].get("of") or submit["data"]["ticket"]
        )
        if result is None or result["data"].get("score") in (None, 0):
            continue
        score = result["data"]["score"]
        relative_errors.append(
            abs(submit["data"]["predicted_score"] - score) / abs(score)
        )
    if len(relative_errors) < MIN_SCORED_SUBMITS_FOR_PREDICTION:
        return metric(
            None,
            "second half <= 0.8 x first half",
            None,
            reason=f"only {len(relative_errors)} scored submits",
        )
    half = len(relative_errors) // 2
    first, second = (
        statistics.median(relative_errors[:half]),
        statistics.median(relative_errors[half:]),
    )
    ok = second <= PREDICTION_IMPROVEMENT_RATIO * first if first > 0 else second == 0
    return metric(
        {"first_half": round(first, 4), "second_half": round(second, 4)},
        "second half <= 0.8 x first half",
        ok,
        n=len(relative_errors),
    )


def tilt(run: RunView):
    if not run.is_herd():
        return metric(
            None,
            "allocation toward it within window",
            None,
            reason="not applicable to this arm",
        )
    window_seconds = run.knobs.get("tilt_window_s", DEFAULT_TILT_WINDOW_SECONDS)
    best_score = run.baseline_score
    trigger = None
    for result in run.worker_results():
        data = result["data"]
        if data.get("score") is None or not data.get("constraint_ok"):
            continue
        ci = data.get("ci") or [data["score"], data["score"]]
        width = (ci[1] - ci[0]) if None not in ci else 0.0
        if data["score"] > best_score and data["score"] - best_score >= width:
            trigger = result
            break
        best_score = max(best_score, data["score"])
    if trigger is None:
        return metric(
            None,
            "allocation toward it within window",
            None,
            reason="no result beat the best by >= 1 CI width",
        )
    ingredients = set(trigger["data"].get("ingredients") or [])
    for allocation in run.of("allocation"):
        if trigger["ts"] <= allocation["ts"] <= trigger["ts"] + window_seconds:
            data = allocation["data"]
            if (
                set(run.ingredients_by_hash.get(data.get("hash"), ())) & ingredients
                and data["workers_after"] > data["workers_before"]
            ):
                return metric(
                    "yes",
                    "allocation toward it within window",
                    True,
                    trigger=trigger["data"]["ticket"],
                    after_s=round(allocation["ts"] - trigger["ts"], 1),
                )
    return metric(
        "no",
        "allocation toward it within window",
        False,
        trigger=trigger["data"]["ticket"],
        window_s=window_seconds,
    )


def generalization(run: RunView):
    final_result = run.final_result()
    checks = run.of("final_check")
    if (
        final_result is None
        or not checks
        or final_result["data"].get("score") is None
        or checks[-1]["data"].get("score") is None
    ):
        return metric(None, "< 1", False, reason="missing final or final_check")
    final_check_data = checks[-1]["data"]
    if (
        final_check_data.get("dataset") == "holdout"
    ):  # problem B: seen = final (dev), unseen = holdout
        seen, unseen = final_result["data"]["score"], final_check_data["score"]
    else:  # problem A: seen = public seeds, unseen = final (dev)
        seen, unseen = final_check_data["score"], final_result["data"]["score"]
    ci = final_result["data"].get("ci") or [None, None]
    width = (ci[1] - ci[0]) if None not in ci else 0.0
    difference = seen - unseen
    value = (
        difference / width
        if width > 0
        else (0.0 if difference == 0 else math.copysign(math.inf, difference))
    )
    return metric(
        round(value, 4),
        "< 1",
        value < 1,
        seen=seen,
        unseen=unseen,
        ci_width=width,
        dataset=final_check_data.get("dataset"),
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


def diagnostics(run: RunView) -> dict:
    worker_results = run.worker_results()
    scored = [
        result
        for result in worker_results
        if result["data"].get("score") is not None
        and result["data"].get("constraint_ok")
    ]
    best = max(scored, key=lambda result: result["data"]["score"], default=None)
    summary = {
        "arm": run.arm,
        "problem": run.problem,
        "T_s": round(run.duration_seconds, 1),
        "worker_submits": len(run.worker_submits(include_duplicates=False)),
        "duplicates": sum(
            1 for submit in run.worker_submits() if submit["data"].get("duplicate")
        ),
        "quota_rejected": len(run.of("quota_rejected")),
        "actors": sorted({submit.get("actor") for submit in run.worker_submits()}),
        "baseline_advanced": len(run.of("baseline_advanced")),
        "integration_failed": len(run.of("integration_failed")),
        "compose_tasks": len(run.of("compose_task")),
        "baseline_reverted": len(run.of("baseline_reverted")),
        "audit_checks": len(run.of("audit_checked")),
        "audit_flags": sum(
            1 for audit in run.of("audit_checked") if audit["data"].get("flagged")
        ),
        "anomalies": len(run.of("anomaly")),
        "system_dev_submits": {
            actor: sum(
                1
                for submit in run.submits
                if submit.get("actor") == actor
                and submit["data"].get("dataset", "dev") == "dev"
            )
            for actor in ("runner", "integrator", "composer")
        },
        "stale_targets": len(run.of("stale_target")),
        "agent_restarts": len(run.of("agent_restart")),
    }
    if best is not None:
        gain_span = run.reference_score - run.baseline_score
        summary["actor_best"] = {
            "ticket": best["data"]["ticket"],
            "actor": best.get("actor"),
            "score": best["data"]["score"],
            "capture": (
                round((best["data"]["score"] - run.baseline_score) / gain_span, 4)
                if gain_span
                else None
            ),
            "ingredients": best["data"].get("ingredients"),
        }
    final_result = run.final_result()
    if final_result is not None:
        summary["final"] = {
            "ticket": final_result["data"]["ticket"],
            "score": final_result["data"]["score"],
            "ingredients": final_result["data"].get("ingredients"),
        }
    return summary


def scorecard(events: list[dict]) -> dict:
    run = RunView(events)
    rows = {}
    for metric_key, _label, metric_function in METRICS:
        try:
            rows[metric_key] = metric_function(run)
        except Exception as error:  # a broken metric must not hide the others
            rows[metric_key] = metric(
                None, "", False, error=f"{type(error).__name__}: {error}"
            )
    run_id = run.events[0].get("run_id") if run.events else None
    return {
        "run_id": run_id,
        "arm": run.arm,
        "problem": run.problem,
        "metrics": rows,
        "diagnostics": diagnostics(run),
    }
