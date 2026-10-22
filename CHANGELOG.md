# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-10-22

### Added

- `lssh skills install|status|uninstall|show`: copies the bundled Agent Skill into
  `~/.agents/skills/lssh` and into agents whose config folder already exists (Claude Code,
  OpenClaw, Qwen Code, Cline, Kiro, Goose, DeepSeek Harness…). A marker file keeps
  upgrades and uninstall away from hand-written skills.
- One-line installer (`scripts/install.sh`) and agent-facing `docs/install.md`.
- The installed skill carries `references/commands.md`, generated from the live CLI.

### Changed

- Distribution name is `lssh-cli` (the PyPI name `lssh` is taken); the command is still
  `lssh`.
- Skill files moved into the package (`src/lssh/_skills/lssh`) so wheels ship them and
  `lssh skills install` works outside a source checkout.
- `lssh -h` points at the `skills` subcommand.

## [0.3.0] - 2026-10-22

### Added

- `lssh-askpass` helper shipped with the package. Credentials come from a single file,
  `~/.config/lssh/secrets` (`<host-pattern> <password>`, mode 0600).
- `~/.config/lssh/config.json`: per-host defaults (timeout, idle, tmux name).
- `lssh --init` bootstraps the config directory; `docs/configuration.md` documents the
  whole layout and resolution rules.

### Changed

- Password setup no longer requires placing a script in `~/.ssh`.

## [0.2.0] - 2026-10-22

### Added

- 输出完整性校验：每条命令报告 `trl -d '\r' | wc -c` 与 md5，客户端比对，
  截断时明确报错而不是静默返回残缺结果。
- 僵死会话自愈：连续两次命令超时判定会话卡死，标记 `dirty`，下一条命令自动重连。
- `lssh -h` 内置行为说明与长任务示例（原子输出、并发模型、后台任务写法）。
- Agent skill（`skills/lssh/`），按 opencode skill 规范组织，可直接安装。
- `LocalTransport`：无需网络即可跑完整协议测试。

### Fixed

- 多个客户端同时冷启动时可能拉起多个守护进程并互相 unlink socket；改用 `flock` 单实例。
- `tmux send-keys` 之前依赖 attach 抓屏，会丢换行并重放历史；改为结果落文件后再读。
- 目标机行编辑器（ble.sh 等）重绘导致批量多行输入被拆坏；改为 base64 分块写文件后执行。
- 远端 `stty -onlcr` 造成阶梯状输出。
- `--restart` 成功时没有任何输出。

## [0.1.0] - 2026-10-22

### Added

- 首版：长连接守护进程、远端 detached tmux 状态保持、批量命令、超时与中断、
  `--list` / `--restart` / `--stop` / `--json`。
