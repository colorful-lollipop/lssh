# lssh 源码阅读 wiki

> 目标：读完之后，你能在脑子里跑起一条命令从 CLI 到远端 shell 再回来的完整链路，
> 能独立定位 bug、加一个选项、改一处协议，而不需要重新通读全部源码。
>
> 文档基于 `lssh 0.4.0`（commit `1a0ed00`）逐行核对生成。源码约 2400 行，测试 48 项
> （47 pass + 1 skip）。
>
> ⚠️ **0.5 起"同机串行"已经变成"同机 3 个槽位并发"**：`_cmd_lock` 换成 `_io_lock`
> （通道原子交互）+ 每槽位一把锁，远端 tmux 里每个槽位一个 window（`w0/w1/w2`），
> 轮询改成 0.2s 切片。本文档第 1、3、4、6 章凡是讲"串行/bash 一把锁"的地方按此修正
> 阅读；`tests/test_concurrency.py` 是这部分的最新"规格说明书"。

---

## 0. 怎么用这份文档

### 0.1 三条阅读路线

**A. 30 分钟速览（只想搞懂它凭什么能用）**

```
第 1 章 → 第 3 章（运行模型）→ 第 6 章（一次命令的字节旅程）→ 收工
```

**B. 半天通读（要能改代码）**

```
第 1 → 2 → 3 章（建立心智模型，别跳）
第 4 章按 4.1→4.7 顺序精读（顺序是精心排的，见 4.0）
第 5 章（七个"为什么"，把设计意图钉死）
第 7 章（测试即规范）
第 8 章（动手实验，必须真的跑）
```

**C. 带着具体任务来（按需查）**

```
第 10 章「改动 cheatsheet」直接查表 → 跳到对应小节精读 → 回第 7 章找对应测试
```

### 0.2 阅读前的自我检查

读之前请确认你能回答这三题（答不上就先去第 3 章）：

1. `lssh host 'cmd'` 和 `ssh host 'cmd'` 分别走 SSH 协议的哪个 channel？
2. 为什么把命令的 stdout 写进文件再 `cat`，而不是直接读 tmux 屏幕？
3. 为什么每台机器只能同时跑一条命令？

### 0.3 术语约定

本文用 `文件:行号` 标注位置，例如 `session.py:256` 指
`src/lssh/session.py` 第 256 行（`run()` 的定义处）。

---

## 1. 这个项目解决什么问题

`README.md:12-27` 已经说得很清楚，但**读懂它才能读懂代码里一半的丑陋之处**，所以复述一遍：

有些 SSH 网关（云堡垒机、跳板机、厂商的"运维通道"）**只实现了 SSH 的 shell channel，
没有实现 exec channel**。后果：

| 你想做的事 | 底层 channel | 结果 |
|---|---|---|
| `ssh host 'cmd'` | exec | 被拒绝（有时候报的是 `VSCode Remote SSH is not enabled` 之类莫名其妙的错） |
| `scp` / `rsync` | subsystem | 失败 |
| 端口转发 | direct-tcpip | 失败 |
| `ssh host`（交互登录） | shell | **能用** |

而这类网关往往还有**登录频率限制**——脚本里循环 `ssh host cmd`，几十次之后直接被限流封禁。

于是核心矛盾变成：**只能登录，且不能频繁登录**。

`lssh` 的答案：**一次登录，永不断开**。

```
一次 ssh -tt 登录  ──►  远端挂一个 detached tmux  ──►  后续所有命令都塞进这个 tmux
                          （cwd / env / 后台任务都活着）
```

这个设计推导出了后面所有的工程细节，先记住这个因果链：

```
只有一条连接 ──► 通道必须分时复用（0.5 起：3 个槽位 + 0.2s 切片轮询）
             ──► 必须自己做超时和中断
             ──► 输出和命令混在同一个字节流里 ──► 必须自己做分帧（marker 协议）
             ──► 远端是个交互式 shell，有行编辑器 ──► 命令必须 base64 分块写入
             ──► 交互式 shell 有回显和提示符 ──► 必须关回显、清 PS1、且用整行锚定 marker
```

### 1.1 能力边界（先认清，避免读代码时困惑）

`README.md:168-182` 列了限制。其中三条是**架构级**的，不是 bug：

- **不流式**：命令跑完才返回（因为要算 md5 校验）。长期任务请 `nohup` + 轮询日志。
- **不转发 stdin**：`echo x | lssh h 'cat'` 不会工作。要喂数据就写远端文件。
- ~~同机串行~~：0.5 起改为**同机 3 槽位并发**（`--slots N`）；每条命令独占一个 window，
  通道按 0.2s 一片分时复用；槽位占满才报忙。异机仍然并行。

---

## 2. 全局地图

### 2.1 目录结构

```
lssh/
├── src/lssh/                   # 全部源码，约 1700 行
│   ├── __init__.py             # 15 行，只有 __version__（从包元数据取，源码树兜底 0.0.0）
│   ├── __main__.py             # 4 行，python -m lssh 的入口
│   ├── paths.py                # 45 行，XDG 路径计算（叶子模块，零依赖）
│   ├── protocol.py             # 152 行，远端脚本构造 + 输出解析（叶子模块，零依赖）
│   ├── transports.py           # 112 行，怎么拿到一个 pty（叶子模块）
│   ├── askpass.py              # 73 行，lssh-askpass 可执行入口（叶子模块）
│   ├── config.py               # 97 行，config.json 解析 + --init
│   ├── session.py              # 409 行 ★★★ 核心：一条长连接会话
│   ├── daemon.py               # 208 行 ★★ 守护进程 + JSON-RPC
│   ├── cli.py                  # 334 行 ★★ 命令行入口 / 守护进程拉起 / REPL
│   ├── skills.py               # 239 行，Agent Skill 安装器
│   ├── skills_cli.py           # 206 行，lssh skills 子命令
│   └── _skills/lssh/           # 打包进 wheel 的技能文本
│       ├── SKILL.md
│       └── references/details.md
├── tests/                      # 48 项，全部不需要网络
│   ├── test_session_local.py   # 25 项 ★ 最重要，几乎是 session 的行为规范
│   ├── test_skills.py          #  9 项
│   ├── test_config.py          #  8 项
│   └── test_daemon.py          #  6 项
├── docs/{configuration,install}.md
├── scripts/install.sh          # 一行安装脚本
└── Makefile / pyproject.toml / .github/workflows/ci.yml
```

### 2.2 模块依赖图（无环，严格分层）

```
                    ┌─────────────┐
        __main__.py │  → cli      │
                    └─────────────┘
                    ┌─────────────┐        ┌──────────────┐
                    │    cli.py   │───────►│ skills_cli.py│  (main 里按 argv[0] 懒加载)
                    └──────┬──────┘        └──────┬───────┘
             ┌─────────────┼─────────────┐        │
             ▼             ▼             ▼        ▼
      ┌──────────┐  ┌──────────┐  ┌─────────┐ ┌────────┐
      │ daemon.py│  │ config.py│  │paths.py │ │skills  │
      └────┬─────┘  └────┬─────┘  └────┬────┘ └───┬────┘
           ▼             ▼             │          │
      ┌──────────┐  ┌─────────┐        │          └──► paths
      │session.py│  │         │◄───────┴──────────┐
      └────┬─────┘  │         │                   │
     ┌─────┴─────┐   │         │                   │
     ▼           ▼   ▼         ▼                   │
┌──────────┐ ┌──────────────┐ ┌──────────┐  ┌──────────┐
│protocol  │ │ transports   │ │ askpass  │  │(独立)    │
│(零依赖)  │ │              │ │(自算路径)│  │          │
└──────────┘ └──────────────┘ └──────────┘  └──────────┘
```

要点（读代码时会踩的三个点）：

1. **`protocol.py` 零依赖**，纯函数，无 IO、无状态。所以它是全文最好读的文件，也是
   单元测试覆盖率最高的地方。**从它开始读**。
2. **`skills.py` 会懒加载 `cli.py`**：`skills.py:108` 在函数体内 `from .cli import build_parser`，
   为的是从当前 CLI 反向生成命令文档（`references/commands.md`），避免文档和代码漂移。
   用函数内导入是为了躲开 `cli.py → skills_cli.py → skills.py` 的循环。
3. **`askpass.py` 自己算了一遍 secrets 路径**（`askpass.py:22`），没有用
   `paths.secrets_path()`。它必须能被 setuptools 当成独立 console script 启动
   （`pyproject.toml:32`），作者选择了"少一个 import 更安全"。这是**技术债**，见第 11 章。

### 2.3 两个可执行入口

`pyproject.toml:30-32`：

```toml
[project.scripts]
lssh         = "lssh.cli:main"
lssh-askpass = "lssh.askpass:main"
```

`lssh-askpass` 是给 OpenSSH 调的（不是给人用的）。它出现的位置很关键：OpenSSH 在
**没有 tty 而需要密码**时会执行 `$SSH_ASKPASS`，把提示串作为 `argv[1]` 传进去，
读 stdout 当密码。所以 lssh 的密码方案是完全"寄生"在 OpenSSH 机制上的。

---

## 3. 运行模型（最重要的一章，别跳过）

### 3.1 进程拓扑

一次 `lssh a5-53 'npu-smi info'` 会存在这五个东西：

```
 ① CLI 进程 (lssh)
    │  短的，跑完就退；可能顺手拉起 ②
    │
    │  Unix domain socket  $XDG_RUNTIME_DIR/lssh/daemon.sock  (0600)
    │  一行 JSON 请求 / 一行 JSON 响应
    ▼
 ② lsshd 守护进程 (python -m lssh --daemon)   ← 常驻，与终端脱离 (setsid)
    │
    │ 每台 host 一个 Session 对象
    ▼
 ③ Session 持有的 pty master fd  ◄── ④ ssh -tt 子进程（一个 host 一个）
    │
    │                                        │ 加密链路（shell channel）
    ▼                                        ▼
 ⑤ 远端 login shell ──► tmux new-session -d -s lssh ──► pane 里的 shell ──► 你的命令
```

四个反直觉的点：

1. **CLI 不建立 SSH 连接**。它只连本地 socket。所以 CLI 可以随便杀、随便重启，
   远端会话不受影响。这是"一个守护进程服务多个 agent"的基础。
2. **`ssh -tt` 是守护进程的子进程，跑在 pty 上**（`transports.py:42-50`）。
   必须给 pty，因为要骗远端开启交互式 shell 并使用 `send-keys`/`stty` 这些需要 tty 的东西。
3. **远端 tmux 是 `-d`（detached）创建的，永不 attach**（`session.py:47-55`）。
   attach 的话你读到的就是 tmux **渲染后的屏幕**——光标定位 + 重绘 + 历史重放，
   换行会丢、内容会重复。这个坑写在 `session.py:6-8` 和 `protocol.py:3-8` 的文档字符串里。
4. **命令的 stdout/stderr/rc 从不经过终端渲染**，而是落在远端 `/tmp/.lssh-<token>.{out,err,rc}`，
   再由外层 shell `cat` 出来并打印 marker。`protocol.py` 全部职责就是这件事。

### 3.2 为什么需要"外层 shell"和"pane 内 shell"两层

这是全项目最绕的地方，单独讲。

ptmx 那一端的字节流里只有一个 shell（"外层 shell"，也就是 ⑤ 里 tmux **之外**那个）。
但我们希望命令跑在 **tmux pane 里**（这样 cwd/env 能跨重连保留）。所以：

| 角色 | 身份 | 干什么 |
|---|---|---|
| **外层 shell** | 远端 login shell，`stty -echo`、`PS1=''` | 写文件、`tmux send-keys`、轮询 `.done`、`cat` 结果、打印 marker |
| **pane 内 shell** | tmux 里那个 shell | 真正执行你的命令 |

带来的好处（`session.py:278-281` 的注释）：

> 命令在 pane 里跑，外层 shell 空闲，才能用轮询实现精确超时

因为外层 shell 是空闲的，lssh 可以在它上面跑一个 `while [ ! -f done ]; do sleep 0.1; done`
循环——**超时逻辑用 shell 实现**，而不是靠 Python 掐本地时间。这让 Ctrl-C 能精确打在
pane 里正在跑的进程上。

**`--no-tmux` 时这个结构塌缩成一层**：外层 shell 直接 `. script`，阻塞直到脚本结束。
于是没有远端超时循环，只有客户端侧的兜底 deadline（`session.py:289`
`deadline = started + timeout + 30`）。这个差异是 `protocol.py:80`（`build_poll_lines`）
只在 `if self.tmux:` 分支里被调用（`session.py:278-281`）的原因，也是
"`--no-tmux` 丢掉跨重连状态"这个说法的真实含义。

### 3.3 会话状态机

`session.py:35-38`：

```
                 start()
   DOWN ─────────────────────► STARTING ──成功──► READY
     ▲                             │               │
     │                             │失败            │ 超时×2 / 读线程发现连接断 / 写失败
     │                             ▼               ▼
     └──────stop()──────────────  DIRTY ◄──────────┘
                                   │
                                   └─ 下一条命令 run() 发现 state != READY → 自动 start()
```

`DIRTY` 的语义是"**这条连接不能用了，但别急着重连**"。下一句 `run()` 会在
`session.py:258-262` 自动 `start()` 重连。这是刻意的懒重连——网关限流，能少登一次就少一次。

触发 `DIRTY` 的四个地方：

| 位置 | 原因 |
|---|---|
| `session.py:145` | `start()` 抛异常 |
| `session.py:195-197` | 读线程发现 `ssh` 子进程已退出（连接断开） |
| `session.py:285` | 往 fd 写失败 |
| `session.py:310` | 命令整体超时（连 marker 都没等到） |
| `session.py:325-329` | 连续两次 `rc == -1`（远端轮询超时 = 会话僵死） |

注意最后一条的 `_strikes` 计数器（`session.py:324-337`）：**单次超时不判死，连续两次才判死**。
理由是单次超时可能只是命令真的慢，而 tmux/网关偶尔会丢一次 relay——留一次容错。

---

## 4. 逐文件精读

### 4.0 阅读顺序为什么是这样

```
protocol.py  ←  纯函数，无 IO，能独立测试，先建立"分帧"概念
   ↓
transports.py ←  只有 112 行，理解 pty 怎么起
   ↓
session.py   ←  90% 的复杂度在这里，前面的铺垫都为它服务
   ↓
daemon.py    ←  session 的容器 + RPC 路由，很快读完
   ↓
cli.py       ←  参数解析 + 守护进程拉起 + 输出格式
   ↓
paths/config/askpass ←  边缘设施，15 分钟扫过
   ↓
skills*.py   ←  与主链路无关，可以最后读甚至不读
```

---

### 4.1 `protocol.py`（152 行）—— 远端脚本构造与输出解析

**一句话**：定义了一套"在一条共享字节流里可靠地分出 stdout / stderr / 退出码"的协议。

#### 4.1.1 分帧格式

`protocol.py:10-17`：

```
<token>S
... stdout 原始字节 ...
<token>M
... stderr 原始字节 ...
<token>R:0
<token>E
```

外加两条**校验行**（`protocol.py:96-101` 生成，必须排在 `S` 之前）：

```
<token> SZ <stdout字节数> <stdout的md5>
<token> MZ <stderr字节数> <stderr的md5>
```

`<token>` = `"LSSH" + uuid4().hex[:12]`（`protocol.py:41`），16 字符，每条命令一个新的。
用随机 token 而不是固定串，是为了防止**命令自己的输出里出现长得像 marker 的行**。

#### 4.1.2 为什么 marker 必须整行锚定

`protocol.py:111-113`：

```python
def marker(token, suffix):
    return re.compile(r"^" + re.escape(token) + suffix + r"$", re.MULTILINE)
```

`^...$` + `MULTILINE` 是关键。因为**远端 shell 会把你的输入回显出来**，
回显里 `printf 'LSSHabcS\n'` 这一坨是**字面量反斜杠 n**，不是真换行，所以
`^LSSHabcS$` 匹配不上。只有命令**真的执行**、`printf` 真的输出换行，才会匹配。

这个技巧在 `_probe()` 里被单独复用了一次（`session.py:229-252`），用来判断"远端 shell
到底执行了我的命令没有"——见 4.3.4。

`test_session_local.py:73-77` 就是这条的回归测试：

```python
def test_parse_rejects_echo():
    """回显里的标记是字面量 \n，不能算数。"""
    echoed = "printf 'T3S\\n'; cat /tmp/x\n"
    assert parse(echoed, token) is None
```

#### 4.1.3 五个构造函数（都是给"外层 shell"用的命令片段）

| 函数 | 行 | 生成的远端命令 | 用途 |
|---|---|---|---|
| `build_script(token, cmd)` | 58 | 被 `{}` 包住、重定向到 out/err、写 rc、touch done | **真正执行的脚本** |
| `build_write_lines(token, script)` | 65 | `: >b64` + 若干 `printf %s '<900字节>' >>b64` + base64 解码 | **把脚本送过去** |
| `build_poll_lines(token, timeout)` | 80 | `while [ ! -f done ] && [ $i -lt N ]; do sleep 0.1; done` | **等结束 + 超时** |
| `build_report_lines(token)` | 91 | 打印 SZ/MZ → `printf 'TS'` → `cat out` → `printf 'TM'` → `cat err` → `printf 'R:%s'` → `printf 'TE'` → `rm` | **回报 + 清理** |
| `new_token()` / `paths(token)` | 41 / 46 | — | 命名 |

`build_script` 的产物（`protocol.py:58-62`）：

```sh
{
你的命令
} >/tmp/.lssh-LSSHxxxx.out 2>/tmp/.lssh-LSSHxxxx.err
echo $? >/tmp/.lssh-LSSHxxxx.rc
: >/tmp/.lssh-LSSHxxxx.done
```

三个细节：

- `{ ... }` 而不是 `( ... )`：分组**不开 subshell**，`cd`/`export` 的效果才能留在 pane 里。
  这是 `test_cwd_persists`（`test_session_local.py:106`）能通过的原因。**这是个关键选择**。
- `echo $?` 在分组之后，捕获的是整段的退出码。
- `: > done` 是"结束哨兵"，外层 shell 靠轮询它判断命令结束（不靠屏幕内容）。

`build_write_lines` 为什么要 base64（`protocol.py:65`）：

> 避开行编辑器/引号/多行问题

目标的 shell 可能开了 ble.sh、zsh 语法高亮、fish 自动补全——这些行编辑器会在你**输入**
的过程中重绘、重排、插入控制序列，多行粘贴会被拆坏。base64 之后payload 是纯
`[A-Za-z0-9+/=]`，**没有引号、没有换行、没有特殊字符**，行编辑器再怎么重绘也不会改内容。
900 字节一块是为了不超过终端行缓冲。

`--no-tmux` 时这一层同样需要：即使没有 tmux，外层 shell 也是交互式的。

`build_report_lines` 的两个坑（都在代码注释里）：

1. **校验行必须在 `S` 之前打印**（`protocol.py:95`）。因为 `SZ/MZ` 里含有 `<token> `，
   如果放在 `S..M` 区间内，就会被当成 stdout 内容的一部分，破坏长度/md5 校验。
2. `printf '\n{token}M\n'` 前面那个 `\n`（`protocol.py:103`）是**换行保险**：
   如果 stdout 文件本身不以换行结尾（例如 `printf abc`），`cat` 之后光标还停在内容末尾，
   不加这个 `\n` 的话 `TM` 就接在 `abc` 后面变成 `abcTM`，整行锚定就失效了。
   同理 `printf 'R:%s\n'` 前面也有 `printf '\n'`（`protocol.py:104`）。

   副作用是：当输出**本来**就以换行结尾时，会多出一个空行（实测 `cat out` 出 `hi\n`，
   加分隔符后区间里是 `hi\n\n`）。所以 `strip_separator()`（`protocol.py:137`）的职责是
   **去掉开头一个 `\n`、结尾一个 `\n`，正好还原成文件的原始字节**。

   注意 `parse()`（`protocol.py:146`）在 `strip_separator` 之后**又做了一次 `.strip("\n")`**，
   那是给用户看的；而 `_verify()`（`session.py:359`）用的是**不带额外 strip** 的
   `strip_separator` 结果去算 md5/长度——因为要和远端 `wc -c <out` 的字节数对齐。
   **这个区别很关键，改校验逻辑时别弄混。**

#### 4.1.4 三个解析函数

```python
parse_raw(segment, token)  →  (rc, 原始stdout区, 原始stderr区) | None   # 不裁剪
strip_separator(raw)       →  去掉首尾各一个 \n                        # 还原文件原文
parse(segment, token)      →  (rc, stdout, stderr) | None              # 给调用方用
```

`parse_raw` 用 `marker().search()` 顺序定位 `S / M / R:(-?\d+) / E`，任一缺失返回 `None`
（= 还没收全，继续等）。这就是"分帧"的完成判定。

**为什么 `rc` 要允许负数**：`R:(-?\d+)`。`-1` 是哨兵值，表示 `rc` 文件根本没生成
（见 `protocol.py:105` `|| echo -1`），即**远端轮询超时**——命令没跑完。`session.py:322`
专门判这一支。

#### 4.1.5 `clean()`

`protocol.py:34-38`：去 ANSI（OSC/CSI/字符集/两字节转义，`protocol.py:26-31` 四个分支的正则），
并把 `\r\n` 和孤立的 `\r` 归一成 `\n`。

为什么要去 ANSI：远端 login shell 的启动脚本会吐一堆控制序列（标题栏、颜色、光标形状），
它们会插到 marker 附近，导致 `^...$` 匹配失败。`clean()` 在**读线程里对每个 chunk**
调用（`session.py:214`），所以 `_buf` 里永远是"干净"的文本。

> ⚠️ 副作用（`session.py:365`）：`_verify` 是拿**已经 `clean()` 过的文本重新 encode**
> 去算 md5 的。如果远端输出里有非法 UTF-8 字节，`clean()` 里的 `"replace"`
> 会把它们换成 `\ufffd`，本地 md5 就和远端的不一致 → 报"输出校验失败"。
> 这也是 README 说"二进制不适合"的底层原因。

---

### 4.2 `transports.py`（112 行）—— 怎么拿到一个 pty

**一句话**：把"远端 shell"抽象成 `spawn() -> Spawned(master_fd, proc)`。

```python
@dataclass
class Spawned:  # transports.py:20
    master_fd: int  # 我们读写的这一端
    proc: subprocess.Popen  # 用来判断"连接是不是断了"


class Transport(Protocol):  # transports.py:26  结构化子类型，不用继承
    def spawn(self, *, cols, rows) -> Spawned: ...
    def describe(self) -> str: ...
```

`_open_pty()`（`transports.py:34`）除了 `pty.openpty()` 还要 `TIOCSWINSZ` 设窗口大小——
不设的话默认 80 列，远端 shell 会按 80 列折行，长命令回显会被插进 `\n`，marker 的
整行锚定就会失败。默认 `cols=512`（`session.py:89`）就是为了"尽量不折行"。

`_spawn()`（`transports.py:40`）把子进程的 stdin/stdout/stderr **全部绑到 pty slave**，
然后 `os.close(slave)`——只留 master。这是标准 pty 用法。

#### SSHTransport（`transports.py:55`）

```python
argv = [
    "ssh",
    "-tt",
    "-o",
    f"ConnectTimeout={self.connect_timeout}",
    "-o",
    "ServerAliveInterval=30",
    "-o",
    "ServerAliveCountMax=3",
]
if self.askpass:
    env["SSH_ASKPASS"] = self.askpass
    env["SSH_ASKPASS_REQUIRE"] = "force"
    argv += ["-o", "NumberOfPasswordPrompts=1"]
argv += self.extra_args
argv.append(self.host)
```

四个设计决策（`transports.py:57-61` 的 docstring）：

1. **刻意不复刻 SSH 协议**，直接 exec 系统 `ssh`。理由：
   - 一些网关会 tarpit / 指纹识别非 OpenSSH 客户端（paramiko 之类）；
   - `~/.ssh/config`、`ProxyJump`、`ControlMaster`、host key 策略**本来就该由 OpenSSH 处理**。
2. `-tt`：**强制**分配 pty（两个 t，即使本地没有 tty 也强制）。
3. `env.pop("TMUX")`（`transports.py:71`）：防止本地在 tmux 里运行时，`$TMUX`
   被传到远端导致远端 tmux 判定异常。
4. `SSH_ASKPASS_REQUIRE=force`：OpenSSH 8.4+ 的开关，强制走 askpass 而不是读 `/dev/tty`。
   `NumberOfPasswordPrompts=1`：只问一次，避免反复失败触发网关告警。

`describe()` 只做展示（给 `--list` 用）。

#### LocalTransport（`transports.py:97`）

```python
argv = [self.shell, "--noprofile", "--norc", "-i"]
env["LSSH_NO_TMUX"] = "1"  # 本地模式不要碰用户的 tmux
```

**这个类是整个项目测试策略的支点**：把"远端"换成本机 shell，完整协议（base64 写入、
marker 解析、超时、Ctrl-C、cwd 保留）**全都能在毫秒级跑完，不需要网络、不需要堡垒机**。

`tests/test_session_local.py:15` 就是三行：

```python
def local_session(**kwargs) -> Session:
    return Session(host="local", transport=LocalTransport(), tmux=None, **kwargs)
```

> 📌 **这是你应该偷走的第一条工程经验**：把"不可测试的外部依赖"抽象成一个 30 行的
> Protocol 实现，就能让 90% 的时序逻辑变成单元测试。

---

### 4.3 `session.py`（409 行）—— 核心

**一句话**：维护一条长连接，提供 `run(cmd) -> Result`，并处理超时、中断、重连、校验。

读这个文件的正确姿势：**先看 `run()` 的主干（256-347），再看它调用的四个 helper**。

#### 4.3.1 数据结构

```python
@dataclass
class Result:  # session.py:61
    ok: bool
    rc: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    error: str | None = None  # 只有失败时才有；不是远端的 stderr


@dataclass
class Session:  # session.py:81
    host: str
    transport: Transport
    tmux: str | None = "lssh"
    default_timeout: float = 120.0
    connect_timeout: float = 60.0  # 等远端 shell 就绪（不是 ssh 的 ConnectTimeout！）
    bootstrap_timeout: float = 40.0
    cols: int = 512
    rows: int = 50
    keep: int = 8_000_000  # _buf 上限，8MB
    # ...下面全是 init=False 的运行时状态
```

⚠️ 易混淆：`Session.connect_timeout`（默认 60，`session.py:87`）是 **`_probe()` 的预算**；
`SSHTransport.connect_timeout`（默认 15，`transports.py:66`）才是 **`ssh -o ConnectTimeout`**。
两个同名不同义，第 11 章会提到 `--connect-timeout` 根本没被转发给守护进程的问题。

运行时状态（`session.py:93-106`）：

| 字段 | 作用 |
|---|---|
| `_fd` | pty master fd。`None` = 通道已关 |
| `_proc` | `ssh` 子进程，用来判断连接是否已断 |
| `_buf` | **累积的干净输出**，读线程写、`run()` 读 |
| `_cv` | `threading.Condition`，`_buf` 的生产者/消费者同步 |
| `_io_lock` | **通道原子交互锁**（多行交互整体原子；绝不长持有）。0.4 的 `_cmd_lock` 已被它 + 每槽位锁取代 |
| `slots` | `Slot` 列表：每项 = 一个远端 tmux window（`w0..wN-1`）+ 一把占位锁 |
| `_start_lock` | 防止并发 `start()` |
| `_reader` | 读线程 |
| `_cancel` | `interrupt()` 用来打断等待循环 |
| `_strikes` | 连续超时计数 |

#### 4.3.2 三个 bootstrap 常量（`session.py:41-58`）

```python
PRE_BOOTSTRAP  = "export TERM=xterm-256color; unset PROMPT_COMMAND; "
                 "stty -echo -onlcr 2>/dev/null; PS1=''; PS2=''"
TMUX_BOOTSTRAP = "tmux has-session -t {session} || tmux new-session -d -s {session}; "
                 "tmux set -g status off; ... history-limit 50000; "
                 "tmux set -g default-terminal screen-256color"
PANE_INIT      = "stty -echo; PS1=''; PS2=''; export TERM=xterm-256color"
```

逐个解释**为什么**：

| 动作 | 为什么必须做 |
|---|---|
| `unset PROMPT_COMMAND` | 有的发行版在 `PROMPT_COMMAND` 里塞 `history -a`、窗口标题设置，会往流里写东西 |
| `stty -echo` | **最重要**：不回显输入，`_buf` 里就不会混入"我自己发的命令" |
| `stty -onlcr` | 不加的话 `\n` 会被 tty 翻译成 `\r\n`，输出变阶梯状（CHANGELOG 里记着这个修复） |
| `PS1='' PS2=''` | 提示符会插入 marker 之间，破坏整行锚定 |
| `TERM=xterm-256color` | 有些 rc 文件里 `tput` 依赖 TERM |
| `tmux new-session -d` | `-d` = detached。**绝不 attach**，这是核心约束 |
| `status off` / `visual-activity off` | tmux 状态栏和活动提示会往 pty 写内容 |
| `history-limit 50000` | 默认太小，长时间会话里的输出会被截断（如果误 attach 的话） |
| `PANE_INIT` 通过 `send-keys -l` 送进去 | pane 里的 shell 是独立进程，`stty`/`PS1` 得单独设 |

注意 `PANE_INIT` 的发送方式（`session.py:134-138`）：

```python
f"tmux send-keys -t {self.tmux} -l {_quote(PANE_INIT)}; "
f"tmux send-keys -t {self.tmux} Enter; fi\n"
```

`-l` = literal，不解释 key name。所以 `Enter` 必须**单独一条** `send-keys` 发。这个模式在
`_trigger()`（`session.py:370-376`）里重复出现，是全项目"怎么往 pane 里塞东西"的唯一姿势。

#### 4.3.3 `start()`（`session.py:110-148`）

```
_start_lock
  ├─ 已 READY → 直接返回（幂等）
  ├─ _close_channel()           # 先清理旧通道
  ├─ state = STARTING
  ├─ transport.spawn()          # ★ 这里才真正建立 ssh 连接
  ├─ 起读线程 _read_loop
  ├─ _probe(connect_timeout)    # ① 等 shell 响应（认证 + 限流都在这一步暴露）
  ├─ _send_pre_bootstrap()      # ② 关回显 / 清提示符
  ├─ tmux has-session || new-session -d + PANE_INIT   # ③ 只有 self.tmux 时才做
  ├─ _probe(bootstrap_timeout)  # ④ 等初始化生效
  └─ state = READY, created_at = now
  异常 → state = DIRTY, last_error, 关通道, 重新抛出
```

`_send_pre_bootstrap()` 里有 `time.sleep(0.05)`（`session.py:152`）。这不是优雅的做法，
但没有更好的办法——`stty -echo` 生效需要一点时间，立刻发下一条命令可能被回显出来。

#### 4.3.4 `_probe()`（`session.py:229-252`）—— 最精巧的一个函数

```python
def _probe(self, timeout):
    """反复投喂带 -OK 的标记，直到远端 shell **真的执行**它。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        token = new_token()
        pattern = marker(token, "-OK")
        start = len(self._buf)  # 只匹配新增部分
        self._send(f"printf '%s-OK\\n' {token}\n")
        until = time.time() + 2.0  # 单次 2 秒
        while time.time() < until:
            if pattern.search(self._buf[start:]):  # 整行匹配 = 真的执行了
                return True
            if self.state == DIRTY and self._fd is None:
                return False
            self._cv.wait(0.2)
    return False
```

**为什么不能"发了就算成功"**：`ssh -tt` 在密码提示阶段就已经有 pty 了，你往 fd 里写的
东西会被扔进 tty 缓冲，**看起来"发送成功"，其实远端 shell 根本没在执行**。所以必须
等到一个"只有命令真的执行才能产生"的证据。

而"证据"就是整行匹配的 `-OK` marker：回显里是字面量 `\n`（不换行），执行结果是真换行。
这就是 4.1.2 的技巧在连接层的复用。

**为什么循环重试**：远端 shell 可能还在跑 `.bashrc`（有些 rc 会 `read` 或 `sleep`）。
重试比"多睡一会儿"更快也更稳。每次 `2s` 上限，外层 `timeout` 兜住。

#### 4.3.5 `_read_loop()`（`session.py:181-219`）

```
loop:
  select([fd], timeout=0.5)
  ├─ 超时 → 检查 proc.poll()：ssh 已退出 → state=DIRTY("连接已断开")，notify_all，return
  └─ 可读 → chunk = os.read(fd, 65536)
        ├─ b"" 且 proc 仍活着 → misses++ ；misses<100 就 sleep 0.02 继续
        └─ 有数据 → _buf += clean(chunk)；超过 keep 就砍半；notify_all
```

三个细节：

1. **只用一个线程读 pty**。所有 `run()` 都从 `_buf` 里按 offset 取，靠 `_cv` 通知。
   这避免了多线程抢读同一个 fd。
2. `select` 超时 0.5s 是为了**定期检查子进程死掉**——否则阻塞在 `os.read` 上，
   ssh 崩了也不知道。
3. `_buf` 超过 8MB 就 `_buf = _buf[-keep//2:]`（`session.py:217-218`）。**注意这会破坏 offset 语义**：
   `run()` 在 `session.py:270` 记的 `offset = len(self._buf)`，如果在这期间缓冲区被截断，
   `self._buf[offset:]` 就会读到错位甚至空。这是一个已知的、极少触发的边界问题
   （需要单条命令输出 >8MB 且触发截断），第 11 章列为技术债。

#### 4.3.6 `run()`（`session.py:256-347`）—— 主干，读三遍

```python
def run(self, cmd, timeout=None):
    timeout = float(timeout or self.default_timeout)

    # ① 懒重连
    if self.state != READY:
        try: self.start()
        except Exception as exc: return Result(ok=False, error=f"连接失败: {exc}")

    # ② 串行锁（per-host）
    with self._cmd_lock:
        token  = new_token();  p = paths(token);  script = build_script(token, cmd)
        self._cancel.clear()
        offset = len(self._buf)             # ★ 只解析这之后的新字节
        started = time.time();  self.last_used = started

        # ③ 发送：写脚本 → 清旧文件 → 触发执行 → （有 tmux 时）轮询 → 回报
        try:
            for line in build_write_lines(token, script): self._send(line + "\n")
            self._send(f"rm -f {p['out']} {p['err']} {p['rc']} {p['done']}\n")
            self._trigger(token, timeout)
            if self.tmux:
                for line in build_poll_lines(token, timeout): self._send(line + "\n")
            for line in build_report_lines(token): self._send(line + "\n")
        except (OSError, RuntimeError) as exc:
            self.state = DIRTY
            return Result(ok=False, error=f"写入失败: {exc}")

        # ④ 等待解析（带兜底 deadline）
        parsed = None
        deadline = started + timeout + 30
        while True:
            parsed = parse(self._buf[offset:], token)
            if parsed is not None: break
            if self._cancel.is_set(): break
            left = deadline - time.time()
            if left <= 0: break
            self._cv.wait(min(left, 0.5))

        # ⑤ 四种结局
        if parsed is None and self._cancel.is_set():  → "已中断（Ctrl-C）"
        if parsed is None:                            → _abort + DIRTY + "命令超时"
        rc, stdout, stderr = parsed
        if rc == -1:                                  → _abort + _strikes++（两次判死）+ "命令超时，已发送 Ctrl-C"
        self._strikes = 0
        if not self._verify(...):                     → "输出校验失败"
        return Result(ok=True, rc=rc, stdout=stdout, stderr=stderr, duration=...)
```

必须理解的四个点：

**（a）`offset` 是分帧的另一半。** `_buf` 是**所有命令共享的累积流**。每次 `run()` 记下
当前位置，只解析 `_buf[offset:]`。token 唯一性 + offset 双重保证不会串味。

**（b）`deadline = started + timeout + 30` 里的 `+30`。** 远端轮询预算是 `timeout`，
客户端预算是 `timeout + 30`。**故意让远端先超时**，这样正常情况下走的是
`rc == -1` 分支（有明确的"轮询超时"语义），而不是客户端硬超时（信息更少）。
那 30 秒是留给 `build_report_lines` 的 `cat` 大输出的。

**（c）`rc == -1` vs `parsed is None` 是两种失败**：

| 分支 | 含义 | 远端状态 | 处理 |
|---|---|---|---|
| `parsed is None` | 连 marker 都没收全 | 流断了 / 卡死 | `_abort` + `DIRTY`（重连） |
| `rc == -1` | marker 收全了，但 rc 文件没写出来 | tmux 里命令还在跑 | `_abort`（Ctrl-C）+ `_strikes++` |

**（d）超时分支把 `_buf` 尾部 2000 字符塞进 `stdout`**（`session.py:315`）。这是**调试**
用的——让你看到"卡住的时候屏幕上是什么"，不是命令的输出。看代码时别被误导。

#### 4.3.7 `_verify()`（`session.py:349-368`）

```python
raw = parse_raw(segment, token)  # 拿到未裁剪的原始区间
texts = {"SZ": strip_separator(raw_out), "MZ": strip_separator(raw_err)}
for suffix, text in texts.items():
    found = marker(token, r" " + suffix + r" (\d+) ([0-9a-f]{32})").search(segment)
    if not found:
        return True  # ← 老版本远端没有校验行 → 不阻塞
    size, digest = int(found.group(1)), found.group(2)
    raw = text.encode("utf-8")  # ← 变量名复用，读的时候注意
    if len(raw) != size or md5(raw).hexdigest() != digest:
        return False
return True
```

设计要点：

- **失败开放**（`return True` when marker missing）：兼容没有校验行的老版本，不因为
  缺功能就拒绝工作。
- 校验的是 **`strip_separator()` 之后的内容**，和远端 `tr -d '\r' <out | wc -c` 对齐。
  远端特意 `tr -d '\r'` 因为文件里可能有 CR（见 `protocol.py:97`）。
- 变量 `raw` 被复用了两次（`protocol.py` 的返回值和 `text.encode()`）。
  能跑，但读起来别扭——第 11 章。
- 对应测试：`test_session_local.py:215-230`（构造一段"好"的 segment 和一个 md5 被改坏的
  版本，断言 `True` / `False`）。

**这个校验解决的是真实故障**：网关会**静默截断**大输出（不发 FIN/RST，就是少给你字节）。
没有 md5 的话你会拿到半截 stdout 却以为成功了。

#### 4.3.8 `_trigger` / `_abort` / `interrupt`

```python
def _trigger(self, token, timeout):        # session.py:370
    if self.tmux:
        self._send(f"tmux send-keys -t {self.tmux} -l {_quote('. ' + script)}\n")
        self._send(f"tmux send-keys -t {self.tmux} Enter\n")
    else:
        self._send(f". {script}\n")

def _abort(self, token):                   # session.py:378
    if self.tmux: self._send(f"tmux send-keys -t {self.tmux} C-c\n")
    self._send("\x03")                     # 给外层 shell 也来一个

def interrupt(self):                       # session.py:386
    self._cancel.set()                     # 让 run() 的等待循环提前退出
    ...同 _abort 的按键发送...
```

- 用 `. script`（source）而不是 `sh script`：**source 才会在当前 shell 里执行**，
  `cd`/`export` 才能留在 pane 里。这是 `test_env_persists` 通过的原因。
- `_abort` 打两个 Ctrl-C：一个进 pane，一个给外层 shell。因为不确定命令卡在哪一层。
- `interrupt()` 是**异步**的：它由客户端（另一个线程/连接）通过 daemon 调
  `{"op":"interrupt"}` 触发（`daemon.py:102`），而 `run()` 正阻塞在 `_cv.wait()` 里。

#### 4.3.9 `info()`（`session.py:395`）—— `--list` 的数据源

```python
{"host", "transport": transport.describe(), "state", 
 "busy": self._cmd_lock.locked(), "idle": now - last_used, 
 "uptime": now - created_at, "last_error"}
```

`busy` 直接问锁，不额外维护状态位——**用一个数据结构的天然属性代替一个状态变量**，
少一个可能不同步的东西。

---

### 4.4 `daemon.py`（208 行）—— 守护进程

**一句话**：`host → Session` 的字典 + Unix socket 上的单行 JSON RPC。

#### 4.4.1 协议（`daemon.py:3-12`）

```
请求:  {"op": "run", "host": "a5-53", "cmd": "...", "timeout": 60}
响应:  {"ok": true, "rc": 0, "stdout": "...", "stderr": "", "duration": 0.21}

op: run | list | restart | stop | ping | interrupt
```

**一行 JSON = 一个请求 = 一个响应 = 一次连接**（`_handle_conn` 处理完就 `close`）。
不做连接复用，简单到不会出错。`MAX_REQUEST = 8MB`（`daemon.py:29`）。

#### 4.4.2 `handle()`（`daemon.py:84-115`）

```
op == ping      → {"ok":true, "pid": ...}
op == stop      → _stopping = True
op == list      → 所有 session.info()，按 host 排序
op == interrupt → session.interrupt()     ┐
op == restart   → session.stop(); start() ├ 这三个需要 host
op == run       → session.run(cmd, timeout).as_dict()
op 未知         → {"ok":false, "error":"未知 op: ..."}
```

注意 **`list`/`ping`/`stop` 不需要 host**，其余走 `self.session(host)` 先取/建 Session。
`session()`（`daemon.py:67`）是懒创建——**第一次 `run` 才会 spawn ssh**。

#### 4.4.3 `serve_forever()`（`daemon.py:149-193`）—— 单实例保护是精华

```python
lock_file = open(self.socket_path.parent / "daemon.lock", "w")
try:
    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    self.log("已有守护进程持有锁，本次启动退出")
    return
```

**为什么必须这样**（注释在 `daemon.py:152-153`）：

> 多个客户端同时冷启动时会各自 spawn 一个守护进程，用独占 flock 只留一个：
> 否则后启动的进程会 unlink 掉前一个的 socket，导致连接莫名被拒。

想象 8 个 agent 同时跑 `lssh`：每个都发现 `daemon_alive()` 为假，每个都 spawn 一个
daemon，8 个 daemon 抢 `bind()` → 后来的 `unlink()` 掉前一个的 socket 文件 →
前一个 daemon 还在跑但没人能连上它，**连接凭空被拒**。这是 CHANGELOG 0.2.0 里记的
真实 bug。

`flock` 是**内核级、随进程退出自动释放**的，所以是唯一正确的选择（`O_EXCL` 文件锁会在
崩溃后留下僵尸锁文件）。

其余初始化：

```python
self.socket_path.unlink()         # 清掉上一次的残留
server.bind(...); os.chmod(0600)  # ★ 只有本用户能连
server.listen(64)
server.settimeout(0.5)            # ★ 让 accept 定期醒来，SIGTERM 才能及时退出
signal.signal(SIGTERM/SIGINT, → _stopping = True)
threading.Thread(_reaper).start()
while not _stopping: accept() → 每连接一个线程
# 收尾：close server → stop 所有 session → unlink socket → log
```

`settimeout(0.5)` 那行注释（`daemon.py:169`）是精髓：**Python 的 `accept()` 不响应信号**
（在 3.5+ 会自动重试），所以必须设超时让它周期性返回 Python 层，才能检查 `_stopping`。

`chmod 0600` 是安全边界（`SECURITY.md:15-19`）：

> 任何能连上这个 socket 的调用方，都能以该 SSH 用户的权限执行命令——把 socket 当成
> 你的 ssh-agent socket 一样对待。

#### 4.4.4 `_reaper()`（`daemon.py:195-208`）

```python
while not _stopping:
    time.sleep(30)
    for host, session in snapshot:
        if (
            session.state == "ready"
            and not session._cmd_lock.locked()
            and now - session.last_used > self.idle_timeout
        ):  # 默认 1800s
            session.stop()
```

三个条件缺一不可：**已就绪**（别重复停）、**空闲**（别把正在跑命令的停掉）、**超时**。
注意它直接读了 `session._cmd_lock`（私有属性）——daemon 和 session 之间是紧耦合的，
这在 Python 里可接受，但越界了。第 11 章会提。

为什么要有 reaper：长连接常驻意味着远端一直有一个 login shell 挂着。30 分钟不用就关掉，
好让网关侧的资源（和你的会话配额）释放。远端 tmux **不受影响**——这就是 detached 的好处，
下次重连 `tmux has-session` 会接上，cwd/env 都还在。

---

### 4.5 `cli.py`（334 行）—— 命令行入口

按"一次调用的执行顺序"读，别按文件顺序读。

#### 4.5.1 控制流全景（`cli.py:252-330`）

```
main(argv)
 ├─ argv[0] == "skills" ? → 懒加载 skills_cli.main(argv[1:])      [255-259]
 ├─ parse_args(argv)                                              [261]
 ├─ --init ? → init_files()，打印 created/exists，return 0        [263-266]
 ├─ apply_settings(args)          ★ 配置合并                      [268]
 ├─ --daemon ? → run_daemon(args) → daemon.serve_forever()        [270-271]
 ├─ 没有 ssh 且没用 --local ? → 退出 2                            [273-275]
 ├─ --list / --stop / --restart ? → ensure_daemon → call(RPC)     [277-304]
 ├─ 没有 host ? → print_help，return 2                            [306-308]
 ├─ cmd = " ".join(args.cmd).strip()；剥掉意外的前导 "--"        [310-312]
 ├─ ensure_daemon(args)                                           [314]
 ├─ cmd 为空 ?
 │    ├─ stdin 是 tty → repl()                                    [317-318]
 │    └─ 否则读 stdin 全部内容当命令                               [319-322]
 ├─ call({"op":"run", ...}) → emit()                              [324-330]
 └─ KeyboardInterrupt → _interrupt() → 退出 130                   [326-329]
```

#### 4.5.2 `apply_settings()`（`cli.py:26-32`）—— 配置合并

```python
settings = resolve(args.host or args.restart)
for key in ("timeout", "idle", "tmux", "connect_timeout", "cols", "rows"):
    if getattr(args, key) is None:
        setattr(args, key, settings[key])
args.askpass = installed_askpass()
```

**只有 CLI 没给（`is None`）的才用配置填**——这就是文档承诺的优先级：

```
命令行 flag  >  config.json 里匹配的 hosts 项  >  config.json 顶层  >  内置默认
```

`argparse` 里这些参数的 `default` 必须写 `None`（`cli.py:232,241,244-247`），
否则你没法区分"用户没给"和"用户给了等于默认值的值"。**这是个常被写错的点**。

#### 4.5.3 守护进程的拉起：`ensure_daemon()`（`cli.py:86-117`）

```python
if daemon_alive(): return
cmd = [sys.executable, "-m", "lssh", "--daemon"]
# 把相关 flag 原样透传：--no-tmux / --tmux / --local / --idle / --cols / --rows / --ssh-arg×N
subprocess.Popen(cmd, stdin/stdout/stderr=DEVNULL, start_new_session=True, close_fds=True)
for _ in range(80):        # 最多等 8 秒
    time.sleep(0.1)
    if daemon_alive(): return
raise SystemExit("lssh: 守护进程启动失败，请查看 " + str(log_path()))
```

- `sys.executable` 而不是 `"lssh"`：保证用的是同一个解释器/venv。
- `start_new_session=True` = `setsid`，脱离终端，父进程退出也不受影响。
- `DEVNULL` 三连：守护进程绝不能持有终端，否则终端关闭时的 SIGHUP 会带走它。
- **轮询 8 秒**而不是等某个信号：`daemon_alive()` 只能靠试连。

`daemon_alive()`（`cli.py:77-83`）的实现有个脆弱点：

```python
sock.sendall(b'{"op": "ping"}\n')
return b'"ok": true' in sock.recv(4096)  # 靠 json.dumps 的默认分隔符
```

它依赖 `json.dumps({"ok": True, ...})` 产出 `"ok": true`（带空格）。如果哪天有人给
`json.dumps` 加了 `separators=(",", ":")`，这行就静默失效了。第 11 章。

#### 4.5.4 客户端 RPC：`call()` / `_call_once()`（`cli.py:120-141`）

```python
def call(request, args=None):
    try:
        return _call_once(request)
    except (ConnectionRefusedError, FileNotFoundError):
        if args is not None:
            ensure_daemon(args)  # 守护进程刚退出（--stop 之后常见）
        return _call_once(request)
```

**重试一次**处理"daemon 刚挂/刚被 stop"的竞态。`CLIENT_TIMEOUT = 24h`（`cli.py:20`）——
因为一条命令可能跑很久，客户端 socket 不能有短超时。

`_call_once` 读响应：`while not buffer.endswith(b"\n")` 循环 `recv(65536)`。
**一行 JSON + 结尾换行**是唯一的帧边界。

#### 4.5.5 退出码映射：`emit()`（`cli.py:144-161`）

```python
if not ok:            → 打 "lssh: <error>" 到 stderr，return 1
rc = result["rc"]
rc == 0               → 0
0 < rc < 256          → rc          （透传远端退出码）
否则（None / 负数 / 越界） → 1
```

设计意图：**让 `lssh h 'cmd'` 能替换 `ssh h 'cmd'` 放进脚本**——退出码语义一致。
连接/超时/校验失败统一返 1（不是 255，因为 255 是 ssh 自己的保留值）。

#### 4.5.6 REPL（`cli.py:171-195`）

`lssh host`（无命令 + stdin 是 tty）进入逐行模式。特殊输入：
`exit` / `quit` / `:q` 退出，`:interrupt` 发 Ctrl-C。每次输入独立成一条命令，
但共享同一个 Session → cwd/env 自然保留。

#### 4.5.7 `build_parser()`（`cli.py:198-249`）—— 值得完整读一遍

`epilog`（`cli.py:205-223`）里塞了完整的行为说明和长任务示例。这不是装饰——它是
**给 AI Agent 看的**，因为 agent 通常先跑 `lssh -h`。同理 `skills.py:114` 会把
`build_parser().format_help()` 抓去生成 `references/commands.md`，保证技能文档不漂移。

参数分三组：

| 用户可见 | `--list` `--restart` `--stop` `--json` `--timeout` `--version` `--init` `--tmux` `--no-tmux` `--ssh-arg` `--connect-timeout` `--idle` `--cols` `--rows` `--local` |
|---|---|
| 隐藏 | `--daemon`（`argparse.SUPPRESS`，内部用） |
| 位置参数 | `host`（`nargs="?"`）、`cmd`（`argparse.REMAINDER`） |

`REMAINDER` 的作用：`lssh h 'echo hi' --json` 里的 `--json` 会被当成命令的一部分
（**选项必须写在 host 前面**，和 `ssh` 一致）。`cli.py:311-312` 那个"剥掉前导 `--`"
的小 hack 就是为了兜住用户写成 `lssh -- h 'cmd'` 的情况。

---

### 4.6 `paths.py` / `config.py` / `askpass.py` —— 边缘设施

#### 4.6.1 `paths.py`（45 行）

| 函数 | 默认位置 | 环境变量覆盖 |
|---|---|---|
| `cache_dir()` | `~/.cache/lssh` | `XDG_CACHE_HOME` |
| `runtime_dir()` | `$XDG_RUNTIME_DIR/lssh`，否则 fallback 到 `cache_dir()` | `XDG_RUNTIME_DIR` |
| `socket_path()` | `runtime_dir()/daemon.sock` | `LSSH_SOCKET` |
| `config_dir()` | `~/.config/lssh` | `XDG_CONFIG_HOME` |
| `config_path()` | `config_dir()/config.json` | — |
| `secrets_path()` | `config_dir()/secrets` | — |
| `log_path()` | `cache_dir()/daemon.log` | `LSSH_LOG` |

`runtime_dir()` 的 fallback（`paths.py:18`）是为了 macOS——它没有 `XDG_RUNTIME_DIR`。
**socket 放在 `$XDG_RUNTIME_DIR`（通常是 tmpfs、重启即清）比放在 `~/.cache` 更合适**。

`askpass_path()`（`paths.py:35`）**是死代码**，全项目无人调用（见第 11 章）。

#### 4.6.2 `config.py`（97 行）

```python
DEFAULTS = {
    "timeout": 120.0,
    "idle": 1800.0,
    "tmux": "lssh",
    "connect_timeout": 15,
    "cols": 512,
    "rows": 50,
}  # :23
```

```python
def resolve(host=None):  # :50
    effective = {k: cfg.get(k, v) for k, v in DEFAULTS.items()}
    for pattern, overrides in (cfg.get("hosts") or {}).items():
        if fnmatch.fnmatch(host, pattern):  # ★ 第一个匹配就 break
            effective.update({k: v for k, v in overrides.items() if v is not None})
            break
    return effective
```

两个细节：

- **first match wins**（`break`），所以 `hosts` 的顺序有意义。文档 `docs/configuration.md:43`
  明确写了"glob, first match wins"。
- `if v is not None` 过滤：让你可以在 hosts 覆盖里写 `null` 表示"这个 key 不覆盖"。
- 匹配对象是**命令行里写的那个字符串**（`~/.ssh/config` 的别名，或 `user@host`），
  不是解析后的真实主机名。因为 lssh 不解 SSH 配置。

`init_files()`（`config.py:79`）幂等：`_write_if_missing` 存在就跳过，返回 `(path, created)`。
secrets 和 config.json 都是 **0600**，目录 **0700**。

#### 4.6.3 `askpass.py`（73 行）

```
OpenSSH 需要密码 → 执行 $SSH_ASKPASS，argv[1] = "root@10.0.0.9's password:"
                                    ↓
                     host_from_prompt()  →  "10.0.0.9"
                                    ↓
                     lookup(host)        →  按顺序 fnmatch 匹配 ~/.config/lssh/secrets
                                    ↓
                     stdout 打印密码，exit 0；找不到 exit 1
```

`host_from_prompt()`（`askpass.py:27`）用 `rfind("@")` 而不是 `split("@")`——因为
某些网关的用户名里带 `@`（比如 `user@domain@10.0.0.9`）。然后按 `'`、空格、`:`、`,`
依次切掉尾巴。

`lookup()`（`askpass.py:38`）：跳过空行和 `#` 注释，`split(None, 1)` 切出 pattern 和
password（password 里可以有空格），`*` 是万能匹配，**first match wins**。

`SECRETS_PATH` 在**模块导入时**计算（`askpass.py:22`），而 `paths.config_dir()`
是**每次调用时**读环境变量。行为差异很小（askpass 是独立进程，环境不会变），
但测试里必须用 `monkeypatch.setattr(askpass, "SECRETS_PATH", ...)` 而不是 `setenv`
（见 `test_config.py:44`）。

---

### 4.7 `skills.py` / `skills_cli.py` —— Agent Skill 安装器

**这部分和主链路完全无关**，可以最后读。它的价值是"如何优雅地往别人的目录里写东西"。

#### 4.7.1 核心问题

「让 AI Agent 学会用 lssh」需要把 `SKILL.md` 放进 Agent 读的目录。但：

- 不同 Agent 读不同目录（`~/.claude/skills`、`~/.qwen/skills`、…）；
- 有一个正在成为标准的跨 Agent 目录 `~/.agents/skills`；
- **绝不能给用户没装的 Agent 创建目录**；
- **绝不能覆盖用户手写的同名技能**。

#### 4.7.2 三个机制

**（a）`Target` + `detected()`**（`skills.py:43-55`）

```python
@dataclass(frozen=True)
class Target:
    agent: str
    root: Callable[[], Path]
    always: bool = False

    def skill_dir(self):
        return self.root() / "skills" / SKILL_NAME

    def detected(self):
        return self.always or self.root().is_dir()
```

`root` 是 **lambda 而不是 Path**——环境变量（`CLAUDE_CONFIG_DIR` 等）必须在**调用时**
读，不能在建表时读死。

`always=True` 的只有 `agents`（跨 Agent 标准目录），因为它是 lssh 的"主副本"。

**（b）`.lssh-install.json` 标记文件**（`skills.py:28`、`:192-198`）

```json
{"skill": "lssh", "version": "0.4.0", "hash": "a1b2c3d4e5f6a7b8", "installed_at": 1761...}
```

**所有写/删操作都先查标记**。`uninstall()`（`skills.py:211`）遇到没有标记的目录直接
`continue`——"别人手写的东西我一根手指都不碰"。`plan()`（`skills.py:151-152`）同样：

```python
elif dest.is_symlink() or dest.exists():
    action = "update" if marker else "skip: existing folder was not installed by lssh"
```

冲突时必须 `--force` 才覆盖（`skills.py:175-176`），对应测试 `test_conflict_needs_force`
（`test_skills.py:66`）。

**（c）内容哈希做"是否过期"判定**（`skills.py:118-128`）

```python
def _source_hash(src, extra):
    digest = hashlib.sha256()
    for path in sorted(src.rglob("*")):
        digest.update(path.relative_to(src).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    for name, text in sorted(extra.items()):
        ...
    return digest.hexdigest()[:16]
```

`status()`（`skills.py:218-234`）比较标记里的 hash 和**当前** bundle 的 hash →
`up_to_date` / `stale`。比版本号更准确（同一个版本也可能改了文档）。

#### 4.7.3 `commands_reference()`（`skills.py:106-115`）

```python
def commands_reference() -> str:
    from .cli import build_parser  # ★ 函数内导入，避开循环依赖

    help_text = build_parser().format_help().rstrip()
    return (
        f"# lssh 命令参考\n\n"
        f"由 lssh skills install 根据 lssh {__version__} 自动生成。\n"
        "参数含义以 lssh -h 为准。\n\n"
        # 下面用三反引号包住 help_text（原文里是字面量，这里示意）
        "FENCE\n" + help_text + "\nFENCE\n"
    )
```

（原文 `skills.py:110-115` 里 `FENCE` 的位置是三个反引号；这里为了避免嵌套代码围栏，
替换成了 `FENCE`。）

**文档从代码生成 → 永不漂移**。这解决了 AI 技能文档的通病（写了 CLI 帮助的副本，
改了 CLI 忘了改技能）。测试 `test_commands_reference_comes_from_cli`
（`test_skills.py:29`）保证它真的来自 CLI。

#### 4.7.4 `skills_cli.py` 的 UX 模式

`install` / `uninstall` **默认只预览**，必须加 `--yes` 才写（`skills_cli.py:92`、`:129`）。
这是"给 AI Agent 用的 CLI"的正确姿势——agent 容易冲动，默认非破坏性。

`_split()`（`skills_cli.py:63`）把 plan 结果分成三类返回：
`targets`（要写的）/ `agents_not_found`（没装的）/ `conflicts`（冲突的），
`--json` 时原样输出，脚本和人都能用。

---

## 5. 七个"为什么" —— 设计决策速查

每一条都能在代码注释或 CHANGELOG 里找到出处。**这是这个项目最值得学的部分**：
后面每一条都对应一个真实踩过的坑。

| # | 决策 | 如果不这么做会怎样 | 出处 |
|---|---|---|---|
| 1 | **只用一条 `ssh -tt`，不做每命令一登录** | 网关限流封禁 | `session.py:5`、`README.md:15-16` |
| 2 | **远端 tmux `-d` detached，永不 attach** | 读到的是渲染后的屏幕，丢换行、重放历史 | `session.py:6-8` |
| 3 | **命令输出写文件再 `cat`，不读屏** | 输出和终端渲染耦死，换行/空白/中文全不可靠 | `protocol.py:3-8` |
| 4 | **命令用 base64 分块写入** | 目标机行编辑器（ble.sh/zsh 高亮）重绘把多行输入拆坏 | `protocol.py:66`、CHANGELOG 0.2.0 |
| 5 | **marker 整行锚定 `^...$`** | 被回显/历史内容误命中，串味 | `protocol.py:112`、`test_session_local.py:73` |
| 6 | **结果带长度 + md5 校验** | 网关静默截断大输出，你拿到半截还以为是完整的 | `session.py:349`、CHANGELOG 0.2.0 |
| 7 | **`flock` 保证守护进程单实例** | 多客户端冷启动互相 unlink socket，连接莫名被拒 | `daemon.py:152`、CHANGELOG 0.2.0 Fixed |

再补三条不那么显眼但同样重要的：

| # | 决策 | 理由 |
|---|---|---|
| 8 | **`{ }` 分组而非 `( )`**（`protocol.py:62`） | 不开 subshell，`cd`/`export` 才能留下来 |
| 9 | **先远端轮询超时，客户端只做兜底**（`session.py:289`） | 远端超时有精确语义（`rc=-1`），能干净地 Ctrl-C |
| 10 | **`Transport` 抽象 + `LocalTransport`**（`transports.py`） | 让 90% 的时序逻辑能在毫秒内、无网络地测 |

---

## 6. 数据流全景：一次 `lssh a5-53 'echo hi'` 的字节旅程

按时间顺序，标出**每一段字节在哪里**。

```
① CLI 进程
   argv = ["a5-53", "echo hi"]
   apply_settings → timeout=120, tmux="lssh", cols=512, rows=50
   ensure_daemon() → daemon_alive() 失败 → Popen(python -m lssh --daemon ...) → 轮询 8s
   call({"op":"run","host":"a5-53","cmd":"echo hi","timeout":120})
   → 写入 socket:  {"op": "run", "host": "a5-53", "cmd": "echo hi", "timeout": 120}\n
   → 阻塞读，等一行 JSON

② 守护进程 (线程 T-conn)
   handle() → session("a5-53")  # 首次调用，懒创建
              → session.run("echo hi", 120)
                │
                ├─ state != READY → start()
                │    ├─ transport.spawn() → pty.openpty(); ssh -tt a5-53（子进程）
                │    │                      fd 写入: 无（ssh 自己连）
                │    ├─ 起读线程 T-read  → select(fd)
                │    ├─ _probe(60)  fd ← "printf '%s-OK\n' LSSHa1b2\n"
                │    │              fd → "printf '%s-OK\\n' LSSHa1b2\r\n"   ← 回显（字面量 \n，不匹配）
                │    │              fd → "LSSHa1b2-OK\r\n"                 ← 真执行（匹配！）
                │    ├─ fd ← PRE_BOOTSTRAP（TERM / unset PROMPT_COMMAND / stty -echo -onlcr / PS1=''）
                │    ├─ fd ← tmux has-session || new-session -d -s lssh; tmux set -g ...
                │    ├─ fd ← tmux send-keys -t lssh -l 'stty -echo; PS1=...' ; tmux send-keys -t lssh Enter
                │    ├─ _probe(40)  → 同样等到一个真执行的标记
                │    └─ state = READY

③ Session.run 主体（持 _cmd_lock）
   token  = "LSSH9f3c2a7b1e4d"
   offset = len(_buf)
   script = "{\necho hi\n} >/tmp/.lssh-LSSH9f3c.out 2>/tmp/.lssh-LSSH9f3c.err\n
             echo $? >/tmp/.lssh-LSSH9f3c.rc\n: >/tmp/.lssh-LSSH9f3c.done\n"

   fd ← : > /tmp/.lssh-...b64
   fd ← printf %s 'ewplZWNobyBoaQp9ID4vdG1wLy5sc3No...' >> /tmp/.lssh-...b64     ← base64 分块，每块 900B
   fd ← (base64 -d ...b64 > ...sh 2>/dev/null || openssl base64 -d -A -in ...b64 > ...sh); rm -f ...b64
   fd ← rm -f ...out ...err ...rc ...done
   fd ← tmux send-keys -t lssh -l '. /tmp/.lssh-LSSH9f3c.sh'      ← 命令进入 pane
   fd ← tmux send-keys -t lssh Enter
   fd ← __lssh_i=0; while [ ! -f ...done ] && [ $__lssh_i -lt 1200 ]; do sleep 0.1; ... done
   fd ← printf 'LSSH9f3c SZ %s %s\n' "$(tr -d '\r' <...out | wc -c)" "$(... | md5sum | cut -d' ' -f1)"; ...
        printf 'LSSH9f3cS\n'; cat ...out; printf '\nLSSH9f3cM\n'; cat ...err; printf '\n';
        printf 'LSSH9f3cR:%s\n' "$(cat ...rc || echo -1)"; printf 'LSSH9f3cE\n'; rm -f ...

④ 远端（外层 shell 执行上面那一长串；pane 里 echo hi 已经跑完并写了 out/rc/done）
   下面是**实测抓到的 fd 字节流**（去掉 ANSI 并已 CRLF→LF，即 clean() 之后）：

   fd → "\n\n\n\n\n\n"                                   ← ★ 杂散换行，见下面的注释
        "LSSH9f3c2a7b SZ 3 764efa883dda1e11db47671c4a3bbd9e\n"
        "LSSH9f3c2a7b MZ 0 d41d8cd98f00b204e9800998ecf8427e\n"
        "LSSH9f3c2a7bS\n"
        "hi\n"
        "\n"                                            ← printf 的换行保险
        "LSSH9f3c2a7bM\n"
        "\n"
        "LSSH9f3c2a7bR:0\n"
        "LSSH9f3c2a7bE\n"

   注意 md5 是 "hi\n"（3 字节）的 md5，不是 "hi"（2 字节）的——因为远端算的是**文件字节**。

⑤ 读线程 T-read
   os.read(fd, 65536) → clean() 去 ANSI、CRLF→LF
   _buf += 上面那一整段
   _cv.notify_all()

⑥ Session.run 的等待循环
   parse(_buf[offset:], token)
     → parse_raw 用 marker().search() 定位四行（所以开头那 6 个杂散换行不影响）
        rc=0
        raw_stdout = "\nhi\n\n"      ← S 行之后到 M 行之前，含首尾各一个分隔换行
        raw_stderr = "\n"            ← err 文件是空的，只剩分隔换行
     → strip_separator: "hi\n" / ""
     → parse 的额外 .strip("\n"): (0, "hi", "")   ← 给用户的干净结果
   _verify: 远端说 3 字节 + md5 764efa...；本地 "hi\n" → 3 字节，md5 相同 → True
   Result(ok=True, rc=0, stdout="hi", stderr="", duration=0.045)

⑦ 守护进程回包
   {"ok": true, "rc": 0, "stdout": "hi", "stderr": "", "duration": 0.045, "error": null}\n

⑧ CLI emit()
   stdout 写出 "hi\n"；rc==0 → 退出码 0
```

> 💡 **开头那 6 个 `\n` 是什么？** 实测确认：**我们每 `_send(line + "\n")` 一次，
> pty 就会回一个 `\n` 上来**（`stty -echo` 挡住了字符回显，但换行仍会被回显）。
> 那一次 `run()` 一共往下推了 6 行（`: >b64` / `printf` 分块 / 解码 / `rm` /
> `. script` / report 那一长串），正好 6 个 `\n`。
>
> 这也说明为什么 `parse_raw` 必须用 `search()` 而不是 `match()`、为什么 `run()` 要记
> `offset`——**流里永远可能有和本条命令无关的字节**。
>
> 顺带一个印证：`start()` 早期抓到的 `_buf` 里能看到 `bash-5.2$ export TERM=...`，
> 那就是 `PS1=''` 生效前的初始提示符。**这就是 `PRE_BOOTSTRAP` 必须清 `PS1/PS2` 的原因**——
> 提示符会被插进 marker 之间，把整行锚定搞坏。

> 💡 **`duration=0.045` 是怎么来的？** `time.time() - started`，`started` 在发送前记录
> （`session.py:271`）。它包含"写脚本 + base64 解码 + 执行 + cat 回来"的全部耗时，
> 不是远端命令自己的运行时间。

**打印这几段字节出来自己对着看一遍**（第 8 章实验 3 会教你怎么做），
这个协议就永久记住了。

---

## 7. 测试怎么读 —— 测试即规范

48 项测试（47 pass + 1 skip），无网络依赖。**它们比文档更权威**。

```bash
make test        # .venv/bin/python -m pytest -q
```

### 7.1 `test_session_local.py`（25 项）—— 最重要的一个

分成三层：

**第一层：纯函数（6 项）** —— 无 Session，直接测 protocol

| 测试 | 断言了什么 |
|---|---|
| `test_clean_strips_ansi_and_crlf` (:36) | `\x1b[?2004hhello\r\n` → `"hello\n"` |
| `test_clean_keeps_utf8` (:41) | `clean()` 不破坏中文 |
| `test_build_script_has_files_and_rc` (:45) | 脚本里真的有 out/done/`echo $?` |
| `test_parse_detects_missing_markers` (:54) | marker 不全 → `None` |
| `test_parse_splits_streams` (:59) | 正常切分 stdout/stderr |
| `test_parse_rejects_echo` (:73) | **回显不算**（4.1.2 那条） |

**第二层：真实本地 shell（`session` fixture，tmux=None，13 项）** —— 完整协议真跑

`test_echo`、`test_exit_code`（含 42）、`test_stderr_separated`、`test_multiline_command`、
`test_cwd_persists`、`test_env_persists`、`test_no_trailing_newline`（`printf abc` → `"abc"`）、
`test_unicode_roundtrip`、`test_large_output`（2000 行）、`test_stdin_not_needed`、
`test_command_with_quotes_and_pipes`、`test_empty_command`

> 这一层是**协议的行为规范**。你改 `protocol.py` 的任何东西，这些测试必须仍然通过。

**第三层：真实 tmux（`tmux_session` fixture，6 项）** —— 只有这条路能精确测超时

```python
@pytest.fixture()
def tmux_session(tmp_path):
    name = "lssh-test-" + next(tempfile._get_candidate_names())
    s = Session(host="local-tmux", transport=LocalTransport(), tmux=name)
    yield s
    s.stop()
    subprocess.run(["tmux", "kill-session", "-t", name], ...)  # ★ 必须清理
```

| 测试 | 断言了什么 |
|---|---|
| `test_timeout_reported` (:137) | `sleep 5` + `timeout=0.6` → `"超时" in error` |
| `test_recover_after_timeout` (:143) | 超时 → `interrupt()` → 下一条命令仍然可用 |
| `test_interrupt_ends_long_command` (:160) | 另起线程跑 `sleep 30`，主线程 `interrupt()`，断言线程能退出 |
| `test_two_timeouts_mark_dirty` (:233) | 连续两次超时 → `state == "dirty"`（4.3.6 的 `_strikes`） |
| `test_verify_detects_truncation` (:215) | 篡改 md5 → `_verify()` 返回 `False` |

注意 `test_interrupt_ends_long_command` 的注释（`:161-165`）坦白了一个限制：

> interrupt() 发 Ctrl-C；本地 shell 没有控制终端时可能不生效，
> 所以这里只断言：命令被中断或至少按时超时返回，且会话随后仍可用。

**测试会诚实地标注自己的边界**——这是好测试的标志。

**可选的真实 SSH 冒烟测试**（`test_real_ssh_optional`，:189）：只有设置了
`LSSH_TEST_SSH=<host>` 才跑，否则 skip（这就是那个 1 skipped）。

### 7.2 `test_daemon.py`（6 项）

`daemon` fixture 在 tmp_path 起一个真实 daemon 线程，`rpc()` 用真 socket 发请求。

| 测试 | 断言 |
|---|---|
| `test_ping` | `{"op":"ping"}` → ok |
| `test_run_and_persist` | 同一 host 两次 run，第二次能看到第一次的 `cd /tmp` |
| `test_hosts_isolated` | host `a` 的 cwd 不影响 host `b` |
| `test_list` | `list` 返回的 host 和 `state == "ready"` |
| `test_restart_keeps_channel_working` | restart 后还能跑命令 |
| `test_unknown_op` | 未知 op → `ok: false` |

### 7.3 `test_config.py`（8 项）

askpass 的 prompt 解析、通配匹配、`*` 兜底、文件缺失、退出码；
config 的四级优先级（`test_config_resolution`）和 `--init` 幂等 + 权限 0600
（`test_init_is_idempotent`）。

### 7.4 `test_skills.py`（9 项）

`plan` 只处理 detected 的 agent；install→update→uninstall 全周期；
冲突需 `--force`；`uninstall` 不碰外来目录；project targets；CLI 预览/安装/status/show；
未知 agent 报错。

**`test_install_leaves_foreign_folder`（:82）是这个模块的核心不变量**：

```python
dest.mkdir(parents=True)
(dest / "SKILL.md").write_text("not ours")
assert skills.uninstall(skills.TARGETS) == []
assert dest.exists()  # ← 不能删别人的
```

---

## 8. 动手实验（必须真的跑，光看学不会）

准备：

```bash
cd ~/code/lssh
make install     # 或者已有 .venv 就跳过
source .venv/bin/activate
```

### 实验 1：跑通一个本地会话（5 分钟）

```python
from lssh.session import Session
from lssh.transports import LocalTransport

s = Session(host="local", transport=LocalTransport(), tmux=None)
print(s.run("cd /tmp && pwd").stdout)  # /tmp
print(s.run("pwd").stdout)  # /tmp  ← 状态保留了！
print(s.run("echo $?").stdout)
print(s.run("seq 1 3").as_dict())
print(s.info())
s.stop()
```

**看什么**：`cwd` 保留 = `{ }` 分组不开 subshell（决策 #8）。

### 实验 2：打开 tmux 路径（5 分钟）

```python
s = Session(host="local-tmux", transport=LocalTransport(), tmux="lssh-exp")
r = s.run("pwd")
print(r.ok, r.rc)
# 另开一个终端：
#   tmux ls                          → 看到 lssh-exp
#   tmux capture-pane -t lssh-exp -p → 看到 pane 里的东西
s.stop()
```

**看什么**：`tmux ls` 里 lssh-exp 是 **detached** 的；`capture-pane` 能看到
`stty -echo; PS1=...` 这些 lssh 塞进去的东西。

### 实验 3：偷看线上字节流（⭐ 最重要的实验，15 分钟）

在 `Session.run()` 的等待循环前（`session.py:288` 附近）临时插一行：

```python
print("=== WIRE ===", repr(self._buf[offset:]))
```

然后：

```python
s = Session(host="local", transport=LocalTransport(), tmux=None)
print(s.run("echo hi").as_dict())
```

你会看到类似：

```
=== WIRE === 'LSSH... SZ 3 49f68a5c...\nLSSH... MZ 0 d41d8cd9...\nLSSH...S\nhi\nLSSH...M\nLSSH...R:0\nLSSH...E\n'
```

**把这段字节和 `protocol.py:91-108` 对照着读一遍**，第 6 章就永久记住了。看完记得删掉这行。

### 实验 4：验证 marker 的整行锚定（10 分钟）

```python
from lssh.protocol import parse, new_token

t = new_token()
# 命令回显（字面量 \n）
print(parse(f"printf '{t}S\\n'; cat /tmp/x\n", t))  # None ← 回显不算
# 真输出
print(parse(f"{t}S\nhi\n{t}M\n{t}R:0\n{t}E\n", t))  # (0, 'hi', '')
```

### 实验 5：亲眼看到超时、Ctrl-C 与 `_strikes`（10 分钟）

```python
import subprocess
from lssh.session import Session
from lssh.transports import LocalTransport

s = Session(host="local-tmux", transport=LocalTransport(), tmux="lssh-exp2")
try:
    for i in (1, 2):
        r = s.run("sleep 30", timeout=0.6)
        print(i, r.ok, r.error, "| strikes =", s._strikes, "| state =", s.state)
    # 1 False 命令超时（0.6s），已发送 Ctrl-C | strikes = 1 | state = ready
    # 2 False 命令超时（0.6s），已发送 Ctrl-C | strikes = 2 | state = dirty
finally:
    s.stop()
    subprocess.run(["tmux", "kill-session", "-t", "lssh-exp2"], capture_output=True)
```

**看什么**：`strikes = 1` 时还 `ready`（留一次容错），`= 2` 才 `dirty`。

> ⚠️ 这个计数器是**连续**计数：中间插一句成功的命令（`s.run("echo alive")`）会把它清零
> （`session.py:337`）。我第一次做这个实验时就踩了这个坑，看到两次超时后 state 仍是 `ready`。
> 这也反过来证明了"单次抖动不该判死"的设计意图。

### 实验 5b：超时后会话仍可用（5 分钟）

```python
s = Session(host="local-tmux", transport=LocalTransport(), tmux="lssh-exp3")
r = s.run("sleep 30", timeout=1.0)
print(r.ok, r.error)  # False 命令超时（1.0s），已发送 Ctrl-C
print(s.run("echo alive").stdout)  # alive ← abort 之后通道是干净的
s.stop()
```

**看什么**：`_abort()` 发的 Ctrl-C 只打断当前命令（`session.py:378`），不会毁掉会话。

### 实验 6：破坏校验，看它报警（5 分钟）

```python
import hashlib
from lssh.transports import LocalTransport

s = Session(host="l", transport=LocalTransport(), tmux=None)
good = hashlib.md5(b"hello").hexdigest()
seg_ok = f"T SZ 5 {good}\nT MZ 0 d41d8cd98f00b204e9800998ecf8427e\nTS\nhello\nTM\nTR:0\nTE\n"
print(s._verify("T", seg_ok))  # True
print(s._verify("T", seg_ok.replace(good, "0" * 32)))  # False ← 校验失败
```

### 实验 7：看守护进程真的只有一个（5 分钟）

**别用默认 socket 做这个实验**（会杀掉你正在用的守护进程）。用临时 socket 隔离：

```bash
export LSSH_SOCKET=$(mktemp -d)/daemon.sock
export LSSH_LOG=$(dirname $LSSH_SOCKET)/daemon.log

for i in $(seq 6); do lssh --list >/dev/null 2>&1 & done; wait

cat $LSSH_LOG
```

实测输出（6 个客户端同时冷启动）：

```
2026-10-23 10:44:40 已有守护进程持有锁，本次启动退出
2026-10-23 10:44:40 启动 pid=68475 socket=/tmp/tmp.kYclhZ32js/daemon.sock
2026-10-23 10:44:40 已有守护进程持有锁，本次启动退出
2026-10-23 10:44:40 已有守护进程持有锁，本次启动退出
2026-10-23 10:44:40 已有守护进程持有锁，本次启动退出
2026-10-23 10:44:40 已有守护进程持有锁，本次启动退出
```

**6 个进程抢，1 个 `bind()` 成功，5 个看到 flock 失败立刻退出**——决策 #7 在起作用。

> ⚠️ 不要用 `pgrep -f "lssh --daemon" | wc -l` 判定：它也会匹配到你**默认 socket 上
> 早已存在的那个守护进程**，计数会大于 1。以日志为准，或用 `ps -eo pid,args | grep -F
> -- "--daemon"` 看完整命令行。

顺手可以看一眼 daemon 的真实命令行（这是最好的"参数到底传没传进去"的证据）：

```
/home/zhu/code/lssh/.venv/bin/python -m lssh --daemon --tmux lssh --idle 1800.0 --cols 512 --rows 50
```

**注意上面没有 `--connect-timeout`**——这正是第 11 章技术债 #1 的现场证据。

收尾：

```bash
lssh --stop          # 用同一个 LSSH_SOCKET 环境变量，只停这个临时守护进程
unset LSSH_SOCKET LSSH_LOG
```

### 实验 8：跟踪一次完整 RPC（5 分钟）

```python
import json, socket
from lssh.paths import socket_path

with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
    s.connect(str(socket_path()))
    s.sendall(b'{"op":"list"}\n')
    print(json.dumps(json.loads(s.recv(65536)), ensure_ascii=False, indent=2))
```

**看什么**：`Daemon.handle()` 的全部 op 都可以这样裸调——这是最好的调试入口。

### 实验 9：改一个选项，走完整条链（30 分钟，第一次改代码）

任务：给 `Result` 加一个 `truncated: bool` 字段，当输出超过 N 字节时置 True。

需要动的文件（体会一下"一个功能穿过几层"）：

1. `protocol.py` — 不用改（长度已经报回来了）
2. `session.py:61-78` — `Result` 加字段 + `as_dict()`
3. `session.py:338-347` — 判断 `len(stdout) > N` 时置位
4. `daemon.py` — 不用改（`as_dict()` 自动带出）
5. `cli.py:144-161` — 可选：在 `emit()` 里打警告
6. `tests/test_session_local.py` — 加测试

**这个练习的价值在于：你会发现主链路只有 3 个文件，改动是线性的。** 这说明架构是干净的。

### 实验 10：读日志排障（10 分钟）

```bash
tail -20 ${XDG_CACHE_HOME:-~/.cache}/lssh/daemon.log
```

日志只记生命周期事件（启动/退出/空闲关闭/锁冲突），**绝不记录命令内容**
（`SECURITY.md:13-14` 承诺）。想想为什么：命令里可能有密码。

---

## 9. 调试入口速查

| 我想知道 | 怎么做 |
|---|---|
| 守护进程活着吗 | `lssh --list` / 裸 socket `{"op":"ping"}` |
| 哪些会话、什么状态 | `lssh --list --json` |
| 守护进程发生了什么 | `tail ~/.cache/lssh/daemon.log` |
| socket 在哪 | `python -c "from lssh.paths import socket_path; print(socket_path())"` |
| 线上字节流长什么样 | 实验 3 的 `print(repr(...))` |
| 不看 ssh 排除网络问题 | `lssh --local /bin/bash h 'cmd'`（**注意 host 仍然必填，但被忽略**） |
| 只跑超时相关测试 | `pytest tests/test_session_local.py -k timeout -v` |
| 远端 tmux 里到底有什么 | `tmux capture-pane -t lssh -p` |
| 残留的远端临时文件 | `ls -la /tmp/.lssh-*`（正常情况应该没有） |

### 9.1 常见故障 → 代码位置

| 现象 | 去看 |
|---|---|
| `连接失败: 远端 shell 未就绪` | `_probe()` `session.py:229`。认证失败或网关限流（60s 预算内远端 shell 没响应） |
| `远端 shell 初始化失败` | `start()` `session.py:139`。bootstrap（`stty`/`tmux`）没生效 |
| `命令超时（Ns），会话已重置` | `session.py:309-318`（marker 都没收到） |
| `命令超时（Ns），已发送 Ctrl-C` | `session.py:322-336`（`rc=-1`，pane 里还在跑） |
| `输出校验失败` | `_verify()` `session.py:349`。网关截断，或输出含非法 UTF-8 |
| `已中断（Ctrl-C）` | `session.py:303-308`，客户端 `interrupt()` 生效了 |
| `守护进程启动失败` | `ensure_daemon()` `cli.py:117`。看 daemon.log |
| `BUSY` 一直不消 | 有命令卡在持锁；等超时或 `--restart` |
| 退出码不对 | `emit()` `cli.py:144-161` 的映射规则 |
| 配置文件没生效 | `resolve()` `config.py:50` 的 first-match-wins；`apply_settings()` 只填 `None` 项 |

---

## 10. 改动 cheatsheet：想做什么 → 改哪里

| 任务 | 文件 | 要点 |
|---|---|---|
| 加一个 CLI 选项 | `cli.py:198` `build_parser()` | 记得 `default=None`（好让配置能填）；如果守护进程也需要，加进 `ensure_daemon()` 的透传列表 `cli.py:89-104` |
| 加一个配置项 | `config.py:23` `DEFAULTS` + `cli.py:29` 的 key 元组 + `docs/configuration.md` | 三处都要改 |
| 加一个 RPC op | `daemon.py:84` `handle()` + `cli.py` 里调用 | 需要 host 的放在 `session = self.session(host)` 之后 |
| 改输出分帧格式 | `protocol.py` 的 `build_report_lines` + `parse_raw` + `session.py:_verify` | **必须同步改这三处**，并跑全量测试 |
| 加一种 Transport（容器/串口/k8s exec） | `transports.py` 新增一个类，实现 `spawn`/`describe` | 不用动 `session.py`——这就是抽象的价值 |
| 改超时语义 | `session.py:288-318` + `protocol.py:80` `build_poll_lines` | 注意远端/客户端两个 budget（`+30`） |
| 改重连策略 | `session.py:324-337` `_strikes` 逻辑 | 现在是"连续 2 次超时判死" |
| 改空闲回收 | `daemon.py:195` `_reaper()` + `--idle` | 30 秒扫一次 |
| 改会话状态展示 | `session.py:395` `info()` + `cli.py:284-295` 的格式化 | |
| 加一个 Agent 目标目录 | `skills.py:58` `TARGETS` 加一行 `Target(...)` | `always=False`（只有检测到才写） |
| 改技能文本 | `src/lssh/_skills/lssh/SKILL.md` | 装到用户目录的副本要重跑 `lssh skills install --yes` |
| 加测试 | `tests/test_session_local.py` 加一个用 `session` 或 `tmux_session` fixture | tmux 相关务必清理 session |
| 改打包 | `pyproject.toml` | `_skills/*` 必须在 `package-data` 里（`:41`），否则 wheel 里没有技能 |

---

## 11. 代码里的技术债与改进点（读代码时的"这里有点怪"清单）

按严重程度排。**这一节是给你练手用的**——每一条都可以做成一个 PR。

### 11.1 真实的 bug

**（1）`--connect-timeout` 从未传到守护进程** ⚠️

`ensure_daemon()`（`cli.py:89-104`）透传了 `--no-tmux / --tmux / --local / --idle /
--cols / --rows / --ssh-arg`，**但没有 `--connect-timeout`**。
守护进程进程里重新 `parse_args` 时该参数是 `None` → `apply_settings` 用配置文件的值（默认 15）。
所以 `lssh --connect-timeout 5 h 'cmd'` 在守护进程已存在时**完全不生效**。

实测证据（`ps` 看守护进程命令行，其中没有 `--connect-timeout`，见实验 7）：

```
python -m lssh --daemon --tmux lssh --idle 1800.0 --cols 512 --rows 50
```

修法：在 `cli.py:99` 附近加
```python
if args.connect_timeout:
    cmd += ["--connect-timeout", str(args.connect_timeout)]
```
注意这仍然只对**新启动**的守护进程有效（守护进程生命周期内参数是固定的）。
更彻底的做法是把 `connect_timeout` 放进 run 请求里，但那要求每个 host 的 Session 重建。

**（2）`_buf` 截断会破坏 `offset` 语义**

`_read_loop` 在 `len(_buf) > keep`（8MB）时执行 `_buf = _buf[-keep//2:]`（`session.py:217-218`），
而 `run()` 记的 `offset`（`session.py:270`）是基于截断前的长度。如果截断发生在一次
`run()` 的等待期间，`self._buf[offset:]` 会错位。

触发条件很苛刻（单条命令输出 > 8MB，且跨过截断点），但**静默错误**比崩溃更糟。

修法：把 `_buf` 换成一个带绝对偏移的 ring buffer，或者在截断时同步调整 `offset`。
更简单：截断时直接 `state = DIRTY` + 报错，明确失败。

**（3）`_read_loop` 的 `misses` 分支会静默结束读线程**

`session.py:205-212`：`os.read` 返回空字节但 `proc.poll()` 还是 `None` 时，重试 100 次
（2 秒）后直接 `return`。读线程死了但 `state` 还是 `READY` → 后续所有 `run()` 都会
干等到 deadline 才超时。应该置 `DIRTY`。

**（4）`daemon_alive()` 依赖 `json.dumps` 的空格**

`cli.py:81`：`b'"ok": true' in sock.recv(4096)`。任何人给 `json.dumps` 加
`separators=(",", ":")` 都会让这个判断永久为 False → 每次命令都拉起新守护进程
（被 flock 挡住，但会白等 8 秒）。应该解析 JSON。

### 11.2 一致性 / 文档漂移

**（5）`config.py` 的文档字符串和现实不符**

`config.py:1-9` 还在讲 `askpass.sh`、"密码优先取 `$LSSH_PASSWORD_CMD`，
其次读同目录的 `secrets`"。但 0.3.0 已重构为"唯一来源：`secrets` + 内置 `lssh-askpass`"，
`--init` 也**不再生成** `askpass.sh`。`paths.askpass_path()`（`paths.py:35`）成了死代码。

修法：删掉 `askpass_path()`，重写 `config.py` 的 docstring。

**（6）`askpass.py` 自己拼了一遍 secrets 路径**

`askpass.py:22` 的 `SECRETS_PATH` 与 `paths.secrets_path()` 逻辑重复。两处逻辑一旦
分叉就是"配置改了但密码读不到"这类难查的 bug。

修法：`askpass.py` 改 `from .paths import secrets_path`，把 `SECRETS_PATH` 变成
调用时计算的函数。注意测试要跟着改（`test_config.py:44` 用的是 `monkeypatch.setattr`）。

**（7）`--restart` 路径里 `ensure_daemon()` 调了两次**

`cli.py:278` 已经调过，`cli.py:299` 又调一次。无害但多余——说明这段 `if args.list or
args.stop or args.restart` 的分支结构没理干净（`list`/`stop` 各自 `return`，
`restart` 掉到末尾）。

**（8）`SESSION.connect_timeout` 与 `SSHTransport.connect_timeout` 同名不同义**

一个是"等远端 shell 就绪"（60s），一个是 `ssh -o ConnectTimeout`（15s）。
`Session` 的 `connect_timeout` 甚至没有被 `Daemon` 暴露成参数（`daemon.py:33-44` 没传）。

修法：改名。`Session.connect_timeout` → `probe_timeout`。

### 11.3 风格问题

**（9）`_verify()` 里变量 `raw` 被复用两次**（`session.py:355` 和 `:365`）。
能跑，但读起来要停一下。改名 `raw_seg` / `raw_bytes`。

**（10）`daemon._reaper()` 直接读 `session._cmd_lock`**（`daemon.py:204`）——
跨对象访问私有属性。`Session.info()` 已经把 `busy` 算好了，reaper 用
`session.info()["busy"]` 更干净（虽然多一点开销）。

**（11）`run()` 超时分支把 `_buf` 尾部塞进 `stdout`**（`session.py:315`）——
这是调试信息，混进 `Result.stdout` 语义上是错的。应该单独开一个 `Result.tail` 字段。

### 11.4 可以做的功能增强（不改架构）

| 想法 | 难度 | 涉及 |
|---|---|---|
| `--stream`（人类 opt-in 的流式） | 中 | `protocol.py` 加一种 report 格式；`CONTRIBUTING.md:25` 明确说"流式是给人类的 opt-in，不是默认" |
| `lssh --doctor`（自检：ssh 版本、tmux、socket、config 权限） | 低 | 新函数 + subcommand |
| `--list` 加远端 hostname/负载 | 低 | 需要额外命令，会打破"一条命令一个 RPC" |
| 输出落盘（`--out FILE`）而不是走 JSON | 低 | `cli.py:emit()`；解决 >5MB 的场景 |
| 每 host 并发 shell 池 | 高 | 违反 `CONTRIBUTING.md:31` 的约束，别做 |

---

## 12. 术语表

| 术语 | 含义 |
|---|---|
| **exec channel** | SSH 的 `session` 通道里"直接执行一条命令"的请求类型。`ssh host cmd` 用它。很多堡垒机禁掉 |
| **shell channel** | SSH 里"给我一个交互式 shell"的请求。`ssh host` 用它。lssh 唯一依赖的通道 |
| **pty / 伪终端** | 让子进程以为自己有终端的机制。`ssh -tt` 需要它，`stty`/`send-keys` 需要它 |
| **`-tt`** | ssh 的两层 t：第一个是"分配 tty"，第二个是"即使本地没有 tty 也强制分配" |
| **detached tmux** | `tmux new-session -d`：创建会话但不 attach。会话在后台活着，可以跨 ssh 断连保留 shell 状态 |
| **attach** | 连接到已有 tmux 会话并渲染屏幕。**lssh 永不 attach**（会丢换行/重放历史） |
| **token** | 每条命令一个 `LSSH` + 12 位 hex 的随机串，用于分帧和防串味 |
| **marker** | 独占一行的 `^<token><suffix>$`。必须整行锚定，否则会被回显误命中 |
| **外层 shell** | 远端 login shell，`ssh -tt` 直接连的那个。负责写文件/发键/轮询/回报 |
| **pane 内 shell** | tmux 里的那个 shell，真正执行用户命令。cwd/env 在这里积累 |
| **`_strikes`** | 连续超时计数器。达到 2 就把会话标记为需要重连 |
| **`DIRTY`** | 会话状态：连接不可用，但等下一句命令时懒重连（不立即重连，省一次登录） |
| **askpass** | OpenSSH 机制：没有 tty 需要密码时执行 `$SSH_ASKPASS` 拿密码 |
| **Agent Skill** | 给 AI Agent 看的说明文档（`SKILL.md` + 参考资料），放在 Agent 会读的目录里 |
| **XDG** | 桌面环境标准的目录约定（`XDG_CONFIG_HOME` / `XDG_CACHE_HOME` / `XDG_RUNTIME_DIR`） |

---

## 13. 阅读打卡清单

按顺序勾掉。**勾不上就回去读对应小节，别硬往下走。**

**第一轮：心智模型（≈40 分钟）**

- [ ] 能用一句话说清"为什么不能用 `ssh host cmd`"（第 1 章）
- [ ] 能画出进程拓扑的 5 个东西（3.1）
- [ ] 能解释"外层 shell"和"pane 内 shell"为什么要分两层（3.2）
- [ ] 能说出 `DOWN/STARTING/READY/DIRTY` 的状态转移，以及 `DIRTY` 为什么是懒重连（3.3）
- [ ] 能复述第 6 章那次 `echo hi` 的字节旅程（对照实验 3 的 `print(repr(...))`）

**第二轮：代码（≈3 小时）**

- [ ] `protocol.py`：能自己写出 `build_report_lines` 的输出格式，知道为什么 `SZ/MZ` 必须在 `S` 之前
- [ ] `protocol.py`：能解释 `^...$` + MULTILINE 为什么能区分回显和执行
- [ ] `transports.py`：能解释为什么不用 paramiko、为什么 `-tt`、为什么 `env.pop("TMUX")`
- [ ] `session.py:_probe`：能解释为什么"写成功"不等于"远端执行了"
- [ ] `session.py:run`：能说出 5 个分支各自的错误信息和它对应的远端状态
- [ ] `session.py:run`：能解释 `deadline = started + timeout + 30` 里那个 30
- [ ] `session.py:run`：能解释 `if self.tmux:` 为什么只包住 poll 那一行
- [ ] `session.py:_verify`：能解释"失败开放"（缺 marker 就返回 True）的理由
- [ ] `daemon.py`：能解释 `flock` 解决的是什么竞态（想象 8 个 agent 同时启动）
- [ ] `daemon.py`：能解释 `server.settimeout(0.5)` 是为了什么
- [ ] `cli.py`：能说出配置的四级优先级，以及为什么 argparse 的 default 必须是 None
- [ ] `askpass.py`：能解释 `rfind("@")` 而不是 `split("@")`

**第三轮：工程习惯（≈1 小时）**

- [ ] 能说出 `LocalTransport` 为什么是整个测试策略的支点
- [ ] 能指出 `test_session_local.py` 三层测试各自的职责边界
- [ ] 能指出 `commands_reference()` 为什么要在函数内导入 `cli`
- [ ] 能说出 `.lssh-install.json` 标记解决了什么问题
- [ ] 能说出至少 3 条第 11 章的技术债，并给出一条的修法

**第四轮：动手（≈1.5 小时）**

- [ ] 实验 1、2、3 跑通（尤其是 3，看到原始字节）
- [ ] 实验 5（超时 → dirty）
- [ ] 实验 7（`flock` 单实例）
- [ ] 实验 9（加 `Result.truncated`，跑通全量测试）

**全部勾完，你就是这个项目的 maintainer 了。**
