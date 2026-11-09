"""Measurement scripts run inside the worker sandbox, with none of the driver's environment (D76).

Workers may add scripts in arms whose map switch is full. The driver's environment
and files hold what workers never see, so a script is run as a worker would be:
through the run's sandbox, with no egress, on a scratch copy of the baseline.
"""

import logging
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from vlah.adapters.executor.sandbox.api import Sandbox
from vlah.core.integrator.opportunity_map import run_measurement
from vlah.core.types.ids import ActorId
from vlah.core.types.params import EgressPolicy

logger = logging.getLogger(__name__)

MAP_SCRIPT_ACTOR = ActorId("map-script")
MAP_SCRIPTS_DIRECTORY = "map-scripts"
NOT_RUN_OUTPUT = "not run: this platform cannot sandbox a measurement script"


@dataclass(kw_only=True)
class SandboxedScriptRunner:
    sandbox: Sandbox
    scratch_root: Path

    def __call__(self, script: Path, baseline_directory: Path) -> str:
        self.scratch_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=self.scratch_root) as scratch:
            sandbox_root = Path(scratch).resolve()
            baseline_copy = sandbox_root / "baseline"
            shutil.copytree(baseline_directory, baseline_copy)
            script_copy = sandbox_root / script.name
            shutil.copyfile(script, script_copy)
            home_directory = sandbox_root / "home"
            temporary_directory = sandbox_root / "tmp"
            home_directory.mkdir()
            temporary_directory.mkdir()
            try:
                wrapped = self.sandbox.wrap(
                    MAP_SCRIPT_ACTOR,
                    (sys.executable, str(script_copy), str(baseline_copy)),
                    {
                        "VLAH_SANDBOX": str(sandbox_root),
                        "HOME": str(home_directory),
                        "TMPDIR": str(temporary_directory),
                        "PATH": os.defpath,
                    },
                    EgressPolicy(),
                )
            except RuntimeError as error:
                logger.error(f"measurement script {script.name} not run: {error}")
                return NOT_RUN_OUTPUT
            else:
                return run_measurement(wrapped.argv, baseline_copy, wrapped.env)
