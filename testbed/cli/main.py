"""tb: the testbed command line (spec 11.3 / 11.8).

Worker-facing:   tb fast | submit | status | history | final
Operator-facing: tb serve | pack-problem | run | precheck | probes | scorecard | report | integrate | selftest | extras
Environment:     TB_RUN_ID, TB_ARM, TB_ACTOR, TB_SLOW_URL, TB_TARGET (default baseline@0)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from testbed.problem_api import IS_CHECKOUT, STATE_DIR


def _env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit(f"tb: environment variable {name} is required")
    return v


def current_target(candidate_dir: Path | None = None) -> str:
    """Which baseline a candidate was built on: the target in its manifest.yaml (copied along with the
    baseline it started from), else TB_TARGET, else the runner's TB_TARGET_FILE, else baseline@0.
    """
    mf = Path(candidate_dir) / "manifest.yaml" if candidate_dir is not None else None
    if mf is not None and mf.exists():
        import yaml

        target = (yaml.safe_load(mf.read_text()) or {}).get("target")
        if target:
            return str(target)
    if os.environ.get("TB_TARGET"):
        return os.environ["TB_TARGET"]
    f = os.environ.get("TB_TARGET_FILE")
    if f and Path(f).exists():
        return Path(f).read_text().strip() or "baseline@0"
    return "baseline@0"


def _problem_for_dir(d: Path):
    """Find the problem a candidate belongs to: walk up for problem.py, else TB_PROBLEM."""
    from testbed.problem_api import load_problem

    p = Path(d).resolve()
    for q in [p, *p.parents]:
        if (q / "problem.py").exists() and (q / "__init__.py").exists():
            return load_problem(q)
    name = os.environ.get("TB_PROBLEM_DIR") or os.environ.get("TB_PROBLEM")
    if not name:
        sys.exit(
            "tb: cannot find the problem; run inside the problem dir or set TB_PROBLEM_DIR"
        )
    return load_problem(name)


# ------------------------------------------------------------------------ worker commands


def cmd_fast(a):
    """Fast tier on one public seed. Inside a run (TB_SLOW_URL and TB_RUN_ID set) the slow-eval
    service evaluates and records the fast_run; elsewhere the local copy of the problem does.
    """
    import httpx

    from testbed.cli.client import ServiceError, SlowClient, zip_candidate
    from testbed.problem_api import candidate_hash

    d = Path(a.dir)
    prob = _problem_for_dir(d)
    if a.seed not in prob.public_seeds:
        sys.exit(f"tb fast: only public seeds {prob.public_seeds} are allowed")
    err = prob.validate_candidate(d)
    if err:
        sys.exit(f"tb fast: invalid candidate: {err}")
    target = a.target or current_target(d)
    if _env("TB_SLOW_URL") and _env("TB_RUN_ID"):
        # No local fallback: a worker must never read a different simulator's numbers as the
        # service's (issue #113).
        try:
            out = SlowClient.from_env().fast(zip_candidate(d), a.seed, target)
        except (ServiceError, httpx.HTTPError) as error:
            sys.exit(f"tb fast: the slow-eval service did not evaluate: {error}")
    else:
        t0 = time.time()
        res = prob.evaluate(d, "fast", a.seed)
        base = _baseline_fast(prob, a.seed)
        h = candidate_hash(d, target)
        printable = {k: v for k, v in res["extra"].items() if k not in ("waits",)}
        out = {
            "hash": h,
            "seed": a.seed,
            "score": res["score"],
            "baseline_score": base,
            "per_item": res["per_item"],
            "extra": printable,
            "elapsed_s": round(time.time() - t0, 1),
        }
        ev = {
            "type": "fast_run",
            "actor": _env("TB_ACTOR", "anon"),
            "data": {
                "hash": h,
                "seed": a.seed,
                "score": res["score"],
                "baseline_score": base,
            },
        }
        backup = Path.home() / ".tb" / "events.jsonl"
        backup.parent.mkdir(parents=True, exist_ok=True)
        with backup.open("a") as f:
            f.write(
                json.dumps(dict(ev, ts=time.time(), run_id=_env("TB_RUN_ID"))) + "\n"
            )
    if a.out:
        Path(a.out).write_text(json.dumps(out, indent=1))
    print(
        f"score {out['score']}   baseline {out['baseline_score']}   (seed {out['seed']}, "
        f"{out['elapsed_s']}s)   hash {out['hash'][:12]}"
    )
    if out["score"] is None:
        print(f"  void: {out['extra'].get('reason')}")
    for it in out["per_item"]:
        print(f"  {it['id']:12s} {it['value']}")


def _baseline_fast(prob, seed):
    """Baseline fast score for a public seed: from baseline_scores.json if packed, else computed and cached."""
    f = prob.root / "baseline_scores.json"
    if f.exists():
        return json.loads(f.read_text())["fast"].get(str(seed))
    from testbed.problem_api import content_hash

    fp = (
        content_hash(prob.root / "sites")[:12]
        if (prob.root / "sites").exists()
        else "x"
    )
    cache = (
        Path(os.environ.get("TB_CACHE", Path.home() / ".tb" / "cache"))
        / f"{prob.name}-{fp}-baseline-fast.json"
    )
    data = json.loads(cache.read_text()) if cache.exists() else {}
    if str(seed) not in data:
        data[str(seed)] = prob.evaluate(prob.baseline_dir(), "fast", seed)["score"]
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data))
    return data[str(seed)]


def cmd_submit(a):
    from testbed.cli.client import SlowClient, write_manifest, zip_candidate

    d = Path(a.dir)
    target = a.target or current_target(d)
    parents = [p for p in (a.parents or "").split(",") if p]
    write_manifest(
        d,
        actor=_env("TB_ACTOR", "anon"),
        predicted=a.predict,
        note=a.note,
        parents=parents,
        direction=a.direction,
        target=target,
        run_id=_env("TB_RUN_ID"),
        arm=_env("TB_ARM"),
    )
    r = SlowClient.from_env().submit(zip_candidate(d), dataset=a.dataset)
    print(json.dumps(r))


def cmd_status(a):
    from testbed.cli.client import SlowClient

    print(json.dumps(SlowClient.from_env().status(a.ticket), indent=1))


def cmd_wait(a):
    """Block until a ticket's result is visible (or --timeout seconds pass), then print it."""
    from testbed.cli.client import SlowClient

    c = SlowClient.from_env()
    end = time.time() + a.timeout
    while True:
        st = c.status(a.ticket)
        if "result" in st or time.time() >= end:
            print(json.dumps(st, indent=1))
            sys.exit(0 if "result" in st else 3)
        time.sleep(min(a.poll, max(1.0, end - time.time())))


def cmd_history(a):
    from testbed.cli.client import SlowClient

    print(json.dumps(SlowClient.from_env().history(), indent=1))


def cmd_final(a):
    from testbed.cli.client import SlowClient

    ref = a.ref
    body = {"ticket": ref} if ref.startswith("t-") else {"hash": ref}
    print(json.dumps(SlowClient.from_env().final(**body)))


# ------------------------------------------------------------------------ operator commands


def cmd_serve(a):
    import uvicorn

    from testbed.slow_eval.app import create_app

    app = create_app(data_dir=Path(a.data), inline=False)
    print(
        f"slow eval on http://{a.host}:{a.port}; system key in {Path(a.data) / 'system.key'} "
        "(runner and integrator only; never give it to agents)"
    )
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


def cmd_pack(a):
    from testbed.cli.pack import pack_problem

    out = pack_problem(a.problem, Path(a.out))
    print(f"packed {a.problem} -> {out}")


def cmd_precheck(a):
    from testbed.problem_api import load_problem

    prob = load_problem(a.problem)
    mod = prob.hidden_module("reference.precheck")
    rep = mod.run(prob, n_seeds=a.seeds, probes=not a.no_probes)
    mod.print_report(rep)
    if a.json:
        Path(a.json).write_text(json.dumps(rep, indent=1, default=str))
    sys.exit(0 if rep["ok"] else 1)


def cmd_probes(a):
    from testbed.problem_api import load_problem

    prob = load_problem(a.problem)
    v, det = prob.hidden_module("probes").run_probes(prob, Path(a.dir), details=True)
    print(
        json.dumps(
            {"detected": sorted(k for k, x in v.items() if x), "details": det}, indent=1
        )
    )


def cmd_scorecard(a):
    from testbed.recorder.log import read_events
    from testbed.scorecard.report import render_one

    print(render_one(read_events(Path(a.events)), fmt=a.format))


def cmd_report(a):
    from testbed.recorder.log import read_events
    from testbed.scorecard.report import render_many

    runs = [read_events(Path(p)) for p in a.events]
    print(render_many(runs, fmt=a.format))


def cmd_run(a):
    from testbed.arms.runner import main as run_main

    run_main(a)


def cmd_selftest(a):
    from testbed.arms.scripted.selftest import main as st_main

    sys.exit(st_main(a))


def cmd_extras(a):
    if not IS_CHECKOUT:
        sys.exit(
            "tb extras requires a source checkout and operator-owned hidden material; "
            "alternative policies are not included in distributions"
        )
    else:
        pass
    from testbed.extras.checks import main as ex_main

    sys.exit(ex_main(a))


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="tb",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser(
        "fast",
        help="run the fast tier on a public seed (served by the slow-eval service inside a run)",
    )
    s.add_argument("dir")
    s.add_argument("--seed", type=int, default=1)
    s.add_argument("--target", default=None)
    s.add_argument(
        "--out",
        default=None,
        help="write the full result JSON here (map/ scripts read it)",
    )
    s.set_defaults(fn=cmd_fast)

    s = sub.add_parser(
        "submit", help="submit a candidate to the slow tier (prediction required)"
    )
    s.add_argument("dir")
    s.add_argument("--predict", type=float, required=True)
    s.add_argument("--note", default="")
    s.add_argument("--parents", default="")
    s.add_argument("--direction", default=None)
    s.add_argument("--target", default=None)
    s.add_argument("--dataset", default="dev", choices=["dev", "audit", "judge"])
    s.set_defaults(fn=cmd_submit)

    s = sub.add_parser("status")
    s.add_argument("ticket")
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser(
        "wait", help="block until a ticket's result is visible, then print it"
    )
    s.add_argument("ticket")
    s.add_argument("--timeout", type=float, default=1800)
    s.add_argument("--poll", type=float, default=10)
    s.set_defaults(fn=cmd_wait)
    s = sub.add_parser("history")
    s.set_defaults(fn=cmd_history)
    s = sub.add_parser("final")
    s.add_argument("ref", help="ticket (t-...) or candidate hash")
    s.set_defaults(fn=cmd_final)

    s = sub.add_parser("serve", help="run the slow-eval service")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--data", default=str(STATE_DIR / "slow_eval"))
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser(
        "pack-problem",
        help="build the worker copy of a problem (hidden files excluded)",
    )
    s.add_argument("problem")
    s.add_argument("out")
    s.set_defaults(fn=cmd_pack)

    s = sub.add_parser("precheck", help="spec 11.6 precheck (slow)")
    s.add_argument("problem", nargs="?", default="sched")
    s.add_argument("--seeds", type=int, default=20)
    s.add_argument("--no-probes", action="store_true")
    s.add_argument("--json", default=None)
    s.set_defaults(fn=cmd_precheck)

    s = sub.add_parser("probes", help="run the ingredient detectors on a candidate")
    s.add_argument("dir")
    s.add_argument("--problem", default="sched")
    s.set_defaults(fn=cmd_probes)

    s = sub.add_parser("scorecard", help="scorecard of one run's events.jsonl")
    s.add_argument("events")
    s.add_argument("--format", default="text", choices=["text", "md", "json"])
    s.set_defaults(fn=cmd_scorecard)

    s = sub.add_parser("report", help="arms side by side")
    s.add_argument("events", nargs="+")
    s.add_argument("--format", default="text", choices=["text", "md", "json"])
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("run", help="run one arm (spec 11.8)")
    s.add_argument("--arm", required=True, choices=["single", "isolated", "herd"])
    s.add_argument("--problem", default="sched")
    s.add_argument(
        "--hours",
        type=float,
        default=4,
        help="wall-clock length of the run (default 4)",
    )
    s.add_argument(
        "--preset",
        default="short",
        choices=["short", "spec"],
        help="time knobs: short = 20-min quota windows (default); spec = the spec's 1-hour windows",
    )
    s.add_argument("--workers", type=int, default=5)
    s.add_argument("--run-id", default=None)
    s.add_argument("--slow-url", default=None)
    s.add_argument(
        "--agent-cmd",
        default=None,
        help="command template launching one LLM agent; see README",
    )
    s.add_argument(
        "--scripted",
        default=None,
        help="drive the arm with a scripted agent instead of an LLM",
    )
    s.add_argument("--overrides", default="{}", help="JSON knob overrides for /runs")
    s.add_argument("--workdir", default=None)
    s.add_argument(
        "--sandbox-allow",
        action="append",
        default=[],
        help="extra read-only path for the agent sandbox (e.g. a node install); repeatable",
    )
    s.add_argument(
        "--unsafe-no-sandbox",
        action="store_true",
        help="run the agents and the composer without a sandbox, so they can read hidden material; "
        "required on hosts without sandbox-exec (non-macOS); recorded as an unsafe_no_sandbox event",
    )
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser(
        "selftest",
        help="spec 11.9 scripted self-tests (perfect_herd, lone_wolf, quota_hog)",
    )
    s.add_argument("--problem", default="stub")
    s.add_argument("--out", default=None)
    s.set_defaults(fn=cmd_selftest)

    s = sub.add_parser("extras", help="extra quick validations (see README)")
    s.add_argument("which", nargs="*", default=[])
    s.add_argument("--seeds", type=int, default=5)
    s.set_defaults(fn=cmd_extras)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
