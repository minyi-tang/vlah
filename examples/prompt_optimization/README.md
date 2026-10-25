# Prompt optimization, end to end on one machine

A small problem that runs the whole herd without the testbed: workers (GitHub
Copilot sessions) improve the system prompt of a support assistant, a hidden
rubric judge scores every candidate, and the Integrator stacks the improvements
into a deliverable. It is a toy problem: the judge is a deterministic rubric over a
hidden set of situations, so a run costs model calls only for the workers.

## Requirements

- The repo installed with `uv sync --all-packages`.
- The GitHub Copilot CLI (`copilot --version`), signed in with your account. Every
  worker runs on the model named in `herd.yaml` (`models.worker`).
- macOS with `sandbox-exec` (every macOS has it) for OS-level egress enforcement.
  Elsewhere, or if the sandbox gets in the way, pass `--unsafe-no-sandbox`.

## Run it

```bash
uv run python examples/prompt_optimization/setup.py ~/vlah-runs/prompt-opt
```

This copies the problem folder to `~/vlah-runs/prompt-opt`, creates the target
repository (`prompt.md` on `main`) next to it, and points `herd.yaml` at it.
Then:

```bash
uv run vlah run ~/vlah-runs/prompt-opt --arm S2 --workers 2 --hours 0.5
```

Watch `~/vlah-runs/prompt-opt/.herd/`: `runs/<run id>/` holds the shared record
(`blackboard.git`), every judge call (`judges/`) and the Integrator's baselines;
`logs/<run id>/` every worker session's output; `sandboxes/<run id>/` the workers'
working directories. Ctrl-C stops the run cleanly; the Integrator closes out and
`runs/<run id>/report.json` names the deliverable baseline and its merged candidates.

Try the arms: `--arm B1` gives every worker its own view (no sharing), `--arm S2`
shares everything. The judge, the quota and the Integrator are the same in both.

## A model-graded judge and Claude Code workers

The phrase rubric is free and deterministic but saturates near 0.75, because it
looks for exact words. `--judge llm` replaces it with a model-graded judge: for
each hidden situation a small assistant model (`claude-haiku-5-5`) answers the
customer with the candidate prompt as its only instructions, and a grader
(`claude-sonnet-5-5`) scores the reply against the policy handbook in `spec.md`.
The starting prompt scores about 0.36, so there is room to improve and to
combine. A score moves by about 0.02 between two runs of the same prompt.

The judge reaches the models through an Anthropic Messages API: by default a
LiteLLM gateway on `http://127.0.0.1:4000` whose key it reads from
`~/litellm-copilot/.env` (`LITELLM_MASTER_KEY`), or any gateway in
`VLAH_JUDGE_BASE_URL` and `VLAH_JUDGE_API_KEY`.

With the same gateway in front of GitHub Copilot, the workers can be Claude Code
sessions. `--claude-command` names the CLI or a wrapper that points it at the
gateway, `--claude-bin` the `claude` binary the wrapper runs, and
`--model-api-url` the gateway, which the worker sandbox is then allowed to reach:

```bash
uv run python examples/prompt_optimization/setup.py ~/vlah-runs/prompt-opt-claude \
  --judge llm --harness claude_code --effort low --slots 1 \
  --claude-command ~/litellm-copilot/claude-ghcp \
  --claude-bin "$HOME/Library/Application Support/Claude/claude-code/<version>/<id>/claude.app/Contents/MacOS/claude" \
  --model-api-url http://127.0.0.1:4000
uv run vlah run ~/vlah-runs/prompt-opt-claude --arm S2 --workers 4 --hours 0.5
```

Every worker runs on `claude-opus-5-5` unless `--model` says otherwise. Each
session gets a private Claude Code config directory, so your own settings, MCP
servers and memory never load.

## Workers in containers

With `executor.kind: docker` every worker session runs in its own container of the
worker image, on a Docker network with no route out; the only way to the model
gateway, the tool server and the judge is the run's egress proxy container, which
enforces the same allowlist as on the host (D75). The container sees nothing but its
sandbox, mounted at `/work`. Build the image once from the repository root, then
point `herd.yaml` at it; the paths in `harness.settings` are paths inside the image,
and `host.docker.internal` is how a container reaches this machine:

```bash
docker build -t vlah-worker:dev -f deploy/worker/Dockerfile .
```

```yaml
executor:
  kind: docker
  settings:
    target_repository: ~/vlah-runs/prompt-opt-docker/target
    target_ref: main
    image: vlah-worker:dev
    resources: {cpus: 2, memory: 2g}
    mounts_read_only:
      ~/litellm-copilot/claude-ghcp: /opt/gateway/claude-ghcp
      ~/litellm-copilot/.env: /opt/gateway/.env
harness:
  kind: claude_code
  settings:
    command: [/opt/gateway/claude-ghcp]
    model_api_urls: [http://host.docker.internal:4000]
    effort: low
    environment: {LITELLM_URL: http://host.docker.internal:4000}
```

`mounts_read_only` binds single files read-only into every worker container (never
anything under the problem folder or the run directory); here the gateway wrapper and
the file it reads its key from. Then run as before:

```bash
uv run vlah run ~/vlah-runs/prompt-opt-docker --arm S2 --workers 2 --hours 0.5
```

`docker ps --filter label=vlah.run=<run id>` lists a run's containers; the run
removes them when it ends, and `logs/<run id>/<agent>-denied.tsv` holds every
connection the proxy refused. `--unsafe-no-sandbox` puts the workers on a network
with a route out, for development only.

## Change the problem

- `target/prompt.md` is the thing being optimized. Replace it with your own prompt.
- `private/cases.json` (from `judge/cases.json` here; `private/` is never committed)
  is the hidden rubric: situations the prompt should prepare the assistant for, split
  into a daily set and an audit set (the Integrator's spot checks compare the two,
  D72). Workers never see this file.
- `private/judge.py` (from `judge/judge.py`) is the judge command. It gets the
  candidate directory and the dataset and prints one JSON line with `score`,
  `per_seed` and `constraint_ok`. To grade with a model instead of a rubric, replace
  it with a script that calls your model and prints the same JSON; `herd.yaml` only
  names the command.
- `charter/` says what the run may spend and `approaches/` what directions the
  workers take; `charter/params.yaml` sets the slots per approach.
