"""lssh — long-lived SSH session runner.

有些 SSH 网关（堡垒机）只实现 shell 通道，拒绝 exec 通道，`ssh host 'cmd'` 直接
被拒，scp/rsync 也随之失效；同时这类网关往往对连接频率敏感。lssh 用一条长连接
换掉每次执行一次登录，并在远端挂一个 tmux 会话保存 shell 状态。
"""

from importlib.metadata import PackageNotFoundError, version

try:  # 源码树里直接跑（未安装）时兜底
    __version__ = version("lssh-cli")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"

__all__ = ["__version__"]
