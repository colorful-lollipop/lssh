# lssh

[![CI](https://github.com/colorful-lollipop/lssh/actions/workflows/ci.yml/badge.svg)](https://github.com/colorful-lollipop/lssh/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
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
git clone https://github.com/colorful-lollipop/lssh && cd lssh
python3 -m venv .venv
.venv/bin/pip install -e .
ln -sf "$PWD/.venv/bin/lssh" ~/.local/bin/lssh
```

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
| `--askpass PATH` | `SSH_ASKPASS` script (see below) |
| `--ssh-arg ARG` | extra argument passed to `ssh`, repeatable |
| `--idle N` | close an idle channel after N seconds, default 1800 |
| `--local SHELL` | debug: run against a local shell instead of ssh |

The exit code is the remote command's (0–255); connection, timeout and verification
failures return 1 with the reason on stderr.

## Credentials

lssh never stores or prints passwords. If a gateway forces password auth, OpenSSH's
askpass hook is used and lssh ships the helper itself (`lssh-askpass`). Resolution order:

1. `$LSSH_PASSWORD_CMD` — any command whose stdout is the password (use a secret manager)
2. system keyring — `secret-tool`, service `lssh`, host `<host>`
3. `~/.config/lssh/secrets` — one `<host-pattern> <password>` per line, mode 0600
4. legacy `# lssh-pw` comment in `~/.ssh/config` (deprecated)

```bash
lssh --init                                       # ~/.config/lssh/{config.json,secrets}
export LSSH_PASSWORD_CMD='pass show lssh/bastion' # recommended
printf '10.0.0.9 %s\n' "$PW" >> ~/.config/lssh/secrets   # plaintext fallback
```

Nothing needs to live in `~/.ssh`. See [docs/configuration.md](docs/configuration.md)
for the full resolution rules and per-host settings.

## Agent skill

The repository ships a skill for coding agents (`skills/lssh/`):

```bash
# A. symlink (recommended — always tracks the repo)
ln -s "$PWD/skills/lssh" ~/.config/opencode/skills/lssh

# B. copy
cp -r skills/lssh ~/.config/opencode/skills/
```

Or let opencode scan the repository directly:

```jsonc
{ "skills": ["~/code/lssh/skills"] }
```

The skill is a thin index (when to use it, four rules, three failure modes). Details live
in `skills/lssh/references/details.md`; command semantics live in `lssh -h`.

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
- Commands on one host are serialized (one shell, one foreground process); different
  hosts run in parallel.
- `state=dirty` in `--list` means the previous run failed; the next command reconnects.

## Development

```bash
make install   # venv + editable install with dev extras
make test      # 30 tests, no network required
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

## License

MIT
