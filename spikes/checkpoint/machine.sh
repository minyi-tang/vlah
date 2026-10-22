#!/usr/bin/env bash
# Every step a machine container runs in the H11 spike. Runs as root; worker steps drop to "node".
# Usage: machine.sh STEP [APPROACH]. Timed steps print key=value lines with epoch seconds.
set -euo pipefail

STEP=$1
APPROACH=${2:-}
PROJECT_DIRECTORY=.claude/projects/-work-repo
OUTGOING=/vol/ckpt/$APPROACH
INCOMING=/vol/incoming/$APPROACH
OVERLAY_OPTIONS=lowerdir=/vol/base/repo,upperdir=/vol/upper,workdir=/vol/overlay-work

as_worker() {
    runuser -u node -- env HOME=/home/node "$@"
}

now() {
    date +%s.%N
}

report_fork() {
    # Print what the fork's first tool call left behind: its time, the nonce it recalled, its session id.
    echo "first_tool_call_at=$(cat /tmp/h11_first_tool_call)"
    echo "recalled_nonce=$(cat /tmp/h11_recalled_nonce)"
    echo "fork_session_id=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["session_id"])' "$1")"
    spill_directory=/home/node/$PROJECT_DIRECTORY/$SESSION_ID
    if [ -d "$spill_directory" ]; then
        echo "spilled_tool_results=$(find "$spill_directory" -type f | wc -l)"
    else
        echo "spilled_tool_results=0"
    fi
}

list_outgoing() {
    for file_path in "$OUTGOING"/*; do echo "file:$(basename "$file_path")=$(stat -c %s "$file_path")"; done
}

case "$STEP" in
setup-source)
    mkdir -p /vol/base /vol/upper /vol/overlay-work /vol/ckpt /work/repo
    tar -C /vol/base -xf /remote/base.tar
    chown node:node /vol/upper /vol/ckpt
    mount -t overlay overlay -o "$OVERLAY_OPTIONS" /work/repo
    ;;
setup-target)
    mkdir -p /vol/base /work/repo
    tar -C /vol/base -xf /remote/base.tar
    ;;
session)
    # Machine A: build the sandbox from the command log, then run the first Claude Code session.
    echo "sandbox_build_started_at=$(now)"
    as_worker bash /vol/command_log.sh
    echo "sandbox_build_finished_at=$(now)"
    echo "sandbox_bytes=$(du -sb /work/repo | cut -f1)"
    echo "sandbox_without_venv_bytes=$(du -sb --exclude=.venv /work/repo | cut -f1)"
    cd /work/repo
    as_worker claude -p --session-id "$SESSION_ID" --allowedTools Bash --output-format json "$FIRST_PROMPT" \
        > /vol/first_session.json
    ;;
reset)
    # Machine B: forget the previous fork, then lay down what this approach expects to find already.
    if mountpoint -q /work/repo; then umount /work/repo; fi
    rm -rf /vol/plain /vol/upper /vol/overlay-work /vol/incoming /home/node/.claude/projects \
        /home/node/.cache/uv /tmp/h11_*
    mkdir -p /vol/plain/repo /vol/upper /vol/overlay-work
    case "$APPROACH" in
    git_bundle | command_replay) cp -a /vol/base/repo/. /vol/plain/repo/ ;;
    *) ;;
    esac
    chown node:node /vol/plain/repo
    if [ "$APPROACH" != overlay_upper ]; then mount --bind /vol/plain/repo /work/repo; fi
    ;;
checkpoint)
    # Machine A: the conversation travels the same way for every approach; the sandbox does not.
    rm -rf "$OUTGOING"
    mkdir -p "$OUTGOING"
    chown node:node "$OUTGOING"
    echo "checkpoint_started_at=$(now)"
    conversation_paths=("$PROJECT_DIRECTORY/$SESSION_ID.jsonl")
    for optional in "$PROJECT_DIRECTORY/$SESSION_ID" "$PROJECT_DIRECTORY/memory"; do
        if [ -e "/home/node/$optional" ]; then conversation_paths+=("$optional"); fi
    done
    tar -C /home/node -cf - "${conversation_paths[@]}" | zstd -q -T0 -o "$OUTGOING/conversation.tar.zst"
    case "$APPROACH" in
    tar_zstd)
        tar -C /work -cf - repo | zstd -q -T0 -3 -o "$OUTGOING/sandbox.tar.zst"
        ;;
    git_bundle)
        cd /work/repo
        stash_commit=$(as_worker git stash create)
        as_worker git update-ref refs/h11/stash "$stash_commit"
        as_worker git bundle create -q "$OUTGOING/repo.bundle" "refs/heads/$WORKER_BRANCH" refs/h11/stash "^$BASE_COMMIT"
        { printf '.git/config\0'; as_worker git ls-files --others -z; } \
            | tar -C /work/repo --null -T - -cf - | zstd -q -T0 -3 -o "$OUTGOING/untracked.tar.zst"
        ;;
    overlay_upper)
        tar -C /vol/upper --xattrs --xattrs-include='trusted.*' -cf - . | zstd -q -T0 -3 -o "$OUTGOING/upper.tar.zst"
        ;;
    command_replay)
        cp /vol/command_log.sh "$OUTGOING/command_log.sh"
        ;;
    tar_without_venv)
        as_worker uv pip freeze --python /work/repo/.venv/bin/python > "$OUTGOING/requirements.lock"
        tar -C /work --exclude=repo/.venv -cf - repo | zstd -q -T0 -3 -o "$OUTGOING/sandbox.tar.zst"
        ;;
    *)
        echo "unknown approach: $APPROACH" >&2
        exit 2
        ;;
    esac
    echo "checkpoint_finished_at=$(now)"
    list_outgoing
    ;;
fetch)
    # Machine B: pull the checkpoint over the network at a fixed rate, one file after another.
    mkdir -p "$INCOMING"
    echo "fetch_started_at=$(now)"
    for file_name in $CHECKPOINT_FILES; do
        curl -sSf --limit-rate "$RATE_BYTES_PER_SECOND" -o "$INCOMING/$file_name" \
            "http://$SOURCE_HOST:8000/$APPROACH/$file_name"
    done
    echo "fetch_finished_at=$(now)"
    ;;
restore)
    echo "restore_started_at=$(now)"
    zstd -q -dc "$INCOMING/conversation.tar.zst" | tar -C /home/node -xf -
    case "$APPROACH" in
    tar_zstd)
        zstd -q -dc "$INCOMING/sandbox.tar.zst" | tar -C /work -xf -
        ;;
    git_bundle)
        cd /work/repo
        as_worker git fetch -q "$INCOMING/repo.bundle" \
            "refs/heads/$WORKER_BRANCH:refs/heads/$WORKER_BRANCH" "refs/h11/stash:refs/h11/stash"
        as_worker git switch -q "$WORKER_BRANCH"
        as_worker git stash apply -q --index refs/h11/stash
        zstd -q -dc "$INCOMING/untracked.tar.zst" | tar -C /work/repo -xf -
        ;;
    overlay_upper)
        zstd -q -dc "$INCOMING/upper.tar.zst" | tar -C /vol/upper --xattrs --xattrs-include='trusted.*' -xf -
        mount -t overlay overlay -o "$OVERLAY_OPTIONS" /work/repo
        ;;
    command_replay)
        as_worker bash "$INCOMING/command_log.sh"
        ;;
    tar_without_venv)
        zstd -q -dc "$INCOMING/sandbox.tar.zst" | tar -C /work -xf -
        as_worker uv venv -q --python /usr/bin/python3 /work/repo/.venv
        as_worker uv pip install -q --python /work/repo/.venv/bin/python -r "$INCOMING/requirements.lock"
        ;;
    *)
        echo "unknown approach: $APPROACH" >&2
        exit 2
        ;;
    esac
    echo "restore_finished_at=$(now)"
    ;;
resume)
    # Machine B: fork the session; its first tool call stamps the time and writes the nonce it recalls.
    cd /work/repo
    echo "resume_started_at=$(now)"
    as_worker claude -p --resume "$SESSION_ID" --fork-session --allowedTools Bash --output-format json \
        "$FORK_PROMPT" > /tmp/h11_resume.json
    report_fork /tmp/h11_resume.json
    ;;
sdk-export)
    # Machine A: copy the session into a directory-backed Agent SDK SessionStore and pack it.
    rm -rf "$OUTGOING"
    mkdir -p "$OUTGOING"
    chown node:node "$OUTGOING"
    echo "checkpoint_started_at=$(now)"
    as_worker /opt/sdk/bin/python /opt/h11/sdk_resume.py export --store "$OUTGOING/store" \
        --session-id "$SESSION_ID" --cwd /work/repo
    tar -C "$OUTGOING" -cf - store | zstd -q -T0 -o "$OUTGOING/store.tar.zst"
    rm -rf "$OUTGOING/store"
    echo "checkpoint_finished_at=$(now)"
    list_outgoing
    ;;
sdk-resume)
    # Machine B: no local transcript at all; the SDK materializes the session from the store.
    rm -rf /home/node/.claude/projects /tmp/h11_* /vol/sdk
    mkdir -p /vol/sdk
    zstd -q -dc "$INCOMING/store.tar.zst" | tar -C /vol/sdk -xf -
    chown -R node:node /vol/sdk
    cd /work/repo
    echo "resume_started_at=$(now)"
    as_worker /opt/sdk/bin/python /opt/h11/sdk_resume.py resume --store /vol/sdk/store \
        --session-id "$SESSION_ID" --cwd /work/repo --prompt "$FORK_PROMPT" > /tmp/h11_sdk_resume.json
    report_fork /tmp/h11_sdk_resume.json
    ;;
*)
    echo "unknown step: $STEP" >&2
    exit 2
    ;;
esac
