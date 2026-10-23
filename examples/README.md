# Prepare a problem folder

The first slice of [W1-13](https://github.com/minyi-tang/vlah/issues/23) implements
problem loading and charter setup. It does not start workers or spend judge runs.
The remaining run, demo, inspection and human-channel commands are still tracked
in that issue.

## Create and sign

```bash
uv run vlah init my-problem --repo /path/to/your/git-working-tree
```

This creates a draft charter, empty `approaches/`, `eval/sources/` and `private/`
directories, an initial map, and private deployment settings in `herd.yaml`.
It does not copy or modify the target repository.

Before signing:

- Replace both `null` values in the `yaml limits` block in
  `charter/constraints.md` with your approved machine-hour and day limits.
  No budget, model, approach, slot count or preregistration number is chosen for you.
- Write the charter and `spec.md`, add initial approaches with `status` and
  optional `parent` frontmatter, and set fixed `slots_per_approach` in `params.yaml`.
- Declare judges in `eval/sources/<id>.yaml`. In `charter/sources.md`, list each
  exact source id and confirm its `trust` and `readable_by` values.
- Configure matching judges in `herd.yaml`, each with exactly one `command` or
  `adapter_class`. Choose your harness and model there before an eventual run.

```bash
uv run vlah sign my-problem
```

Signing validates the folder first and changes only `charter/status.md` to
`aligned`. An invalid file leaves the status unchanged. The generated draft
cannot be signed until its limits are filled in. Signing records charter
approval, not deployment readiness: it does not execute judges, check installed
adapters, require a nonempty approach portfolio, or replace the harness placeholder.

## Load from Python

```python
from pathlib import Path

from vlah.core.problem.loader import load_problem

problem = load_problem(Path("my-problem"))
public_files = problem.public_files
```

The loader implements the frozen `LoadProblem` contract. It parses schemas,
checks source and approach references, and reports failures with the filename
and field. Missing `gates.yaml` means no gates; missing `egress.yaml` means no
additional allowed hosts. Present but malformed files are rejected. A target
block in `progress.md` is optional; a limits block in `constraints.md` is required.

The public snapshot contains only the documented charter files, `spec.md`,
`approaches/*.md`, `eval/sources/*.yaml`, `map/gap.md` and measurement files.
It excludes `herd.yaml`, `private/`, repository metadata, run state and unrelated
files. Measurement trees omit hidden entries, `private/`, `herd.yaml` and Python
cache files. Public files and their parent directories cannot be symbolic links.
Keep secrets and judge definitions outside these public paths.
