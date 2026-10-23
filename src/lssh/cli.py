"""命令行入口：lssh。"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import sys
import time
import uuid
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
    settings = resolve(args.host or args.restart or args.kill)
    for key in ("timeout", "idle", "tmux", "connect_timeout", "cols", "rows", "slots"):
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
        max_slots=args.slots or 3,
    )
    daemon.serve_forever()
    return 0


# --------------------------------------------------------------------- 客户端


def _connect(timeout: float = CLIENT_TIMEOUT) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
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
    if args.slots:
        cmd += ["--slots", str(args.slots)]
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
    # 客户端等待上限跟着请求的超时走（+60s 给回收 pane 留余量），
    # 否则守护进程抽风时 CLI 会一直吊到 24h。
    limit = CLIENT_TIMEOUT
    if request.get("op") == "run":
        limit = float(request.get("timeout") or 120.0) + float(request.get("wait") or 0) + 60.0
    with _connect(min(limit, CLIENT_TIMEOUT)) as sock:
        sock.sendall((json.dumps(request) + "\n").encode())
        buffer = b""
        while not buffer.endswith(b"\n"):
            chunk = sock.recv(1 << 16)
            if not chunk:
                break
            buffer += chunk
    if not buffer.strip():
        return {"ok": False, "error": "守护进程没有返回数据（可能正忙或已退出，试试 lssh --kill）"}
    return json.loads(buffer.decode("utf-8", "replace"))


def emit(result: dict[str, Any], as_json: bool) -> int:
    status = result.get("status") or ("completed" if result.get("ok") else "error")
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
        return _exit_code(result, status)

    if result.get("stdout"):
        sys.stdout.write(result["stdout"].rstrip("\n") + "\n")
    if result.get("stderr"):
        sys.stderr.write(result["stderr"].rstrip("\n") + "\n")
    if result.get("note"):
        sys.stderr.write(f"lssh: {result['note']}\n")
    if not result.get("ok"):
        sys.stderr.write(f"lssh: {result.get('error', '未知错误')}\n")
        remote = result.get("remote") or {}
        if remote.get("stdout_file"):
            alive = (
                ("是" if remote.get("alive") else "否")
                if remote.get("reachable")
                else "未知（远端不可达）"
            )
            sys.stderr.write(
                f"lssh: 远端现场 {remote.get('stdout_file')} / {remote.get('rc_file')}"
                f"（命令是否还在跑: {alive}）\n"
            )
    return _exit_code(result, status)


def _exit_code(result: dict[str, Any], status: str) -> int:
    """退出码语义（照 timeout(1) 的约定，方便脚本判定）：

    0    命令成功
    124  **客户端放弃等待**（timeout/running/unknown/disconnected）——
         不代表命令失败，更不代表没执行过，禁止据此重跑破坏性命令
    130  用户中断
    其他  远端命令的 rc / 1
    """
    if status in ("timeout", "running", "unknown", "disconnected"):
        return 124
    if status == "cancelled":
        return 130
    rc = result.get("rc")
    if isinstance(rc, int) and 0 < rc < 256:
        return rc
    return 0 if result.get("ok") else 1


def _interrupt(host: str, req_id: str | None = None) -> None:
    """只打断自己那一条命令：req_id 让守护进程精确定位到具体槽位。

    没有 req_id（老守护进程/刚重启）时守护进程会退化成"打断该主机所有在跑的"。
    """
    try:
        call({"op": "interrupt", "host": host, "req_id": req_id})
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
        req_id = uuid.uuid4().hex
        try:
            result = call(
                {"op": "run", "host": host, "cmd": line, "timeout": timeout, "req_id": req_id}
            )
        except KeyboardInterrupt:
            _interrupt(host, req_id)
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
            "  lssh --list / lssh --kill a5-53 / lssh --wait 60 a5-53 'uptime'\n"
            "  lssh --slot 1 a5-53 'npu-smi info'   # 钉在某个槽位\n"
            "  lssh skills install --yes        # 把技能装进本机 AI Agent\n"
            "\n"
            "行为:\n"
            "  * 每条命令跑完才返回（原子输出，带长度+md5 校验），不提供流式。\n"
            "  * 同一台机器最多 3 个并发槽位（--slots 可调）：某条长任务挂住时，\n"
            "    另一条命令仍能秒回。连续命令粘在同一槽位，cwd/env 照旧保持；\n"
            "    并发时会落到不同槽位（各自的 cwd），建议用绝对路径或自带 cd。\n"
            "  * 不同机器并行；多个 agent 共享同一守护进程。--list 可看每个槽位在跑什么。\n"
            "  * **timeout ≠ 没执行**：超时只代表客户端放弃等待。lssh 会去远端查现场，\n"
            "    明确告诉你是 completed / running / disconnected，并带回已产出的输出。\n"
            "    默认不打断远端命令（--interrupt-on-timeout 才发 Ctrl-C）。\n"
            "  * 退出码：0 成功、124 客户端放弃等待（勿据此重跑）、130 中断、其余为远端 rc。\n"
            "\n"
            "长任务推荐自己放后台再轮询:\n"
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
    parser.add_argument(
        "--kill",
        metavar="HOST",
        help="强制回收整台机器的远端会话（所有槽位一起清）并断开，下次调用自动重建",
    )
    parser.add_argument("--stop", action="store_true", help="关闭守护进程")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON")
    parser.add_argument("--timeout", type=float, default=None, help="单条命令超时秒数（默认 120）")
    parser.add_argument(
        "--wait",
        type=float,
        default=0.0,
        help="会话忙时最多排队等待秒数（默认 0 = 立刻报忙并说明在跑什么）",
    )
    parser.add_argument(
        "--interrupt-on-timeout",
        action="store_true",
        help="超时后给远端发 Ctrl-C（默认不发：mkfs/dd 这类命令误打断比超时更危险）",
    )
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
    parser.add_argument(
        "--slots",
        type=int,
        default=None,
        help="每台机器最大并发槽位数（默认 3；1 = 退回串行），需重启守护进程生效",
    )
    parser.add_argument(
        "--slot", type=int, default=None, help="把命令钉在指定槽位（默认自动挑空闲的）"
    )
    parser.add_argument("--cols", type=int, default=None, help="伪终端列数（默认 512，减少折行）")
    parser.add_argument("--rows", type=int, default=None, help="伪终端行数")
    parser.add_argument("--local", metavar="SHELL", default=None, help="调试用：在本地 shell 上跑")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # 子命令：lssh skills ...
    if argv and argv[0] == "skills":
        from .skills_cli import main as skills_main

        return skills_main(argv[1:])

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

    if args.list or args.stop or args.restart or args.kill:
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
                slots = item.get("slots") or []
                used = sum(1 for s in slots if s.get("busy"))
                flag = f"  BUSY {used}/{len(slots)}" if used else ""
                print(
                    f"{item['host']:<16} {item['state']:<8} "
                    f"idle={item['idle']:>7.1f}s up={item['uptime']:>7.1f}s{flag}"
                )
                for slot in slots:
                    running = slot.get("running")
                    pending = slot.get("pending")
                    if running:
                        print(
                            f"{'':<16} w{slot['slot']} running: {running['cmd']!r} "
                            f"({running['for']}s)"
                        )
                    elif pending:
                        tail = "远端仍在跑" if not pending.get("unconfirmed") else "远端可能仍在跑"
                        print(
                            f"{'':<16} w{slot['slot']} {tail}: {pending['cmd']!r} "
                            f"({pending['for']}s)"
                        )
                if item.get("last_error"):
                    print(f"{'':<16} last_error: {item['last_error']}")
            return 0
        if args.stop:
            print(json.dumps(call({"op": "stop"}), ensure_ascii=False))
            return 0
        if args.kill:
            result = call({"op": "kill", "host": args.kill}, args)
            if result.get("ok") and not args.json:
                print(f"lssh: 已回收 {args.kill}（远端 tmux 会话已删除，下次调用自动重建）")
                return 0
            return emit(result, args.json)
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

    req_id = uuid.uuid4().hex
    try:
        result = call(
            {
                "op": "run",
                "host": args.host,
                "cmd": cmd,
                "timeout": args.timeout,
                "wait": args.wait,
                "interrupt_on_timeout": bool(args.interrupt_on_timeout),
                "req_id": req_id,
                "slot": args.slot,
            },
            args,
        )
    except KeyboardInterrupt:
        _interrupt(args.host, req_id)
        sys.stderr.write("\nlssh: 已中断（远端已收到 Ctrl-C）\n")
        return 130
    return emit(result, args.json)


if __name__ == "__main__":
    sys.exit(main())
