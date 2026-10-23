from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from lssh.protocol import build_script, clean, new_token, parse, paths
from lssh.session import Session
from lssh.transports import LocalTransport


def local_session(**kwargs) -> Session:
    return Session(host="local", transport=LocalTransport(), tmux=None, **kwargs)


def make_tmux_session(tmux_name: str, **kwargs) -> Session:
    """用干净的 bash 起 pane：本机 ~/.bashrc 会加载 ble.sh，行编辑器会把
    tmux send-keys 打进去的字符吃掉，测出来的行为跟生产不一致。"""
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", tmux_name, "bash", "--noprofile", "--norc"],
        capture_output=True,
        check=False,
    )
    return Session(host="local-tmux", transport=LocalTransport(), tmux=tmux_name, **kwargs)


@pytest.fixture()
def tmux_session(tmp_path):
    """走真实 tmux 的**单槽位**会话（超时/中断的语义在这里最干净）。"""
    name = "lssh-test-" + next(tempfile._get_candidate_names())  # noqa: SLF001
    s = make_tmux_session(name, max_slots=1)
    yield s
    s.stop()
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True, check=False)


@pytest.fixture()
def session():
    s = local_session()
    yield s
    s.stop()


def test_clean_strips_ansi_and_crlf():
    raw = b"\x1b[?2004hhello\r\nworld\r\n"
    assert clean(raw) == "hello\nworld\n"


def test_clean_keeps_utf8():
    assert "中文 ✓" in clean("中文 ✓\r\n")


def test_build_script_has_files_and_rc():
    token = new_token()
    script = build_script(token, "echo hi")
    assert "echo hi" in script
    assert f"/tmp/.lssh-{token}.out" in script
    assert f"/tmp/.lssh-{token}.done" in script
    assert "echo $?" in script


def test_parse_detects_missing_markers():
    assert parse("no markers here", new_token()) is None
    assert parse("abcLSSH1S\ndef", "LSSH1") is None


def test_parse_splits_streams():
    token = "T1"
    segment = "junk\nT1S\nline1\nline2\nT1M\nwarn\nT1R:0\nT1E\ntail"
    rc, out, err = parse(segment, token)
    assert rc == 0
    assert out == "line1\nline2"
    assert err == "warn"


def test_parse_negative_rc():
    segment = "T2S\nT2M\nT2R:137\nT2E\n"
    assert parse(segment, "T2") == (137, "", "")


def test_parse_rejects_echo():
    """回显里的标记是字面量 \\n，不能算数。"""
    token = "T3"
    echoed = "printf 'T3S\\n'; cat /tmp/x\n"
    assert parse(echoed, token) is None


# ---------------------------------------------------------------- 真实执行（本地）


def test_echo(session):
    result = session.run("echo hi")
    assert result.ok and result.rc == 0
    assert result.stdout == "hi"
    assert result.stderr == ""


def test_exit_code(session):
    assert session.run("false").rc == 1
    assert session.run("exit_code=42; (exit $exit_code)").rc == 42


def test_stderr_separated(session):
    result = session.run("echo out; echo err >&2")
    assert result.stdout == "out"
    assert result.stderr == "err"


def test_multiline_command(session):
    result = session.run("echo one\necho two")
    assert result.stdout == "one\ntwo"


def test_cwd_persists(session):
    session.run("cd /tmp")
    assert session.run("pwd").stdout == "/tmp"


def test_env_persists(session):
    session.run("export LSSH_TEST=42")
    assert session.run("echo $LSSH_TEST").stdout == "42"


def test_no_trailing_newline(session):
    result = session.run("printf abc")
    assert result.stdout == "abc"


def test_unicode_roundtrip(session):
    assert session.run("printf '中文'").stdout == "中文"


def test_large_output(session):
    result = session.run("seq 1 2000")
    lines = result.stdout.splitlines()
    assert len(lines) == 2000
    assert lines[0] == "1" and lines[-1] == "2000"


def test_stdin_not_needed(session):
    result = session.run("cat /dev/null; echo done")
    assert result.stdout == "done"


def test_timeout_reported(tmux_session):
    result = tmux_session.run("sleep 5", timeout=0.6)
    assert not result.ok
    assert "超时" in (result.error or "")


def test_recover_after_timeout(tmux_session):
    assert not tmux_session.run("sleep 5", timeout=0.6).ok
    tmux_session.interrupt()
    time.sleep(0.3)
    assert tmux_session.run("echo back").stdout == "back"


def test_command_with_quotes_and_pipes(session):
    result = session.run("printf '%s\\n' 'a b' | tr ' ' '+'")
    assert result.stdout == "a+b"


def test_empty_command(session):
    result = session.run("")
    assert result.ok and result.rc == 0


def test_interrupt_ends_long_command(tmux_session):
    """interrupt() 发 Ctrl-C；本地 shell 没有控制终端时可能不生效，

    所以这里只断言：命令被中断或至少按时超时返回，且会话随后仍可用。
    真实 SSH 场景下远端 pty 有控制终端，Ctrl-C 会正常打断。
    """
    s = tmux_session
    try:
        s.start()
        import threading
        import time

        box: dict = {}

        def go():
            box["r"] = s.run("sleep 30", timeout=20)

        thread = threading.Thread(target=go)
        thread.start()
        time.sleep(0.5)
        s.interrupt()
        thread.join(timeout=15)
        assert not thread.is_alive()
        assert not box["r"].ok
        assert s.run("echo back").stdout == "back"
    finally:
        s.stop()


def test_real_ssh_optional():
    """设置 LSSH_TEST_SSH=<host> 时对真实主机做一次冒烟测试。"""
    import os

    host = os.environ.get("LSSH_TEST_SSH")
    if not host:
        pytest.skip("未设置 LSSH_TEST_SSH")
    if subprocess.run(["ssh", "-V"], capture_output=True).returncode not in (0, 1):
        pytest.skip("没有 ssh 客户端")
    from lssh.transports import SSHTransport

    s = Session(
        host=host, transport=SSHTransport(host=host, askpass=os.environ.get("LSSH_ASKPASS"))
    )
    try:
        result = s.run("echo lssh-ok; uname -s")
        assert result.ok and "lssh-ok" in result.stdout
        assert s.run("cd /tmp; pwd").stdout.strip().endswith("/tmp")
    finally:
        s.stop()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


def test_verify_detects_truncation():
    """远端报告的长度/md5 与收到的不一致时必须报错，而不是静默返回。"""
    import hashlib

    s = local_session()
    token = "T9"
    body = "hello"
    good = hashlib.md5(body.encode()).hexdigest()
    seg_ok = (
        f"{token} SZ {len(body)} {good}\n{token}MZ 0 d41d8cd98f00b204e9800998ecf8427e\n"
        f"{token}S\n{body}\n{token}M\n{token}R:0\n{token}E\n"
    )
    assert s._verify(token, seg_ok) is True

    seg_bad = seg_ok.replace(good, "0" * 32)
    assert s._verify(token, seg_bad) is False


def test_timeout_reports_still_running(tmux_session):
    """超时 = 客户端放弃等待，不等于命令失败。

    旧行为是「超时即 Ctrl-C + 标脏」，对 mkfs/dd 这类命令是二次伤害；
    新行为：如实报告"仍在远端跑"，且不打断它。
    """
    result = tmux_session.run("sleep 5", timeout=0.5)
    assert not result.ok
    assert result.status == "running"
    assert "仍在远端执行" in (result.error or "")
    assert "未发送 Ctrl-C" in (result.error or "")
    # 会话本身是健康的，只是远端 pane 还占着
    assert tmux_session.state == "ready"
    assert tmux_session.info()["remote_busy"] is not None

    # 单槽位下，下一条命令必须快速失败，且不能把还在跑的那条 Ctrl-C 掉
    second = tmux_session.run("echo x", timeout=5)
    assert second.status == "busy"
    assert "仍在远端运行" in (second.error or "")
    assert tmux_session.state == "ready"


def test_finish_unfinished_detects_remote_completion(session):
    """客户端超时/断连，但远端其实已经跑完：必须报 completed 并带回输出。"""
    token = new_token()
    p = paths(token)
    Path(p["out"]).write_text("all done\n")
    Path(p["err"]).write_text("")
    Path(p["rc"]).write_text("0")
    session.start()

    result = session._finish_unfinished(
        session.slots[0], token, 1.0, time.time(), disconnected=False, interrupt=False
    )

    assert result.status == "completed"
    assert result.rc == 0
    assert "all done" in result.stdout
    assert result.note and "不要重跑" in result.note

    for key in ("out", "err", "rc"):
        Path(p[key]).unlink(missing_ok=True)
