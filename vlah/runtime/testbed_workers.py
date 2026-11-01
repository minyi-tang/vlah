"""Per-actor testbed credentials and public files around the configured worker harness."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from vlah.adapters.harness.egress import url_rules
from vlah.core.types.candidates import CandidateManifest
from vlah.core.types.clock import Clock
from vlah.core.types.ids import ActorId
from vlah.core.types.params import EgressPolicy
from vlah.core.types.tasks import Task
from vlah.ports.errors import NotSupportedError
from vlah.ports.executor import AgentSpec, Handle, StartPack
from vlah.ports.harness import (
    ContextUsage,
    Harness,
    HookConfig,
    ToolEndpoint,
    Workspace,
)

FORBIDDEN_ENVIRONMENT = frozenset(
    {
        "TB_SYSTEM_KEY",
        "VLAH_PUSH_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_ENTERPRISE_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "COPILOT_GITHUB_TOKEN",
    }
)
BASELINE_ZERO = "baseline@0"


@dataclass(kw_only=True)
class TestbedHarness:
    harness: Harness
    slow_eval_url: str
    run_id: str
    tokens: dict[ActorId, str] = field(repr=False)
    sandbox_root: Path
    worker_runtime: Path
    problem_files: dict[str, bytes]
    egress: EgressPolicy
    clock: Clock

    @property
    def supports_tool_hooks(self) -> bool:
        return self.harness.supports_tool_hooks

    @property
    def supports_checkpoint(self) -> bool:
        return False

    def build(
        self,
        agent: ActorId,
        task: Task,
        start_pack: StartPack,
        tool_endpoint: ToolEndpoint,
        hook_config: HookConfig,
        workspace: Workspace,
    ) -> AgentSpec:
        spec = self.harness.build(
            agent, task, start_pack, tool_endpoint, hook_config, workspace
        )
        forbidden = sorted(FORBIDDEN_ENVIRONMENT & spec.env.keys())
        if forbidden:
            raise ValueError(
                f"worker environment contains operator credentials: {forbidden}; "
                "use the configured model gateway, not repository credentials"
            )
        elif agent not in self.tokens:
            raise ValueError(f"no testbed token was registered for {agent}")
        else:
            sandbox = self.sandbox_root / agent / task.id
            manifest = CandidateManifest(
                kind="patch",
                target=BASELINE_ZERO,
                approach=task.approach,
                author=agent,
                created_at=self.clock.now_seconds(),
            )
            files = {
                **spec.files,
                **{
                    f"problem/{relative}": content.decode("utf-8")
                    for relative, content in self.problem_files.items()
                },
                "BASELINE_TARGET": f"{BASELINE_ZERO}\n",
                "target/policy/manifest.yaml": yaml.safe_dump(
                    manifest.model_dump(mode="json", exclude_none=True)
                ),
            }
            rules = (
                *(spec.egress or EgressPolicy()).allow,
                *self.egress.allow,
                *url_rules((self.slow_eval_url,), "testbed"),
            )
            return spec.model_copy(
                update={
                    "files": files,
                    "egress": EgressPolicy(allow=tuple(dict.fromkeys(rules))),
                    "env": {
                        **spec.env,
                        "TB_SLOW_URL": self.slow_eval_url,
                        "TB_RUN_ID": self.run_id,
                        "TB_ARM": "herd",
                        "TB_ACTOR": agent,
                        "TB_TOKEN": self.tokens[agent],
                        "TB_PRIVATE": "none",
                        "TB_PROBLEM_DIR": str(sandbox / "problem"),
                        "TB_TARGET_FILE": str(sandbox / "BASELINE_TARGET"),
                        "HOME": str(sandbox / ".home"),
                        "TMPDIR": str(sandbox / ".tmp"),
                        "XDG_CONFIG_HOME": str(sandbox / ".home" / ".config"),
                        "XDG_CACHE_HOME": str(sandbox / ".home" / ".cache"),
                        "PYTHONPATH": str(self.worker_runtime),
                        "PYTHONNOUSERSITE": "1",
                    },
                }
            )

    def usage(self, transcript_path: Path) -> ContextUsage:
        return self.harness.usage(transcript_path)

    def recent_tool_calls(self, transcript_path: Path, count: int) -> tuple[str, ...]:
        return self.harness.recent_tool_calls(transcript_path, count)

    def rate_limit_retries(self, transcript_path: Path) -> int:
        return self.harness.rate_limit_retries(transcript_path)

    def export_state(self, handle: Handle) -> bytes:
        raise NotSupportedError("testbed worker checkpoints are not supported")

    def import_state(self, state_blob: bytes) -> AgentSpec:
        raise NotSupportedError("testbed worker checkpoints are not supported")
