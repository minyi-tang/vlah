"""The smoke loop (v2.2 Appendix C, day one, item 1): it must stay green on every PR.

Two scripted workers each submit a candidate, the slow judge reports after its
latency, and each submitter is handed the follow-up for its own result (D28).
"""

from pathlib import Path

import pytest

from tests.fixtures.herd import TOY_SOURCE, make_toy_herd
from vlah.arms.config import PRESETS
from vlah.core.types.events import ResultReady, event_from_json

pytestmark = pytest.mark.smoke

STEP_SECONDS = 300.0


def test_results_return_to_their_submitters(tmp_path: Path) -> None:
    toy = make_toy_herd(tmp_path, PRESETS["S2"])
    for _ in range(4):
        toy.herd.step(STEP_SECONDS)

    submitted_actors = sorted(job.actor for job in toy.source.jobs.values())
    assert submitted_actors == ["worker-01", "worker-02"]

    blackboard = toy.herd.components.blackboard
    assert blackboard.head_sequence() == 2
    result_events = [
        event_from_json(blackboard.read(pointer_path, None).decode("utf-8"))
        for pointer_path in [
            blackboard.read(f"events/_seq/{sequence}", None).decode("utf-8")
            for sequence in (1, 2)
        ]
    ]
    for event in result_events:
        assert event.type == "result_ready"
        assert isinstance(event.data, ResultReady)
        assert event.data.level == TOY_SOURCE.id
        assert event.data.paired.n_items == 5

    follow_ups = {seen.agent: seen.task for seen in toy.worker.follow_ups}
    assert set(follow_ups) == {"worker-01", "worker-02"}
    for agent, task in follow_ups.items():
        assert task.for_agent == agent
        assert task.priority == 0
        assert task.refs[0] == toy.worker.submitted_claim_ids[agent]
    assert not toy.herd.running, "every worker has finished its follow-up"
