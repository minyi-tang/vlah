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
