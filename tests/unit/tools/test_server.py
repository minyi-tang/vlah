"""The MCP tool server over real HTTP: the token names the agent, the hook answers (D60, D68)."""

import asyncio
import statistics
import time
from pathlib import Path

import httpx2
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from tests.fixtures.tools import make_tools_harness, serving
from vlah.arms.config import PRESETS
from vlah.core.types.claims import Claim, EvidenceRef, render_claim
from vlah.core.types.ids import ActorId
from vlah.core.types.interrupts import Interrupt

pytestmark = pytest.mark.tools

WORKER = ActorId("worker-01")
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


def test_the_hook_answers_within_fifty_milliseconds(tmp_path: Path) -> None:
    """The PreToolUse hook runs before every tool call, so it must be fast (tools/api.py)."""
    harness = make_tools_harness(tmp_path, PRESETS["S2"])
    token = harness.tools.register_agent(WORKER, tmp_path)
    with (
        serving(harness.tools) as server,
        httpx2.Client(headers={"Authorization": f"Bearer {token}"}) as http,
    ):
        url = server.hook_config_for(WORKER).interrupt_check_url
        http.post(url)
        durations = []
        for _ in range(HOOK_SAMPLES):
            started = time.perf_counter()
            assert http.post(url).status_code == 200
            durations.append(time.perf_counter() - started)
    assert statistics.median(durations) < HOOK_BUDGET_SECONDS, durations
