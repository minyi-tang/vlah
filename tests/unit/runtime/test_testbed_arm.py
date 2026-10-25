"""The herd-arm command submits, merges and closes out against the real HTTP service."""

import json
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from random import Random

import httpx
import pytest

from testbed.cli.client import write_manifest, zip_candidate
from testbed.integrator.core import IntegratorConfig
from tests.unit.runtime.testbed_support import (
    RunningService,
    ScriptedCliHarness,
    make_packed,
    make_template,
    running_service,
    write_files,
)
from vlah.adapters.executor.local.passthrough import PassthroughSandbox
from vlah.adapters.harness.scripted.harness import ScriptedWorkerHarness
from vlah.adapters.signal.testbed_http.client import SlowTierClient
from vlah.arms.config import PRESETS
from vlah.cli import main
from vlah.core.blackboard.api import MergedCommit
from vlah.core.integrator.core_adapter import config_from_params
from vlah.core.integrator.service import HerdIntegrator
from vlah.core.problem.loader import load_problem
from vlah.core.types.candidates import candidate_hash
from vlah.core.types.clock import FakeClock
from vlah.core.types.events import BaselineAdvanced
from vlah.core.types.ids import ActorId
from vlah.ports.executor import AgentSpec
from vlah.runtime.assembly import RunComponents, RunContext
from vlah.runtime.testbed_assembly import TestbedAssembly as ArmAssembly
from vlah.runtime.testbed_problem import convert_testbed_problem
from vlah.runtime.testbed_workers import TestbedHarness as ArmHarness

pytestmark = pytest.mark.runtime


@pytest.fixture
def service(tmp_path: Path) -> Iterator[RunningService]:
    with running_service(tmp_path / "service") as running:
        yield running


@pytest.fixture
def template(tmp_path: Path) -> Path:
    return make_template(tmp_path / "template")


@pytest.fixture
def packed(tmp_path: Path) -> Path:
    return make_packed(tmp_path / "packed")


def test_arm_cli_submits_merges_and_records_the_exact_final(
    tmp_path: Path,
    service: RunningService,
    template: Path,
    packed: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner's entry point was missing; workers could only simulate locally (#122/#115)."""
    configured_harnesses: list[ArmHarness] = []
    launched_specs: list[AgentSpec] = []
    integrators: list[HerdIntegrator] = []
    runtime_clock = FakeClock()
    real_harness = ArmAssembly.make_harness
    real_close = ArmAssembly.close
    real_build = ArmAssembly.build

    def finish_after_two_merges(merged_commit: MergedCommit) -> None:
        for event in merged_commit.events:
            if isinstance(event.data, BaselineAdvanced) and event.data.baseline == 2:
                runtime_clock.advance(3600)

    def record_merges(assembly: ArmAssembly, context: RunContext) -> RunComponents:
        components = real_build(assembly, context)
        components.blackboard.subscribe(finish_after_two_merges)
        return components

    def scripted_harness(assembly: ArmAssembly, context: RunContext) -> ArmHarness:
        harness = real_harness(assembly, context)
        assert isinstance(harness, ArmHarness)
        assert isinstance(harness.harness, ScriptedWorkerHarness)
        assert harness.harness.config.source_ids == ("slow",)
        assert harness.harness.config.script == "sleep"
        harness.harness = ScriptedCliHarness(default_program="fixture")
        configured_harnesses.append(harness)
        return harness

    def assembly_factory(
        *,
        client: SlowTierClient,
        integrator_config: IntegratorConfig,
        worker_count: int,
    ) -> ArmAssembly:
        return ArmAssembly(
            client=client,
            integrator_config=integrator_config,
            worker_count=worker_count,
            executor_poll_seconds=0.02,
            waiter_seconds=0.02,
        )

    def fixture_sandbox(
        assembly: ArmAssembly, context: RunContext
    ) -> PassthroughSandbox:
        return PassthroughSandbox()

    def remember_close(assembly: ArmAssembly) -> None:
        for parts in assembly.parts:
            integrators.append(parts.integrator)
            launched_specs.extend(
                session.spec for session in parts.executor.sessions.values()
            )
        real_close(assembly)

    monkeypatch.setattr(ArmAssembly, "make_harness", scripted_harness)
    monkeypatch.setattr(ArmAssembly, "build", record_merges)
    if sys.platform != "darwin":
        monkeypatch.setattr(ArmAssembly, "make_sandbox", fixture_sandbox)
    monkeypatch.setattr(ArmAssembly, "close", remember_close)
    monkeypatch.setattr(
        "vlah.runtime.testbed_command.TestbedAssembly", assembly_factory
    )
    monkeypatch.setattr("vlah.runtime.testbed_command.SUPERVISOR_POLL_SECONDS", 0.02)
    monkeypatch.setattr(
        "vlah.runtime.testbed_command.SystemClock", lambda: runtime_clock
    )
    monkeypatch.setenv("TB_SYSTEM_KEY", service.core.system_key)
    config_path = tmp_path / "integrator.json"
    config_path.write_text(IntegratorConfig().to_json(), encoding="utf-8")
    assert (
        main(
            [
                "arm",
                "run",
                "--arm",
                "S2",
                "--problem",
                str(packed),
                "--slow-url",
                service.client.base_url,
                "--run-id",
                service.client.run_id,
                "--workers",
                "2",
                "--hours",
                "0.003",
                "--integrator-config",
                str(config_path),
                "--template",
                str(template),
            ]
        )
        == 0
    )
    run_root = tmp_path / "herd-problem/.herd/runs" / service.client.run_id
    report = json.loads((run_root / "report.json").read_text())
    assert report["deliverable"]["k"] == 2
    assert len(report["deliverable"]["candidates"]) == 2
    events = service.core.events(service.client.run_id)["events"]
    assert len([event for event in events if event["type"] == "baseline_advanced"]) == 2
    assert {"blackboard_write", "blackboard_read"} <= {
        event["type"] for event in events
    }
    final = next(event for event in events if event["type"] == "final")
    assert final["actor"] == "runner"
    assert (
        final["data"]["hash"]
        == json.loads((run_root / "final.json").read_text())["hash"]
    )
    assert not any(event["type"] == "actor_final" for event in events)
    fast_events = [event for event in events if event["type"] == "fast_run"]
    assert {event["actor"] for event in fast_events} == {"worker-01", "worker-02"}
    harness = configured_harnesses[0]
    assert len(set(harness.tokens.values())) == 3
    assert config_from_params(integrators[0].params) == IntegratorConfig()
    calibration = service.core.calibration(service.client.run_id)
    assert integrators[0].calibration.per_seed == tuple(
        calibration["baseline_slow"]["per_seed"]
    )
    assert integrators[0].calibration.audit_per_seed == tuple(
        calibration["baseline_audit"]["per_seed"]
    )
    assert integrators[0].calibration.sigma_dev == calibration["audit_sigma"]["dev"]
    assert integrators[0].calibration.sigma_audit == calibration["audit_sigma"]["audit"]
    assert {spec.agent for spec in launched_specs} == {"worker-01", "worker-02"}
    for spec in launched_specs:
        assert "TB_SYSTEM_KEY" not in spec.env
        assert spec.env["TB_ACTOR"] == spec.agent
        assert spec.env["TB_TOKEN"] == harness.tokens[spec.agent]
        assert spec.env["TB_SLOW_URL"] == service.client.base_url
        assert spec.env["TB_RUN_ID"] == service.client.run_id
        assert spec.env["TB_PROBLEM_DIR"].endswith("/problem")
        assert spec.env["TB_TARGET_FILE"].endswith("/BASELINE_TARGET")
        assert spec.egress is not None
        assert any(rule.purpose == "testbed" for rule in spec.egress.allow)
    assert (
        service.client.call(
            "POST",
            "/runs",
            {
                "run_id": "r-single",
                "arm": "single",
                "problem": "stub",
                "N": 1,
            },
        ).status
        == 200
    )
    single = SlowTierClient(
        base_url=service.client.base_url,
        run_id="r-single",
        system_key=service.core.system_key,
    )
    for actor in ("worker-01", "worker-02"):
        actor_root = (
            tmp_path / "herd-problem/.herd/sandboxes" / service.client.run_id / actor
        )
        fast_path = next(actor_root.glob("*/fast.json"))
        fast = json.loads(fast_path.read_text())
        recorded = next(
            event["data"] for event in fast_events if event["actor"] == actor
        )
        assert (fast["score"], fast["hash"]) == (recorded["score"], recorded["hash"])
        candidate_directory = fast_path.parent / "target/policy"
        write_manifest(
            candidate_directory, actor, predicted=-900.0, target="baseline@0"
        )
        archive = zip_candidate(candidate_directory)
        single_fast = single.call(
            "POST",
            "/fast",
            {
                "zip_b64": archive,
                "seed": 1,
                "target": "baseline@0",
            },
            ActorId("solo"),
        )
        assert single_fast.status == 200 and isinstance(single_fast.body, dict)
        for field_name in ("score", "baseline_score", "hash"):
            assert fast[field_name] == single_fast.body[field_name]
        headers = {
            "X-TB-Run": service.client.run_id,
            "X-TB-Actor": actor,
            "X-TB-Token": harness.tokens[ActorId(actor)],
        }
        for method, path, body in (
            ("GET", "/calibration", None),
            ("GET", "/history", None),
            ("GET", f"/status/{final['data']['ticket']}", None),
            ("POST", "/submit", {"zip_b64": archive, "dataset": "dev"}),
            ("POST", "/final", {"ticket": final["data"]["ticket"]}),
            ("POST", "/events", {"events": [{"type": "worker_probe", "data": {}}]}),
        ):
            assert (
                httpx.request(
                    method,
                    f"{service.client.base_url}{path}",
                    headers=headers,
                    json=body,
                ).status_code
                == 403
            )
        assert (
            httpx.get(f"{service.client.base_url}/meta", headers=headers).status_code
            == 200
        )
        headers["X-TB-Actor"] = "worker-00"
        assert (
            httpx.get(f"{service.client.base_url}/history", headers=headers).status_code
            == 403
        )
    assert (
        service.core.final_check(
            service.client.run_id, {"ticket": final["data"]["ticket"]}
        )["score"]
        == -880.0
    )


def test_conversion_keeps_candidate_hashes_and_operator_policy(
    tmp_path: Path,
    template: Path,
    packed: Path,
) -> None:
    problem = convert_testbed_problem(
        packed, template, tmp_path / "converted", clock=FakeClock()
    )
    original = load_problem(template)
    assert problem.params == original.params
    assert problem.sources == original.sources
    assert problem.limits == original.limits
    assert not any(name.startswith("eval/") for name in problem.public_files.files)
    assert candidate_hash(
        problem.root / "target/policy", "baseline@0"
    ) == candidate_hash(packed / "policy", "baseline@0")
    assert not (problem.root / "target/policy/.git").exists()
    assert not {"herd.yaml", "private/key"} & problem.public_files.files.keys()


@pytest.mark.parametrize(
    "unsafe_path",
    ["private/key", "problem.yaml", "reference/solution.py", "policy/link"],
)
def test_conversion_refuses_hidden_material_and_links(
    tmp_path: Path,
    template: Path,
    packed: Path,
    unsafe_path: str,
) -> None:
    if unsafe_path.endswith("link"):
        (packed / unsafe_path).symlink_to(template / "herd.yaml")
    else:
        write_files(packed, {unsafe_path: "operator-only\n"})
    with pytest.raises(ValueError, match="packed worker problem|symbolic links"):
        convert_testbed_problem(
            packed, template, tmp_path / "converted", clock=FakeClock()
        )
    assert not (tmp_path / "converted").exists()


def test_unsigned_template_and_mismatched_merger_are_refused_before_start(
    tmp_path: Path,
    template: Path,
    packed: Path,
) -> None:
    status = template / "charter/status.md"
    status.write_text("draft\n")
    with pytest.raises(ValueError, match="operator approval"):
        convert_testbed_problem(
            packed, template, tmp_path / "converted", clock=FakeClock()
        )
    status.write_text("aligned\n")
    problem = convert_testbed_problem(
        packed, template, tmp_path / "converted", clock=FakeClock()
    )
    assembly = ArmAssembly(
        client=SlowTierClient(
            base_url="http://127.0.0.1:1", run_id="r-refused", system_key="fixture"
        ),
        integrator_config=replace(config_from_params(problem.params), audit_max=6),
        worker_count=2,
    )
    context = RunContext(
        run_id="r-refused",
        run_root=problem.root / ".herd/runs/r-refused",
        arm=PRESETS["S2"],
        params=problem.params,
        problem=problem,
        clock=FakeClock(),
        random=Random(0),
        duration_seconds=1,
    )
    with pytest.raises(ValueError, match="integrator-config differs"):
        assembly.build(context)
    assert not context.run_root.exists()
