# Security policy

## Reporting a vulnerability

Please report security issues privately (GitHub Security Advisories or email the
maintainer) instead of opening a public issue. You can expect an initial response within
a few days.

## Threat model

`lssh` is a local convenience tool that drives the system OpenSSH client:

- It never stores, reads, or logs credentials. Password authentication, when unavoidable,
  is delegated to an `SSH_ASKPASS` script owned by the user.
- It does not listen on the network. The daemon exposes a Unix socket with mode `0600`
  under `$XDG_RUNTIME_DIR` (or `~/.cache/lssh`).
- Commands are written to the remote shell as text; the tool does not sanitize them. Any
  caller that can talk to the daemon socket can execute commands with the SSH user's
  privileges — treat the socket like your SSH agent socket.
- Remote temporary files (`/tmp/.lssh-<token>.*`) hold command output until the result is
  read, then are removed. On multi-user targets, prefer a private `TMPDIR`.
- The tool does not implement SSH itself; it inherits the security properties (and
  `~/.ssh/config` host key policy) of the OpenSSH client.
