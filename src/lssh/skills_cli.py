"""``lssh skills``：把技能装进本机的 AI Agent。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from . import skills as sk


def _targets(project: bool) -> list[sk.Target]:
    return sk.project_targets(Path.cwd()) if project else list(sk.TARGETS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lssh skills",
        description="把 lssh 的 Agent Skill 装进本机检测到的 AI Agent。",
    )
    sub = parser.add_subparsers(dest="command")

    install = sub.add_parser("install", help="安装或更新技能（默认只预览）")
    install.add_argument("-y", "--yes", action="store_true", help="真正写入；不加只显示计划")
    install.add_argument("--force", action="store_true", help="覆盖不是 lssh 写入的同名目录")
    install.add_argument(
        "-a", "--agent", action="append", default=[], help="只处理指定 Agent，可重复"
    )
    install.add_argument(
        "--project", action="store_true", help="装到当前目录（./.agents、./.claude）"
    )
    install.add_argument("--json", action="store_true", help="输出 JSON")

    uninstall = sub.add_parser("uninstall", help="卸载技能（只删 lssh 写入的目录）")
    uninstall.add_argument("-y", "--yes", action="store_true", help="真正删除；不加只显示计划")
    uninstall.add_argument(
        "-a", "--agent", action="append", default=[], help="只处理指定 Agent，可重复"
    )
    uninstall.add_argument("--project", action="store_true", help="处理当前目录")
    uninstall.add_argument("--json", action="store_true", help="输出 JSON")

    status = sub.add_parser("status", help="查看每个 Agent 的安装状态")
    status.add_argument("--project", action="store_true", help="查看当前目录")
    status.add_argument("--json", action="store_true", help="输出 JSON")

    show = sub.add_parser("show", help="打印技能正文（SKILL.md）")
    show.add_argument("--path-only", action="store_true", help="只打印目录路径")

    return parser


def _check_agents(agents: list[str], targets: list[sk.Target]) -> None:
    known = {t.agent for t in targets}
    unknown = [a for a in agents if a not in known]
    if unknown:
        raise SystemExit(
            f"lssh skills: 未知 Agent：{', '.join(unknown)}\n可选：{', '.join(sorted(known))}"
        )


def _split(rows: list[dict]) -> dict:
    written = [
        {k: v for k, v in r.items() if k != "installed_version" or v}
        for r in rows
        if not r["action"].startswith("skip")
    ]
    conflicts = [r["path"] for r in rows if r["action"].startswith("skip: existing")]
    return {
        "targets": written,
        "agents_not_found": [
            r["agent"] for r in rows if r["action"] == "skip: agent not installed"
        ],
        "conflicts": conflicts,
    }


def _report_written(rows: list[dict]) -> None:
    for row in rows:
        if not row["action"].startswith(("install", "update", "replace")):
            continue
        print(f"  {row['action']:<8} {row['agent']:<16} {row['path']}")
    missing = [r["agent"] for r in rows if r["action"] == "skip: agent not installed"]
    if missing:
        print(f"  未检测到：{', '.join(missing)}")


def _cmd_install(args: argparse.Namespace) -> int:
    targets = _targets(args.project)
    _check_agents(args.agent, targets)
    if not args.yes:
        rows = sk.plan(targets, args.agent or None)
        data = {"applied": False, **_split(rows)}
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print("将把技能写到：")
            _report_written(rows)
            conflicts = data["conflicts"]
            if conflicts:
                print("  已有同名目录（不是 lssh 装的），确认替换加 --force：")
                for path in conflicts:
                    print(f"    {path}")
            print("确认无误后执行：lssh skills install --yes")
        return 0

    rows = sk.install(targets, args.agent or None, force=args.force)
    data = {"applied": True, **_split(rows)}
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print(f"lssh {__version__} 技能已安装：")
    _report_written(rows)
    conflicts = data["conflicts"]
    if conflicts:
        print("  跳过（已有同名目录，需要 --force）：")
        for path in conflicts:
            print(f"    {path}")
    installed = [r["agent"] for r in data["targets"] if r.get("done")]
    if installed:
        print("新开一个 Agent 会话即可使用；OpenCode / Codex / Cursor 等读 ~/.agents/skills。")
    return 0


def _cmd_uninstall(args: argparse.Namespace) -> int:
    targets = _targets(args.project)
    _check_agents(args.agent, targets)
    if not args.yes:
        rows = [r for r in sk.status(targets)["targets"] if r["installed"]]
        rows = [r for r in rows if not args.agent or r["agent"] in args.agent]
        if args.json:
            print(
                json.dumps(
                    {"applied": False, "would_remove": [r["path"] for r in rows]},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif rows:
            print("将删除：")
            for row in rows:
                print(f"  {row['agent']:<16} {row['path']}")
            print("确认后执行：lssh skills uninstall --yes")
        else:
            print("没有由 lssh 安装的技能。")
        return 0

    removed = sk.uninstall(targets, args.agent or None)
    if args.json:
        print(json.dumps({"applied": True, "removed": removed}, ensure_ascii=False, indent=2))
    elif removed:
        for row in removed:
            print(f"已删除 {row['agent']:<16} {row['path']}")
    else:
        print("没有由 lssh 安装的技能。")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    data = sk.status(_targets(args.project))
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    for row in data["targets"]:
        if not row["detected"]:
            continue
        if row["installed"]:
            state = "up-to-date" if row["up_to_date"] else "stale"
            print(f"{row['agent']:<16} {state:<11} {row['version'] or '-':<8} {row['path']}")
        else:
            print(f"{row['agent']:<16} {'not installed':<11} {'-':<8} {row['path']}")
    covered = data["agents_using_standard_dir"]
    if covered:
        print(f"\n读 ~/.agents/skills 的 Agent：{', '.join(covered)}")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    data = sk.show()
    if args.path_only:
        print(data["path"])
    else:
        print(f"# {data['path']}\n")
        print(data["skill_md"], end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    handler = {
        "install": _cmd_install,
        "uninstall": _cmd_uninstall,
        "status": _cmd_status,
        "show": _cmd_show,
    }[args.command]
    return handler(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
