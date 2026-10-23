#!/bin/zsh
# Keep the existing VS Code profile and extensions; isolate Code 2/Insiders
# user data and Codex credentials.
set -euo pipefail
setopt null_glob

readonly PROFILE_ROOT="${VSCODE_PROFILE_ROOT:-$HOME/.vscode-isolated}"
readonly CODE1_ROOT="$PROFILE_ROOT/vscode1-portdan"
readonly CODE1_CODEX_HOME="$CODE1_ROOT/codex-home"
readonly CODE2_ROOT="$PROFILE_ROOT/vscode2-openai"
readonly CODE2_DATA="$CODE2_ROOT/user-data"
readonly CODE2_EXTENSIONS="${VSCODE_EXTENSIONS_DIR:-$HOME/.vscode/extensions}"
readonly CODE2_CODEX_HOME="$CODE2_ROOT/codex-home"
readonly INSIDERS_ROOT="$PROFILE_ROOT/insiders-portdan"
readonly INSIDERS_DATA="$INSIDERS_ROOT/user-data"
readonly INSIDERS_EXTENSIONS="${VSCODE_INSIDERS_EXTENSIONS_DIR:-$HOME/.vscode-insiders/extensions}"
readonly INSIDERS_CODEX_HOME="$INSIDERS_ROOT/codex-home"
readonly INSIDERS_CONFIG="${VSCODE_INSIDERS_CONFIG:-${0:A:h}/codex-insiders-api-key.conf}"
readonly INSIDERS_MODEL_CATALOG="${VSCODE_INSIDERS_MODEL_CATALOG:-${0:A:h}/codex-insiders-models.json}"

die() { print -u2 -- "VS Code 启动失败：$1"; exit 1; }
find_app() {
  local candidate
  for candidate in "$@"; do
    [[ -d "$candidate" ]] && { print -r -- "$candidate"; return 0; }
  done
  return 1
}

launch_isolated_app() {
  local app="$1" user_data="$2" extensions="$3" codex_home="$4" api_key="${5:-}"
  local executable
  executable="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$app/Contents/Info.plist" 2>/dev/null || true)"
  [[ -n "$executable" && -x "$app/Contents/MacOS/$executable" ]] || die "无法找到应用程序执行文件：$app"

  # The shell used by Codex can export ELECTRON_RUN_AS_NODE/VSCODE_* values.
  # Start these independent Electron instances with a clean environment;
  # otherwise the app may run as Node or attach to the existing VS Code.
  if [[ -n "$api_key" ]]; then
    env -i HOME="$HOME" USER="${USER:-}" PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
      TMPDIR="${TMPDIR:-/tmp}" LANG="${LANG:-zh_CN.UTF-8}" \
      CODEX_HOME="$codex_home" OPENAI_API_KEY="$api_key" \
      "$app/Contents/MacOS/$executable" \
      --user-data-dir "$user_data" --extensions-dir "$extensions" --locale=zh-CN --new-window >/dev/null 2>&1 &
  else
    env -i HOME="$HOME" USER="${USER:-}" PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
      TMPDIR="${TMPDIR:-/tmp}" LANG="${LANG:-zh_CN.UTF-8}" \
      CODEX_HOME="$codex_home" "$app/Contents/MacOS/$executable" \
      --user-data-dir "$user_data" --extensions-dir "$extensions" --locale=zh-CN --new-window >/dev/null 2>&1 &
  fi
}

launch_stable_app() {
  local app="$1"
  local executable
  executable="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleExecutable' "$app/Contents/Info.plist" 2>/dev/null || true)"
  [[ -n "$executable" && -x "$app/Contents/MacOS/$executable" ]] || die "无法找到应用程序执行文件：$app"

  # The Codex extension uses vscode1-codex-wrapper.command from VS Code's user
  # settings, so this process does not need provider credentials in its env.
  env -i HOME="$HOME" USER="${USER:-}" PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    TMPDIR="${TMPDIR:-/tmp}" LANG="${LANG:-zh_CN.UTF-8}" \
    "$app/Contents/MacOS/$executable" --new-window >/dev/null 2>&1 &
}

readonly STABLE_APP="$(find_app "/Applications/Visual Studio Code.app" "$HOME/Applications/Visual Studio Code.app" || true)"
[[ -n "$STABLE_APP" ]] || die "找不到 Visual Studio Code.app"
readonly CODE2_APP="$(find_app "/Applications/Visual Studio Code 2.app" "$HOME/Applications/Visual Studio Code 2.app" || true)"
[[ -n "$CODE2_APP" ]] || die "找不到 Visual Studio Code 2.app"
readonly INSIDERS_APP="$(find_app "/Applications/Visual Studio Code - Insiders.app" "$HOME/Applications/Visual Studio Code - Insiders.app" || true)"
[[ -n "$INSIDERS_APP" ]] || die "找不到 Visual Studio Code - Insiders.app"

mkdir -p "$CODE1_CODEX_HOME" "$CODE2_DATA" "$CODE2_CODEX_HOME" "$INSIDERS_DATA" "$INSIDERS_CODEX_HOME"
[[ -d "$CODE2_EXTENSIONS" ]] || die "找不到稳定版 VS Code 扩展目录：$CODE2_EXTENSIONS"
[[ -d "$INSIDERS_EXTENSIONS" ]] || die "找不到 Insiders 扩展目录：$INSIDERS_EXTENSIONS"

# VS Code 1 keeps its existing user-data directory, while its wrapper supplies
# the relay API key only to the Codex child process.
if [[ ! -e "$CODE1_CODEX_HOME/config.toml" ]]; then
  cat > "$CODE1_CODEX_HOME/config.toml" <<'EOF'
model_provider = "VSCode1Relay"
model = "gpt-5.6-sol"
model_reasoning_effort = "high"

[model_providers.VSCode1Relay]
name = "VS Code 1 Relay"
base_url = "https://new.sharedchat.cc/codex"
wire_api = "responses"
env_key = "OPENAI_API_KEY"
EOF
  chmod 600 "$CODE1_CODEX_HOME/config.toml"
fi

# Reuse an existing official OAuth login only when Code 2 has no auth file.
# API-key logins and third-party endpoints are deliberately not copied.
if [[ ! -e "$CODE2_CODEX_HOME/auth.json" && -r "$HOME/.codex/auth.json" ]]; then
  auth_mode="$(jq -r '.auth_mode // empty' "$HOME/.codex/auth.json" 2>/dev/null || true)"
  if [[ "$auth_mode" == "chatgpt" || "$auth_mode" == "oauth" ]]; then
    cp "$HOME/.codex/auth.json" "$CODE2_CODEX_HOME/auth.json"
    chmod 600 "$CODE2_CODEX_HOME/auth.json"
  fi
fi

# Omitting base_url/API key selects the official OpenAI provider. Code 2 will
# show the normal account sign-in flow if it is not authenticated yet.
cat > "$CODE2_CODEX_HOME/config.toml" <<'EOF'
model_provider = "OpenAI"
model = "gpt-5.6-sol"
model_reasoning_effort = "low"

[model_providers.OpenAI]
name = "OpenAI"
wire_api = "responses"
requires_openai_auth = true
EOF
chmod 600 "$CODE2_CODEX_HOME/config.toml"

[[ -r "$INSIDERS_CONFIG" ]] || die "找不到 Insiders 独立 API Key 配置：$INSIDERS_CONFIG"
[[ -r "$INSIDERS_MODEL_CATALOG" ]] || die "找不到 Insiders 模型目录：$INSIDERS_MODEL_CATALOG"
source "$INSIDERS_CONFIG"
[[ -n "${OPENAI_API_KEY:-}" ]] || die "Insiders 独立 API Key 为空"
readonly INSIDERS_MODEL="${OPENAI_MODEL:-deepseek-v4-flash}"
case "$INSIDERS_MODEL" in
  deepseek-v4-flash|deepseek-v4-pro) ;;
  *) die "Insiders 只允许 deepseek-v4-flash 或 deepseek-v4-pro，当前为：$INSIDERS_MODEL" ;;
esac
cp "$INSIDERS_MODEL_CATALOG" "$INSIDERS_CODEX_HOME/models.json"
cat > "$INSIDERS_CODEX_HOME/config.toml" <<EOF
model_provider = "Portdan"
model = "${INSIDERS_MODEL}"
model_reasoning_effort = "low"
model_catalog_json = "${INSIDERS_CODEX_HOME}/models.json"

[model_providers.Portdan]
name = "Portdan"
base_url = "https://portdan.com"
wire_api = "responses"
requires_openai_auth = true
EOF
chmod 600 "$INSIDERS_CODEX_HOME/config.toml"

if [[ ! -e "$CODE2_CODEX_HOME/auth.json" ]]; then
  print -- "提示：Code 2 尚未有官方 OpenAI 登录凭据。首次登录可执行："
  print -- "  CODEX_HOME='$CODE2_CODEX_HOME' codex login --device-auth"
fi

launch_stable_app "$STABLE_APP"
launch_isolated_app "$CODE2_APP" "$CODE2_DATA" "$CODE2_EXTENSIONS" "$CODE2_CODEX_HOME"
launch_isolated_app "$INSIDERS_APP" "$INSIDERS_DATA" "$INSIDERS_EXTENSIONS" "$INSIDERS_CODEX_HOME" "$OPENAI_API_KEY"

disown 2>/dev/null || true
print -- "已启动稳定版 VS Code（原用户数据/登录 + 独立中转站配置）、VS Code 2（官方 OpenAI 账户）和 Insiders（DeepSeek V4 Flash/Pro）。"
