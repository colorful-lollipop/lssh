# Configuration

lssh keeps everything it owns under one directory. Nothing is written to `~/.ssh`,
and nothing is installed on the remote host.

## Files

```
${XDG_CONFIG_HOME:-~/.config}/lssh/      # 0700
├── config.json                          # 0600  non-secret settings
├── secrets                              # 0600  optional plaintext fallback
└── (askpass.sh)                          # optional, only for custom setups
```

Runtime state, never touched by hand:

```
${XDG_RUNTIME_DIR:-~/.cache}/lssh/daemon.sock   # 0600, one per user
${XDG_CACHE_HOME:-~/.cache}/lssh/daemon.log     # no command content is logged
```

`lssh --init` creates the directory and the two files (idempotent).

## config.json

```jsonc
{
  "timeout": 120,          // per-command timeout, seconds
  "idle": 1800,            // close an idle channel after N seconds
  "tmux": "lssh",          // remote tmux session name
  "connect_timeout": 15,   // ssh ConnectTimeout
  "cols": 512,             // pty columns (wide = fewer wrapped lines)
  "rows": 50,
  "askpass": null,         // override the askpass helper path
  "hosts": {
    "a5-*": { "timeout": 600, "idle": 7200 },
    "prod-db": { "tmux": "lssh-db" }
  }
}
```

Resolution order for every key:

1. command-line flag
2. `hosts` entry whose pattern matches the host (glob, first match wins)
3. top-level value
4. built-in default

`hosts` keys are glob patterns matched with `fnmatch` against the host as written on
the command line (an `~/.ssh/config` alias, or `user@host`).

## Credentials

lssh never stores, reads or prints passwords by itself. When a gateway forces password
authentication, OpenSSH calls `$SSH_ASKPASS`; lssh points it at the `lssh-askpass`
helper installed with the package, which resolves the password in this order:

| # | Source | Notes |
|---|---|---|
| 1 | `$LSSH_PASSWORD_CMD` | command whose stdout is the password — use a real secret manager |
| 2 | system keyring | `secret-tool lookup service lssh host <host>` (libsecret) |
| 3 | `~/.config/lssh/secrets` | one `<host-pattern> <password>` per line, mode 0600 |
| 4 | `~/.ssh/config` | legacy `# lssh-pw <host> <password>` line, deprecated |

Examples:

```bash
# 1. secret manager (recommended)
export LSSH_PASSWORD_CMD='pass show lssh/bastion'

# 2. keyring
printf '%s' "$PW" | secret-tool store --label='lssh bastion' service lssh host 10.0.0.9

# 3. plaintext fallback
printf '10.0.0.9 %s\n' "$PW" >> ~/.config/lssh/secrets   # file is 0600
```

Custom helper: `--askpass /path/to/script`, or `"askpass": "..."` in `config.json`,
or `$LSSH_ASKPASS`. All three take precedence over the built-in helper.

The built-in helper lives in `src/lssh/askpass.py` and is installed as the
`lssh-askpass` entry point, so it is versioned and tested with the rest of the package.

## Rule of thumb

- Prefer keys. Passwords are a gateway limitation, not a feature.
- Never commit secrets, and never pass them on the command line (they show up in `ps`).
- Keep `secrets` at mode 0600; `lssh --init` does this for you.
