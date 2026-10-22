from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from lssh.daemon import Daemon
from lssh.transports import LocalTransport


@pytest.fixture()
def daemon(tmp_path):
    sock = tmp_path / "daemon.sock"
    d = Daemon(
        transport_factory=lambda host: LocalTransport(),
        socket_path=sock,
        log_path=tmp_path / "daemon.log",
        tmux=None,
        idle_timeout=60,
    )
    thread = threading.Thread(target=d.serve_forever, daemon=True)
    thread.start()
    for _ in range(100):
        if sock.exists():
            break
        time.sleep(0.02)
    yield d, sock
    d._stopping = True
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(str(sock))
            s.sendall(b'{"op":"stop"}\n')
            s.recv(4096)
    except OSError:
        pass


def rpc(sock, payload):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(30)
        s.connect(str(sock))
        s.sendall((json.dumps(payload) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data.decode())


def test_ping(daemon):
    _, sock = daemon
    assert rpc(sock, {"op": "ping"})["ok"]


def test_run_and_persist(daemon):
    _, sock = daemon
    r = rpc(sock, {"op": "run", "host": "h1", "cmd": "echo hello"})
    assert r["ok"] and r["rc"] == 0 and r["stdout"] == "hello"

    rpc(sock, {"op": "run", "host": "h1", "cmd": "cd /tmp"})
    assert rpc(sock, {"op": "run", "host": "h1", "cmd": "pwd"})["stdout"] == "/tmp"


def test_hosts_isolated(daemon):
    _, sock = daemon
    rpc(sock, {"op": "run", "host": "a", "cmd": "cd /tmp"})
    assert rpc(sock, {"op": "run", "host": "b", "cmd": "pwd"})["stdout"] != "/tmp"


def test_list(daemon):
    _, sock = daemon
    rpc(sock, {"op": "run", "host": "h1", "cmd": "true"})
    items = rpc(sock, {"op": "list"})["sessions"]
    assert [i["host"] for i in items] == ["h1"]
    assert items[0]["state"] == "ready"


def test_restart_keeps_channel_working(daemon):
    _, sock = daemon
    rpc(sock, {"op": "run", "host": "h1", "cmd": "true"})
    assert rpc(sock, {"op": "restart", "host": "h1"})["ok"]
    assert rpc(sock, {"op": "run", "host": "h1", "cmd": "echo again"})["stdout"] == "again"


def test_unknown_op(daemon):
    _, sock = daemon
    assert rpc(sock, {"op": "nope", "host": "h1"})["ok"] is False
