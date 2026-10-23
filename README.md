# lssh

[![CI](https://github.com/colorful-lollipop/lssh/actions/workflows/ci.yml/badge.svg)](https://github.com/colorful-lollipop/lssh/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![Agent Skills](https://img.shields.io/badge/Agent%20Skills-ready-A46BFF)](docs/install.md)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**English** | [简体中文](README.zh-CN.md)

**Long-lived SSH sessions for machines that refuse the exec channel.**

Some SSH gateways (cloud bastions, jump hosts, vendor "ops channels") implement only the
SSH *shell* channel. An exec request (`ssh host 'cmd'`) is rejected outright, and
`scp`/`rsync` fail with it because they need the subsystem channel. These gateways are
also often rate-limited, so a script that loops over `ssh host cmd` quickly gets throttled
or banned.

`lssh` trades "one login per command" for **one long-lived connection**:

- a local daemon keeps a single `ssh -tt` session per host;
- inside it, a **detached tmux** session (default name `lssh`) keeps cwd, environment
  variables and background jobs alive across reconnects;
- commands are shipped as base64 chunks into a remote temp file and executed there, so
  line editors (ble.sh, zsh syntax highlighting) cannot mangle multi-line input, and no
  prompt ever leaks into stdout;
- every command carries a random token, so stdout, stderr and the exit code are split
  precisely — and verified by length + md5 before being returned.

To callers it behaves like a working `ssh host cmd`:

```console
$ lssh a5-53 'npu-smi info -l | head -5'
$ lssh web-1 'cd /opt/app && git log -1 --oneline'
```

## One-line install

Send this to your agent — it installs the CLI, teaches itself the skill, and reports back:

```text
Install lssh from https://github.com/colorful-lollipop/lssh (run its `scripts/install.sh`),
then run `lssh skills install --yes` and show me `lssh skills status`.
```

From a terminal, [`scripts/install.sh`](scripts/install.sh) does the same thing.

## How it works

```
 lssh (CLI)                 lsshd (daemon)                    remote
   │  JSON over Unix socket    │                                │
   ├──────────────────────────►│  ssh -tt (one connection)      │
   │                           ├───────────────────────────────►│ login shell
   │                           │                                └─ tmux "lssh"
   │                           │                                      └─ your command
   │◄──────────────────────────┤  stdout / stderr / rc           │
```

Per command:

```sh
# 1. write the script (base64 chunks, immune to line editors)
printf %s '<chunk>' >> /tmp/.lssh-<tok>.b64
base64 -d /tmp/.lssh-<tok>.b64 > /tmp/.lssh-<tok>.sh

# 2. run it inside the tmux pane, output goes to files
tmux send-keys -t lssh -l '. /tmp/.lssh-<tok>.sh'
tmux send-keys -t lssh Enter
#   script: { cmd; } > out 2> err; echo $? > rc; : > done

# 3. once done, print markers and cat the files (with size + md5)
```

Output never travels through a rendered tmux screen, so line breaks, blank lines and
binary-ish payloads survive.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/colorful-lollipop/lssh/main/scripts/install.sh | sh
```

The script installs the command with `uv` (or `pipx`, bootstrapping `uv` if neither
exists), then runs `lssh skills install --yes`. Set `LSSH_NO_SKILLS=1` to skip the skill.

<details>
<summary>Manual / from source</summary>

```bash
uv tool install git+https://github.com/colorful-lollipop/lssh   # or: pipx install ...
```

```bash
git clone https://github.com/colorful-lollipop/lssh && cd lssh
python3 -m venv .venv && .venv/bin/pip install -e .
ln -sf "$PWD/.venv/bin/lssh" ~/.local/bin/lssh
```

</details>

Requirements: an OpenSSH client and Python ≥ 3.9 locally. The remote side needs a POSIX
shell plus `base64` (falls back to `openssl base64 -d`) and, for state persistence,
`tmux` — everything is stock; **nothing is installed on the target**.

## Usage

```bash
lssh <host> 'cmd'           # run (separate multiple commands with ; or newlines)
lssh <host>                 # line-by-line REPL against one remote shell
lssh --list                 # sessions: state / idle / uptime / busy
lssh --restart <host>       # reconnect (the remote tmux session survives)
lssh --json <host> 'cmd'    # raw JSON result
lssh -h                     # behaviour notes and long-task recipe
```

Options must come before the host (same as `ssh`):

| Option | Meaning |
|---|---|
| `--timeout N` | per-command timeout, default 120 s |
| `--no-tmux` | do not use remote tmux (loses state across reconnects) |
| `--tmux NAME` | remote tmux session name, default `lssh` |
| `--ssh-arg ARG` | extra argument passed to `ssh`, repeatable |
| `--idle N` | close an idle channel after N seconds, default 1800 |
| `--local SHELL` | debug: run against a local shell instead of ssh |

The exit code is the remote command's (0–255); connection, timeout and verification
failures return 1 with the reason on stderr.

## Credentials

lssh never stores or prints passwords. When a gateway forces password auth, OpenSSH's
askpass hook is used and lssh ships the helper (`lssh-askpass`), which reads one file:

`~/.config/lssh/secrets` — `<host-pattern> <password>` per line, mode `0600`, `*` wildcard:

```bash
lssh --init                                     # creates config.json + secrets (0600)
printf '10.0.0.9 %s\n' "$PW" >> ~/.config/lssh/secrets
```

Prefer keys where possible. `secrets` may be a symlink, so a secret manager can generate
it for you. See [docs/configuration.md](docs/configuration.md).

## Agent skill

The wheel ships an Agent Skill; one command puts it where your agents look:

```bash
lssh skills install          # preview: which agents, which paths
lssh skills install --yes    # write
lssh skills status           # installed / stale / missing
lssh skills show             # print SKILL.md (to copy it anywhere by hand)
```

One canonical copy goes to `~/.agents/skills/lssh`, the cross-agent location read by
Codex, Cursor, Gemini CLI, GitHub Copilot, Windsurf, OpenCode and others. Agents that only
read their own folder (Claude Code, OpenClaw, Qwen Code, Cline, Kiro, Goose, DeepSeek
Harness…) get a copy when that folder already exists — nothing is created for agents you
do not have.

Every directory lssh writes carries `.lssh-install.json`; upgrades and
`lssh skills uninstall --yes` only touch those. `--project` installs into `./.agents` and
`./.claude` for a single repository, `--agent NAME` limits the run to one agent. The skill
also bundles `references/commands.md`, generated from the live CLI, so it cannot drift
from `lssh -h`.

## Known limitations

- The remote shell must have POSIX `sh` semantics.
- Commands run in an interactive shell, therefore:
  - stdin is not forwarded (`echo ... | cmd` or write a file instead);
  - programs that require a TTY (`top`, `vim`) are not suitable;
  - do not call `exit` — it ends the session (the daemon reconnects automatically).
- Output is returned **when the command finishes** (atomic, checksummed). There is no
  streaming; run long jobs in the background and poll their log.
- A timeout sends `Ctrl-C` and waits 3 s; if that does not settle the shell, the session
  is rebuilt. Whether `Ctrl-C` actually interrupts depends on the remote pty having a
  controlling terminal (usually true over SSH).
- One host gets up to 3 concurrent slots (`--slots N`), so a command that hangs for
  hours no longer blocks a quick query. Commands land on a free slot; **sequential
  commands stick to the same slot**, so `cd`/`export` still persist — but two commands
  running concurrently have independent cwd/env (use absolute paths). Only when all
  slots are taken does lssh answer "busy" (`--wait 30` queues). Different hosts always
  run in parallel.
- `state=dirty` in `--list` means the previous run failed; the next command reconnects.

## Development

```bash
make install   # venv + editable install with dev extras
make test      # full suite, no network required
make lint      # ruff check + format check
```

`LocalTransport` exercises the full protocol without ssh or a bastion:

```python
from lssh.session import Session
from lssh.transports import LocalTransport

s = Session(host="local", transport=LocalTransport(), tmux=None)
print(s.run("cd /tmp && pwd").stdout)
```

## Docs

- [CHANGELOG.md](CHANGELOG.md) — release notes
- [CONTRIBUTING.md](CONTRIBUTING.md) — design constraints and test setup
- [SECURITY.md](SECURITY.md) — threat model and reporting
- [docs/configuration.md](docs/configuration.md) — files, settings, credential sources
- [docs/install.md](docs/install.md) — install instructions for AI agents

## License

MIT
