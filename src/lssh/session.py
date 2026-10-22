"""一条长连接会话。

设计要点（踩过的坑都在这里）：

1. 只维持**一条** `ssh -tt` 长连接，命令都在这条连接里跑 —— 网关限流主要针对登录。
2. 远端可选挂一个 **detached** tmux 会话保存 shell 状态（cwd/env/后台任务），
   但**绝不 attach**：attach 读到的 pty 流是 tmux 渲染后的屏幕（光标定位 + 历史
   重绘），会丢换行、重复内容。命令通过 `tmux send-keys` 递进去。
3. 命令的 stdout/stderr/rc 全部写进临时文件，再由外层 shell `cat` 出来打标记，
   这样拿到的是原始字节，跟终端渲染彻底解耦。
"""

from __future__ import annotations

import os
import select
import subprocess
import threading
import time
from dataclasses import dataclass, field

from .protocol import (
    build_poll_lines,
    build_report_lines,
    build_script,
    build_write_lines,
    clean,
    marker,
    new_token,
    parse,
    paths,
)
from .transports import Spawned, Transport

READY = "ready"
STARTING = "starting"
DOWN = "down"
DIRTY = "dirty"

#: 外层 shell 的初始化：关回显、清提示符，避免干扰标记解析。
PRE_BOOTSTRAP = (
    "export TERM=xterm-256color; unset PROMPT_COMMAND; "
    "stty -echo -onlcr 2>/dev/null; PS1=''; PS2=''"
)

#: 准备 tmux：只建不 attach，关状态栏、放大历史。
TMUX_BOOTSTRAP = (
    "tmux has-session -t {session} 2>/dev/null || "
    "tmux new-session -d -s {session} 2>/dev/null; "
    "tmux set -g status off 2>/dev/null; "
    "tmux set -g visual-activity off 2>/dev/null; "
    "tmux set -g visual-bell off 2>/dev/null; "
    "tmux set -g history-limit 50000 2>/dev/null; "
    "tmux set -g default-terminal screen-256color 2>/dev/null"
)

#: pane 里的 shell 也要关回显、清提示符（不影响结果，纯属减少噪音）。
PANE_INIT = "stty -echo 2>/dev/null; PS1=''; PS2=''; export TERM=xterm-256color"


@dataclass
class Result:
    ok: bool
    rc: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    error: str | None = None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "rc": self.rc,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration": round(self.duration, 3),
            "error": self.error,
        }


@dataclass
class Session:
    host: str
    transport: Transport
    tmux: str | None = "lssh"
    default_timeout: float = 120.0
    connect_timeout: float = 60.0
    bootstrap_timeout: float = 40.0
    cols: int = 512
    rows: int = 50
    keep: int = 8_000_000

    state: str = field(default=DOWN, init=False)
    last_error: str | None = field(default=None, init=False)
    created_at: float | None = field(default=None, init=False)
    last_used: float = field(default_factory=time.time, init=False)

    _fd: int | None = field(default=None, init=False)
    _proc: subprocess.Popen[bytes] | None = field(default=None, init=False)
    _buf: str = field(default="", init=False)
    _cv: threading.Condition = field(default_factory=threading.Condition, init=False)
    _cmd_lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _start_lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _reader: threading.Thread | None = field(default=None, init=False)
    _cancel: threading.Event = field(default_factory=threading.Event, init=False)
    _strikes: int = field(default=0, init=False)

    # ------------------------------------------------------------------ 连接

    def start(self) -> None:
        with self._start_lock:
            if self.state == READY:
                return
            self._close_channel()
            self.state = STARTING
            spawned: Spawned = self.transport.spawn(cols=self.cols, rows=self.rows)
            self._fd = spawned.master_fd
            self._proc = spawned.proc
            self._buf = ""
            self._reader = threading.Thread(
                target=self._read_loop, name=f"lssh-read-{self.host}", daemon=True
            )
            self._reader.start()
            try:
                if not self._probe(self.connect_timeout):
                    raise RuntimeError("远端 shell 未就绪（认证失败或被限流？）")
                self._send_pre_bootstrap()
                if self.tmux:
                    self._send(
                        "if command -v tmux >/dev/null 2>&1; then "
                        + TMUX_BOOTSTRAP.format(session=self.tmux)
                        + "; fi\n"
                    )
                    self._send(
                        f"if command -v tmux >/dev/null 2>&1; then "
                        f"tmux send-keys -t {self.tmux} -l {_quote(PANE_INIT)}; "
                        f"tmux send-keys -t {self.tmux} Enter; fi\n"
                    )
                if not self._probe(self.bootstrap_timeout):
                    raise RuntimeError("远端 shell 初始化失败")
                self.state = READY
                self.created_at = time.time()
                self.last_used = time.time()
            except Exception as exc:
                self.state = DIRTY
                self.last_error = str(exc)
                self._close_channel()
                raise

    def _send_pre_bootstrap(self) -> None:
        self._send(PRE_BOOTSTRAP + "\n")
        time.sleep(0.05)

    def stop(self) -> None:
        self.state = DOWN
        self._close_channel()

    def _close_channel(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        if self._proc is not None:
            try:
                self._proc.terminate()
            except OSError:
                pass
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    self._proc.kill()
                except OSError:
                    pass
            self._proc = None

    # ------------------------------------------------------------------ I/O

    def _read_loop(self) -> None:
        misses = 0
        while True:
            fd = self._fd
            if fd is None:
                return
            try:
                ready, _, _ = select.select([fd], [], [], 0.5)
            except (OSError, ValueError):
                return
            if not ready:
                proc = self._proc
                if proc is not None and proc.poll() is not None:
                    with self._cv:
                        if self.state == READY:
                            self.state = DIRTY
                            self.last_error = "连接已断开"
                        self._cv.notify_all()
                    return
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                proc = self._proc
                if proc is not None and proc.poll() is None:
                    misses += 1
                    if misses < 100:
                        time.sleep(0.02)
                        continue
                return
            misses = 0
            text = clean(chunk)
            with self._cv:
                self._buf += text
                if len(self._buf) > self.keep:
                    self._buf = self._buf[-self.keep // 2 :]
                self._cv.notify_all()

    def _send(self, data: str | bytes) -> None:
        if self._fd is None:
            raise RuntimeError("会话未连接")
        raw = data.encode() if isinstance(data, str) else data
        view = memoryview(raw)
        while view:
            view = view[os.write(self._fd, view) :]

    def _probe(self, timeout: float) -> bool:
        """反复投喂带 -OK 的标记，直到远端 shell **真的执行**它。

        必须要求占整行：终端回显里是字面量 ``\\n``，只有执行结果才是真实换行。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            token = new_token()
            pattern = marker(token, "-OK")
            with self._cv:
                start = len(self._buf)
            try:
                self._send(f"printf '%s-OK\\n' {token}\n")
            except (OSError, RuntimeError):
                return False
            until = time.time() + 2.0
            with self._cv:
                while time.time() < until:
                    if pattern.search(self._buf[start:]):
                        return True
                    if self.state == DIRTY and self._fd is None:
                        return False
                    self._cv.wait(0.2)
        return False

    # ------------------------------------------------------------------ 执行

    def run(self, cmd: str, timeout: float | None = None) -> Result:
        timeout = float(timeout or self.default_timeout)
        if self.state != READY:
            try:
                self.start()
            except Exception as exc:
                return Result(ok=False, error=f"连接失败: {exc}")

        with self._cmd_lock:
            token = new_token()
            p = paths(token)
            script = build_script(token, cmd)
            self._cancel.clear()
            with self._cv:
                offset = len(self._buf)
            started = time.time()
            self.last_used = started
            try:
                for line in build_write_lines(token, script):
                    self._send(line + "\n")
                self._send(f"rm -f {p['out']} {p['err']} {p['rc']} {p['done']}\n")
                self._trigger(token, timeout)
                if self.tmux:
                    # 命令在 pane 里跑，外层 shell 空闲，才能用轮询实现精确超时
                    for line in build_poll_lines(token, timeout):
                        self._send(line + "\n")
                for line in build_report_lines(token):
                    self._send(line + "\n")
            except (OSError, RuntimeError) as exc:
                self.state = DIRTY
                return Result(ok=False, error=f"写入失败: {exc}")

            parsed = None
            deadline = started + timeout + 30
            with self._cv:
                while True:
                    parsed = parse(self._buf[offset:], token)
                    if parsed is not None:
                        break
                    if self._cancel.is_set():
                        break
                    left = deadline - time.time()
                    if left <= 0:
                        break
                    self._cv.wait(min(left, 0.5))

            self.last_used = time.time()
            if parsed is None and self._cancel.is_set():
                return Result(
                    ok=False,
                    duration=time.time() - started,
                    error="已中断（Ctrl-C）",
                )
            if parsed is None:
                self._abort(token)
                self.state = DIRTY
                self.last_error = f"命令超时（{timeout}s）"
                return Result(
                    ok=False,
                    stdout=self._buf[offset:][:2000].strip("\n"),
                    duration=time.time() - started,
                    error=f"命令超时（{timeout}s），会话已重置",
                )

            rc, stdout, stderr = parsed
            elapsed = time.time() - started
            if rc == -1:  # rc 文件没写出来 == 轮询超时
                self._abort(token)
                self._strikes += 1
                if self._strikes >= 2:
                    # 连续两次超时基本等于会话僵死（网关吞了 relay 或 pane 卡住），
                    # 主动断开，让下一条命令重连，而不是继续对着死通道超时。
                    self.state = DIRTY
                    self.last_error = "连续超时，会话已标记为需要重连"
                return Result(
                    ok=False,
                    stdout=stdout,
                    stderr=stderr,
                    duration=elapsed,
                    error=f"命令超时（{timeout}s），已发送 Ctrl-C",
                )
            self._strikes = 0
            if not self._verify(token, self._buf[offset:]):
                return Result(
                    ok=False,
                    rc=rc,
                    stdout=stdout,
                    stderr=stderr,
                    duration=elapsed,
                    error="输出校验失败（长度/md5 不一致，可能被网关截断）",
                )
            return Result(ok=True, rc=rc, stdout=stdout, stderr=stderr, duration=elapsed)

    def _verify(self, token: str, segment: str) -> bool:
        """用远端报告的长度/md5 校验收到的内容，防止静默截断。"""
        import hashlib

        from .protocol import parse_raw, strip_separator

        raw = parse_raw(segment, token)
        if raw is None:
            return True
        _, raw_out, raw_err = raw
        texts = {"SZ": strip_separator(raw_out), "MZ": strip_separator(raw_err)}
        for suffix, text in texts.items():
            found = marker(token, r" " + suffix + r" (\d+) ([0-9a-f]{32})").search(segment)
            if not found:
                return True  # 老远端/没有校验信息就不阻塞
            size, digest = int(found.group(1)), found.group(2)
            raw = text.encode("utf-8")
            if len(raw) != size or hashlib.md5(raw).hexdigest() != digest:
                return False
        return True

    def _trigger(self, token: str, timeout: float) -> None:
        script = paths(token)["script"]
        if self.tmux:
            self._send(f"tmux send-keys -t {self.tmux} -l {_quote('. ' + script)}\n")
            self._send(f"tmux send-keys -t {self.tmux} Enter\n")
        else:
            self._send(f". {script}\n")

    def _abort(self, token: str) -> None:
        try:
            if self.tmux:
                self._send(f"tmux send-keys -t {self.tmux} C-c 2>/dev/null\n")
            self._send("\x03")
        except (OSError, RuntimeError):
            pass

    def interrupt(self) -> None:
        self._cancel.set()
        try:
            if self.tmux:
                self._send(f"tmux send-keys -t {self.tmux} C-c 2>/dev/null\n")
            self._send("\x03")
        except (OSError, RuntimeError):
            pass

    def info(self) -> dict:
        return {
            "host": self.host,
            "transport": self.transport.describe(),
            "state": self.state,
            "busy": self._cmd_lock.locked(),
            "idle": round(time.time() - self.last_used, 1),
            "uptime": round(time.time() - self.created_at, 1) if self.created_at else 0,
            "last_error": self.last_error,
        }


def _quote(text: str) -> str:
    """给 tmux send-keys -l 用的单引号转义（内容只含路径和简单命令）。"""
    return "'" + text.replace("'", "'\\''") + "'"
