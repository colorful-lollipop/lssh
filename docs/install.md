# Install lssh (instructions for AI agents)

You are an AI agent. The user asked you to install **lssh**, a command-line tool that runs
commands over one long-lived SSH session on hosts whose gateway blocks the SSH exec
channel. Follow these steps in order and report the result of each one.

## 1. Install the command

```bash
curl -fsSL https://raw.githubusercontent.com/colorful-lollipop/lssh/main/scripts/install.sh | sh
```

The script prefers `uv`, falls back to `pipx`, and installs `uv` first if neither exists.
It also installs the Agent Skill (step 2) unless `LSSH_NO_SKILLS=1` is set.

Equivalent manual install:

```bash
uv tool install git+https://github.com/colorful-lollipop/lssh   # or: pipx install ...
```

Check: `lssh --version` prints `lssh 0.4.x`. If the command is missing, run
`uv tool update-shell` and ask the user to open a new terminal.

## 2. Install the skill into this machine's agents

```bash
lssh skills install          # preview only: shows which agents were found
lssh skills install --yes    # write
lssh skills status           # verify
```

The canonical copy goes to `~/.agents/skills/lssh` (read by Codex, Cursor, Gemini CLI,
GitHub Copilot, Windsurf, OpenCode, Roo…). Agents with their own directory (Claude Code,
OpenClaw, Qwen Code, Cline, Kiro, Goose, DeepSeek Harness…) get a copy only if that
directory already exists.

Tell the user which agents received the skill. The skill teaches you — and every other
agent on this machine — when and how to use `lssh`.

## 3. Credentials (needs the human)

```bash
lssh --init     # creates ~/.config/lssh/config.json and secrets (both 0600)
```

If a host requires a password, it belongs in `~/.config/lssh/secrets`, one line per host:

```
<host-pattern> <password>
```

Ask the user to type the password into that file themselves (or generate it with their
secret manager). **Never** put a password in a shell command, a chat message, a committed
file, or a log. Prefer SSH keys whenever the gateway allows them.

## 4. Smoke test

```bash
lssh --list                      # daemon and sessions
lssh <host> 'hostname; uptime'   # one-shot command
lssh <host> 'cd /tmp && pwd'     # cwd persists on the same host
```

If the first connection fails with `远端 shell 未就绪`, the gateway is usually
rate-limiting: wait 2–3 minutes and try once. Do not retry in a loop.

## 5. Finish

Read the installed skill (`lssh skills show`) and tell the user, in one short paragraph,
what you can now do: run commands on the configured hosts over a single connection, keep
cwd/env between commands, queue several agents on the same host, and handle long jobs via
background processes plus log polling.

## Optional

- `lssh skills install --project` installs into `./.agents` and `./.claude` for a single
  repository instead of the user's home.
- `lssh skills install --agent claude-code --yes` limits the install to one agent.
- `lssh --help` documents timeouts, tmux naming, `--json`, and the REPL.
- Configuration (`~/.config/lssh/config.json`) supports per-host defaults; see
  [configuration.md](configuration.md).
