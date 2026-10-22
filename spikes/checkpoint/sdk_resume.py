"""Fork a Claude Code session through the Agent SDK, with the transcript kept in a directory-backed store.

export (source machine): copy a local session transcript into the store.
resume (target machine): fork the stored session in --cwd with a new prompt; print the result as JSON.
"""

import argparse
import asyncio
import json
from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    SessionKey,
    SessionStoreEntry,
    import_session_to_store,
    query,
)


class DirectorySessionStore:
    """Keeps each transcript as one JSONL file: ROOT/PROJECT_KEY/SESSION_ID[/SUBPATH].jsonl."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path_for(self, key: SessionKey) -> Path:
        subpath = key.get("subpath")
        file_stem = f"{key['session_id']}/{subpath}" if subpath else key["session_id"]
        return self.root / key["project_key"] / f"{file_stem}.jsonl"

    async def append(self, key: SessionKey, entries: list[SessionStoreEntry]) -> None:
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as transcript_file:
            transcript_file.writelines(json.dumps(entry) + "\n" for entry in entries)

    async def load(self, key: SessionKey) -> list[SessionStoreEntry] | None:
        path = self.path_for(key)
        if not path.exists():
            return None
        else:
            return [json.loads(line) for line in path.read_text().splitlines() if line]


async def fork_from_store(
    store: DirectorySessionStore, session_id: str, working_directory: Path, prompt: str
) -> None:
    options = ClaudeAgentOptions(
        session_store=store,
        resume=session_id,
        fork_session=True,
        cwd=working_directory,
        allowed_tools=["Bash"],
    )
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            print(
                json.dumps(
                    {
                        "session_id": message.session_id,
                        "is_error": message.is_error,
                        "result": message.result,
                    }
                )
            )
        else:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["export", "resume"])
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--prompt", default="")
    parsed = parser.parse_args()
    store = DirectorySessionStore(parsed.store)
    if parsed.mode == "export":
        asyncio.run(
            import_session_to_store(parsed.session_id, store, directory=str(parsed.cwd))
        )
    else:
        asyncio.run(
            fork_from_store(store, parsed.session_id, parsed.cwd, parsed.prompt)
        )


if __name__ == "__main__":
    main()
