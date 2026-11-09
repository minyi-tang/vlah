"""The prompt assembly every role shares, its budgets, and capabilities as bundles
(docs/role-prompts.md)."""

import json
from dataclasses import replace

import pytest
from pydantic import JsonValue

from tests.fixtures.worker import TASK, WORKER
from vlah.adapters.harness.claude_code import harness as claude_code
from vlah.adapters.harness.copilot import harness as copilot
from vlah.core.problem.api import AdapterChoice, HerdConfig
from vlah.core.types.params import Params
from vlah.ports.executor import StartPack
from vlah.ports.harness import HookConfig, ToolEndpoint, Workspace
from vlah.prompts.assemble import (
    BOUNDARIES,
    BOUNDARIES_WORDS,
    CARD_WORDS,
    MISSION_WORDS,
    SUGGESTIONS_WORDS,
    WORKSPACE_WORDS,
    assemble,
    role_text,
)
from vlah.prompts.capabilities import CAPABILITIES, HERD, SHELL, Capability, McpServer
from vlah.prompts.profile import WORKER as WORKER_PROFILE
from vlah.prompts.profile import with_capabilities
from vlah.runtime.local_assembly import worker_profile

pytestmark = pytest.mark.harness

ENDPOINT = ToolEndpoint(url="http://127.0.0.1:8100/mcp", bearer_token="agent-token")
HOOKS = HookConfig(
    interrupt_check_url="http://127.0.0.1:8100/hook/check",
    interrupt_ack_url="http://127.0.0.1:8100/hook/ack",
)
WORKSPACE = Workspace(
    target_repository="/repos/target",
    target_ref="main",
    blackboard_clone_url="file:///runs/r-1/blackboard.git",
)
START_PACK = StartPack(files={"CONTEXT.md": "## Your task\n\nGo faster."})
WEB_SEARCH = Capability(
    name="web_search",
    tools={"claude_code": ("mcp__web_search__*",)},
    mcp_servers=(McpServer(name="web_search", url="https://search.example.com/mcp"),),
    allowed_urls=("search.example.com",),
    boundary="Web search answers from the open web, never from the herd.",
    card="Use web_search for documentation you do not have.",
)
WITH_WEB_SEARCH = replace(WORKER_PROFILE, capabilities=(HERD, SHELL, WEB_SEARCH))


@pytest.mark.parametrize(
    ("part", "text", "words"),
    [
        ("mission", role_text(WORKER_PROFILE, "mission"), MISSION_WORDS),
        ("workspace", role_text(WORKER_PROFILE, "workspace"), WORKSPACE_WORDS),
        ("suggestions", role_text(WORKER_PROFILE, "suggestions"), SUGGESTIONS_WORDS),
        ("boundaries", BOUNDARIES.read_text("utf-8"), BOUNDARIES_WORDS),
        *(
            (f"card {name}", capability.card, CARD_WORDS)
            for name, capability in CAPABILITIES.items()
        ),
    ],
)
def test_every_prompt_part_fits_its_word_budget(
    part: str, text: str, words: int
) -> None:
    """A part that grows past its budget is a prompt change the design asks to make
    one item at a time, not by accretion."""
    assert len(text.split()) <= words, part


def test_sections_come_in_the_fixed_order_with_the_agent_filled_in() -> None:
    task = TASK.model_copy(update={"refs": ("c-worker-02-00001",)})
    prompt = assemble(WORKER_PROFILE, WORKER, task)
    headings = [line for line in prompt.splitlines() if line.startswith("# ")]
    assert headings == [
        "# Mission",
        "# Workspace",
        "# Boundaries",
        "# Tools: herd",
        "# Tools: shell",
        "# Suggestions",
        "# Your task",
    ]
    assert "$" not in prompt, "every placeholder is filled"
    assert f"You are {WORKER}," in prompt
    assert f"claims/{WORKER}/" in prompt
    boundaries = prompt.split("# Boundaries")[1].split("# Tools: herd")[0]
    assert HERD.boundary in boundaries and SHELL.boundary in boundaries
    assert prompt.rstrip().endswith(
        f"Your goal: {task.goal}\n\nIt is about: c-worker-02-00001."
    )
    assert "These are suggestions, not rules." in prompt
    assert "YAML frontmatter" not in prompt.split("# Suggestions")[1]


def test_a_granted_capability_reaches_the_prompt_and_both_harnesses_together() -> None:
    """Adding a tool set is one bundle: its card in the prompt, its tools in the
    allowlist, its MCP server in the config and its host in the egress policy."""
    params = Params()
    claude = claude_code.ClaudeCodeHarness(
        config=claude_code.ClaudeCodeConfig(
            model="claude-opus-5-5",
            context_window_tokens=200_000,
            profile=WITH_WEB_SEARCH,
        ),
        params=params,
    ).build(WORKER, TASK, START_PACK, ENDPOINT, HOOKS, WORKSPACE)
    command = list(claude.command)
    prompt = command[command.index("-p") + 1]
    assert "# Tools: web_search" in prompt and WEB_SEARCH.card in prompt
    assert WEB_SEARCH.boundary in prompt
    allowed = command[command.index("--allowedTools") + 1].split(",")
    assert allowed == [
        "mcp__herd__*",
        "Bash",
        "Read",
        "Edit",
        "Write",
        "Glob",
        "Grep",
        "mcp__web_search__*",
    ]
    servers = json.loads(claude.files[claude_code.MCP_CONFIG_PATH])["mcpServers"]
    assert set(servers) == {"herd", "web_search"}
    assert servers["web_search"] == {
        "type": "http",
        "url": "https://search.example.com/mcp",
        "headers": {},
    }
    assert claude.egress is not None
    assert any(rule.host == "search.example.com" for rule in claude.egress.allow)

    spec = copilot.CopilotHarness(
        config=copilot.CopilotConfig(model="gpt-6.1-sol", profile=WITH_WEB_SEARCH),
        params=params,
    ).build(WORKER, TASK, START_PACK, ENDPOINT, HOOKS, WORKSPACE)
    command = list(spec.command)
    assert "# Tools: web_search" in command[command.index("-p") + 1]
    assert command[command.index("--allow-url") + 1] == "search.example.com"
    servers = json.loads(spec.files[copilot.MCP_CONFIG_PATH])["mcpServers"]
    assert set(servers) == {"herd", "web_search"}
    assert spec.egress is not None
    assert any(rule.host == "search.example.com" for rule in spec.egress.allow)


def test_herd_yaml_names_capabilities_by_name_and_refuses_unknown_ones() -> None:
    profile = with_capabilities(WORKER_PROFILE, ("shell",))
    assert profile.capabilities == (SHELL,)
    assert not profile.has("herd")
    assert "# Tools: herd" not in assemble(profile, WORKER, TASK)
    with pytest.raises(ValueError, match="computer"):
        with_capabilities(WORKER_PROFILE, ("herd", "computer"))


def test_the_runtime_reads_the_capability_set_from_harness_settings() -> None:
    def herd_config(settings: dict[str, JsonValue]) -> HerdConfig:
        return HerdConfig(
            judges={},
            executor=AdapterChoice(kind="local"),
            harness=AdapterChoice(kind="claude_code", settings=settings),
        )

    assert worker_profile(herd_config({})) is WORKER_PROFILE
    assert worker_profile(
        herd_config({"capabilities": ["shell", "herd"]})
    ).capabilities == (
        SHELL,
        HERD,
    )
