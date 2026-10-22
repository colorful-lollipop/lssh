from __future__ import annotations

import subprocess
import sys
import tempfile
import time

import pytest

from lssh.protocol import build_script, clean, new_token, parse
from lssh.session import Session
from lssh.transports import LocalTransport


def local_session(**kwargs) -> Session:
    return Session(host="local", transport=LocalTransport(), tmux=None, **kwargs)


@pytest.fixture()
def tmux_session(tmp_path):
    """走真实 tmux 的会话（超时/中断只有这条路能精确控制）。"""
    name = "lssh-test-" + next(tempfile._get_candidate_names())  # noqa: SLF001
    s = Session(host="local-tmux", transport=LocalTransport(), tmux=name)
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


def test_two_timeouts_mark_dirty(tmux_session):
    assert not tmux_session.run("sleep 5", timeout=0.5).ok
    assert not tmux_session.run("sleep 5", timeout=0.5).ok
    assert tmux_session.state == "dirty"
