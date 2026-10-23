"""Controlled Agent mode shared by the CloudAI desktop client.

The model only emits a small JSON action protocol.  Every filesystem write,
command and URL launch is validated and can be confirmed by the UI.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import webbrowser
from pathlib import Path
from urllib.parse import urlparse


AGENT_MAX_REQUESTS = 12
AGENT_READ_LIMIT = 64_000
AGENT_MODEL_RESULT_LIMIT = 12_000
AGENT_WRITE_LIMIT = 2_000_000
AGENT_URL_BLOCK_HINTS = (
    "google.com", "youtube.com", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "telegram.org", "wikipedia.org", "reddit.com",
)
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml",
    ".xml", ".html", ".htm", ".rtf", ".log", ".ini", ".cfg", ".conf", ".toml",
    ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".go",
    ".rs", ".swift", ".kt", ".php", ".rb", ".sh", ".bat", ".ps1", ".sql", ".css",
}
SENSITIVE_PARTS = {".aws", ".config", ".gnupg", ".ssh", ".terraform", ".kube"}


def extract_json(text):
    """Extract the first balanced JSON object, including braces in strings."""
    if not text:
        return None
    source = str(text).strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", source, flags=re.S | re.I)
    candidates = [fenced.group(1).strip(), source] if fenced else [source]
    for candidate in candidates:
        for start, char in enumerate(candidate):
            if char != "{":
                continue
            depth = 0
            quoted = escaped = False
            for index in range(start, len(candidate)):
                current = candidate[index]
                if quoted:
                    if escaped:
                        escaped = False
                    elif current == "\\":
                        escaped = True
                    elif current == '"':
                        quoted = False
                    continue
                if current == '"':
                    quoted = True
                elif current == "{":
                    depth += 1
                elif current == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            value = json.loads(candidate[start:index + 1])
                            if isinstance(value, dict):
                                return value
                        except (TypeError, ValueError):
                            break
    return None


def _allowed_roots(extra_roots=()):
    home = Path.home()
    roots = [Path.cwd(), home / "Desktop", home / "Documents", home / "Downloads"]
    roots.extend(Path(item) for item in extra_roots if item)
    return [root.resolve() for root in roots]


def is_path_allowed(path, extra_roots=()):
    try:
        resolved = Path(os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))).resolve()
    except (OSError, RuntimeError, TypeError):
        return False
    if any(part.lower() in SENSITIVE_PARTS for part in resolved.parts):
        return False
    for root in _allowed_roots(extra_roots):
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def read_path(path, extra_roots=()):
    try:
        resolved = Path(os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))).resolve()
    except (OSError, RuntimeError, TypeError):
        return {"ok": False, "error": "invalid_path"}
    if not is_path_allowed(resolved, extra_roots):
        return {"ok": False, "error": "path_not_allowed", "path": str(resolved)}
    if not resolved.exists():
        return {"ok": False, "error": "file_not_found", "path": str(resolved)}
    if resolved.is_dir():
        try:
            items = []
            for item in sorted(resolved.iterdir(), key=lambda p: p.name.lower()):
                if item.name.startswith("."):
                    continue
                items.append(item.name + ("/" if item.is_dir() else ""))
                if len(items) >= 500:
                    break
            return {"ok": True, "path": str(resolved), "type": "directory", "truncated": len(items) >= 500, "content": "\n".join(items)}
        except OSError as exc:
            return {"ok": False, "error": str(exc), "path": str(resolved)}
    if resolved.suffix.lower() not in TEXT_EXTENSIONS:
        return {"ok": False, "error": "unsupported_or_binary_file", "path": str(resolved)}
    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
        return {"ok": True, "path": str(resolved), "type": "file", "truncated": len(text) > AGENT_READ_LIMIT, "content": text[:AGENT_READ_LIMIT]}
    except OSError as exc:
        return {"ok": False, "error": str(exc), "path": str(resolved)}


def write_path(path, content, extra_roots=()):
    try:
        resolved = Path(os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))).resolve()
    except (OSError, RuntimeError, TypeError):
        return {"ok": False, "error": "invalid_path"}
    if not is_path_allowed(resolved, extra_roots):
        return {"ok": False, "error": "path_not_allowed", "path": str(resolved)}
    text = str(content or "")
    if len(text.encode("utf-8", errors="ignore")) > AGENT_WRITE_LIMIT:
        return {"ok": False, "error": "content_too_large", "path": str(resolved)}
    resolved.parent.mkdir(parents=True, exist_ok=True)
    backup = ""
    if resolved.exists():
        backup_path = Path(str(resolved) + ".cloudai.bak")
        shutil.copy2(resolved, backup_path)
        backup = str(backup_path)
    fd, temporary = tempfile.mkstemp(prefix=".cloudai-agent-", dir=str(resolved.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, resolved)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"ok": True, "path": str(resolved), "backup": backup}


def run_command(command):
    if isinstance(command, list):
        argv = [str(item) for item in command if str(item)]
    else:
        try:
            argv = shlex.split(str(command or ""))
        except ValueError as exc:
            return {"ok": False, "error": f"invalid_command:{exc}"}
    if not argv:
        return {"ok": False, "error": "empty_command"}
    executable = os.path.basename(argv[0]).lower()
    blocked = {"rm", "rmdir", "del", "erase", "format", "mkfs", "shutdown", "reboot", "halt", "sudo", "su", "chmod", "chown"}
    if executable in blocked or executable in {"sh", "bash", "zsh", "fish", "cmd", "powershell", "pwsh"}:
        return {"ok": False, "error": "command_not_allowed", "command": argv}
    try:
        process = subprocess.run(argv, cwd=str(Path.cwd()), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=60)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "command_timeout", "command": argv}
    except OSError as exc:
        return {"ok": False, "error": str(exc), "command": argv}
    return {"ok": process.returncode == 0, "command": argv, "exit_code": process.returncode, "stdout": process.stdout[:AGENT_READ_LIMIT], "stderr": process.stderr[:AGENT_READ_LIMIT]}


def open_url(url, proxy_enabled=False):
    value = str(url or "").strip()
    if not value:
        return {"ok": False, "error": "empty_url"}
    if not re.match(r"^https?://", value, flags=re.I):
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return {"ok": False, "error": "invalid_url", "url": value}
    host = parsed.hostname or ""
    if not proxy_enabled and any(host == item or host.endswith("." + item) for item in AGENT_URL_BLOCK_HINTS):
        return {"ok": False, "error": "blocked_url", "url": value}
    webbrowser.open(value)
    return {"ok": True, "url": value}


def result_for_model(result):
    rendered = json.dumps(result if isinstance(result, dict) else str(result), ensure_ascii=False)
    return rendered[:AGENT_MODEL_RESULT_LIMIT] + ("\n...[result shortened]" if len(rendered) > AGENT_MODEL_RESULT_LIMIT else "")


class CloudAgentSession:
    def __init__(self, model_call, config, messages=(), confirm_callback=None, search_callback=None,
                 extra_roots=(), extra_tools=(), extra_tool_executor=None):
        self.model_call = model_call
        self.config = config or {}
        self.messages = list(messages or ())
        self.confirm_callback = confirm_callback
        self.search_callback = search_callback
        self.extra_roots = tuple(extra_roots or ())
        self.extra_tools = [item for item in (extra_tools or ()) if isinstance(item, dict)]
        self.extra_tool_executor = extra_tool_executor
        self.request_count = 0

    def system_prompt(self, request):
        language = self.config.get("language", "zh_cn")
        language_name = "Simplified Chinese" if str(language).startswith("zh") else "the user's language"
        plugin_help = ""
        if self.extra_tools:
            plugin_lines = []
            for item in self.extra_tools:
                action = str(item.get("action", "")).strip()
                if not action:
                    continue
                schema = json.dumps(item.get("parameters", {"type": "object"}), ensure_ascii=False)
                plugin_lines.append(f'{{"action":"{action}","arguments":{{}}}}  # {item.get("description", "")}; schema: {schema}'.strip())
            if plugin_lines:
                plugin_help = "\nThird-party plugin actions (arguments must follow the listed schema):\n" + "\n".join(plugin_lines) + "\n"
        return ("You are CloudAI Agent Mode. Reply only with one JSON object.\n"
                '{"action":"read_file","path":"/path"}\n'
                '{"action":"write_file","path":"/path","content":"text"}\n'
                '{"action":"run_command","command":["program","arg"]}\n'
                '{"action":"open_url","url":"https://example.com"}\n'
                '{"action":"search_web","query":"keywords"}\n'
                '{"action":"final","content":"answer"}\n\n'
                + plugin_help +
                f"Use {language_name} for final content. Read before editing unknown files. Never delete data. "
                "Actions are controlled and may be rejected. Continue the original task after each action.\n\n"
                f"User request:\n{request}").strip()

    def allowed(self, action, payload):
        if action in {"write_file", "run_command", "open_url"} or action.startswith("plugin."):
            return bool(self.confirm_callback and self.confirm_callback(action, payload))
        if action in {"read_file", "search_web"}:
            return not self.config.get("agent_confirm_actions", False) or bool(self.confirm_callback and self.confirm_callback(action, payload))
        return False

    def execute(self, payload):
        action = str(payload.get("action", "")).strip().lower()
        if action == "final":
            return {"ok": True, "final": str(payload.get("content", ""))}
        if not action:
            return {"ok": False, "error": "missing_action"}
        if not self.allowed(action, payload):
            return {"ok": False, "error": "action_rejected", "action": action}
        if action == "read_file":
            return read_path(payload.get("path", ""), self.extra_roots)
        if action == "write_file":
            return write_path(payload.get("path", ""), payload.get("content", ""), self.extra_roots)
        if action == "run_command":
            return run_command(payload.get("command", ""))
        if action == "open_url":
            proxy = any(os.environ.get(key) for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"))
            return open_url(payload.get("url", ""), proxy)
        if action == "search_web":
            if not self.search_callback:
                return {"ok": False, "error": "search_unavailable"}
            try:
                return self.search_callback(str(payload.get("query", "")))
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        if action.startswith("plugin."):
            if not self.extra_tool_executor:
                return {"ok": False, "error": "plugin_tools_unavailable", "action": action}
            try:
                result = self.extra_tool_executor(action, payload.get("arguments", {}))
                # Keep the Agent loop's action-result contract object-shaped
                # while allowing plugins to return any JSON value.
                return result if isinstance(result, dict) else {"ok": True, "result": result}
            except Exception as exc:
                return {"ok": False, "error": f"plugin_tool_error:{exc}", "action": action}
        return {"ok": False, "error": f"unknown_action:{action}"}

    def run(self, user_request):
        history = []
        for item in self.messages[-10:]:
            if isinstance(item, dict) and item.get("role") in {"user", "assistant", "ai"}:
                history.append({"role": "assistant" if item.get("role") == "ai" else item.get("role"), "content": str(item.get("content", ""))})
        prompt = [{"role": "system", "content": self.system_prompt(user_request)}, *history, {"role": "user", "content": user_request}]
        transcript = []
        final = ""
        for _ in range(AGENT_MAX_REQUESTS):
            self.request_count += 1
            answer = self.model_call(prompt)
            payload = extract_json(answer)
            if not payload:
                final = str(answer or "")
                break
            result = self.execute(payload)
            transcript.append(result)
            if result.get("final") is not None:
                final = result.get("final") or ""
                break
            prompt.extend([
                {"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)},
                {"role": "user", "content": f"Action result: {result_for_model(result)}\nContinue the original task and return JSON."},
            ])
        else:
            final = f"Agent reached the {AGENT_MAX_REQUESTS}-step safety limit."
        if transcript:
            final = (final or "Agent completed the requested actions.") + f"\n\nAgent steps: {len(transcript)}"
        return final or "CloudAI returned an empty answer."
