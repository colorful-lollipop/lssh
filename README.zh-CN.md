# lssh

[![CI](https://github.com/colorful-lollipop/lssh/actions/workflows/ci.yml/badge.svg)](https://github.com/colorful-lollipop/lssh/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![Agent Skills](https://img.shields.io/badge/Agent%20Skills-ready-A46BFF)](docs/install.md)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

[English](README.md) | **简体中文**

**给「拒绝 exec 通道」的机器用的长连接 SSH 执行器。**

有些 SSH 网关（云堡垒机、跳板机、厂商的"运维通道"）只实现 SSH 的 *shell* 通道：
exec 请求（`ssh host 'cmd'`）直接被拒，`scp`/`rsync` 因为要走 subsystem 通道也一并失效。
这类网关往往还对连接频率敏感，脚本里循环 `ssh host cmd` 很容易被限流甚至封禁。

`lssh` 用**一条长连接**换掉"每条命令一次登录"：

- 本地常驻守护进程为每台机器维持一条 `ssh -tt` 会话；
- 会话内部挂一个 **detached tmux**（默认名 `lssh`），断线重连后 cwd / 环境变量 /
  后台任务都还在；
- 命令以 base64 分块写进远端临时文件再执行，**不怕目标机的行编辑器**
  （ble.sh、zsh 语法高亮之类）拆坏多行输入，也不会有提示符混进 stdout；
- 每条命令带随机标记，精确切出 stdout / stderr / 退出码，并在返回前做长度 + md5 校验。

对上层来说，它就是一个"能用的 `ssh host cmd`"：

```console
$ lssh a5-53 'npu-smi info -l | head -5'
$ lssh web-1 'cd /opt/app && git log -1 --oneline'
```

## 一句话安装

把这句话发给你的 Agent —— 它会装好命令、把技能教给自己，然后向你汇报：

```text
从 https://github.com/colorful-lollipop/lssh 安装 lssh（运行其中的 `scripts/install.sh`），
然后运行 `lssh skills install --yes`，再把 `lssh skills status` 的结果给我看。
```

在终端里执行 [`scripts/install.sh`](scripts/install.sh) 效果相同。

## 工作原理

```
 lssh (CLI)                 lsshd (daemon)                   远端
   │  JSON over Unix socket    │                              │
   ├──────────────────────────►│  ssh -tt（一条长连接）        │
   │                           ├─────────────────────────────►│ 登录 shell
   │                           │                              └─ tmux "lssh"
   │                           │                                    └─ 你的命令
   │◄──────────────────────────┤  stdout / stderr / rc         │
```

单条命令在远端的形态：

```sh
# 1. 写脚本（base64 分块，免疫行编辑器）
printf %s '<chunk>' >> /tmp/.lssh-<tok>.b64
base64 -d /tmp/.lssh-<tok>.b64 > /tmp/.lssh-<tok>.sh

# 2. 在 tmux pane 里执行，输出落到文件
tmux send-keys -t lssh -l '. /tmp/.lssh-<tok>.sh'
tmux send-keys -t lssh Enter
#   脚本内容：{ cmd; } > out 2> err; echo $? > rc; : > done

# 3. 结束后打标记、cat 文件（带长度 + md5 校验）
```

输出不经过 tmux 的屏幕渲染，所以换行、空行、类二进制内容都能原样保留。

## 安装

```bash
curl -fsSL https://raw.githubusercontent.com/colorful-lollipop/lssh/main/scripts/install.sh | sh
```

脚本用 `uv`（没有就用 `pipx`，两者都没有则先装 `uv`）安装命令，然后执行
`lssh skills install --yes`。设置 `LSSH_NO_SKILLS=1` 可跳过技能安装。

<details>
<summary>手动 / 从源码安装</summary>

```bash
uv tool install git+https://github.com/colorful-lollipop/lssh   # 或：pipx install ...
```

```bash
git clone https://github.com/colorful-lollipop/lssh && cd lssh
python3 -m venv .venv && .venv/bin/pip install -e .
ln -sf "$PWD/.venv/bin/lssh" ~/.local/bin/lssh
```

</details>

要求：本机有 OpenSSH 客户端 + Python ≥ 3.9。远端只需要 POSIX shell 和 `base64`
（缺失时退回 `openssl base64 -d`），需要状态保持时再加 `tmux`——全是系统自带的，
**目标机上不安装任何东西**。

## 用法

```bash
lssh <host> 'cmd'           # 执行；多条命令用 ; 或换行分隔
lssh <host>                 # 逐行 REPL，送进同一个远端 shell
lssh --list                 # 会话状态：state / idle / uptime / 每个槽位在跑什么
lssh --restart <host>       # 断开重连（远端 tmux 会话保留）
lssh --json <host> 'cmd'    # 原始 JSON 结果
lssh -h                     # 行为说明与长任务写法
```

开关必须写在 host 之前（和 `ssh` 一样）：

| 开关 | 说明 |
|---|---|
| `--timeout N` | 单条命令超时秒数，默认 120 |
| `--no-tmux` | 不启用远端 tmux（断线后状态丢失） |
| `--tmux NAME` | 远端 tmux 会话名，默认 `lssh` |
| `--ssh-arg ARG` | 透传给 `ssh` 的额外参数，可重复 |
| `--idle N` | 空闲多少秒关闭通道，默认 1800 |
| `--slots N` | 每台机器最大并发槽位（远端 tmux window 数），默认 3；`1` = 退回串行 |
| `--slot N` | 把这条命令钉在指定槽位（默认自动挑空闲的） |
| `--local SHELL` | 调试用：把"远端"换成本地 shell |

退出码与远端命令一致（0–255）；连接、超时、校验失败返回 1，并在 stderr 说明原因。

## 凭证

lssh 不保存、不读取、不打印密码。网关强制密码认证时，走 OpenSSH 的 askpass 钩子，
帮手程序 `lssh-askpass` 由本包提供，只读一个文件：

`~/.config/lssh/secrets` —— 每行 `<host-pattern> <password>`，权限 `0600`，支持 `*` 通配：

```bash
lssh --init                                     # 生成 config.json 与 secrets（0600）
printf '10.0.0.9 %s\n' "$PW" >> ~/.config/lssh/secrets
```

优先用密钥。`secrets` 可以是软链，交给密钥管理器生成即可。
详见 [docs/configuration.md](docs/configuration.md)。

## Agent skill

wheel 里自带一份 Agent Skill，一条命令装到本机所有 Agent 能找到的位置：

```bash
lssh skills install          # 预览：装进哪些 Agent、写到哪
lssh skills install --yes    # 写入
lssh skills status           # 已装 / 过期 / 未装
lssh skills show             # 打印 SKILL.md（也可手工复制）
```

规范副本写到 `~/.agents/skills/lssh`——Codex、Cursor、Gemini CLI、GitHub Copilot、
Windsurf、OpenCode 等读这个跨 Agent 标准目录。只读自己目录的 Agent（Claude Code、
OpenClaw、Qwen Code、Cline、Kiro、Goose、DeepSeek Harness…）在该目录已存在时各拿一份，
**不会为用户没装的 Agent 创建目录**。

每个写入的目录都带 `.lssh-install.json` 标记，升级和 `lssh skills uninstall --yes`
只碰这些目录。`--project` 装进当前仓库的 `./.agents` 与 `./.claude`，`--agent NAME`
只处理一个 Agent。技能还附带由当前 CLI 生成的 `references/commands.md`，不会和
`lssh -h` 脱节。

## 已知限制

- 远端 shell 需要 POSIX `sh` 语义。
- 命令跑在交互式 shell 里，因此：
  - stdin 不转发（要喂数据用 `echo ... | cmd` 或写文件）；
  - 需要 TTY 的程序（`top`、`vim`）不适合；
  - 不要调用 `exit`，那会结束会话（守护进程会自动重连）。
- 输出在**命令结束后**才返回（原子、可校验），没有流式；长任务请放后台轮询日志。
- 超时会先发 `Ctrl-C` 并等 3 秒收尾，收不回来就重建会话。能否真正打断取决于远端
  pty 是否有控制终端（真实 SSH 场景通常可以）。
- 同一台机器最多 3 个并发槽位（`--slots` 可调）：一条长任务挂住时，另一条命令
  仍能秒回。命令会被投到空闲槽位；**顺序命令粘在同一槽位**，所以 `cd`/`export`
  照旧保持，而**并发执行的两条命令 cwd/环境各自独立**（建议用绝对路径）。
  三个槽位都占满时才会报"会话忙"（`--wait 30` 排队）。
- `--list` 中 `state=dirty` 表示上次执行异常，下一条命令会自动重连。

## 开发

```bash
make install   # venv + 可编辑安装（含 dev 依赖）
make test      # 全量用例，无需网络
make lint      # ruff check + format check
```

`LocalTransport` 让整套协议在没有 ssh、没有堡垒机的情况下可测：

```python
from lssh.session import Session
from lssh.transports import LocalTransport

s = Session(host="local", transport=LocalTransport(), tmux=None)
print(s.run("cd /tmp && pwd").stdout)
```

## 项目文档

- [CHANGELOG.md](CHANGELOG.md) — 版本变更
- [CONTRIBUTING.md](CONTRIBUTING.md) — 设计约束与开发方式
- [SECURITY.md](SECURITY.md) — 威胁模型与漏洞上报
- [docs/configuration.md](docs/configuration.md) — 配置目录、按 host 设置、凭证来源
- [docs/install.md](docs/install.md) — 给 AI Agent 的安装步骤

## License

MIT
