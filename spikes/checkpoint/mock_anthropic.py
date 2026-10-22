"""Scripted stand-in for the Anthropic Messages API, so Claude Code runs without a real model.

A user prompt containing [[bash:COMMAND]] gets one Bash tool call running COMMAND. The
placeholder {recalled_nonce} in COMMAND is filled from earlier tool results in the
conversation the client sent, so the reply proves the resumed history reached the model.
Every request is appended to a JSONL log with the usage returned for it.
"""

import argparse
import json
import re
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

COMMAND_MARKER = re.compile(r"\[\[bash:(.*?)\]\]", re.DOTALL)
NONCE_PATTERN = re.compile(r"NONCE-[0-9a-f]{32}")
CHARACTERS_PER_TOKEN = 4
UNCACHED_SHARE_DENOMINATOR = 5
MODEL_NAME = "mock-model"


@dataclass(kw_only=True)
class ScriptedReply:
    kind: str
    text: str
    bash_command: str


def blocks_of(message: object) -> list[dict[str, object]]:
    """The content blocks of one wire message; a plain string becomes one text block."""
    if not isinstance(message, dict):
        return []
    else:
        content = message.get("content")
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
        elif isinstance(content, list):
            return [block for block in content if isinstance(block, dict)]
        else:
            return []


def text_of_block(block: dict[str, object]) -> str:
    if block.get("type") == "text":
        return str(block.get("text", ""))
    elif block.get("type") == "tool_result":
        return "".join(text_of_block(inner) for inner in blocks_of(block))
    else:
        return ""


def recalled_nonce(earlier_messages: list[object]) -> str:
    """The last nonce that appeared in a tool result before the current prompt."""
    found = "NOT-FOUND"
    for message in earlier_messages:
        for block in blocks_of(message):
            if block.get("type") == "tool_result":
                matches = NONCE_PATTERN.findall(text_of_block(block))
                found = matches[-1] if matches else found
            else:
                pass
    return found


def choose_reply(request_body: dict[str, object]) -> ScriptedReply:
    messages = request_body.get("messages")
    if not isinstance(messages, list) or not request_body.get("tools"):
        return ScriptedReply(kind="side_query", text="ok", bash_command="")
    else:
        user_indexes = [
            i
            for i, message in enumerate(messages)
            if isinstance(message, dict) and message.get("role") == "user"
        ]
        last_user_index = user_indexes[-1] if user_indexes else len(messages)
        last_blocks = blocks_of(messages[last_user_index]) if user_indexes else []
        if any(block.get("type") == "tool_result" for block in last_blocks):
            return ScriptedReply(kind="after_tool", text="Done.", bash_command="")
        else:
            prompt_text = "\n".join(text_of_block(block) for block in last_blocks)
            markers = COMMAND_MARKER.findall(prompt_text)
            if not markers:
                return ScriptedReply(
                    kind="no_marker", text="No command requested.", bash_command=""
                )
            else:
                nonce = recalled_nonce(messages[:last_user_index])
                return ScriptedReply(
                    kind="tool_use",
                    text="",
                    bash_command=markers[-1].replace("{recalled_nonce}", nonce),
                )


def usage_for(request_body: dict[str, object]) -> dict[str, int]:
    """Deterministic token counts sized like the request, most of it as a cache read."""
    estimated_tokens = max(1, len(json.dumps(request_body)) // CHARACTERS_PER_TOKEN)
    uncached_tokens = max(1, estimated_tokens // UNCACHED_SHARE_DENOMINATOR)
    return {
        "input_tokens": uncached_tokens,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": estimated_tokens - uncached_tokens,
        "output_tokens": 0,
    }


def content_blocks(
    reply: ScriptedReply, message_number: int
) -> list[dict[str, object]]:
    if reply.kind == "tool_use":
        tool_input = {
            "command": reply.bash_command,
            "description": "Run the requested command",
        }
        return [
            {
                "type": "tool_use",
                "id": f"toolu_mock_{message_number}",
                "name": "Bash",
                "input": tool_input,
            }
        ]
    else:
        return [{"type": "text", "text": reply.text}]


def sse_events(
    reply: ScriptedReply, usage: dict[str, int], message_number: int
) -> list[tuple[str, dict[str, object]]]:
    blocks = content_blocks(reply, message_number)
    message = {
        "id": f"msg_mock_{message_number}",
        "type": "message",
        "role": "assistant",
        "model": MODEL_NAME,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": usage,
    }
    events: list[tuple[str, dict[str, object]]] = [
        ("message_start", {"type": "message_start", "message": message})
    ]
    for index, block in enumerate(blocks):
        if block["type"] == "tool_use":
            start_block = {**block, "input": {}}
            delta = {
                "type": "input_json_delta",
                "partial_json": json.dumps(block["input"]),
            }
        else:
            start_block = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": block["text"]}
        events.append(
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": start_block,
                },
            )
        )
        events.append(
            (
                "content_block_delta",
                {"type": "content_block_delta", "index": index, "delta": delta},
            )
        )
        events.append(
            ("content_block_stop", {"type": "content_block_stop", "index": index})
        )
    stop_reason = "tool_use" if reply.kind == "tool_use" else "end_turn"
    final_delta = {"stop_reason": stop_reason, "stop_sequence": None}
    events.append(
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": final_delta,
                "usage": {"output_tokens": 12},
            },
        )
    )
    events.append(("message_stop", {"type": "message_stop"}))
    return events


class MockState:
    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.lock = threading.Lock()
        self.message_count = 0

    def record(self, entry: dict[str, object]) -> int:
        with self.lock:
            self.message_count += 1
            entry["message_number"] = self.message_count
            with self.log_path.open("a") as log_file:
                log_file.write(json.dumps(entry) + "\n")
            return self.message_count


def make_handler(state: MockState) -> type[BaseHTTPRequestHandler]:
    class MockHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *log_arguments: object) -> None:
            pass

        def send_json(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("request-id", "req_mock")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            state.record(
                {"time_seconds": time.time(), "method": "GET", "path": self.path}
            )
            self.send_json(
                404,
                {
                    "type": "error",
                    "error": {"type": "not_found_error", "message": self.path},
                },
            )

        def do_POST(self) -> None:
            received_seconds = time.time()
            length = int(self.headers.get("Content-Length", "0"))
            request_body = json.loads(self.rfile.read(length) or b"{}")
            assert isinstance(request_body, dict)
            path = self.path.split("?")[0]
            if path.endswith("/count_tokens"):
                state.record(
                    {
                        "time_seconds": received_seconds,
                        "method": "POST",
                        "path": self.path,
                    }
                )
                self.send_json(
                    200, {"input_tokens": sum(usage_for(request_body).values())}
                )
            elif path.endswith("/v1/messages"):
                reply = choose_reply(request_body)
                usage = usage_for(request_body)
                entry: dict[str, object] = {
                    "time_seconds": received_seconds,
                    "method": "POST",
                    "path": self.path,
                    "session_header": self.headers.get("x-claude-code-session-id", ""),
                    "reply_kind": reply.kind,
                    "bash_command": reply.bash_command,
                    "usage": usage,
                    "body": request_body,
                }
                message_number = state.record(entry)
                if request_body.get("stream"):
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("request-id", f"req_mock_{message_number}")
                    self.end_headers()
                    for event_name, event_data in sse_events(
                        reply, usage, message_number
                    ):
                        self.wfile.write(
                            f"event: {event_name}\ndata: {json.dumps(event_data)}\n\n".encode()
                        )
                    self.wfile.flush()
                else:
                    stop_reason = "tool_use" if reply.kind == "tool_use" else "end_turn"
                    message = {
                        "id": f"msg_mock_{message_number}",
                        "type": "message",
                        "role": "assistant",
                        "model": MODEL_NAME,
                        "content": content_blocks(reply, message_number),
                        "stop_reason": stop_reason,
                        "stop_sequence": None,
                        "usage": {**usage, "output_tokens": 12},
                    }
                    self.send_json(200, message)
            else:
                state.record(
                    {
                        "time_seconds": received_seconds,
                        "method": "POST",
                        "path": self.path,
                    }
                )
                self.send_json(
                    404,
                    {
                        "type": "error",
                        "error": {"type": "not_found_error", "message": self.path},
                    },
                )

    return MockHandler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument(
        "--log",
        type=Path,
        required=True,
        help="JSONL file that receives one line per request",
    )
    parsed = parser.parse_args()
    server = ThreadingHTTPServer(
        ("0.0.0.0", parsed.port), make_handler(MockState(parsed.log))
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
