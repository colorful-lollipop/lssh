# Contributing

Thanks for taking the time to contribute.

## Development setup

```bash
git clone <repo> && cd lssh
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

## Checks before sending a PR

```bash
make lint     # ruff check + ruff format --check
make test     # pytest（本地 shell 与真实 tmux，不需要网络）
```

CI runs the same checks on Python 3.9 / 3.11 / 3.13.

## Design constraints (please keep these)

1. **Do not stream command output by default.** Output is collected in a temp file on the
   target and read back in one piece so it can be checksummed. Streaming is an opt-in
   feature for humans, not the default for agents.
2. **Never add code that must run on the target.** `lssh` is client-side only: sshd, a login
   shell, and optionally `tmux` are all it may rely on.
3. **Avoid the exec channel entirely.** Some gateways only implement the shell channel;
   anything that needs `scp`, `direct-tcpip`, or `subsystem` is out of scope.
4. **Commands on one host are serialized.** Concurrency across hosts is fine; per-host
   parallelism would require more shells and is deliberately not implemented.
5. **Secrets never enter this repository or its logs.** Password handling is delegated to
   `SSH_ASKPASS`; do not read or print `~/.ssh/config` password lines.

## Tests

- Unit tests use `LocalTransport` (a local shell instead of ssh).
- Timeout/interrupt tests spin up a real `tmux` session and clean it up afterwards.
- A real-host smoke test runs only when `LSSH_TEST_SSH=<host>` is set.

## Commit messages

Conventional-ish, imperative subject, wrapped body. One logical change per commit.
