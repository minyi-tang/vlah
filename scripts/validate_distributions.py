"""Validate the actual build output, without rebuilding the files that will be promoted."""

import argparse
import json
import shutil
import sys
import tarfile
from email.parser import BytesParser
from pathlib import Path
from tempfile import TemporaryDirectory

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from scripts.distribution_checks import (
    check_testbed_distributions,
    check_vlah_distributions,
    run_command,
    source_files,
    wheel_files,
)
from scripts.installed_checks import (
    check_installed_testbed,
    check_installed_vlah,
    installed_testbed,
)
from scripts.release_plan import artifact_hashes, read_projects


def validate_distributions(directory: Path, repository_root: Path) -> dict[str, str]:
    if not __debug__:
        raise RuntimeError(
            "Distribution assertions require Python without optimization"
        )
    else:
        projects = read_projects(repository_root)
        filenames = {
            filename
            for project in projects.values()
            for filename in project.filenames()
        }
        validated_hashes = artifact_hashes(directory, filenames)
        with TemporaryDirectory(prefix="vlah-release-check-") as temporary:
            workdir = Path(temporary).resolve()
            if workdir.is_relative_to(repository_root):
                raise ValueError("Distribution checks must run outside the checkout")
            else:
                distributions: dict[str, dict[str, Path]] = {}
                for name, project in projects.items():
                    basename = f"{name.replace('-', '_')}-{project.version}"
                    wheel = directory / f"{basename}-py3-none-any.whl"
                    source = directory / f"{basename}.tar.gz"
                    source_contents = source_files(source)
                    wheel_contents = wheel_files(wheel)
                    for content in (
                        source_contents["PKG-INFO"],
                        wheel_contents[f"{basename}.dist-info/METADATA"],
                    ):
                        metadata = BytesParser().parsebytes(content)
                        if (
                            canonicalize_name(metadata["Name"]) != name
                            or metadata["Version"] != project.version
                        ):
                            raise ValueError(
                                f"Incorrect distribution metadata for {name}"
                            )
                        else:
                            for requirement in metadata.get_all("Requires-Dist", []):
                                if Requirement(requirement).url is not None:
                                    raise ValueError(
                                        "Published dependencies cannot be local URLs"
                                    )
                                else:
                                    pass
                    extracted = workdir / name / "source"
                    with tarfile.open(source) as archive:
                        archive.extractall(extracted, filter="data")
                    rebuilt_directory = workdir / name / "rebuilt"
                    run_command(
                        [
                            "uv",
                            "build",
                            "--no-sources",
                            "--wheel",
                            str(extracted / basename),
                            "--out-dir",
                            str(rebuilt_directory),
                        ],
                        workdir,
                    )
                    distributions[name] = {
                        "wheel": wheel,
                        "sdist": source,
                        "rebuilt": rebuilt_directory / wheel.name,
                    }
                check_vlah_distributions(
                    distributions["vlah"], repository_root, projects["vlah"].version
                )
                check_testbed_distributions(
                    distributions["vlah-testbed"],
                    repository_root,
                    projects["vlah-testbed"].version,
                )
                for artifact in ("wheel", "sdist", "rebuilt"):
                    installed_root = workdir / f"vlah-{artifact}"
                    installed_root.mkdir()
                    check_installed_vlah(
                        distributions["vlah"][artifact],
                        distributions["vlah-testbed"]["wheel"],
                        installed_root,
                        repository_root,
                    )
                for artifact in ("wheel", "rebuilt"):
                    installed_root = workdir / f"testbed-{artifact}"
                    installed_root.mkdir()
                    with installed_testbed(
                        distributions["vlah-testbed"][artifact],
                        installed_root,
                        repository_root,
                    ) as installed:
                        for check in ("cli", "paths", "herd"):
                            check_installed_testbed(installed, check, repository_root)
                        if sys.platform == "darwin" and shutil.which("sandbox-exec"):
                            check_installed_testbed(
                                installed, "sandbox", repository_root
                            )
                        else:
                            pass
        if artifact_hashes(directory, filenames) != validated_hashes:
            raise ValueError("Distributions changed while being validated")
        else:
            return validated_hashes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    arguments = parser.parse_args()
    validated_hashes = validate_distributions(
        arguments.dist.resolve(), Path(__file__).resolve().parents[1]
    )
    arguments.manifest.write_text(json.dumps(validated_hashes, sort_keys=True) + "\n")
    print(
        f"Validated {len(validated_hashes)} original distributions; checksums recorded."
    )


if __name__ == "__main__":
    main()
else:
    pass
