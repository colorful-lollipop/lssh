"""文件系统路径（socket / 日志 / 运行时目录）。"""

from __future__ import annotations

import os
from pathlib import Path

APP = "lssh"


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / APP


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    return Path(base) / APP if base else cache_dir()


def socket_path() -> Path:
    override = os.environ.get("LSSH_SOCKET")
    return Path(override) if override else runtime_dir() / "daemon.sock"


def log_path() -> Path:
    override = os.environ.get("LSSH_LOG")
    return Path(override) if override else cache_dir() / "daemon.log"
