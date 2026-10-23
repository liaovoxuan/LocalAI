"""Third-party plugin support for the CloudAI desktop client.

Plugins are deliberately small Python packages loaded from user-owned
directories.  Loading is isolated per plugin and every Agent tool call is
still confirmed by the existing CloudAI Agent safety dialog.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import os
import re
import sys
import traceback
from pathlib import Path


PLUGIN_API_VERSION = 1
PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
PLUGIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def get_cloudai_plugin_dirs(app_data_dir, base_dir=None):
    """Return plugin roots in priority order, without duplicate paths."""
    candidates = []
    environment = os.environ.get("CLOUDAI_PLUGIN_PATH", "")
    candidates.extend(item for item in environment.split(os.pathsep) if item.strip())
    if app_data_dir:
        candidates.append(os.path.join(app_data_dir, "plugins"))
    if base_dir:
        candidates.append(os.path.join(base_dir, "plugins"))
    if getattr(sys, "_MEIPASS", ""):
        candidates.append(os.path.join(sys._MEIPASS, "plugins"))
    result = []
    seen = set()
    for item in candidates:
        try:
            path = str(Path(item).expanduser().resolve())
        except (OSError, RuntimeError, TypeError):
            continue
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result


def _call_plugin_callback(callback, arguments, context):
    """Call a callback with the documented two-argument API.

    One-argument and zero-argument callbacks are accepted as a small
    compatibility convenience for early community plugins.
    """
    try:
        signature = inspect.signature(callback)
    except (TypeError, ValueError):
        return callback(arguments, context)
    positional = [
        parameter for parameter in signature.parameters.values()
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
    ]
    accepts_varargs = any(parameter.kind == parameter.VAR_POSITIONAL for parameter in signature.parameters.values())
    if accepts_varargs or len(positional) >= 2:
        return callback(arguments, context)
    if len(positional) == 1:
        return callback(arguments)
    return callback()


class CloudAIPluginContext:
    """Runtime context passed to a plugin callback."""

    def __init__(self, app, manifest, plugin_dir):
        self.app = app
        self.manifest = dict(manifest)
        self.plugin_dir = str(plugin_dir)

    @property
    def plugin_id(self):
        return str(self.manifest.get("id", ""))

    def notify(self, message, error=False):
        """Show a message through CloudAI's UI thread when available."""
        handler = getattr(self.app, "show_plugin_notice", None)
        if callable(handler):
            return handler(str(message), error=bool(error))
        return None

    def open_plugin_folder(self):
        handler = getattr(self.app, "open_path_in_file_manager", None)
        if callable(handler):
            return handler(self.plugin_dir)
        return False


class CloudAIPluginAPI:
    """Registration API exposed to one plugin only."""

    def __init__(self, host, manifest, plugin_dir):
        self._host = host
        self.manifest = dict(manifest)
        self.plugin_dir = str(plugin_dir)
        self.context = CloudAIPluginContext(host.app, self.manifest, plugin_dir)

    def register_tool(self, name, callback, description="", parameters=None):
        """Register an Agent tool.

        The callback receives ``(arguments, context)`` and should return a
        JSON-serializable value.  ``parameters`` is an OpenAI-compatible JSON
        Schema object for the tool arguments.
        """
        return self._host._register_tool(
            self.manifest,
            self.context,
            name,
            callback,
            description,
            parameters,
        )

    def register_action(self, label, callback, description=""):
        """Register a button displayed under CloudAI Settings > Tools."""
        return self._host._register_action(
            self.manifest,
            self.context,
            label,
            callback,
            description,
        )

    # Short aliases make small plugins pleasant to read while the explicit
    # names above remain the documented and stable API.
    tool = register_tool
    action = register_action


class CloudAIPluginHost:
    """Discover, validate and run third-party CloudAI plugins."""

    def __init__(self, app, plugin_dirs=()):
        self.app = app
        self.plugin_dirs = tuple(str(item) for item in plugin_dirs if item)
        self.records = []
        self.tools = {}
        self.actions = []
        self.load()

    def load(self):
        for root in self.plugin_dirs:
            try:
                entries = sorted(Path(root).iterdir(), key=lambda item: item.name.lower())
            except (OSError, RuntimeError):
                continue
            for plugin_dir in entries:
                if not plugin_dir.is_dir() or plugin_dir.name.startswith("."):
                    continue
                self._load_directory(plugin_dir)

    def _record(self, manifest, plugin_dir, status, error=""):
        self.records.append({
            "id": manifest.get("id", plugin_dir.name),
            "name": manifest.get("name", manifest.get("id", plugin_dir.name)),
            "version": manifest.get("version", ""),
            "path": str(plugin_dir),
            "status": status,
            "error": str(error or ""),
        })

    def _load_directory(self, plugin_dir):
        manifest_path = plugin_dir / "plugin.json"
        manifest = {"id": plugin_dir.name, "name": plugin_dir.name}
        try:
            with manifest_path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if not isinstance(loaded, dict):
                raise ValueError("plugin.json must contain an object")
            manifest.update(loaded)
            self._validate_manifest(manifest, plugin_dir)
            if manifest.get("enabled", True) is False:
                self._record(manifest, plugin_dir, "disabled")
                return
            entry_path = self._entry_path(manifest, plugin_dir)
            module_name = "cloudai_plugin_" + re.sub(r"[^A-Za-z0-9_]", "_", manifest["id"])
            spec = importlib.util.spec_from_file_location(module_name, entry_path)
            if spec is None or spec.loader is None:
                raise ImportError("cannot create a module loader")
            module = importlib.util.module_from_spec(spec)
            old_path = list(sys.path)
            sys.path.insert(0, str(plugin_dir))
            try:
                spec.loader.exec_module(module)
            finally:
                sys.path[:] = old_path
            register = getattr(module, "register", None)
            if not callable(register):
                register = getattr(module, "create_plugin", None)
            if not callable(register):
                raise ValueError("plugin entry must expose register(api)")
            api = CloudAIPluginAPI(self, manifest, plugin_dir)
            register(api)
            self._record(manifest, plugin_dir, "loaded")
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            self._record(manifest, plugin_dir, "error", detail)
            self._log_error(plugin_dir, detail)

    @staticmethod
    def _validate_manifest(manifest, plugin_dir):
        plugin_id = str(manifest.get("id", "")).strip()
        if not PLUGIN_ID_RE.fullmatch(plugin_id):
            raise ValueError("manifest id must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
        manifest["id"] = plugin_id
        name = str(manifest.get("name", plugin_id)).strip()
        manifest["name"] = name or plugin_id
        api_version = manifest.get("api_version", PLUGIN_API_VERSION)
        if int(api_version) != PLUGIN_API_VERSION:
            raise ValueError(f"unsupported plugin api_version: {api_version}")
        if not plugin_dir.is_dir():
            raise ValueError("plugin directory does not exist")

    @staticmethod
    def _entry_path(manifest, plugin_dir):
        entry = str(manifest.get("entry", "plugin.py")).strip() or "plugin.py"
        entry_path = (plugin_dir / entry).resolve()
        try:
            entry_path.relative_to(plugin_dir.resolve())
        except ValueError as exc:
            raise ValueError("entry must stay inside the plugin directory") from exc
        if entry_path.suffix.lower() != ".py" or not entry_path.is_file():
            raise ValueError("entry must point to an existing .py file")
        return entry_path

    @staticmethod
    def _validate_name(name):
        value = str(name or "").strip()
        if not PLUGIN_NAME_RE.fullmatch(value):
            raise ValueError("name must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
        return value

    def _register_tool(self, manifest, context, name, callback, description, parameters):
        local_name = self._validate_name(name)
        if not callable(callback):
            raise TypeError("tool callback must be callable")
        action = f"plugin.{manifest['id']}.{local_name}".lower()
        if action in self.tools:
            raise ValueError(f"duplicate tool: {action}")
        schema = parameters if isinstance(parameters, dict) else {
            "type": "object",
            "additionalProperties": True,
        }
        self.tools[action] = {
            "action": action,
            "plugin_id": manifest["id"],
            "plugin_name": manifest["name"],
            "name": local_name,
            "description": str(description or f"Tool provided by {manifest['name']}"),
            "parameters": schema,
            "callback": callback,
            "context": context,
        }
        return action

    def _register_action(self, manifest, context, label, callback, description):
        text = str(label or "").strip()
        if not text:
            raise ValueError("action label cannot be empty")
        if not callable(callback):
            raise TypeError("action callback must be callable")
        self.actions.append({
            "plugin_id": manifest["id"],
            "plugin_name": manifest["name"],
            "label": text,
            "description": str(description or ""),
            "callback": callback,
            "context": context,
        })
        return text

    def agent_tools(self):
        return [
            {
                "action": item["action"],
                "description": f"[{item['plugin_name']}] {item['description']}",
                "parameters": item["parameters"],
            }
            for item in self.tools.values()
        ]

    def invoke_tool(self, action, arguments):
        item = self.tools.get(str(action))
        if not item:
            return {"ok": False, "error": "plugin_tool_not_found", "action": str(action)}
        try:
            value = _call_plugin_callback(item["callback"], arguments if isinstance(arguments, dict) else {}, item["context"])
            return value if isinstance(value, (dict, list, str, int, float, bool)) or value is None else str(value)
        except Exception as exc:
            self._log_error(item["context"].plugin_dir, f"tool {action}: {type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"plugin_tool_error: {exc}"}

    def invoke_action(self, item):
        try:
            return _call_plugin_callback(item["callback"], {}, item["context"])
        except Exception as exc:
            self._log_error(item["context"].plugin_dir, f"action {item.get('label')}: {type(exc).__name__}: {exc}")
            item["context"].notify(f"插件操作失败：{exc}", error=True)
            return None

    @staticmethod
    def _log_error(plugin_dir, detail):
        try:
            log_path = Path(plugin_dir) / "cloudai-error.log"
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(detail + "\n")
                handle.write(traceback.format_exc() + "\n")
        except OSError:
            pass
