from __future__ import annotations

import json
import os

from lssh import askpass, config


def test_host_from_prompt():
    assert askpass.host_from_prompt("root@10.0.0.9's password:") == "10.0.0.9"
    assert askpass.host_from_prompt("user@host.example.com's password:") == "host.example.com"
    assert askpass.host_from_prompt("Password:") == ""


def test_from_command(monkeypatch):
    monkeypatch.setenv("LSSH_PASSWORD_CMD", "printf '  spaced  \\n'")
    assert askpass.from_command() == "  spaced  "
    monkeypatch.setenv("LSSH_PASSWORD_CMD", "false")
    assert askpass.from_command() is None
    monkeypatch.delenv("LSSH_PASSWORD_CMD")
    assert askpass.from_command() is None


def test_from_secrets_file(tmp_path):
    path = tmp_path / "secrets"
    path.write_text("# comment\n10.0.0.9 hunter2\n*.example.com wildcard\n")
    assert askpass.from_secrets_file("10.0.0.9", path) == "hunter2"
    assert askpass.from_secrets_file("host.example.com", path) == "wildcard"
    assert askpass.from_secrets_file("10.0.0.10", path) is None


def test_from_ssh_config_legacy(tmp_path):
    path = tmp_path / "config"
    path.write_text("Host a\n    HostName 10.0.0.9\n")
    assert askpass.from_ssh_config("10.0.0.9", path) is None
    path.write_text("# lssh-pw 10.0.0.9 hunter2\n")
    assert askpass.from_ssh_config("10.0.0.9", path) == "hunter2"


def test_resolve_prefers_command(tmp_path, monkeypatch):
    secrets = tmp_path / "secrets"
    secrets.write_text("10.0.0.9 from-file\n")
    monkeypatch.setenv("LSSH_PASSWORD_CMD", "printf from-cmd")
    assert askpass.resolve("root@10.0.0.9's password:", secrets) == "from-cmd"

    monkeypatch.delenv("LSSH_PASSWORD_CMD")
    assert askpass.resolve("root@10.0.0.9's password:", secrets) == "from-file"


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
