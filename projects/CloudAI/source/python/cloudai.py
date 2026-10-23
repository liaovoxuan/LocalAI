"""CloudAI headless Python implementation.

The public behavior mirrors the sibling cpp implementation: provider configuration, API-key
storage, OpenAI-compatible chat completions, text-file context, moderation and
usage probing.  The module uses only Python's standard library.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlparse

APP_NAME = "CloudAI"
APP_VERSION = "v1.0.1"
MASK = "********"
SECRET_PREFIX = "v1:"
SECRET_SALT = b"CloudAI"
MAX_FILE_BYTES = 256 * 1024
MAX_FILE_CHARS = 12_000
AGENT_MAX_REQUESTS = 12
AGENT_READ_LIMIT = 64_000
AGENT_MODEL_RESULT_LIMIT = 12_000
AGENT_WRITE_LIMIT = 2_000_000
AGENT_SENSITIVE_PARTS = {".aws", ".config", ".gnupg", ".ssh", ".terraform", ".kube"}
AGENT_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml", ".html", ".htm", ".rtf", ".log", ".ini", ".cfg", ".conf", ".toml", ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".go", ".rs", ".swift", ".kt", ".php", ".rb", ".sh", ".bat", ".ps1", ".sql", ".css",
}


@dataclass(frozen=True)
class ProviderInfo:
    code: str
    name: str
    base_url: str
    models: tuple[str, ...]


@dataclass
class ProviderConfig:
    enabled: bool = False
    base_url: str = ""
    model: str = ""


@dataclass
class CloudConfig:
    first_run_completed: bool = False
    provider: str = "openai_official"
    default_model: str = ""
    providers: dict[str, ProviderConfig] = field(default_factory=dict)


@dataclass
class LocalConfig:
    language: str = "zh_cn"
    theme: str = "auto"


@dataclass
class HttpResponse:
    status_code: int
    body: str


@dataclass
class HttpRequest:
    method: str
    url: str
    headers: dict[str, str]
    body: str = ""
    timeout_seconds: int = 60


def default_providers() -> dict[str, ProviderInfo]:
    return {
        "openai_official": ProviderInfo("openai_official", "OpenAI", "https://api.openai.com/v1", ("gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini")),
        "openai_compatible": ProviderInfo("openai_compatible", "OpenAI Compatible", "http://localhost:8000/v1", ("gpt-4.1-mini", "qwen-plus", "deepseek-chat")),
        "openrouter": ProviderInfo("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", ("openai/gpt-4o-mini", "anthropic/claude-3.5-sonnet")),
        "deepseek": ProviderInfo("deepseek", "DeepSeek", "https://api.deepseek.com/v1", ("deepseek-chat", "deepseek-reasoner")),
        "siliconflow": ProviderInfo("siliconflow", "SiliconFlow", "https://api.siliconflow.cn/v1", ("Qwen/Qwen2.5-7B-Instruct", "deepseek-ai/DeepSeek-V3")),
        "custom": ProviderInfo("custom", "Custom OpenAI-compatible", "", ("custom-model",)),
    }


def default_cloud_config() -> CloudConfig:
    providers = default_providers()
    return CloudConfig(
        default_model=providers["openai_official"].models[0],
        providers={
            code: ProviderConfig(code == "openai_official", info.base_url, info.models[0])
            for code, info in providers.items()
        },
    )


def normalize_language(language: str) -> str:
    value = str(language or "").strip().lower().replace("_", "-")
    aliases = {
        "zh-cn": "zh_cn", "zh": "zh_cn", "zh-hans": "zh_cn", "zh-tw": "zh_tw", "zh-hant": "zh_tw",
        "en": "en_us", "en-us": "en_us", "en-gb": "en_uk", "en-uk": "en_uk",
    }
    return aliases.get(value, value if value in {"ja", "fr", "de", "ko", "es", "it", "pt", "ru", "nl", "sv", "da", "fi", "no", "tr", "pl", "cs", "uk", "el", "ar", "mn", "th", "vi", "id", "ms", "hi"} else "zh_cn")


def mask_key(value: str) -> str:
    return MASK if value else ""


def _encode_secret(value: str) -> str:
    raw = bytes(byte ^ SECRET_SALT[i % len(SECRET_SALT)] for i, byte in enumerate(value.encode("utf-8")))
    return SECRET_PREFIX + base64.b64encode(raw).decode("ascii")


def _decode_secret(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if not text.startswith(SECRET_PREFIX):
        return text
    try:
        raw = base64.b64decode(text[len(SECRET_PREFIX):].encode("ascii"), validate=True)
        return bytes(byte ^ SECRET_SALT[i % len(SECRET_SALT)] for i, byte in enumerate(raw)).decode("utf-8")
    except (ValueError, UnicodeError):
        return ""


class ConfigStore:
    def __init__(self, app_data_dir: str | os.PathLike[str]):
        self.app_data_dir = Path(app_data_dir)

    @property
    def cloud_config_dir(self) -> Path:
        return self.app_data_dir / "config"

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_json(self, path: Path, value: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)

    def load_local_config(self) -> LocalConfig:
        value = self._read_json(self.app_data_dir / "config.json")
        return LocalConfig(normalize_language(value.get("language", "zh_cn")), str(value.get("theme", "auto") or "auto"))

    def save_local_config(self, config: LocalConfig) -> None:
        self._write_json(self.app_data_dir / "config.json", {"language": normalize_language(config.language), "theme": config.theme or "auto"})

    def _secrets(self) -> dict[str, str]:
        raw = self._read_json(self.cloud_config_dir / "cloudai_secrets.json")
        normalized = {key: _encode_secret(_decode_secret(value)) for key, value in raw.items() if key in default_providers() and _decode_secret(value)}
        if normalized != raw:
            self._write_json(self.cloud_config_dir / "cloudai_secrets.json", normalized)
        return normalized

    def get_api_key(self, provider: str) -> str:
        return _decode_secret(self._secrets().get(provider, ""))

    def set_api_key(self, provider: str, api_key: str) -> None:
        secrets = self._secrets()
        value = str(api_key or "").strip()
        if value and value != MASK:
            secrets[provider] = _encode_secret(value)
        else:
            secrets.pop(provider, None)
        self._write_json(self.cloud_config_dir / "cloudai_secrets.json", secrets)

    def configure_provider(self, provider: str, base_url: str, model: str) -> None:
        config = self.load_cloud_config()
        item = config.providers.get(provider)
        if item is None:
            raise ValueError("unknown_provider")
        base = str(base_url or "").strip()
        selected_model = str(model or "").strip()
        parsed = urlparse(base)
        if not base or parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("provider_url_required")
        if not selected_model:
            raise ValueError("provider_model_required")
        item.base_url = base
        item.model = selected_model
        item.enabled = True
        config.provider = provider
        config.default_model = selected_model
        config.first_run_completed = True
        self.save_cloud_config(config)

    def load_cloud_config(self) -> CloudConfig:
        config = default_cloud_config()
        value = self._read_json(self.cloud_config_dir / "cloudai_config.json")
        config.first_run_completed = bool(value.get("first_run_completed", config.first_run_completed))
        provider = str(value.get("provider", config.provider))
        if provider in config.providers:
            config.provider = provider
        if value.get("default_model"):
            config.default_model = str(value["default_model"])
        for code, item in config.providers.items():
            raw = value.get("providers", {}).get(code, {}) if isinstance(value.get("providers", {}), dict) else {}
            if isinstance(raw, dict):
                item.enabled = bool(raw.get("enabled", item.enabled))
                item.base_url = str(raw.get("base_url", item.base_url) or item.base_url)
                item.model = str(raw.get("model", item.model) or item.model)
        return config

    def save_cloud_config(self, config: CloudConfig) -> None:
        providers = {}
        for code, item in config.providers.items():
            providers[code] = {"enabled": bool(item.enabled), "base_url": item.base_url, "model": item.model, "api_key": mask_key(self.get_api_key(code))}
        self._write_json(self.cloud_config_dir / "cloudai_config.json", {"first_run_completed": bool(config.first_run_completed), "provider": config.provider, "default_model": config.default_model, "providers": providers})


def content_allowed(text: str) -> bool:
    normalized = re.sub(r"\s+", "", str(text or "").lower())
    blocked = ("制作炸弹", "诈骗教程", "绕过执法", "儿童色情", "terroristmanual", "makeabomb", "creditcarddump", "childsexual")
    return not any(item in normalized for item in blocked)


def moderated_text(text: str) -> str:
    return text if content_allowed(text) else "这个话题不合适，换一个聊聊吧！"


def response_text_from_json(value: Mapping[str, Any] | str) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return ""
    if not isinstance(value, Mapping):
        return ""
    for key in ("response", "text", "output", "content", "answer"):
        if isinstance(value.get(key), str):
            return value[key].strip()
    choices = value.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], Mapping) else {}
        if isinstance(first.get("text"), str):
            return first["text"].strip()
        message = first.get("message") if isinstance(first.get("message"), Mapping) else {}
        if isinstance(message.get("content"), str):
            return message["content"].strip()
    return ""


def summarize_usage_data(value: Mapping[str, Any] | str) -> str:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return ""
    if not isinstance(value, Mapping):
        return ""
    if isinstance(value.get("data"), Mapping):
        nested = summarize_usage_data(value["data"])
        if nested:
            return nested
    for key in ("total_usage", "total_credits", "balance", "total_granted", "total_used", "limit", "used", "remaining", "total"):
        if key in value:
            return f"{key}={value[key]}"
    if isinstance(value.get("data"), list):
        return f"{len(value['data'])} records"
    return ""


TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml", ".html", ".htm", ".rtf", ".log", ".ini", ".cfg", ".conf", ".toml", ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".go", ".rs", ".swift", ".kt", ".php", ".rb", ".sh", ".bat", ".ps1", ".sql", ".css"}


def _read_file(path: Path) -> str:
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return ""
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            return ""
        return path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, ValueError):
        return ""


def messages_with_files(messages: Sequence[Mapping[str, Any]], file_paths: Sequence[str | os.PathLike[str]]) -> list[dict[str, str]]:
    result = [{"role": str(item.get("role", "assistant")), "content": str(item.get("content", ""))} for item in messages]
    if not file_paths:
        return result
    if not result:
        result.append({"role": "user", "content": ""})
    context: list[str] = []
    unreadable: list[str] = []
    for raw_path in file_paths:
        path = Path(raw_path)
        content = _read_file(path)
        if content:
            if len(content) > MAX_FILE_CHARS:
                content = content[:MAX_FILE_CHARS] + "\n...[truncated]"
            context.append(f"[File: {path.name}]\n{content}")
        else:
            unreadable.append(path.name)
    suffix = ""
    if context:
        suffix += "\n\nUse the following file content to answer:\n\n" + "\n\n".join(context)
    if unreadable:
        suffix += "\n\nThe attached file(s) could not be read as text by this app: " + ", ".join(unreadable) + ". If you cannot access them directly, say that clearly."
    result[-1]["content"] = (result[-1]["content"] + suffix).strip()
    return result


def agent_extract_json(text: str) -> dict[str, Any] | None:
    """Parse a balanced JSON action object from model prose or a markdown fence."""
    source = str(text or "").strip()
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
                            return value if isinstance(value, dict) else None
                        except (TypeError, ValueError):
                            break
    return None


def _agent_path_allowed(path: str | os.PathLike[str], extra_roots: Sequence[str | os.PathLike[str]] = ()) -> Path | None:
    try:
        resolved = Path(os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))).resolve()
        if any(part.lower() in AGENT_SENSITIVE_PARTS for part in resolved.parts):
            return None
        roots = [Path.cwd(), Path.home() / "Desktop", Path.home() / "Documents", Path.home() / "Downloads"]
        roots.extend(Path(item) for item in extra_roots if item)
        for root in roots:
            try:
                resolved.relative_to(root.resolve())
                return resolved
            except ValueError:
                continue
    except (OSError, RuntimeError, TypeError):
        pass
    return None


def agent_read_path(path: str, extra_roots=()) -> dict[str, Any]:
    resolved = _agent_path_allowed(path, extra_roots)
    if resolved is None:
        return {"ok": False, "error": "path_not_allowed"}
    if not resolved.exists():
        return {"ok": False, "error": "file_not_found", "path": str(resolved)}
    if resolved.is_dir():
        items = [item.name + ("/" if item.is_dir() else "") for item in sorted(resolved.iterdir(), key=lambda p: p.name.lower()) if not item.name.startswith(".")][:500]
        return {"ok": True, "path": str(resolved), "type": "directory", "content": "\n".join(items), "truncated": len(items) >= 500}
    if resolved.suffix.lower() not in AGENT_TEXT_EXTENSIONS:
        return {"ok": False, "error": "unsupported_or_binary_file", "path": str(resolved)}
    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
        return {"ok": True, "path": str(resolved), "type": "file", "content": content[:AGENT_READ_LIMIT], "truncated": len(content) > AGENT_READ_LIMIT}
    except OSError as exc:
        return {"ok": False, "error": str(exc), "path": str(resolved)}


def agent_write_path(path: str, content: str, extra_roots=()) -> dict[str, Any]:
    resolved = _agent_path_allowed(path, extra_roots)
    if resolved is None:
        return {"ok": False, "error": "path_not_allowed"}
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


def agent_run_command(command: Any) -> dict[str, Any]:
    try:
        argv = [str(item) for item in command if str(item)] if isinstance(command, list) else shlex.split(str(command or ""))
    except ValueError as exc:
        return {"ok": False, "error": f"invalid_command:{exc}"}
    if not argv:
        return {"ok": False, "error": "empty_command"}
    executable = os.path.basename(argv[0]).lower()
    blocked = {"rm", "rmdir", "del", "erase", "format", "mkfs", "shutdown", "reboot", "halt", "sudo", "su", "chmod", "chown", "sh", "bash", "zsh", "fish", "cmd", "powershell", "pwsh"}
    if executable in blocked:
        return {"ok": False, "error": "command_not_allowed", "command": argv}
    try:
        process = subprocess.run(argv, cwd=str(Path.cwd()), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", timeout=60)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "command_timeout", "command": argv}
    except OSError as exc:
        return {"ok": False, "error": str(exc), "command": argv}
    return {"ok": process.returncode == 0, "command": argv, "exit_code": process.returncode, "stdout": process.stdout[:AGENT_READ_LIMIT], "stderr": process.stderr[:AGENT_READ_LIMIT]}


def agent_open_url(url: str) -> dict[str, Any]:
    value = str(url or "").strip()
    if not value:
        return {"ok": False, "error": "empty_url"}
    if not re.match(r"^https?://", value, flags=re.I):
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return {"ok": False, "error": "invalid_url", "url": value}
    webbrowser.open(value)
    return {"ok": True, "url": value}


class CloudAIAgentSession:
    """Headless counterpart of the desktop AgentSession."""
    def __init__(self, client: "CloudAIClient", messages=(), confirm_callback=None, extra_roots=(), search_callback=None):
        self.client = client
        self.messages = list(messages or ())
        self.confirm_callback = confirm_callback
        self.extra_roots = tuple(extra_roots or ())
        self.search_callback = search_callback

    def _allowed(self, action: str, payload: Mapping[str, Any]) -> bool:
        if action in {"read_file", "search_web"} and not self.confirm_callback:
            return True
        return bool(self.confirm_callback and self.confirm_callback(action, payload))

    def _execute(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action", "")).strip().lower()
        if action == "final":
            return {"ok": True, "final": str(payload.get("content", ""))}
        if action not in {"read_file", "write_file", "run_command", "open_url", "search_web"}:
            return {"ok": False, "error": "unsupported_action", "action": action}
        if not self._allowed(action, payload):
            return {"ok": False, "error": "action_rejected", "action": action}
        if action == "read_file":
            return agent_read_path(str(payload.get("path", "")), self.extra_roots)
        if action == "write_file":
            return agent_write_path(str(payload.get("path", "")), str(payload.get("content", "")), self.extra_roots)
        if action == "run_command":
            return agent_run_command(payload.get("command", ""))
        if action == "open_url":
            return agent_open_url(str(payload.get("url", "")))
        if not self.search_callback:
            return {"ok": False, "error": "search_unavailable"}
        return self.search_callback(str(payload.get("query", "")))

    def run(self, user_request: str) -> str:
        config = self.client.store.load_local_config()
        language = "Simplified Chinese" if config.language.startswith("zh") else "the user's language"
        system = ("You are CloudAI Agent Mode. Reply only with JSON actions: "
                  '{"action":"read_file","path":"/path"}, '
                  '{"action":"write_file","path":"/path","content":"text"}, '
                  '{"action":"run_command","command":["program","arg"]}, '
                  '{"action":"open_url","url":"https://example.com"}, '
                  '{"action":"search_web","query":"keywords"}, '
                  '{"action":"final","content":"answer"}. '
                  f"Use {language}; read before editing, never delete data.\n\nTask:\n{user_request}")
        history = [{"role": "assistant" if item.get("role") == "ai" else item.get("role", "user"), "content": str(item.get("content", ""))} for item in self.messages[-10:] if isinstance(item, Mapping) and item.get("role") in {"user", "assistant", "ai"}]
        prompt = [{"role": "system", "content": system}, *history, {"role": "user", "content": user_request}]
        transcript = []
        for _ in range(AGENT_MAX_REQUESTS):
            answer = self.client.ask(prompt)
            payload = agent_extract_json(answer)
            if not payload:
                final = answer
                break
            result = self._execute(payload)
            transcript.append(result)
            if result.get("final") is not None:
                final = result.get("final", "")
                break
            prompt.extend([{"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)}, {"role": "user", "content": "Action result: " + json.dumps(result, ensure_ascii=False)[:AGENT_MODEL_RESULT_LIMIT] + "\nContinue the task and return JSON."}])
        else:
            final = f"Agent reached the {AGENT_MAX_REQUESTS}-step safety limit."
        return (final or "CloudAI returned an empty answer.") + (f"\n\nAgent steps: {len(transcript)}" if transcript else "")


def openai_chat_url(base_url: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        return ""
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


def _urllib_http(request: HttpRequest) -> HttpResponse:
    body = request.body.encode("utf-8") if request.body else None
    req = urllib.request.Request(request.url, data=body, headers=request.headers, method=request.method)
    try:
        with urllib.request.urlopen(req, timeout=request.timeout_seconds) as response:
            return HttpResponse(response.status, response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as error:
        return HttpResponse(error.code, error.read().decode("utf-8", errors="replace"))


class CloudAIClient:
    def __init__(self, store: ConfigStore, http_client: Callable[[HttpRequest], HttpResponse] | None = None):
        self.store = store
        self.http_client = http_client or _urllib_http

    def ask(self, messages: Sequence[Mapping[str, Any]], file_paths: Sequence[str | os.PathLike[str]] = ()) -> str:
        combined = "\n".join(f"{item.get('role', 'assistant')}: {item.get('content', '')}" for item in messages)
        if not content_allowed(combined):
            return "这个话题不合适，换一个聊聊吧！"
        config = self.store.load_cloud_config()
        provider = config.providers.get(config.provider)
        if provider is None:
            raise RuntimeError("invalid_provider_config")
        api_key = self.store.get_api_key(config.provider)
        if not api_key:
            raise PermissionError("missing_api_key")
        model = provider.model or config.default_model or default_providers()[config.provider].models[0]
        url = openai_chat_url(provider.base_url)
        if not model or not url:
            raise RuntimeError("invalid_provider_config")
        payload = {"model": model, "temperature": 0.7, "stream": False, "messages": messages_with_files(messages, file_paths)}
        response = self.http_client(HttpRequest("POST", url, {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}, json.dumps(payload, ensure_ascii=False), 180))
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"http_error_{response.status_code}")
        return moderated_text(response_text_from_json(response.body))

    def run_agent(self, user_request: str, messages: Sequence[Mapping[str, Any]] = (), confirm_callback=None, extra_roots=(), search_callback=None) -> str:
        """Run the same bounded JSON-action Agent protocol as the desktop client."""
        return CloudAIAgentSession(self, messages, confirm_callback, extra_roots, search_callback).run(user_request)

    def fetch_usage(self) -> str:
        config = self.store.load_cloud_config()
        api_key = self.store.get_api_key(config.provider)
        if not api_key:
            raise PermissionError("missing_api_key")
        provider = config.providers.get(config.provider) or ProviderConfig()
        base = provider.base_url.rstrip("/")
        if base.endswith("/chat/completions"):
            base = base[:-17]
        urls = {
            "openai_official": ["https://api.openai.com/v1/dashboard/billing/usage", "https://api.openai.com/v1/usage"],
            "openrouter": ["https://openrouter.ai/api/v1/credits"],
            "deepseek": ["https://api.deepseek.com/user/balance"],
            "siliconflow": ["https://api.siliconflow.cn/v1/user/info", "https://api.siliconflow.cn/v1/user/balance"],
        }.get(config.provider, []) + ([base + suffix for suffix in ("/usage", "/dashboard/billing/usage", "/user/usage")] if base else [])
        for url in dict.fromkeys(urls):
            response = self.http_client(HttpRequest("GET", url, {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}, "", 20))
            if response.status_code in (404, 405):
                continue
            if 200 <= response.status_code < 300:
                summary = summarize_usage_data(response.body)
                if summary:
                    return summary
        return ""


def default_app_data_dir() -> Path:
    override = os.environ.get("CLOUDAI_DATA_DIR")
    if override:
        return Path(override)
    return Path.home() / ".local" / "share" / APP_NAME


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    store = ConfigStore(args[0] if args else default_app_data_dir())
    config = store.load_cloud_config()
    if os.environ.get("CLOUDAI_API_KEY"):
        store.set_api_key(config.provider, os.environ["CLOUDAI_API_KEY"])
    print(f"CloudAI {APP_VERSION} Python core")
    print(f"Provider: {config.provider}\nModel: {config.providers[config.provider].model}")
    print("Type a message. /exit to quit.")
    for line in sys.stdin:
        line = line.rstrip("\n")
        if line == "/exit":
            break
        if not line:
            continue
        try:
            print(CloudAIClient(store).ask(({"role": "user", "content": line},)))
        except PermissionError:
            print("当前 Provider 尚未配置 API Key，请前往设置填写。")
        except Exception as error:
            print(f"CloudAI error: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
