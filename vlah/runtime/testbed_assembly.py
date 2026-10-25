"""The real herd over the testbed HTTP service, with no operator data in worker sandboxes."""

import hmac
import math
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue

from testbed.integrator.core import IntegratorConfig
from vlah.adapters.executor.sandbox.api import Sandbox
from vlah.adapters.signal.testbed_http.client import (
    SlowTierAggregate,
    SlowTierBaselineScores,
    SlowTierClient,
    SlowTierModel,
    SlowTierRecorder,
    SlowTierSource,
)
from vlah.core.blackboard.api import Blackboard, MergedCommit, PublicProblemFiles
from vlah.core.integrator.core_adapter import CoreCalibration, config_from_params
from vlah.core.types.ids import ActorId
from vlah.ports.harness import Harness
from vlah.ports.signal_source import CandidateRef, Dataset
from vlah.runtime.api import Report
from vlah.runtime.assembly import RunComponents, RunContext
from vlah.runtime.compose import COMPOSE_AGENT
from vlah.runtime.herd import worker_id
from vlah.runtime.local_assembly import (
    CALIBRATION_ADAPTER,
    CALIBRATION_FILE_NAME,
    EvaluationSetup,
    LocalAssembly,
    export_baseline,
    layout_files,
    protected_directories,
    sandbox_root_for,
    setting_mapping,
    setting_texts,
    worker_harness,
)
from vlah.runtime.testbed_problem import (
    PACKED_DIRECTORY,
    packed_files,
    require_approved_template,
    worker_problem_files,
)
from vlah.runtime.testbed_sandbox import TestbedSandbox, stage_worker_runtime
from vlah.runtime.testbed_workers import FORBIDDEN_ENVIRONMENT, TestbedHarness

MERGER_EVENTS = frozenset(
    {
        "baseline_advanced",
        "baseline_reverted",
        "integration_failed",
        "interaction_found",
        "audit_checked",
    }
)


class AuditNoise(SlowTierModel):
    dev: float = Field(ge=0, allow_inf_nan=False)
    audit: float = Field(ge=0, allow_inf_nan=False)


class CalibrationReply(SlowTierModel):
    baseline_slow: SlowTierAggregate
    baseline_audit: SlowTierAggregate
    audit_sigma: AuditNoise
    arm: str
    N: int = Field(ge=1)

    def core_calibration(self) -> CoreCalibration:
        daily = self.baseline_slow
        audit = self.baseline_audit
        for aggregate in (daily, audit):
            if (
                aggregate.score is None
                or not math.isfinite(aggregate.score)
                or not aggregate.per_seed
                or len(aggregate.per_seed) != aggregate.n
                or any(
                    seed is None or not math.isfinite(seed)
                    for seed in aggregate.per_seed
                )
            ):
                raise ValueError("the slow tier's calibration contains a void baseline")
            else:
                pass
        assert daily.score is not None
        return CoreCalibration(
            per_seed=tuple(seed for seed in daily.per_seed if seed is not None),
            score=daily.score,
            audit_per_seed=tuple(seed for seed in audit.per_seed if seed is not None),
            sigma_dev=self.audit_sigma.dev,
            sigma_audit=self.audit_sigma.audit,
        )


class RegisteredActors(SlowTierModel):
    run_id: str
    actors: tuple[str, ...]
    scopes: dict[str, Literal["fast", "full"]]


class FinalReply(SlowTierModel):
    recorded: str
    ticket: str
    hash: str


class TestbedSource(SlowTierSource):
    def submit(
        self, candidate: CandidateRef, actor: ActorId, baseline: str, dataset: Dataset
    ) -> str:
        service_actor = ActorId("composer") if actor == COMPOSE_AGENT else actor
        return super().submit(candidate, service_actor, baseline, dataset)


@dataclass(frozen=True, kw_only=True)
class CalibratedBaselineScores:
    daily: SlowTierBaselineScores
    calibration: CoreCalibration

    def __call__(
        self, source_id: str, baseline: str, dataset: Dataset
    ) -> tuple[float, ...] | None:
        if dataset == "dev":
            return self.daily(source_id, baseline, dataset)
        elif dataset == "audit" and baseline == "baseline@0":
            return self.calibration.audit_per_seed
        else:
            return None


@dataclass(frozen=True, kw_only=True)
class TestbedMergeRecorder:
    client: SlowTierClient = field(repr=False)

    def __call__(self, merged_commit: MergedCommit) -> None:
        events: list[JsonValue] = [
            {
                "type": event.type,
                "actor": event.actor,
                "ts": event.ts,
                "data": event.data.model_dump(mode="json"),
            }
            for event in merged_commit.events
            if event.type in MERGER_EVENTS and event.data is not None
        ]
        if events:
            reply = self.client.call("POST", "/events", {"events": events})
            if reply.status != 200:
                raise RuntimeError(
                    f"the testbed recorder refused merger events: {reply.status}"
                )
            else:
                pass
        else:
            pass


@dataclass(kw_only=True)
class TestbedAssembly(LocalAssembly):
    """The Assembly interface used by build_herd and observability wrappers."""

    client: SlowTierClient = field(repr=False)
    integrator_config: IntegratorConfig
    worker_count: int
    tokens: dict[ActorId, str] = field(
        default_factory=dict[ActorId, str], init=False, repr=False
    )
    worker_runtime: Path | None = field(default=None, init=False)

    def build(self, context: RunContext) -> RunComponents:
        require_approved_template(context.problem)
        if self.parts:
            raise ValueError("a TestbedAssembly belongs to one run")
        elif self.client.run_id != context.run_id:
            raise ValueError("the testbed client and herd must name the same run")
        elif not self.client.system_key:
            raise ValueError("TB_SYSTEM_KEY is required by the testbed arm runner")
        elif self.integrator_config != config_from_params(context.params):
            raise ValueError(
                "--integrator-config differs from config_from_params(params)"
            )
        else:
            pass
        settings = context.problem.herd_config.harness.settings
        forbidden = sorted(
            FORBIDDEN_ENVIRONMENT & setting_mapping(settings, "environment").keys()
        )
        if forbidden:
            raise ValueError(
                f"herd.yaml contains forbidden worker credentials: {forbidden}"
            )
        else:
            pass
        self.worker_runtime = stage_worker_runtime(context.run_root / "worker-runtime")
        self.make_sandbox(context)
        self.tokens = {
            actor: hmac.digest(
                self.client.system_key.encode(),
                f"vlah-testbed-worker\0{context.run_id}\0{actor}".encode(),
                "sha256",
            ).hex()
            for actor in (
                COMPOSE_AGENT,
                *(worker_id(number) for number in range(1, self.worker_count + 1)),
            )
        }
        actor_tokens: dict[str, JsonValue] = {
            str(actor): token for actor, token in self.tokens.items()
        }
        reply = self.client.call(
            "POST", "/actors", {"actor_tokens": actor_tokens, "scope": "fast"}
        )
        if reply.status != 200:
            raise ValueError(
                f"the slow tier refused worker token registration ({reply.status}); "
                "it must support operator-authenticated POST /actors before workers start"
            )
        else:
            registered = RegisteredActors.model_validate(reply.body)
            if (
                registered.run_id != context.run_id
                or not self.tokens.keys() <= set(registered.actors)
                or any(registered.scopes.get(actor) != "fast" for actor in self.tokens)
            ):
                raise ValueError(
                    "the slow tier did not acknowledge fast-only scope for every worker"
                )
            else:
                return super().build(context)

    def baseline_directory(
        self, context: RunContext, target_repository: Path, target_ref: str
    ) -> Path:
        return export_baseline(
            target_repository, f"{target_ref}:policy", context.run_root / "baseline0"
        )

    def public_files(self, context: RunContext) -> PublicProblemFiles:
        return layout_files(worker_problem_files(context.problem))

    def evaluation(
        self, context: RunContext, blackboard: Blackboard, baseline_directory: Path
    ) -> EvaluationSetup:
        reply = self.client.call("GET", "/calibration", None)
        if reply.status != 200:
            raise ValueError(f"the slow tier refused calibration ({reply.status})")
        else:
            response = CalibrationReply.model_validate(reply.body)
        if response.arm != "herd" or response.N != self.worker_count:
            raise ValueError(
                "the service run is not a herd with the requested worker count"
            )
        else:
            calibration = response.core_calibration()
        (context.run_root / CALIBRATION_FILE_NAME).write_bytes(
            CALIBRATION_ADAPTER.dump_json(calibration, indent=2)
        )
        blackboard.subscribe(TestbedMergeRecorder(client=self.client))
        return EvaluationSetup(
            sources=(
                TestbedSource(
                    client=self.client, declaration=context.problem.sources[0]
                ),
            ),
            baseline_scores=CalibratedBaselineScores(
                daily=SlowTierBaselineScores(client=self.client, blackboard=blackboard),
                calibration=calibration,
            ),
            calibration=calibration,
            integrator_params=context.params,
            recorder=SlowTierRecorder(client=self.client),
            verdict_judge=None,
        )

    def make_sandbox(self, context: RunContext) -> Sandbox:
        assert self.worker_runtime is not None
        herd_config = context.problem.herd_config
        command = setting_texts(herd_config.harness.settings, "command")
        executable = shutil.which(
            command[0]
            if command
            else ("copilot" if herd_config.harness.kind == "copilot" else "claude")
        )
        if executable is None:
            raise ValueError(
                "the configured worker harness executable is not installed"
            )
        else:
            pass
        configured_paths = setting_texts(
            herd_config.executor.settings, "sandbox_read_paths"
        )
        sandbox = TestbedSandbox(
            clock=context.clock,
            work_directory=context.run_root / "egress",
            protected_directories=protected_directories(
                context.problem.root, context.run_root
            ),
            worker_runtime=self.worker_runtime,
            runtime_read_paths=(
                *(Path(path).expanduser() for path in configured_paths),
                Path(executable),
            ),
        )
        sandbox.require_enforcement()
        return sandbox

    def make_harness(self, context: RunContext) -> Harness:
        assert self.worker_runtime is not None
        return TestbedHarness(
            harness=worker_harness(context.problem.herd_config, context.params),
            slow_eval_url=self.client.base_url,
            run_id=context.run_id,
            tokens=self.tokens,
            sandbox_root=sandbox_root_for(context.run_root, context.run_id),
            worker_runtime=self.worker_runtime,
            problem_files=packed_files(context.problem.root / PACKED_DIRECTORY),
            egress=context.problem.egress,
            clock=context.clock,
        )

    def finalize(self, report: Report) -> FinalReply:
        """After the herd's closeout, call the same /final endpoint as tb final, as runner."""
        if report.run_id != self.client.run_id or len(self.parts) != 1:
            raise ValueError("the final report must belong to this testbed assembly")
        else:
            version = self.parts[0].integrator.core.versions[report.deliverable.k]
        if version.hash is None:
            raise RuntimeError(
                "the Integrator's deliverable has no completed testbed measurement"
            )
        else:
            reply = self.client.call(
                "POST", "/final", {"hash": version.hash}, ActorId("runner")
            )
        if reply.status != 200:
            raise RuntimeError(
                f"the slow tier refused the run's final ({reply.status}): {reply.body}"
            )
        else:
            final = FinalReply.model_validate(reply.body)
        if final.recorded != "final" or final.hash != version.hash:
            raise RuntimeError(
                "the slow tier did not record the frozen deliverable as the run final"
            )
        else:
            return final
