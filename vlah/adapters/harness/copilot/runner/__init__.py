"""Runs Copilot CLI with a private home and mirrors its session events to VLAH_TRANSCRIPT.

- COPILOT_HOME is the sandbox's .vlah/copilot, where the harness wrote the hooks
  and the herd's MCP server, so a worker never loads the host user's MCP servers,
  skills, plugins or memory.
- The session id is derived from the transcript path, so the events file is known
  before Copilot starts: $COPILOT_HOME/session-state/<id>/events.jsonl. It is copied
  to VLAH_TRANSCRIPT as it grows, for the worker loop, the hook and the trace.
- stdout passes through to the executor's log. The exit code is Copilot's.
"""

import subprocess
import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

HOME_DIRECTORY = ".vlah/copilot"
MIRROR_INTERVAL_SECONDS = 0.5


def session_id_for(transcript: Path) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, transcript.resolve().as_uri()))


@dataclass(kw_only=True)
class EventMirror:
    """Appends whatever the events file gained since the last copy."""

    source: Path
    destination: Path
    copied_bytes: int = 0
    stopping: threading.Event = field(default_factory=threading.Event)

    def copy_new(self) -> None:
        if not self.source.is_file():
            return
        else:
            with self.source.open("rb") as source:
                source.seek(self.copied_bytes)
                added = source.read()
            if added:
                with self.destination.open("ab") as destination:
                    destination.write(added)
                self.copied_bytes += len(added)
            else:
                pass

    def run(self) -> None:
        while not self.stopping.wait(MIRROR_INTERVAL_SECONDS):
            self.copy_new()


def main(
    command: Sequence[str], environment: Mapping[str, str], working_directory: Path
) -> int:
    transcript = Path(environment["VLAH_TRANSCRIPT"])
    transcript.parent.mkdir(parents=True, exist_ok=True)
    home = (working_directory / HOME_DIRECTORY).resolve()
    session_id = session_id_for(transcript)
    mirror = EventMirror(
        source=home / "session-state" / session_id / "events.jsonl",
        destination=transcript,
    )
    thread = threading.Thread(target=mirror.run, daemon=True)
    thread.start()
    try:
        completed = subprocess.run(
            [*command, "--session-id", session_id],
            env={**environment, "COPILOT_HOME": str(home)},
            check=False,
        )
    finally:
        mirror.stopping.set()
        thread.join()
        mirror.copy_new()
    return completed.returncode
