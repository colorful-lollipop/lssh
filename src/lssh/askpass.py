"""``lssh-askpass`` —— OpenSSH 的 askpass 帮手。

OpenSSH 需要密码时会执行 ``$SSH_ASKPASS``，并把提示串（例如
``root@10.0.0.9's password:``）作为唯一参数传进来。本程序从
``~/.config/lssh/secrets`` 里取密码：

    # <host-pattern> <password>，一行一条，`*` 通配，文件权限 0600
    10.0.0.9 s3cret
    *.example.com another

只有一个密码来源，规则简单到可以一眼看完。文件可以是软链——想接密钥管理器时，
让别的程序生成它再链过来即可。
"""

from __future__ import annotations

import fnmatch
import os
import sys
from pathlib import Path

SECRETS_PATH = (
    Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "lssh" / "secrets"
)


def host_from_prompt(prompt: str) -> str:
    """从 ``root@host's password:`` 取出 host；取不到就返回空串。"""
    at = prompt.rfind("@")
    if at < 0:
        return ""
    rest = prompt[at + 1 :]
    for sep in ("'", " ", ":", ","):
        rest = rest.split(sep, 1)[0]
    return rest.strip()


def lookup(host: str, path: Path | None = None) -> str | None:
    """按顺序匹配 ``secrets`` 里的 <host-pattern>；`*` 永远匹配。"""
    path = path or SECRETS_PATH
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


def resolve(prompt: str, secrets_path: Path | None = None) -> str | None:
    return lookup(host_from_prompt(prompt), secrets_path)


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
