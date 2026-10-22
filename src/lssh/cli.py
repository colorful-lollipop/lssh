"""命令行入口：lssh。"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import time
from typing import Any

from . import __version__
from .config import init_files, installed_askpass, resolve
from .daemon import Daemon
from .paths import log_path, socket_path
from .transports import LocalTransport, SSHTransport

CLIENT_TIMEOUT = 24 * 3600.0


# --------------------------------------------------------------------- 守护进程


def apply_settings(args: argparse.Namespace) -> None:
    """CLI 参数 > 配置（按 host 匹配）> 内置默认值。"""
    settings = resolve(args.host or args.restart)
    for key in ("timeout", "idle", "tmux", "connect_timeout", "cols", "rows"):
        if getattr(args, key) is None:
            setattr(args, key, settings[key])
    args.askpass = installed_askpass()


def transport_factory(args: argparse.Namespace):
    if args.local:
        return lambda host: LocalTransport(shell=args.local)

    askpass = args.askpass

    def make(host: str) -> SSHTransport:
        return SSHTransport(
            host=host,
            extra_args=list(args.ssh_arg or []),
            askpass=askpass,
            connect_timeout=args.connect_timeout,
        )

    return make


def run_daemon(args: argparse.Namespace) -> int:
    daemon = Daemon(
        transport_factory(args),
        socket_path=socket_path(),
        log_path=log_path(),
        tmux=None if args.no_tmux else args.tmux,
        default_timeout=args.timeout or 120.0,
        idle_timeout=args.idle,
        cols=args.cols,
        rows=args.rows,
    )
    daemon.serve_forever()
    return 0


# --------------------------------------------------------------------- 客户端


def _connect() -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(CLIENT_TIMEOUT)
    sock.connect(str(socket_path()))
    return sock


def daemon_alive() -> bool:
    try:
        with _connect() as sock:
            sock.sendall(b'{"op": "ping"}\n')
            return b'"ok": true' in sock.recv(4096)
    except OSError:
        return False


def ensure_daemon(args: argparse.Namespace) -> None:
    if daemon_alive():
        return
    cmd = [sys.executable, "-m", "lssh", "--daemon"]
    if args.no_tmux:
        cmd.append("--no-tmux")
    if args.tmux:
        cmd += ["--tmux", args.tmux]

    if args.local:
        cmd += ["--local", args.local]
    if args.idle:
        cmd += ["--idle", str(args.idle)]
    if args.cols:
        cmd += ["--cols", str(args.cols)]
    if args.rows:
        cmd += ["--rows", str(args.rows)]
    for extra in args.ssh_arg or []:
        cmd += ["--ssh-arg", extra]
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    for _ in range(80):
        time.sleep(0.1)
        if daemon_alive():
            return
    raise SystemExit("lssh: 守护进程启动失败，请查看 " + str(log_path()))


def call(request: dict[str, Any], args: argparse.Namespace | None = None) -> dict[str, Any]:
    try:
        return _call_once(request)
    except (ConnectionRefusedError, FileNotFoundError):
        # 守护进程刚退出/正在重启（--stop 之后立刻调用常见）
        if args is not None:
            ensure_daemon(args)
        return _call_once(request)


def _call_once(request: dict[str, Any]) -> dict[str, Any]:
    with _connect() as sock:
        sock.sendall((json.dumps(request) + "\n").encode())
        buffer = b""
        while not buffer.endswith(b"\n"):
            chunk = sock.recv(1 << 16)
            if not chunk:
                break
            buffer += chunk
    if not buffer.strip():
        return {"ok": False, "error": "守护进程没有返回数据"}
    return json.loads(buffer.decode("utf-8", "replace"))


def emit(result: dict[str, Any], as_json: bool) -> int:
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
        if not result.get("ok"):
            return 1
        return 0 if result.get("rc") in (0, None) else 1

    if result.get("stdout"):
        sys.stdout.write(result["stdout"].rstrip("\n") + "\n")
    if result.get("stderr"):
        sys.stderr.write(result["stderr"].rstrip("\n") + "\n")
    if not result.get("ok"):
        sys.stderr.write(f"lssh: {result.get('error', '未知错误')}\n")
        return 1
    rc = result.get("rc")
    if rc == 0:
        return 0
    return rc if isinstance(rc, int) and 0 < rc < 256 else 1


def _interrupt(host: str) -> None:
    try:
        call({"op": "interrupt", "host": host})
    except (OSError, ValueError):
        pass


def repl(host: str, timeout: float | None) -> int:
    print(f"lssh: 已接入 {host}（远端会话保持 cwd/环境；Ctrl-D 退出）")
    while True:
        try:
            line = input(f"{host}> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        line = line.strip()
        if not line:
            continue
        if line in ("exit", "quit", ":q"):
            return 0
        if line == ":interrupt":
            call({"op": "interrupt", "host": host})
            continue
        try:
            result = call({"op": "run", "host": host, "cmd": line, "timeout": timeout})
        except KeyboardInterrupt:
            _interrupt(host)
            print("^C（已向远端发送 Ctrl-C）")
            continue
        code = emit(result, False)
        if code:
            print(f"[rc={code}]")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lssh",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "长连接远端命令执行器：一条 SSH 连接 + 远端 tmux，用于 exec 通道被禁或容易限流的机器。"
        ),
        epilog=(
            "示例:\n"
            "  lssh a5-53 'npu-smi info | head -5'\n"
            "  lssh a5-53 'cd /opt/app && ls; df -h .'\n"
            "  lssh --timeout 30 --json a5-53 'uptime'\n"
            "  lssh --list / lssh --restart a5-53\n"
            "\n"
            "行为:\n"
            "  * 每条命令跑完才返回（原子输出，带长度+md5 校验），不提供流式。\n"
            "  * 同一台机器串行排队，不同机器并行；多个 agent 共享同一守护进程。\n"
            "  * 退出码 = 远端命令的 rc；连接/超时/校验失败返回 1 并打印原因。\n"
            "\n"
            "长任务请自己放后台并用 tail 轮询:\n"
            "  lssh h 'cd /opt && nohup make -j16 > /tmp/b.log 2>&1 & echo $!'\n"
            "  lssh h 'tail -5 /tmp/b.log; pgrep -f make || echo DONE'\n"
            "\n"
            "远端 cwd/环境是共享的（tmux pane 复用），建议用绝对路径或自带 cd。\n"
        ),
    )
    parser.add_argument("host", nargs="?", help="SSH 目标（~/.ssh/config 别名或 user@host）")
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="要执行的命令（可分多行）")

    parser.add_argument("--list", action="store_true", help="列出会话状态")
    parser.add_argument("--restart", metavar="HOST", help="断开重连指定主机")
    parser.add_argument("--stop", action="store_true", help="关闭守护进程")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON")
    parser.add_argument("--timeout", type=float, default=None, help="单条命令超时秒数（默认 120）")
    parser.add_argument("--version", action="version", version=f"lssh {__version__}")
    parser.add_argument(
        "--init",
        action="store_true",
        help="创建 ~/.config/lssh/{config.json,secrets}（幂等）",
    )

    parser.add_argument("--daemon", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--tmux", default=None, help="远端 tmux 会话名（默认 lssh）")
    parser.add_argument("--no-tmux", action="store_true", help="不启用远端 tmux 持久化")
    parser.add_argument("--ssh-arg", action="append", metavar="ARG", help="透传给 ssh 的额外参数")
    parser.add_argument("--connect-timeout", type=int, default=None, help="SSH ConnectTimeout 秒数")
    parser.add_argument("--idle", type=float, default=None, help="空闲多少秒后关闭通道")
    parser.add_argument("--cols", type=int, default=None, help="伪终端列数（默认 512，减少折行）")
    parser.add_argument("--rows", type=int, default=None, help="伪终端行数")
    parser.add_argument("--local", metavar="SHELL", default=None, help="调试用：在本地 shell 上跑")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.init:
        for path, created in init_files():
            print(("已创建 " if created else "已存在 ") + path)
        return 0

    apply_settings(args)

    if args.daemon:
        return run_daemon(args)

    if shutil.which("ssh") is None and not args.local:
        print("lssh: 找不到 ssh 客户端", file=sys.stderr)
        return 2

    if args.list or args.stop or args.restart:
        ensure_daemon(args)
        if args.list:
            result = call({"op": "list"})
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            sessions = result.get("sessions", [])
            if not sessions:
                print("（无会话）")
            for item in sessions:
                flag = "  BUSY" if item.get("busy") else ""
                print(
                    f"{item['host']:<16} {item['state']:<8} "
                    f"idle={item['idle']:>7.1f}s up={item['uptime']:>7.1f}s{flag}"
                )
                if item.get("last_error"):
                    print(f"{'':<16} last_error: {item['last_error']}")
            return 0
        if args.stop:
            print(json.dumps(call({"op": "stop"}), ensure_ascii=False))
            return 0
        ensure_daemon(args)
        result = call({"op": "restart", "host": args.restart})
        if result.get("ok") and not args.json:
            print(f"lssh: 已重连 {args.restart}")
            return 0
        return emit(result, args.json)

    if not args.host:
        build_parser().print_help()
        return 2

    cmd = " ".join(args.cmd).strip()
    if cmd.startswith("--"):
        cmd = cmd[2:].strip()

    ensure_daemon(args)

    if not cmd:
        if sys.stdin.isatty():
            return repl(args.host, args.timeout)
        cmd = sys.stdin.read()
        if not cmd.strip():
            print("lssh: 空命令", file=sys.stderr)
            return 2

    try:
        result = call({"op": "run", "host": args.host, "cmd": cmd, "timeout": args.timeout})
    except KeyboardInterrupt:
        _interrupt(args.host)
        sys.stderr.write("\nlssh: 已中断（远端已收到 Ctrl-C）\n")
        return 130
    return emit(result, args.json)


if __name__ == "__main__":
    sys.exit(main())
