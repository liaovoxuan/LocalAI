#!/bin/zsh
set -euo pipefail
setopt null_glob

readonly VSCODE1_CODEX_HOME="${VSCODE1_CODEX_HOME:-$HOME/.vscode-isolated/vscode1-portdan/codex-home}"
readonly VSCODE1_PROVIDER_CONFIG="${VSCODE1_PROVIDER_CONFIG:-${0:A:h}/codex-vscode2-api-key.conf}"
codex_bins=("$HOME"/.vscode/extensions/openai.chatgpt-*/bin/macos-*/codex)

[[ -r "$VSCODE1_PROVIDER_CONFIG" ]] || {
  print -u2 -- "VS Code Codex wrapper: 找不到中转站凭据：$VSCODE1_PROVIDER_CONFIG"
  exit 1
}
source "$VSCODE1_PROVIDER_CONFIG"
[[ -n "${OPENAI_API_KEY:-}" ]] || {
  print -u2 -- "VS Code Codex wrapper: 中转站 API Key 为空。"
  exit 1
}

if (( ${#codex_bins[@]} == 0 )); then
  print -u2 -- "VS Code Codex wrapper: 找不到 openai.chatgpt 扩展内置的 codex。"
  exit 1
fi

export CODEX_HOME="$VSCODE1_CODEX_HOME"
export OPENAI_API_KEY
exec "${codex_bins[-1]}" "$@"
