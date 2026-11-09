"""Convert a packed worker problem and an approved herd template without executing either."""

import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import yaml

from vlah.core.blackboard.api import PublicProblemFiles
from vlah.core.problem.api import Problem
from vlah.core.problem.loader import load_problem, require_unlinked_path
from vlah.core.types.clock import Clock

PACKED_DIRECTORY = "packed"
CANDIDATE_DIRECTORY = "policy"
PACKED_ROOT_FILES = frozenset(
    {"__init__.py", "problem.py", "tools.md", "baseline_scores.json"}
)
PACKED_DIRECTORIES = frozenset({"policy", "sim", "sites", "map"})
FORBIDDEN_NAMES = frozenset(
    {
        ".git",
        ".herd",
        "private",
        "hidden",
        "reference",
        "probes",
        "planted",
        "herd.yaml",
        "problem.yaml",
        "test_hidden.py",
        "pi_prompt.md",
    }
)
SLOW_ADAPTER = "vlah.adapters.signal.testbed_http.client:SlowTierSource"


def packed_files(packed_root: Path) -> dict[str, bytes]:
    """Accept only a worker pack, never a source problem or linked operator material."""
    root = packed_root.resolve(strict=True)
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        require_unlinked_path(root, path)
        if any(part in FORBIDDEN_NAMES for part in relative.parts):
            raise ValueError(f"{path}: not a packed worker problem")
        elif "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        elif not path.is_file():
            continue
        elif (
            relative.parts[0] not in PACKED_DIRECTORIES
            and relative.as_posix() not in PACKED_ROOT_FILES
        ):
            raise ValueError(f"{path}: unexpected file in the packed worker problem")
        else:
            files[relative.as_posix()] = path.read_bytes()
    required = PACKED_ROOT_FILES | {"policy/params.yaml"}
    missing = sorted(required - files.keys())
    if missing:
        raise ValueError(f"{root}: incomplete worker pack; missing {missing}")
    else:
        return files


def worker_problem_files(problem: Problem) -> PublicProblemFiles:
    return PublicProblemFiles(
        root=problem.root,
        files={
            name: content
            for name, content in problem.public_files.files.items()
            if not name.startswith("eval/")
        },
    )


def require_approved_template(problem: Problem) -> None:
    if problem.status != "aligned":
        raise ValueError(
            f"{problem.root}: charter/status.md is {problem.status}; obtain operator "
            "approval for the source policy and charter, then use a signed --template"
        )
    elif not any(problem.params.slots_per_approach.values()):
        raise ValueError("charter/params.yaml: slots_per_approach has no worker slots")
    elif len(problem.sources) != 1 or problem.sources[0].mode != "pull":
        raise ValueError("the testbed template must declare exactly one pull source")
    elif (
        problem.herd_config.judges[problem.sources[0].id].adapter_class != SLOW_ADAPTER
    ):
        raise ValueError(f"the testbed judge must use adapter_class {SLOW_ADAPTER}")
    elif problem.gates:
        raise ValueError(
            "testbed constraints are checked by the service; local candidate gates "
            "are not supported by the testbed assembly"
        )
    else:
        pass


def convert_testbed_problem(
    packed_root: Path, template_root: Path, destination: Path, *, clock: Clock
) -> Problem:
    """Seed target/policy from the pack; preserve every operator policy in the template."""
    template = load_problem(template_root)
    require_approved_template(template)
    payloads = packed_files(packed_root)
    root = destination.resolve()
    if root.exists():
        raise ValueError(f"{root} already exists; choose a fresh testbed run directory")
    else:
        root.mkdir(parents=True, mode=0o700)
    for relative, content in template.public_files.files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    for relative, content in payloads.items():
        path = root / PACKED_DIRECTORY / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    target = root / "target"
    shutil.copytree(root / PACKED_DIRECTORY / CANDIDATE_DIRECTORY, target / "policy")
    subprocess.run(["git", "init", "-q", "-b", "main", str(target)], check=True)
    subprocess.run(["git", "-C", str(target), "add", "policy"], check=True)
    commit_date = f"@{int(clock.now_seconds())} +0000"
    subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "-c",
            "user.name=VLAH",
            "-c",
            "user.email=herd@localhost",
            "commit",
            "-q",
            "--no-gpg-sign",
            "-m",
            "Seed the packed testbed candidate",
        ],
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": commit_date,
            "GIT_COMMITTER_DATE": commit_date,
        },
        check=True,
    )
    executor = template.herd_config.executor.model_copy(
        update={
            "settings": {
                **template.herd_config.executor.settings,
                "target_repository": str(target),
                "target_ref": "HEAD",
            }
        }
    )
    herd_config = template.herd_config.model_copy(update={"executor": executor})
    (root / "herd.yaml").write_text(
        yaml.safe_dump(herd_config.model_dump(mode="json")), encoding="utf-8"
    )
    problem = load_problem(root)
    return replace(problem, public_files=worker_problem_files(problem))
