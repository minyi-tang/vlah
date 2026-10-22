# H11 spike: cross-machine checkpoint with sandbox state

Measures how long it takes to checkpoint a headless Claude Code worker on one machine and fork it on
another, sandbox included, and how big the checkpoint is. The findings are in
[docs/spikes/h11.md](../../docs/spikes/h11.md). This folder only reproduces them. It is not
production code and nothing in `vlah/` imports it.

## Run it

```bash
python3 spikes/checkpoint/run.py
```

Needs Docker (privileged containers, for the overlay mount) and network access to npm and PyPI. It
needs no API key: a scripted mock answers for the model. A run with the defaults takes about
10 minutes and writes `results/h11-<UTC time>.json`, then prints a Markdown table.

Options: `--artifact-megabytes` (sweep outputs in the sandbox, default 300), `--repetitions`
(default 3), `--transfer-megabytes-per-second` (the A-to-B link, default 100), `--keep-containers`.

## What happens

1. `Dockerfile` builds one image with Claude Code and the Agent SDK pinned. It plays machine A,
   machine B and the mock model API (`mock_anthropic.py`). A and B have separate volumes and talk
   only over a Docker network.
2. Machine A clones the target repo (this repo's `HEAD`, standing in for testbed problem A until it
   lands) and runs `worker_commands.sh`, the command log of one worker session: a uv venv with the
   simulator's dependencies, commits, a deleted file, staged and unstaged edits, untracked notes, and
   about 300 MB of sweep outputs from `make_artifacts.py`. The sandbox sits on an overlayfs mount,
   so the overlay approach has an upper directory to snapshot.
3. A first Claude Code session (`claude -p`) runs one tool call that reads a random nonce. Its output
   is large enough that Claude Code saves it to a side file next to the transcript.
4. For each approach and repetition, `machine.sh` checkpoints on A, B fetches the files over HTTP at
   the set rate, restores, and runs `claude -p --resume <id> --fork-session` with a new goal. The
   fork's first tool call stamps the time and writes the nonce the mock found in the history it
   received.
5. `run.py` checks each fork. The file manifest from `manifest.py` must match A's, the git state
   must match, the recalled nonce must be right, and the first model request must carry the earlier
   goal and its tool result.
6. It also checks that the usage in the transcript matches what the mock returned. Last, it repeats
   the fork through the Agent SDK with the transcript only in a `SessionStore` (`sdk_resume.py`).

## Approaches

| Name | Checkpoint | Restore on B |
|---|---|---|
| `tar_zstd` | tar plus zstd of the whole working directory | untar |
| `git_bundle` | git bundle of the new commits and a stash commit, plus a tar of untracked and ignored files | fetch, switch, apply the stash, untar (B has a clone of the base commit) |
| `overlay_upper` | tar plus zstd of the overlayfs upper directory | untar it, then mount it over the same base layer |
| `command_replay` | the command log | run the log on a clone of the base commit |
| `tar_without_venv` | tar plus zstd without `.venv`, plus `uv pip freeze` | untar, then rebuild the venv from the frozen list |

The conversation travels the same way for all of them: the transcript JSONL, the session's side-file
folder and the project memory folder, packed with zstd.
