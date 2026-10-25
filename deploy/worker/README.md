# The worker image

The image every container and pod executor starts a worker from (Wave L, #127).
It holds git, Python with the `vlah` package (the interrupt hook and the scripted
worker run inside the sandbox), Node with the Claude Code and GitHub Copilot CLIs,
and a non-root user `worker` whose home is `/work/.home`. `/work` is the sandbox the
executor mounts.

Build it from the repository root, so the package is in the build context:

```bash
docker build -t vlah-worker:dev -f deploy/worker/Dockerfile .
```

The repository's `.dockerignore` is an allowlist: only `pyproject.toml`, `README.md`,
`LICENSE` and `vlah/` reach the build, so no problem folder, `private/` or run state
can end up in a layer. The image carries no credential: workers get their tool
token and model gateway settings per session, in the environment. `tests/infra/test_worker_image.py`
checks both (`uv run pytest -m infra`, with Docker running).

The CLI versions are pinned as build arguments and bumped on purpose:

```bash
docker build -t vlah-worker:dev -f deploy/worker/Dockerfile \
  --build-arg CLAUDE_CODE_VERSION=2.1.310 --build-arg COPILOT_CLI_VERSION=1.0.103 .
```
