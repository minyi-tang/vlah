"""The testbed's integrator driver: IntegratorCore wired to the slow-eval service (single and isolated arms).

It feeds the core with every verified worker result (and composer result), measures through the
service, runs compose tasks with an optional compose agent, and records the core's decisions as
events. The herd arm does the same with its own wrapper around IntegratorCore (issue #10, 3c/3d).
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from testbed.cli.client import ServiceError, SlowClient, unzip_to
from testbed.integrator.core import (
    IntegratorConfig,
    IntegratorCore,
    Measurement,
    QuotaExhausted,
    VerifiedCandidate,
    Version,
)

IGNORE_FROM = {"runner", "integrator", "slow_eval"}


class SlowEvalMeasurer:
    """Measurer backed by the slow-eval HTTP service (re-measures, audits, bookkeeping)."""

    def __init__(self, client: SlowClient):
        self.client = client

    def submit(
        self,
        candidate_dir: Path,
        baseline: int,
        dataset: str,
        note: str,
        parents: list[str],
        exempt: bool,
        predicted: float,
    ) -> str:
        extra = {"quota": "exempt"} if exempt else {}
        try:
            r = self.client.submit_dir(
                candidate_dir,
                predicted=predicted,
                dataset=dataset,
                target=f"baseline@{baseline}",
                note=note,
                parents=parents,
                direction="integrator",
                **extra,
            )
        except ServiceError as e:
            if e.code == 429:
                raise QuotaExhausted(e.body.get("next_slot_at"))
            raise
        return r.get("of") or r["ticket"]

    def result(self, ticket: str) -> Measurement | None:
        st = self.client.status(ticket)
        if st["state"] not in ("done", "failed") or "result" not in st:
            return None
        res = st["result"]
        return Measurement(
            ticket,
            st.get("hash"),
            res.get("per_seed") or [],
            res.get("score"),
            bool(res.get("constraint_ok")) and res.get("score") is not None,
        )


class Integrator:
    def __init__(
        self,
        client: SlowClient,
        workdir: Path,
        baseline_dir: Path,
        clock: Callable[[], float],
        config: IntegratorConfig = IntegratorConfig(),
        composer: Callable[[dict], object] | None = None,
        on_advance: Callable[[Version], None] | None = None,
        log: Callable[[str], None] | None = None,
    ):
        self.c = client.as_actor("integrator")
        self.clock = clock
        self.composer = composer
        self.log = log or (lambda m: None)
        self.core = IntegratorCore(
            config,
            Path(workdir),
            Path(baseline_dir),
            self.c.calibration(),
            clock,
            SlowEvalMeasurer(self.c),
            on_advance,
        )
        self.cursor = 0
        self.seen: set = set()
        self.pending_compose: list = []

    # views used by the runner, the scripted environment and the tests
    versions = property(lambda self: self.core.versions)
    queue = property(lambda self: self.core.queue)
    task = property(lambda self: self.core.task)
    stats = property(lambda self: self.core.stats)
    frozen = property(lambda self: self.core.frozen)
    cfg = property(lambda self: self.core.cfg)
    wd = property(lambda self: self.core.wd)
    merges_since_audit = property(lambda self: self.core.merges_since_audit)

    def poll_events(self):
        ev = self.c.events(self.cursor)
        self.cursor = ev["next"]
        for e in ev["events"]:
            if e["type"] != "result" or self.core.closed:
                continue
            d = e["data"]
            actor = e.get("actor") or "anon"
            if (
                actor in IGNORE_FROM
                or d.get("dataset", "dev") != "dev"
                or d["ticket"] in self.seen
            ):
                continue
            self.seen.add(d["ticket"])
            if (
                d.get("state") != "done"
                or not d.get("constraint_ok")
                or d.get("score") is None
            ):
                continue
            files = self.core.wd / "cands" / d["hash"]
            if not files.exists():
                unzip_to(self.c.candidate(d["hash"])["zip_b64"], files)
                (files / "manifest.yaml").unlink(missing_ok=True)
            self.core.add(
                VerifiedCandidate(
                    d["ticket"],
                    d["hash"],
                    d.get("target") or "baseline@0",
                    d.get("per_seed") or [],
                    d["score"],
                    files,
                    actor,
                )
            )

    def publish(self, decisions: list[dict]):
        events = []
        for dec in decisions:
            data = {k: v for k, v in dec.items() if k != "type"}
            if dec["type"] == "compose_task":
                if self.composer is not None:
                    produced = self.composer(
                        dec
                    )  # a Path, None, or a Future resolving to either
                    if produced:
                        self.pending_compose.append((dec, produced, 0.0))
                data = {
                    k: data[k] for k in ("task_id", "candidates", "baseline", "why")
                }
            elif dec["type"] == "baseline_advanced":
                self.log(
                    f"baseline@{dec['baseline']} <- {dec['candidate'][:12]} gain {dec['paired']['mean']:.2f}"
                    f" (se {dec['paired']['se']:.2f})"
                )
            events.append({"type": dec["type"], "data": data})
        if events:
            self.c.post_events(events)

    def flush_compose(self):
        keep = []
        now = self.clock()
        for task, produced, not_before in self.pending_compose:
            if self.core.closed:
                break
            if hasattr(produced, "done"):
                if not produced.done():
                    keep.append((task, produced, not_before))
                    continue
                produced = produced.result() if produced.exception() is None else None
                if not produced:
                    continue
            if now < not_before:
                keep.append((task, produced, not_before))
                continue
            try:
                self.c.as_actor("composer").submit_dir(
                    Path(produced),
                    predicted=task["predicted"],
                    target=task["target"],
                    note=f"{task['task_id']}: {task['why']}",
                    parents=task["candidates"],
                    direction="compose",
                )
            except ServiceError as e:
                if e.code == 429 and e.body.get("next_slot_at"):
                    keep.append((task, produced, e.body["next_slot_at"]))
        self.pending_compose = keep

    def step(self):
        self.poll_events()
        self.flush_compose()
        self.publish(self.core.step())

    def due(self, now: float):
        if any(hasattr(x[1], "done") and not x[1].done() for x in self.pending_compose):
            return "wait"
        core_due = self.core.due(now)
        if self.pending_compose and not self.core.closed:
            nb = min(x[2] for x in self.pending_compose)
            pending = "now" if now >= nb else nb
            if core_due in ("now", "wait"):
                return core_due
            if core_due is None:
                return pending
            return pending if pending == "now" else min(pending, core_due)
        return core_due

    def finalize(self, wait: Callable[[], None] | None = None) -> Version:
        v, decisions = self.core.closeout(wait)
        self.publish(decisions)
        return v
