"""tb run: one arm of the comparison (spec 11.8).

    tb run --arm single|isolated|herd --problem sched --hours 48 --workers 5 \
           --agent-cmd 'claude -p "$(cat {prompt_file})" --dangerously-skip-permissions'

Steps: POST /runs -> pack the problem -> one workspace per agent (problem/, work/, baseline/,
PROMPT.md, TOOLS.md) -> launch the agents with TB_* in the environment -> run the integrator
(push new baselines to agents only in single and herd) -> stop at the deadline -> actor finals stay
actor_final; the run-level final is the integrator's last baseline -> final_check -> scorecard.

Without --agent-cmd an arm can be driven by a scripted agent (``--scripted``) for smoke tests.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

from testbed.cli.client import SlowClient
from testbed.cli.pack import leaked, pack_problem
from testbed.integrator.core import IntegratorConfig
from testbed.integrator.driver import Integrator
from testbed.integrator.merge import copy_tree
from testbed.problem_api import REPO_ROOT, STATE_DIR, load_problem, resolve_problem_dir
from testbed.recorder.log import read_events
from testbed.scorecard.report import render_one

PUSH_ARMS = {"single", "herd"}
# Why an LLM arm runs without the agent sandbox: the operator asked for it on a macOS host that has
# sandbox-exec, the host is not macOS, or macOS lacks sandbox-exec. Recorded in the run's events.
UnsandboxedReason = Literal["flag", "not-macos", "no-sandbox-exec"]
NO_HUMAN = "无人在线，按你的方案执行，把对齐内容写进 PLAN.md。"
CONTINUE = (
    "\n\n---\n会话重启（第 {n} 次启动）。距结束还有 {left:.1f} 小时。你不记得上一次会话做过什么："
    "先读 PLAN.md 和你自己的笔记、`tb history`、`BASELINE_CHANGELOG.md`，然后接着做。"
    "随时把进展写进 PLAN.md，会话随时可能结束。\n"
)


PRESETS = {
    # the spec's 48 h numbers (§3, §8, 11.3): 1-hour quota windows
    "spec": {
        "window_s": 3600,
        "per_window": 2,
        "visible_delay_s": 600,
        "tilt_window_s": 7200,
        "integrator_total": 16,
    },
    # short test runs (default): 20-minute windows, everything else scaled with them, so official
    # evaluations stay as scarce per window of agent work as in the spec. 240 s covers two overlapping
    # 20-seed evaluations on a 10-core machine (one takes 86-123 s).
    "short": {
        "window_s": 1200,
        "per_window": 2,
        "visible_delay_s": 240,
        "tilt_window_s": 2400,
        "integrator_total": 16,
    },
}


def time_knobs(preset: str, hours: float) -> dict:
    """Slow-eval knobs for a run of `hours` under a time preset (one setting scales all clocks)."""
    p = PRESETS[preset]
    windows = max(1, math.ceil(hours * 3600 / p["window_s"]))
    return {
        "hour_s": p["window_s"],
        "quota_per_hour": p["per_window"],
        "quota_total": p["per_window"] * windows,
        "visible_delay_s": p["visible_delay_s"],
        "tilt_window_s": p["tilt_window_s"],
        "quota_integrator_per_hour": 1,
        "quota_integrator_total": min(p["integrator_total"], windows),
    }


def integrator_config(arm: str) -> IntegratorConfig:
    """Identical for every arm (11.8.1). The arm difference (push or not) lives in the runner."""
    return IntegratorConfig()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_local_service(data_dir: Path) -> str:
    import uvicorn

    from testbed.slow_eval.app import create_app

    port = _free_port()
    app = create_app(data_dir=data_dir, inline=False)
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            import httpx

            httpx.get(url + "/docs", timeout=1)
            return url
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("slow-eval service did not start")


def fill_prompt(problem_dir: Path, hours: float, knobs: dict) -> str:
    tpl = (problem_dir / "pi_prompt.md").read_text()
    text = tpl.format(
        hours=f"{hours:g}",
        window_min=f"{knobs.get('hour_s', 3600) / 60:g}",
        quota_per_window=knobs["quota_per_hour"],
        quota_total=knobs["quota_total"],
        slow_seeds=knobs["slow_seeds"],
        visible_delay_min=f"{knobs['visible_delay_s'] / 60:g}",
    )
    return text.rstrip() + "\n\n" + NO_HUMAN + "\n"


def _real(p) -> str:
    return os.path.realpath(str(p))


def agent_tmp(ws: Path) -> Path:
    """Short per-agent temp dir (Unix socket paths, used by multiprocessing, must stay under 104 bytes)."""
    import hashlib

    # /tmp resolves to /private/tmp on macOS, which the sandbox rules name; it is /tmp on Linux
    return Path(_real("/tmp")) / (
        "tb-" + hashlib.sha1(_real(ws).encode()).hexdigest()[:10]
    )


def agent_sandbox_prefix(ws: Path, extra_allow: list[Path] | None = None) -> list[str]:
    """macOS allow-list sandbox for one agent (anti-cheat rule 1 on a single machine).

    Everything under /Users, the user's TMPDIR and /private/tmp is unreadable and unwritable except:
    the agent's own workspace (read/write), the venv and the Python it links to, the testbed package
    without testbed/extras, the agent CLI binary, and any --sandbox-allow paths. The repo, var/
    (precheck, earlier runs, the slow-eval data and its system key), the spec documents, other
    agents' workspaces and other sessions' files are therefore out of reach. Callers decide whether
    the host can run it (tb run refuses otherwise, unless --unsafe-no-sandbox).
    """
    import testbed

    pkg = Path(testbed.__file__).resolve().parent
    allow_ro = [Path(sys.prefix), Path(sys.base_prefix), pkg] + list(extra_allow or [])
    sys_tmp = os.environ.get("TMPDIR") or "/tmp"
    q = lambda x: '"' + _real(x).replace('"', "") + '"'  # noqa: E731
    rules = [
        "(version 1)",
        "(allow default)",
        '(deny file-read-data file-write* (subpath "/Users"))',
        f"(deny file-read-data file-write* (subpath {q(sys_tmp)}))",
        '(deny file-read-data file-write* (subpath "/private/tmp"))',
    ]
    for a in allow_ro:
        if Path(a).exists():
            kind = "subpath" if Path(a).is_dir() else "literal"
            rules.append(f"(allow file-read-data ({kind} {q(a)}))")
    rules.append(
        f"(allow file-read-data (literal {q(pkg.parent)}))"
    )  # sys.path entry: listing only
    rules.append(
        f"(deny file-read-data (subpath {q(pkg / 'extras')}))"
    )  # alternative policies live there
    pkg_re = re.escape(_real(pkg)).replace('"', "")
    rules.append(
        f'(deny file-read-data (regex #"^{pkg_re}/.*\\.(md|toml|json|jsonl|txt)$"))'
    )  # docs, DECISIONS
    rules.append(f"(allow file-read-data file-write* (subpath {q(ws)}))")
    tmp = agent_tmp(ws)
    tmp.mkdir(parents=True, exist_ok=True)
    rules.append(f"(allow file-read-data file-write* (subpath {q(tmp)}))")
    return ["sandbox-exec", "-p", " ".join(rules)]


def agent_env(base_env: dict, ws: Path, actor: str) -> dict:
    """Per-agent HOME/TMPDIR/CLI config inside the workspace, so nothing is shared through them."""
    home = ws / ".home"
    tmp = agent_tmp(ws)
    for d in (home, tmp):
        d.mkdir(parents=True, exist_ok=True)
    return dict(
        base_env,
        TB_ACTOR=actor,
        HOME=str(home),
        TMPDIR=str(tmp),
        COPILOT_HOME=str(home / ".copilot"),
        CLAUDE_CONFIG_DIR=str(home / ".claude"),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_CACHE_HOME=str(home / ".cache"),
    )


def cli_binary_allow(cmd_template: str) -> list[Path]:
    try:
        first = shlex.split(cmd_template)[0]
    except (ValueError, IndexError):
        return []
    w = shutil.which(first)
    return [Path(w), Path(_real(w))] if w else []


class Agent:
    def __init__(self, actor, d: Path, proc, log):
        self.actor, self.dir, self.proc, self.log = actor, d, proc, log

    def alive(self):
        return self.proc.poll() is None

    def stop(self):
        if self.alive():
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=20)
            except Exception:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except Exception:
                    pass


def make_workspace(
    actor: str,
    wd: Path,
    packed: Path,
    problem_dir: Path,
    prompt: str,
    baseline_dir: Path,
) -> Path:
    d = wd / "agents" / actor
    d.mkdir(parents=True, exist_ok=True)
    copy_tree(packed, d / "problem")
    copy_tree(baseline_dir, d / "work")
    copy_tree(baseline_dir, d / "baseline")
    for sub_ in (
        "work",
        "baseline",
    ):  # travels with copies, so tb fast/submit know the true base
        (d / sub_ / "manifest.yaml").write_text("target: baseline@0\n")
    (d / "BASELINE_TARGET").write_text("baseline@0\n")
    (d / "BASELINE_CHANGELOG.md").write_text(
        "# baseline changes\n\nbaseline@0: the production policy.\n"
    )
    (d / "PROMPT.md").write_text(prompt)
    tools = problem_dir / "tools.md"
    if tools.exists():
        shutil.copy2(tools, d / "TOOLS.md")
    return d


def pusher(agent_dirs: list[Path]):
    def push(v):
        for d in agent_dirs:
            b = d / "baseline"
            changelog = d / "BASELINE_CHANGELOG.md"
            log = (
                changelog.read_text() if changelog.exists() else "# baseline changes\n"
            )
            copy_tree(Path(v.dir), b)
            (b / "manifest.yaml").write_text(f"target: baseline@{v.k}\n")
            (d / "BASELINE_TARGET").write_text(f"baseline@{v.k}\n")
            changelog.write_text(
                log + f"\nbaseline@{v.k}: {v.note}; files {v.files_changed}; "
                f"paired gain {json.dumps(v.paired)}\n"
            )

    return push


def llm_composer(
    agent_cmd: str,
    wd: Path,
    env: dict,
    extra_allow: list[Path],
    sandboxed: bool,
    timeout_s: float = 1800,
):
    """Compose task executor: a fresh agent session outside every arm's worker pool (11.8.1 step 2).
    It runs in its own sandboxed workspace (unless the run is unsandboxed) and in the background, so
    the integrator keeps going.
    """
    from concurrent.futures import ThreadPoolExecutor

    pool = ThreadPoolExecutor(max_workers=1)

    def run(task):
        d = wd / "compose" / task["task_id"]
        for name, key in (
            ("base", "base_dir"),
            ("ours", "ours_dir"),
            ("theirs", "theirs_dir"),
        ):
            copy_tree(Path(task[key]), d / name)
        copy_tree(Path(task["ours_dir"]), d / "out")
        (d / "PROMPT.md").write_text(
            "两个改动在文本上冲突了，请把它们合成一个候选。\n"
            "base/ 是两者共同的起点；ours/ 是当前基线（已包含先前合入的改动）；theirs/ 是新通过正式评测的候选。\n"
            "在 out/ 里写出同时保留两边意图的版本（out/ 现在是 ours/ 的拷贝）。只改 out/，不要提交，不要运行 tb submit。\n"
            f"冲突原因：{task['why']}\n"
        )
        cmd = agent_cmd.format(
            prompt_file=str(d / "PROMPT.md"), workdir=str(d), actor="composer"
        )
        e = agent_env(env, d, "composer")
        prefix = agent_sandbox_prefix(d, extra_allow) if sandboxed else []
        if prefix:
            e["TB_SANDBOX"] = "inherit"
        else:
            pass
        try:
            with (d / "agent.log").open("w") as log:
                subprocess.run(
                    prefix + ["/bin/sh", "-c", cmd],
                    cwd=d,
                    env=e,
                    timeout=timeout_s,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
        except subprocess.TimeoutExpired:
            return None
        from testbed.problem_api import content_hash

        return (
            d / "out" if content_hash(d / "out") != content_hash(d / "ours") else None
        )

    return lambda task: pool.submit(run, task)


HERD_CMD = (
    "uv run vlah arm run --arm S2 --problem {problem_dir} --slow-url {slow_url} --run-id {run_id} "
    "--workers {workers} --hours {hours} --integrator-config {integrator_config}"
)


def main(a):
    arm = a.arm
    if a.scripted:
        return run_scripted(a)
    if arm != "herd" and not a.agent_cmd:
        sys.exit(
            "tb run: --agent-cmd is required (or --scripted NAME). See README: 'Running an arm with an LLM'."
        )
    # Anti-cheat rule 1 (TB §2): the single and isolated arms' agents and the composer must not read
    # hidden material. The herd runtime launches and confines its own workers.
    unsandboxed_reason: UnsandboxedReason | None
    if arm == "herd":
        unsandboxed_reason = None
    elif sys.platform != "darwin":
        unsandboxed_reason = "not-macos"
    elif not shutil.which("sandbox-exec"):
        unsandboxed_reason = "no-sandbox-exec"
    elif a.unsafe_no_sandbox:
        unsandboxed_reason = "flag"
    else:
        unsandboxed_reason = None
    if unsandboxed_reason is not None and not a.unsafe_no_sandbox:
        sys.exit(
            f"tb run: no agent sandbox on this host ({unsandboxed_reason}: {sys.platform}), so the agents "
            "and the composer would run with read access to hidden material. Pass --unsafe-no-sandbox "
            "to run anyway; the run then records an unsafe_no_sandbox event."
        )
    else:
        pass
    run_id = a.run_id or f"r-{datetime.now():%Y%m%d-%H%M%S}-{arm}"
    wd = Path(a.workdir or STATE_DIR / "runs" / run_id)
    wd.mkdir(parents=True, exist_ok=True)
    url = (
        a.slow_url
        or os.environ.get("TB_SLOW_URL")
        or start_local_service(STATE_DIR / "slow_eval")
    )
    key = os.environ.get("TB_SYSTEM_KEY")
    key_file = STATE_DIR / "slow_eval" / "system.key"
    if not key and key_file.exists():
        key = key_file.read_text().strip()
    if not key:
        sys.exit(
            "tb run: no system key (set TB_SYSTEM_KEY to the contents of the service's system.key)"
        )
    overrides = dict(time_knobs(a.preset, a.hours), **json.loads(a.overrides or "{}"))
    if arm == "isolated":
        overrides["per_actor_share"] = True
    n = 1 if arm == "single" else a.workers
    runner = SlowClient(url, run_id, "runner", key=key)
    actors = ["solo"] if arm == "single" else [f"w{i + 1}" for i in range(n)]
    # one token per agent (single and isolated): an agent can act only as itself (DECISIONS I14)
    tokens = {x: secrets.token_hex(16) for x in actors} if arm != "herd" else {}
    print(
        f"[{run_id}] creating run on {url} (calibration may take a few minutes the first time)"
    )
    runner.create_run(arm, a.problem, n, overrides, actor_tokens=tokens)
    if unsandboxed_reason is not None:
        runner.post_events(
            [
                {
                    "type": "unsafe_no_sandbox",
                    "data": {"actor": "all", "reason": unsandboxed_reason},
                }
            ]
        )
        print(
            f"[{run_id}] WARNING: the agents and the composer run without a sandbox ({unsandboxed_reason})"
        )
    else:
        pass
    knobs = runner.calibration()["knobs"]
    problem_dir = resolve_problem_dir(a.problem)
    prob = load_problem(a.problem)
    packed = pack_problem(a.problem, wd / "packed")
    assert not leaked(packed), leaked(packed)
    (wd / "integrator_config.json").write_text(
        integrator_config(arm).to_json()
    )  # identical in every arm
    if arm == "herd":
        return run_herd(a, runner, wd, packed, url, key, knobs)
    prompt = fill_prompt(problem_dir, a.hours, knobs)
    dirs = [
        make_workspace(x, wd, packed, problem_dir, prompt, prob.baseline_dir())
        for x in actors
    ]
    base_env = {k: v for k, v in os.environ.items() if not k.startswith("TB_")}
    base_env.update(
        {
            "TB_RUN_ID": run_id,
            "TB_ARM": arm,
            "TB_SLOW_URL": url,
            "PATH": str(Path(sys.executable).parent)
            + os.pathsep
            + base_env.get("PATH", ""),
        }
    )
    for k in (
        "HOME",
        "TMPDIR",
        "COPILOT_HOME",
        "CLAUDE_CONFIG_DIR",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
    ):
        base_env.pop(k, None)
    extra_allow = [Path(x) for x in (a.sandbox_allow or [])]
    cmd_tpl = a.agent_cmd
    extra_allow += cli_binary_allow(cmd_tpl)
    integ = Integrator(
        runner,
        wd / "integrator",
        prob.baseline_dir(),
        time.time,
        integrator_config(arm),
        composer=llm_composer(
            a.agent_cmd,
            wd,
            base_env,
            extra_allow,
            sandboxed=unsandboxed_reason is None,
        ),
        on_advance=pusher(dirs) if arm in PUSH_ARMS else None,
        log=lambda m: print(f"[integrator] {m}"),
    )
    deadline = time.time() + a.hours * 3600
    agents: dict[str, Agent] = {}
    starts: dict[str, list[float]] = {x: [] for x in actors}

    def launch(actor, d):
        n = len(starts[actor]) + 1
        prompt_file = d / "PROMPT.md"
        if (
            n > 1
        ):  # identical continuation for every arm (D77-style restart, no checkpoint)
            prompt_file = d / "CONTINUE.md"
            prompt_file.write_text(
                (d / "PROMPT.md").read_text()
                + CONTINUE.format(n=n, left=(deadline - time.time()) / 3600)
            )
            runner.post_events(
                [{"type": "agent_restart", "data": {"actor": actor, "start": n}}]
            )
        env = agent_env(base_env, d, actor)
        env.update(
            TB_PROBLEM_DIR=str(d / "problem"),
            TB_TARGET_FILE=str(d / "BASELINE_TARGET"),
            TB_TOKEN=tokens[actor],
        )
        prefix = (
            agent_sandbox_prefix(d, extra_allow) if unsandboxed_reason is None else []
        )
        if (
            prefix
        ):  # macOS cannot nest sandbox-exec: policies inherit the agent's sandbox
            env["TB_SANDBOX"] = "inherit"
        else:
            pass
        cmd = cmd_tpl.format(prompt_file=str(prompt_file), workdir=str(d), actor=actor)
        log = (d / "agent.log").open("a")
        log.write(f"\n===== start {n} at {datetime.now():%H:%M:%S} =====\n")
        log.flush()
        proc = subprocess.Popen(
            prefix + ["/bin/sh", "-c", cmd],
            cwd=d,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        agents[actor] = Agent(actor, d, proc, log)
        starts[actor].append(time.time())
        print(f"[{run_id}] {actor}: start {n} (pid {proc.pid})")

    for actor, d in zip(actors, dirs):
        launch(actor, d)
    try:
        while time.time() < deadline:
            integ.step()
            for actor, d in zip(actors, dirs):
                ag = agents[actor]
                if not ag.alive() and deadline - time.time() > 60:
                    recent = [t for t in starts[actor] if time.time() - t < 600]
                    if len(recent) >= 3:  # crash loop: at most 3 starts per 10 minutes
                        continue
                    launch(actor, d)
            time.sleep(integ.cfg.poll_s)
    except KeyboardInterrupt:
        print("interrupted; closing the run")
    for x in agents.values():
        x.stop()
    finish(runner, integ, wd, knobs)


def run_herd(
    a, runner: SlowClient, wd: Path, packed: Path, url: str, key: str, knobs: dict
):
    """Herd arm (issue #10, 3c): start the herd's own runtime. It wraps testbed.integrator in-process with the
    config in integrator_config.json, so the testbed starts no integrator of its own here.
    """
    template = os.environ.get("TB_HERD_CMD") or HERD_CMD
    cmd = template.format(
        problem_dir=shlex.quote(str(packed)),
        slow_url=url,
        run_id=runner.run_id,
        workers=a.workers,
        hours=a.hours,
        integrator_config=shlex.quote(str(wd / "integrator_config.json")),
    )
    # The herd runtime is trusted infrastructure: it gets the system key (it writes run-level events and
    # the final as the runner). Keeping it from its own workers is the herd's job.
    env = dict(
        os.environ,
        TB_RUN_ID=runner.run_id,
        TB_ARM="herd",
        TB_SLOW_URL=url,
        TB_SYSTEM_KEY=key,
        TB_PROBLEM_DIR=str(packed),
    )
    print(f"[{runner.run_id}] herd: {cmd}")
    with (wd / "herd.log").open("w") as log:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", cmd],
            cwd=REPO_ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            proc.wait(timeout=a.hours * 3600 + 1800)  # its own closeout gets 30 minutes
        except subprocess.TimeoutExpired:
            Agent("herd", wd, proc, log).stop()
        except KeyboardInterrupt:
            Agent("herd", wd, proc, log).stop()
    if proc.returncode not in (0, None):
        print(
            f"[{runner.run_id}] herd exited with {proc.returncode}; see {wd / 'herd.log'}",
            file=sys.stderr,
        )
    events = runner.events(0)["events"]
    if not any(
        e["type"] == "final" for e in events
    ):  # the herd did not call tb final: use its last baseline
        last = next(
            (
                e["data"].get("ticket")
                for e in reversed(events)
                if e["type"] in ("integrator_frozen", "baseline_advanced")
                and e["data"].get("ticket")
            ),
            None,
        )
        if last is None:
            base = Path(load_problem(a.problem).baseline_dir())
            last = runner.submit_dir(
                base,
                predicted=0.0,
                target="baseline@0",
                note="herd made no final: baseline@0",
                direction="runner",
            ).get("ticket")
        runner.final(ticket=last)
    final = [e for e in runner.events(0)["events"] if e["type"] == "final"][-1]
    runner.final_check(final["data"]["ticket"])
    write_scorecard(runner, wd)


def write_scorecard(runner: SlowClient, wd: Path):
    events = runner.events(0)["events"]
    out = wd / "events.jsonl"
    out.write_text("".join(json.dumps(e) + "\n" for e in events))
    card = render_one(events)
    (wd / "scorecard.txt").write_text(card + "\n")
    print(card)
    print(f"\nevents: {out}\nscorecard: {wd / 'scorecard.txt'}")


def finish(runner: SlowClient, integ: Integrator, wd: Path, knobs: dict):
    delay = min(5.0, float(knobs.get("visible_delay_s", 600)))
    v = integ.finalize(wait=lambda: time.sleep(delay))
    runner.final(ticket=v.ticket)
    runner.final_check(v.ticket)
    write_scorecard(runner, wd)


def run_scripted(a):
    """Scripted arm (no LLM, manual clock) -- the 11.9 fake herds, or your own scenario module."""
    from testbed.arms.scripted.env import ScriptedEnv
    from testbed.arms.scripted.scenarios import SCENARIOS, SELFTEST_KNOBS

    fn, _arm, n = SCENARIOS[a.scripted]
    run_id = a.run_id or f"r-{datetime.now():%Y%m%d-%H%M%S}-{a.arm}-{a.scripted}"
    wd = Path(a.workdir or STATE_DIR / "runs" / run_id)
    ov = dict(SELFTEST_KNOBS, **json.loads(a.overrides or "{}"))
    env = ScriptedEnv(
        wd,
        problem=a.problem,
        arm=a.arm,
        n=n if a.arm != "single" else 1,
        run_id=run_id,
        overrides=ov,
    )
    try:
        fn(env)
    finally:
        env.close()
    shutil.copy(env.events_path(), wd / "events.jsonl")
    card = render_one(read_events(wd / "events.jsonl"))
    (wd / "scorecard.txt").write_text(card + "\n")
    print(card)
    print(f"\nevents: {wd / 'events.jsonl'}")
