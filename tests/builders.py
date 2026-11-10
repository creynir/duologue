"""Small, deterministic Claude transcripts and Codex rollouts for tests.

The record shapes follow what Claude Code 2.1.3xx and Codex 0.163 write; the content is invented.
"""

from __future__ import annotations

from duologue.translate.common import iso_ms

CWD = "/work/project"
SESSION = "5e550000-0000-4000-8000-000000000001"
START_MS = 1_793_800_800_000  # 2026-11-04T14:00:00Z


class ClaudeTranscript:
    """Builds Claude records with sequential uuids and timestamps one second apart."""

    def __init__(self, cwd: str = CWD, session: str = SESSION, start_ms: int = START_MS):
        self.cwd, self.session, self.ms = cwd, session, start_ms
        self.records: list[dict] = []
        self._n = 0
        self._parent: str | None = None

    def _record(self, role: str, message: dict, **extra) -> dict:
        self._n += 1
        u = f"c1a0de00-0000-4000-8000-{self._n:012d}"
        r = {
            "parentUuid": self._parent,
            "isSidechain": False,
            "userType": "external",
            "cwd": self.cwd,
            "sessionId": self.session,
            "version": "2.1.303",
            "gitBranch": "",
            "type": role,
            "uuid": u,
            "timestamp": iso_ms(self.ms),
            "message": message,
            **extra,
        }
        self.ms += 1000
        self._parent = u
        self.records.append(r)
        return r

    def user(self, content, **extra) -> dict:
        return self._record("user", {"role": "user", "content": content}, **extra)

    def assistant(self, content, **extra) -> dict:
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        message = {
            "id": f"msg_{self._n:024d}",
            "type": "message",
            "role": "assistant",
            "model": "claude-test-model",
            "content": content,
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        return self._record("assistant", message, **extra)

    def queued(self, prompt, mode: str = "prompt", human: bool = True) -> dict:
        """A message typed while Claude was busy, as Claude Code 2.1.3xx records it: an
        attachment that Claude takes into the running turn."""
        r = self._record("attachment", {})
        del r["message"]
        r["attachment"] = {
            "type": "queued_command",
            "prompt": prompt,
            "commandMode": mode,
            "origin": {"kind": "human" if human else "system"},
            "humanTurn": human,
            "delivery_id": f"d-{self._n}",
            "source_uuid": f"s-{self._n}",
            "timestamp": r["timestamp"],
        }
        return r

    def call(self, name: str, inp: dict, result, error: bool = False) -> str:
        """A tool call and its result; returns the tool_use id."""
        tool_id = f"toolu_{self._n + 1:024d}"
        self.assistant([{"type": "tool_use", "id": tool_id, "name": name, "input": inp}])
        block = {
            "type": "tool_result",
            "tool_use_id": tool_id,
            "content": result,
            "is_error": error,
        }
        self.user([block])
        return tool_id


class CodexRollout:
    """Builds Codex rollout records (without ordinals) with timestamps one second apart."""

    def __init__(self, thread_id: str = "th-1", start_ms: int = START_MS):
        self.thread_id, self.ms = thread_id, start_ms
        self.records: list[dict] = []
        self.turn_id: str | None = None

    def _line(self, kind: str, payload: dict) -> dict:
        r = {"timestamp": iso_ms(self.ms), "type": kind, "payload": payload}
        self.ms += 1000
        self.records.append(r)
        return r

    def start(self, turn_id: str) -> None:
        self.turn_id = turn_id
        self._line("event_msg", {"type": "task_started", "turn_id": turn_id})

    def item(self, item: dict) -> None:
        payload = {"type": "item_completed", "thread_id": self.thread_id, "turn_id": self.turn_id}
        self._line("event_msg", {**payload, "item": item})

    def response(self, payload: dict) -> None:
        self._line("response_item", payload)

    def complete(self, last_message: str | None = None) -> None:
        payload = {"type": "task_complete", "turn_id": self.turn_id}
        self._line("event_msg", {**payload, "last_agent_message": last_message})

    def user_says(self, text: str, item_id: str) -> None:
        content = [{"type": "text", "text": text, "text_elements": []}]
        self.item({"type": "UserMessage", "id": item_id, "content": content})

    def agent_says(self, text: str, item_id: str) -> None:
        content = [{"type": "Text", "text": text}]
        self.item(
            {"type": "AgentMessage", "id": item_id, "content": content, "phase": "final_answer"}
        )
