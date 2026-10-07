"""A scripted stand-in for the Anthropic Messages API, for running the REAL Claude Code
CLI in a test sandbox without a token or network (tests/test_claude_code_profile.py).

The test image's managed settings point Claude Code at it. Claude Code's model turns
are read from the run's spec, from a ``probe: {"turns": [...]}`` line (the same line
the probe Agent profile reads). The Nth model call of the conversation answers with
the Nth turn:

- ``{"tool": "Bash", "input": {...}}``  calls one of Claude Code's tools;
- ``{"seen": ["MARK", ...]}``  replies with ``seen: <the marks found>`` (in what Claude
  Code sent after the run's prompt: tool results, skill bodies, system notes) and then
  ``RUN-OUTCOME: done``.

Calls without tools (titles, summaries) get a short text answer.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
_SCRIPT = re.compile(r"^\s*probe:\s*(\{.*\})\s*$", re.MULTILINE)


def _text(content) -> str:
    return json.dumps(content)


def turns_of(messages: list[dict]) -> list[dict]:
    for m in messages:
        if m.get("role") == "user":
            found = _SCRIPT.search(_flatten(m["content"]))
            if found:
                return json.loads(found.group(1)).get("turns", [])
    return []


def _flatten(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))


def answer(body: dict) -> tuple[dict, str]:
    """(content block, stop reason) for one model call."""
    messages = body.get("messages", [])
    if not body.get("tools"):
        return {"type": "text", "text": "ok"}, "end_turn"
    turns = turns_of(messages)
    n = sum(1 for m in messages if m.get("role") == "assistant")
    turn = turns[n] if n < len(turns) else {"seen": []}
    if "tool" in turn:
        return {"type": "tool_use", "id": f"toolu_{n:04d}_{uuid.uuid4().hex[:8]}", "name": turn["tool"], "input": turn.get("input", {})}, "tool_use"
    later = _text(messages[1:])
    seen = [mark for mark in turn.get("seen", []) if mark in later]
    return {"type": "text", "text": f"seen: {', '.join(seen)}\nRUN-OUTCOME: done\n"}, "end_turn"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.end_headers()

    def do_GET(self) -> None:
        self._json({})

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        if log := os.environ.get("FAKE_ANTHROPIC_LOG"):  # for debugging the fake itself
            with open(log, "a") as f:
                f.write(json.dumps({"t": time.time(), "path": self.path, "tools": [t.get("name") for t in body.get("tools") or []],
                                    "n": sum(1 for m in body.get("messages", []) if m.get("role") == "assistant")}) + "\n")
        if "count_tokens" in self.path:
            return self._json({"input_tokens": 10})
        block, stop = answer(body)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        start = dict(block, input={}) if block["type"] == "tool_use" else dict(block, text="")
        self._event("message_start", {"type": "message_start", "message": {
            "id": f"msg_{uuid.uuid4().hex}", "type": "message", "role": "assistant", "model": body.get("model", "fake"),
            "content": [], "stop_reason": None, "usage": {"input_tokens": 1, "output_tokens": 1}}})
        self._event("content_block_start", {"type": "content_block_start", "index": 0, "content_block": start})
        delta = ({"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
                 if block["type"] == "tool_use" else {"type": "text_delta", "text": block["text"]})
        self._event("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta})
        self._event("content_block_stop", {"type": "content_block_stop", "index": 0})
        self._event("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                                       "usage": {"output_tokens": 1}})
        self._event("message_stop", {"type": "message_stop"})

    def _json(self, data: dict) -> None:
        raw = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _event(self, name: str, data: dict) -> None:
        self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
        self.wfile.flush()


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
