# The worker image

The image every container and pod executor starts a worker from (Wave L, #127).
It holds git, Python with the `vlah` package (the interrupt hook and the scripted
worker run inside the sandbox), Node with the Claude Code and GitHub Copilot CLIs,
and a non-root user `worker` whose home is `/work/.home`. `/work` is the sandbox the
executor mounts.

Build it from the repository root. No prior wheel build or PyPI publication is
needed; use a task-specific tag on a shared Docker host:

```bash
docker build -t vlah-worker:package-vlah -f deploy/worker/Dockerfile .
```

The `packages` stage builds both local distributions, then installs only VLAH
with normal dependency resolution. A constraint selects the locally built
`vlah-testbed` wheel even if a newer compatible version exists on an index.
Third-party dependencies still come from the configured package index, and
resolution failures stop the build.

The full testbed wheel is an operator distribution, not a worker problem pack.
Before copying the installed environment into the final `worker` stage, the
build removes its bundled `testbed/problems` catalog, including problem
definitions, policies, maps and seeds. The shared integrator and dependency
metadata remain installed. Built-in `tb` problems are intentionally unavailable
in this image; use the normal testbed distribution on the operator side.

The repository's `.dockerignore` allows the VLAH package and explicitly listed
public testbed build inputs. Keep that list aligned with
`testbed/pyproject.toml` and `testbed/hatch_build.py`; missing required inputs fail
the build. Private/hidden material, run state, environment files and key files
are excluded. The final image has neither source directories nor wheel/build
inputs, and never inherits a layer from the `packages` stage.

The image carries no credential: workers get their tool token and model gateway
settings per session, in the environment. With Docker running,
`uv run pytest -m infra tests/infra/test_worker_image.py` builds an isolated,
uniquely tagged image, checks dependencies and the installed CLI as well as the
existing information/non-root/tooling boundaries, then removes its test images.

The CLI versions are pinned as build arguments and bumped on purpose:

```bash
docker build -t vlah-worker:package-vlah -f deploy/worker/Dockerfile \
  --build-arg CLAUDE_CODE_VERSION=2.1.310 --build-arg COPILOT_CLI_VERSION=1.0.103 .
```
