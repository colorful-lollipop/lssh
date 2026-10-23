"""一条长连接会话 + 多个并发执行槽位（slot）。

设计要点（踩过的坑都在这里）：

1. 只维持**一条** `ssh -tt` 长连接，所有命令都走这条连接 —— 网关限流主要针对
   登录，多开一条连接就多一次被限流/被踢的机会。
2. 远端可选挂一个 **detached** tmux 会话保存 shell 状态（cwd/env/后台任务），
   但**绝不 attach**：attach 读到的 pty 流是 tmux 渲染后的屏幕（光标定位 + 历史
   重绘），会丢换行、重复内容。命令通过 `tmux send-keys` 递进**指定 window**。
3. **并发靠"一个 window 一个槽位 + 通道分片"**：
   * 远端 tmux 会话里为每个槽位开一个 window（`lssh:w0`、`lssh:w1`…），
     一条命令独占一个 window，互不打断；因此一条 `sleep 3600` 挂住时，
     另一个 window 仍能跑"查日志"这类命令。
   * 共享的 SSH 通道**只做短交互**：投递、探测、以及 0.2s 一片的轮询。
     Python 侧每片结束就放开通道锁，别的槽位的命令才能插进来。
     旧实现里轮询在外面一口气阻塞到 timeout，通道被单条命令独占 —— 那才是
     "lssh 单条命令串行"的病根。
4. 顺序调用的粘性：连续的命令优先复用上一次的槽位，`cd`/`export` 才符合直觉；
   只有它忙的时候才切到空闲槽位（此时 cwd 各自独立，用绝对路径或自带 `cd`）。
5. 命令的 stdout/stderr/rc 全部写进临时文件，再由外层 shell `cat` 出来打标记，
   这样拿到的是原始字节，跟终端渲染彻底解耦。
"""

from __future__ import annotations

import os
import re
import select
import subprocess
import threading
import time
from dataclasses import dataclass, field

from .protocol import (
    build_report_block,
    build_script,
    build_slice_lines,
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

#: pane 里的 shell 也要关回显、清提示符（不影响结果，纯属减少噪音）。
PANE_INIT = "stty -echo 2>/dev/null; PS1=''; PS2=''; export TERM=xterm-256color"

#: 远端 window 里跑的 shell —— 必须**干净**：不读 rc。
#: 用户 rc 里的行编辑器（ble.sh 等）会把 `send-keys` 打进去的字符吃进自己的
#: 编辑缓冲：Enter 变成"插入换行"，命令永远不执行，而且屏幕上看起来一切正常。
#: 实测症状：命令"莫名其妙就超时"、多次重投会在 pane 里堆出 6 行同样的命令。
#: 所以 window 由我们自己用 bash --noprofile --norc（退化为 sh）拉起。
CLEAN_SHELL = (
    "if command -v bash >/dev/null 2>&1; then exec bash --noprofile --norc; else exec sh; fi"
)

#: pane 前景进程是这些名字时，才认为 pane 空闲可投喂命令。
#: 其余情况（apt-get/vim/tail -f/被 Ctrl-Z 挂起的进程…）一律视为「忙」：
#: 此时 send-keys 打进去的内容会被当成那个进程的 stdin，命令静默丢失。
IDLE_COMMANDS = frozenset(
    {"bash", "sh", "dash", "zsh", "ksh", "ash", "fish", "tcsh", "csh", "busybox"}
)

#: 回收 pane 后等待它回到 shell 的秒数。
RECLAIM_WAIT = 1.5

#: 轮询切片长度（秒）。一个切片 = 通道被单条命令占用的**最长时间上限**，
#: 决定了"长任务挂住时另一条命令多久能拿到通道"。
SLICE = 0.2

#: 单片轮询的等待余量（含同通道上其它命令的排队延迟）。
SLICE_WAIT = SLICE + 1.5


def _tmux_init_lines(session: str, count: int) -> list[str]:
    """保证 tmux 会话与 w0..w{count-1} 这些 window 都存在（只建不 attach）。"""
    listing = f"tmux list-windows -t {session} -F '#{{window_name}}' 2>/dev/null"
    shell = _quote(CLEAN_SHELL)
    lines = [
        "if command -v tmux >/dev/null 2>&1; then",
        # 新窗口都用干净 shell 起；老会话（0.4.x 只有一个无名 window）先认领成 w0。
        f"  tmux has-session -t {session} 2>/dev/null || "
        f"tmux new-session -d -s {session} -n w0 {shell} 2>/dev/null;",
        f"  tmux set-option -t {session} default-command {shell} 2>/dev/null;",
        "  tmux set -g status off 2>/dev/null;",
        "  tmux set -g visual-activity off 2>/dev/null;",
        "  tmux set -g visual-bell off 2>/dev/null;",
        "  tmux set -g history-limit 50000 2>/dev/null;",
        "  tmux set -g default-terminal screen-256color 2>/dev/null;",
        f"  {listing} | grep -qx w0 || tmux rename-window -t {session}:0 w0 2>/dev/null;",
    ]
    for index in range(1, count):
        lines.append(
            f"  {listing} | grep -qx w{index} || "
            f"tmux new-window -d -t {session}: -n w{index} {shell} 2>/dev/null;"
        )
    lines.append("fi")
    return lines


@dataclass
class Result:
    ok: bool
    rc: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    error: str | None = None
    #: completed / failed / timeout / disconnected / busy / error / cancelled
    #: 关键语义：timeout = **客户端放弃等待**，不等于命令失败或没执行。
    status: str = "ok"
    #: 补充说明（例如"客户端超时但远端命令其实已成功"）
    note: str | None = None
    #: 远端现场：脚本/输出文件路径、进程是否存活等，超时/断连时用于自证
    remote: dict | None = None
    #: 这条命令实际用掉的槽位（远端 window 序号，便于人工排障）
    slot: int | None = None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "status": self.status,
            "rc": self.rc,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration": round(self.duration, 3),
            "error": self.error,
            "note": self.note,
            "remote": self.remote,
            "slot": self.slot,
        }


@dataclass
class Slot:
    """一个执行槽位：远端 tmux 里一个 window，同一时刻只跑一条命令。"""

    index: int
    tmux: str | None = None
    #: 占位锁：谁拿到谁用这个槽位（等价于"这个 window 正在被一条命令使用"）
    lock: threading.Lock = field(default_factory=threading.Lock)
    #: 只打断**本槽位**那条命令的取消信号
    cancel: threading.Event = field(default_factory=threading.Event)
    #: 客户端请求 id，让 `interrupt` 能精确找到要打断哪一条
    req_id: str | None = None
    token: str | None = None
    cmd: str | None = None
    since: float | None = None
    #: 上一条命令客户端超时了、但远端可能还在跑 —— 这时绝不能往里投喂新命令
    pending: dict | None = None

    @property
    def target(self) -> str | None:
        """tmux target（`lssh:w0`）。没有 tmux 时命令直接跑在外层 shell 里。"""
        return f"{self.tmux}:w{self.index}" if self.tmux else None

    def info(self) -> dict:
        running = None
        if self.cmd and self.since:
            stripped = self.cmd.strip()
            first = stripped.splitlines()[0][:80] if stripped else ""
            running = {"cmd": first, "for": round(time.time() - self.since, 1)}
            if self.token:
                running["token"] = self.token
        pending = None
        if self.pending:
            info = self.pending
            cmd = (info.get("cmd") or "").strip()
            pending = {
                "cmd": cmd.splitlines()[0][:80] if cmd else "",
                "for": round(time.time() - info["since"], 1),
                "unconfirmed": bool(info.get("unconfirmed")),
            }
        return {
            "slot": self.index,
            "target": self.target,
            "busy": self.lock.locked(),
            "running": running,
            "pending": pending,
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
    #: 并发槽位上限（远端 tmux window 数）。没有 tmux 时恒为 1。
    max_slots: int = 3

    state: str = field(default=DOWN, init=False)
    last_error: str | None = field(default=None, init=False)
    created_at: float | None = field(default=None, init=False)
    last_used: float = field(default_factory=time.time, init=False)

    _fd: int | None = field(default=None, init=False)
    _proc: subprocess.Popen[bytes] | None = field(default=None, init=False)
    _buf: str = field(default="", init=False)
    #: 缓冲区**头部被裁掉**的字符数。所有位置都用「绝对偏移」= _buf_base + 下标，
    #: 这样裁剪缓冲区不会让 in-flight 命令记下的 offset 失效（曾导致解析永远失败）。
    _buf_base: int = field(default=0, init=False)
    _cv: threading.Condition = field(default_factory=threading.Condition, init=False)
    #: 通道互斥：**同一时刻只有一条交互在往 pty 里写**（多行交互必须整体原子）。
    #: 注意它绝不能被长等待持有 —— 那等于把并发又改回串行。
    _io_lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    _start_lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _reader: threading.Thread | None = field(default=None, init=False)
    #: in-flight 命令的绝对起点（裁剪缓冲区时不许越过最老的那个）
    _inflight: dict[str, int] = field(default_factory=dict, init=False)
    _strikes: int = field(default=0, init=False)
    #: 上一次用过的槽位：顺序命令粘在这里，`cd`/`export` 才有意义
    _last_slot: int = field(default=0, init=False)
    slots: list[Slot] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        count = max(1, int(self.max_slots)) if self.tmux else 1
        self.slots = [Slot(index=index, tmux=self.tmux) for index in range(count)]

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
                    self._ensure_tmux()
                    # 上一轮可能留下「还在跑东西」的 window（长命令、被挂起的
                    # 进程、vim…）。不清掉它，后面所有 send-keys 都会被当成那个
                    # 进程的 stdin，命令静默丢失 —— 这是"莫名其妙就超时"的根源。
                    for slot in self.slots:
                        if not self._pane_idle(slot):
                            self._reclaim(slot)
                        self._pane_init(slot)
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
        with self._io_lock:
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
                # 通道真的没了：必须**立刻**把状态置脏并叫醒等待方。
                # 旧实现直接 return，run() 因此空等到 timeout（"命令早就跑完了却报超时"）。
                with self._cv:
                    if self.state in (READY, STARTING):
                        self.state = DIRTY
                        self.last_error = "连接已断开" + _cause(self._buf)
                    self._cv.notify_all()
                return
            misses = 0
            text = clean(chunk)
            with self._cv:
                self._buf += text
                self._trim_locked()
                self._cv.notify_all()

    def _send(self, data: str | bytes) -> None:
        if self._fd is None:
            raise RuntimeError("会话未连接")
        raw = data.encode() if isinstance(data, str) else data
        view = memoryview(raw)
        while view:
            view = view[os.write(self._fd, view) :]

    def _exchange(self, data: str) -> int:
        """原子地投递一段**完整**交互（多行也算一次），返回缓冲区起点。

        多行交互必须整体原子：两个线程的行交错进同一个 shell 会把脚本写坏。
        """
        with self._io_lock:
            start = self._pos()
            self._send(data if data.endswith("\n") else data + "\n")
            return start

    # ---------------------------------------------------------------- 缓冲区

    def _pos(self) -> int:
        """当前缓冲区末尾的**绝对**偏移（头部被裁掉的部分也算在内）。"""
        with self._cv:
            return self._buf_base + len(self._buf)

    def _segment(self, start_abs: int) -> str:
        """取绝对偏移 start_abs 之后的片段（裁剪过也不会错位）。"""
        with self._cv:
            index = max(0, start_abs - self._buf_base)
            return self._buf[index:]

    def _trim_locked(self) -> None:
        """裁剪过长的缓冲区，但**绝不越过 in-flight 命令的起点**。

        旧实现直接 `self._buf = self._buf[-keep//2:]`，会让 run() 记下的相对
        offset 失效，表现为「明明有输出却解析不到 → 超时/空输出」。
        现在可能有多个命令同时在飞，保护线取最老的那个。
        """
        overflow = len(self._buf) - self.keep
        if overflow <= 0:
            return
        if self._inflight:
            overflow = min(overflow, min(self._inflight.values()) - self._buf_base)
        if overflow <= 0:
            return
        self._buf = self._buf[overflow:]
        self._buf_base += overflow

    def _probe(self, timeout: float) -> bool:
        """反复投喂带 -OK 的标记，直到远端 shell **真的执行**它。

        必须要求占整行：终端回显里是字面量 ``\\n``，只有执行结果才是真实换行。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            token = new_token()
            pattern = marker(token, "-OK")
            try:
                start = self._exchange(f"printf '%s-OK\\n' {token}\n")
            except (OSError, RuntimeError):
                return False
            until = time.time() + 2.0
            with self._cv:
                while time.time() < until:
                    if pattern.search(self._segment(start)):
                        return True
                    if self.state == DIRTY and self._fd is None:
                        return False
                    self._cv.wait(0.2)
        return False

    def _await(self, pattern: re.Pattern[str], start: int, timeout: float) -> bool:
        deadline = time.time() + timeout
        with self._cv:
            while time.time() < deadline:
                if pattern.search(self._segment(start)):
                    return True
                if self._fd is None:
                    return False
                self._cv.wait(0.1)
        return False

    # ------------------------------------------------------------- tmux 槽位

    def _ensure_tmux(self) -> None:
        if not self.tmux:
            return
        lines = _tmux_init_lines(self.tmux, len(self.slots))
        with self._io_lock:
            for line in lines:
                self._send(line + "\n")
        time.sleep(0.2)

    def _pane_command(self, slot: Slot, timeout: float = 4.0) -> str | None:
        """问 tmux：这个 window 的前景进程是什么。外层 shell 永远空闲，所以问得到。"""
        if not slot.target:
            return None
        token = new_token()
        pattern = re.compile(re.escape(token) + r"-P ([A-Za-z0-9._+-]*)")
        expr = (
            f"tmux list-panes -t {slot.target} -F '#{{pane_current_command}}' 2>/dev/null | head -1"
        )
        try:
            start = self._exchange(f"printf '{token}-P %s\\n' \"$({expr})\"\n")
        except (OSError, RuntimeError):
            return None
        if not self._await(pattern, start, timeout):
            return None
        found = pattern.search(self._segment(start))
        return found.group(1).strip() if found else None

    def _pane_idle(self, slot: Slot) -> bool:
        """这个槽位现在能不能安全接收命令。

        只按 `#{pane_current_command}` 判断：往外层 pane 里投喂探测标记是**危险**的
        —— 命令还在跑时字符会落进它的 stdin，而且遇到 ble.sh 这类行编辑器还会把
        shell 搞进续行状态，后续命令全被吞掉。
        更细的"我们自己那条命令还在不在跑"交给 rc 文件与 /proc 判断。
        """
        if not slot.target:
            return True
        command = self._pane_command(slot)
        # 查不到（会话没了 / list-panes 超时）时按"不空闲"处理，
        # 让 _reclaim 走"重建 window"的分支 —— 死 pane 必须能自愈。
        if command is None or command == "":
            return False
        return command in IDLE_COMMANDS

    def _pane_init(self, slot: Slot) -> None:
        if not slot.target:
            return
        with self._io_lock:
            self._send(
                f"tmux send-keys -t {slot.target} -l {_quote(PANE_INIT)} 2>/dev/null; "
                f"tmux send-keys -t {slot.target} Enter 2>/dev/null\n"
            )

    def _rc_of(self, token: str, timeout: float = 4.0) -> int | None:
        """轻量查一次 rc 文件（不往 pane 里敲字符，绝不打扰正在跑的命令）。"""
        p = paths(token)
        probe = new_token()
        pattern = re.compile(re.escape(probe) + r"-RC (\S+)")
        try:
            start = self._exchange(
                f"__rc=$(cat {p['rc']} 2>/dev/null || echo X); "
                f"printf '{probe}-RC %s\\n' \"$__rc\"\n"
            )
        except (OSError, RuntimeError):
            return None
        if not self._await(pattern, start, timeout):
            return None
        found = pattern.search(self._segment(start))
        if not found or found.group(1) == "X":
            return None
        return int(found.group(1))

    def _reclaim(self, slot: Slot, *, wait: float = RECLAIM_WAIT, force: bool = False) -> bool:
        """把**这一个** window 拉回 shell：先 Ctrl-C，不行就杀掉该 window 重建。

        绝不能 `kill-session` —— 那会连坐同一会话里其它槽位正在跑的长任务，
        而"长任务挂住、另一条命令照常用"恰恰是并发的目的。
        force=True 时不看 pane 状态直接重建（用于"这个 window 在吞字符"的自愈）。
        """
        if not slot.target:
            return True
        if not force:
            if self._pane_idle(slot):
                return True
            with self._io_lock:
                self._send(f"tmux send-keys -t {slot.target} C-c 2>/dev/null\n")
            time.sleep(wait)
            if self._pane_idle(slot):
                return True
        self._ensure_tmux()
        with self._io_lock:
            self._send(f"tmux kill-window -t {slot.target} 2>/dev/null\n")
            self._send(
                f"tmux new-window -d -t {self.tmux}: -n w{slot.index} "
                f"{_quote(CLEAN_SHELL)} 2>/dev/null\n"
            )
        time.sleep(0.3)
        self._pane_init(slot)
        time.sleep(wait)
        return self._pane_idle(slot)

    # ------------------------------------------------------------------ 执行

    def run(
        self,
        cmd: str,
        timeout: float | None = None,
        wait: float = 0.0,
        interrupt_on_timeout: bool = False,
        *,
        req_id: str | None = None,
        slot: int | None = None,
    ) -> Result:
        """跑一条命令（自动挑一个空闲槽位）。

        wait: 全部槽位都忙时最多等多少秒（0 = 立刻报忙，并说明每个槽位在跑什么）。
        slot: 指定槽位序号（调试/粘性用），不指定就自动挑。
        req_id: 客户端请求 id，`interrupt` 靠它精确打断这一条。
        interrupt_on_timeout: 超时后是否给远端发 Ctrl-C。默认**不发** ——
            对 mkfs/dd/rsync 这类破坏性长任务，误发 Ctrl-C 比超时本身更危险。
        """
        timeout = float(timeout or self.default_timeout)
        if self.state != READY:
            try:
                self.start()
            except Exception as exc:
                return Result(ok=False, status="error", error=f"连接失败: {exc}")

        chosen = self._pick_slot(wait, prefer=slot)
        if chosen is None:
            return Result(ok=False, status="busy", error=self._busy_message())

        chosen.cancel.clear()
        chosen.req_id = req_id
        try:
            return self._run_slot(chosen, cmd, timeout, interrupt_on_timeout)
        finally:
            chosen.cmd = chosen.since = chosen.token = None
            chosen.req_id = None
            chosen.lock.release()

    def _pick_slot(self, wait: float, prefer: int | None = None) -> Slot | None:
        """挑一个槽位：先看指定/上次用过的，再看其它空闲的。

        粘性很重要：顺序执行的 `cd /tmp` → `pwd` 必须落在同一个 window，
        否则 cwd 就不再"保持"了。只有它忙着时才会切槽位。
        """
        # `--slot N` 是硬约束：钉在 N 上等，不偷偷换到别的槽位
        if prefer is not None and 0 <= prefer < len(self.slots):
            pinned = self.slots[prefer]
            deadline = time.time() + max(0.0, wait)
            while not pinned.lock.acquire(blocking=False):
                if time.time() >= deadline:
                    return None
                time.sleep(0.05)
            self._last_slot = pinned.index
            return pinned

        order: list[Slot] = []
        if 0 <= self._last_slot < len(self.slots):
            order.append(self.slots[self._last_slot])
        order += [s for s in self.slots if s not in order]

        deadline = time.time() + max(0.0, wait)
        while True:
            acquired: list[Slot] = []
            for candidate in order:
                if candidate.lock.acquire(blocking=False):
                    acquired.append(candidate)
            if acquired:
                # 优先挑"客户端看来干净"的槽位：上一次超时后远端还压着命令的不算
                fresh = [s for s in acquired if s.pending is None] or acquired
                chosen = fresh[0]
                for other in acquired:
                    if other is not chosen:
                        other.lock.release()
                self._last_slot = chosen.index
                return chosen
            if time.time() >= deadline:
                return None
            time.sleep(0.05)

    def _busy_message(self) -> str:
        parts = []
        for slot in self.slots:
            if slot.cmd and slot.since:
                stripped = slot.cmd.strip()
                first = stripped.splitlines()[0][:60] if stripped else ""
                parts.append(f"w{slot.index}: {first!r} 已 {time.time() - slot.since:.0f}s")
            elif slot.pending:
                info = slot.pending
                cmd = (info.get("cmd") or "").strip()
                first = cmd.splitlines()[0][:60] if cmd else ""
                parts.append(
                    f"w{slot.index}: 上一条超时后仍在远端跑 {first!r} "
                    f"已 {time.time() - info['since']:.0f}s"
                )
            elif slot.lock.locked():
                parts.append(f"w{slot.index}: 正在准备命令")
        detail = "；".join(parts) or "全部槽位被占用"
        return (
            f"会话忙（{len(self.slots)} 个槽位都没空：{detail}）。"
            f"用 lssh --wait 30 排队，或 lssh --kill {self.host} 强制回收"
        )

    def _pending_message(self, slot: Slot) -> str:
        info = slot.pending or {}
        cmd = (info.get("cmd") or "").strip()
        first = cmd.splitlines()[0][:60] if cmd else ""
        since = info.get("since") or time.time()
        tail = (
            "仍在远端运行"
            if not info.get("unconfirmed")
            else "可能仍在远端运行（连接中断时未能确认）"
        )
        return (
            f"槽位 w{slot.index} 忙（上一条命令{tail}：{first!r}，"
            f"已 {time.time() - since:.0f}s）。"
            f"用 lssh --list 观察，或 lssh --kill {self.host} 强制终止"
        )

    def _run_slot(
        self, slot: Slot, cmd: str, timeout: float, interrupt_on_timeout: bool = False
    ) -> Result:
        token = new_token()
        p = paths(token)
        script = build_script(token, cmd)

        # pane 里可能还压着上一条没跑完的东西（比如被 Ctrl-Z 挂起的进程），
        # 这时 send-keys 打进去的字符会被它当成 stdin，命令静默丢失。
        if slot.target:
            if slot.pending is not None:
                # 上一条超时的命令可能还在跑：先看 rc 文件判断它结束没，
                # 绝不往它里面敲字符。
                if self._rc_of(slot.pending["token"]) is None:
                    return Result(
                        ok=False, status="busy", error=self._pending_message(slot), slot=slot.index
                    )
                slot.pending = None
            if not self._pane_idle(slot) and not self._reclaim(slot):
                return Result(
                    ok=False,
                    status="busy",
                    error=f"槽位 w{slot.index} 被占用且无法回收（可用 lssh --kill 重建会话）",
                    slot=slot.index,
                )

        offset = self._pos()
        started = time.time()
        self.last_used = started
        slot.token, slot.cmd, slot.since = token, cmd, started
        self._inflight[token] = offset
        try:
            try:
                if not self._write_script(token, script):
                    return Result(
                        ok=False,
                        status="error",
                        error="远端脚本写入失败（连续 3 次没落盘，通道可能丢了字节）",
                        slot=slot.index,
                    )
                with self._io_lock:
                    self._send(
                        f"rm -f {p['out']} {p['err']} {p['rc']} {p['done']} {p['started']}\n"
                    )
                    self._trigger(slot, token)
                self._ensure_started(slot, token)
            except (OSError, RuntimeError) as exc:
                self.state = DIRTY
                return Result(ok=False, error=f"写入失败: {exc}", slot=slot.index)

            return self._poll_slot(slot, token, timeout, started, offset, interrupt_on_timeout)
        finally:
            self._inflight.pop(token, None)

    def _poll_slot(
        self,
        slot: Slot,
        token: str,
        timeout: float,
        started: float,
        offset: int,
        interrupt_on_timeout: bool,
    ) -> Result:
        """分片轮询：每片只占通道 SLICE 秒，其余时间让给别的槽位。"""
        slice_lines = build_slice_lines(token, SLICE)
        report_lines = build_report_block(token, cleanup=False)
        reason = "deadline"
        while True:
            try:
                with self._io_lock:
                    for line in slice_lines + report_lines:
                        self._send(line + "\n")
            except (OSError, RuntimeError) as exc:
                self.state = DIRTY
                return Result(ok=False, error=f"写入失败: {exc}", slot=slot.index)

            parsed = self._wait_parsed(offset, token, SLICE_WAIT, slot)
            if parsed is not None:
                break
            if slot.cancel.is_set():
                reason = "cancelled"
                break
            # 通道断了就别再等：旧实现会在这里空等到 600s，
            # 于是"命令早就跑完了"被报成"超时"。
            if self._fd is None or self.state in (DIRTY, DOWN):
                reason = "disconnected"
                break
            if time.time() >= started + timeout:
                reason = "deadline"
                break

        self.last_used = time.time()
        if parsed is None:
            if reason == "cancelled":
                return Result(
                    ok=False,
                    status="cancelled",
                    duration=time.time() - started,
                    error="已中断（Ctrl-C）",
                    slot=slot.index,
                )
            return self._finish_unfinished(
                slot,
                token,
                timeout,
                started,
                disconnected=(reason == "disconnected"),
                interrupt=interrupt_on_timeout,
                partial=self._segment(offset),
            )

        rc, stdout, stderr = parsed
        elapsed = time.time() - started
        if rc == -1:  # rc 文件没写出来 == 轮到超时了但命令没完成
            return self._finish_unfinished(
                slot, token, timeout, started, disconnected=False, interrupt=interrupt_on_timeout
            )
        self._strikes = 0
        if not self._verify(token, self._segment(offset)):
            self._cleanup(token)
            return Result(
                ok=False,
                rc=rc,
                status="truncated",
                stdout=stdout,
                stderr=stderr,
                duration=elapsed,
                error="输出校验失败（长度/md5 不一致，可能被网关截断）",
                slot=slot.index,
            )
        self._cleanup(token)
        return Result(
            ok=rc == 0,
            rc=rc,
            status="completed" if rc == 0 else "failed",
            stdout=stdout,
            stderr=stderr,
            duration=elapsed,
            slot=slot.index,
        )

    def _wait_parsed(
        self, offset: int, token: str, wait: float, slot: Slot | None = None
    ) -> tuple[int, str, str] | None:
        """等本片的结果标记（不持通道锁）。"""
        deadline = time.time() + wait
        with self._cv:
            while True:
                segment = self._segment(offset)
                if token in segment:  # 先做廉价子串判断，避免每轮都对整段跑正则
                    parsed = parse(segment, token)
                    if parsed is not None:
                        return parsed
                if slot is not None and slot.cancel.is_set():
                    return None
                if self._fd is None or self.state in (DIRTY, DOWN):
                    return None
                left = deadline - time.time()
                if left <= 0:
                    return None
                self._cv.wait(min(left, 0.1))

    # ------------------------------------------------------- 未完命令的收尾

    def _finish_unfinished(
        self,
        slot: Slot,
        token: str,
        timeout: float,
        started: float,
        *,
        disconnected: bool,
        interrupt: bool,
        partial: str = "",
    ) -> Result:
        """命令没等到结束标记时的收尾。

        原则：
        * **先查现场再下结论** —— 绝大多数"超时"其实是客户端没收到结束信号，
          远端早就跑完了（反馈里 4 块盘的 mkfs 就是这样）。
        * 默认**不发 Ctrl-C**：对 mkfs/dd/rsync 误打断比超时本身更危险。
        * 超时也回传已落盘的输出，并显式标注截断。
        * 占住**这个槽位**（不是整个会话）：其它槽位照常可用来查询。
        """
        elapsed = time.time() - started
        if interrupt:
            self._abort(slot)
        info = self._postmortem(token)
        if info.get("rc") is None and not info.get("stdout"):
            # 命令可能刚起来还没来得及产出：稍等一下再探一次，尽量把已有输出带回去
            time.sleep(1.0)
            info = self._postmortem(token)
        p = paths(token)
        remote = {
            "script": p["script"],
            "stdout_file": p["out"],
            "stderr_file": p["err"],
            "rc_file": p["rc"],
            "reachable": info.get("reachable", False),
            "alive": info.get("alive", False),
        }
        stdout = info.get("stdout") or ""
        stderr = info.get("stderr") or ""
        if info.get("truncated"):
            stdout += "\n<<<lssh: 输出在超时点被截断>>>"

        if info.get("rc") is not None:
            rc = int(info["rc"])
            self._cleanup(token)
            self._strikes = 0
            return Result(
                ok=rc == 0,
                rc=rc,
                status="completed",
                stdout=stdout,
                stderr=stderr,
                duration=elapsed,
                error=None if rc == 0 else f"命令已在远端结束，退出码 {rc}",
                note="客户端等待超时/断连，但命令已在远端执行完毕 —— 不要重跑",
                remote=remote,
                slot=slot.index,
            )

        if info.get("alive"):
            slot.pending = {"token": token, "cmd": self._running_text(slot), "since": started}
            self.last_error = f"命令仍在远端运行（{timeout}s 后客户端放弃等待）"
            return Result(
                ok=False,
                rc=None,
                status="running",
                stdout=stdout,
                stderr=stderr,
                duration=elapsed,
                error=(
                    f"超时：命令仍在远端执行（{timeout}s 后客户端放弃等待"
                    + ("" if interrupt else "，未发送 Ctrl-C")
                    + "）"
                ),
                note=(
                    f"远端脚本仍在 w{slot.index} 里跑，勿重跑；"
                    "其它槽位仍可用于查询（lssh --list 观察 / lssh --kill 强制回收）"
                ),
                remote=remote,
                slot=slot.index,
            )

        # 连不上/探测不回包：只能如实说"不知道"，绝不臆测成功或失败。
        # 同时占住这个槽位：下一条命令先去核对 rc，别让回收把可能还在跑的任务杀掉。
        slot.pending = {
            "token": token,
            "cmd": self._running_text(slot),
            "since": started,
            "unconfirmed": True,
        }
        self.last_error = "超时且无法确认远端状态"
        if not info.get("reachable"):
            # 只有**通道**本身坏了才算"僵死"；单纯的命令跑得久不该牵连整个会话
            self._strikes += 1
            if self._strikes >= 2:
                self.state = DIRTY
                self.last_error = "连续探测不到远端现场，会话已标记为需要重连"
        stdout = stdout or partial.strip("\n")[:2000]
        return Result(
            ok=False,
            rc=None,
            status="disconnected" if disconnected else "unknown",
            stdout=stdout,
            stderr=stderr,
            duration=elapsed,
            error=(
                "连接已断开，无法确认命令是否执行完毕"
                if disconnected
                else "超时，且现场探测失败，无法确认远端状态"
            ),
            note="远端可能仍在执行或已执行完毕，请勿盲目重跑；建议人工核对",
            remote=remote,
            slot=slot.index,
        )

    def _running_text(self, slot: Slot) -> str:
        return slot.cmd or ""

    def _postmortem(self, token: str, wait: float = 10.0) -> dict:
        """超时/断连后到远端查现场：rc 文件在不在、脚本还活着吗、已产出多少输出。

        复用同一条通道（断了才重连一次），一次往返拿全，尽量少碰堡垒机。
        投递在通道锁里，**等待在锁外** —— 探测 10s 不能把别的槽位一起冻住。
        """
        p = paths(token)
        if self._fd is None or self.state in (DIRTY, DOWN):
            try:
                self.start()
            except Exception as exc:  # noqa: BLE001
                return {"reachable": False, "rc": None, "alive": False, "error": f"重连失败: {exc}"}

        probe = new_token()
        base = p["out"].rsplit("/", 1)[-1]
        script = (
            # 先判存活、再读 rc：顺序反了会漏掉"扫描期间命令刚好结束"的情况，
            # 那条命令明明成功却被判成 unknown。
            f"__alive=NO; for d in /proc/[0-9]*; do "
            f"ls -l $d/fd 2>/dev/null | grep -q '{base}' && {{ __alive=YES; break; }}; "
            "done; "
            f"__rc=$(cat {p['rc']} 2>/dev/null || echo X); "
            f'printf \'{probe}-R %s %s\\n\' "$__rc" "$__alive"; '
            f"printf '{probe}-B\\n'; tail -c 200000 {p['out']} 2>/dev/null; "
            f"printf '\\n{probe}-E\\n'; "
            f"tail -c 4000 {p['err']} 2>/dev/null | tr '\\n' ' '; "
            f"printf '\\n{probe}-Z\\n'"
        )
        try:
            start = self._exchange(script)
        except (OSError, RuntimeError) as exc:
            return {"reachable": False, "rc": None, "alive": False, "error": str(exc)}

        if not self._await(marker(probe, "-Z"), start, wait):
            return {
                "reachable": True,
                "rc": None,
                "alive": False,
                "stdout": "",
                "stderr": "",
                "error": "现场探测未回包",
            }

        segment = self._segment(start)
        head = re.search(re.escape(probe) + r"-R (\S+) (\S+)", segment)
        rc: int | None = None
        alive = False
        if head:
            rc = None if head.group(1) == "X" else int(head.group(1))
            alive = head.group(2) == "YES"
        out = _between(segment, probe + "-B", probe + "-E")
        err = _between(segment, probe + "-E", probe + "-Z").strip()
        return {
            "reachable": True,
            "rc": rc,
            "alive": alive,
            "stdout": out,
            "stderr": err,
            "truncated": len(out) >= 200000,
        }

    def _cleanup(self, token: str) -> None:
        p = paths(token)
        try:
            with self._io_lock:
                self._send(f"rm -f {p['out']} {p['err']} {p['rc']} {p['done']} {p['script']}\n")
        except (OSError, RuntimeError):
            pass

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

    def _trigger(self, slot: Slot, token: str) -> None:
        """把脚本投进指定槽位（调用方持通道锁，保证两行不被别的线程插队）。"""
        script = paths(token)["script"]
        if slot.target:
            self._send(f"tmux send-keys -t {slot.target} -l {_quote('. ' + script)}\n")
            self._send(f"tmux send-keys -t {slot.target} Enter\n")
        else:
            self._send(f". {script}\n")

    def _write_script(self, token: str, script: str, retries: int = 3) -> bool:
        """把脚本写到远端并**按字节数校验**：pty 偶尔丢行，丢了必须重写。

        只检查"文件存在"是不够的：`base64 -d > file` 是**先建文件再写内容**，
        存在 ≠ 写完。曾经因此在脚本只写了一行时就开始 source，命令半截执行。
        """
        target = paths(token)["script"]
        size = len(script.encode())
        for _ in range(retries):
            with self._io_lock:
                for line in build_write_lines(token, script):
                    self._send(line + "\n")
            if self._file_size_is(target, size, 1.0):
                return True
        return False

    def _file_size_is(self, path: str, size: int, wait: float) -> bool:
        probe = new_token()
        pattern = re.compile(re.escape(probe) + r"-(YES|NO)")
        try:
            start = self._exchange(
                f'[ -f {path} ] && [ "$(wc -c <{path})" -eq {size} ] '
                f"&& printf '{probe}-YES\\n' || printf '{probe}-NO\\n'\n"
            )
        except (OSError, RuntimeError):
            return False
        if not self._await(pattern, start, wait):
            return False
        found = pattern.search(self._segment(start))
        return bool(found and found.group(1) == "YES")

    def _pane_has_text(self, slot: Slot, needle: str, wait: float) -> bool:
        """只读地看 pane 屏幕上有没有这段文字（用来判断按键到底送没送进去）。"""
        if not slot.target:
            return False
        probe = new_token()
        pattern = re.compile(re.escape(probe) + r"-(YES|NO)")
        try:
            start = self._exchange(
                f"tmux capture-pane -p -S -200 -t {slot.target} 2>/dev/null "
                f"| grep -qF '{needle}' && printf '{probe}-YES\\n' "
                f"|| printf '{probe}-NO\\n'\n"
            )
        except (OSError, RuntimeError):
            return False
        if not self._await(pattern, start, wait):
            return False
        found = pattern.search(self._segment(start))
        return bool(found and found.group(1) == "YES")

    def _ensure_started(
        self, slot: Slot, token: str, timeout: float = 20.0, retries: int = 6
    ) -> bool:
        """确认命令**真的开始在远端跑了**。

        重投投喂必须极其谨慎：万一第一条其实已经开始执行，再投一次就会把
        mkfs/dd 这类命令跑第二遍。所以只有「屏幕上没有那行命令」时才重投。
        """
        p = paths(token)
        needle = f"{token}.sh"
        deadline = time.time() + timeout
        sent = 1
        rebuilt = False
        while time.time() < deadline:
            if self._file_exists(p["started"], 0.5):
                return True
            if not slot.target:
                return True
            if self._pane_has_text(slot, needle, 0.6):
                # 按键已经进到 pane 里了，只是还没开始执行：等着，别重投
                time.sleep(0.2)
                continue
            if sent >= retries:
                if rebuilt:
                    break
                # 重投到上限都没落盘、屏幕上也没有那行命令 —— 这个 window 在吞字符
                # （老会话里用户的 shell 挂了 ble.sh 之类的行编辑器）。
                # `.started` 是脚本第一行，被 source 后毫秒内就该出现；它没出现就
                # 说明脚本根本没被执行，所以重建 window 不会误杀正在跑的命令。
                self.last_error = f"槽位 w{slot.index} 在吞输入，已重建 window"
                rebuilt = True
                sent = 1
                self._reclaim(slot, force=True)
                with self._io_lock:
                    self._trigger(slot, token)
                deadline = max(deadline, time.time() + 15.0)
                continue
            with self._io_lock:
                self._trigger(slot, token)
            sent += 1
        return self._file_exists(p["started"], 0.5)

    def _file_exists(self, path: str, wait: float) -> bool:
        probe = new_token()
        pattern = re.compile(re.escape(probe) + r"-(YES|NO)")
        try:
            start = self._exchange(
                f"[ -e {path} ] && printf '{probe}-YES\\n' || printf '{probe}-NO\\n'\n"
            )
        except (OSError, RuntimeError):
            return False
        if not self._await(pattern, start, wait):
            return False
        found = pattern.search(self._segment(start))
        return bool(found and found.group(1) == "YES")

    def _abort(self, slot: Slot) -> None:
        """只打断这个槽位里的命令。

        tmux 模式下**只**用 send-keys 往那个 window 发 Ctrl-C：往通道裸发 \\x03
        会打在外层 shell 的前台任务上，而那个前台任务可能是**别的槽位**的轮询，
        顺手也可能把别人的命令带下水。
        """
        try:
            with self._io_lock:
                if slot.target:
                    self._send(f"tmux send-keys -t {slot.target} C-c 2>/dev/null\n")
                else:
                    self._send("\x03")
        except (OSError, RuntimeError):
            pass

    def kill(self) -> dict:
        """强制回收：Ctrl-C + 杀掉远端 tmux 会话 + 断开通道，下次调用自动重连。

        这是"整个会话都僵了"的应急出口 —— 会连坐所有槽位，日常请用 `interrupt`。
        """
        for slot in self.slots:
            slot.cancel.set()
            slot.pending = None
        if self.tmux and self._fd is not None:
            # 必须**等确认**再关通道：send 只是丢进 pty 缓冲区，紧接着 stop()
            # 会把 ssh 进程杀掉，kill-session 经常还没被执行（实测：命令"成功"
            # 了但远端会话还在）。
            probe = new_token()
            pattern = re.compile(re.escape(probe) + r"-K")
            try:
                start = self._exchange(
                    f"tmux kill-session -t {self.tmux} 2>/dev/null; printf '{probe}-K\\n'"
                )
                self._await(pattern, start, 5.0)
            except (OSError, RuntimeError):
                pass
        elif self.slots:
            self._abort(self.slots[0])
        self.stop()
        self.last_error = None
        return self.info()

    def interrupt(self, req_id: str | None = None, slot: int | None = None) -> list[int]:
        """打断命令：优先按 req_id/slot 精确定位，找不到才退化成"打断所有在跑的"。"""
        targets = self._target_slots(req_id, slot)
        for target in targets:
            target.cancel.set()
            # 被用户打断的命令不会再写 rc 文件，别再拿它当"远端还在跑"。
            target.pending = None
            self._abort(target)
        return [t.index for t in targets]

    def _target_slots(self, req_id: str | None = None, slot: int | None = None) -> list[Slot]:
        if req_id:
            hit = [s for s in self.slots if s.req_id == req_id]
            if hit:
                return hit
        if slot is not None and 0 <= slot < len(self.slots):
            return [self.slots[slot]]
        # 退化：没给 req_id/slot（老客户端）时，凡是"在跑的"和"上次超时后远端还
        # 在跑的"都打断 —— 这正是 `interrupt` 原本的语义。
        return [s for s in self.slots if s.lock.locked() or s.pending]

    def info(self) -> dict:
        slots = [slot.info() for slot in self.slots]
        busy = any(slot["busy"] for slot in slots)
        running = next((s["running"] for s in slots if s["running"]), None)
        pending = next((s["pending"] for s in slots if s["pending"]), None)
        return {
            "host": self.host,
            "transport": self.transport.describe(),
            "state": self.state,
            "busy": busy,
            "slots": slots,
            "running": running,
            "remote_busy": pending,
            "idle": round(time.time() - self.last_used, 1),
            "uptime": round(time.time() - self.created_at, 1) if self.created_at else 0,
            "last_error": self.last_error,
        }


def _quote(text: str) -> str:
    """给 tmux send-keys -l 用的单引号转义（内容只含路径和简单命令）。"""
    return "'" + text.replace("'", "'\\''") + "'"


def _between(text: str, start_marker: str, end_marker: str) -> str:
    """取两行标记之间的内容（标记各占一行）。"""
    pattern = re.compile(
        r"^" + re.escape(start_marker) + r"$\n?(.*?)^" + re.escape(end_marker) + r"$",
        re.MULTILINE | re.DOTALL,
    )
    found = pattern.search(text)
    if not found:
        return ""
    return found.group(1)


def _cause(buf: str, limit: int = 160) -> str:
    """从通道尾部抓 ssh 层的报错，归为 cause，别和 lssh 自己的判定混在一起。"""
    hints = ("Connection", "disconnect", "closed", "Permission denied", "ssh:")
    lines = [line.strip() for line in buf[-4000:].splitlines() if line.strip()]
    picked = [line for line in lines if any(hint in line for hint in hints)]
    if not picked:
        return ""
    return "（ssh 层: " + "；".join(picked[-2:])[:limit] + "）"
