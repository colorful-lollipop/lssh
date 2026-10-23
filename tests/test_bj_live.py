"""真实主机的并发验收（默认 skip）。

    LSSH_TEST_SSH=bj .venv/bin/python -m pytest tests/test_bj_live.py -v

为什么单列一个文件：本机 tmux 只能证明"实现没把通道锁死"，真实链路还要多一层
堡垒机/pty/tmux 的加持，命令行长度、shell rc、tmux 版本都可能不一样。

刻意用独立的远端 tmux 会话名（默认 `lssh-dev`）和独立的槽位，绝不碰用户平时
在用的 `lssh` 会话 —— 测试把 `kill-session` 打在自己身上才是安全的。
"""

from __future__ import annotations

import os
import time

import pytest

from lssh.session import Session
from lssh.transports import SSHTransport

HOST = os.environ.get("LSSH_TEST_SSH")
pytestmark = pytest.mark.skipif(not HOST, reason="未设置 LSSH_TEST_SSH（例如 bj）")

#: 查询命令的"秒回"上限（含一次真实 SSH 往返的抖动）。
FAST = 8.0
#: 长任务在远端写日志的文件。
LOG = "/tmp/lssh-live-long.log"


@pytest.fixture(scope="module")
def remote():
    session = Session(
        host=HOST,
        transport=SSHTransport(host=HOST, askpass=os.environ.get("LSSH_ASKPASS")),
        tmux=os.environ.get("LSSH_TEST_TMUX", "lssh-dev"),
        max_slots=3,
        default_timeout=60,
    )
    try:
        session.start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"连不上 {HOST}: {exc}")
    yield session
    session.kill()


def test_slots_exist_on_remote(remote):
    names = remote.run("tmux list-windows -t lssh-dev -F '#{window_name}'").stdout.split()
    assert {"w0", "w1", "w2"} <= set(names)


def test_long_task_holds_one_slot_while_queries_return(remote):
    """核心验收：长任务挂住 → 另一条命令照样查记录，且槽位互不干扰。"""
    remote.run(f"rm -f {LOG}")
    long_cmd = f"i=0; while [ $i -lt 120 ]; do date +%T >> {LOG}; i=$((i+1)); sleep 1; done"
    started = remote.run(long_cmd, timeout=2.5)
    assert started.status == "running", started.as_dict()
    busy_slot = started.slot
    assert remote.info()["slots"][busy_slot]["pending"] is not None

    # 三条查询：必须都快、都落在别的槽位上
    for query in (f"tail -n 2 {LOG}", "uname -n", "whoami"):
        mark = time.time()
        result = remote.run(query, timeout=30)
        elapsed = time.time() - mark
        assert result.ok, result.as_dict()
        assert result.stdout.strip(), f"{query} 没有输出"
        assert elapsed < FAST, f"查询被长任务堵住了（{elapsed:.1f}s）：{query}"
        assert result.slot != busy_slot

    # 长任务还在跑（被查询打断会让"挂住"这件事本身失真）
    assert remote.run(f"tail -n 1 {LOG}").ok

    # 收尾：精确打断长任务，会话仍然健康
    assert remote.interrupt() == [busy_slot]
    time.sleep(0.5)
    assert remote.run("echo after-interrupt", timeout=30).stdout == "after-interrupt"


def test_parallel_slots_on_real_host(remote):
    """3 个槽位并行 + 第 4 条快速报忙。"""
    import threading

    results: dict = {}

    def run(index: int) -> None:
        results[index] = remote.run(f"sleep 5; echo slot{index}", timeout=60)

    threads = [threading.Thread(target=run, args=(i,), daemon=True) for i in range(3)]
    for thread in threads:
        thread.start()
    time.sleep(2.0)

    mark = time.time()
    busy = remote.run("echo nope", timeout=10)
    assert busy.status == "busy"
    assert time.time() - mark < FAST

    for thread in threads:
        thread.join(timeout=60)
    assert sorted(r.stdout for r in results.values()) == ["slot0", "slot1", "slot2"]
    assert remote.run("echo reuse", timeout=30).ok


def test_sequential_keeps_cwd_on_real_host(remote):
    remote.run("cd /tmp")
    assert remote.run("pwd").stdout.strip() == "/tmp"
