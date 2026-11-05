"""duologue against the real Claude Code and Codex CLIs, in throwaway homes.

Both CLIs run for real (tools included) against the local model stand-ins in mock_models.py, so
no account or network is needed. The tests check what matters to a person: each app shows the
other's turns natively, each model is shown them on its next turn, and nothing is copied twice.

Run with ``pytest -m e2e``. The CLIs come from ``DUOLOGUE_E2E_CLAUDE`` and ``DUOLOGUE_E2E_CODEX``,
else ``claude`` and ``codex`` on the PATH; CI installs the versions in ci.yml.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from mock_models import MockModels, bash, message, shell

from duologue.codex.threads import Codex

CLAUDE = os.environ.get("DUOLOGUE_E2E_CLAUDE") or shutil.which("claude")
CODEX = os.environ.get("DUOLOGUE_E2E_CODEX") or shutil.which("codex")
MISSING = not (CLAUDE and CODEX)
if MISSING and os.environ.get("DUOLOGUE_E2E_REQUIRED"):  # CI: a skip would hide a broken job
    raise RuntimeError("the e2e tests need the claude and codex CLIs on the PATH")
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(MISSING, reason="needs the claude and codex CLIs"),
]
# Variables of a Claude Code or Codex the tests may run inside: none may leak into the CLIs.
LEAKY = ("CLAUDE", "ANTHROPIC", "CODEX", "OPENAI", "DUOLOGUE")
ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@dataclass
class World:
    root: Path
    mock: MockModels

    def __post_init__(self):
        for d in ("home", "work", "claude", "codex", "duologue"):
            (self.root / d).mkdir()
        (self.root / "codex" / "config.toml").write_text(
            'model = "mock-model"\nmodel_provider = "mock"\napproval_policy = "never"\n'
            'sandbox_mode = "danger-full-access"\n\n[model_providers.mock]\nname = "mock"\n'
            f'base_url = "{self.mock.url}/v1"\nwire_api = "responses"\n'
            "requires_openai_auth = false\nrequest_max_retries = 0\nstream_max_retries = 0\n"
        )
        (self.root / "duologue" / "config.toml").write_text(
            f'claude_bin = "{CLAUDE}"\ncodex_bin = "{CODEX}"\nquiet_seconds = 0\n'
        )

    @property
    def work(self) -> Path:
        return self.root / "work"

    def env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(LEAKY)}
        return {
            **env,
            "HOME": str(self.root / "home"),
            "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
            "CODEX_HOME": str(self.root / "codex"),
            "DUOLOGUE_HOME": str(self.root / "duologue"),
            "ANTHROPIC_BASE_URL": self.mock.url,
            "ANTHROPIC_API_KEY": "sk-ant-mock",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            "TERM": "dumb",
        }

    def run(self, argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            argv,
            cwd=self.work,
            env=self.env(),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=180,
        )

    def claude(self, prompt: str, *args: str) -> str:
        """Run one Claude Code turn; return its session id."""
        r = self.run(
            [
                CLAUDE,
                "-p",
                prompt,
                "--model",
                "claude-mock-1",
                "--dangerously-skip-permissions",
                "--output-format",
                "json",
                *args,
            ]
        )
        result = json.loads([ln for ln in r.stdout.splitlines() if ln.startswith("{")][-1])
        assert not result.get("is_error"), result
        return result["session_id"]

    def codex(self, *args: str) -> str:
        """Run one Codex turn (``codex exec``); return the thread id."""
        r = self.run([CODEX, "exec", "--skip-git-repo-check", *args])
        assert r.returncode == 0, r.stderr[-2000:]
        found = re.search(r"session id: (" + ID.pattern + ")", r.stdout + r.stderr)
        assert found, r.stdout[-2000:]
        return found.group(1)

    def duologue(self, *args: str, ok: bool = True) -> str:
        r = self.run([sys.executable, "-m", "duologue", *args])
        if ok:
            assert r.returncode == 0, r.stdout + r.stderr
        return r.stdout + r.stderr

    def codex_items(self, thread_id: str) -> list[str]:
        """The item kinds the Codex app is given for the thread, oldest turn first."""
        codex = Codex(self.root / "codex", CODEX, env=self.env())
        with codex.app_server() as server:
            page = server.request(
                "thread/turns/list",
                {"threadId": thread_id, "itemsView": "full", "limit": 50, "sortDirection": "asc"},
            )
        return [i["type"] for turn in page["data"] for i in turn["items"]]


def codex_model_texts(body: dict) -> list[str]:
    """Every text the Codex model was given in one request."""
    return [
        c.get("text", "")
        for item in body.get("input") or []
        if item.get("type") == "message"
        for c in item.get("content") or []
        if isinstance(c, dict)
    ]


def claude_calls(body: dict) -> list[tuple[dict, dict | None]]:
    """(tool_use, its tool_result) pairs in one Claude request, matched by id."""
    blocks = [
        b
        for m in body.get("messages") or []
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict)
    ]
    results = {b.get("tool_use_id"): b for b in blocks if b.get("type") == "tool_result"}
    return [(b, results.get(b.get("id"))) for b in blocks if b.get("type") == "tool_use"]


def result_text(result: dict | None) -> str:
    content = (result or {}).get("content")
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
    return str(content or "")


@pytest.fixture
def world(tmp_path: Path):
    mock = MockModels().start()
    mock.on(
        "messages",
        "run the claude probe",
        [
            {"type": "text", "text": "Running it."},
            bash("toolu_e2e00000000000000000001", "echo probe-from-claude"),
        ],
    )
    mock.on(
        "messages", "probe-from-claude", [{"type": "text", "text": "Claude saw probe-from-claude."}]
    )
    mock.on("responses", "run the codex probe", [shell("call_e2e_1", "echo probe-from-codex")])
    mock.on("responses", "probe-from-codex", [message("Codex saw probe-from-codex.")])
    yield World(tmp_path, mock)
    mock.stop()


def test_one_conversation_in_both_apps(world: World):
    session = world.claude("hello from claude")
    thread = world.codex("hello from codex")
    out = world.duologue("link", "--claude", session, "--codex", thread, "--name", "e2e")
    assert "turns from now on are copied both ways" in out

    # A Claude turn with a real tool call reaches Codex as Codex's own items...
    world.claude("run the claude probe", "--resume", session)
    assert "copied 1 Claude turn(s) to Codex" in world.duologue("sync")
    assert world.codex_items(thread)[-4:] == [
        "userMessage",
        "agentMessage",
        "commandExecution",
        "agentMessage",
    ]
    # ...and Codex's model is shown the command and what it printed on its next turn.
    world.codex("resume", thread, "what did claude run")
    notes = [
        t
        for t in codex_model_texts(world.mock.seen("responses")[-1])
        if t.startswith("[Claude ran Bash] echo probe-from-claude")
    ]
    assert len(notes) == 1 and notes[0].rstrip().endswith("\nprobe-from-claude")

    # A Codex turn with a real command reaches Claude as tool calls, and Claude resumes with it.
    world.codex("resume", thread, "run the codex probe")
    assert "copied 2 Codex turn(s) to Claude" in world.duologue("sync")
    world.claude("what did codex run", "--resume", session)
    request = world.mock.seen("messages")[-1]
    calls = [
        (use, result)
        for use, result in claude_calls(request)
        if use.get("input", {}).get("command") == "echo probe-from-codex"
    ]
    assert len(calls) == 1 and "probe-from-codex" in result_text(calls[0][1])
    assert "continued in Codex" in json.dumps(request["messages"])

    # That Claude turn goes back to Codex once, and then nothing is left to copy.
    assert "copied 1 Claude turn(s) to Codex" in world.duologue("sync")
    assert "idle: up to date" in world.duologue("sync")
    assert world.duologue("sync").count("synced") == 0
    assert '"e2e"  linked' in world.duologue("status")


def test_a_claude_session_gets_a_new_codex_thread(world: World):
    session = world.claude("run the claude probe")
    out = world.duologue("link", "--claude", session)
    thread = re.search(r"created Codex thread (" + ID.pattern + ")", out).group(1)
    assert "commandExecution" in world.codex_items(thread)
    world.codex("resume", thread, "what happened so far")
    shown = json.dumps(world.mock.seen("responses")[-1]["input"])
    assert "[Claude ran Bash] echo probe-from-claude" in shown


def test_a_codex_thread_gets_a_new_claude_session(world: World):
    thread = world.codex("run the codex probe")
    out = world.duologue("link", "--codex", thread)
    session = re.search(r"created Claude session (" + ID.pattern + ")", out).group(1)
    assert world.claude("what did codex run", "--resume", session) == session
    request = world.mock.seen("messages")[-1]
    commands = [use["input"].get("command") for use, _ in claude_calls(request)]
    assert commands == ["echo probe-from-codex"]
    assert "continued in Codex" in json.dumps(request["messages"])
    # The session goes on in Claude, and that turn reaches the Codex thread: real Claude Code
    # writes bookkeeping records (api-request-blob) on the way, which must not stop the pair.
    assert "copied 1 Claude turn(s) to Codex" in world.duologue("sync")


def test_doctor_knows_this_codex(world: World):
    out = world.duologue("doctor", ok=False)
    assert "item format known" in out, out
