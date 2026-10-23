"""The MCP tool server over real HTTP: the token names the agent, the hook answers (D60, D68)."""

import asyncio
import threading
import time
from pathlib import Path

import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from tests.fixtures.fakes import FakeIndexer
from tests.fixtures.tools import make_tools_harness, serving
from vlah.adapters.memory.store import MemoryKeyValueStore
from vlah.arms.config import PRESETS
from vlah.core.router.router import KeyValueRouter, PendingInterrupt
from vlah.core.types.claims import Claim, EvidenceRef, render_claim
from vlah.core.types.ids import ActorId
from vlah.core.types.interrupts import Interrupt
from vlah.core.types.params import Params
from vlah.core.types.tasks import Task
from vlah.tools.herd_tools import HerdTools

pytestmark = pytest.mark.tools

WORKER = ActorId("worker-01")
OTHER = ActorId("worker-02")
TOOL_NAMES = ["eval_request", "repo_read", "repo_write", "search", "surprise"]
HOOK_BUDGET_SECONDS = 0.050
HOOK_SAMPLES = 20


def insight(author: ActorId) -> tuple[str, str]:
    claim = Claim(
        id=f"c-{author}-00001",
        type="insight",
        status="proposed",
        approach="a-01",
        author=author,
        evidence=(EvidenceRef(path="spec.md"),),
        body="Read the spec.",
    )
    return f"claims/{author}/{claim.id}.md", render_claim(claim)


async def call_tools(
    url: str, token: str
) -> tuple[list[str], dict[str, object], dict[str, object]]:
    path, text = insight(WORKER)
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                listed = await session.list_tools()
                read = await session.call_tool("repo_read", {"path": "spec.md"})
                write = await session.call_tool("repo_write", {"files": {path: text}})
                assert not read.is_error and not write.is_error
                assert read.structured_content is not None
                assert write.structured_content is not None
                return (
                    sorted(tool.name for tool in listed.tools),
                    read.structured_content,
                    write.structured_content,
                )


def test_the_bearer_token_decides_who_is_calling(tmp_path: Path) -> None:
    harness = make_tools_harness(tmp_path, PRESETS["S2"])
    harness.blackboard.write(ActorId("human"), {"spec.md": b"the spec"}, "spec")
    token = harness.tools.register_agent(WORKER, tmp_path)
    with serving(harness.tools) as server:
        endpoint = server.endpoint_for(WORKER)
        assert endpoint.bearer_token == token
        names, read, write = asyncio.run(call_tools(endpoint.url, token))
        assert names == TOOL_NAMES
        assert read["result"] == {"path": "spec.md", "content": "the spec"}
        assert write["result"] == {
            "written": ["claims/worker-01/c-worker-01-00001.md"],
            "sequence_numbers": [],
        }, "a claim alone carries no event, so it takes no merge sequence number"
        assert [event.actor for event in harness.recorder.events] == [WORKER, WORKER]
        for headers in ({}, {"Authorization": "Bearer not-a-token"}):
            response = httpx2.post(endpoint.url, headers=headers, json={})
            assert response.status_code == 401


def test_the_hook_returns_the_pending_interrupt_and_acknowledges_it(
    tmp_path: Path,
) -> None:
    harness = make_tools_harness(tmp_path, PRESETS["S2"])
    token = harness.tools.register_agent(WORKER, tmp_path)
    interrupt = Interrupt(
        id="i-1",
        agent=WORKER,
        kind="hard",
        rule=2,
        reason="your target was refuted",
        written_ts=0.0,
    )
    harness.router.pending[WORKER] = interrupt
    authorized = {"Authorization": f"Bearer {token}"}
    with serving(harness.tools) as server:
        hook = server.hook_config_for(WORKER)
        checked = httpx2.post(hook.interrupt_check_url, headers=authorized)
        assert checked.status_code == 200
        assert checked.json()["interrupt"]["id"] == "i-1"
        acknowledged = httpx2.post(
            hook.interrupt_ack_url, headers=authorized, json={"interrupt_id": "i-1"}
        )
        assert acknowledged.status_code == 200 and harness.router.acknowledged == [
            "i-1"
        ]
        assert httpx2.post(hook.interrupt_check_url, headers=authorized).json() == {
            "interrupt": None
        }
        stranger = httpx2.post(
            hook.interrupt_check_url, headers={"Authorization": "Bearer nope"}
        )
        assert stranger.status_code == 401
        missing_id = httpx2.post(hook.interrupt_ack_url, headers=authorized, json={})
        assert missing_id.status_code == 400


def test_the_hook_answers_within_fifty_milliseconds_while_another_agent_writes(
    tmp_path: Path,
) -> None:
    """The hook runs before every tool call, so it must never wait on another call."""
    harness = make_tools_harness(tmp_path, PRESETS["S2"])
    harness.blackboard.write(ActorId("human"), {"spec.md": b"spec"}, "spec")
    harness.tools.register_agent(WORKER, tmp_path)
    token = harness.tools.register_agent(OTHER, tmp_path)
    harness.recorder.gate = threading.Event()
    path, text = insight(WORKER)
    writer = threading.Thread(
        target=harness.tools.repo_write, args=(WORKER, {path: text})
    )
    durations: list[float] = []
    with (
        serving(harness.tools) as server,
        httpx2.Client(
            headers={"Authorization": f"Bearer {token}"}, timeout=1.0
        ) as http,
    ):
        url = server.hook_config_for(OTHER).interrupt_check_url
        http.post(url)
        writer.start()
        try:
            assert harness.recorder.entered.wait(10.0), "the write reached the recorder"
            for _ in range(HOOK_SAMPLES):
                started = time.perf_counter()
                assert http.post(url).status_code == 200
                durations.append(time.perf_counter() - started)
        finally:
            harness.recorder.gate.set()
            writer.join(10.0)
    assert max(durations) < HOOK_BUDGET_SECONDS, durations


def test_a_bad_ack_body_is_a_400_and_a_stranger_a_401(tmp_path: Path) -> None:
    harness = make_tools_harness(tmp_path, PRESETS["S2"])
    token = harness.tools.register_agent(WORKER, tmp_path)
    with serving(harness.tools) as server:
        url = server.hook_config_for(WORKER).interrupt_ack_url
        not_json = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        assert httpx2.post(url, headers=not_json, content=b"{oops").status_code == 400
        stranger = {"Authorization": "Bearer nope"}
        assert httpx2.post(url, headers=stranger, content=b"{oops").status_code == 401


def test_only_the_pending_interrupt_can_be_acknowledged(tmp_path: Path) -> None:
    """An ack with another id answered 200 and the hook could not tell it had missed."""
    harness = make_tools_harness(tmp_path, PRESETS["S2"])
    router = KeyValueRouter(
        store=MemoryKeyValueStore(clock=harness.clock, namespace="router"),
        leases=harness.leases,
        indexer=FakeIndexer(),
        arm=PRESETS["S2"],
        params=Params(),
        clock=harness.clock,
    )
    tools = HerdTools(
        blackboard=harness.blackboard,
        objects=harness.objects,
        indexer=FakeIndexer(),
        eval_service=harness.eval_service,
        leases=harness.leases,
        router=router,
        recorder=harness.recorder,
        arm=PRESETS["S2"],
        params=Params(),
        clock=harness.clock,
        run_id="r-tools",
    )
    token = tools.register_agent(WORKER, tmp_path)
    authorized = {"Authorization": f"Bearer {token}"}
    interrupt = Interrupt(
        id="i-worker-01-2-7",
        agent=WORKER,
        kind="hard",
        rule=2,
        reason="your target was refuted",
        written_ts=0.0,
    )
    with serving(tools) as server:
        hook = server.hook_config_for(WORKER)
        nothing_pending = httpx2.post(
            hook.interrupt_ack_url, headers=authorized, json={"interrupt_id": "i-x"}
        )
        assert nothing_pending.status_code == 409
        lease = harness.leases.acquire(
            WORKER,
            Task(
                id="t-1", approach="a-01", kind="advance", goal="g", created_by="queue"
            ),
            "handle-1",
        )
        router.write_hard(
            lease.task_id,
            PendingInterrupt(
                interrupt=interrupt, priority=2, rule_id="target-changed", target="c-x"
            ),
        )
        foreign = httpx2.post(
            hook.interrupt_ack_url,
            headers=authorized,
            json={"interrupt_id": "i-worker-02-2-7"},
        )
        assert foreign.status_code == 409
        checked = httpx2.post(hook.interrupt_check_url, headers=authorized)
        assert checked.json()["interrupt"]["id"] == interrupt.id, "still pending"
        own = httpx2.post(
            hook.interrupt_ack_url,
            headers=authorized,
            json={"interrupt_id": interrupt.id},
        )
        assert own.status_code == 200
        assert httpx2.post(hook.interrupt_check_url, headers=authorized).json() == {
            "interrupt": None
        }
