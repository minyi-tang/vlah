"""Load a charter and keep deployment settings out of the public problem files."""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import yaml
from pydantic import TypeAdapter

from vlah.core.blackboard.api import PublicProblemFiles
from vlah.core.problem.api import Approach, HerdConfig, Problem
from vlah.core.types.params import (
    EgressPolicy,
    GateSpec,
    Params,
    parse_limits,
    parse_target,
)
from vlah.core.types.sources import SourceDecl

CHARTER_FILES = (
    "status.md",
    "problem.md",
    "why.md",
    "constraints.md",
    "progress.md",
    "taste.md",
    "non_goals.md",
    "escalation.md",
    "assumptions.md",
    "disagreements.md",
    "sources.md",
    "egress.md",
    "transcript.ptr",
    "params.yaml",
    "gates.yaml",
    "egress.yaml",
)
STATUS_SCHEMA = TypeAdapter(Literal["draft", "aligned", "revising"])
GATES_SCHEMA = TypeAdapter(tuple[GateSpec, ...])


def require_unlinked_path(root: Path, path: Path) -> None:
    """Public paths must not follow links to private or external material."""
    current_path = root
    for part in path.relative_to(root).parts:
        current_path = current_path / part
        if current_path.is_symlink():
            raise ValueError(f"{current_path}: path: symbolic links are not allowed")
        else:
            pass


def public_view(problem_root: Path) -> PublicProblemFiles:
    """Snapshot only the documented public inputs, never deployment or run state."""
    root = problem_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"{root}: problem_root: expected a directory")
    else:
        pass
    paths = [
        root / "spec.md",
        root / "map/gap.md",
        *(root / "charter" / name for name in CHARTER_FILES),
    ]
    for directory_name, pattern in (
        ("approaches", "*.md"),
        ("eval/sources", "*.yaml"),
    ):
        directory = root / directory_name
        require_unlinked_path(root, directory)
        if directory.exists() and not directory.is_dir():
            raise ValueError(f"{directory}: path: expected a directory")
        else:
            paths.extend(sorted(directory.glob(pattern)))

    measurements = root / "map/measurements"
    require_unlinked_path(root, measurements)
    if measurements.exists() and not measurements.is_dir():
        raise ValueError(f"{measurements}: path: expected a directory")
    elif measurements.exists():
        pending = [measurements]
        while pending:
            path = pending.pop()
            require_unlinked_path(root, path)
            if path.is_dir():
                pending.extend(
                    sorted(
                        child
                        for child in path.iterdir()
                        if not child.name.startswith(".")
                        and child.name not in {"private", "herd.yaml", "__pycache__"}
                        and child.suffix != ".pyc"
                    )
                )
            else:
                paths.append(path)
    else:
        pass

    public_files: dict[str, bytes] = {}
    for path in sorted(paths):
        require_unlinked_path(root, path)
        if not path.exists():
            continue
        elif not path.is_file():
            raise ValueError(f"{path}: file: expected a regular file")
        else:
            public_files[path.relative_to(root).as_posix()] = path.read_bytes()
    return PublicProblemFiles(root=root, files=public_files)


@contextmanager
def problem_text(
    problem_files: PublicProblemFiles, relative_path: str, field: str
) -> Iterator[str]:
    """Attach a filename and field to decoding, YAML and schema errors."""
    path = problem_files.root / relative_path
    if relative_path not in problem_files.files:
        raise ValueError(f"{path}: {field}: required file is missing")
    else:
        pass
    try:
        yield problem_files.files[relative_path].decode("utf-8")
    except (ValueError, yaml.YAMLError) as error:
        raise ValueError(f"{path}: {field}: {error}") from error


def load_approaches(problem_files: PublicProblemFiles) -> tuple[Approach, ...]:
    approaches: list[Approach] = []
    for relative_path in problem_files.files:
        if not relative_path.startswith("approaches/"):
            continue
        else:
            pass
        with problem_text(problem_files, relative_path, "frontmatter") as text:
            lines = text.splitlines(keepends=True)
            if not lines or lines[0].strip() != "---":
                raise ValueError("expected an opening --- line")
            else:
                pass
            closing_index = next(
                (
                    index
                    for index in range(1, len(lines))
                    if lines[index].strip() == "---"
                ),
                None,
            )
            if closing_index is None:
                raise ValueError("expected a closing --- line")
            else:
                frontmatter: object = yaml.safe_load("".join(lines[1:closing_index]))
            if not isinstance(frontmatter, dict):
                raise ValueError("expected a YAML mapping")
            else:
                approach = Approach.model_validate(
                    {
                        "id": Path(relative_path).stem,
                        **frontmatter,
                        "body": "".join(lines[closing_index + 1 :]),
                    }
                )
            if approach.id != Path(relative_path).stem:
                raise ValueError("id must match the filename")
            else:
                approaches.append(approach)

    approach_ids = {approach.id for approach in approaches}
    for approach in approaches:
        if approach.parent is not None and approach.parent not in approach_ids:
            raise ValueError(
                f"{problem_files.root}/approaches/{approach.id}.md: "
                f"parent: unknown approach {approach.parent!r}"
            )
        else:
            pass
    return tuple(approaches)


def load_problem(problem_root: Path) -> Problem:
    """Validate the frozen problem schemas without executing any problem code."""
    public_files = public_view(problem_root)
    root = public_files.root
    with problem_text(public_files, "charter/status.md", "status") as text:
        status = STATUS_SCHEMA.validate_python(text.strip())
    with problem_text(public_files, "charter/params.yaml", "params") as text:
        params = Params.model_validate(yaml.safe_load(text))
    with problem_text(public_files, "charter/constraints.md", "limits") as text:
        limits = parse_limits(text)
    with problem_text(public_files, "charter/progress.md", "target") as text:
        target = parse_target(text)
    with problem_text(public_files, "spec.md", "spec_markdown") as text:
        spec_markdown = text

    if "charter/gates.yaml" in public_files.files:
        with problem_text(public_files, "charter/gates.yaml", "gates") as text:
            gates = GATES_SCHEMA.validate_python(yaml.safe_load(text))
    else:
        gates = ()
    if "charter/egress.yaml" in public_files.files:
        with problem_text(public_files, "charter/egress.yaml", "egress") as text:
            egress = EgressPolicy.model_validate(yaml.safe_load(text))
    else:
        egress = EgressPolicy()

    sources: list[SourceDecl] = []
    for relative_path in public_files.files:
        if not relative_path.startswith("eval/sources/"):
            continue
        else:
            pass
        with problem_text(public_files, relative_path, "source") as text:
            source = SourceDecl.model_validate(yaml.safe_load(text))
            if source.id != Path(relative_path).stem:
                raise ValueError("id must match the filename")
            else:
                sources.append(source)
    with problem_text(public_files, "charter/sources.md", "sources") as text:
        visible_text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        for source in sources:
            if (
                re.search(rf"(?<![\w.-]){re.escape(source.id)}(?![\w.-])", visible_text)
                is None
            ):
                raise ValueError(f"missing source {source.id!r}")
            else:
                pass

    approaches = load_approaches(public_files)
    approach_ids = {approach.id for approach in approaches}
    for approach_id, slot_count in params.slots_per_approach.items():
        if approach_id not in approach_ids or slot_count < 0:
            raise ValueError(
                f"{root}/charter/params.yaml: slots_per_approach.{approach_id}: "
                "must name an existing approach and a nonnegative slot count"
            )
        else:
            pass

    source_ids = {source.id for source in sources}
    if target is not None and target.source not in source_ids:
        raise ValueError(
            f"{root}/charter/progress.md: target.source: unknown source {target.source!r}"
        )
    else:
        pass
    for source_id, run_count in limits.runs.items():
        if source_id not in source_ids or run_count < 0:
            raise ValueError(
                f"{root}/charter/constraints.md: limits.runs.{source_id}: "
                "must name an existing source and a nonnegative run count"
            )
        else:
            pass

    herd_path = root / "herd.yaml"
    require_unlinked_path(root, herd_path)
    if not herd_path.is_file():
        raise ValueError(f"{herd_path}: herd_config: expected a regular file")
    else:
        pass
    try:
        herd_config = HerdConfig.model_validate(
            yaml.safe_load(herd_path.read_text(encoding="utf-8"))
        )
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise ValueError(f"{herd_path}: herd_config: {error}") from error
    if set(herd_config.judges) != source_ids:
        raise ValueError(
            f"{herd_path}: judges: must match the sources in eval/sources/"
        )
    else:
        pass
    for source_id, judge in herd_config.judges.items():
        if bool(judge.command) == bool(judge.adapter_class):
            raise ValueError(
                f"{herd_path}: judges.{source_id}: "
                "set exactly one of command and adapter_class"
            )
        else:
            pass

    return Problem(
        root=root,
        status=status,
        params=params,
        gates=gates,
        egress=egress,
        sources=tuple(sources),
        approaches=approaches,
        spec_markdown=spec_markdown,
        limits=limits,
        target=target,
        public_files=public_files,
        herd_config=herd_config,
    )
