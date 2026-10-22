#!/bin/sh
# lssh 的 SSH_ASKPASS 脚本示例。
#
# 约定：密码放在 ~/.ssh/config 的注释行里，格式
#     # lssh-pw <主机/IP> <密码>
# 用 "*" 作为主机可以匹配任意主机。脚本只输出匹配到的密码，不写日志、不落盘。
#
# 安装：
#     cp examples/lssh-askpass.sh ~/.ssh/lssh-askpass.sh
#     chmod 700 ~/.ssh/lssh-askpass.sh
# lssh 会自动探测 ~/.ssh/lssh-askpass.sh，无需额外参数。
set -u
prompt="${1:-}"
host=$(printf '%s' "$prompt" | sed -n "s/.*@\([^ ']*\).*/\1/p")
[ -z "$host" ] && exit 1
pw=$(awk -v h="$host" '
  /^# lssh-pw / {
    if ($3 == h) { print $4; found = 1; exit }
    if ($3 == "*" && !fallback) { fallback = $4 }
  }
  END { if (!found && fallback != "") print fallback }
' "$HOME/.ssh/config")
[ -z "$pw" ] && exit 1          # 没匹配到就别给空密码，避免误试
printf '%s\n' "$pw"
