"""``lssh-askpass`` —— SSH_ASKPASS 帮手，密码来源按固定顺序解析。

OpenSSH 在需要密码时会执行 ``$SSH_ASKPASS``，并把提示串（例如
``root@10.0.0.9's password:``）作为唯一参数传进来。本模块就是那个被执行的程序，
它按下面的顺序取密码，第一个有结果的胜出：

1. ``$LSSH_PASSWORD_CMD`` —— 任意命令，stdout 即密码。
   推荐接真正的密钥管理器，例如 ``pass show lssh/bastion``、
   ``op read op://private/bastion/password``、``security find-generic-password -w``。
2. 系统 keyring —— 如果存在 ``secret-tool``（libsecret），查询
   ``service=lssh, host=<host>``。写入方式见文档。
3. ``~/.config/lssh/secrets`` —— 每行 ``<host-pattern> <password>``，权限 600。
   最后兜底，纯文本，风险自担。
4. ``~/.ssh/config`` 里形如 ``# lssh-pw <host> <password>`` 的注释行 —— 兼容旧配置，
   属于过渡方案，计划移除。

本程序把密码写到 stdout，其它任何东西都不输出（日志会污染 askpass 协议）。
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import sys
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "lssh"


def host_from_prompt(prompt: str) -> str:
    """从 ``root@host's password:`` 取出 host；取不到就返回空串。"""
    at = prompt.rfind("@")
    if at < 0:
        return ""
    rest = prompt[at + 1 :]
    for sep in ("'", " ", ":", ","):
        rest = rest.split(sep, 1)[0]
    return rest.strip()


def from_command(env: dict[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    command = env.get("LSSH_PASSWORD_CMD")
    if not command:
        return None
    try:
        done = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.rstrip("\n") or None


def from_keyring(host: str) -> str | None:
    if not host or not shutil.which("secret-tool"):
        return None
    try:
        done = subprocess.run(
            ["secret-tool", "lookup", "service", "lssh", "host", host],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.rstrip("\n") or None


def from_secrets_file(host: str, path: Path | None = None) -> str | None:
    path = path or CONFIG_DIR / "secrets"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pattern, password = parts
        if pattern == "*" or fnmatch.fnmatch(host, pattern):
            return password
    return None


def from_ssh_config(host: str, path: Path | None = None) -> str | None:
    """兼容旧的 ``# lssh-pw <host> <password>`` 注释行。"""
    path = path or Path.home() / ".ssh" / "config"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.startswith("# lssh-pw "):
            continue
        parts = line.split()
        if len(parts) >= 4 and (parts[2] == host or parts[2] == "*"):
            return parts[3]
    return None


def resolve(prompt: str, secrets_path: Path | None = None) -> str | None:
    host = host_from_prompt(prompt)
    return (
        from_command()
        or from_keyring(host)
        or from_secrets_file(host, secrets_path)
        or from_ssh_config(host)
    )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    prompt = argv[1] if len(argv) > 1 else ""
    password = resolve(prompt)
    if not password:
        return 1
    sys.stdout.write(password + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
