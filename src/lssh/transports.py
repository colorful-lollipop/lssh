"""传输层：如何拿到一个"远端 shell"的 pty。

抽象出 Transport 之后，守护进程本身不关心对端是 SSH、容器还是本地 shell，
测试也就能用 LocalTransport 在毫秒内跑通（不需要网络、不需要堡垒机）。
"""

from __future__ import annotations

import fcntl
import os
import pty
import shlex
import struct
import subprocess
import termios
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Spawned:
    master_fd: int
    proc: subprocess.Popen[bytes]


class Transport(Protocol):
    """产出一个已连接远端 shell 的 pty 主端。"""

    def spawn(self, *, cols: int, rows: int) -> Spawned: ...

    def describe(self) -> str: ...


def _open_pty(cols: int, rows: int) -> tuple[int, int]:
    master, slave = pty.openpty()
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    return master, slave


def _spawn(argv: list[str], env: dict[str, str] | None, cols: int, rows: int) -> Spawned:
    master, slave = _open_pty(cols, rows)
    proc = subprocess.Popen(
        argv,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        close_fds=True,
        env=env,
    )
    os.close(slave)
    return Spawned(master_fd=master, proc=proc)


@dataclass
class SSHTransport:
    """用系统 OpenSSH 客户端拉起一条交互 shell。

    刻意不复刻 SSH 协议：部分网关会 tarpit 非 OpenSSH 客户端，而且让 OpenSSH
    处理 ~/.ssh/config、ControlMaster、ProxyJump 才是用户预期行为。
    """

    host: str
    extra_args: list[str] = field(default_factory=list)
    askpass: str | None = None
    connect_timeout: int = 15
    environ: dict[str, str] = field(default_factory=dict)

    def spawn(self, *, cols: int, rows: int) -> Spawned:
        env = dict(os.environ)
        env.pop("TMUX", None)  # 防止本地 tmux 干扰远端判定
        env.setdefault("DISPLAY", ":0")
        env.update(self.environ)

        argv = [
            "ssh",
            "-tt",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
        ]
        if self.askpass:
            env["SSH_ASKPASS"] = self.askpass
            env["SSH_ASKPASS_REQUIRE"] = "force"
            argv += ["-o", "NumberOfPasswordPrompts=1"]
        argv += self.extra_args
        argv.append(self.host)
        return _spawn(argv, env, cols, rows)

    def describe(self) -> str:
        return "ssh " + " ".join(shlex.quote(a) for a in [*self.extra_args, self.host])


@dataclass
class LocalTransport:
    """本地 shell（测试用）：等价于"远端"就是本机。"""

    shell: str = "/bin/bash"
    extra_args: list[str] = field(default_factory=list)

    def spawn(self, *, cols: int, rows: int) -> Spawned:
        env = dict(os.environ)
        env["TERM"] = "xterm-256color"
        env["LSSH_NO_TMUX"] = "1"  # 本地模式不要碰用户的 tmux
        argv = [self.shell, "--noprofile", "--norc", "-i", *self.extra_args]
        return _spawn(argv, env, cols, rows)

    def describe(self) -> str:
        return "local:" + self.shell
