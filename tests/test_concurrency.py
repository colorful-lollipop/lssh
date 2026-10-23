"""并发回归测试：一条长任务挂住时，另一条命令必须还能秒回。

这些用例锁死的是本次改造的**用户可见目标**：

  "支持一个长时间任务挂住，还能另外用一个去查询记录。"

全部走本机 tmux（不联网、不碰堡垒机），断言里带时间上限：只要有人把
"分片轮询"改回"一口气阻塞到 timeout"，时间断言立刻失败。
"""

from __future__ import annotations

import json
import socket
import subprocess
import tempfile
import threading
import time

import pytest

from lssh.daemon import Daemon
from lssh.session import Session
from lssh.transports import LocalTransport

#: 单条命令"秒回"的上限。本机 tmux 实测 0.2~0.6s，留足抖动余量。
FAST = 3.0


def _tmux_name(prefix: str) -> str:
    return prefix + next(tempfile._get_candidate_names())  # noqa: SLF001


def _new_tmux(name: str) -> None:
    """用干净 bash 起 tmux 会话（避免 ~/.bashrc 里的 ble.sh 吃掉按键）。"""
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "bash", "--noprofile", "--norc"],
        capture_output=True,
        check=False,
    )


def _kill_tmux(name: str) -> None:
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True, check=False)


def _wait(predicate, timeout: float = 15.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _run_in_thread(session: Session, cmd: str, **kwargs):
    box: dict = {}

    def go() -> None:
        box["r"] = session.run(cmd, **kwargs)

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    return thread, box


def _slot_busy(session: Session, index: int) -> bool:
    info = session.info()["slots"][index]
    return bool(info["running"])


@pytest.fixture()
def slots3():
    name = _tmux_name("lssh-conc-")
    _new_tmux(name)
    session = Session(host="conc", transport=LocalTransport(), tmux=name, max_slots=3)
    yield session
    session.stop()
    _kill_tmux(name)


def test_all_slots_created_up_front(slots3):
    """三个槽位在握手时就建好，不是用到才建（少一次往返，也便于排障）。"""
    assert slots3.run("echo warm").ok
    names = subprocess.run(
        ["tmux", "list-windows", "-t", slots3.tmux, "-F", "#{window_name}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    assert {"w0", "w1", "w2"} <= set(names)
    assert [s["target"] for s in slots3.info()["slots"]] == [
        f"{slots3.tmux}:w0",
        f"{slots3.tmux}:w1",
        f"{slots3.tmux}:w2",
    ]


def test_query_while_long_task_runs(slots3):
    """核心场景：一个长任务挂住，连发三条查询都不该等它。"""
    session = slots3
    assert session.run("echo warm").slot == 0

    long_thread, long_box = _run_in_thread(session, "sleep 6", timeout=60)
    assert _wait(lambda: _slot_busy(session, 0)), "长任务没有进入槽位 w0"

    started = time.time()
    for index in range(3):
        mark = time.time()
        result = session.run(f"echo q{index}", timeout=10)
        assert time.time() - mark < FAST, "查询被长任务堵住了"
        assert result.ok, result.as_dict()
        assert result.stdout == f"q{index}"
        assert result.slot != 0, "查询不应该挤在被占用的槽位里"
    assert time.time() - started < 5.0

    long_thread.join(timeout=30)
    assert long_box["r"].ok
    assert not _slot_busy(session, 0)


def test_busy_only_when_every_slot_is_taken(slots3):
    """3 个槽位都在跑，第 4 条才会报忙 —— 而且必须**立刻**报，不是干等。"""
    session = slots3
    workers = [_run_in_thread(session, f"sleep 6 # {i}", timeout=60) for i in range(3)]
    assert _wait(lambda: all(_slot_busy(session, i) for i in range(3)))

    mark = time.time()
    result = session.run("echo should-be-busy", timeout=10)
    assert time.time() - mark < FAST
    assert result.status == "busy"
    assert "槽位" in (result.error or "")

    for thread, box in workers:
        thread.join(timeout=40)
        assert box["r"].ok
    # 全部释放之后又能用
    assert session.run("echo ok", timeout=10).stdout == "ok"


def test_wait_queues_instead_of_failing(slots3):
    """--wait：排队而不是立刻失败。"""
    session = slots3
    workers = [_run_in_thread(session, "sleep 3", timeout=60) for _ in range(3)]
    assert _wait(lambda: all(_slot_busy(session, i) for i in range(3)))
    result = session.run("echo queued", timeout=30, wait=20)
    assert result.ok, result.as_dict()
    assert result.stdout == "queued"
    for thread, _box in workers:
        thread.join(timeout=40)


def test_interrupt_hits_only_the_target_slot(slots3):
    """打断必须精确到那条命令，不能连坐别的槽位（这是并发的前提）。"""
    session = slots3
    long_thread, long_box = _run_in_thread(session, "sleep 30", timeout=60, req_id="req-long")
    assert _wait(lambda: _slot_busy(session, 0))

    other = session.run("echo other", timeout=10)
    assert other.ok and other.stdout == "other" and other.slot == 1

    assert session.interrupt(req_id="req-long") == [0]
    long_thread.join(timeout=30)
    assert not long_thread.is_alive()
    assert long_box["r"].status == "cancelled"
    # 其它槽位与被中断的槽位都还能继续用
    assert session.run("echo after", timeout=15).stdout == "after"


def test_timeout_keeps_only_its_own_slot(slots3):
    """超时只把**一个**槽位标成 pending，其它槽位照常可用。"""
    session = slots3
    result = session.run("sleep 6", timeout=0.5)
    assert result.status == "running"
    assert result.slot == 0
    pending = session.info()["slots"][0]["pending"]
    assert pending is not None and "sleep 6" in pending["cmd"]

    quick = session.run("echo quick", timeout=10)
    assert quick.ok and quick.stdout == "quick" and quick.slot == 1

    # 打断挂住的那条（没有 req_id 时退化成"打断所有还在跑的"，正好命中 pending）
    assert session.interrupt() == [0]
    assert session.info()["slots"][0]["pending"] is None
    assert session.run("echo reuse", timeout=15, slot=0).stdout == "reuse"


def test_pending_slot_reopens_when_remote_finishes(slots3):
    """超时的命令在远端跑完之后，那个槽位必须能自动重新启用（不需要 --kill）。"""
    session = slots3
    assert session.run("sleep 3", timeout=0.4).status == "running"
    time.sleep(3.2)
    result = session.run("echo back", timeout=15, slot=0)
    assert result.ok and result.stdout == "back"
    assert session.info()["slots"][0]["pending"] is None


def test_sequential_commands_keep_cwd(slots3):
    """顺序命令粘在同一槽位：`cd`/`export` 的语义不能被并发改坏。"""
    session = slots3
    session.run("cd /tmp")
    assert session.run("pwd").stdout == "/tmp"
    session.run("export LSSH_CONC=42")
    assert session.run("echo $LSSH_CONC").stdout == "42"

    long_thread, _box = _run_in_thread(session, "sleep 3", timeout=60)
    assert _wait(lambda: _slot_busy(session, 0))
    # 槽位 0 被占时才会切槽位 —— 此时 cwd 是各自独立的（设计如此）
    elsewhere = session.run("pwd", timeout=10)
    assert elsewhere.slot == 1
    assert elsewhere.stdout != "/tmp"
    long_thread.join(timeout=30)


def test_parallel_callers_queue_and_all_succeed(slots3):
    session = slots3
    results: list = []
    lock = threading.Lock()

    def go(index: int) -> None:
        result = session.run(f"echo n{index}", timeout=30, wait=30)
        with lock:
            results.append(result)

    threads = [threading.Thread(target=go, args=(i,), daemon=True) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(results) == 6
    assert all(r.ok for r in results), [r.as_dict() for r in results]
    assert sorted(r.stdout for r in results) == [f"n{i}" for i in range(6)]
    assert len({r.slot for r in results}) >= 2, "6 条命令应该被摊到多个槽位"


# --------------------------------------------------------------- 守护进程层


def _slots_of(sock, host: str) -> list[dict]:
    sessions = _rpc(sock, {"op": "list"})["sessions"]
    for item in sessions:
        if item["host"] == host:
            return item.get("slots") or []
    return []


def _rpc(sock, payload: dict, timeout: float = 120.0) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(sock))
        client.sendall((json.dumps(payload) + "\n").encode())
        data = b""
        while not data.endswith(b"\n"):
            chunk = client.recv(65536)
            if not chunk:
                break
            data += chunk
    return json.loads(data.decode())


@pytest.fixture()
def conc_daemon(tmp_path):
    """带 tmux 的守护进程：验证 RPC 层不会被单条长命令串行化。"""
    name = _tmux_name("lssh-dconc-")
    _new_tmux(name)
    sock = tmp_path / "daemon.sock"
    daemon = Daemon(
        transport_factory=lambda host: LocalTransport(),
        socket_path=sock,
        log_path=tmp_path / "daemon.log",
        tmux=name,
        max_slots=3,
        idle_timeout=60,
        reaper_interval=0.05,  # 让空闲回收真的在测试时间窗里跑起来
    )
    thread = threading.Thread(target=daemon.serve_forever, daemon=True)
    thread.start()
    for _ in range(100):
        if sock.exists():
            break
        time.sleep(0.02)
    yield daemon, sock
    daemon._stopping = True
    _kill_tmux(name)


def test_daemon_serves_queries_during_long_task(conc_daemon):
    _daemon, sock = conc_daemon
    box: dict = {}

    def run_long() -> None:
        box["r"] = _rpc(
            sock, {"op": "run", "host": "h1", "cmd": "sleep 5", "timeout": 60, "req_id": "L"}
        )

    thread = threading.Thread(target=run_long, daemon=True)
    thread.start()
    assert _wait(lambda: any(s["running"] for s in _slots_of(sock, "h1")))

    mark = time.time()
    quick = _rpc(sock, {"op": "run", "host": "h1", "cmd": "echo hi", "timeout": 20})
    assert quick["ok"] and quick["stdout"] == "hi"
    assert time.time() - mark < FAST

    mark = time.time()
    _rpc(sock, {"op": "list"})
    assert time.time() - mark < FAST

    assert _rpc(sock, {"op": "interrupt", "host": "h1", "req_id": "L"})["ok"]
    thread.join(timeout=30)
    assert box["r"]["status"] == "cancelled"

    again = _rpc(sock, {"op": "run", "host": "h1", "cmd": "echo again", "timeout": 20})
    assert again["ok"] and again["stdout"] == "again"


def test_daemon_pins_requested_slot(conc_daemon):
    _daemon, sock = conc_daemon
    result = _rpc(sock, {"op": "run", "host": "h1", "cmd": "echo pinned", "slot": 2})
    assert result["ok"] and result["slot"] == 2


def test_daemon_does_not_close_channel_under_running_task(conc_daemon):
    """空闲回收不能掐断正在跑的长任务（idle_timeout 再小也不行）。"""
    _daemon, sock = conc_daemon
    box: dict = {}

    def run_long() -> None:
        box["r"] = _rpc(sock, {"op": "run", "host": "h1", "cmd": "sleep 3", "timeout": 60})

    thread = threading.Thread(target=run_long, daemon=True)
    thread.start()
    assert _wait(lambda: any(s["running"] for s in _slots_of(sock, "h1")))
    conc_daemon[0].idle_timeout = 0.1  # 强行触发回收
    time.sleep(0.3)
    thread.join(timeout=30)
    assert box["r"]["ok"], box["r"]
