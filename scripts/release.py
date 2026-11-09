"""Plan, stage, dry-run, or promote one validated release bundle."""

import argparse
import hashlib
import os
import shutil
from pathlib import Path

from pydantic import TypeAdapter

from scripts.release_plan import (
    ReleaseManifest,
    ReleasePlan,
    Sha256,
    artifact_hashes,
    plan_release,
    read_bundle,
    read_projects,
)
from scripts.release_publish import (
    check_dependency,
    check_registry,
    publish_github,
    publish_pypi,
)


def prepare_bundle(
    repository_root: Path,
    directory: Path,
    validation_manifest: Path,
    plan: ReleasePlan,
    output: Path,
) -> ReleaseManifest:
    projects = read_projects(repository_root)
    filenames = {
        filename for project in projects.values() for filename in project.filenames()
    }
    validated = TypeAdapter(dict[str, Sha256]).validate_json(
        validation_manifest.read_bytes()
    )
    if artifact_hashes(directory, filenames) != validated:
        raise ValueError("Build output differs from the distribution validation report")
    elif plan.versions != {name: project.version for name, project in projects.items()}:
        raise ValueError("Project versions changed after release planning")
    else:
        selected = {name: validated[name] for name in sorted(plan.filenames())}
        dependencies = (
            {
                name: validated[name]
                for name in sorted(projects["vlah-testbed"].filenames())
            }
            if plan.package == "vlah"
            else {}
        )
        manifest = ReleaseManifest(
            **plan.model_dump(), files=selected, dependency_files=dependencies
        )
        output.mkdir()
        for filename in sorted(selected):
            shutil.copyfile(directory / filename, output / filename)
        content = manifest.model_dump_json(indent=2).encode() + b"\n"
        (output / "release.json").write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        read_bundle(output, digest)
        return manifest


def workflow_plan(repository_root: Path) -> ReleasePlan:
    return plan_release(
        repository_root,
        os.environ["GITHUB_EVENT_NAME"],
        os.environ["GITHUB_REF"],
        os.environ["GITHUB_REPOSITORY"],
        os.environ["GITHUB_SHA"],
        os.environ.get("RELEASE_PUBLISHING_ENABLED", ""),
        os.environ["RELEASE_ENVIRONMENT"],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("--output", type=Path, required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--plan", type=Path, required=True)
    prepare_parser.add_argument("--dist", type=Path, required=True)
    prepare_parser.add_argument("--validation-manifest", type=Path, required=True)
    prepare_parser.add_argument("--output", type=Path, required=True)
    for command in ("dry-run", "publish-pypi", "publish-github"):
        command_parser = commands.add_parser(command)
        command_parser.add_argument("--bundle", type=Path, required=True)
        command_parser.add_argument("--manifest-sha256", required=True)
    arguments = parser.parse_args()
    repository_root = Path(__file__).resolve().parents[1]
    if arguments.command == "plan":
        plan = workflow_plan(repository_root)
        arguments.output.write_text(plan.model_dump_json(indent=2) + "\n")
        print(
            "Validated tag release plan."
            if plan.tag
            else "Build-only plan; publishing is disabled."
        )
    elif arguments.command == "prepare":
        plan = ReleasePlan.model_validate_json(arguments.plan.read_bytes())
        manifest = prepare_bundle(
            repository_root,
            arguments.dist.resolve(),
            arguments.validation_manifest,
            plan,
            arguments.output.resolve(),
        )
        if plan.publish:
            check_dependency(manifest, os.environ["PYPI_JSON_URL"])
        else:
            pass
        digest = hashlib.sha256(
            (arguments.output / "release.json").read_bytes()
        ).hexdigest()
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as workflow_output:
            workflow_output.write(
                f"manifest-sha256={digest}\npublish={str(plan.publish).lower()}\n"
            )
        print(
            f"Prepared {len(manifest.files)} distributions; manifest SHA-256: {digest}"
        )
    else:
        directory = arguments.bundle.resolve()
        manifest = read_bundle(directory, arguments.manifest_sha256)
        if arguments.command != "dry-run":
            current_plan = workflow_plan(repository_root)
            if not current_plan.publish or current_plan != ReleasePlan.model_validate(
                manifest.model_dump(exclude={"files", "dependency_files"})
            ):
                raise ValueError(
                    "Publishing requires the same enabled, protected tag-push plan"
                )
            else:
                pass
        else:
            pass
        if arguments.command == "publish-github":
            if manifest.package is None:
                raise ValueError("GitHub publication requires a selected package")
            else:
                project = read_projects(repository_root)[manifest.package]
                check_registry(
                    project,
                    manifest.files,
                    os.environ["PYPI_JSON_URL"],
                    require_complete=True,
                )
                publish_github(directory, manifest, repository_root)
        else:
            publish_pypi(
                directory,
                manifest,
                os.environ["PYPI_INDEX_URL"],
                os.environ["PYPI_PUBLISH_URL"],
                os.environ["PYPI_JSON_URL"],
                dry_run=arguments.command == "dry-run",
            )


if __name__ == "__main__":
    main()
else:
    pass
