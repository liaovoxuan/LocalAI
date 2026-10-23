"""Host-level regression tests for LocalAI's CloudAI integration.

These tests exercise the production host module with isolated temporary data,
mocked vault/network boundaries, and a real Tk interpreter where GUI behavior is
part of the contract. They intentionally do not contact a provider or use the
user's application-data directory.
"""
from __future__ import annotations

import ast
import copy
import inspect
import queue
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import clouderrors
import cloudattachments
import cloudproviders
import cloudsecrets
import cloudusage
import local_ai


class TempHostDataMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.patches = mock.patch.multiple(
            local_ai,
            APP_DATA_DIR=str(self.root),
            CHAT_DIR=str(self.root / "chats"),
            EXPORT_DIR=str(self.root / "exports"),
            LOG_DIR=str(self.root / "logs"),
            CONFIG_FILE=str(self.root / "config.json"),
        )
        self.patches.start()

    def tearDown(self):
        self.patches.stop()
        self._tmp.cleanup()

    def canonical_config(self, **overrides):
        config = copy.deepcopy(local_ai.DEFAULT_CONFIG)
        config.update(overrides)
        local_ai.normalize_provider_config(config)
        return config


class CloudConfigPersistenceTests(TempHostDataMixin, unittest.TestCase):
    def test_mode_aliases_normalize_to_local_or_cloud(self):
        for value in ("localai", "local_ai", "offline", "on-device"):
            self.assertEqual(local_ai.normalize_mode(value), "local")
        for value in ("cloudai", "cloud_ai", "recommended", "online"):
            self.assertEqual(local_ai.normalize_mode(value), "cloud")

    def test_legacy_public_config_is_migrated_to_canonical_shape(self):
        legacy = {
            "mode": "online",
            "cloud": {
                "public": {
                    "provider": "deepseek",
                    "model": "deepseek-chat",
                    "providers": {
                        "deepseek": {
                            "base_url": "https://api.deepseek.com/v1",
                            "model": "deepseek-reasoner",
                        }
                    },
                }
            },
        }
        Path(local_ai.CONFIG_FILE).write_text(__import__("json").dumps(legacy), encoding="utf-8")
        loaded = local_ai.load_config()
        self.assertEqual(loaded["mode"], "cloud")
        self.assertEqual(loaded["cloud"]["provider"], "deepseek")
        self.assertEqual(loaded["cloud"]["default_model"], "deepseek-chat")
        self.assertNotIn("public", loaded["cloud"])
        self.assertEqual(loaded["cloud"]["providers"]["deepseek"]["model"], "deepseek-reasoner")
        persisted = __import__("json").loads(Path(local_ai.CONFIG_FILE).read_text(encoding="utf-8"))
        self.assertNotIn("public", persisted["cloud"])

    def test_recursive_redaction_removes_credentials_blobs_and_private_state(self):
        payload = {
            "api_key": "sk-top-level",
            "nested": {
                "openai_api_key": "sk-nested",
                "access_token": "token",
                "secret": "secret",
                "items": ["v1:encoded", {"api_secret": "hidden", "_request": "state"}],
            },
            "safe": "value",
        }
        clean = local_ai._config_safe_to_serialize(payload)
        rendered = str(clean)
        for secret in ("sk-top-level", "sk-nested", "v1:encoded", "hidden", "state"):
            self.assertNotIn(secret, rendered)
        self.assertEqual(clean["nested"]["access_token"], "")
        self.assertEqual(clean["nested"]["secret"], "")
        self.assertEqual(clean["safe"], "value")
        self.assertEqual(clean["nested"]["items"][1], {"api_secret": ""})

    def test_vault_write_readback_failure_does_not_clear_entered_key(self):
        with mock.patch.object(local_ai.cloudsecrets, "set_api_key"), mock.patch.object(
            local_ai.cloudsecrets, "get_api_key", return_value=""
        ):
            with self.assertRaises(RuntimeError) as caught:
                local_ai._persist_api_key("deepseek", "sk-retain-me")
        self.assertIn("retained", str(caught.exception))
        self.assertNotIn("sk-retain-me", str(caught.exception))

    def test_save_failure_keeps_callers_plaintext_and_does_not_write_json(self):
        config = self.canonical_config(
            mode="cloud",
            cloud={
                "provider": "deepseek",
                "default_model": "deepseek-chat",
                "providers": {"deepseek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"}},
            },
        )
        before = copy.deepcopy(config)
        with mock.patch.object(local_ai, "_persist_api_key", side_effect=RuntimeError("vault unavailable")):
            with self.assertRaises(RuntimeError):
                local_ai.save_config(config, cloud_key_updates={"deepseek": "sk-entered"})
        self.assertEqual(config, before)
        self.assertFalse(Path(local_ai.CONFIG_FILE).exists())

    def test_delayed_key_deletion_only_happens_at_save(self):
        config = self.canonical_config(mode="cloud")
        calls = []
        with mock.patch.object(local_ai, "save_config", side_effect=lambda cfg, cloud_key_updates=None: calls.append((cfg, cloud_key_updates))):
            # This is the same draft contract used by the cloud settings dialog.
            draft = copy.deepcopy(config)
            removed = {"deepseek"}
            key_drafts = {"deepseek": ""}
            updates = {name: value for name, value in key_drafts.items() if not cloudsecrets.is_masked_key(value) and (value or name in removed)}
            self.assertEqual(calls, [])
            local_ai.save_config(draft, cloud_key_updates=updates)
        self.assertEqual(updates, {"deepseek": ""})
        self.assertEqual(len(calls), 1)


class CloudRoutingTests(TempHostDataMixin, unittest.TestCase):
    def test_cloud_mode_routes_to_cloud_and_not_local_provider(self):
        config = self.canonical_config(mode="cloud")
        with mock.patch.object(local_ai, "ask_cloud", return_value="cloud answer") as cloud, mock.patch.object(
            local_ai, "ask_ollama", side_effect=AssertionError("local fallback must not run")
        ):
            result = local_ai.ask_model([{"role": "user", "content": "hello"}], "ignored-local", config)
        self.assertEqual(result, "cloud answer")
        cloud.assert_called_once()

    def test_cloud_error_is_not_converted_to_ollama_fallback(self):
        config = self.canonical_config(mode="cloud")
        error = clouderrors.CloudAIError(clouderrors.CLOUD_REQUEST_ERROR)
        with mock.patch.object(local_ai, "ask_cloud", side_effect=error), mock.patch.object(
            local_ai, "ask_ollama", side_effect=AssertionError("cloud errors must not fall back")
        ):
            with self.assertRaises(clouderrors.CloudAIError):
                local_ai.ask_model([], "model", config)

    def test_local_mode_dispatch_remains_ollama(self):
        config = self.canonical_config(mode="local", provider="ollama")
        with mock.patch.object(local_ai, "ask_ollama", return_value="local answer") as ollama:
            result = local_ai.ask_model([{"role": "user", "content": "hello"}], "qwen2.5:7b", config)
        self.assertEqual(result, "local answer")
        ollama.assert_called_once()

    def test_ask_cloud_uses_resolved_model_and_bounded_attachments(self):
        config = self.canonical_config(mode="cloud")
        attachment = self.root / "context.txt"
        attachment.write_text("bounded context", encoding="utf-8")
        target = cloudproviders.resolve_active_provider(config, api_key="sk-test")
        with mock.patch.object(local_ai, "resolve_cloud_target", return_value=target), mock.patch.object(
            local_ai, "ask_openai_chat_completions", return_value="answer"
        ) as request:
            result = local_ai.ask_cloud(
                [{"role": "user", "content": "summarize"}], config, [attachment], on_chunk=None
            )
        self.assertEqual(result, "answer")
        args, kwargs = request.call_args
        self.assertEqual(args[1], target.model)
        self.assertEqual(args[3], target.base_url)
        self.assertEqual(args[4], target.api_key)
        content = args[0][-1]["content"]
        rendered = str(content)
        self.assertIn("data:text/plain;base64,", rendered)
        self.assertIn("Ym91bmRlZCBjb250ZXh0", rendered)

    def test_attachment_aggregate_limit_is_honored_at_host_boundary(self):
        files = []
        for index in range(3):
            path = self.root / f"file-{index}.txt"
            path.write_text("x" * 20_000, encoding="utf-8")
            files.append(path)
        context = cloudattachments.build_file_context(files, total_limit=1_000)
        self.assertLessEqual(len(context.split("\n", 1)[1]), 1_000 + len(cloudattachments.TRUNCATION_MARKER))
        self.assertTrue(context.endswith(cloudattachments.TRUNCATION_MARKER))

    def test_native_file_aggregate_limit_adds_truncation_marker(self):
        attachment = self.root / "native.txt"
        attachment.write_bytes(b"native payload")
        result = cloudattachments.messages_with_files(
            [{"role": "user", "content": "prompt"}],
            [attachment],
            model="gpt-4o",
            total_limit=8,
        )
        blocks = result[-1]["content"]
        self.assertEqual(blocks[0], {"type": "text", "text": "prompt"})
        self.assertEqual(blocks[-1], {"type": "text", "text": cloudattachments.TRUNCATION_MARKER.strip()})
        self.assertNotIn("data:", str(blocks))


class TkHarness:
    """Extract the production ChatApp class without entering its mainloop."""

    @staticmethod
    def chat_app_class():
        source = textwrap.dedent(inspect.getsource(local_ai.run_chat_gui))
        tree = ast.parse(source)
        selected = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in {"draw_round_rect", "RoundedButton", "ChatApp"}:
                selected.append(node)
        # ``ChatApp`` is nested in run_chat_gui's try block; compile only the
        # selected definitions, preserving their production method bodies.
        namespace = vars(local_ai).copy()
        namespace.update({"tk": tk, "ttk": ttk, "filedialog": filedialog, "messagebox": messagebox})
        exec(compile(ast.Module(body=selected, type_ignores=[]), "<ChatAppTestHarness>", "exec"), namespace)
        return namespace["ChatApp"], namespace

    @staticmethod
    def app(config):
        chat_class, namespace = TkHarness.chat_app_class()
        app = chat_class.__new__(chat_class)
        tk.Tk.__init__(app)
        app.config_data = config
        app.asking = False
        app.closing = False
        app.agent_enabled = False
        app.current_model = "qwen2.5:7b"
        app.current_size = "small"
        app.streaming_text = ""
        app.streaming_update_job = None
        app.cloud_reply_queue = queue.Queue()
        app.cloud_request_snapshot = (copy.deepcopy(config), [{"role": "user", "content": "hello"}])
        # Request-token state: poll_cloud_reply/ask_in_background read active_request,
        # and request_is_current compares the live queue/chat/path/mode against it.
        app.active_request = None
        app.reply_poll_job = None
        app.scroll_remainders = {}
        app.chat_data = {"messages": [{"role": "user", "content": "hello"}, {"role": "ai", "content": "thinking"}]}
        app.current_path = "chat.json"
        app.colors = {"window": "white", "text": "black", "muted": "gray", "input": "white", "border": "gray", "surface": "white", "surface_hover": "#eee"}
        app.ui_scale = 1.0
        app.ct = lambda key, **kwargs: key
        app.child_geometry = lambda width, height: f"{width}x{height}"
        app.render_messages = lambda **kwargs: None
        app.update_send_button_state = lambda: None
        app.add_message_bubble = lambda *args, **kwargs: None
        app.after_cancel = lambda _job: None
        app.finish_ai_answer = lambda answer, request=None: setattr(app, "_finished_answer", answer)
        return app, namespace


class CloudTkIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.config_patches = mock.patch.multiple(
            local_ai,
            APP_DATA_DIR=str(self.root),
            CHAT_DIR=str(self.root / "chats"),
            EXPORT_DIR=str(self.root / "exports"),
            LOG_DIR=str(self.root / "logs"),
            CONFIG_FILE=str(self.root / "config.json"),
        )
        self.config_patches.start()
        self.app, self.namespace = TkHarness.app(copy.deepcopy(local_ai.DEFAULT_CONFIG) | {"mode": "cloud"})

    def tearDown(self):
        try:
            self.app.destroy()
        finally:
            self.config_patches.stop()
            self._tmp.cleanup()

    def test_cloud_queue_is_consumed_by_polling_on_tk_caller_thread(self):
        caller = threading.get_ident()
        observed = []
        self.app.asking = True
        self.app.append_stream_chunk = lambda chunk: observed.append(("chunk", chunk, threading.get_ident()))
        self.app.poll_cloud_reply = self.namespace["ChatApp"].poll_cloud_reply.__get__(self.app)
        self.app.after = lambda *_args: None
        request = {
            "config": copy.deepcopy(self.app.config_data),
            "messages": [{"role": "user", "content": "hello"}],
            "queue": self.app.cloud_reply_queue,
            "chat": self.app.chat_data,
            "path": self.app.current_path,
            "mode": "cloud",
            "agent": False,
            "model": self.app.current_model,
            "size": self.app.current_size,
            "direct": True,
        }
        self.app.active_request = request
        self.app.cloud_reply_queue.put(("chunk", "part"))
        self.app.cloud_reply_queue.put(("done", "final"))
        self.app.poll_cloud_reply(request)
        self.assertEqual(observed, [("chunk", "part", caller)])
        self.assertEqual(self.app._finished_answer, "final")

        # A token from a superseded request must not drain the live queue or overwrite
        # the answer that is already on screen.
        stale = dict(request, mode="local")
        self.app.cloud_reply_queue.put(("done", "stale"))
        self.app.poll_cloud_reply(stale)
        self.assertEqual(self.app._finished_answer, "final",
                         "a stale request token wrote a superseded answer")
        self.assertFalse(self.app.cloud_reply_queue.empty(),
                         "a stale request token drained the live queue")

    def test_cloud_worker_sends_chunks_and_done_to_queue_without_tk_calls(self):
        worker_thread = []
        self.app.cloud_request_snapshot = (copy.deepcopy(self.app.config_data), [{"role": "user", "content": "hello"}])
        self.app.ask_in_background = self.namespace["ChatApp"].ask_in_background.__get__(self.app)
        self.app.after = mock.Mock(side_effect=AssertionError("worker must not call Tk"))

        def fake_ask(messages, model, config, on_chunk=None):
            worker_thread.append(threading.get_ident())
            on_chunk("stream")
            return "answer"

        def run_worker():
            self.app.ask_in_background("hello", [])

        with mock.patch.dict(self.namespace, {"ask_model": fake_ask}):
            thread = threading.Thread(target=run_worker, daemon=True)
            thread.start()
            thread.join(timeout=2)
        events = [self.app.cloud_reply_queue.get_nowait(), self.app.cloud_reply_queue.get_nowait()]
        self.assertEqual(events, [("chunk", "stream"), ("done", "answer")])
        self.assertTrue(worker_thread)
        self.assertNotEqual(worker_thread[0], threading.get_ident())
        self.app.after.assert_not_called()

    def test_local_worker_sends_chunks_and_done_to_queue_without_tk_calls(self):
        worker_thread = []
        self.app.config_data["mode"] = "local"
        # A request token captures the mode/provider at send time, and on_chunk drops
        # anything whose token no longer matches the live mode.  The harness snapshot
        # still describes cloud mode, so refresh it exactly as send_message does.
        self.app.cloud_request_snapshot = (
            copy.deepcopy(self.app.config_data), [{"role": "user", "content": "hello"}])
        self.app.should_use_web_search = lambda *_args: False
        self.app.ask_in_background = self.namespace["ChatApp"].ask_in_background.__get__(self.app)
        self.app.after = mock.Mock(side_effect=AssertionError("worker must not call Tk"))

        def fake_ask(prompt, model, size, on_chunk=None):
            worker_thread.append(threading.get_ident())
            on_chunk("local stream")
            return "local answer"

        with mock.patch.dict(self.namespace, {"ask_local": fake_ask}):
            thread = threading.Thread(
                target=lambda: self.app.ask_in_background("hello", []), daemon=True)
            thread.start()
            thread.join(timeout=2)
        events = [self.app.cloud_reply_queue.get_nowait(), self.app.cloud_reply_queue.get_nowait()]
        self.assertEqual(events, [("chunk", "local stream"), ("done", "local answer")])
        self.assertTrue(worker_thread)
        self.assertNotEqual(worker_thread[0], threading.get_ident())
        self.app.after.assert_not_called()

    def test_session_switches_are_guarded_during_local_and_cloud_requests(self):
        self.app.create_new_chat = self.namespace["ChatApp"].create_new_chat.__get__(self.app)
        self.app.load_history_chat = self.namespace["ChatApp"].load_history_chat.__get__(self.app)
        self.app.toggle_mode = self.namespace["ChatApp"].toggle_mode.__get__(self.app)
        self.app.show_provider_settings = self.namespace["ChatApp"].show_provider_settings.__get__(self.app)
        for mode in ("local", "cloud"):
            with self.subTest(mode=mode):
                self.app.config_data["mode"] = mode
                self.app.asking = True
                with mock.patch.object(messagebox, "showinfo") as showinfo, mock.patch.object(
                    self.app, "render_messages", side_effect=AssertionError("chat must not switch")
                ), mock.patch.dict(self.namespace, {"save_config": mock.Mock()}):
                    self.app.create_new_chat()
                    self.app.load_history_chat("other.json")
                    self.app.toggle_mode()
                    self.app.show_provider_settings()
                self.assertEqual(showinfo.call_count, 4)

    def test_idle_state_allows_conversation_and_routing_switches(self):
        self.app.asking = False
        self.app.history_visible = False
        self.app.create_new_chat = self.namespace["ChatApp"].create_new_chat.__get__(self.app)
        self.app.load_history_chat = self.namespace["ChatApp"].load_history_chat.__get__(self.app)
        self.app.toggle_mode = self.namespace["ChatApp"].toggle_mode.__get__(self.app)
        self.app.mode_button = mock.Mock()
        self.app.model_button = mock.Mock()
        self.app.provider_button = mock.Mock()
        self.app.refresh_agent_button = mock.Mock()
        self.app.render_messages = mock.Mock()
        with mock.patch.object(messagebox, "showinfo") as showinfo, mock.patch.dict(
            self.namespace,
            {
                "new_chat": lambda _config: ("new.json", {"messages": []}),
                "load_chat": lambda _filename: ("loaded.json", {"messages": []}),
                "save_config": mock.Mock(),
            },
        ):
            self.app.create_new_chat()
            self.app.load_history_chat("other.json")
            self.app.toggle_mode()
        self.assertFalse(showinfo.called)
        self.assertGreaterEqual(self.app.render_messages.call_count, 2)

    def test_mode_toggle_and_cloud_labels_use_canonical_settings(self):
        self.app.mode_button = mock.Mock()
        self.app.model_button = mock.Mock()
        self.app.provider_button = mock.Mock()
        self.app.refresh_agent_button = mock.Mock()
        self.app.toggle_mode = self.namespace["ChatApp"].toggle_mode.__get__(self.app)
        save_config = mock.Mock()
        with mock.patch.dict(self.namespace, {"save_config": save_config}):
            self.app.toggle_mode()
        self.assertEqual(self.app.config_data["mode"], "local")
        save_config.assert_called_once()
        self.assertEqual(self.app.mode_label(), "local_mode_label")

    def test_cloud_settings_save_captures_provider_model_url_and_key(self):
        buttons = []
        saved_calls = []
        self.app.flat_button = lambda parent, label, command, **kwargs: _CommandButton(buttons, label, command)
        self.app.show_cloud_settings = self.namespace["ChatApp"].show_cloud_settings.__get__(self.app)
        save_config = lambda draft, cloud_key_updates=None: saved_calls.append((draft, cloud_key_updates))
        with mock.patch.dict(self.namespace, {"save_config": save_config}):
            self.app.show_cloud_settings()
            entries = [child for child in self.app.winfo_children() if isinstance(child, tk.Toplevel)]
            self.assertEqual(len(entries), 1)
            dialog = entries[0]
            body = dialog.winfo_children()[0]
            selector = next(child for child in body.winfo_children() if isinstance(child, ttk.Combobox))
            selector.set("DeepSeek")
            selector.event_generate("<<ComboboxSelected>>")
            self.app.update_idletasks()
            input_widgets = [child for child in body.winfo_children() if isinstance(child, tk.Entry) and not isinstance(child, ttk.Combobox)]
            api_key_entry = next(widget for widget in input_widgets if widget.cget("show") == "*")
            visible_entries = [widget for widget in input_widgets if widget.cget("show") != "*"]
            model_entry, base_url_entry = visible_entries
            model_entry.delete(0, "end")
            model_entry.insert(0, "deepseek-chat")
            base_url_entry.delete(0, "end")
            base_url_entry.insert(0, "https://api.deepseek.com/v1")
            api_key_entry.delete(0, "end")
            api_key_entry.insert(0, "sk-ui-key")
            next(button.command for button in buttons if button.label == "save")()
        draft, updates = saved_calls[0]
        self.assertEqual(draft["cloud"]["provider"], "deepseek")
        self.assertEqual(draft["cloud"]["providers"]["deepseek"]["model"], "deepseek-chat")
        self.assertEqual(draft["cloud"]["providers"]["deepseek"]["base_url"], "https://api.deepseek.com/v1")
        self.assertEqual(updates["deepseek"], "sk-ui-key")

    def test_cloud_usage_worker_runs_off_tk_thread(self):
        observed = []
        target = cloudproviders.resolve_active_provider(self.app.config_data, api_key="sk-usage")
        with mock.patch.dict(self.namespace, {"resolve_cloud_target": lambda _cfg: target}):
            with mock.patch.object(
                local_ai.cloudusage, "fetch_cloud_usage", side_effect=lambda *args, **kwargs: observed.append(threading.get_ident()) or "balance=1"
            ):
                self.app.flat_button = lambda parent, label, command, **kwargs: _CommandButton([], label, command)
                self.app.show_cloud_usage = self.namespace["ChatApp"].show_cloud_usage.__get__(self.app)
                self.app.show_cloud_usage()
            deadline = time.time() + 2
            while time.time() < deadline and not observed:
                self.app.update()
                time.sleep(0.01)
        self.assertTrue(observed)
        self.assertNotEqual(observed[0], threading.get_ident())


class _CommandButton:
    def __init__(self, bucket, label, command):
        self.bucket = bucket
        self.label = label
        self.command = command
        if bucket is not None:
            bucket.append(self)

    def pack(self, **_kwargs):
        return None


if __name__ == "__main__":
    unittest.main()
