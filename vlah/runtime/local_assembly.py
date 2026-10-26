"""The composition root for a run on one machine (D58, D61, D67): the real adapters.

LocalAssembly builds every component from herd.yaml and the problem folder: a git
blackboard under the run root, a key-value store, the ladder over command judges,
the MCP tool server on a local port, local worker processes under the egress
sandbox, the Copilot harness, the Integrator over the shared merger with baseline@0
measured by the top judge, the Critic's holdout checks with the top judge scoring
the sealed verdict, and the budget, stop rules and metrics.

The key-value store holds the queue, leases, budget meter, router and stop-rule
state. herd.yaml's stores.key_value picks it: sqlite (the default) keeps it in
<run root>/state.sqlite, and redis takes settings.url. Either survives the process,
so a run can be resumed. baseline@0's calibration is measured once per run and kept
in <run root>/calibration.json, so a resumed run spends no judge run on it.

Second real adapters (Kubernetes, S3, a second harness) come after H1 (D61).
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

import redis
import uvicorn
from pydantic import JsonValue, TypeAdapter

from vlah.adapters.executor.docker.client import DockerCli, DockerClient
from vlah.adapters.executor.docker.egress import IMAGE_PYTHON
from vlah.adapters.executor.docker.executor import ContainerResources, DockerExecutor
from vlah.adapters.executor.local.executor import LocalExecutor
from vlah.adapters.executor.local.workspace import ViewSettings
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
from vlah.adapters.memory.store import MemoryObjectStore, MemoryVectorIndex
from vlah.adapters.signal.command.judge import CommandBaselineScores, CommandJudge
from vlah.adapters.store.redis.store import RedisKeyValueStore
from vlah.adapters.store.sqlite.store import SqliteKeyValueStore
from vlah.core.blackboard.api import Blackboard, PublicProblemFiles
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
from vlah.core.types.baseline import BaselineRecord
from vlah.core.types.params import GateSpec, Params
from vlah.core.types.paths import may_create
from vlah.ports.errors import NotSupportedError
from vlah.ports.harness import Harness
from vlah.ports.store import KeyValueStore
from vlah.roles.critic.holdout import HoldoutCritic, SealedVerdict, integrator_params
from vlah.runtime.assembly import RunComponents, RunContext
from vlah.runtime.compose import compose_arm
from vlah.runtime.herd import top_level_source
from vlah.runtime.status import KeyValueQueueCounts
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
SUPPORTED_EXECUTORS = ("local", "docker")
DOCKER_EXECUTOR = "docker"
DEFAULT_WORKER_IMAGE = "vlah-worker:dev"
# Containers reach the machine by this name; the tool servers then listen on every
# interface, because the name does not resolve to the host's loopback on Linux.
CONTAINER_HOST = "host.docker.internal"
ALL_INTERFACES = "0.0.0.0"
KEY_VALUE_STORE = "key_value"
SUPPORTED_KEY_VALUE_STORES = ("sqlite", "redis")
STATE_FILE_NAME = "state.sqlite"
CALIBRATION_FILE_NAME = "calibration.json"
CALIBRATION_ADAPTER = TypeAdapter(CoreCalibration)


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
            f"executor {herd_config.executor.kind!r} is not available on one machine; "
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


def key_value_store(herd_config: HerdConfig, context: RunContext) -> KeyValueStore:
    """The store herd.yaml names under stores.key_value; SQLite in the run root by default."""
    choice = herd_config.stores.get(KEY_VALUE_STORE)
    if choice is None or choice.kind == "sqlite":
        return SqliteKeyValueStore(
            path=context.run_root / STATE_FILE_NAME,
            clock=context.clock,
            namespace=context.run_id,
        )
    elif choice.kind == "redis":
        url = setting_text(choice.settings, "url")
        if url is None:
            raise ValueError(
                f"herd.yaml: stores.{KEY_VALUE_STORE}.settings.url is required for redis"
            )
        else:
            return RedisKeyValueStore(
                client=redis.Redis.from_url(url),
                clock=context.clock,
                namespace=context.run_id,
            )
    else:
        raise NotSupportedError(
            f"herd.yaml: stores.{KEY_VALUE_STORE}.kind {choice.kind!r} is not "
            f"available; use one of {SUPPORTED_KEY_VALUE_STORES}"
        )


def is_docker(herd_config: HerdConfig) -> bool:
    return herd_config.executor.kind == DOCKER_EXECUTOR


def tool_server_hosts(herd_config: HerdConfig, tool_host: str) -> tuple[str, str]:
    """The host the sessions are told and the one the servers bind: the same on this
    machine, the containers' name for the host and every interface under Docker."""
    if is_docker(herd_config):
        return CONTAINER_HOST, ALL_INTERFACES
    else:
        return tool_host, tool_host


def worker_python(herd_config: HerdConfig) -> str:
    """The Python the hook and the scripted worker run with: herd.yaml's, else the
    image's under Docker, else this interpreter."""
    return setting_text(herd_config.harness.settings, "python_executable") or (
        IMAGE_PYTHON if is_docker(herd_config) else sys.executable
    )


def container_resources(settings: dict[str, JsonValue]) -> ContainerResources:
    """executor.settings.resources: {cpus: 2, memory: 2g}, with those defaults."""
    value = settings.get("resources")
    if value is None:
        return ContainerResources()
    elif isinstance(value, dict):
        cpus = value.get("cpus")
        memory = value.get("memory")
        if (cpus is not None and not isinstance(cpus, (int, float))) or (
            memory is not None and not isinstance(memory, str)
        ):
            raise ValueError(
                f"herd.yaml: executor.settings.resources needs a number of cpus and a "
                f"memory size such as 2g, not {value!r}"
            )
        else:
            defaults = ContainerResources()
            return ContainerResources(
                cpus=float(cpus) if cpus is not None else defaults.cpus,
                memory=memory if memory is not None else defaults.memory,
            )
    else:
        raise ValueError(
            f"herd.yaml: executor.settings.resources must be a mapping, not {value!r}"
        )


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
            python_executable=worker_python(herd_config),
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
            python_executable=worker_python(herd_config),
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
                python_executable=worker_python(herd_config),
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
    def start(cls, tools: HerdTools, host: str, bind_host: str) -> "ServedToolServer":
        """host is the one the sessions are told; bind_host is the one uvicorn listens on."""
        server = McpToolServer(tools=tools, host=host, port=free_port())
        uvicorn_server = uvicorn.Server(
            uvicorn.Config(
                server.app(), host=bind_host, port=server.port, log_level="warning"
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


@dataclass(frozen=True, kw_only=True)
class DeliverableFiles:
    """The deliverable's directory in the Integrator's merger, for the sealed verdict.

    IntegratorService hands over only the deliverable's record, so the composition
    root, which built the Integrator, reads the version's directory from its core.
    """

    integrator: HerdIntegrator

    def __call__(self, deliverable: BaselineRecord) -> Path:
        return Path(self.integrator.core.versions[deliverable.k].dir)


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


def run_calibration(
    judge: CommandJudge, baseline_directory: Path, sigma: float | None, path: Path
) -> CoreCalibration:
    """baseline@0's calibration, measured on the run's first start and read back after."""
    if path.exists():
        return CALIBRATION_ADAPTER.validate_json(path.read_bytes())
    else:
        calibration = calibrate(judge, baseline_directory, sigma)
        path.write_bytes(CALIBRATION_ADAPTER.dump_json(calibration, indent=2))
        return calibration


@dataclass(kw_only=True)
class LocalRunParts:
    """What a run owns besides its components: the things close() must stop."""

    tool_servers: tuple[ServedToolServer, ...]
    executor: LocalExecutor | DockerExecutor
    integrator: HerdIntegrator
    key_value_store: KeyValueStore


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
    docker: DockerClient = field(default_factory=DockerCli)
    # Set by the stack: how each worker's clone opens its view over HTTP (#129).
    view_settings: ViewSettings | None = None
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

    def build_executor(
        self,
        context: RunContext,
        blackboard: Blackboard,
        objects: MemoryObjectStore,
        claims: McpToolServer,
    ) -> LocalExecutor | DockerExecutor:
        """Worker processes under the egress sandbox, or one container per session (#128)."""
        problem = context.problem
        run_root = context.run_root
        settings = problem.herd_config.executor.settings
        sandbox_root = sandbox_root_for(run_root, context.run_id)
        log_root = log_root_for(run_root, context.run_id)
        if is_docker(problem.herd_config):
            return DockerExecutor(
                run_id=context.run_id,
                image=setting_text(settings, "image") or DEFAULT_WORKER_IMAGE,
                sandbox_root=sandbox_root,
                log_root=log_root,
                blackboard=blackboard,
                objects=objects,
                claims=claims,
                params=context.params,
                clock=context.clock,
                docker=self.docker,
                resources=container_resources(settings),
                mounts_read_only={
                    Path(host_path): container_path
                    for host_path, container_path in setting_mapping(
                        settings, "mounts_read_only"
                    ).items()
                },
                refused_mount_roots=(problem.root, run_root),
                allow_unenforced=self.allow_unenforced_egress,
                view_settings=self.view_settings,
                poll_seconds=self.executor_poll_seconds,
            )
        else:
            sandbox = ProcessSandbox(
                clock=context.clock,
                work_directory=run_root / "egress",
                protected_directories=protected_directories(problem.root, run_root),
                allow_unenforced=self.allow_unenforced_egress,
            )
            return LocalExecutor(
                sandbox_root=sandbox_root,
                log_root=log_root,
                blackboard=blackboard,
                objects=objects,
                claims=claims,
                sandbox=sandbox,
                params=context.params,
                clock=context.clock,
                inherited_environment=os.environ,
                view_settings=self.view_settings,
                poll_seconds=self.executor_poll_seconds,
            )

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
        store = key_value_store(problem.herd_config, context)
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
        tool_host, tool_bind_host = tool_server_hosts(
            problem.herd_config, self.tool_host
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
                tool_host,
                tool_bind_host,
            )
            for arm, is_compose in (
                (context.arm, False),
                (compose_arm(context.arm), True),
            )
        )
        executor = self.build_executor(
            context, blackboard, objects, tool_servers[0].server
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
            # The spot checks get holdout_run_cap less the Critic's rechecks (D66).
            params=integrator_params(context.params, context.arm),
            clock=context.clock,
            top_level=top_level,
            work_directory=run_root / "integrator",
            baseline_directory=baseline_directory,
            calibration=run_calibration(
                judges[top_level],
                baseline_directory,
                self.integrator_sigma,
                run_root / CALIBRATION_FILE_NAME,
            ),
            waiter=SleepingWaiter(
                eval_service=eval_service, seconds=self.waiter_seconds
            ),
        )
        metrics = HerdMetrics(
            blackboard=blackboard,
            indexer=indexer,
            budget=meter,
            router=router,
            top_level=top_level,
        )
        critic = HoldoutCritic(
            blackboard=blackboard,
            eval_service=eval_service,
            integrator=integrator,
            arm=context.arm,
            params=context.params,
            clock=context.clock,
            top_level=top_level,
            work_directory=run_root / "critic",
            verdict=SealedVerdict(
                judge=judges[top_level],
                repeat_count=context.params.verdict_repeat_count,
                deliverable_directory=DeliverableFiles(integrator=integrator),
                metrics=metrics,
            ),
        )
        self.parts.append(
            LocalRunParts(
                tool_servers=tool_servers,
                executor=executor,
                integrator=integrator,
                key_value_store=store,
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
            metrics=metrics,
            executor=executor,
            harness=harness,
            tool_server=tool_servers[0].server,
            compose_tool_server=tool_servers[1].server,
            target_repository=str(target_repository),
            target_ref=target_ref,
            count_queued_tasks=KeyValueQueueCounts(
                task_queue=task_queue, compose_queue=compose_queue
            ),
            critic=critic,
        )

    def close(self) -> None:
        """Stop what the runs own: worker processes, the tool servers, then the store."""
        for parts in self.parts:
            parts.executor.close()
            parts.integrator.close()
            for served in parts.tool_servers:
                served.close()
            store = parts.key_value_store
            if isinstance(store, SqliteKeyValueStore):
                store.close()
            elif isinstance(store, RedisKeyValueStore):
                store.client.close()
            else:
                pass
        self.parts.clear()
