"""Read-isolated testbed workers; only public runtime files and each worker's files are readable."""

import inspect
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from testbed.integrator.core import IntegratorConfig
from vlah.adapters.executor.sandbox.api import WrappedCommand
from vlah.adapters.executor.sandbox.macos import quoted
from vlah.adapters.executor.sandbox.sandbox import LAYER_MACOS, ProcessSandbox
from vlah.core.types.ids import ActorId
from vlah.core.types.params import EgressPolicy
from vlah.ports.errors import NotSupportedError

PUBLIC_TESTBED_MODULES = (
    "__init__.py",
    "problem_api.py",
    "cli/__init__.py",
    "cli/client.py",
    "cli/main.py",
)
# note (Copilot): These are macOS system runtimes, not operator deployment directories.
SYSTEM_READ_PATHS = tuple(
    Path(path) for path in ("/System", "/usr", "/bin", "/sbin", "/Library")
)


def stage_worker_runtime(destination: Path) -> Path:
    """Copy importable runtime code, not the source repository or testbed operator modules."""
    destination.mkdir(parents=True)
    package_root = Path(__file__).resolve().parents[1]
    for source in sorted(package_root.rglob("*.py")):
        if "__pycache__" in source.parts:
            continue
        else:
            output = destination / "vlah" / source.relative_to(package_root)
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, output)
    testbed_root = Path(inspect.getfile(IntegratorConfig)).resolve().parents[1]
    for relative in PUBLIC_TESTBED_MODULES:
        source = testbed_root / relative
        output = destination / "testbed" / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, output)
    return destination


@dataclass(kw_only=True)
class TestbedSandbox(ProcessSandbox):
    worker_runtime: Path
    runtime_read_paths: tuple[Path, ...] = ()

    def require_enforcement(self) -> None:
        if self.layer_two != LAYER_MACOS or self.allow_unenforced:
            raise NotSupportedError(
                "testbed workers require the macOS read-allowlist sandbox; "
                "the generic egress sandbox does not hide operator files"
            )
        else:
            source_root = Path(__file__).resolve().parents[2]
            operator_paths = (
                *self.protected_directories,
                Path.home() / ".vlah-private",
            )
            for path in self.runtime_read_paths:
                resolved = path.resolve(strict=True)
                if source_root.is_relative_to(resolved) or resolved.is_relative_to(
                    source_root
                ):
                    raise ValueError(
                        f"sandbox_read_paths must not expose the source repository: {path}"
                    )
                elif any(
                    protected.resolve().is_relative_to(resolved)
                    or resolved.is_relative_to(protected.resolve())
                    for protected in operator_paths
                ):
                    raise ValueError(
                        f"sandbox_read_paths must not expose operator files: {path}"
                    )
                else:
                    pass

    def wrap(
        self,
        agent: ActorId,
        argv: tuple[str, ...],
        env: dict[str, str],
        policy: EgressPolicy,
    ) -> WrappedCommand:
        self.require_enforcement()
        sandbox_root = Path(env["VLAH_SANDBOX"]).resolve(strict=True)
        for name in ("HOME", "TMPDIR"):
            directory = Path(env[name])
            if not directory.resolve().is_relative_to(sandbox_root):
                raise ValueError(f"{name} must stay inside the worker sandbox")
            else:
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        wrapped = super().wrap(agent, argv, env, policy)
        profile = self.work_directory / f"{agent}.sb"
        read_paths = (
            *SYSTEM_READ_PATHS,
            Path(sys.prefix),
            Path(sys.base_prefix),
            self.worker_runtime,
            *self.runtime_read_paths,
        )
        rules = [
            "(deny file-read* file-write*)",
            "(allow file-read-metadata)",
            # note (Copilot): The macOS loader opens the root directory before exec.
            '(allow file-read* (literal "/"))',
            "(deny process-info*)",
            "(allow process-info* (target self))",
            '(deny mach-lookup (global-name "com.apple.securityd"))',
            *(
                (
                    f"(allow file-read* (subpath {quoted(str(path.resolve()))}))"
                    if path.is_dir()
                    else f"(allow file-read* (literal {quoted(str(path.resolve()))}))"
                )
                for path in read_paths
            ),
            f"(allow file-read* file-write* (subpath {quoted(str(sandbox_root))}))",
            '(allow file-read* file-write* (subpath "/dev"))',
        ]
        with profile.open("a", encoding="utf-8") as output:
            output.write("\n".join(rules) + "\n")
        return wrapped
