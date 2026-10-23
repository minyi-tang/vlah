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
        extra_fields = {"quota": "exempt"} if exempt else {}
        try:
            receipt = self.client.submit_dir(
                candidate_dir,
                predicted=predicted,
                dataset=dataset,
                target=f"baseline@{baseline}",
                note=note,
                parents=parents,
                direction="integrator",
                **extra_fields,
            )
        except ServiceError as error:
            if error.status_code == 429:
                raise QuotaExhausted(error.body.get("next_slot_at"))
            raise
        return receipt.get("of") or receipt["ticket"]

    def result(self, ticket: str) -> Measurement | None:
        status = self.client.status(ticket)
        if status["state"] not in ("done", "failed") or "result" not in status:
            return None
        result_fields = status["result"]
        return Measurement(
            ticket,
            status.get("hash"),
            result_fields.get("per_seed") or [],
            result_fields.get("score"),
            bool(result_fields.get("constraint_ok"))
            and result_fields.get("score") is not None,
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
        self.client = client.as_actor("integrator")
        self.clock = clock
        self.composer = composer
        self.log = log or (lambda message: None)
        self.core = IntegratorCore(
            config,
            Path(workdir),
            Path(baseline_dir),
            self.client.calibration(),
            clock,
            SlowEvalMeasurer(self.client),
            on_advance,
        )
        self.cursor = 0
        self.seen_tickets: set = set()
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
        events_page = self.client.events(self.cursor)
        self.cursor = events_page["next"]
        for event in events_page["events"]:
            if event["type"] != "result" or self.core.closed:
                continue
            result_data = event["data"]
            actor = event.get("actor") or "anon"
            if (
                actor in IGNORE_FROM
                or result_data.get("dataset", "dev") != "dev"
                or result_data["ticket"] in self.seen_tickets
            ):
                continue
            self.seen_tickets.add(result_data["ticket"])
            if (
                result_data.get("state") != "done"
                or not result_data.get("constraint_ok")
                or result_data.get("score") is None
            ):
                continue
            payload_dir = self.core.wd / "cands" / result_data["hash"]
            if not payload_dir.exists():
                unzip_to(
                    self.client.candidate(result_data["hash"])["zip_b64"], payload_dir
                )
                (payload_dir / "manifest.yaml").unlink(missing_ok=True)
            self.core.add(
                VerifiedCandidate(
                    result_data["ticket"],
                    result_data["hash"],
                    result_data.get("target") or "baseline@0",
                    result_data.get("per_seed") or [],
                    result_data["score"],
                    payload_dir,
                    actor,
                )
            )

    def publish(self, decisions: list[dict]):
        events = []
        for decision in decisions:
            event_data = {
                field: value for field, value in decision.items() if field != "type"
            }
            if decision["type"] == "compose_task":
                if self.composer is not None:
                    produced = self.composer(
                        decision
                    )  # a Path, None, or a Future resolving to either
                    if produced:
                        self.pending_compose.append((decision, produced, 0.0))
                event_data = {
                    field: event_data[field]
                    for field in ("task_id", "candidates", "baseline", "why")
                }
            elif decision["type"] == "baseline_advanced":
                self.log(
                    f"baseline@{decision['baseline']} <- {decision['candidate'][:12]}"
                    f" gain {decision['paired']['mean']:.2f}"
                    f" (se {decision['paired']['se']:.2f})"
                )
            events.append({"type": decision["type"], "data": event_data})
        if events:
            self.client.post_events(events)

    def flush_compose(self):
        still_pending = []
        now = self.clock()
        for task, produced, not_before in self.pending_compose:
            if self.core.closed:
                break
            if hasattr(produced, "done"):
                if not produced.done():
                    still_pending.append((task, produced, not_before))
                    continue
                produced = produced.result() if produced.exception() is None else None
                if not produced:
                    continue
            if now < not_before:
                still_pending.append((task, produced, not_before))
                continue
            try:
                self.client.as_actor("composer").submit_dir(
                    Path(produced),
                    predicted=task["predicted"],
                    target=task["target"],
                    note=f"{task['task_id']}: {task['why']}",
                    parents=task["candidates"],
                    direction="compose",
                )
            except ServiceError as error:
                if error.status_code == 429 and error.body.get("next_slot_at"):
                    still_pending.append((task, produced, error.body["next_slot_at"]))
        self.pending_compose = still_pending

    def step(self):
        self.poll_events()
        self.flush_compose()
        self.publish(self.core.step())

    def due(self, now: float):
        if any(
            hasattr(produced, "done") and not produced.done()
            for _, produced, _ in self.pending_compose
        ):
            return "wait"
        core_due = self.core.due(now)
        if self.pending_compose and not self.core.closed:
            earliest_not_before = min(
                not_before for _, _, not_before in self.pending_compose
            )
            pending_due = "now" if now >= earliest_not_before else earliest_not_before
            if core_due in ("now", "wait"):
                return core_due
            if core_due is None:
                return pending_due
            return pending_due if pending_due == "now" else min(pending_due, core_due)
        return core_due

    def finalize(self, wait: Callable[[], None] | None = None) -> Version:
        version, decisions = self.core.closeout(wait)
        self.publish(decisions)
        return version
