"""The composition root for a run on one machine (D58, D61, D67): the real adapters.

LocalAssembly builds every component from herd.yaml and the problem folder: a git
blackboard under the run root, an in-process key-value store, the ladder over
command judges, the MCP tool server on a local port, local worker processes under
the egress sandbox, the Copilot harness, the Integrator over the shared merger with
baseline@0 measured by the top judge, and the budget, stop rules and metrics.

Second real adapters (Kubernetes, Redis, S3, a second harness) come after H1 (D61).
"""

import logging
import os
import socket
import statistics
import subprocess
import sys
import tarfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from pydantic import JsonValue

from vlah.adapters.executor.local.executor import LocalExecutor
from vlah.adapters.executor.sandbox.sandbox import ProcessSandbox
from vlah.adapters.harness.claude_code.harness import (
    DEFAULT_MODEL_API_URLS,
    ClaudeCodeConfig,
    ClaudeCodeHarness,
)
from vlah.adapters.harness.copilot.harness import CopilotConfig, CopilotHarness
from vlah.adapters.harness.scripted.harness import (
    ScriptedWorkerConfig,
    ScriptedWorkerHarness,
)
from vlah.adapters.memory.model import MemoryModel
from vlah.adapters.memory.store import (
    MemoryKeyValueStore,
    MemoryObjectStore,
    MemoryVectorIndex,
)
from vlah.adapters.signal.command.judge import CommandBaselineScores, CommandJudge
from vlah.core.blackboard.api import PublicProblemFiles
from vlah.core.blackboard.layout import PROBLEM_AUTHOR, init_layout, open_blackboard
from vlah.core.budget.meter import KeyValueBudgetMeter
from vlah.core.eval.api import EvalService
from vlah.core.eval.jobs import GateOutcome
from vlah.core.eval.service import LadderEvalService
from vlah.core.indexer.indexer import StoreIndexer
from vlah.core.integrator.core_adapter import CoreCalibration
from vlah.core.integrator.service import HerdIntegrator
from vlah.core.metrics.standing import HerdMetrics
from vlah.core.problem.api import HerdConfig
from vlah.core.queue.queue import KeyValueLeases, KeyValueTaskQueue
from vlah.core.router.router import KeyValueRouter
from vlah.core.stop.rules import BudgetStopRules, EscalationPolicy
from vlah.core.types.params import GateSpec, Params
from vlah.core.types.paths import may_create
from vlah.ports.errors import NotSupportedError
from vlah.ports.harness import Harness
from vlah.runtime.assembly import RunComponents, RunContext
from vlah.runtime.compose import compose_arm
from vlah.runtime.herd import top_level_source
from vlah.testing.scripted_worker import SCRIPTS
from vlah.tools.herd_tools import HerdTools
from vlah.tools.server import McpToolServer

logger = logging.getLogger(__name__)

BLACKBOARD_NAME = "blackboard"
# Worker sandboxes and session logs live beside the runs, never inside a run
# directory: a sandbox there could reach the blackboard's bare repository by path
# (#32), and the sandbox profile denies every write under the run directory, the
# session's own stdout and stderr included.
SANDBOXES_DIRECTORY = "sandboxes"
LOGS_DIRECTORY = "logs"
HERD_DIRECTORY = ".herd"
DEFAULT_TARGET_REF = "HEAD"
SERVER_START_TIMEOUT_SECONDS = 10.0
GATE_TIMEOUT_SECONDS = 1800.0
OUTPUT_TAIL_CHARACTERS = 2000
SUPPORTED_HARNESSES = ("copilot", "claude_code", "scripted")
# Used when herd.yaml does not say the model's window; Claude models have at least this.
DEFAULT_CONTEXT_WINDOW_TOKENS = 200_000
SUPPORTED_EXECUTORS = ("local",)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def setting_text(settings: dict[str, JsonValue], key: str) -> str | None:
    value = settings.get(key)
    if value is None:
        return None
    elif isinstance(value, str):
        return value
    else:
        raise ValueError(f"herd.yaml: {key} must be a string, not {value!r}")


def setting_texts(settings: dict[str, JsonValue], key: str) -> tuple[str, ...]:
    value = settings.get(key)
    if value is None:
        return ()
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(str(item) for item in value)
    else:
        raise ValueError(f"herd.yaml: {key} must be a list of strings, not {value!r}")


def export_baseline(
    target_repository: Path, target_ref: str, destination: Path
) -> Path:
    """The target's files at target_ref, without .git: baseline@0 for the Integrator."""
    if destination.exists():
        return destination
    else:
        destination.mkdir(parents=True)
        archive = destination.parent / f"{destination.name}.tar"
        with archive.open("wb") as output:
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(target_repository),
                    "archive",
                    "--format=tar",
                    target_ref,
                ],
                stdout=output,
                check=True,
            )
        with tarfile.open(archive) as tar:
            tar.extractall(destination, filter="data")
        archive.unlink()
        return destination


def layout_files(problem_files: PublicProblemFiles) -> PublicProblemFiles:
    """The public files the problem's author may create on the blackboard.

    The path rules reserve a few public paths for other writers (map/gap.md is the
    Integrator's until the contract lets the human seed it); those are left out
    with a warning instead of failing the whole layout.
    """
    allowed = {
        path: content
        for path, content in problem_files.files.items()
        if may_create(PROBLEM_AUTHOR, path)
    }
    skipped = sorted(set(problem_files.files) - set(allowed))
    if skipped:
        logger.warning(f"not laid out, the author may not create them: {skipped}")
    else:
        pass
    return PublicProblemFiles(root=problem_files.root, files=allowed)


def sandbox_root_for(run_root: Path, run_id: str) -> Path:
    """<...>/.herd/sandboxes/<run id>, a sibling of the runs directory."""
    return run_root.parent.parent / SANDBOXES_DIRECTORY / run_id


def log_root_for(run_root: Path, run_id: str) -> Path:
    """<...>/.herd/logs/<run id>: the workers write their session output here."""
    return run_root.parent.parent / LOGS_DIRECTORY / run_id


def protected_directories(problem_root: Path, run_root: Path) -> tuple[Path, ...]:
    """Where workers may not write: the run's records and the problem's own files.

    The problem folder's .herd/ holds the sandboxes themselves, so it is left out.
    """
    return (
        run_root,
        *(
            entry
            for entry in sorted(problem_root.iterdir())
            if entry.name != HERD_DIRECTORY
        ),
    )


def target_of(herd_config: HerdConfig) -> tuple[Path, str]:
    """The target working tree and ref from herd.yaml's executor settings."""
    if herd_config.executor.kind not in SUPPORTED_EXECUTORS:
        raise NotSupportedError(
            f"executor {herd_config.executor.kind!r} is not available before H1 (D61); "
            f"use one of {SUPPORTED_EXECUTORS}"
        )
    else:
        pass
    settings = herd_config.executor.settings
    repository = setting_text(settings, "target_repository")
    if repository is None:
        raise ValueError("herd.yaml: executor.settings.target_repository is required")
    else:
        return Path(repository).expanduser(), (
            setting_text(settings, "target_ref") or DEFAULT_TARGET_REF
        )


def setting_number(settings: dict[str, JsonValue], key: str) -> int | None:
    value = settings.get(key)
    if value is None:
        return None
    elif isinstance(value, int) and not isinstance(value, bool):
        return value
    else:
        raise ValueError(f"herd.yaml: {key} must be a whole number, not {value!r}")


def setting_mapping(settings: dict[str, JsonValue], key: str) -> dict[str, str]:
    value = settings.get(key)
    if value is None:
        return {}
    elif isinstance(value, dict) and all(
        isinstance(item, str) for item in value.values()
    ):
        return {str(name): str(item) for name, item in value.items()}
    else:
        raise ValueError(f"herd.yaml: {key} must map names to strings, not {value!r}")


def worker_model(herd_config: HerdConfig) -> str:
    model = (
        setting_text(herd_config.harness.settings, "model")
        or herd_config.models.get("worker")
        or herd_config.models.get("default")
    )
    if model is None:
        raise ValueError("herd.yaml: set models.worker (the one model for every role)")
    else:
        return model


def copilot_harness(herd_config: HerdConfig, params: Params) -> CopilotHarness:
    settings = herd_config.harness.settings
    command = setting_texts(settings, "command")
    return CopilotHarness(
        config=CopilotConfig(
            model=worker_model(herd_config),
            reasoning_effort=setting_text(settings, "reasoning_effort"),
            allowed_urls=setting_texts(settings, "allowed_urls"),
            copilot_command=command if command else ("copilot",),
            environment=setting_mapping(settings, "environment"),
        ),
        params=params,
    )


def claude_code_harness(herd_config: HerdConfig, params: Params) -> ClaudeCodeHarness:
    settings = herd_config.harness.settings
    command = setting_texts(settings, "command")
    model_api_urls = setting_texts(settings, "model_api_urls")
    return ClaudeCodeHarness(
        config=ClaudeCodeConfig(
            model=worker_model(herd_config),
            context_window_tokens=(
                setting_number(settings, "context_window_tokens")
                or DEFAULT_CONTEXT_WINDOW_TOKENS
            ),
            effort=setting_text(settings, "effort"),
            claude_command=command if command else ("claude",),
            allowed_urls=setting_texts(settings, "allowed_urls"),
            model_api_urls=model_api_urls if model_api_urls else DEFAULT_MODEL_API_URLS,
            environment=setting_mapping(settings, "environment"),
        ),
        params=params,
    )


def scripted_harness(
    herd_config: HerdConfig, source_ids: tuple[str, ...]
) -> ScriptedWorkerHarness:
    """The model-free worker (#127); settings: script, python_executable, window_tokens."""
    settings = herd_config.harness.settings
    script = setting_text(settings, "script") or "submit_one"
    if script not in SCRIPTS:
        raise ValueError(f"herd.yaml: harness.settings.script must be one of {SCRIPTS}")
    else:
        return ScriptedWorkerHarness(
            config=ScriptedWorkerConfig(
                source_ids=source_ids,
                script=script,
                python_executable=(
                    setting_text(settings, "python_executable") or sys.executable
                ),
                window_tokens=(
                    setting_number(settings, "window_tokens")
                    or DEFAULT_CONTEXT_WINDOW_TOKENS
                ),
                environment=setting_mapping(settings, "environment"),
            )
        )


def worker_harness(
    herd_config: HerdConfig, params: Params, source_ids: tuple[str, ...]
) -> Harness:
    """The harness herd.yaml names: copilot (#48), claude_code (#18) or scripted (#127)."""
    kind = herd_config.harness.kind
    if kind == "copilot":
        return copilot_harness(herd_config, params)
    elif kind == "claude_code":
        return claude_code_harness(herd_config, params)
    elif kind == "scripted":
        return scripted_harness(herd_config, source_ids)
    else:
        raise NotSupportedError(
            f"harness {kind!r} is not available; use one of {SUPPORTED_HARNESSES}"
        )


@dataclass(frozen=True, kw_only=True)
class CommandGateRunner:
    """A gate from charter/gates.yaml, run as a shell command in the candidate."""

    problem_root: Path

    def __call__(self, gate: GateSpec, candidate_directory: Path) -> GateOutcome:
        try:
            completed = subprocess.run(
                gate.command,
                shell=True,
                cwd=candidate_directory,
                env={
                    **os.environ,
                    "VLAH_CANDIDATE_DIR": str(candidate_directory),
                    "VLAH_PROBLEM_ROOT": str(self.problem_root),
                },
                capture_output=True,
                text=True,
                timeout=GATE_TIMEOUT_SECONDS,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as error:
            return GateOutcome(passed=False, output=str(error))
        return GateOutcome(
            passed=completed.returncode == 0,
            output=(completed.stdout + completed.stderr)[-OUTPUT_TAIL_CHARACTERS:],
        )


@dataclass(kw_only=True)
class ServedToolServer:
    """An MCP tool server on its own thread; the executor's claim ledger too."""

    server: McpToolServer
    uvicorn_server: uvicorn.Server
    thread: threading.Thread

    @classmethod
    def start(cls, tools: HerdTools, host: str) -> "ServedToolServer":
        server = McpToolServer(tools=tools, host=host, port=free_port())
        uvicorn_server = uvicorn.Server(
            uvicorn.Config(
                server.app(), host=server.host, port=server.port, log_level="warning"
            )
        )
        thread = threading.Thread(
            target=uvicorn_server.run, name=f"tool-server-{server.port}", daemon=True
        )
        thread.start()
        deadline = time.monotonic() + SERVER_START_TIMEOUT_SECONDS
        while not uvicorn_server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not uvicorn_server.started:
            raise RuntimeError(f"the tool server on {server.base_url} did not start")
        else:
            return cls(server=server, uvicorn_server=uvicorn_server, thread=thread)

    def close(self) -> None:
        self.uvicorn_server.should_exit = True
        self.thread.join(SERVER_START_TIMEOUT_SECONDS)


@dataclass(frozen=True, kw_only=True)
class SleepingWaiter:
    """The Integrator's closeout waiter in a real run: let the judge work, then tick."""

    eval_service: EvalService
    seconds: float

    def __call__(self) -> None:
        time.sleep(self.seconds)
        self.eval_service.tick()


def calibrate(
    judge: CommandJudge, baseline_directory: Path, sigma: float | None
) -> CoreCalibration:
    """baseline@0 on the top judge, daily and audit sets; the noise from its seeds."""
    daily = judge.measure_directory(baseline_directory, "dev")
    if daily.value is None or not daily.per_seed:
        raise ValueError(
            f"{judge.declaration.id} could not score baseline@0: "
            f"{daily.extra.get('error', 'no per-seed scores')}"
        )
    else:
        pass
    audit = judge.measure_directory(baseline_directory, "audit")
    seeds = daily.per_seed
    noise = (
        sigma
        if sigma is not None
        else (statistics.stdev(seeds) / len(seeds) ** 0.5 if len(seeds) > 1 else 0.0)
    )
    return CoreCalibration(
        per_seed=seeds,
        score=daily.value,
        audit_per_seed=audit.per_seed if audit.value is not None else None,
        sigma_dev=noise,
        sigma_audit=noise,
    )


@dataclass(kw_only=True)
class LocalRunParts:
    """What a run owns besides its components: the things close() must stop."""

    tool_servers: tuple[ServedToolServer, ...]
    executor: LocalExecutor
    integrator: HerdIntegrator


@dataclass(kw_only=True)
class LocalAssembly:
    """runtime.assembly.Assembly over the real adapters, for one machine."""

    tool_host: str = "127.0.0.1"
    # Development only: run workers without OS-level egress enforcement.
    allow_unenforced_egress: bool = False
    executor_poll_seconds: float = 2.0
    waiter_seconds: float = 5.0
    judge_timeout_seconds: float = 3600.0
    # The audit noise for spot checks; None derives it from baseline@0's seeds.
    integrator_sigma: float | None = None
    parts: list[LocalRunParts] = field(default_factory=list[LocalRunParts])

    def judges(self, context: RunContext, run_root: Path) -> dict[str, CommandJudge]:
        judges: dict[str, CommandJudge] = {}
        for source in context.problem.sources:
            judge_config = context.problem.herd_config.judges[source.id]
            if judge_config.command is None:
                raise NotSupportedError(
                    f"herd.yaml: judges.{source.id}: only command judges run here yet; "
                    "adapter_class judges are the testbed arm's (#21)"
                )
            else:
                judges[source.id] = CommandJudge(
                    source_declaration=source,
                    command=judge_config.command,
                    problem_root=context.problem.root,
                    clock=context.clock,
                    timeout_seconds=self.judge_timeout_seconds,
                    log_directory=run_root / "judges",
                )
        return judges

    def build(self, context: RunContext) -> RunComponents:
        problem = context.problem
        run_root = context.run_root
        run_root.mkdir(parents=True, exist_ok=True)
        target_repository, target_ref = target_of(problem.herd_config)
        baseline_directory = export_baseline(
            target_repository, target_ref, run_root / "baseline0"
        )
        blackboard = open_blackboard(run_root.parent, context.run_id, BLACKBOARD_NAME)
        if not blackboard.list_paths("spec.md", None):
            init_layout(blackboard, layout_files(problem.public_files))
        else:
            pass
        objects = MemoryObjectStore()
        store = MemoryKeyValueStore(clock=context.clock, namespace=context.run_id)
        task_queue = KeyValueTaskQueue(store=store)
        compose_queue = KeyValueTaskQueue(store=store, pool="external")
        leases = KeyValueLeases(
            store=store,
            task_queue=task_queue,
            blackboard=blackboard,
            clock=context.clock,
            lease_ttl_seconds=context.params.lease_ttl_seconds,
        )
        top_level = top_level_source(problem.sources)
        if top_level is None:
            raise ValueError("the problem declares no pull judge in eval/sources/")
        else:
            pass
        indexer = StoreIndexer(
            blackboard=blackboard,
            vectors=MemoryVectorIndex(),
            model=MemoryModel(),
            params=context.params,
            clock=context.clock,
            top_level=top_level,
        )
        router = KeyValueRouter(
            store=store,
            leases=leases,
            indexer=indexer,
            arm=context.arm,
            params=context.params,
            clock=context.clock,
        )
        meter = KeyValueBudgetMeter(store=store, limits=problem.limits)
        judges = self.judges(context, run_root)
        gate_runner = CommandGateRunner(problem_root=problem.root)
        eval_service = LadderEvalService(
            blackboard=blackboard,
            sources=tuple(judges[source.id] for source in problem.sources),
            indexer=indexer,
            budget=meter,
            gates=problem.gates,
            gate_runner=gate_runner,
            baseline_scores=CommandBaselineScores(
                judges=judges,
                baseline_directory=baseline_directory,
                blackboard=blackboard,
            ),
            arm=context.arm,
            params=context.params,
            clock=context.clock,
            random=context.random,
        )
        tool_servers = tuple(
            ServedToolServer.start(
                HerdTools(
                    blackboard=blackboard,
                    objects=objects,
                    indexer=indexer,
                    eval_service=eval_service,
                    leases=leases,
                    router=router,
                    recorder=None,
                    arm=arm,
                    params=context.params,
                    clock=context.clock,
                    run_id=context.run_id,
                    is_compose=is_compose,
                ),
                self.tool_host,
            )
            for arm, is_compose in (
                (context.arm, False),
                (compose_arm(context.arm), True),
            )
        )
        sandbox = ProcessSandbox(
            clock=context.clock,
            work_directory=run_root / "egress",
            protected_directories=protected_directories(problem.root, run_root),
            allow_unenforced=self.allow_unenforced_egress,
        )
        executor = LocalExecutor(
            sandbox_root=sandbox_root_for(run_root, context.run_id),
            log_root=log_root_for(run_root, context.run_id),
            blackboard=blackboard,
            objects=objects,
            claims=tool_servers[0].server,
            sandbox=sandbox,
            params=context.params,
            clock=context.clock,
            inherited_environment=os.environ,
            poll_seconds=self.executor_poll_seconds,
        )
        harness = worker_harness(
            problem.herd_config,
            context.params,
            tuple(source.id for source in problem.sources),
        )
        integrator = HerdIntegrator(
            blackboard=blackboard,
            eval_service=eval_service,
            task_queue=task_queue,
            compose_queue=compose_queue,
            gates=problem.gates,
            gate_runner=gate_runner,
            arm=context.arm,
            params=context.params,
            clock=context.clock,
            top_level=top_level,
            work_directory=run_root / "integrator",
            baseline_directory=baseline_directory,
            calibration=calibrate(
                judges[top_level], baseline_directory, self.integrator_sigma
            ),
            waiter=SleepingWaiter(
                eval_service=eval_service, seconds=self.waiter_seconds
            ),
        )
        self.parts.append(
            LocalRunParts(
                tool_servers=tool_servers, executor=executor, integrator=integrator
            )
        )
        return RunComponents(
            blackboard=blackboard,
            indexer=indexer,
            task_queue=task_queue,
            compose_queue=compose_queue,
            leases=leases,
            eval_service=eval_service,
            router=router,
            integrator=integrator,
            budget=meter,
            stop_rules=BudgetStopRules(
                store=store,
                blackboard=blackboard,
                meter=meter,
                integrator=integrator,
                limits=problem.limits,
                target=problem.target,
                policy=EscalationPolicy(),
            ),
            metrics=HerdMetrics(
                blackboard=blackboard,
                indexer=indexer,
                budget=meter,
                router=router,
                top_level=top_level,
            ),
            executor=executor,
            harness=harness,
            tool_server=tool_servers[0].server,
            compose_tool_server=tool_servers[1].server,
            target_repository=str(target_repository),
            target_ref=target_ref,
        )

    def close(self) -> None:
        """Stop what the runs own: worker processes, then the tool servers."""
        for parts in self.parts:
            parts.executor.close()
            parts.integrator.close()
            for served in parts.tool_servers:
                served.close()
        self.parts.clear()
