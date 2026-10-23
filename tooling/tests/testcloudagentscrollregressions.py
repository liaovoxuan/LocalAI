"""Regression contracts for the two user-reproduced defects.

Gate 1 - Cloud Agent mode
    The Agent control must be visible, enabled and operable while the app is in cloud
    mode; its selected state must survive Local/Cloud switches and theme rebuilds; an
    Agent send must use the selected cloud provider/model through the existing vault,
    normalized-error, usage, attachment and stream/session paths; ``ask_ollama`` must
    never be a fallback; ordinary non-Agent cloud chat must be unchanged; one
    activation must create exactly one request; mode switches and window close must
    reject stale stream callbacks; and an unsupported provider capability must surface
    a localized message instead of hiding Agent mode or silently rerouting.

Gate 2 - Mapped-window scrolling
    Every scroll contract runs on a genuinely mapped Tk window (never only withdrawn),
    covering small macOS deltas, short momentum-like sequences, direction, bounded and
    proportional movement, ownership by the widget under the pointer, nested dialogs,
    scrollbar thumb drag round-trip, reading-position preservation on resize,
    binding idempotence and cleanup on dialog/app close.

All provider traffic goes to a loopback stub.  No real provider, secret or internet
call is made, and no credential is written outside the isolated temp vault.
"""
from __future__ import annotations

import ast
import copy
import http.server
import inspect
import json
import os
import queue
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
DESKTOP_DIR = REPO_ROOT / "projects" / "LocalAI" / "desktop"
if str(DESKTOP_DIR) not in sys.path:
    sys.path.insert(0, str(DESKTOP_DIR))

import tkinter as tk  # noqa: E402
from tkinter import filedialog, messagebox, ttk  # noqa: E402

import local_ai  # noqa: E402
import cloudsecrets  # noqa: E402

WINDOW_GEOMETRY = "900x600+60+60"
MESSAGE_COUNT = 14
VAULT_KEY = "sk-loopback-agent-gate-0001"
AGENT_FINAL = json.dumps({"action": "final", "content": "agent finished"})
ATTACHMENT_MARKER = "ATTACHMARKER9"


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
class _WheelEvent:
    def __init__(self, num=None, delta=0, widget=None):
        self.num = num
        self.delta = delta
        self.widget = widget


class _Harness:
    _namespace = None

    @classmethod
    def namespace(cls):
        if cls._namespace is None:
            source = textwrap.dedent(inspect.getsource(local_ai.run_chat_gui))
            tree = ast.parse(source)
            selected = [
                node for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                and node.name in {"draw_round_rect", "RoundedButton", "ChatApp"}
            ]
            namespace = vars(local_ai).copy()
            namespace.update({"tk": tk, "ttk": ttk,
                              "filedialog": filedialog, "messagebox": messagebox})
            exec(compile(ast.Module(body=selected, type_ignores=[]),
                         "<CloudAgentScrollHarness>", "exec"), namespace)
            cls._namespace = namespace
        return cls._namespace

    @classmethod
    def chat_app(cls):
        return cls.namespace()["ChatApp"]


class _SyncThread:
    """Run worker bodies inline so a send is deterministic and leaves no thread."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **_ignored):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)

    def join(self, timeout=None):
        return None

    def is_alive(self):
        return False


class _AgentStubHandler(http.server.BaseHTTPRequestHandler):
    server_version = "AgentGateStub/1.0"

    def log_message(self, *args):
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {}
        with self.server.lock:  # type: ignore[attr-defined]
            self.server.requests.append({  # type: ignore[attr-defined]
                "path": self.path.split("?")[0],
                "auth": self.headers.get("Authorization", ""),
                "body": body,
            })
        status = self.server.status  # type: ignore[attr-defined]
        content_type = "application/json"
        if status != 200:
            payload = json.dumps(
                {"error": {"message": self.server.error_text}}).encode()  # type: ignore[attr-defined]
        elif body.get("stream"):
            # The GUI streams; answer as an OpenAI-compatible SSE provider would.
            text = self.server.reply  # type: ignore[attr-defined]
            step = max(len(text) // 3, 1)
            events = "".join(
                "data: " + json.dumps({"choices": [{"delta": {"content": piece}}]}) + "\n\n"
                for piece in (text[i:i + step] for i in range(0, len(text), step))
            )
            payload = (events + "data: [DONE]\n\n").encode()
            content_type = "text/event-stream"
        else:
            payload = json.dumps(
                {"choices": [{"message": {"content": self.server.reply}}]}).encode()  # type: ignore[attr-defined]
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _AgentStub:
    """Loopback OpenAI-compatible provider; records every request."""

    def __init__(self, reply=AGENT_FINAL, status=200,
                 error_text="tools are not supported by this model"):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _AgentStubHandler)
        self.httpd.requests = []  # type: ignore[attr-defined]
        self.httpd.lock = threading.Lock()  # type: ignore[attr-defined]
        self.httpd.reply = reply  # type: ignore[attr-defined]
        self.httpd.status = status  # type: ignore[attr-defined]
        self.httpd.error_text = error_text  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    @property
    def requests(self):
        with self.httpd.lock:  # type: ignore[attr-defined]
            return list(self.httpd.requests)  # type: ignore[attr-defined]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class _GuiCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "chats").mkdir(parents=True, exist_ok=True)
        self.patches = mock.patch.multiple(
            local_ai,
            APP_DATA_DIR=str(self.root),
            CHAT_DIR=str(self.root / "chats"),
            EXPORT_DIR=str(self.root / "exports"),
            LOG_DIR=str(self.root / "logs"),
            CONFIG_FILE=str(self.root / "config.json"),
        )
        self.patches.start()
        # Keep the loopback stub immune to a developer proxy without leaking env.
        self._env = mock.patch.dict(
            os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}, clear=False)
        self._env.start()
        self.app = None

    def tearDown(self):
        if self.app is not None:
            try:
                self.app.destroy()
            except Exception:
                pass
        self._env.stop()
        self.patches.stop()
        self._tmp.cleanup()

    # -- configuration -----------------------------------------------------
    def config_for(self, mode="local", agent_enabled=False, base_url="http://127.0.0.1:9/v1",
                   model="cloud-stub-model", provider="openai_compatible"):
        config = copy.deepcopy(local_ai.DEFAULT_CONFIG)
        config.update({"mode": mode, "agent_enabled": agent_enabled, "language": "en_us",
                       "theme": "light", "last_model": "local-qwen"})
        config["cloud"] = {
            "provider": provider,
            "default_model": model,
            "providers": {provider: {"base_url": base_url, "model": model, "api_key": ""}},
        }
        return local_ai.normalize_provider_config(config)

    def seed_vault(self, provider="openai_compatible", key=VAULT_KEY):
        cloudsecrets.set_api_key(str(self.root), provider, key)

    # -- applications ------------------------------------------------------
    def make_app(self, mode="local", agent_enabled=False, mapped=False, **kwargs):
        chat_app = _Harness.chat_app()
        # Neutralise deferred jobs: a pending after() timer must never mutate state
        # in the middle of an assertion.
        with mock.patch.object(chat_app, "after",
                               lambda self, delay, func=None, *args: "job"):
            app = chat_app(self.config_for(mode, agent_enabled, **kwargs),
                           web_func=lambda *args, **kw: "")
        if mapped:
            app.geometry(WINDOW_GEOMETRY)
            app.update()
        else:
            app.withdraw()
            app.update_idletasks()
        # Mapping the window schedules a deferred resize (after(160, ...)); leaving it
        # live would let the canvas change size mid-assertion on a loaded machine, so
        # no Tk timer is allowed to run for the lifetime of the test.  Everything the
        # contracts need is invoked explicitly instead.
        self._timers = mock.patch.multiple(
            app,
            after=mock.Mock(side_effect=lambda delay, func=None, *args: "job"),
            after_cancel=mock.Mock(return_value=None),
        )
        self._timers.start()
        self.addCleanup(self._timers.stop)
        self.app = app
        return app

    # -- deterministic Tk helpers -----------------------------------------
    @staticmethod
    def render_without_timers(app, messages):
        app.chat_data["messages"] = list(messages)
        with mock.patch.object(app, "after",
                               side_effect=lambda delay, func=None, *a: "job"):
            app.render_messages()
            app.update_scroll_region()
        app.update_idletasks()

    @staticmethod
    def wheel(app, widget, num=None, delta=0, target=None):
        app.last_trackpad_scroll_time = 0.0
        app.handle_trackpad_scroll(
            _WheelEvent(num=num, delta=delta, widget=widget), target or app.canvas)
        app.update_idletasks()

    @staticmethod
    def yview(app):
        return tuple(round(value, 6) for value in app.canvas.yview())

    @staticmethod
    def warp_pointer(app, widget):
        x = widget.winfo_rootx() + max(widget.winfo_width() // 2, 1)
        y = widget.winfo_rooty() + max(widget.winfo_height() // 2, 1)
        app.event_generate("<Motion>", warp=True, x=x, y=y)
        app.update_idletasks()

    def send(self, app, text="do the thing", agent_calls=None):
        """Drive one full send synchronously and drain the reply queue."""
        app.input_text.delete("1.0", "end")
        app.input_text.insert("1.0", text)
        app.update_send_button_state()
        overrides = {
            "threading": SimpleNamespace(Thread=_SyncThread, Event=threading.Event),
            "save_chat_async": lambda *a, **k: None,
        }
        if agent_calls is not None:
            real_agent = local_ai.AgentSession

            def recording_agent(*args, **kwargs):
                agent_calls.append((args, kwargs))
                return real_agent(*args, **kwargs)

            overrides["AgentSession"] = recording_agent
        with mock.patch.dict(_Harness.namespace(), overrides):
            app.send_message()
        app.poll_cloud_reply()
        app.update_idletasks()


# --------------------------------------------------------------------------- #
# Gate 1a - the Agent control itself
# --------------------------------------------------------------------------- #
class CloudAgentControlTests(_GuiCase):
    def test_agent_control_is_visible_in_cloud_mode(self):
        app = self.make_app(mode="cloud", agent_enabled=True)
        self.assertTrue(hasattr(app, "agent_button"))
        self.assertTrue(app.agent_button.winfo_exists(),
                        "the Agent control is destroyed in cloud mode")
        self.assertTrue(str(app.agent_button.text).strip(),
                        "the Agent control has an empty label in cloud mode")

    def test_agent_control_is_enabled_in_cloud_mode(self):
        app = self.make_app(mode="cloud", agent_enabled=True)
        self.assertTrue(app.agent_available(),
                        "Agent mode is reported unavailable in cloud mode")
        self.assertEqual(app.agent_button.state, "normal",
                         "the Agent control is disabled in cloud mode")

    def test_agent_control_is_mouse_operable_in_cloud_mode(self):
        app = self.make_app(mode="cloud", agent_enabled=False)
        with mock.patch.object(messagebox, "showinfo") as showinfo, \
                mock.patch.dict(_Harness.namespace(), {"save_config": mock.Mock()}):
            app.agent_button.on_click()
            app.agent_button.on_release()
        self.assertEqual(showinfo.call_count, 0,
                         "clicking the Agent control in cloud mode was refused")
        self.assertTrue(app.agent_enabled, "the click did not select Agent mode")

    def test_agent_control_is_keyboard_operable_in_cloud_mode(self):
        app = self.make_app(mode="cloud", agent_enabled=False)
        self.assertIn(app.agent_button.cget("takefocus"), (1, "1", True),
                      "the Agent control is not in the keyboard focus order")
        for sequence in ("<KeyPress-space>", "<KeyRelease-space>",
                         "<KeyPress-Return>", "<KeyRelease-Return>"):
            with self.subTest(sequence=sequence):
                self.assertTrue(app.agent_button.bind(sequence),
                                f"the Agent control has no {sequence} binding")
        with mock.patch.object(messagebox, "showinfo") as showinfo, \
                mock.patch.dict(_Harness.namespace(), {"save_config": mock.Mock()}):
            app.agent_button.on_key_press()
            app.agent_button.on_key_release()
        self.assertEqual(showinfo.call_count, 0,
                         "keyboard activation of the Agent control was refused")
        self.assertTrue(app.agent_enabled, "keyboard activation did not select Agent mode")

    def test_agent_control_shows_the_enabled_highlight(self):
        app = self.make_app(mode="cloud", agent_enabled=True)
        self.assertEqual(app.agent_button.text, app.ct("agent_on"))
        self.assertEqual(app.agent_button.button_bg, app.colors["web_on_bg"],
                         "the enabled Agent control is not visually distinguished")

    def test_agent_control_shows_the_disabled_highlight_when_off(self):
        app = self.make_app(mode="cloud", agent_enabled=False)
        self.assertEqual(app.agent_button.text, app.ct("agent_off"))
        self.assertEqual(app.agent_button.button_bg, app.colors["surface"])


# --------------------------------------------------------------------------- #
# Gate 1b - selected state survives switches and theme rebuilds
# --------------------------------------------------------------------------- #
class CloudAgentStatePersistenceTests(_GuiCase):
    def test_enabled_state_survives_local_to_cloud_switch(self):
        app = self.make_app(mode="local", agent_enabled=True)
        with mock.patch.dict(_Harness.namespace(), {"save_config": mock.Mock()}), \
                mock.patch.object(messagebox, "showinfo") as showinfo:
            app.toggle_mode()
        self.assertEqual(app.config_data["mode"], "cloud")
        self.assertTrue(app.agent_enabled, "the Agent selection was dropped by the switch")
        self.assertTrue(app.agent_available(), "Agent mode became unavailable after switching")
        self.assertEqual(app.agent_button.text, app.ct("agent_on"))
        self.assertEqual(showinfo.call_count, 0,
                         "switching to cloud reported Agent mode as paused")

    def test_enabled_state_survives_cloud_to_local_and_back(self):
        app = self.make_app(mode="cloud", agent_enabled=True)
        with mock.patch.dict(_Harness.namespace(), {"save_config": mock.Mock()}), \
                mock.patch.object(messagebox, "showinfo"):
            app.toggle_mode()
            self.assertTrue(app.agent_enabled)
            app.toggle_mode()
        self.assertTrue(app.agent_enabled)
        self.assertEqual(app.agent_button.text, app.ct("agent_on"))

    def test_enabled_state_survives_a_theme_rebuild(self):
        app = self.make_app(mode="cloud", agent_enabled=True)
        self.assertEqual(app.agent_button.button_bg, app.colors["web_on_bg"])
        app.config_data["theme"] = "dark"
        app.colors = local_ai.theme_palette("dark")
        app.rebuild_theme_colors()
        app.update_idletasks()
        self.assertTrue(app.agent_enabled, "the theme rebuild cleared the Agent selection")
        self.assertEqual(app.agent_button.text, app.ct("agent_on"),
                         "the theme rebuild reset the Agent label to off")
        self.assertEqual(app.agent_button.button_bg, app.colors["web_on_bg"],
                         "the theme rebuild wiped the enabled Agent highlight")

    def test_disabled_state_survives_a_theme_rebuild(self):
        app = self.make_app(mode="cloud", agent_enabled=False)
        app.config_data["theme"] = "dark"
        app.colors = local_ai.theme_palette("dark")
        app.rebuild_theme_colors()
        app.update_idletasks()
        self.assertFalse(app.agent_enabled)
        self.assertEqual(app.agent_button.text, app.ct("agent_off"))


# --------------------------------------------------------------------------- #
# Gate 1c - routing, shared paths, fallbacks and lifecycle
# --------------------------------------------------------------------------- #
class CloudAgentRoutingTests(_GuiCase):
    def _agent_config(self, base_url, model="cloud-stub-model"):
        return self.config_for(mode="cloud", agent_enabled=True,
                               base_url=base_url, model=model)

    def test_agent_send_uses_the_selected_cloud_provider_and_model(self):
        with _AgentStub() as stub:
            self.seed_vault()
            config = self._agent_config(stub.base_url, model="selected-cloud-model")
            with mock.patch.object(local_ai, "ask_ollama",
                                   side_effect=AssertionError("agent fell back to Ollama")):
                answer = local_ai.AgentSession("local-qwen", "small", config).run("do it")
        self.assertIn("agent finished", answer)
        requests = stub.requests
        self.assertTrue(requests, "the agent made no provider request")
        self.assertTrue(all(r["path"].endswith("/chat/completions") for r in requests))
        self.assertEqual(requests[0]["body"]["model"], "selected-cloud-model",
                         "the agent used the local model name instead of the cloud model")

    def test_agent_send_reads_the_key_from_the_vault(self):
        with _AgentStub() as stub:
            self.seed_vault(key=VAULT_KEY)
            config = self._agent_config(stub.base_url)
            self.assertEqual(config.get("api_key"), "",
                             "the test config must not carry a plaintext key")
            local_ai.AgentSession("local-qwen", "small", config).run("do it")
        self.assertEqual(stub.requests[0]["auth"], f"Bearer {VAULT_KEY}",
                         "the agent did not read the credential from the vault")

    def test_agent_cloud_failure_is_a_normalized_localized_error(self):
        with _AgentStub(status=400, error_text="tools are not supported by this model") as stub:
            self.seed_vault()
            config = self._agent_config(stub.base_url)
            with mock.patch.object(local_ai, "ask_ollama",
                                   side_effect=AssertionError("agent fell back to Ollama")):
                answer = local_ai.AgentSession("local-qwen", "small", config).run("do it")
        self.assertTrue(answer.strip(), "a cloud failure produced no message")
        self.assertNotIn("tools are not supported by this model", answer,
                         "raw provider text leaked into the agent reply")

    def test_agent_send_never_calls_ask_ollama(self):
        with _AgentStub() as stub:
            self.seed_vault()
            config = self._agent_config(stub.base_url)
            with mock.patch.object(local_ai, "ask_ollama",
                                   side_effect=AssertionError("agent fell back to Ollama")) as ollama:
                local_ai.AgentSession("local-qwen", "small", config).run("do it")
        ollama.assert_not_called()

    def test_agent_send_keeps_the_attachment_bound(self):
        with _AgentStub() as stub:
            self.seed_vault()
            config = self._agent_config(stub.base_url)
            attachment = self.root / "context.txt"
            attachment.write_text(f"attachment {ATTACHMENT_MARKER} " + "x" * 60000,
                                  encoding="utf-8")
            local_ai.AgentSession("local-qwen", "small", config).run(f"analyse {attachment}")
        sent = json.dumps(stub.requests[0]["body"])
        self.assertIn(attachment.name, sent,
                      "the agent never surfaced the attachment path to the model")
        self.assertNotIn("x" * 5000, sent,
                         "the agent inlined bulk attachment content instead of using the "
                         "bounded attachment path")
        self.assertLessEqual(len(sent), local_ai.CLOUD_ATTACHMENT_TOTAL_CHARS + 20000,
                             "the agent request bypassed the aggregate bound")

    def test_agent_stream_events_flow_through_the_shared_queue(self):
        app = self.make_app(mode="cloud", agent_enabled=True)
        app.cloud_reply_queue = queue.Queue()
        app.cloud_request_snapshot = (copy.deepcopy(app.config_data),
                                      [{"role": "user", "content": "hello"}])

        class _StreamingAgent:
            def __init__(self, *args, **kwargs):
                pass

            def run(self, request, on_chunk=None):
                if on_chunk:
                    on_chunk("partial")
                return "AGENT DONE"

        with mock.patch.dict(_Harness.namespace(), {"AgentSession": _StreamingAgent}):
            app.ask_in_background("hello", [])

        streamed = []
        real_append = app.append_stream_chunk
        app.asking = True
        with mock.patch.object(app, "append_stream_chunk",
                               side_effect=lambda value: (streamed.append(value),
                                                          real_append(value))[1]):
            app.poll_cloud_reply()
        self.assertEqual(streamed, ["partial"],
                         "the streamed chunk never reached the chat surface")
        self.assertFalse(app.asking, "the terminal event did not clear the busy state")
        self.assertEqual(app.chat_data["messages"][-1]["content"], "AGENT DONE")

    def test_agent_send_in_cloud_mode_runs_the_agent_loop(self):
        self.seed_vault()
        with _AgentStub() as stub:
            app = self.make_app(mode="cloud", agent_enabled=True, base_url=stub.base_url)
            agent_calls = []
            with mock.patch.object(local_ai, "ask_ollama",
                                   side_effect=AssertionError("fell back to Ollama")):
                self.send(app, "do the thing", agent_calls=agent_calls)
        self.assertEqual(len(agent_calls), 1,
                         "a cloud Agent send did not construct the agent loop")
        self.assertTrue(
            app.chat_data["messages"][-1]["content"].startswith("agent finished"),
            "the agent's answer did not reach the chat surface: "
            f"{app.chat_data['messages'][-1]['content']!r}")

    def test_one_activation_creates_exactly_one_request(self):
        self.seed_vault()
        with _AgentStub() as stub:
            app = self.make_app(mode="cloud", agent_enabled=True, base_url=stub.base_url)
            agent_calls = []
            self.send(app, "do the thing", agent_calls=agent_calls)
            requests = stub.requests
        self.assertEqual(len(agent_calls), 1, "the agent loop ran more than once")
        self.assertEqual(len(requests), 1,
                         f"one activation produced {len(requests)} provider requests")

    def test_ordinary_non_agent_cloud_chat_is_unchanged(self):
        app = self.make_app(mode="cloud", agent_enabled=False)
        dispatch = []
        app.cloud_reply_queue = queue.Queue()
        app.cloud_request_snapshot = (copy.deepcopy(app.config_data),
                                      [{"role": "user", "content": "hello"}])

        class _NeverAgent:
            def __init__(self, *args, **kwargs):
                dispatch.append("agent")

        with mock.patch.dict(_Harness.namespace(), {
                "AgentSession": _NeverAgent,
                "ask_model": lambda *a, **k: dispatch.append("ask_model") or "PLAIN"}):
            app.ask_in_background("hello", [])
        self.assertEqual(dispatch, ["ask_model"],
                         "a non-Agent cloud chat was rerouted through the agent")

    def test_mode_switch_rejects_stale_stream_callbacks(self):
        app = self.make_app(mode="cloud", agent_enabled=True)
        app.cloud_reply_queue = queue.Queue()
        seen = []

        class _SwitchingAgent:
            def __init__(self, *args, **kwargs):
                pass

            def run(self, request, on_chunk=None):
                on_chunk("first")
                app.config_data["mode"] = "local"   # user switches away mid-stream
                on_chunk("second")
                return "late"

        with mock.patch.dict(_Harness.namespace(), {"AgentSession": _SwitchingAgent}):
            app.ask_in_background("hello", [])
        while not app.cloud_reply_queue.empty():
            seen.append(app.cloud_reply_queue.get_nowait())
        self.assertEqual(seen, [("chunk", "first")],
                         "a stream from the previous mode kept delivering events")

    def test_close_rejects_stale_stream_callbacks(self):
        app = self.make_app(mode="cloud", agent_enabled=True)
        app.cloud_reply_queue = queue.Queue()
        seen = []

        class _ClosingAgent:
            def __init__(self, *args, **kwargs):
                pass

            def run(self, request, on_chunk=None):
                on_chunk("first")
                app.closing = True                  # window closes mid-stream
                on_chunk("second")
                return "late"

        with mock.patch.dict(_Harness.namespace(), {"AgentSession": _ClosingAgent}):
            app.ask_in_background("hello", [])
        while not app.cloud_reply_queue.empty():
            seen.append(app.cloud_reply_queue.get_nowait())
        self.assertEqual(seen, [("chunk", "first")],
                         "events kept flowing after the window started closing")

    def test_unsupported_provider_capability_is_a_localized_message(self):
        with _AgentStub(status=400, error_text="tools are not supported by this model") as stub:
            self.seed_vault()
            config = self._agent_config(stub.base_url)
            with mock.patch.object(local_ai, "ask_ollama",
                                   side_effect=AssertionError("fallback used")):
                answer = local_ai.AgentSession("local-qwen", "small", config).run("do it")
        localized = {
            local_ai.cloud_error_text(config, exc)
            for exc in (local_ai.clouderrors.CloudAIError(local_ai.clouderrors.CLOUD_REQUEST_ERROR),
                        local_ai.clouderrors.CloudAIError(local_ai.clouderrors.CLOUD_NO_KEY),
                        local_ai.clouderrors.CloudAIError(local_ai.clouderrors.CLOUD_AUTH_ERROR))
        }
        self.assertTrue(any(text and text in answer for text in localized),
                        f"the capability failure was not a localized message: {answer!r}")
        self.assertNotIn("tools are not supported", answer,
                         "the raw provider message was surfaced to the user")


# --------------------------------------------------------------------------- #
# Gate 2a - mapped-window scrolling
# --------------------------------------------------------------------------- #
class MappedWindowScrollTests(_GuiCase):
    def _app_with_content(self, mode="local"):
        app = self.make_app(mode=mode, mapped=True)
        self.render_without_timers(app, [
            {"role": "user" if index % 2 == 0 else "ai",
             "content": f"message {index} " + "lorem ipsum " * 20}
            for index in range(MESSAGE_COUNT)
        ])
        return app

    def test_window_is_really_mapped_with_a_real_canvas(self):
        app = self._app_with_content()
        self.assertTrue(app.winfo_ismapped(), "the window under test is not mapped")
        self.assertGreater(app.canvas.winfo_width(), 1)
        self.assertGreater(app.canvas.winfo_height(), 1)

    def test_small_positive_and_negative_macos_deltas_move_one_step(self):
        app = self._app_with_content()
        with mock.patch.object(app, "pointer_over_chat_area", return_value=True), \
                mock.patch.object(local_ai.platform, "system", return_value="Darwin"):
            app.canvas.yview_moveto(0.3)
            app.update_idletasks()
            before_px = self.top_pixels(app)
            self.wheel(app, app.canvas, delta=1)
            positive_px = self.top_pixels(app)
            self.wheel(app, app.canvas, delta=-1)
            negative_px = self.top_pixels(app)

        self.assertGreater(abs(positive_px - before_px), 0.5,
                           "a small positive macOS delta did not move the view")
        self.assertGreater(abs(negative_px - positive_px), 0.5,
                           "a small negative macOS delta did not move the view")
        self.assertLess((positive_px - before_px) * (negative_px - positive_px), 0,
                        "the two small deltas moved in the same direction")
        self.assertAlmostEqual(negative_px, before_px, delta=2.0,
                               msg="a positive then negative delta did not round-trip")

    def test_button4_and_button5_map_to_opposite_directions(self):
        # Each button event is applied to a settled canvas positioned mid-document, so
        # a deferred scrollregion recompute cannot mask the second direction.  (The
        # pre-existing suite fires both events back to back on an unsettled canvas,
        # where the first event legitimately lands the view on the bottom bound.)
        app = self._app_with_content()
        pixel_canvas = float(app.canvas.cget("yscrollincrement") or 0) == 1
        step = 36 if pixel_canvas else local_ai.get_scroll_units(3)

        recorded = []
        app.canvas.yview_scroll = lambda units, what: recorded.append((units, what))
        for num, sign in ((4, -1), (5, 1)):
            app.canvas.yview_moveto(0.5)
            app.update_idletasks()
            app.scroll_region_job = None
            recorded.clear()
            self.wheel(app, app.canvas, num=num)
            self.assertEqual(recorded, [(sign * step, "units")],
                             f"Button-{num} produced {recorded!r}")

    def test_macos_delta_direction_and_magnitude_follow_the_gesture(self):
        # The chat canvas is a pixel canvas (yscrollincrement=1).  On macOS a wheel
        # delta therefore selects the direction and scales the step, and the pixel
        # figure is clamped before it is applied.
        app = self._app_with_content()
        pixel_canvas = float(app.canvas.cget("yscrollincrement") or 0) == 1
        self.assertTrue(pixel_canvas, "the chat canvas is no longer a pixel canvas")
        factor = 3 if pixel_canvas else 1
        limit = max(36.0, app.canvas.winfo_height() / 3)

        recorded = []
        app.canvas.yview_scroll = lambda units, what: recorded.append((units, what))
        with mock.patch.object(app, "pointer_over_chat_area", return_value=True), \
                mock.patch.object(local_ai.platform, "system", return_value="Darwin"):
            for delta in (1, 2, 6, -1, -2, -6):
                app.canvas.yview_moveto(0.4)
                app.update_idletasks()
                app.scroll_remainders.clear()
                recorded.clear()
                self.wheel(app, app.canvas, delta=delta)
                self.assertEqual(len(recorded), 1,
                                 f"delta {delta} produced {recorded!r}")
                units, what = recorded[0]
                self.assertEqual(what, "units")
                expected = max(-limit, min(limit, -delta * factor))
                self.assertEqual(units, expected,
                                 f"delta {delta} did not scale to {expected}: {units}")

    def test_fractional_deltas_accumulate_instead_of_being_lost(self):
        # Windows-style deltas are fractional once scaled; the remainder accumulator
        # must carry the fraction forward so slow gestures still move the view.
        app = self._app_with_content()
        pixel_canvas = float(app.canvas.cget("yscrollincrement") or 0) == 1
        per_event = -5 / 120 * (36 if pixel_canvas else local_ai.get_scroll_units(3))
        self.assertNotEqual(per_event, int(per_event),
                            "choose a delta that is genuinely fractional")

        recorded = []
        app.canvas.yview_scroll = lambda units, what: recorded.append((units, what))
        with mock.patch.object(app, "pointer_over_chat_area", return_value=True), \
                mock.patch.object(local_ai.platform, "system", return_value="Windows"):
            app.canvas.yview_moveto(0.5)
            app.update_idletasks()
            app.scroll_remainders.clear()
            for _ in range(2):
                self.wheel(app, app.canvas, delta=5)
        self.assertTrue(recorded, "the fractional deltas moved nothing at all")
        self.assertEqual(sum(units for units, _ in recorded), int(per_event * 2),
                         f"sub-unit movement was lost: {recorded}")

    def test_momentum_like_delta_sequence_is_monotonic_and_bounded(self):
        app = self._app_with_content()
        with mock.patch.object(app, "pointer_over_chat_area", return_value=True), \
                mock.patch.object(local_ai.platform, "system", return_value="Darwin"):
            app.canvas.yview_moveto(0.1)
            app.update_idletasks()
            positions = [self.yview(app)[0]]
            for _ in range(5):          # a short momentum-like burst
                self.wheel(app, app.canvas, delta=3)
                positions.append(self.yview(app)[0])
        ascending = positions == sorted(positions)
        descending = positions == sorted(positions, reverse=True)
        self.assertTrue(ascending or descending,
                        f"the burst was not monotonic in either direction: {positions}")
        self.assertTrue(0.0 <= positions[-1] <= 1.0, "the burst left the document bounds")

    def test_movement_is_bounded_and_proportional(self):
        app = self._app_with_content()
        with mock.patch.object(app, "pointer_over_chat_area", return_value=True), \
                mock.patch.object(local_ai.platform, "system", return_value="Darwin"):
            app.canvas.yview_moveto(0.0)
            app.update_idletasks()
            self.wheel(app, app.canvas, num=5)
            first = self.yview(app)
            step = first[0]
            self.assertGreater(step, 0, "one notch did not move the view")
            self.assertLess(step, 0.5, f"one notch jumped {step:.3f} of the document")
            self.wheel(app, app.canvas, num=5)
            second = self.yview(app)
            self.assertAlmostEqual(second[0] - first[0], step, places=6,
                                   msg="successive notches moved by different amounts")

    def test_ownership_follows_the_widget_under_the_pointer(self):
        app = self._app_with_content()
        app.canvas.yview_moveto(0.4)
        app.update_idletasks()
        before = self.yview(app)

        self.warp_pointer(app, app.canvas)
        self.assertTrue(app.pointer_over_chat_area())
        self.wheel(app, app.canvas, num=5)
        self.assertNotEqual(before, self.yview(app), "wheel over the chat area did not scroll")

        self.warp_pointer(app, app.input_text)
        self.assertFalse(app.pointer_over_chat_area(),
                         "the input area was misread as the chat area")
        settled = self.yview(app)
        self.wheel(app, app.input_text, num=5)
        self.assertEqual(settled, self.yview(app),
                         "a wheel event over the input area scrolled the chat")

    def test_wheel_over_a_message_bubble_scrolls_with_the_pointer_over_the_chat(self):
        app = self._app_with_content()
        bubbles = [widget for widget in app.canvas.winfo_children() if isinstance(widget, tk.Frame)]
        self.assertTrue(bubbles, "no message bubbles were rendered")
        inner = bubbles[0].winfo_children()[0] if bubbles[0].winfo_children() else bubbles[0]
        self.warp_pointer(app, app.canvas)
        self.assertTrue(app.pointer_over_chat_area())
        app.canvas.yview_moveto(0.3)
        app.update_idletasks()
        before = self.yview(app)
        self.wheel(app, inner, num=5)
        self.assertNotEqual(before, self.yview(app),
                            "wheel over a mapped message bubble did not scroll")

    def test_nested_settings_dialog_does_not_move_the_chat(self):
        app = self._app_with_content()
        app.canvas.yview_moveto(0.4)
        app.update_idletasks()
        before = self.yview(app)
        app.show_settings()
        app.update_idletasks()
        dialogs = [child for child in app.winfo_children() if isinstance(child, tk.Toplevel)]
        self.assertTrue(dialogs, "the settings dialog did not open")
        dialog = dialogs[-1]
        dialog.geometry("420x470+%d+%d" % (app.winfo_rootx() + 640, app.winfo_rooty() + 240))
        dialog.update()
        surface = dialog.winfo_children()[0] if dialog.winfo_children() else dialog
        self.warp_pointer(app, dialog)
        self.assertFalse(app.pointer_over_chat_area(),
                         "the settings dialog was misread as the chat area")
        self.wheel(app, surface, num=5)
        self.assertEqual(before, self.yview(app),
                         "scrolling inside the settings dialog moved the chat")
        dialog.destroy()

    def test_scrollbar_thumb_drag_round_trip(self):
        app = self._app_with_content()
        command = app.scrollbar.cget("command")
        self.assertTrue(command, "the scrollbar is not wired to the canvas")
        app.tk.call(command, "moveto", 0.55)
        app.update_idletasks()
        self.assertAlmostEqual(app.canvas.yview()[0], 0.55, places=3)
        self.assertAlmostEqual(app.scrollbar.get()[0], 0.55, places=3)

    @staticmethod
    def top_pixels(app):
        """Absolute canvas offset of the top of the viewport."""
        first = app.canvas.yview()[0]
        region = [float(value) for value in str(app.canvas.cget("scrollregion")).split()]
        return first * (region[3] - region[1])

    def test_resize_preserves_the_reading_position(self):
        app = self._app_with_content()
        app.canvas.yview_moveto(0.45)
        app.update_idletasks()
        self.assertLess(self.yview(app)[1], 0.999, "the test did not start mid-document")
        before_px = self.top_pixels(app)

        app.chat_data["messages"].append({"role": "ai", "content": "extra " + "z" * 300})
        with mock.patch.object(app, "after",
                               side_effect=lambda delay, func=None, *a: "job"):
            app.render_messages(preserve_scroll=True)
            app.update_scroll_region_only()
        app.update_idletasks()
        after_px = self.top_pixels(app)
        self.assertAlmostEqual(
            after_px, before_px, delta=2.0,
            msg=f"a resize moved the reader by {after_px - before_px:.1f}px")

    def test_resize_repins_to_the_bottom_when_already_at_the_bottom(self):
        app = self._app_with_content()
        app.canvas.yview_moveto(1.0)
        app.update_idletasks()
        self.assertGreaterEqual(self.yview(app)[1], 0.999)
        old_bottom = float(str(app.canvas.cget("scrollregion")).split()[3])

        app.chat_data["messages"].append({"role": "ai", "content": "newest " + "w" * 500})
        with mock.patch.object(app, "after",
                               side_effect=lambda delay, func=None, *a: "job"):
            app.render_messages()
            app.update_scroll_region()
        app.update_idletasks()
        new_bottom = float(str(app.canvas.cget("scrollregion")).split()[3])
        self.assertGreater(new_bottom, old_bottom, "the scrollregion did not grow")
        self.assertGreaterEqual(self.yview(app)[1], 0.999,
                                "the view no longer follows the newest message")


# --------------------------------------------------------------------------- #
# Gate 2b - binding idempotence and cleanup
# --------------------------------------------------------------------------- #
class ScrollBindingLifecycleTests(_GuiCase):
    def _app_with_content(self):
        app = self.make_app(mapped=True)
        self.render_without_timers(app, [
            {"role": "ai", "content": f"m{index} " + "lorem " * 20}
            for index in range(6)
        ])
        return app

    @staticmethod
    def handler_count(widget):
        return (widget.bind("<MouseWheel>") or "").count("handle_trackpad_scroll_binding")

    def test_rerender_does_not_duplicate_wheel_bindings(self):
        app = self._app_with_content()
        for _ in range(3):
            self.render_without_timers(app, app.chat_data["messages"])
        self.assertEqual(self.handler_count(app.canvas), 1)
        self.assertEqual(self.handler_count(app.chat_panel), 1)

    def test_theme_rebuild_does_not_duplicate_wheel_bindings(self):
        app = self._app_with_content()
        for theme in ("dark", "light", "dark"):
            app.config_data["theme"] = theme
            app.colors = local_ai.theme_palette(theme)
            app.rebuild_theme_colors()
            app.update_idletasks()
        self.assertEqual(self.handler_count(app.canvas), 1)
        self.assertEqual(self.handler_count(app.chat_panel), 1)

    def test_mode_switch_does_not_duplicate_wheel_bindings(self):
        app = self._app_with_content()
        with mock.patch.dict(_Harness.namespace(), {"save_config": mock.Mock()}), \
                mock.patch.object(messagebox, "showinfo"):
            app.toggle_mode()
            app.toggle_mode()
        app.update_idletasks()
        self.assertEqual(self.handler_count(app.canvas), 1)
        self.assertEqual(self.handler_count(app.chat_panel), 1)

    def test_dialog_close_releases_its_bindings(self):
        app = self._app_with_content()
        dialog = tk.Toplevel(app)
        canvas = tk.Canvas(dialog, bg="white")
        canvas.pack(fill="both", expand=True)
        app.bind_scroll_to_widget(canvas, canvas)
        self.assertIn(canvas, app.scroll_targets)
        dialog.destroy()
        app.unbind_scroll_widget(canvas)
        self.assertNotIn(canvas, app.scroll_targets,
                         "a destroyed dialog widget stayed registered")
        self.assertEqual(self.handler_count(app.canvas), 1,
                         "closing a dialog disturbed the chat bindings")

    def test_app_close_releases_every_binding(self):
        app = self._app_with_content()
        self.assertTrue(app.scroll_targets, "no scroll targets were registered")
        with mock.patch.object(app, "after_idle", side_effect=lambda func: None):
            app.close_app()
        self.assertEqual(app.scroll_targets, {}, "scroll targets survived app close")
        self.assertEqual(app.scroll_binding_ids, {})
        self.assertFalse(app.scroll_bindings_active)
        self.assertEqual(self.handler_count(app.canvas), 0,
                         "the chat canvas kept its wheel handler after close")
        self.assertFalse(app.tk.call("bind", "all", "<MouseWheel>"),
                         "a global wheel binding leaked")


# --------------------------------------------------------------------------- #
# Gate self-checks
# --------------------------------------------------------------------------- #
class GateSelfCheckTests(unittest.TestCase):
    GUARDED_SYMBOLS = (
        "def agent_available",
        "def refresh_agent_button",
        "def toggle_agent",
        "def ask_in_background",
        "def bind_scroll_to_widget",
        "def pointer_over_chat_area",
        "def handle_trackpad_scroll",
        "def update_scroll_region",
        "def rebuild_theme_colors",
        "class AgentSession",
    )

    def test_harness_extracts_the_production_chat_app(self):
        chat_app = _Harness.chat_app()
        for method in ("agent_available", "refresh_agent_button", "toggle_agent",
                       "ask_in_background", "handle_trackpad_scroll",
                       "pointer_over_chat_area", "update_scroll_region",
                       "rebuild_theme_colors", "send_message", "poll_cloud_reply"):
            with self.subTest(method=method):
                self.assertTrue(callable(getattr(chat_app, method, None)),
                                f"the extracted ChatApp lost {method}()")
        self.assertIs(_Harness.namespace().get("AgentSession"), local_ai.AgentSession)

    def test_gate_targets_still_exist_in_the_production_source(self):
        source = (DESKTOP_DIR / "local_ai.py").read_text(encoding="utf-8")
        for symbol in self.GUARDED_SYMBOLS:
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, source,
                              f"production no longer defines {symbol!r}; the gate is vacuous")

    def test_gate_module_is_imported_from_the_audited_desktop_package(self):
        self.assertEqual(Path(local_ai.__file__).resolve().parent, DESKTOP_DIR.resolve())


if __name__ == "__main__":
    unittest.main()
