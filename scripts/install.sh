#!/usr/bin/env sh
# lssh one-line installer (macOS / Linux / WSL):
#   curl -fsSL https://raw.githubusercontent.com/colorful-lollipop/lssh/main/scripts/install.sh | sh
#
# Installs the `lssh` command, then copies the Agent Skill into every AI agent
# already present on this machine. Set LSSH_NO_SKILLS=1 to skip that step.
set -eu

# Once the package is on PyPI, the default can become `lssh-cli`.
SOURCE="${LSSH_SOURCE:-git+https://github.com/colorful-lollipop/lssh}"

say() { printf '\033[1;36m==>\033[0m %s\n' "$1"; }

if command -v uv >/dev/null 2>&1; then
  say "Installing lssh with uv"
  uv tool install --force "$SOURCE"
elif command -v pipx >/dev/null 2>&1; then
  say "Installing lssh with pipx"
  pipx install --force "$SOURCE"
else
  say "uv not found — installing uv first (https://docs.astral.sh/uv/)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  PATH="$HOME/.local/bin:$PATH"
  export PATH
  uv tool install --force "$SOURCE"
fi

PATH="$HOME/.local/bin:$PATH"
export PATH
if ! command -v lssh >/dev/null 2>&1; then
  echo "lssh was installed but is not on PATH. Run 'uv tool update-shell', then open a new terminal." >&2
  exit 1
fi

lssh --version
lssh --init

if [ "${LSSH_NO_SKILLS:-0}" != "1" ]; then
  say "Installing the lssh skill into your AI agents"
  lssh skills install --yes
fi

say "Done. Next: put passwords in ~/.config/lssh/secrets, then run 'lssh <host> uptime'."
