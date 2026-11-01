"""Bundle only the reviewed public problem files, including in standalone source archives."""

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

METADATA_FILES = ("LICENSE", "README.md", "hatch_build.py", "pyproject.toml")
PUBLIC_PROBLEM_FILES = (
    "sched/__init__.py",
    "sched/problem.py",
    "sched/pi_prompt.md",
    "sched/tools.md",
    "sched/map/gap.py",
    "sched/policy/params.yaml",
    "sched/policy/policy.py",
    "sched/sim/__init__.py",
    "sched/sim/engine.py",
    "sched/sim/policy_api.py",
    "sched/sim/policy_host.py",
    "sched/sim/workload.py",
    "sched/sites/common.yaml",
    "sched/sites/site1.yaml",
    "sched/sites/site2.yaml",
    "sched/sites/site3.yaml",
    "sched/sites/site4.yaml",
    "sched/sites/site5.yaml",
    "sched/sites/site6.yaml",
    "stub/__init__.py",
    "stub/problem.py",
    "stub/pi_prompt.md",
    "stub/tools.md",
    "stub/stub.yaml",
    "stub/policy/params.yaml",
)


def require_regular_file(source: Path) -> None:
    """A public filename must not redirect the build to private material."""
    if source.resolve() != source:
        raise ValueError(f"distribution input must not be a symlink: {source}")
    elif not source.is_file():
        raise FileNotFoundError(f"required distribution input is missing: {source}")
    else:
        pass


class PublicResourcesHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        if version == "editable":
            return
        else:
            root = Path(self.root).resolve()
            for source in self.build_config.only_include:
                require_regular_file(Path(source))
            for relative_path in METADATA_FILES:
                require_regular_file(root / relative_path)
            problems_root = root / "problems"
            if not problems_root.is_dir():
                problems_root = root.parent / "problems"
            else:
                pass
            force_include: dict[str, str] = {}
            for relative_path in PUBLIC_PROBLEM_FILES:
                source = problems_root / relative_path
                require_regular_file(source)
                force_include[str(source)] = f"problems/{relative_path}"
            if self.target_name == "sdist":
                for relative_path in METADATA_FILES:
                    force_include[str(root / relative_path)] = relative_path
            else:
                pass
            build_data["force_include"] = force_include
