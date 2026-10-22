---
name: Bug report
about: Something does not work as documented
---

**What happened**

**What you expected**

**Reproduce**

```bash
# commands
```

**Environment**

- lssh version (`lssh --version`):
- Python version:
- OS / shell:
- Target: plain sshd / bastion (exec channel blocked?) / tmux available?

**Diagnostics**

```
lssh --list
tail -20 ~/.cache/lssh/daemon.log
```

Please remove passwords, hostnames you do not want public, and command output that
contains secrets before pasting.
