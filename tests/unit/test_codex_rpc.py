import socket
import sys
import tempfile
from pathlib import Path

import pytest

from duologue.codex.rpc import AppServer, AppServerError, control_socket, daemon_running

# A scripted app-server: what it does depends on the method it is asked.
SERVER = r"""
import json, sys
def send(m):
    sys.stdout.write(json.dumps(m) + "\n"); sys.stdout.flush()
seen = []
for raw in sys.stdin:
    msg = json.loads(raw)
    if "id" not in msg:
        continue
    m, rid = msg["method"], msg["id"]
    seen.append(msg)
    if m == "initialize":
        send({"id": rid, "result": {}})
    elif m == "seen":
        send({"id": rid, "result": {"seen": seen}})
    elif m == "ask-first":
        send({"id": "srv-1", "method": "item/commandExecution/requestApproval", "params": {}})
        answer = json.loads(sys.stdin.readline())
        send({"id": rid, "result": {"answer": answer}})
    elif m == "noise":
        sys.stdout.write("not json\n[1]\n")
        send({"method": "thread/started", "params": {}})
        send({"id": 999, "result": {}})
        send({"id": rid, "result": {"ok": True}})
    elif m == "fail":
        busy = "thread x already has an active writer"
        send({"id": rid, "error": {"code": -32600, "message": busy}})
    elif m == "other-error":
        send({"id": rid, "error": {"code": -32601, "message": "method not found"}})
    elif m == "exit":
        sys.stderr.write("boom\n"); sys.stderr.flush(); sys.exit(3)
    elif m == "silent":
        pass
"""


@pytest.fixture
def server():
    with AppServer([sys.executable, "-c", SERVER], timeout=10) as s:
        yield s


def test_the_connection_opens_with_initialize(server):
    first = server.request("seen")["seen"][0]
    assert first["method"] == "initialize"
    assert first["params"]["clientInfo"]["name"] == "duologue"
    assert first["params"]["capabilities"] == {"experimentalApi": True}


def test_a_question_from_the_server_is_refused(server):
    answer = server.request("ask-first")["answer"]
    assert answer["id"] == "srv-1"
    assert answer["error"]["code"] == -32601


def test_notifications_and_noise_are_skipped(server):
    assert server.request("noise") == {"ok": True}


def test_an_error_answer(server):
    with pytest.raises(AppServerError) as e:
        server.request("fail")
    assert e.value.active_writer and e.value.code == -32600 and e.value.method == "fail"
    with pytest.raises(AppServerError) as e:
        server.request("other-error")
    assert not e.value.active_writer


def test_a_server_that_exits(server):
    with pytest.raises(AppServerError, match="exited"):
        server.request("exit")
    with pytest.raises(AppServerError):
        server.request("anything")


def test_a_server_that_does_not_answer(server):
    with pytest.raises(TimeoutError):
        server.request("silent", timeout=0.3)


def test_a_server_that_never_starts():
    with pytest.raises(AppServerError, match="exited"):
        AppServer([sys.executable, "-c", "import sys; sys.exit(1)"], timeout=10)


def test_close_after_the_input_is_closed():
    s = AppServer([sys.executable, "-c", SERVER], timeout=10)
    s._proc.stdin.close()
    s.close()
    assert s._proc.poll() is not None


def test_daemon_running():
    with tempfile.TemporaryDirectory(dir="/tmp") as home:
        home = Path(home)
        assert not daemon_running(home)
        path = control_socket(home)
        path.parent.mkdir(parents=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        assert not daemon_running(home)  # a socket file nobody listens on
        listener.listen()
        try:
            assert daemon_running(home)
        finally:
            listener.close()
