"""Problem loading, public visibility and the charter setup commands."""

import subprocess
from pathlib import Path

import pytest

from vlah import __version__
from vlah.cli import main
from vlah.cli.charter import init_problem
from vlah.core.problem.api import LoadProblem
from vlah.core.problem.loader import load_problem, public_view

pytestmark = pytest.mark.cli


@pytest.fixture
def problem_root(tmp_path: Path) -> Path:
    repository = tmp_path / "target"
    repository.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", str(repository)],
        check=True,
        capture_output=True,
    )
    root = init_problem(tmp_path / "problem", repository)
    files = {
        "charter/constraints.md": (
            "# Constraints\n\n```yaml limits\n"
            "eval_machine_hours: 4\ndays: 2\nruns: {slow: 8}\n```\n"
        ),
        "charter/progress.md": (
            "# Progress\n\n```yaml target\n"
            "source: slow\nscore: 3.5\ndirection: higher\n```\n"
        ),
        "charter/sources.md": "- [x] slow: trust=0.9, readable_by=[critic]\n",
        "charter/params.yaml": "slots_per_approach: {first: 2}\n",
        "charter/gates.yaml": (
            "- name: unit_tests\n  command: pytest -q\n  when: [pre_submit, guard]\n"
        ),
        "charter/egress.yaml": (
            "allow:\n- host: packages.example.org\n"
            "  ports: [443]\n  purpose: mirror\n"
        ),
        "spec.md": "# Improve the synthetic test problem\n",
        "approaches/first.md": (
            "---\nstatus: active\nparent: null\n---\nExplore the first direction.\n"
        ),
        "approaches/second.md": (
            "---\nstatus: frozen\nparent: first\n---\nA related direction.\n"
        ),
        "eval/sources/slow.yaml": (
            "id: slow\nmode: pull\nlatency: 10m\n"
            "budget: {per_day: 4, allocated_by: human}\n"
            "cost_machine_hours: 0.5\ntrust: 0.9\nreadable_by: [critic]\n"
        ),
        "herd.yaml": (
            "judges:\n  slow: {command: private/judge.py}\n"
            "executor: {kind: local}\nharness: {kind: scripted}\n"
        ),
        "private/judge.py": "PRIVATE_JUDGE_SENTINEL\n",
        "private/data.json": "PRIVATE_DATA_SENTINEL\n",
        "map/measurements/wait/script.py": "raise RuntimeError('must not execute')\n",
        "map/measurements/wait/input.bin": "\x00public measurement input\n",
    }
    for relative_path, text in files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def test_load_problem_preserves_schemas_and_public_bytes(problem_root: Path) -> None:
    loader: LoadProblem = load_problem
    problem = loader(problem_root)
    assert problem.status == "draft"
    assert problem.limits.eval_machine_hours == 4
    assert problem.limits.runs == {"slow": 8}
    assert problem.target is not None
    assert (problem.target.source, problem.target.score) == ("slow", 3.5)
    assert problem.params.slots_per_approach == {"first": 2}
    assert problem.sources[0].latency_seconds == 600
    assert problem.gates[0].when == ("pre_submit", "guard")
    assert problem.egress.allow[0].host == "packages.example.org"
    assert problem.approaches[0].body == "Explore the first direction.\n"
    assert problem.approaches[1].parent == "first"
    assert problem.herd_config.judges["slow"].command == "private/judge.py"
    assert problem.spec_markdown == (problem_root / "spec.md").read_text()
    for relative_path, content in problem.public_files.files.items():
        assert content == (problem_root / relative_path).read_bytes()
    assert problem.public_files.files["map/measurements/wait/input.bin"].startswith(
        b"\x00public"
    )


@pytest.mark.parametrize(
    ("relative_path", "text", "field"),
    [
        ("charter/status.md", "approved\n", "status"),
        ("charter/params.yaml", "heartbeat_interval_seconds: -1\n", "heartbeat"),
        ("charter/params.yaml", "unknown_parameter: 1\n", "unknown_parameter"),
        (
            "charter/params.yaml",
            "slots_per_approach: {missing: 1}\n",
            "slots_per_approach",
        ),
        (
            "charter/params.yaml",
            "slots_per_approach: {first: -1}\n",
            "slots_per_approach",
        ),
        ("charter/params.yaml", "unclosed: [\n", "params"),
        ("charter/constraints.md", "# No limits\n", "limits"),
        (
            "charter/constraints.md",
            "```yaml limits\neval_machine_hours: -1\ndays: 2\n```\n",
            "eval_machine_hours",
        ),
        (
            "charter/constraints.md",
            "```yaml limits\neval_machine_hours: 4\ndays: 2\nruns: {absent: 2}\n```\n",
            "limits.runs",
        ),
        ("charter/progress.md", "```yaml target\nsource: slow\n```\n", "score"),
        (
            "charter/progress.md",
            "```yaml target\nsource: absent\nscore: 1\ndirection: higher\n```\n",
            "target.source",
        ),
        (
            "charter/gates.yaml",
            "- name: test\n  command: echo ok\n  when: [never]\n",
            "when",
        ),
        (
            "charter/egress.yaml",
            "allow: [{host: example.org, purpose: unknown}]\n",
            "purpose",
        ),
        ("eval/sources/slow.yaml", "id: wrong\n", "source"),
        ("approaches/first.md", "status: active\n", "frontmatter"),
        ("approaches/first.md", "---\nstatus: active\n", "frontmatter"),
        ("approaches/first.md", "---\n- active\n---\n", "frontmatter"),
        ("approaches/first.md", "---\nstatus: unknown\n---\n", "status"),
        (
            "approaches/first.md",
            "---\nstatus: active\nparent: missing\n---\n",
            "parent",
        ),
        ("approaches/first.md", "---\nid: wrong\nstatus: active\n---\n", "id"),
        ("charter/sources.md", "- [x] slow_extra: trust=0.9\n", "slow"),
        ("charter/sources.md", "<!-- - [ ] slow: trust=0.9 -->\n", "slow"),
        ("herd.yaml", "judges: {}\n", "executor"),
        (
            "herd.yaml",
            "judges: {}\nexecutor: {kind: local}\nharness: {kind: scripted}\n",
            "judges",
        ),
    ],
)
def test_invalid_configuration_names_file_and_field(
    problem_root: Path, relative_path: str, text: str, field: str
) -> None:
    (problem_root / relative_path).write_text(text, encoding="utf-8")
    with pytest.raises(ValueError) as failure:
        load_problem(problem_root)
    assert relative_path in str(failure.value)
    assert field in str(failure.value)


@pytest.mark.parametrize(
    "relative_path",
    [
        "spec.md",
        "charter/status.md",
        "charter/params.yaml",
        "charter/constraints.md",
        "charter/progress.md",
        "charter/sources.md",
        "herd.yaml",
    ],
)
def test_required_file_is_not_defaulted(problem_root: Path, relative_path: str) -> None:
    (problem_root / relative_path).unlink()
    with pytest.raises(ValueError, match=relative_path):
        load_problem(problem_root)


def test_optional_gate_egress_and_target_files(problem_root: Path) -> None:
    (problem_root / "charter/gates.yaml").unlink()
    (problem_root / "charter/egress.yaml").unlink()
    (problem_root / "charter/progress.md").write_text("# Progress\n")
    problem = load_problem(problem_root)
    assert problem.gates == ()
    assert problem.egress.allow == ()
    assert problem.target is None


def test_source_id_must_match_filename(problem_root: Path) -> None:
    source = problem_root / "eval/sources/slow.yaml"
    source.write_text(source.read_text().replace("id: slow", "id: different"))
    with pytest.raises(ValueError, match="slow.yaml.*id"):
        load_problem(problem_root)


@pytest.mark.parametrize(
    "judge",
    ["{}", "{command: run, adapter_class: package:Judge}"],
)
def test_judge_requires_one_execution_method(problem_root: Path, judge: str) -> None:
    config = problem_root / "herd.yaml"
    config.write_text(config.read_text().replace("{command: private/judge.py}", judge))
    with pytest.raises(ValueError, match=r"herd.yaml.*judges.slow"):
        load_problem(problem_root)


def test_public_view_excludes_private_state_and_unlisted_files(
    problem_root: Path,
) -> None:
    for relative_path in (
        ".env",
        ".git/config",
        ".herd/credentials",
        "charter/herd.yaml",
        "charter/notes.txt",
        "eval/judge.py",
        "map/measurements/wait/.env",
        "map/measurements/wait/private/hidden.json",
        "map/measurements/wait/herd.yaml",
    ):
        path = problem_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("PRIVATE_SENTINEL\n")
    view = public_view(problem_root)
    assert "eval/sources/slow.yaml" in view.files
    assert "map/measurements/wait/script.py" in view.files
    assert "charter/sources.md" in view.files
    assert all(
        "private" not in Path(path).parts and Path(path).name != "herd.yaml"
        for path in view.files
    )
    assert all(b"PRIVATE" not in content for content in view.files.values())


@pytest.mark.parametrize(
    "relative_path",
    [
        "spec.md",
        "charter",
        "charter/status.md",
        "approaches",
        "eval",
        "eval/sources",
        "map",
        "map/measurements",
        "map/measurements/wait/script.py",
        "herd.yaml",
    ],
)
def test_loader_rejects_linked_files_and_parent_directories(
    problem_root: Path, relative_path: str
) -> None:
    path = problem_root / relative_path
    original = path.rename(problem_root.parent / "original")
    path.symlink_to(original, target_is_directory=original.is_dir())
    with pytest.raises(ValueError, match="symbolic links"):
        load_problem(problem_root)


def test_invalid_text_encoding_names_file(problem_root: Path) -> None:
    (problem_root / "spec.md").write_bytes(b"\xff")
    with pytest.raises(ValueError, match="spec.md.*spec_markdown"):
        load_problem(problem_root)


@pytest.mark.parametrize(
    "relative_path", ["approaches", "eval/sources", "map/measurements"]
)
def test_public_directory_cannot_be_a_file(
    problem_root: Path, relative_path: str
) -> None:
    path = problem_root / relative_path
    path.rename(problem_root.parent / "original")
    path.write_text("not a directory\n")
    with pytest.raises(ValueError, match=f"{relative_path}.*directory"):
        load_problem(problem_root)


def test_init_sign_and_reload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = tmp_path / "target"
    repository.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    root = tmp_path / "new-problem"
    assert main(["init", str(root), "--repo", str(repository)]) == 0
    assert (root / "private").is_dir()
    assert tuple((root / "private").iterdir()) == ()
    assert (root / "charter/status.md").read_text() == "draft\n"
    assert main(["sign", str(root)]) == 2
    assert "charter/constraints.md" in capsys.readouterr().err
    assert (root / "charter/status.md").read_text() == "draft\n"
    (root / "charter/constraints.md").write_text(
        "```yaml limits\neval_machine_hours: 4\ndays: 2\n```\n"
    )
    assert main(["sign", str(root)]) == 0
    problem = load_problem(root)
    assert problem.status == "aligned"
    assert problem.approaches == ()
    assert problem.params.slots_per_approach == {}
    assert problem.herd_config.models == {}
    assert problem.herd_config.executor.settings["target_repository"] == str(repository)
    assert str(repository).encode() not in b"".join(problem.public_files.files.values())


def test_sign_validates_before_changing_status(problem_root: Path) -> None:
    status_path = problem_root / "charter/status.md"
    status_path.write_text("revising\n")
    (problem_root / "charter/sources.md").write_text("# No sources confirmed\n")
    assert main(["sign", str(problem_root)]) == 2
    assert status_path.read_text() == "revising\n"


def test_init_never_overwrites_existing_folder(problem_root: Path) -> None:
    before = (problem_root / "spec.md").read_bytes()
    assert (
        main(["init", str(problem_root), "--repo", str(problem_root.parent / "target")])
        == 2
    )
    assert (problem_root / "spec.md").read_bytes() == before


def test_init_rejects_non_repository_without_creating_folder(tmp_path: Path) -> None:
    root = tmp_path / "new"
    assert main(["init", str(root), "--repo", str(tmp_path)]) == 2
    assert not root.exists()


@pytest.mark.parametrize(
    "arguments", [["--help"], ["init", "--help"], ["sign", "--help"]]
)
def test_command_help(arguments: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(arguments) == 0
    assert "usage: vlah" in capsys.readouterr().out


def test_version_and_argument_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"vlah {__version__}"
    assert main(["init", "new"]) == 2
    assert "--repo" in capsys.readouterr().err
    assert main(["not-a-command"]) == 2
    assert "invalid choice" in capsys.readouterr().err
