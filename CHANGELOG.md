# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **同机并发：每台机器默认 3 个槽位**。远端 tmux 会话里为每个槽位开一个 window
  （`lssh:w0`…`w2`，共用同一条 SSH 连接，不额外登录），命令投到空闲的那个。
  这样"一个长任务挂住、另一个命令查记录"才成立：真机（bj）实测长任务占住 w0 时，
  另一条 `tail` 只要 0.2–0.5s。`--slots N` 调整槽位数，`--slots 1` 完全退回串行；
  `--slot N` 把命令钉死在某个槽位。
- `--list` 按槽位列出每个 window 在跑什么（`BUSY 1/3`、`w0 running: ...`、
  `w1 远端仍在跑: ...`）。
- `Result.slot` / JSON `slot`：结果里带上"这条命令用的是哪个槽位"，排障不用猜。
- `config.json` 新增 `"slots"`（默认 3）。
- 测试：`tests/test_concurrency.py`（本机 tmux，含"长任务挂住时查询必须秒回"的
  时间断言）与 `tests/test_bj_live.py`（真机验收，`LSSH_TEST_SSH=bj` 打开）。
- `lssh --kill HOST`：Ctrl-C + 杀掉远端 tmux 会话 + 断开，下次调用自动重连。
  这是"pane 被上一个命令占着、`--restart` 也救不回来"的应急出口。
- `lssh --wait SECONDS`：会话忙时排队等待。默认改成**立刻返回并说明在跑什么**，
  不再无限期挂住客户端（旧实现能挂到 24h）。
- `--list` 显示 `running:` / `远端仍在跑:` 两条新信息，超时时还能看到远端现场路径。
- 退出码语义对齐 `timeout(1)`：`0` 成功、`124` 客户端放弃等待（**不代表命令失败**）、
  `130` 中断、其余为远端 rc。
- `Result.status`：`completed|failed|running|timeout|disconnected|busy|cancelled|unknown`。

### Fixed

- **同机串行的病根**：旧实现把"等命令结束"整段丢给外层 shell 一次阻塞到 timeout，
  共享的 SSH 通道被单条命令独占。现在改成 **0.2s 一片的切片轮询**，每片结束就放开
  通道，别的槽位才能插进来。
- **`interrupt` 会误伤别的命令**：现在带 `req_id` 精确到那一条所在的 window，
  只对那个 window 发 Ctrl-C；裸 `\x03` 不再打进通道（会打到别人的前台任务上）。
- **新 window 用干净 shell 起**（`bash --noprofile --norc`，没有 bash 就 `sh`）：
  用户 rc 里的 ble.sh 之类行编辑器会把 `send-keys` 吞进自己的编辑缓冲，Enter 变成
  "插入换行"，命令永不执行、屏幕上看还一切正常 —— 这正是"莫名其妙就超时"的来源。
  老会话里遇到这种 window 会自动重建一次。
- **`--kill` 之前经常没真生效**：它 `send` 完 `kill-session` 立刻就把 ssh 进程杀了，
  命令常常还没被执行。现在会等远端回执确认再关通道。
- 空闲回收不再看单一全局锁：只要还有槽位在跑命令就不关通道（旧逻辑在超时收尾后
  可能把远端仍在跑的长任务掐断）。
- **超时不再是"失败"的同义词**。命令没等到结束标记时，先到远端查现场（rc 文件、
  进程是否还持有输出文件、已产出的输出），据此给出 `completed` / `running` /
  `disconnected` / `unknown`，而不是一律报"超时"。反馈里那 4 块 NVMe 的 `mkfs`
  就是"明明成功了却报超时"，调用方一旦据此重跑会二次格式化。
- **超时默认不再发 Ctrl-C**（`--interrupt-on-timeout` 显式开启）。对 `mkfs`/`dd`/
  `rsync` 误打断比超时本身更危险。
- **超时也回传已落盘的输出**，并在被截断时标注 `<<<lssh: 输出在超时点被截断>>>`。
  根因是收尾前 report 行就把 `.out`/`.sh` 删了。
- **通道断开会被立刻发现**。旧实现里 pty EOF 只是让读线程静默退出，命令要空等到
  timeout 才报错（`Connection idle too long` 那种断连会白等 600s）。
- **写入脚本按字节数校验**，并按需重写。`base64 -d > file` 是"先建文件再写内容"，
  只判"文件存在"会导致脚本半截被执行（现场表现：命令没跑、莫名超时）。
- **缓冲区裁剪不再让 in-flight 命令错位**：改用绝对偏移并保护未完成的区间，
  旧的 `buf = buf[-keep//2:]` 会让解析永远失败。

### Changed

- 远端 tmux pane 在重连时先做健康检查并回收（`--restart` 现在真的能救回被占用的
  pane；旧实现只换 SSH 通道，pane 里的进程照旧吞掉新命令）。
- 收尾余量从 `timeout + 30s` 收紧到 `timeout + 3s`。

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
