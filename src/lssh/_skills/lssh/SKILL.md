---
name: lssh
description: Run commands over long-lived SSH sessions on gateways that block the exec channel (ssh host 'cmd' rejected, scp failing, or connections throttled). Use when the user mentions lssh, a bastion/jump host that refuses exec, or a machine that must keep cwd/env and tolerate repeated commands. Covers long jobs, timeouts and concurrency.
license: MIT
compatibility: Requires the `lssh` command (Python 3.9+) and an OpenSSH client locally; the remote side only needs a POSIX shell and, optionally, tmux.
metadata:
  homepage: "https://github.com/colorful-lollipop/lssh"
---

# lssh

`lssh <host> 'cmd'` 相当于可用的 `ssh <host> 'cmd'`：一条长连接 + 远端 tmux，
命令跑完才返回。同一台机器默认 **3 个并发槽位**（`--slots` 可调）：一条长任务挂住时，
另一条命令照样秒回；不同机器并行。

本机没有 `lssh` 时先装（装完技能会落到各 Agent 的技能目录）：

```bash
uv tool install git+https://github.com/colorful-lollipop/lssh && lssh skills install --yes
```

## 什么时候用

- ✅ 目标拒绝 exec 通道（`ssh host 'cmd'` 返回 `VSCode Remote SSH is not enabled` 或直接断）
- ✅ 网关对连接频率敏感（循环 ssh 会被限流封禁）
- ✅ 需要跨命令保持 cwd / 环境变量
- ❌ 普通机器：直接 `ssh host 'cmd'` 就行
- ❌ 需要 scp、端口转发、全屏 TTY（vim/top）、实时流式输出

## 四条规则

1. **选项写在 host 前面**：`lssh --timeout 30 a5-53 'cmd'`
2. **同一台机器合并成一次调用**，不要循环：`lssh h 'a; b; c'`
   （要看住一个长任务、同时查记录，直接开第二个 `lssh` 即可，不必塞进一条命令）
3. **长任务放后台**，用轮询看结果，别让 lssh 干等
4. **超时 ≠ 没执行**：客户端放弃等待不代表命令失败。看返回的 `status`
   （`completed` / `failed` / `running` / `disconnected`），`running` 时**不要重跑**，
   否则 `mkfs`/`dd` 这类命令会被跑第二遍
5. **不要** `--stop`（影响所有 agent）、跑交互式程序、读密码

## 速查

```bash
lssh <host> 'cmd'                # 执行，多条用 ; 或换行
lssh --json <host> 'cmd'         # {ok, status, rc, stdout, stderr, note, remote}
lssh --timeout 30 <host> 'cmd'   # 超时秒数，默认 120（超时≠失败，见下）
lssh --list                      # 会话状态 + 远端还在跑什么
lssh --wait 60 <host> 'cmd'      # 槽位都忙时最多排队 60s（默认立刻报忙）
lssh --slot 1 <host> 'cmd'       # 钉在 1 号槽位（默认自动挑空闲的）
lssh --list                      # 每行一个槽位：w0/w1/w2 在跑什么
lssh --restart <host>            # 重连（远端状态保留）
lssh --kill <host>               # 强制回收**整台机器**的远端会话（所有槽位一起清）
lssh --interrupt-on-timeout      # 超时给远端发 Ctrl-C（默认不发，避免打断 mkfs/dd）
lssh <host>                      # 进入逐行 REPL
```

退出码：`0` 成功、`124` 客户端放弃等待（**不代表命令失败**）、`130` 中断、
其余为远端命令的 rc。

## 长任务

```bash
lssh a5-53 'cd /opt && nohup make -j16 > /tmp/b.log 2>&1 & echo $!'
lssh a5-53 'tail -5 /tmp/b.log; pgrep -f "make -j16" || echo DONE'
```

超时了先看 `status`：`running` = 远端还在跑（已产出的输出照样带回）。这时**不用**等它：
长任务只占住一个槽位，另一个 `lssh` 调用照常能查日志（真机实测查询 ~0.2–0.5s 返回）。
确实要打断再 `lssh --kill <host>`（注意它会连坐这台机器的其它槽位）。

## 常见错误

| 提示 | 处理 |
|---|---|
| `status=running` / 退出码 124 | 客户端放弃等待，**命令可能还在跑**：别重跑，`--list` 看或 `--kill` 回收 |
| `status=disconnected` | 通道断了，远端状态未知：同样别重跑，重连后按 `remote` 里的 `.out/.rc` 核对 |
| `status=completed` + note「不要重跑」 | 客户端超时但命令其实跑完了，直接用返回的输出 |
| `输出校验失败` | 重跑一次；仍失败 `lssh --restart <host>` |
| `连接失败: 远端 shell 未就绪` | 等 2–3 分钟，别连续重试（限流） |
| `会话忙（3 个槽位都没空 …）` | 三个槽位都在跑；用 `--wait 60` 排队，`lssh --list` 看是谁在跑 |
| `槽位 wN 忙（上一条超时后仍在远端跑）` | 那个 window 里还有超时未结束的命令：等它跑完、`--wait` 换个槽位，或 `--kill` |

主机清单、并发实测、反模式和完整排障见 `references/details.md`；
完整参数见 `references/commands.md`（安装时由 CLI 生成）或 `lssh -h`。
