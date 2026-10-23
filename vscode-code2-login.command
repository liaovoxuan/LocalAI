#!/bin/zsh
# Authenticate Code 2 with an official OpenAI account, without touching the
# stable VS Code/Insiders credentials.
set -euo pipefail

readonly SCRIPT_DIR="${0:A:h}"
readonly CODE2_HOME="${VSCODE_PROFILE_ROOT:-$HOME/.vscode-isolated}/vscode2-openai/codex-home"
readonly CODEX_BIN="${CODEX_BIN:-$(command -v codex || true)}"

[[ -n "$CODEX_BIN" && -x "$CODEX_BIN" ]] || {
  print -u2 -- "找不到 codex 命令，请先安装/更新 OpenAI Codex CLI。"
  exit 1
}

mkdir -p "$CODE2_HOME"
chmod 700 "$CODE2_HOME"
print -- "将为 Code 2 单独执行 OpenAI 官方设备登录。"
print -- "浏览器完成登录后，凭据只写入：$CODE2_HOME"
env -i HOME="$HOME" USER="${USER:-}" PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  CODEX_HOME="$CODE2_HOME" "$CODEX_BIN" login --device-auth
