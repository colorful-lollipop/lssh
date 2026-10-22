# lssh

[![CI](https://github.com/colorful-lollipop/lssh/actions/workflows/ci.yml/badge.svg)](https://github.com/colorful-lollipop/lssh/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**Long-lived SSH sessions for machines that refuse the exec channel.**

有些 SSH 网关（云堡垒机、跳板机、厂商的"运维通道"）只实现 SSH 的 *shell* 通道，
收到 exec 请求（`ssh host 'cmd'`）会直接断开；`scp`/`rsync` 走 subsystem 通道，同样失效。
这类网关往往还对连接频率敏感，脚本里循环 `ssh host cmd` 很容易被限流甚至封禁。

`lssh` 用**一条长连接**换掉"每条命令一次登录"：

- 本地常驻守护进程为每台机器维持一条 `ssh -tt` 会话；
- 会话内部再挂一个远端 **tmux**（默认 `lssh`），断线重连后 cwd / 环境变量 / 后台任务都还在；
- 命令以 base64 分块写入远端临时文件后 `eval`，因此**不怕目标机的行编辑器**（ble.sh、
  zsh 语法高亮之类）重绘多行输入，也不会把提示符混进 stdout；
- 每条命令带随机标记，精确切出 `stdout` / `stderr` / 退出码。

对上层来说，它就是一个"能用的 `ssh host cmd`"：

```console
$ lssh a5-53 'npu-smi info -l | head -5'
$ lssh web-1 'cd /opt/app && git log -1 --oneline'
```

## 工作原理

```
 lssh (CLI)                 lsshd (daemon)                  远端
   │  JSON over Unix socket    │                               │
   ├──────────────────────────►│  ssh -tt (一条长连接)          │
   │                           ├──────────────────────────────►│ bash
   │                           │                               └─ tmux a5d
   │                           │                                     └─ 你的命令
   │◄──────────────────────────┤  stdout / stderr / rc          │
```

一条命令在远端的执行形状：

```sh
__lssh_err=$(mktemp ...)
printf '<token>S\n'
{ <你的命令> } 2>"$__lssh_err"
__lssh_rc=$?
printf '\n<token>R:%s\n' "$__lssh_rc"
cat "$__lssh_err"; rm -f "$__lssh_err"
printf '\n<token>E\n'
```

## 安装

```bash
git clone <repo> && cd lssh
python3 -m venv .venv
.venv/bin/pip install -e .
ln -sf "$PWD/.venv/bin/lssh" ~/.local/bin/lssh
```

要求：本机有 OpenSSH 客户端；Python ≥ 3.9。远端需要有 `tmux`（没有也能用，只是失去
跨连接的状态保持）和 `base64`（缺了会退回 `openssl base64 -d`）。

## 用法

```bash
lssh <host> '命令'          # 执行；多条命令用 ; 或换行分隔
lssh <host>                 # 伪交互：每行一条命令，送进同一个远端 shell
lssh --list                 # 各机器会话状态（state / 空闲时长 / 是否忙）
lssh --restart <host>       # 断开重连（远端 tmux 会话保留，cwd 还在）
lssh --stop                 # 关闭守护进程（所有通道）
lssh --json <host> '命令'   # 输出原始 JSON
```

常用开关（须写在 host 之前，和 `ssh` 一样）：

| 开关 | 说明 |
|------|------|
| `--timeout N` | 单条命令超时秒数，默认 120 |
| `--no-tmux` | 不启用远端 tmux 持久化 |
| `--tmux NAME` | 远端 tmux 会话名，默认 `lssh` |
| `--askpass PATH` | 指定 `SSH_ASKPASS` 脚本（见下） |
| `--ssh-arg ARG` | 透传给 `ssh` 的额外参数，可重复 |
| `--idle N` | 空闲多少秒关闭通道，默认 1800 |
| `--local SHELL` | 调试用：把"远端"换成本地 shell |

退出码与远端命令一致（0–255）；连接/超时类错误返回 1 并打印原因。

## 密码认证（只在必要时）

`lssh` 自己**不保存、不读取、不打印任何密码**。如果目标只能用密码登录，提供一个
`SSH_ASKPASS` 脚本即可（默认会探测 `~/.ssh/lssh-askpass.sh`）：

```bash
#!/bin/sh
# ~/.ssh/lssh-askpass.sh
# 例如：密码写在 ~/.ssh/config 的注释行里，按提示中的主机名匹配
prompt="$1"
host=$(printf '%s' "$prompt" | sed -n "s/.*@\([^ ']*\).*/\1/p")
awk -v h="$host" '/^# lssh-pw / { if ($3 == h || $3 == "*") { print $4; exit } }' "$HOME/.ssh/config"
```

```sshconfig
# ~/.ssh/config
# lssh-pw 10.0.0.9 s3cret   ← 由上面的脚本读取；这个文件本来就应该 600
Host bastion-a
    HostName 10.0.0.9
    User deploy
```

优先用密钥；密码方案只在网关强制时使用，并且务必把脚本权限设为 600。

## Agent skill

仓库自带一份给 agent 用的 skill（`skills/lssh/`），安装方式二选一：

```bash
# A. 目录软链（推荐，仓库更新即生效）
ln -s "$PWD/skills/lssh" ~/.config/opencode/skills/lssh

# B. 复制
cp -r skills/lssh ~/.config/opencode/skills/
```

或者让 opencode 直接扫描仓库：

```jsonc
{ "skills": ["~/code/lssh/skills"] }
```

skill 只是薄薄一层索引（何时用、三条规则、常见错误），细节在
`skills/lssh/references/details.md`，命令语义在 `lssh -h` 里。

## 已知限制

- **远端 shell 必须是 POSIX sh 语义**（远端 `tmux` 里的默认 shell 即可）。
- 命令走的是**交互式 shell**，因此：
  - stdin 不会被转发（要喂数据请自己 `echo ... | cmd` 或写文件）；
  - 需要 TTY 的程序（`top`、`vim`）不适合；
  - 命令里不要调用 `exit`，那会结束会话（守护进程会自动重连）。
- 超时会先发 `Ctrl-C` 并留 3 秒收尾；收不回来则重建会话。是否真能打断取决于远端
  pty 是否有控制终端（真实 SSH 场景通常可以）。
- 守护进程按 host 串行执行；不同 host 之间并行。多人/多 agent 共用同一台机器时，
  命令会自动排队而不是互相踩。
- `--list` 里的 `state` 为 `dirty` 表示上次执行异常、下一条命令会自动重连。

## 开发

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/ruff check .
```

`LocalTransport` 让整套协议可以在没有网络、没有堡垒机的情况下测试：

```python
from lssh.session import Session
from lssh.transports import LocalTransport

s = Session(host="local", transport=LocalTransport(), tmux=None)
print(s.run("cd /tmp && pwd").stdout)
```

## 项目文档

- [CHANGELOG.md](CHANGELOG.md) — 版本变更
- [CONTRIBUTING.md](CONTRIBUTING.md) — 开发约定与测试方式
- [SECURITY.md](SECURITY.md) — 威胁模型与漏洞上报
- [docs/bastion-loss-report.md](docs/bastion-loss-report.md) — 某类网关的丢块实测记录（含复核结论）

## License

MIT
