"""tb extras: quick validations beyond the spec, all without an LLM.

  probes-robust   Do the ingredient detectors judge behaviour, not code? Independently written
                  policies (testbed/extras/alt_policies) must be detected as exactly what they do.
  composability   All 32 subsets of the five ingredients (+ the conflict pair): how much does the best
                  single / pair / triple capture? Pairwise interactions. If one ingredient already
                  captures most of the gain, "assembly" cannot separate the arms.
  fidelity        Is the fast tier a usable proxy for the slow tier? Rank correlation between public
                  single-seed scores and slow scores; does the trap look like a win on the fast tier?
  audit-noise     False-alarm rate of the integrator's audit rule (2.5 sigma sqrt2) on honest merges,
                  using real problem-A noise on dev vs audit seeds.
  oracle          Idealized scripted arms on the real problem, the real slow-eval rules and the real
                  integrator: how fast can single / isolated / herd reach 70% capture if every worker
                  already knows one ingredient? This is the ceiling the isolated control gets from the
                  shared integrator alone.

Results go to var/extras/<name>.md and stdout.
"""

from __future__ import annotations

import itertools
import json
import math
import shutil
import tempfile
from pathlib import Path

import yaml

from testbed.arms.scripted.env import ScriptedEnv
from testbed.problem_api import STATE_DIR, content_hash, load_problem, mean, sd

OUT = STATE_DIR / "extras"
FIVE = ["backfill", "admission", "locality", "packing", "est_order"]
ALT_DIR = Path(__file__).resolve().parent / "alt_policies"
ALT_EXPECTED = {
    "backfill_skip": {"backfill"},
    "admission_global": {"admission"},
    "locality_rack": {"locality"},
    "packing_recent": {"packing"},
    "est_order_aged": {"est_order"},
    "sjf_pure": {"est_order", "trap_sjf"},
    "est_order_aged2h": {
        "est_order",
        "trap_sjf",
    },  # spec probe window is 2 h; see fidelity for its slow-tier constraint
    "fairshare_jobs": {"fairshare"},
    "noise": set(),
}
HOUR_SECONDS = 3600.0
CAPTURE_TARGET = 0.7
AUDIT_K = 2.5  # the integrator's audit rule: flag when |dev gap - audit gap| > 2.5 sigma sqrt2
ORACLE_KNOBS = {
    "visible_delay_s": 600,
    "quota_per_hour": 2,
    "quota_total": 96,
    "tilt_window_s": 7200,
}
ORACLE_HOURS = 24


def variants_module(problem):
    return problem.hidden_module("reference.variants")


class Memo:
    """(content hash, seed, tier) -> per-seed result summary, persisted across extras."""

    def __init__(self, problem):
        self.problem = problem
        sites_dir = Path(problem.root) / "sites"
        self.sites_fingerprint = (
            content_hash(sites_dir)[:12] if sites_dir.exists() else ""
        )
        self.path = OUT / f"cache-{self.sites_fingerprint}.json"
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def scores(self, candidate_dir: Path, seeds, tier="slow"):
        hash_value = content_hash(candidate_dir)
        missing_seeds = [
            seed for seed in seeds if f"{hash_value}:{seed}:{tier}" not in self.data
        ]
        if missing_seeds:
            for seed, result in zip(
                missing_seeds,
                self.problem.evaluate_many(candidate_dir, missing_seeds, tier),
            ):
                self.data[f"{hash_value}:{seed}:{tier}"] = {
                    "score": result["score"],
                    "per_item": result["per_item"],
                    "waits_p99": {
                        item["id"]: item["value"]
                        for item in result["per_item"]
                        if item["id"].endswith("p99")
                    },
                }
            OUT.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data))
        return [self.data[f"{hash_value}:{seed}:{tier}"] for seed in seeds]


def subset_candidate(problem, ingredients, root: Path):
    name = "+".join(sorted(ingredients)) or "baseline"
    candidate_dir = root / name
    if not candidate_dir.exists():
        if not ingredients:
            shutil.copytree(problem.baseline_dir(), candidate_dir)
        else:
            variants_module(problem).make_candidate(
                sorted(ingredients), dest=candidate_dir
            )
    return candidate_dir


def alt_policy_candidate(problem, name, root: Path):
    candidate_dir = root / f"alt-{name}"
    if not candidate_dir.exists():
        candidate_dir.mkdir(parents=True)
        shutil.copy2(ALT_DIR / f"{name}.py", candidate_dir / "policy.py")
        (candidate_dir / "params.yaml").write_text("{}\n")
    return candidate_dir


def spearman(left_values, right_values):
    def ranks(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        rank_of = [0.0] * len(values)
        for position, i in enumerate(order):
            rank_of[i] = position
        return rank_of

    left_ranks, right_ranks = ranks(left_values), ranks(right_values)
    left_mean, right_mean = mean(left_ranks), mean(right_ranks)
    numerator = sum(
        (left - left_mean) * (right - right_mean)
        for left, right in zip(left_ranks, right_ranks)
    )
    denominator = math.sqrt(
        sum((left - left_mean) ** 2 for left in left_ranks)
        * sum((right - right_mean) ** 2 for right in right_ranks)
    )
    return numerator / denominator if denominator else float("nan")


def probes_robust(problem, root, seed_count, emit):
    rows, ok_all = [], True
    for name, want in ALT_EXPECTED.items():
        got = {
            ingredient
            for ingredient, fired in problem.probes(
                alt_policy_candidate(problem, name, root)
            ).items()
            if fired
        }
        ok = got == want
        ok_all &= ok
        rows.append(
            f"| {name} | {sorted(want)} | {sorted(got)} | {'ok' if ok else 'MISMATCH'} |"
        )
    emit(
        [
            "# probes-robust",
            "",
            "| policy | expected | detected | |",
            "| --- | --- | --- | --- |",
        ]
        + rows
        + ["", f"RESULT: {'PASS' if ok_all else 'FAIL'}"]
    )
    return ok_all


def composability(problem, root, seed_count, emit):
    memo = Memo(problem)
    seeds = problem.seeds("dev")[:seed_count]
    scores_by_subset = {}
    for size in range(0, 6):
        for subset in itertools.combinations(FIVE, size):
            scores_by_subset[frozenset(subset)] = [
                result["score"]
                for result in memo.scores(
                    subset_candidate(problem, subset, root), seeds
                )
            ]
    extra_subsets = {
        "backfill+fairshare": {"backfill", "fairshare"},
        "fairshare": {"fairshare"},
    }
    for ingredients in extra_subsets.values():
        scores_by_subset[frozenset(ingredients)] = [
            result["score"]
            for result in memo.scores(
                subset_candidate(problem, ingredients, root), seeds
            )
        ]
    baseline_scores = scores_by_subset[frozenset()]
    full_scores = scores_by_subset[frozenset(FIVE)]
    full_gain = mean(full_scores) - mean(baseline_scores)

    def capture(subset):
        return (mean(scores_by_subset[subset]) - mean(baseline_scores)) / full_gain

    lines = [
        "# composability",
        "",
        f"{seed_count} dev seeds; capture = (subset - baseline) / (all five - baseline)",
        "",
    ]
    best = {}
    for size in range(1, 6):
        subsets = [
            subset
            for subset in scores_by_subset
            if len(subset) == size and subset <= set(FIVE)
        ]
        best_subset = max(subsets, key=capture)
        best[size] = (sorted(best_subset), capture(best_subset))
        lines.append(
            f"best {size}: {sorted(best_subset)}  capture {capture(best_subset):.0%}"
        )
    single_gains = {
        ingredient: mean(scores_by_subset[frozenset([ingredient])])
        - mean(baseline_scores)
        for ingredient in FIVE
    }
    lines += [
        "",
        f"sum of single gains / full gain = {sum(single_gains.values()) / full_gain:.2f} (1 = additive)",
        "",
        "pairwise interaction = gain(a+b) - gain(a) - gain(b), in units of its paired s.e.",
        "",
        "| pair | interaction | z |",
        "| --- | --- | --- |",
    ]
    for left, right in itertools.combinations(FIVE, 2):
        pair_scores = scores_by_subset[frozenset([left, right])]
        interactions = [
            pair_score - left_score - right_score + baseline_score
            for pair_score, left_score, right_score, baseline_score in zip(
                pair_scores,
                scores_by_subset[frozenset([left])],
                scores_by_subset[frozenset([right])],
                baseline_scores,
            )
        ]
        z_score = (
            mean(interactions) / (sd(interactions) / math.sqrt(len(interactions)))
            if sd(interactions) > 0
            else float("inf")
        )
        lines.append(f"| {left}+{right} | {mean(interactions):+.1f} | {z_score:+.1f} |")
    backfill_score, fairshare_score, both_score = (
        mean(scores_by_subset[frozenset(ingredients)])
        for ingredients in (["backfill"], ["fairshare"], ["backfill", "fairshare"])
    )
    lines += [
        "",
        f"conflict pair: backfill {backfill_score:.1f}, fairshare {fairshare_score:.1f}, both {both_score:.1f}  "
        f"-> both worse than backfill alone: {both_score < backfill_score}",
    ]
    lines += ["", "| subset | score | capture |", "| --- | --- | --- |"]
    for subset in sorted(
        (subset for subset in scores_by_subset if subset <= set(FIVE)),
        key=capture,
        reverse=True,
    ):
        lines.append(
            f"| {'+'.join(sorted(subset)) or 'baseline'} | {mean(scores_by_subset[subset]):.1f} | {capture(subset):.0%} |"
        )
    ok = best[1][1] < CAPTURE_TARGET and both_score < backfill_score
    lines += [
        "",
        f"RESULT: {'PASS' if ok else 'WARN'}  (pass = no single ingredient reaches 70% capture and the conflict holds)",
    ]
    emit(lines)
    return ok


def fidelity(problem, root, seed_count, emit):
    memo = Memo(problem)
    dev_seeds = problem.seeds("dev")[:seed_count]
    candidates = {
        "baseline": subset_candidate(problem, [], root),
        "reference": subset_candidate(problem, FIVE, root),
    }
    for ingredient in FIVE + ["trap_sjf", "fairshare"]:
        candidates[ingredient] = subset_candidate(problem, [ingredient], root)
    for left, right in [
        ("backfill", "admission"),
        ("locality", "est_order"),
        ("backfill", "fairshare"),
    ]:
        candidates[f"{left}+{right}"] = subset_candidate(problem, [left, right], root)
    for name in ALT_EXPECTED:
        candidates[f"alt-{name}"] = alt_policy_candidate(problem, name, root)
    slow_scores, fast_scores = {}, {}
    for name, candidate_dir in candidates.items():
        slow_scores[name] = (
            mean(
                result["score"]
                for result in memo.scores(candidate_dir, dev_seeds)
                if result["score"] is not None
            )
            if all(
                result["score"] is not None
                for result in memo.scores(candidate_dir, dev_seeds)
            )
            else None
        )
        fast_scores[name] = [
            result["score"]
            for result in memo.scores(candidate_dir, problem.public_seeds, "fast")
        ]
    names = [
        name
        for name in candidates
        if slow_scores[name] is not None and None not in fast_scores[name]
    ]
    rho_mean = spearman(
        [mean(fast_scores[name]) for name in names],
        [slow_scores[name] for name in names],
    )
    rho_single = [
        spearman(
            [fast_scores[name][i] for name in names],
            [slow_scores[name] for name in names],
        )
        for i in range(len(problem.public_seeds))
    ]
    baseline_fast = fast_scores["baseline"]
    trap_wins = sum(
        1
        for trap_score, baseline_score in zip(fast_scores["trap_sjf"], baseline_fast)
        if trap_score is not None and trap_score > baseline_score
    )
    baseline_public = memo.scores(candidates["baseline"], problem.public_seeds, "fast")
    trap_public = memo.scores(candidates["trap_sjf"], problem.public_seeds, "fast")
    visible_regressions = 0
    for baseline_result, trap_result in zip(baseline_public, trap_public):
        if any(
            trap_result["waits_p99"][site] > 1.02 * baseline_result["waits_p99"][site]
            for site in baseline_result["waits_p99"]
        ):
            visible_regressions += 1
    lines = [
        "# fidelity (fast tier vs slow tier)",
        "",
        f"{len(names)} candidates; slow = mean of {seed_count} dev seeds; fast = public seeds {problem.public_seeds}",
        "",
        f"Spearman(mean of 5 public seeds, slow) = {rho_mean:.2f}",
        "Spearman(single public seed, slow)   = "
        + ", ".join(f"{rho:.2f}" for rho in rho_single),
        f"trap_sjf beats baseline on the fast tier on {trap_wins}/{len(baseline_fast)} public seeds",
        f"trap_sjf shows a p99 regression (>2%) at some site on {visible_regressions}/{len(baseline_fast)} public seeds",
        "",
        "| candidate | slow | fast seeds 1..5 |",
        "| --- | --- | --- |",
    ]
    for name in names:
        lines.append(
            f"| {name} | {slow_scores[name]:.1f} | "
            + ", ".join(f"{score:.0f}" for score in fast_scores[name])
            + " |"
        )
    lines += [
        "",
        "constraint on the slow tier (dev seeds) for the trap-like candidates:",
    ]
    baseline_aggregate = problem.aggregate(
        problem.evaluate_many(candidates["baseline"], dev_seeds, "slow")
    )
    for name in (
        "trap_sjf",
        "alt-sjf_pure",
        "alt-est_order_aged2h",
        "alt-est_order_aged",
        "est_order",
    ):
        aggregate = problem.aggregate(
            problem.evaluate_many(candidates[name], dev_seeds, "slow")
        )
        violations = problem.constraint_violations(aggregate, baseline_aggregate)
        lines.append(
            f"  {name:22s} {'ok' if not violations else 'VIOLATES ' + '; '.join(violations)}"
        )
    ok = rho_mean >= 0.8 and trap_wins >= 3
    lines += [
        "",
        f"RESULT: {'PASS' if ok else 'WARN'}  (pass = fast tier ranks well on average and the trap looks attractive on it)",
    ]
    emit(lines)
    return ok


def audit_noise(problem, root, seed_count, emit):
    memo = Memo(problem)
    dev_seeds, audit_seeds = (
        problem.seeds("dev")[:seed_count],
        problem.seeds("audit")[:seed_count],
    )

    def per_seed_scores(ingredients, seeds):
        return [
            result["score"]
            for result in memo.scores(
                subset_candidate(problem, ingredients, root), seeds
            )
        ]

    baseline_dev, reference_dev = (
        per_seed_scores([], dev_seeds),
        per_seed_scores(FIVE, dev_seeds),
    )
    baseline_audit, reference_audit = (
        per_seed_scores([], audit_seeds),
        per_seed_scores(FIVE, audit_seeds),
    )
    sigma_dev = sd(
        [
            reference - baseline
            for reference, baseline in zip(reference_dev, baseline_dev)
        ]
    ) / math.sqrt(seed_count)
    sigma_audit = sd(
        [
            reference - baseline
            for reference, baseline in zip(reference_audit, baseline_audit)
        ]
    ) / math.sqrt(seed_count)
    threshold = AUDIT_K * math.sqrt(sigma_dev**2 + sigma_audit**2)
    flagged, total, worst = 0, 0, 0.0
    for size in range(0, 4):
        for prefix in itertools.combinations(FIVE, size):
            for pair in itertools.combinations(
                [ingredient for ingredient in FIVE if ingredient not in prefix], 2
            ):  # 2-merge segments
                before, after = list(prefix), list(prefix) + list(pair)
                dev_gap = mean(per_seed_scores(after, dev_seeds)) - mean(
                    per_seed_scores(before, dev_seeds)
                )
                audit_gap = mean(per_seed_scores(after, audit_seeds)) - mean(
                    per_seed_scores(before, audit_seeds)
                )
                gap = abs(dev_gap - audit_gap)
                worst = max(worst, gap / threshold if threshold else 0)
                flagged += gap > threshold
                total += 1
    lines = [
        "# audit-noise",
        "",
        f"{seed_count} dev + {seed_count} audit seeds; sigma_dev {sigma_dev:.2f}, sigma_audit {sigma_audit:.2f}, "
        f"threshold 2.5*sqrt(sd^2+sa^2) = {threshold:.2f}",
        "",
        f"honest 2-merge segments flagged: {flagged}/{total}  (worst gap = {worst:.2f} x threshold)",
        "",
        f"RESULT: {'PASS' if flagged == 0 else 'WARN'}",
    ]
    emit(lines)
    return flagged == 0


def oracle(problem, root, seed_count, emit):
    knobs = dict(ORACLE_KNOBS, slow_seeds=seed_count)
    rows = []
    for arm, worker_count in (("single", 1), ("isolated", 5), ("herd", 5)):
        env = ScriptedEnv(
            root / f"oracle-{arm}",
            problem=problem.name,
            arm=arm,
            n=worker_count,
            run_id=f"r-oracle-{arm}",
            overrides=knobs,
        )
        calibration = env.core.calibration(env.run_id)
        baseline_score, reference_score = (
            calibration["baseline_slow"]["score"],
            calibration["reference_slow"]["score"],
        )
        hours_to_target = None
        actors = ["solo"] if arm == "single" else [f"w{i + 1}" for i in range(5)]
        remaining = list(FIVE)
        env.advance_to(60.0)
        if arm == "isolated":
            for actor, ingredient in zip(actors, remaining):
                candidate_dir = variants_module(problem).make_candidate(
                    [ingredient], dest=root / f"oracle-{arm}" / "work" / ingredient
                )
                env.submit(
                    actor,
                    candidate_dir,
                    predicted=baseline_score,
                    target="baseline@0",
                    direction=ingredient,
                )
            remaining = []
        while env.now() < ORACLE_HOURS * HOUR_SECONDS:
            if remaining:
                base_k, base_dir = env.base_for(actors[0])
                ingredient = remaining.pop(0)
                actor = actors[(len(FIVE) - len(remaining) - 1) % len(actors)]
                candidate_dir = (
                    root / f"oracle-{arm}" / "work" / f"{ingredient}@{base_k}"
                )
                params_yaml = (
                    yaml.safe_load((base_dir / "params.yaml").read_text()) or {}
                )
                if (
                    "ingredients" not in params_yaml
                ):  # baseline@0 is the production policy, not the switchable one
                    variants_module(problem).make_candidate(
                        [ingredient], dest=candidate_dir
                    )
                else:
                    shutil.copytree(base_dir, candidate_dir, dirs_exist_ok=True)
                    params_yaml["ingredients"][ingredient] = True
                    (candidate_dir / "params.yaml").write_text(
                        yaml.safe_dump(params_yaml)
                    )
                try:
                    env.submit(
                        actor,
                        candidate_dir,
                        predicted=baseline_score,
                        target=f"baseline@{base_k}",
                        direction=ingredient,
                    )
                except Exception:
                    remaining.insert(0, ingredient)
                env.advance_to(env.now() + 900)
            else:
                env.advance_to(env.now() + 1800)
            version = env.integrator.versions[-1]
            if (
                hours_to_target is None
                and version.score is not None
                and (version.score - baseline_score)
                / (reference_score - baseline_score)
                >= CAPTURE_TARGET
            ):
                hours_to_target = env.now() / HOUR_SECONDS
        version = env.integrator.versions[-1]
        capture_fraction = (
            (version.score - baseline_score) / (reference_score - baseline_score)
            if version.score is not None
            else float("nan")
        )
        rows.append(
            f"| {arm} | {worker_count} | "
            f"{hours_to_target if hours_to_target is None else round(hours_to_target, 1)} | "
            f"{capture_fraction:.0%} | "
            f"{env.integrator.stats['merged']} | {env.integrator.stats['remeasures']} |"
        )
        env.close()
    emit(
        [
            "# oracle arms (every worker already knows one ingredient; reference implementations)",
            "",
            f"slow tier with {seed_count} seeds, 10-minute results, 2 per hour; integrator re-measures 1 per hour, 16 total",
            "",
            "| arm | workers | hours to 70% capture | capture at 24 h | merges | integrator re-measures |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        + rows
        + [
            "",
            "Read: if isolated reaches the same capture as herd, the shared integrator alone delivers assembly;",
            "herd can then only win where workers fail to find or compose ingredients by themselves.",
            "",
            "RESULT: INFO",
        ]
    )
    return True


CHECKS = {
    "probes-robust": probes_robust,
    "composability": composability,
    "fidelity": fidelity,
    "audit-noise": audit_noise,
    "oracle": oracle,
}


def main(arguments) -> int:
    problem = load_problem(getattr(arguments, "problem", "sched"))
    which = arguments.which or list(CHECKS)
    root = Path(tempfile.mkdtemp(prefix="tb-extras-"))
    OUT.mkdir(parents=True, exist_ok=True)
    status = 0
    for name in which:
        report_lines = []

        def emit(lines, report_lines=report_lines):
            report_lines.extend(lines)
            print("\n".join(lines) + "\n", flush=True)

        ok = CHECKS[name](problem, root, arguments.seeds, emit)
        (OUT / f"{name}.md").write_text("\n".join(report_lines) + "\n")
        status |= 0 if ok else 1
    print(f"reports in {OUT}")
    return status
