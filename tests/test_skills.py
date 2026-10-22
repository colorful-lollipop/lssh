from __future__ import annotations

import json

import pytest

from lssh import skills
from lssh.skills_cli import main as skills_main


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / ".config"))
    for var in ("CLAUDE_CONFIG_DIR", "OPENCLAW_HOME", "DSH_HOME", "CODEX_HOME"):
        monkeypatch.delenv(var, raising=False)
    return root


def test_bundled_skill_exists():
    src = skills.bundled_dir()
    assert (src / "SKILL.md").is_file()
    assert (src / "references" / "details.md").is_file()
    assert "name: lssh" in (src / "SKILL.md").read_text(encoding="utf-8")


def test_commands_reference_comes_from_cli():
    text = skills.commands_reference()
    assert "--timeout" in text
    assert "lssh skills install" in text


def test_plan_only_targets_detected_agents(home, tmp_path):
    (home / ".claude").mkdir()
    rows = {row["agent"]: row for row in skills.plan(skills.TARGETS)}
    assert rows["agents"]["action"] == "install"
    assert rows["claude-code"]["action"] == "install"
    assert rows["qwen-code"]["action"] == "skip: agent not installed"


def test_install_update_uninstall(home):
    rows = skills.install(skills.TARGETS, ["agents"])
    dest = home / ".agents" / "skills" / "lssh"
    assert rows[0]["action"] == "install"
    assert (dest / "SKILL.md").is_file()
    assert (dest / "references" / "details.md").is_file()
    assert (dest / "references" / "commands.md").is_file()

    marker = json.loads((dest / skills.MARKER).read_text(encoding="utf-8"))
    assert marker["version"]
    assert marker["hash"]

    state = skills.status(skills.TARGETS)
    row = next(r for r in state["targets"] if r["agent"] == "agents")
    assert row["installed"] and row["up_to_date"]

    assert skills.install(skills.TARGETS, ["agents"])[0]["action"] == "update"

    removed = skills.uninstall(skills.TARGETS, ["agents"])
    assert [r["agent"] for r in removed] == ["agents"]
    assert not dest.exists()


def test_conflict_needs_force(home):
    dest = home / ".agents" / "skills" / "lssh"
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text("hand written", encoding="utf-8")

    row = skills.plan(skills.TARGETS, ["agents"])[0]
    assert row["action"].startswith("skip: existing")

    skills.install(skills.TARGETS, ["agents"])
    assert (dest / "SKILL.md").read_text(encoding="utf-8") == "hand written"

    skills.install(skills.TARGETS, ["agents"], force=True)
    assert (dest / skills.MARKER).is_file()
    assert "name: lssh" in (dest / "SKILL.md").read_text(encoding="utf-8")


def test_uninstall_leaves_foreign_folder(home):
    dest = home / ".agents" / "skills" / "lssh"
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text("not ours", encoding="utf-8")
    assert skills.uninstall(skills.TARGETS) == []
    assert dest.exists()


def test_project_targets(tmp_path):
    rows = skills.install(skills.project_targets(tmp_path), None)
    assert {r["agent"] for r in rows if r.get("done")} == {"project-agents", "project-claude"}
    assert (tmp_path / ".agents" / "skills" / "lssh" / "SKILL.md").is_file()
    assert (tmp_path / ".claude" / "skills" / "lssh" / "SKILL.md").is_file()


def test_cli_preview_then_install(home, capsys):
    assert skills_main(["install"]) == 0
    out = capsys.readouterr().out
    assert "lssh skills install --yes" in out
    assert not (home / ".agents" / "skills" / "lssh").exists()

    assert skills_main(["install", "--yes", "--agent", "agents"]) == 0
    assert (home / ".agents" / "skills" / "lssh" / "SKILL.md").is_file()
    capsys.readouterr()

    assert skills_main(["status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    row = next(r for r in data["targets"] if r["agent"] == "agents")
    assert row["installed"] and row["up_to_date"]

    assert skills_main(["show", "--path-only"]) == 0
    assert capsys.readouterr().out.strip().endswith("_skills/lssh")


def test_cli_rejects_unknown_agent(home):
    with pytest.raises(SystemExit):
        skills_main(["install", "--yes", "--agent", "nope"])
