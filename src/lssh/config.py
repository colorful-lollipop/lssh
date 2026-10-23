"""本地配置：``~/.config/lssh/{config.json,askpass.sh,secrets}``。

设计上把「工具怎么连」和「密码从哪来」都收进项目自己的目录，不再散落到
``~/.ssh``：

* ``config.json`` —— 默认值（超时、tmux 名、askpass 路径）与按 host 匹配的覆盖；
* ``askpass.sh`` —— 由 ``lssh --init`` 生成，密码优先取 ``$LSSH_PASSWORD_CMD``，
  其次读同目录的 ``secrets``；
* ``secrets`` —— 每行 ``<host-pattern> <password>``，权限 600，仅作兜底。
"""

from __future__ import annotations

import fnmatch
import json
import shutil
import stat
import sys
from pathlib import Path

from .paths import config_dir, config_path, secrets_path

DEFAULTS: dict = {
    "timeout": 120.0,
    "idle": 1800.0,
    "tmux": "lssh",
    "connect_timeout": 15,
    "cols": 512,
    "rows": 50,
    #: 每台机器最多几个并发槽位（远端 tmux window 数）。
    #: 一条长任务挂住时，其它槽位仍可用来查询；设为 1 就退回串行行为。
    "slots": 3,
}

SECRETS_TEMPLATE = """\
# lssh secrets — 每行 "<host-pattern> <password>"，本文件必须是 600。
# host-pattern 支持 * 通配；匹配的是 SSH 提示串里的主机部分。
# 文件可以是软链：需要密钥管理器时，让别的程序生成它即可。
"""


def load() -> dict:
    path = config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def resolve(host: str | None = None) -> dict:
    """全局默认值 + 第一个匹配的 host 覆盖。"""
    cfg = load()
    effective = {key: cfg.get(key, value) for key, value in DEFAULTS.items()}
    patterns = cfg.get("hosts") or {}
    if host and isinstance(patterns, dict):
        for pattern, overrides in patterns.items():
            if fnmatch.fnmatch(host, pattern) and isinstance(overrides, dict):
                effective.update({k: v for k, v in overrides.items() if v is not None})
                break
    return effective


def installed_askpass() -> str | None:
    """找到本包安装的 ``lssh-askpass``（venv 的 bin 目录或 PATH）。"""
    beside = Path(sys.executable).with_name("lssh-askpass")
    if beside.exists():
        return str(beside)
    return shutil.which("lssh-askpass")


def _write_if_missing(path: Path, text: str, mode: int) -> tuple[str, bool]:
    if path.exists():
        return str(path), False
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return str(path), True


def init_files() -> list[tuple[str, bool]]:
    """幂等地创建配置目录与三个文件，返回 (path, created)。"""
    config_dir().mkdir(parents=True, exist_ok=True)
    config_dir().chmod(stat.S_IRWXU)
    results = [_write_if_missing(secrets_path(), SECRETS_TEMPLATE, 0o600)]
    default_config = {
        "timeout": DEFAULTS["timeout"],
        "idle": DEFAULTS["idle"],
        "tmux": DEFAULTS["tmux"],
        "slots": DEFAULTS["slots"],
        "hosts": {},
    }
    results.append(
        _write_if_missing(
            config_path(),
            json.dumps(default_config, indent=2, ensure_ascii=False) + "\n",
            0o600,
        )
    )
    return results
