"""本地守护进程：持有长连接，对外提供 Unix socket 上的一行 JSON RPC。

RPC 形如::

    {"op": "run", "host": "a5-53", "cmd": "npu-smi info", "timeout": 60,
     "req_id": "...", "slot": 1}
    {"ok": true, "rc": 0, "stdout": "...", "stderr": "", "duration": 0.21, "slot": 1}

    {"op": "list"}                  # 所有会话 + 各自槽位状态
    {"op": "restart", "host": "..."} # 断线重连（远端 tmux 保留）
    {"op": "kill", "host": "..."}    # 强制回收整个远端 tmux 会话 + 断开
    {"op": "interrupt", "host": "...", "req_id": "..."}  # 只打断那一条命令
    {"op": "stop"}                  # 关闭所有连接并退出
    {"op": "ping"}
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import threading
import time
from pathlib import Path
from typing import Callable

from .session import Session
from .transports import Transport

MAX_REQUEST = 8 * 1024 * 1024


class Daemon:
    def __init__(
        self,
        transport_factory: Callable[[str], Transport],
        socket_path: Path,
        log_path: Path,
        *,
        tmux: str | None = "lssh",
        default_timeout: float = 120.0,
        idle_timeout: float = 1800.0,
        cols: int = 512,
        rows: int = 50,
        max_slots: int = 3,
        reaper_interval: float = 30.0,
    ) -> None:
        self.transport_factory = transport_factory
        self.socket_path = Path(socket_path)
        self.log_path = Path(log_path)
        self.tmux = tmux
        self.default_timeout = default_timeout
        self.idle_timeout = idle_timeout
        self.cols = cols
        self.rows = rows
        self.max_slots = max(1, int(max_slots))
        #: 空闲回收的巡检周期（测试里调到很小，用来验证"跑任务时绝不关通道"）
        self.reaper_interval = float(reaper_interval)
        self.sessions: dict[str, Session] = {}
        self._lock = threading.Lock()
        self._stopping = False

    # ------------------------------------------------------------------ 基础设施

    def log(self, message: str) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
        except OSError:
            pass

    def session(self, host: str) -> Session:
        with self._lock:
            session = self.sessions.get(host)
            if session is None:
                session = Session(
                    host=host,
                    transport=self.transport_factory(host),
                    tmux=self.tmux,
                    default_timeout=self.default_timeout,
                    cols=self.cols,
                    rows=self.rows,
                    max_slots=self.max_slots,
                )
                self.sessions[host] = session
            return session

    # ------------------------------------------------------------------ RPC

    def handle(self, request: dict) -> dict:
        op = request.get("op", "run")

        if op == "ping":
            return {"ok": True, "pid": os.getpid()}
        if op == "stop":
            self._stopping = True
            return {"ok": True}
        if op == "list":
            with self._lock:
                items = [s.info() for s in self.sessions.values()]
            return {"ok": True, "sessions": sorted(items, key=lambda i: i["host"])}

        host = request.get("host")
        if not host:
            return {"ok": False, "error": "缺少 host"}

        session = self.session(host)
        if op == "interrupt":
            slots = session.interrupt(req_id=request.get("req_id"), slot=request.get("slot"))
            return {"ok": True, "slots": slots}
        if op == "restart":
            try:
                session.stop()
                session.start()
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"重连失败: {exc}"}
            return {"ok": True, "session": session.info()}
        if op == "kill":
            return {"ok": True, "session": session.kill()}
        if op == "run":
            cmd = request.get("cmd") or ""
            slot = request.get("slot")
            return session.run(
                cmd,
                request.get("timeout"),
                wait=float(request.get("wait") or 0),
                interrupt_on_timeout=bool(request.get("interrupt_on_timeout")),
                req_id=request.get("req_id"),
                slot=int(slot) if slot is not None else None,
            ).as_dict()
        return {"ok": False, "error": f"未知 op: {op}"}

    def _handle_conn(self, conn: socket.socket) -> None:
        try:
            data = b""
            while b"\n" not in data and len(data) < MAX_REQUEST:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            if not data.strip():
                return
            try:
                request = json.loads(data.decode("utf-8", "replace").strip())
            except ValueError as exc:
                conn.sendall(
                    (json.dumps({"ok": False, "error": f"请求不是 JSON: {exc}"}) + "\n").encode()
                )
                return
            try:
                response = self.handle(request)
            except Exception as exc:  # noqa: BLE001
                response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            conn.sendall((json.dumps(response, ensure_ascii=False) + "\n").encode())
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    # ------------------------------------------------------------------ 生命周期

    def serve_forever(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        # 多个客户端同时冷启动时会各自 spawn 一个守护进程，用独占 flock 只留一个：
        # 否则后启动的进程会 unlink 掉前一个的 socket，导致连接莫名被拒。
        lock_file = open(self.socket_path.parent / "daemon.lock", "w")  # noqa: SIM115
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.log("已有守护进程持有锁，本次启动退出")
            return

        try:
            self.socket_path.unlink()
        except OSError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        server.listen(64)
        server.settimeout(0.5)  # 让 accept 定期醒来，SIGTERM 才能及时退出
        self.log(f"启动 pid={os.getpid()} socket={self.socket_path}")

        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: setattr(self, "_stopping", True))
        threading.Thread(target=self._reaper, name="lssh-reaper", daemon=True).start()

        while not self._stopping:
            try:
                conn, _ = server.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

        server.close()
        for session in list(self.sessions.values()):
            session.stop()
        try:
            self.socket_path.unlink()
        except OSError:
            pass
        self.log("退出")

    def _reaper(self) -> None:
        while not self._stopping:
            time.sleep(self.reaper_interval)
            now = time.time()
            with self._lock:
                items = list(self.sessions.items())
            for host, session in items:
                # 只要还有槽位在跑命令（哪怕远端任务已经超时、客户端走了），
                # 就不能关通道：关掉等于把正在跑的长任务从中间掐断。
                if session.state != "ready" or any(s.lock.locked() for s in session.slots):
                    continue
                if now - session.last_used > self.idle_timeout:
                    self.log(f"{host}: 空闲 {now - session.last_used:.0f}s，关闭通道")
                    session.stop()
