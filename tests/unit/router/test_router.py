"""The router's rules 1-6, arm gating, rule-file edits, priority, task binding, revocation
and the H3 fallback (v2.2 section 8)."""

import threading
from importlib.resources import files

import pytest

from tests.fixtures.fakes import FakeLeases
from tests.fixtures.router import (
    APPROACH,
    OTHER,
    WORKER,
    RouterHarness,
    ScriptedStates,
    approach_status,
    baseline_advanced,
    claim_status,
    commit,
    event,
    human_claim,
    make_router,
    result_ready,
)
from vlah.adapters.memory.store import MemoryKeyValueStore
from vlah.arms.config import PRESETS, ArmName
from vlah.core.blackboard.api import MergedCommit
from vlah.core.indexer.api import ClaimState
from vlah.core.router.router import (
    LATENCY_KEY,
    OVERDUE_KEY,
    PENDING_KEY,
    KeyValueRouter,
    interrupt_key,
    parse_envelope,
)
from vlah.core.router.rules import parse_rules
from vlah.core.types.clock import FakeClock
from vlah.core.types.events import Event
from vlah.core.types.ids import ActorId
from vlah.core.types.params import Params
from vlah.core.types.tasks import Task

pytestmark = pytest.mark.router

TARGET = "c-worker-01-00001"
DEPENDENCY = "c-worker-02-00007"
DEFAULT_RULES_TEXT = (
    files("vlah.core.router").joinpath("rules.default.yaml").read_text(encoding="utf-8")
)
THIRD = ActorId("worker-03")


def leased(arm: ArmName, rules_text: str | None = None) -> RouterHarness:
    harness = make_router(
        PRESETS[arm], rules=parse_rules(rules_text) if rules_text else None
    )
    harness.lease(
        target_claims=(TARGET,),
        depends_on=(DEPENDENCY,),
        files_touched=("solver.py",),
    )
    return harness


def fired(harness: RouterHarness, merged_commit: MergedCommit) -> list[tuple[int, str]]:
    return [
        (interrupt.rule, interrupt.kind)
        for interrupt in harness.router.on_commit(merged_commit)
    ]


def shown_rules(harness: RouterHarness, agent: ActorId = WORKER) -> list[int]:
    """The rule of each pending hard interrupt, acknowledging each in turn."""
    rules: list[int] = []
    while (interrupt := harness.router.check(agent)) is not None:
        rules.append(interrupt.rule)
        harness.router.acknowledge(agent, interrupt.id)
    return rules


def baseline(seq: int, number: int, files_changed: tuple[str, ...]) -> Event:
    advanced = baseline_advanced(seq, files_changed)
    assert advanced.data is not None
    return advanced.model_copy(
        update={
            "data": advanced.data.model_copy(
                update={"baseline": number, "parent_baseline": number - 1}
            )
        }
    )


def reinstated(
    seq: int, target: str, actor: str = "human", role: str = "human"
) -> Event:
    return event(
        seq,
        "reinstated",
        actor=actor,
        role=role,
        target=target,
        reason="the refutation was wrong",
    )


def task(task_id: str) -> Task:
    return Task(
        id=task_id, approach=APPROACH, kind="advance", goal="g", created_by="queue"
    )


@pytest.mark.parametrize(
    ("description", "merged_commit", "expected"),
    [
        ("1: my result arrived", commit(result_ready(1, WORKER)), [(1, "hard")]),
        ("1: someone else's result", commit(result_ready(1, OTHER)), []),
        (
            "2: my target changed",
            commit(claim_status(1, TARGET, "corroborated")),
            [(2, "hard")],
        ),
        (
            "2: my target was reinstated",
            commit(reinstated(1, TARGET)),
            [(2, "hard")],
        ),
        (
            "2: another claim changed",
            commit(claim_status(1, "c-x-00001", "refuted")),
            [],
        ),
        (
            "3: my approach frozen",
            commit(approach_status(1, APPROACH, "frozen")),
            [(3, "hard")],
        ),
        (
            "3: my approach reinstated",
            commit(approach_status(1, APPROACH, "active")),
            [],
        ),
        (
            "3: another approach frozen",
            commit(approach_status(1, "a-02", "frozen")),
            [],
        ),
        (
            "3: my approach has no budget",
            commit(
                event(
                    1,
                    "allocation",
                    actor="planner",
                    role="planner",
                    target=APPROACH,
                    to="0",
                )
            ),
            [(3, "hard")],
        ),
        (
            "4: dependency refuted by the Critic",
            commit(claim_status(1, DEPENDENCY, "refuted")),
            [(4, "hard")],
        ),
        (
            "4: dependency refuted by one worker",
            commit(
                claim_status(1, DEPENDENCY, "refuted", actor="worker-03", role="worker")
            ),
            [],
        ),
        (
            "5: a human wrote about my approach",
            commit(claims=(human_claim(APPROACH),)),
            [(5, "hard")],
        ),
        (
            "5: a human wrote about another approach",
            commit(claims=(human_claim("a-02"),)),
            [],
        ),
        (
            "6: the baseline touched my files",
            commit(baseline_advanced(1, ("solver.py", "x.py"))),
            [(6, "hard")],
        ),
        (
            "6: the baseline missed my files",
            commit(baseline_advanced(1, ("other.py",))),
            [(6, "soft")],
        ),
        (
            "7: anything else",
            commit(event(1, "plateau", actor="planner", role="planner")),
            [],
        ),
    ],
)
def test_each_rule_fires_on_its_event_and_only_then(
    description: str, merged_commit: MergedCommit, expected: list[tuple[int, str]]
) -> None:
    assert fired(leased("S2"), merged_commit) == expected, description


def test_a_single_source_refutation_waits_until_a_second_source_confirms() -> None:
    """D34: one worker's refutation only goes into the diff; confirmation interrupts."""
    harness = leased("S2")
    refutation = claim_status(
        1, DEPENDENCY, "refuted", actor="worker-03", role="worker"
    )
    assert fired(harness, commit(refutation)) == []
    assert harness.router.soft_notes(WORKER) == (), "not even a soft note"
    harness.indexer.cascaded_refuted.add(DEPENDENCY)
    confirmation = claim_status(
        2, DEPENDENCY, "refuted", actor="worker-04", role="worker"
    )
    assert fired(harness, commit(confirmation)) == [(4, "hard")]


@pytest.mark.parametrize(
    ("description", "withdrawal", "is_withdrawn"),
    [
        ("reinstated by a human", reinstated(2, DEPENDENCY), True),
        (
            "reinstated by the Critic",
            reinstated(2, DEPENDENCY, actor="critic-1", role="critic"),
            True,
        ),
        (
            "a human moves it back to proposed",
            claim_status(2, DEPENDENCY, "proposed", actor="human", role="human"),
            True,
        ),
        (
            "a worker says reinstated",
            reinstated(2, DEPENDENCY, actor="worker-03", role="worker"),
            False,
        ),
        ("another claim is reinstated", reinstated(2, "c-x-00001"), False),
    ],
)
def test_a_reinstated_dependency_withdraws_the_pending_refutation_interrupt(
    description: str, withdrawal: Event, is_withdrawn: bool
) -> None:
    """D34: the agent was restarted for a refutation a human had already withdrawn."""
    timeout = Params().interrupt_ack_timeout_seconds
    harness = leased("S2")
    (refuted,) = harness.router.on_commit(
        commit(claim_status(1, DEPENDENCY, "refuted"))
    )
    assert harness.router.on_commit(commit(withdrawal)) == ()
    pending = harness.router.check(WORKER)
    assert (pending is None) is is_withdrawn, description
    assert harness.router.overdue(harness.clock.now_seconds() + timeout) == (
        () if is_withdrawn else (refuted,)
    )
    assert fired(harness, commit(claim_status(3, DEPENDENCY, "refuted"))) == [
        (4, "hard")
    ], "a later refutation interrupts again"


def test_a_reinstated_target_is_announced_as_a_reinstatement() -> None:
    """Rule 2's template says "moved to {to}", and a reinstated event has no to."""
    harness = leased("S2")
    (interrupt,) = harness.router.on_commit(
        commit(reinstated(1, TARGET, actor="critic-1", role="critic"))
    )
    assert interrupt.reason == (
        f"{TARGET}, which your task is about, was reinstated by critic-1. "
        "the refutation was wrong"
    )


def test_withdrawing_the_shown_interrupt_shows_the_next_one() -> None:
    harness = leased("S2")
    harness.router.on_commit(commit(claim_status(1, DEPENDENCY, "refuted")))
    harness.router.on_commit(commit(result_ready(2, WORKER)))
    harness.router.on_commit(commit(reinstated(3, DEPENDENCY)))
    assert shown_rules(harness) == [1]


@pytest.mark.parametrize(
    ("arm", "expected_rules"),
    [("B1", {1}), ("S1", {1, 6}), ("S2", {1, 2, 3, 4, 5, 6})],
)
def test_arm_presets_gate_the_rules(arm: ArmName, expected_rules: set[int]) -> None:
    harness = leased(arm)
    everything = commit(
        result_ready(1, WORKER),
        claim_status(2, TARGET, "refuted"),
        approach_status(3, APPROACH, "dead_end"),
        claim_status(4, DEPENDENCY, "refuted"),
        baseline_advanced(5, ("solver.py",)),
        claims=(human_claim(APPROACH),),
    )
    assert {rule for rule, _ in fired(harness, everything)} == expected_rules


def test_editing_the_rules_file_changes_behaviour_without_code() -> None:
    edited = DEFAULT_RULES_TEXT.replace(
        "  match: misses_lease_files\n  kind: soft",
        "  match: misses_lease_files\n  kind: hard",
    )
    without_rule_2 = "\n\n".join(
        block
        for block in DEFAULT_RULES_TEXT.split("\n\n")
        if "id: target-changed" not in block
    )
    assert edited != DEFAULT_RULES_TEXT
    assert fired(leased("S2", edited), commit(baseline_advanced(1, ("other.py",)))) == [
        (6, "hard")
    ]
    assert (
        fired(leased("S2", without_rule_2), commit(claim_status(1, TARGET, "refuted")))
        == []
    )


@pytest.mark.parametrize(
    ("description", "broken", "message"),
    [
        (
            "an unknown predicate",
            DEFAULT_RULES_TEXT.replace("submitted_by_agent", "always"),
            "unknown rule predicates",
        ),
        (
            "a hard rule without a priority",
            DEFAULT_RULES_TEXT.replace("  priority: 6\n", ""),
            "has no priority",
        ),
        (
            "a revoke entry naming no rule",
            DEFAULT_RULES_TEXT.replace("  revokes: dependency-refuted\n", ""),
            "revokes nothing",
        ),
        (
            "a revoke entry naming a rule not in the file",
            DEFAULT_RULES_TEXT.replace("revokes: dependency-refuted", "revokes: x"),
            "not in the file",
        ),
    ],
)
def test_a_malformed_rules_file_is_rejected(
    description: str, broken: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        parse_rules(broken)


def test_the_most_urgent_interrupt_is_shown_first_and_the_rest_follow() -> None:
    harness = leased("S2")
    harness.router.on_commit(commit(result_ready(1, WORKER)))
    harness.router.on_commit(commit(claims=(human_claim(APPROACH),)))
    harness.router.on_commit(commit(baseline_advanced(2, ("solver.py",))))
    assert harness.router.pending_count(WORKER) == 2
    assert shown_rules(harness) == [5, 6, 1], "human input comes first (P6)"
    assert harness.store.get(interrupt_key(WORKER)) is not None
    assert harness.store.sorted_range(PENDING_KEY) == ()


@pytest.mark.parametrize(
    ("description", "first", "second", "expected_order"),
    [
        (
            "a human note displaces 'approach frozen'",
            commit(approach_status(1, APPROACH, "frozen")),
            commit(claims=(human_claim(APPROACH),)),
            ["A human wrote", "Your approach a-01 is now frozen"],
        ),
        (
            "two rebases of equal priority keep their order",
            commit(baseline(1, 4, ("solver.py",))),
            commit(baseline(2, 5, ("solver.py",))),
            ["Baseline 4", "Baseline 5"],
        ),
        (
            "a target change displaces the result follow-up",
            commit(result_ready(1, WORKER)),
            commit(claim_status(2, TARGET, "corroborated")),
            ["moved to corroborated", "The result for candidate"],
        ),
    ],
)
def test_a_displaced_interrupt_is_shown_after_the_acknowledgement(
    description: str,
    first: MergedCommit,
    second: MergedCommit,
    expected_order: list[str],
) -> None:
    """A displaced interrupt was only counted: the agent kept working on a frozen
    approach, never rebased onto baseline 5, and never claimed its follow-up."""
    harness = leased("S2")
    harness.router.on_commit(first)
    harness.router.on_commit(second)
    reasons: list[str] = []
    while (interrupt := harness.router.check(WORKER)) is not None:
        reasons.append(interrupt.reason)
        harness.router.acknowledge(WORKER, interrupt.id)
    assert len(reasons) == 2, description
    assert all(
        text in reason for text, reason in zip(expected_order, reasons, strict=True)
    ), (description, reasons)


def test_each_event_interrupts_an_agent_once() -> None:
    harness = leased("S2")
    merged = commit(result_ready(1, WORKER))
    assert len(harness.router.on_commit(merged)) == 1
    assert harness.router.on_commit(merged) == (), "a replayed commit is deduplicated"


def test_acknowledging_clears_only_the_interrupt_shown() -> None:
    harness = leased("S2")
    (interrupt,) = harness.router.on_commit(commit(result_ready(1, WORKER)))
    harness.router.acknowledge(WORKER, "i-someone-else")
    assert harness.router.check(WORKER) == interrupt, "a foreign id does nothing"
    harness.clock.advance(12.0)
    harness.router.acknowledge(WORKER, interrupt.id)
    assert harness.router.check(WORKER) is None
    assert harness.router.stats().latencies_seconds == (12.0,)


class RacingStore(MemoryKeyValueStore):
    """Runs a commit between one read of the interrupt key and the swap that follows."""

    router: KeyValueRouter | None
    inject: MergedCommit | None

    def __init__(self, *, clock: FakeClock, namespace: str) -> None:
        super().__init__(clock=clock, namespace=namespace)
        self.router = None
        self.inject = None

    def get(self, key: str) -> bytes | None:
        raw = super().get(key)
        if (
            self.inject is not None
            and self.router is not None
            and key == interrupt_key(WORKER)
        ):
            injected, self.inject = self.inject, None
            self.router.on_commit(injected)
        else:
            pass
        return raw


def test_an_acknowledgement_survives_an_arrival_between_its_read_and_its_swap() -> None:
    """A commit for the same agent between the read and the swap made a valid
    acknowledgement a no-op, and the hook denied the next tool call again."""
    clock = FakeClock()
    store = RacingStore(clock=clock, namespace="router-test")
    leases = FakeLeases(clock=clock)
    router = KeyValueRouter(
        store=store,
        leases=leases,
        indexer=ScriptedStates(),
        arm=PRESETS["S2"],
        params=Params(),
        clock=clock,
    )
    leases.acquire(WORKER, task("t-1"), "handle")
    (shown,) = router.on_commit(commit(result_ready(1, WORKER)))
    store.router = router
    store.inject = commit(result_ready(2, WORKER))
    router.acknowledge(WORKER, shown.id)
    assert store.inject is None, "the arrival landed during the acknowledgement"
    after = router.check(WORKER)
    assert after is not None and after.id == "i-worker-01-result-ready-2"
    assert router.stats().latencies_seconds == (0.0,)


def test_concurrent_hooks_committers_and_the_supervisor_lose_no_acknowledgement() -> (
    None
):
    """Every interrupt a hook acknowledged was recorded or had gone overdue first,
    nothing was recorded twice, and router:pending agrees with the envelopes at rest."""
    commits_per_committer = 150
    supervisor_ticks = 40
    agents = (WORKER, OTHER, THIRD)
    harness = make_router(
        PRESETS["S2"], params=Params(interrupt_ack_timeout_seconds=5.0)
    )
    for agent in agents:
        harness.lease(agent=agent, files_touched=("solver.py",))
    raised: list[str] = []
    acked: list[str] = []
    bookkeeping = threading.Lock()
    committers_done = threading.Event()

    def committer(offset: int) -> None:
        for index in range(commits_per_committer):
            seq = offset * commits_per_committer + index + 1
            agent = agents[seq % len(agents)]
            merged = commit(
                result_ready(seq, agent)
                if seq % 2
                else baseline_advanced(seq, ("solver.py",))
            )
            hard = [i.id for i in harness.router.on_commit(merged) if i.kind == "hard"]
            with bookkeeping:
                raised.extend(hard)

    def hook(agent: ActorId) -> None:
        # The flag is read before the check: a miss after it is final.
        while True:
            is_done = committers_done.is_set()
            shown = harness.router.check(agent)
            if shown is not None:
                harness.router.acknowledge(agent, shown.id)
                with bookkeeping:
                    acked.append(shown.id)
            elif is_done:
                return
            else:
                continue

    def supervisor() -> None:
        for _ in range(supervisor_ticks):
            harness.clock.advance(1.0)
            harness.router.overdue(harness.clock.now_seconds())

    committers = [threading.Thread(target=committer, args=(i,)) for i in range(3)]
    hooks = [threading.Thread(target=hook, args=(agent,)) for agent in agents]
    watcher = threading.Thread(target=supervisor)
    for thread in (*committers, *hooks, watcher):
        thread.start()
    for thread in committers:
        thread.join()
    committers_done.set()
    for thread in (*hooks, watcher):
        thread.join()

    latency_ids = [member for member, _ in harness.store.sorted_range(LATENCY_KEY)]
    overdue_ids = {member for member, _ in harness.store.sorted_range(OVERDUE_KEY)}
    assert len(raised) == len(set(raised)) == len(latency_ids) + len(overdue_ids)
    assert len(set(latency_ids)) == len(latency_ids), "a latency recorded twice"
    assert not (set(latency_ids) & overdue_ids), "acknowledged and overdue"
    assert (
        set(acked) - set(latency_ids) <= overdue_ids
    ), "an acknowledgement was dropped"
    assert harness.store.sorted_range(PENDING_KEY) == ()
    assert all(
        parse_envelope(harness.store.get(interrupt_key(agent))) is None
        for agent in agents
    )


def test_overdue_returns_unacknowledged_interrupts_past_the_timeout_once() -> None:
    timeout = Params().interrupt_ack_timeout_seconds
    harness = leased("S2")
    harness.lease(agent=OTHER, approach="a-02")
    (mine,) = harness.router.on_commit(commit(result_ready(1, WORKER)))
    (theirs,) = harness.router.on_commit(commit(result_ready(2, OTHER)))
    harness.router.acknowledge(OTHER, theirs.id)
    harness.clock.advance(timeout - 1)
    harness.router.on_commit(commit(claims=(human_claim(APPROACH),)))
    assert harness.router.overdue(harness.clock.now_seconds()) == ()
    harness.clock.advance(1)
    (due,) = harness.router.overdue(harness.clock.now_seconds())
    assert (
        due.agent == WORKER and due.rule == 5
    ), "the newer write did not reset the clock"
    assert (
        harness.router.check(WORKER) == mine
    ), "the next one shows, the restart carries the first"
    assert harness.router.overdue(harness.clock.now_seconds() + timeout - 1) == ()
    assert harness.router.overdue(harness.clock.now_seconds() + timeout) == (mine,)
    assert harness.router.check(WORKER) is None
    assert harness.router.stats().overdue_fraction == 2 / 3


def test_an_interrupt_ends_with_the_task_it_was_written_for() -> None:
    """The next task's first hook check showed the previous task's interrupt, the
    fallback restarted the new session with it, and it counted as overdue."""
    timeout = Params().interrupt_ack_timeout_seconds
    harness = leased("S2")
    (interrupt,) = harness.router.on_commit(
        commit(baseline_advanced(1, ("solver.py",)))
    )
    harness.router.on_commit(commit(baseline_advanced(2, ("other.py",))))
    (note,) = harness.router.soft_notes(WORKER)
    harness.leases.release(WORKER)
    assert harness.router.check(WORKER) is None, "no lease: nobody to show it to"
    assert harness.router.soft_notes(WORKER) == ()
    assert harness.router.overdue(harness.clock.now_seconds() + timeout) == ()
    harness.leases.acquire(WORKER, task(f"t-{WORKER}"), "handle-restarted")
    assert harness.router.check(WORKER) == interrupt, "a restart keeps the task"
    assert harness.router.soft_notes(WORKER) == (note,)
    harness.leases.release(WORKER)
    harness.leases.acquire(WORKER, task("t-next"), "handle-next")
    assert harness.router.check(WORKER) is None
    assert harness.router.soft_notes(WORKER) == ()
    assert harness.store.sorted_range(PENDING_KEY) == (), "the stale state is cleared"
    assert harness.router.overdue(harness.clock.now_seconds() + timeout) == ()
    assert harness.router.stats().overdue_count == 0
    (fresh,) = harness.router.on_commit(commit(result_ready(3, WORKER)))
    assert harness.router.check(WORKER) == fresh


def test_overdue_discards_what_the_agent_could_not_acknowledge() -> None:
    timeout = Params().interrupt_ack_timeout_seconds
    harness = leased("S2")
    harness.router.on_commit(commit(result_ready(1, WORKER)))
    harness.leases.release(WORKER)
    harness.leases.acquire(WORKER, task("t-next"), "handle-next")
    assert harness.router.overdue(harness.clock.now_seconds() + timeout) == ()
    assert harness.router.stats().overdue_count == 0
    assert parse_envelope(harness.store.get(interrupt_key(WORKER))) is None
    assert harness.store.sorted_range(PENDING_KEY) == ()


def test_a_failing_predicate_loses_only_its_rule_and_lease(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One raising predicate lost the whole commit's routing, and with it the
    follow-up task queued after the router."""
    harness = leased("S2")
    harness.lease(agent=OTHER, approach="a-02")

    def raising(claim_id: str) -> ClaimState:
        raise KeyError(claim_id)

    monkeypatch.setattr(harness.indexer, "claim_state", raising)
    merged = commit(
        claim_status(1, DEPENDENCY, "refuted", actor="worker-03", role="worker"),
        result_ready(2, OTHER),
        claim_status(3, TARGET, "corroborated"),
    )
    assert fired(harness, merged) == [(2, "hard"), (1, "hard")]
    assert "rule dependency-refuted failed on 1 for worker-01" in caplog.text


class CountingStore(MemoryKeyValueStore):
    reads: int

    def __init__(self, *, clock: FakeClock, namespace: str) -> None:
        super().__init__(clock=clock, namespace=namespace)
        self.reads = 0

    def get(self, key: str) -> bytes | None:
        self.reads += 1
        return super().get(key)


def test_check_is_one_store_read() -> None:
    """A miss, the common case on every tool call, reads one key; a hit reads the
    envelope and then the lease through its own port."""
    clock = FakeClock()
    store = CountingStore(clock=clock, namespace="router-test")
    leases = FakeLeases(clock=clock)
    router = KeyValueRouter(
        store=store,
        leases=leases,
        indexer=ScriptedStates(),
        arm=PRESETS["S2"],
        params=Params(),
        clock=clock,
    )
    leases.acquire(WORKER, task("t-1"), "handle")
    store.reads = 0
    assert router.check(WORKER) is None
    assert store.reads == 1
    router.on_commit(commit(result_ready(1, WORKER)))
    store.reads = 0
    assert router.check(WORKER) is not None
    assert store.reads == 1
