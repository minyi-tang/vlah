"""The start pack for a session that replaces one near its context limit (D77).

Not a fork: a fork copies the whole conversation, so the child would start at the
limit. The new session gets five parts instead:
1. the original start pack;
2. the claims the worker wrote for this task;
3. the interrupt content, if any;
4. the working-directory diff;
5. the last restart_tool_call_count tool calls.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path

from vlah.core.types.interrupts import Interrupt
from vlah.ports.executor import StartPack

CONTEXT_FILE = "CONTEXT.md"
GIT_TIMEOUT_SECONDS = 60.0
RESTART_INTRO = (
    "Your previous session for this task came close to the end of its context "
    "window, so the herd started this fresh one. Below are the claims you wrote, "
    "the state of your working directory and your last tool calls. Continue the "
    "task from here."
)


@dataclass(frozen=True, kw_only=True)
class RestartParts:
    claims: tuple[str, ...]
    interrupt: Interrupt | None
    working_diff: str
    tool_calls: tuple[str, ...]


def working_directory_diff(target: Path, target_ref: str) -> str:
    """Changes since the baseline ref, committed or not, and the new files' names."""
    if not target.is_dir():
        return "There is no working directory yet."
    else:
        diff = subprocess.run(
            ["git", "-C", str(target), "diff", target_ref],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        ).stdout
        untracked = subprocess.run(
            ["git", "-C", str(target), "ls-files", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        ).stdout.split()
        new_files = "\n".join(f"new file: {path}" for path in untracked)
        return "\n".join(part for part in (diff.strip(), new_files) if part) or (
            "No changes."
        )


def section(heading: str, body: str) -> str:
    return f"## {heading}\n\n{body.strip()}\n"


def restart_sections(parts: RestartParts) -> tuple[str, ...]:
    sections = [
        section("Context restart", RESTART_INTRO),
        section(
            "Your claims from this task",
            "\n\n".join(parts.claims) or "You have not written a claim yet.",
        ),
    ]
    if parts.interrupt is not None:
        sections.append(
            section(
                "Interrupt",
                f"{parts.interrupt.kind} interrupt {parts.interrupt.id} "
                f"(rule {parts.interrupt.rule}): {parts.interrupt.reason}",
            )
        )
    else:
        pass
    sections.append(
        section("Working directory diff", f"```diff\n{parts.working_diff}\n```")
    )
    sections.append(
        section(
            "Your last tool calls",
            "\n".join(f"- {call}" for call in parts.tool_calls) or "None.",
        )
    )
    return tuple(sections)


def restart_pack(original: StartPack, parts: RestartParts) -> StartPack:
    context = original.files.get(CONTEXT_FILE, "").rstrip()
    restarted = "\n\n".join([context, *restart_sections(parts)]).lstrip()
    return StartPack(
        files={**original.files, CONTEXT_FILE: restarted},
        interrupt=parts.interrupt or original.interrupt,
    )
