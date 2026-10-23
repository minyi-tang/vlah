"""The opportunity map: measurement scripts rerun on each new baseline (D76).

Scripts live on the blackboard at map/measurements/<id>/<script>. The Integrator
runs each one against the baseline's files, writes its output to
map/measurements/<id>/<k>/output.txt and emits map_updated. Blackboard files are
append-only, so map/gap.md is written once, on the first baseline measured, and
the per-baseline outputs are the map after that.

By arm (ArmConfig.map): full and updates_no_scripts rerun the scripts after every
advance (an S1 worker only cannot add scripts, which the tool server enforces);
initial_only and initial_own measure baseline@0 once.
"""

import logging
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from vlah.arms.config import ArmConfig
from vlah.core.blackboard.api import Blackboard
from vlah.core.integrator.records import INTEGRATOR, IntegratorRecords

logger = logging.getLogger(__name__)

MEASUREMENTS_PREFIX = "map/measurements/"
GAP_PATH = "map/gap.md"
OUTPUT_FILE_NAME = "output.txt"
SCRIPT_TIMEOUT_SECONDS = 120.0
OUTPUT_CHARACTERS = 4000
UPDATING_ARMS: frozenset[str] = frozenset({"full", "updates_no_scripts"})


class ScriptRunner(Protocol):
    def __call__(self, script: Path, baseline_directory: Path) -> str:
        """Run one measurement script against a copy of the baseline; its output."""
        ...


def run_script(script: Path, baseline_directory: Path) -> str:
    """The default runner: the script under this Python, in a scratch copy of the baseline."""
    with tempfile.TemporaryDirectory() as scratch:
        copy = Path(scratch) / "baseline"
        shutil.copytree(baseline_directory, copy)
        try:
            completed = subprocess.run(
                [sys.executable, str(script), str(copy)],
                cwd=copy,
                capture_output=True,
                text=True,
                timeout=SCRIPT_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return f"timed out after {SCRIPT_TIMEOUT_SECONDS:g} s"
        output = completed.stdout if completed.returncode == 0 else completed.stderr
        return output[:OUTPUT_CHARACTERS]


def gap_summary(k: int, outputs: dict[str, str]) -> str:
    sections = "\n\n".join(
        f"## {measurement_id}\n\n{text}" for measurement_id, text in outputs.items()
    )
    return (
        f"# Opportunity map\n\nMeasured on baseline@{k}. This file is written once; "
        f"every later baseline's measurements are under {MEASUREMENTS_PREFIX}<id>/<k>/"
        f"{OUTPUT_FILE_NAME}, and map_updated events name the latest.\n\n{sections}\n"
    )


@dataclass(kw_only=True)
class OpportunityMap:
    blackboard: Blackboard
    records: IntegratorRecords
    arm: ArmConfig
    work_directory: Path
    runner: ScriptRunner = run_script

    def scripts(self) -> dict[str, tuple[str, ...]]:
        """Measurement scripts by id: map/measurements/<id>/<script>, not the outputs."""
        found: dict[str, list[str]] = {}
        for path in self.blackboard.list_paths(MEASUREMENTS_PREFIX, None):
            segments = path.removeprefix(MEASUREMENTS_PREFIX).split("/")
            if len(segments) == 2:
                found.setdefault(segments[0], []).append(path)
            else:
                continue
        return {key: tuple(sorted(value)) for key, value in found.items()}

    def materialize(self, path: str) -> Path:
        target = self.work_directory / "map" / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.blackboard.read(path, None))
        return target

    def updates_after_advances(self) -> bool:
        return self.arm.map in UPDATING_ARMS

    def run(self, k: int, baseline_directory: Path) -> None:
        """Measure baseline@k with every script whose output for k is not there yet."""
        outputs: dict[str, str] = {}
        files: dict[str, bytes] = {}
        for measurement_id, paths in sorted(self.scripts().items()):
            output_path = (
                f"{MEASUREMENTS_PREFIX}{measurement_id}/{k}/{OUTPUT_FILE_NAME}"
            )
            if self.blackboard.list_paths(output_path, None):
                continue
            else:
                text = "\n".join(
                    f"## {Path(path).name}\n"
                    f"{self.runner(self.materialize(path), baseline_directory)}"
                    for path in paths
                )
                outputs[measurement_id] = text
                files[output_path] = text.encode("utf-8")
        if not files:
            return
        else:
            if not self.blackboard.list_paths(GAP_PATH, None):
                files[GAP_PATH] = gap_summary(k, outputs).encode("utf-8")
            else:
                pass
            receipt = self.blackboard.write(
                INTEGRATOR, files, f"opportunity map on baseline@{k}"
            )
            if receipt.status == "rejected":
                logger.error(
                    f"the blackboard rejected the map outputs: {receipt.violations}"
                )
            else:
                self.records.write_event(
                    "map_updated",
                    data={
                        "baseline": k,
                        "measurements": sorted(outputs),
                        "output": MEASUREMENTS_PREFIX,
                    },
                )
