"""把内置的 Agent Skill 装进本机检测到的 AI Agent。

一份规范副本写到 ``~/.agents/skills/lssh``（Codex、Cursor、Gemini CLI、Copilot、
Windsurf、Roo、OpenCode…都读这个跨 Agent 标准目录）。只读自己目录的 Agent
（Claude Code、OpenClaw、Qwen Code…）各拿到一份副本，但**只在该 Agent 的配置
目录已经存在时**才写——绝不为用户没装的 Agent 创建目录。

每个由本工具写入的目录都带 ``.lssh-install.json`` 标记，升级/卸载只碰带标记的
目录，不会覆盖别人手写的同名技能。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .paths import config_dir

SKILL_NAME = "lssh"
MARKER = ".lssh-install.json"


# --------------------------------------------------------------------- 目标目录


def _home() -> Path:
    return Path.home()


def _env_dir(var: str, default: Path) -> Path:
    value = os.environ.get(var)
    return Path(value) if value else default


@dataclass(frozen=True)
class Target:
    """一个 Agent 的技能目录：``<config>/skills/<name>``。"""

    agent: str
    root: Callable[[], Path]
    always: bool = False

    def skill_dir(self) -> Path:
        return self.root() / "skills" / SKILL_NAME

    def detected(self) -> bool:
        return self.always or self.root().is_dir()


TARGETS: list[Target] = [
    # 跨 Agent 标准目录，OpenCode 也会读
    Target("agents", lambda: _home() / ".agents", always=True),
    Target("claude-code", lambda: _env_dir("CLAUDE_CONFIG_DIR", _home() / ".claude")),
    Target("openclaw", lambda: _env_dir("OPENCLAW_HOME", _home() / ".openclaw")),
    Target("qwen-code", lambda: _home() / ".qwen"),
    Target("kiro", lambda: _home() / ".kiro"),
    Target("cline", lambda: _home() / ".cline"),
    Target("trae", lambda: _home() / ".trae"),
    Target("trae-cn", lambda: _home() / ".trae-cn"),
    Target("codebuddy", lambda: _home() / ".codebuddy"),
    Target("lingma", lambda: _home() / ".lingma"),
    Target("qoder", lambda: _home() / ".qoder"),
    Target("iflow", lambda: _home() / ".iflow"),
    Target("goose", lambda: config_dir().parent / "goose"),
    Target("deepseek-harness", lambda: _env_dir("DSH_HOME", _home() / ".dsh")),
]

# 这些 Agent 读 ~/.agents/skills，只用于 status 展示，不再重复写一份。
COVERED_BY_STANDARD: dict[str, Callable[[], Path]] = {
    "codex": lambda: _env_dir("CODEX_HOME", _home() / ".codex"),
    "cursor": lambda: _home() / ".cursor",
    "gemini-cli": lambda: _home() / ".gemini",
    "github-copilot": lambda: _home() / ".copilot",
    "windsurf": lambda: _home() / ".codeium" / "windsurf",
    "roo-code": lambda: _home() / ".roo",
    "opencode": lambda: config_dir().parent / "opencode",
}


def project_targets(cwd: Path) -> list[Target]:
    return [
        Target("project-agents", lambda: cwd / ".agents", always=True),
        Target("project-claude", lambda: cwd / ".claude", always=True),
    ]


def bundled_dir() -> Path:
    """包里带的技能目录（wheel 与源码树都适用）。"""
    packaged = Path(__file__).parent / "_skills" / SKILL_NAME
    if (packaged / "SKILL.md").is_file():
        return packaged
    raise FileNotFoundError("bundled skill not found: " + str(packaged))


# --------------------------------------------------------------------- 内容与标记


def commands_reference() -> str:
    """从当前 CLI 生成命令参考，保证技能文档不会和代码脱节。"""
    from .cli import build_parser

    return (
        "# lssh 命令参考\n\n"
        f"由 `lssh skills install` 根据 lssh {__version__} 自动生成。\n"
        "参数含义以 `lssh -h` 为准。\n\n"
        "```\n" + build_parser().format_help().rstrip() + "\n```\n"
    )


def _source_hash(src: Path, extra: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in src.rglob("*") if p.is_file()):
        digest.update(path.relative_to(src).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    for name, text in sorted(extra.items()):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(text.encode())
    return digest.hexdigest()[:16]


def read_marker(dest: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((dest / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------- plan / install


def plan(targets: Sequence[Target], agents: Sequence[str] | None = None) -> list[dict[str, Any]]:
    rows = []
    for target in targets:
        if agents and target.agent not in agents:
            continue
        dest = target.skill_dir()
        marker = read_marker(dest)
        if not target.detected():
            action = "skip: agent not installed"
        elif dest.is_symlink() or dest.exists():
            action = "update" if marker else "skip: existing folder was not installed by lssh"
        else:
            action = "install"
        rows.append(
            {
                "agent": target.agent,
                "path": str(dest),
                "action": action,
                "installed_version": (marker or {}).get("version"),
            }
        )
    return rows


def install(
    targets: Sequence[Target], agents: Sequence[str] | None = None, force: bool = False
) -> list[dict[str, Any]]:
    src = bundled_dir()
    extra = {"references/commands.md": commands_reference()}
    digest = _source_hash(src, extra)
    results = []
    for step in plan(targets, agents):
        action = step["action"]
        if action.startswith("skip: existing") and force:
            action = "replace"
        if action.startswith("skip"):
            results.append(step)
            continue

        dest = Path(step["path"])
        if dest.is_symlink() or dest.is_file():
            dest.unlink()
        elif dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest)
        ref = dest / "references"
        ref.mkdir(exist_ok=True)
        (ref / "commands.md").write_text(extra["references/commands.md"], encoding="utf-8")

        marker = {
            "skill": SKILL_NAME,
            "version": __version__,
            "hash": digest,
            "installed_at": int(time.time()),
        }
        (dest / MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        results.append({**step, "action": action, "done": True, "version": __version__})
    return results


def uninstall(
    targets: Sequence[Target], agents: Sequence[str] | None = None
) -> list[dict[str, Any]]:
    removed = []
    for target in targets:
        if agents and target.agent not in agents:
            continue
        dest = target.skill_dir()
        if read_marker(dest) is None:
            continue  # 只删自己写过的目录
        shutil.rmtree(dest)
        removed.append({"agent": target.agent, "path": str(dest), "removed": True})
    return removed


def status(targets: Sequence[Target]) -> dict[str, Any]:
    current = _source_hash(bundled_dir(), {"references/commands.md": commands_reference()})
    rows = []
    for target in targets:
        marker = read_marker(target.skill_dir())
        rows.append(
            {
                "agent": target.agent,
                "detected": target.detected(),
                "installed": marker is not None,
                "version": (marker or {}).get("version"),
                "up_to_date": bool(marker and marker.get("hash") == current),
                "path": str(target.skill_dir()),
            }
        )
    covered = [name for name, root in COVERED_BY_STANDARD.items() if root().is_dir()]
    return {"targets": rows, "agents_using_standard_dir": covered}


def show() -> dict[str, str]:
    src = bundled_dir()
    return {"path": str(src), "skill_md": (src / "SKILL.md").read_text(encoding="utf-8")}
