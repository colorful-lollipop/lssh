from __future__ import annotations

import json
import os

from lssh import askpass, config


def test_host_from_prompt():
    assert askpass.host_from_prompt("root@10.0.0.9's password:") == "10.0.0.9"
    assert askpass.host_from_prompt("user@host.example.com's password:") == "host.example.com"
    assert askpass.host_from_prompt("Password:") == ""


def test_lookup_exact_and_wildcard(tmp_path):
    path = tmp_path / "secrets"
    path.write_text(
        "# comment\n10.0.0.9 hunter2\n*.example.com wildcard\nbroken-line-without-password\n"
    )
    assert askpass.lookup("10.0.0.9", path) == "hunter2"
    assert askpass.lookup("host.example.com", path) == "wildcard"
    assert askpass.lookup("10.0.0.10", path) is None


def test_lookup_star(tmp_path):
    path = tmp_path / "secrets"
    path.write_text("* fallback\n")
    assert askpass.lookup("anything", path) == "fallback"


def test_missing_file(tmp_path):
    assert askpass.lookup("10.0.0.9", tmp_path / "nope") is None


def test_resolve_from_prompt(tmp_path):
    path = tmp_path / "secrets"
    path.write_text("10.0.0.9 hunter2\n")
    assert askpass.resolve("root@10.0.0.9's password:", path) == "hunter2"


def test_main_exit_codes(tmp_path, capsys, monkeypatch):
    secrets = tmp_path / "secrets"
    secrets.write_text("10.0.0.9 hunter2\n")
    monkeypatch.setattr(askpass, "SECRETS_PATH", secrets)
    assert askpass.main(["lssh-askpass", "root@10.0.0.9's password:"]) == 0
    assert capsys.readouterr().out == "hunter2\n"
    assert askpass.main(["lssh-askpass", "root@10.0.0.10's password:"]) == 1


def test_config_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / "lssh").mkdir()
    (tmp_path / "lssh" / "config.json").write_text(
        json.dumps({"timeout": 30, "hosts": {"a5-*": {"timeout": 600}}})
    )
    assert config.resolve("a5-53")["timeout"] == 600
    assert config.resolve("bj")["timeout"] == 30
    assert config.resolve(None)["timeout"] == 30
    assert config.resolve("bj")["tmux"] == "lssh"


def test_init_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    first = config.init_files()
    assert all(created for _, created in first)
    second = config.init_files()
    assert not any(created for _, created in second)
    assert (tmp_path / "lssh" / "secrets").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "lssh" / "config.json").exists()
    assert os.path.isdir(tmp_path / "lssh")
