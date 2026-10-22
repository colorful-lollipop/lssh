"""远端脚本构造与输出解析。

为什么把结果写进文件再 cat
--------------------------
如果直接让命令在 tmux 的 pane 里输出，读到的其实是 tmux **客户端渲染后的屏幕**
（光标定位 + 重绘），既会丢换行，也会在 attach/重绘时把历史内容重复一遍。所以：
命令在 pane（或当前 shell）里执行，但 stdout/stderr/rc 全部落到临时文件；外层
shell 再把文件 cat 出来并打印标记。文件内容是原始字节，解析才可靠。

标记格式（每个命令一个随机 token）::

    <token>S
    ... stdout ...
    <token>M
    ... stderr ...
    <token>R:0
    <token>E
"""

from __future__ import annotations

import base64
import re
import uuid

ANSI_RE = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC ... BEL / ST
    r"|\x1b\[[0-9;?]*[ -/]*[@-~]"  # CSI
    r"|\x1b[()#][0-9A-Za-z]"  # 字符集选择
    r"|\x1b[@-Z\\-_]"  # 其余两字节序列
)


def clean(data: bytes | str) -> str:
    """去掉终端控制序列，并把 CRLF/CR 归一成 LF。"""
    text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    text = ANSI_RE.sub("", text)
    return text.replace("\r\n", "\n").replace("\r", "")


def new_token() -> str:
    """每个命令一个随机标记，避免与命令输出串味。"""
    return "LSSH" + uuid.uuid4().hex[:12]


def paths(token: str) -> dict[str, str]:
    base = f"/tmp/.lssh-{token}"
    return {
        "script": base + ".sh",
        "b64": base + ".b64",
        "out": base + ".out",
        "err": base + ".err",
        "rc": base + ".rc",
        "done": base + ".done",
    }


def build_script(token: str, cmd: str) -> str:
    """在远端执行、把结果写文件的脚本。"""
    p = paths(token)
    body = cmd if cmd.strip() else "true"
    return f"{{\n{body}\n}} >{p['out']} 2>{p['err']}\necho $? >{p['rc']}\n: >{p['done']}\n"


def build_write_lines(token: str, script: str, chunk: int = 900) -> list[str]:
    """外层 shell 用：base64 分块写文件，避开行编辑器/引号/多行问题。"""
    p = paths(token)
    payload = base64.b64encode(script.encode()).decode()
    lines = [f": > {p['b64']}"]
    for start in range(0, len(payload), chunk):
        lines.append(f"printf %s '{payload[start : start + chunk]}' >> {p['b64']}")
    lines.append(
        f"(base64 -d {p['b64']} > {p['script']} 2>/dev/null "
        f"|| openssl base64 -d -A -in {p['b64']} > {p['script']}); "
        f"rm -f {p['b64']}"
    )
    return lines


def build_poll_lines(token: str, timeout: float, interval: float = 0.1) -> list[str]:
    """外层 shell 用：等 .done（等价于等命令结束），带超时。"""
    p = paths(token)
    rounds = max(1, int(timeout / interval))
    return [
        f"__lssh_i=0; while [ ! -f {p['done']} ] && "
        f"[ $__lssh_i -lt {rounds} ]; do sleep {interval}; "
        "__lssh_i=$((__lssh_i+1)); done"
    ]


def build_report_lines(token: str) -> list[str]:
    """外层 shell 用：把结果按标记打印出来。"""
    p = paths(token)
    return [
        # 校验信息放在最前面：不能落进 S..M / M..R 的内容区间里
        f"printf '{token} SZ %s %s\\n' "
        f"\"$(tr -d '\\r' <{p['out']} 2>/dev/null | wc -c)\" "
        f"\"$(tr -d '\\r' <{p['out']} 2>/dev/null | md5sum | cut -d' ' -f1)\"; "
        f"printf '{token} MZ %s %s\\n' "
        f"\"$(tr -d '\\r' <{p['err']} 2>/dev/null | wc -c)\" "
        f"\"$(tr -d '\\r' <{p['err']} 2>/dev/null | md5sum | cut -d' ' -f1)\"; "
        f"printf '{token}S\\n'; cat {p['out']} 2>/dev/null; "
        f"printf '\\n{token}M\\n'; cat {p['err']} 2>/dev/null; "
        f"printf '\\n'; printf '{token}R:%s\\n' \"$(cat {p['rc']} 2>/dev/null "
        f'|| echo -1)"; '
        f"printf '{token}E\\n'; rm -f {p['out']} {p['err']} {p['rc']} "
        f"{p['done']} {p['script']}"
    ]


def marker(token: str, suffix: str) -> re.Pattern[str]:
    """标记必须独占一行，才不会被回显或历史内容误命中。"""
    return re.compile(r"^" + re.escape(token) + suffix + r"$", re.MULTILINE)


def parse_raw(segment: str, token: str) -> tuple[int, str, str] | None:
    """切出 (rc, 原始 stdout 区, 原始 stderr 区)，不做换行裁剪，供校验用。"""
    start = marker(token, "S").search(segment)
    if not start:
        return None
    mid = marker(token, "M").search(segment, start.end())
    if not mid:
        return None
    rc_m = marker(token, r"R:(-?\d+)").search(segment, mid.end())
    if not rc_m:
        return None
    end = marker(token, "E").search(segment, rc_m.end())
    if not end:
        return None
    return (
        int(rc_m.group(1)),
        segment[start.end() : mid.start()],
        segment[mid.end() : rc_m.start()],
    )


def strip_separator(raw: str) -> str:
    """去掉标记行自带的换行和分隔用的换行，还原成文件原始内容。"""
    if raw.startswith("\n"):
        raw = raw[1:]
    if raw.endswith("\n"):
        raw = raw[:-1]
    return raw


def parse(segment: str, token: str) -> tuple[int, str, str] | None:
    """从缓冲区片段里切出 (rc, stdout, stderr)，标记不完整时返回 None。"""
    parsed = parse_raw(segment, token)
    if parsed is None:
        return None
    rc, stdout, stderr = parsed
    return rc, strip_separator(stdout).strip("\n"), strip_separator(stderr).strip("\n")
