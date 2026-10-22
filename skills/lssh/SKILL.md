---
name: lssh
description: 在 exec 通道被禁或容易限流的 SSH 机器上执行命令（如 a5-* 集群）。长连接复用、批量命令、状态保持、长任务与超时处理。
---

# lssh

`lssh <host> 'cmd'` 相当于可用的 `ssh <host> 'cmd'`：一条长连接 + 远端 tmux，
命令跑完才返回，同一台机器串行、不同机器并行。

## 什么时候用

- ✅ 目标拒绝 exec 通道（`ssh host 'cmd'` 返回 `VSCode Remote SSH is not enabled` 或直接断）
- ✅ 网关对连接频率敏感（循环 ssh 会被限流封禁）
- ✅ 需要跨命令保持 cwd / 环境变量
- ❌ 普通机器：直接 `ssh host 'cmd'` 就行
- ❌ 需要 scp、端口转发、全屏 TTY（vim/top）、实时流式输出

## 四条规则

1. **选项写在 host 前面**：`lssh --timeout 30 a5-53 'cmd'`
2. **同一台机器合并成一次调用**，不要循环：`lssh h 'a; b; c'`
3. **长任务放后台**，用轮询看结果，别让 lssh 干等
4. **不要** `--stop`（影响所有 agent）、命令里 `exit`、跑交互式程序、读密码

## 速查

```bash
lssh <host> 'cmd'                # 执行，多条用 ; 或换行
lssh --json <host> 'cmd'         # {ok, rc, stdout, stderr, duration, error}
lssh --timeout 30 <host> 'cmd'   # 超时秒数，默认 120
lssh --list                      # 会话状态（ready / dirty / BUSY）
lssh --restart <host>            # 重连（远端状态保留）
lssh <host>                      # 进入逐行 REPL
```

## 长任务

```bash
lssh a5-53 'cd /opt && nohup make -j16 > /tmp/b.log 2>&1 & echo $!'
lssh a5-53 'tail -5 /tmp/b.log; pgrep -f "make -j16" || echo DONE'
```

## 三种常见错误

| 提示 | 处理 |
|---|---|
| `命令超时` | 改后台任务；连续两次超时会自动重连 |
| `输出校验失败` | 重跑一次；仍失败 `lssh --restart <host>` |
| `连接失败: 远端 shell 未就绪` | 等 2–3 分钟，别连续重试（限流） |

主机清单、并发实测、反模式和完整排障见 `references/details.md`。
