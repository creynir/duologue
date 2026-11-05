"""Local stand-ins for the two model APIs, so the real CLIs run without accounts or network.

One server answers both OpenAI's Responses API (``POST /v1/responses``, what Codex calls) and
Anthropic's Messages API (``POST /v1/messages``, what Claude Code calls), streaming as they do.
What it answers is scripted: the first rule whose text appears in the request's last user input
(a typed message or a tool result) gives the reply. Every request is kept, so a test can check
what each model was shown.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class Rule:
    api: str  # "responses" or "messages"
    match: str
    reply: list[dict]  # Responses output items, or Messages content blocks


@dataclass
class MockModels:
    rules: list[Rule] = field(default_factory=list)
    requests: list[dict] = field(default_factory=list)

    def start(self) -> MockModels:
        mock = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                self._json({"data": [], "models": []})

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {}
                path = self.path.split("?")[0]
                mock.requests.append({"path": path, "body": body})
                if path.endswith("/responses"):
                    self._sse(mock.responses(body, len(mock.requests)))
                elif path.endswith("/count_tokens"):
                    self._json({"input_tokens": 10})
                elif path.endswith("/messages"):
                    if body.get("stream"):
                        self._sse(mock.messages_stream(body, len(mock.requests)))
                    else:
                        self._json(mock.message(body, len(mock.requests)))
                else:
                    self._json({})

            def _json(self, doc):
                data = json.dumps(doc).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _sse(self, events):
                data = "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    # --------------------------------------------------------------------------- scripting
    def on(self, api: str, match: str, reply: list[dict]) -> None:
        self.rules.append(Rule(api, match, reply))

    def seen(self, api: str) -> list[dict]:
        """The request bodies one API received, oldest first."""
        suffix = "/responses" if api == "responses" else "/messages"
        return [r["body"] for r in self.requests if r["path"].endswith(suffix)]

    def _pick(self, api: str, body: dict) -> list[dict]:
        text = last_input(api, body)
        for rule in self.rules:
            if rule.api == api and rule.match in text:
                return rule.reply
        if api == "responses":
            return [message("ok")]
        return [{"type": "text", "text": "ok"}]

    # ----------------------------------------------------------------------------- answers
    def responses(self, body: dict, n: int) -> list[tuple[str, dict]]:
        rid = f"resp_{n}"
        events = [("response.created", {"type": "response.created", "response": {"id": rid}})]
        for i, item in enumerate(self._pick("responses", body)):
            item = {"id": f"item_{n}_{i}", **item}
            events.append(
                (
                    "response.output_item.done",
                    {"type": "response.output_item.done", "output_index": i, "item": item},
                )
            )
        usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        done = {"type": "response.completed", "response": {"id": rid, "usage": usage}}
        return [*events, ("response.completed", done)]

    def message(self, body: dict, n: int) -> dict:
        blocks = self._pick("messages", body)
        stop = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
        return {
            "id": f"msg_mock{n:06d}",
            "type": "message",
            "role": "assistant",
            "model": body.get("model", "claude-mock"),
            "content": blocks,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    def messages_stream(self, body: dict, n: int) -> list[tuple[str, dict]]:
        msg = self.message(body, n)
        start = {**msg, "content": [], "stop_reason": None}
        events = [("message_start", {"type": "message_start", "message": start})]
        for i, b in enumerate(msg["content"]):
            if b["type"] == "text":
                first = {"type": "text", "text": ""}
                delta = {"type": "text_delta", "text": b["text"]}
            else:
                first = {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}
                delta = {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}
            events += [
                (
                    "content_block_start",
                    {"type": "content_block_start", "index": i, "content_block": first},
                ),
                (
                    "content_block_delta",
                    {"type": "content_block_delta", "index": i, "delta": delta},
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": i}),
            ]
        end = {"stop_reason": msg["stop_reason"], "stop_sequence": None}
        events.append(
            (
                "message_delta",
                {"type": "message_delta", "delta": end, "usage": {"output_tokens": 5}},
            )
        )
        events.append(("message_stop", {"type": "message_stop"}))
        return events


def last_input(api: str, body: dict) -> str:
    """The text of the request's last user input: a typed message or a tool result."""
    if api == "responses":
        for item in reversed(body.get("input") or []):
            if item.get("type") == "function_call_output":
                out = item.get("output")
                return out if isinstance(out, str) else json.dumps(out)
            if item.get("type") == "message" and item.get("role") == "user":
                return json.dumps(item.get("content"))
        return ""
    for m in reversed(body.get("messages") or []):
        if m.get("role") == "user":  # Claude Code 2.1.318 puts a system message last
            return json.dumps(m.get("content"))
    return ""


def message(text: str) -> dict:
    """A Responses output message."""
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def shell(call_id: str, cmd: str) -> dict:
    """A Responses call of Codex's shell tool."""
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": "exec_command",
        "arguments": json.dumps({"cmd": cmd}),
    }


def bash(tool_id: str, cmd: str) -> dict:
    """A Messages call of Claude Code's Bash tool."""
    return {
        "type": "tool_use",
        "id": tool_id,
        "name": "Bash",
        "input": {"command": cmd, "description": "probe"},
    }
