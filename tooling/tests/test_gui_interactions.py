"""GUI interaction contracts for the LocalAI desktop app.

Four symptom classes are covered, all without pixel comparison:

1. isolated config + button commands (single invocation, no duplicate callbacks)
2. button state / theme contrast (no stale palette, readable state maps)
3. ttk Combobox colours in light and dark themes (normal / readonly / disabled / popdown)
4. mouse-wheel & trackpad scrolling (direction, bounds, hover ownership, nested dialogs,
   bind/unbind cleanup)

Plus keyboard activation and the requirement that theme switching never changes
Local/Cloud routing.

The production module ``local_ai.py`` is owned by another workstream, so these tests
only ever *read* it: the ``ChatApp``/``RoundedButton`` classes are extracted from
``run_chat_gui`` the same way ``test_cloud_host_integration`` does, and every source
contract is evaluated against the file as it exists at run time.

Second-pass additions (classes 6-9) lock the scrolling/slider and theme-propagation
behaviour: scrollbar thumb wiring and dragging, scrollregion growth after content
resize with resize debouncing, macOS/Windows/Linux wheel-delta normalisation, nested
widget scroll ownership, binding idempotence and destruction cleanup, palette
propagation to every button option, and Combobox style/popdown/readonly/disabled
propagation.

Regression checklist - these contracts were red against the first audited revision and
are green against the current one; keep them green:

* stale theme colours - ``test_theme_switch_rethemes_every_toolbar_button``
  (``agent_button`` / ``mode_button`` / ``add_file_button`` kept the old palette)
* themed Combobox - ``ComboboxThemeContractTests`` (no ``ttk.Style`` configuration,
  no readonly/disabled state map, no ``*TCombobox*Listbox`` popdown colours)
* scroll binding lifecycle - ``test_bind_scroll_to_widget_does_not_duplicate_handlers``,
  ``test_scroll_cleanup_helpers_are_not_no_ops``,
  ``test_scroll_cleanup_unbinds_the_chat_canvas``
* wheel ownership - ``test_wheel_over_a_foreign_widget_does_not_scroll_the_chat_canvas``

Do not weaken, skip or delete a contract to make a red test pass: a red test here is a
production defect report.
"""
from __future__ import annotations

import ast
import copy
import inspect
import queue
import re
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import local_ai


REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_PATH = REPO_ROOT / "projects" / "LocalAI" / "desktop" / "local_ai.py"

#: Buttons created in ``build_ui`` that must follow the active palette after a
#: theme switch.  ``send_button`` is state driven and asserted separately.
THEMED_TOOLBAR_BUTTONS = (
    "logo_button",
    "new_button",
    "history_button",
    "agent_button",
    "web_button",
    "settings_button",
    "mode_button",
    "add_file_button",
)

PALETTE_STATE_KEYS = (
    "window", "toolbar", "panel", "surface", "surface_hover", "input",
    "text", "muted", "border", "disabled", "web_on_bg", "web_on_fg",
)

#: WCAG AA contrast for normal text.
MIN_CONTRAST = 4.5


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
class GuiHarness:
    """Extract the production GUI classes without entering a mainloop."""

    _data = None

    @classmethod
    def data(cls):
        if cls._data is None:
            source = textwrap.dedent(inspect.getsource(local_ai.run_chat_gui))
            tree = ast.parse(source)
            selected = [
                node for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                and node.name in {"draw_round_rect", "RoundedButton", "ChatApp"}
            ]
            namespace = vars(local_ai).copy()
            namespace.update(
                {"tk": tk, "ttk": ttk, "filedialog": filedialog, "messagebox": messagebox}
            )
            # ``ChatApp`` is nested inside run_chat_gui's try block; compile only
            # the selected definitions so method bodies stay production code.
            exec(
                compile(ast.Module(body=selected, type_ignores=[]), "<GuiTestHarness>", "exec"),
                namespace,
            )
            cls._data = (namespace, tree, source)
        return cls._data

    @classmethod
    def namespace(cls):
        return cls.data()[0]

    @classmethod
    def chat_app(cls):
        return cls.namespace()["ChatApp"]

    @classmethod
    def rounded_button(cls):
        return cls.namespace()["RoundedButton"]


def _gui_func_source(name):
    _namespace, tree, source = GuiHarness.data()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    return ""


def _production_source():
    return PRODUCTION_PATH.read_text(encoding="utf-8")


def _relative_luminance(rgb):
    channels = []
    for raw in rgb:
        value = raw / 255.0
        channels.append(value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4)
    red, green, blue = channels
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


class GuiTestCase(unittest.TestCase):
    """Base case: isolated app-data directory, no real user data touched."""

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
        # A dedicated hidden root normalises colour specs; never the app under test.
        self._root = tk.Tk()
        self._root.withdraw()
        self.app = None
        self._extra_roots = []

    def tearDown(self):
        for widget in self._extra_roots:
            try:
                widget.destroy()
            except Exception:
                pass
        if self.app is not None:
            try:
                self.app.destroy()
            except Exception:
                pass
        try:
            self._root.destroy()
        except Exception:
            pass
        self.patches.stop()
        self._tmp.cleanup()

    # -- helpers -----------------------------------------------------------
    def config_for(self, mode="local", theme="light"):
        config = copy.deepcopy(local_ai.DEFAULT_CONFIG)
        config.update({"mode": mode, "theme": theme, "language": "en_us"})
        if mode == "cloud":
            config["cloud"] = {
                "provider": "openai_compatible",
                "default_model": "stub-model",
                "providers": {
                    "openai_compatible": {
                        "base_url": "http://127.0.0.1:9/v1",
                        "model": "stub-model",
                        "api_key": "",
                    }
                },
            }
        return local_ai.normalize_provider_config(config)

    def make_app(self, mode="local", theme="light"):
        app = GuiHarness.chat_app()(self.config_for(mode, theme), web_func=lambda *a, **k: "")
        app.withdraw()
        app.update_idletasks()
        self.app = app
        return app

    def make_toplevel(self):
        parent = self.app if self.app is not None else self._root
        widget = tk.Toplevel(parent)
        widget.withdraw()
        self._extra_roots.append(widget)
        return widget

    def rgb(self, color):
        """Normalise any Tk colour spec to an (r, g, b) tuple via Tk itself."""
        return self._root.winfo_rgb(color)

    def contrast(self, foreground, background):
        first = _relative_luminance(self.rgb(foreground))
        second = _relative_luminance(self.rgb(background))
        high, low = max(first, second), min(first, second)
        return (high + 0.05) / (low + 0.05)

    def widget_tree(self, root=None):
        root = root if root is not None else self.app
        for child in root.winfo_children():
            yield child
            yield from self.widget_tree(child)


# --------------------------------------------------------------------------- #
# 1. isolated config + button commands / single invocation
# --------------------------------------------------------------------------- #
class IsolatedConfigAndButtonCommandTests(GuiTestCase):
    def test_app_uses_the_isolated_config_not_the_real_user_data_dir(self):
        app = self.make_app()
        self.assertEqual(Path(local_ai.CONFIG_FILE).parent, self.root)
        self.assertNotIn(str(Path.home()), str(local_ai.CONFIG_FILE.replace(str(self.root), "")))
        self.assertEqual(app.config_data["language"], "en_us")

    def test_saving_from_the_gui_stays_inside_the_temporary_directory(self):
        app = self.make_app()
        app.config_data["theme"] = "dark"
        local_ai.save_config(app.config_data)
        written = sorted(p.name for p in self.root.iterdir())
        self.assertIn("config.json", written)
        self.assertTrue((self.root / "config.json").exists())

    def test_rounded_button_binds_exactly_one_click_handler(self):
        app = self.make_app()
        calls = []
        button = GuiHarness.rounded_button()(app, text="go", command=lambda: calls.append(1))
        script = button.bind("<Button-1>")
        self.assertEqual(script.count("on_click"), 1,
                         f"duplicate click binding detected: {script!r}")

    def test_rounded_button_runs_its_command_once_per_activation(self):
        app = self.make_app()
        calls = []
        button = GuiHarness.rounded_button()(app, text="go", command=lambda: calls.append(1))
        for _ in range(3):
            button.on_click()
        self.assertEqual(len(calls), 3)

    def test_disabled_rounded_button_ignores_activation(self):
        app = self.make_app()
        calls = []
        button = GuiHarness.rounded_button()(
            app, text="go", command=lambda: calls.append(1), state="disabled"
        )
        button.on_click()
        self.assertEqual(calls, [])

    def test_flat_button_binds_exactly_one_click_handler(self):
        app = self.make_app()
        calls = []
        widget = app.flat_button(app, "save", lambda: calls.append(1))
        script = widget.bind("<Button-1>")
        self.assertEqual(len(re.findall(r"lambda", script)), 1,
                         f"duplicate click binding detected: {script!r}")

    def test_theme_rebuild_does_not_add_click_handlers(self):
        app = self.make_app()
        before = {
            name: (getattr(app, name).bind("<Button-1>") or "").count("on_click")
            for name in ("logo_button", "new_button", "settings_button")
        }
        app.colors = local_ai.theme_palette("dark")
        app.config_data["theme"] = "dark"
        app.rebuild_theme_colors()
        after = {
            name: (getattr(app, name).bind("<Button-1>") or "").count("on_click")
            for name in ("logo_button", "new_button", "settings_button")
        }
        self.assertEqual(before, after, "theme rebuild changed the click handler count")


# --------------------------------------------------------------------------- #
# 2. button state / theme contrast / stale palette
# --------------------------------------------------------------------------- #
class ButtonStateAndThemeContrastTests(GuiTestCase):
    def test_palette_defines_every_button_state_key_in_both_themes(self):
        for theme in ("light", "dark"):
            palette = local_ai.theme_palette(theme)
            for key in PALETTE_STATE_KEYS:
                self.assertIn(key, palette, f"{theme} palette is missing {key!r}")

    def test_button_state_maps_are_readable_in_both_themes(self):
        for theme in ("light", "dark"):
            palette = local_ai.theme_palette(theme)
            pairs = {
                "surface/text": (palette["surface"], palette["text"]),
                "surface_hover/text": (palette["surface_hover"], palette["text"]),
                "disabled/text": (palette["disabled"], palette["text"]),
                "web_on_bg/web_on_fg": (palette["web_on_bg"], palette["web_on_fg"]),
                "input/text": (palette["input"], palette["text"]),
            }
            for label, (background, foreground) in pairs.items():
                ratio = self.contrast(foreground, background)
                self.assertGreaterEqual(
                    ratio, MIN_CONTRAST,
                    f"{theme} {label} contrast {ratio:.2f} is below {MIN_CONTRAST}",
                )

    def test_disabled_palette_entry_is_consumed_by_the_send_button(self):
        source = _gui_func_source("update_send_button_state")
        self.assertIn('colors["disabled"]', source,
                      "the disabled state colour is never applied to the send button")

    def test_theme_switch_rethemes_every_toolbar_button(self):
        app = self.make_app(theme="light")
        app.config_data["theme"] = "dark"
        app.colors = local_ai.theme_palette("dark")
        app.rebuild_theme_colors()
        app.update_idletasks()

        stale = {}
        for name in THEMED_TOOLBAR_BUTTONS:
            button = getattr(app, name, None)
            if button is None:
                continue
            if button.button_bg != app.colors["surface"]:
                stale[name] = button.button_bg
        self.assertEqual(
            stale, {},
            "theme switch left stale palette colours on: "
            f"{stale} (expected surface {app.colors['surface']})",
        )

    def test_send_button_state_colours_match_the_active_theme(self):
        app = self.make_app(theme="dark")
        app.config_data["theme"] = "dark"
        app.colors = local_ai.theme_palette("dark")
        app.rebuild_theme_colors()
        app.update_idletasks()
        allowed = {app.colors["disabled"], "#2563eb"}
        self.assertIn(app.send_button.button_bg, allowed,
                      f"send button kept a foreign colour {app.send_button.button_bg!r}")

    def test_disabled_send_button_uses_the_disabled_state_colour(self):
        app = self.make_app(theme="dark")
        app.input_text.delete("1.0", "end")
        app.send_button_enabled = None
        app.update_send_button_state()
        app.update_idletasks()
        self.assertEqual(app.send_button.button_bg, app.colors["disabled"])


# --------------------------------------------------------------------------- #
# 3. ttk Combobox colours (light & dark)
# --------------------------------------------------------------------------- #
class ComboboxThemeContractTests(GuiTestCase):
    def test_ttk_combobox_style_is_configured(self):
        source = _production_source()
        self.assertTrue(
            "ttk.Style(" in source,
            "no ttk.Style() is configured, so Combobox colours follow the "
            "native macOS theme instead of the app palette",
        )
        missing = [option for option in ("fieldbackground", "foreground", "background")
                   if option not in source]
        self.assertEqual(missing, [], f"the ttk Combobox style never sets {missing}")

    def test_combobox_popdown_listbox_colours_are_configured(self):
        source = _production_source()
        missing = [key for key in ("*TCombobox*Listbox.background", "*TCombobox*Listbox.foreground")
                   if key not in source]
        self.assertEqual(missing, [], f"the Combobox popdown never sets {missing}")

    def test_combobox_style_is_reapplied_on_theme_change(self):
        app = self.make_app(theme="light")
        style = ttk.Style(app)
        light_field = style.lookup("TCombobox", "fieldbackground")
        app.config_data["theme"] = "dark"
        app.colors = local_ai.theme_palette("dark")
        app.rebuild_theme_colors()
        app.update_idletasks()
        dark_field = style.lookup("TCombobox", "fieldbackground")
        self.assertEqual(
            dark_field, app.colors["input"],
            "the Combobox field colour did not follow an in-app theme switch",
        )
        self.assertNotEqual(light_field, dark_field,
                            "the Combobox style never changed between light and dark")

    def test_combobox_field_colours_track_the_active_palette(self):
        for theme in ("light", "dark"):
            app = self.make_app(theme=theme)
            style = ttk.Style(app)
            palette = app.colors
            self.assertEqual(
                style.lookup("TCombobox", "fieldbackground"), palette["input"],
                f"{theme}: Combobox field background does not match the palette input colour",
            )
            self.assertEqual(
                style.lookup("TCombobox", "foreground"), palette["text"],
                f"{theme}: Combobox foreground does not match the palette text colour",
            )
            app.destroy()
            self.app = None

    def test_combobox_readonly_and_disabled_maps_are_defined(self):
        app = self.make_app()
        style = ttk.Style(app)
        for option in ("fieldbackground", "foreground"):
            state_map = style.map("TCombobox", option)
            self.assertTrue(
                state_map,
                f"TCombobox {option!r} has no readonly/disabled state map "
                "(readonly and disabled fields would inherit the native colours)",
            )


# --------------------------------------------------------------------------- #
# 4. mouse-wheel / trackpad scrolling
# --------------------------------------------------------------------------- #
class _WheelEvent:
    def __init__(self, num=None, delta=0, widget=None):
        self.num = num
        self.delta = delta
        self.widget = widget


class ScrollingContractTests(GuiTestCase):
    def _fresh(self, app):
        # Bypass the anti-rebound throttle so one event == one scroll request.
        app.last_trackpad_scroll_time = 0.0

    def _record_scrolls(self, app):
        recorded = []
        app.canvas.yview_scroll = lambda units, what: recorded.append((units, what))
        return recorded

    def test_button4_and_button5_map_to_opposite_directions(self):
        app = self.make_app()
        recorded = self._record_scrolls(app)
        # Settle the deferred scrollregion job once and start each direction from a
        # mid-document position.  Otherwise the first wheel event consumes the pending
        # job, which re-pins the view to the bottom bound and vetoes the second scroll,
        # hiding the direction mapping this test exists to pin down.
        app.update_scroll_region()
        app.update_idletasks()
        for num in (4, 5):
            app.scroll_region_job = None
            app.canvas.yview_moveto(0.5)
            app.update_idletasks()
            self._fresh(app)
            app.handle_trackpad_scroll(_WheelEvent(num=num, widget=app.canvas))
        self.assertEqual(len(recorded), 2, f"expected two scrolls, got {recorded}")
        self.assertLess(recorded[0][0], 0, "Button-4 (scroll up) must be negative")
        self.assertGreater(recorded[1][0], 0, "Button-5 (scroll down) must be positive")

    def test_wheel_delta_direction_matches_the_platform(self):
        app = self.make_app()
        recorded = self._record_scrolls(app)
        app.update_scroll_region()
        app.update_idletasks()
        for system, positive, negative in (
            ("Darwin", -1, 1),
            ("Linux", -1, 1),
        ):
            with mock.patch.object(local_ai.platform, "system", return_value=system):
                recorded.clear()
                # One event per direction, each from a settled mid-document view, so the
                # first event cannot consume the pending scrollregion job and pin the
                # view to the bottom before the opposite delta is applied.
                for delta in (120, -120):
                    app.scroll_region_job = None
                    app.canvas.yview_moveto(0.5)
                    app.update_idletasks()
                    self._fresh(app)
                    app.handle_trackpad_scroll(_WheelEvent(delta=delta, widget=app.canvas))
                self.assertEqual(len(recorded), 2, f"{system}: {recorded}")
                self.assertLess(recorded[0][0], 0, f"{system}: delta>0 must scroll up")
                self.assertGreater(recorded[1][0], 0, f"{system}: delta<0 must scroll down")

    def test_wheel_does_not_scroll_beyond_the_canvas_bounds(self):
        app = self.make_app()
        # Settle the deferred scrollregion job before sampling: otherwise the first
        # wheel event performs that recompute, which legitimately re-pins the view and
        # invalidates the sample without any scroll having happened.
        app.update_scroll_region()
        app.update_idletasks()
        before = app.canvas.yview()
        for _ in range(5):
            self._fresh(app)
            app.handle_trackpad_scroll(_WheelEvent(num=5, widget=app.canvas))
        app.update_idletasks()
        after = app.canvas.yview()
        self.assertGreaterEqual(after[0], 0.0)
        self.assertLessEqual(after[1], 1.0)
        self.assertEqual(before, after, "an empty chat canvas moved outside its bounds")
        self.assertEqual(app.scroll_remainders.get(app.canvas, 0), 0,
                         "a refused scroll must not bank a fractional remainder")

    def test_bind_scroll_to_widget_does_not_duplicate_handlers(self):
        app = self.make_app()
        app.bind_scroll_to_widget(app.canvas)
        app.bind_scroll_to_widget(app.canvas)
        script = app.canvas.bind("<MouseWheel>") or ""
        self.assertEqual(
            script.count("handle_trackpad_scroll"), 1,
            f"bind_scroll_to_widget accumulates handlers: {script!r}",
        )

    def test_wheel_over_a_foreign_widget_does_not_scroll_the_chat_canvas(self):
        app = self.make_app()
        dialog = self.make_toplevel()
        dialog_canvas = tk.Canvas(dialog)
        recorded = self._record_scrolls(app)
        with mock.patch.object(app, "pointer_over_chat_area", return_value=False):
            self._fresh(app)
            app.handle_trackpad_scroll(_WheelEvent(num=5, widget=dialog_canvas))
        self.assertEqual(
            recorded, [],
            "a wheel event over another widget scrolled the chat canvas",
        )

    def test_wheel_over_the_chat_area_still_scrolls(self):
        app = self.make_app()
        # Ownership is decided by the widget chain, so the canvas has to be a
        # registered scroll surface, and a downward wheel is only legitimate while the
        # view is off the bottom bound.
        self.assertIn(app.canvas, app.scroll_targets,
                      "the chat canvas is no longer a registered scroll surface")
        app.update_scroll_region()
        app.update_idletasks()
        app.canvas.yview_moveto(0.0)
        app.update_idletasks()
        recorded = self._record_scrolls(app)
        with mock.patch.object(app, "pointer_over_chat_area", return_value=True):
            self._fresh(app)
            app.handle_trackpad_scroll(_WheelEvent(num=5, widget=app.canvas))
        self.assertEqual(len(recorded), 1, "the chat canvas ignored a legitimate wheel event")

    def test_no_global_wheel_binding_is_installed(self):
        app = self.make_app()
        script = app.tk.call("bind", "all", "<MouseWheel>")
        self.assertFalse(script, f"a global bind_all wheel handler leaked: {script!r}")

    def test_source_has_no_unbalanced_bind_all(self):
        source = _production_source()
        self.assertEqual(
            source.count("bind_all("), source.count("unbind_all("),
            "bind_all is used without a matching unbind_all, which leaks past destroyed widgets",
        )

    def test_scroll_cleanup_unbinds_the_chat_canvas(self):
        app = self.make_app()
        app.disable_chat_scroll_bindings_if_needed()
        script = app.canvas.bind("<MouseWheel>") or ""
        self.assertNotIn(
            "handle_trackpad_scroll", script,
            "disable_chat_scroll_bindings_if_needed did not release the canvas binding",
        )

    def test_scroll_cleanup_helpers_are_not_no_ops(self):
        for name in ("schedule_disable_chat_scroll_bindings",
                     "disable_chat_scroll_bindings_if_needed"):
            body = _gui_func_source(name)
            self.assertTrue(body, f"{name} is missing from ChatApp")
            work = [line.strip() for line in body.splitlines()[1:] if line.strip()]
            self.assertFalse(
                work and all(line.startswith("return") for line in work),
                f"{name} is a no-op, so scroll bindings can never be released",
            )

    def test_repeated_dialog_open_close_does_not_accumulate_scroll_handlers(self):
        app = self.make_app()
        baseline = (app.canvas.bind("<MouseWheel>") or "").count("handle_trackpad_scroll")
        for _ in range(3):
            app.show_settings()
            app.update_idletasks()
            for child in list(app.winfo_children()):
                if isinstance(child, tk.Toplevel):
                    child.destroy()
        app.update_idletasks()
        after = (app.canvas.bind("<MouseWheel>") or "").count("handle_trackpad_scroll")
        self.assertEqual(baseline, after, "opening dialogs leaked extra scroll handlers")
        self.assertFalse(app.tk.call("bind", "all", "<MouseWheel>"))


# --------------------------------------------------------------------------- #
# 5. keyboard activation and theme switching must not break routing
# --------------------------------------------------------------------------- #
class KeyboardAndRoutingTests(GuiTestCase):
    def _press_enter(self, app):
        sent = []
        with mock.patch.object(app, "send_message", side_effect=lambda: sent.append(1)):
            result = app.handle_enter(None)
        return result, sent

    def test_enter_sends_exactly_one_message_in_local_mode(self):
        app = self.make_app(mode="local")
        result, sent = self._press_enter(app)
        self.assertEqual(sent, [1], "keyboard activation sent the message more than once")
        self.assertEqual(result, "break")

    def test_enter_sends_exactly_one_message_in_cloud_mode(self):
        app = self.make_app(mode="cloud")
        result, sent = self._press_enter(app)
        self.assertEqual(sent, [1], "keyboard activation sent the message more than once")
        self.assertEqual(result, "break")

    def test_enter_after_a_theme_switch_is_still_single(self):
        app = self.make_app(mode="cloud", theme="light")
        app.config_data["theme"] = "dark"
        app.colors = local_ai.theme_palette("dark")
        app.rebuild_theme_colors()
        app.update_idletasks()
        _result, sent = self._press_enter(app)
        self.assertEqual(sent, [1], "a theme switch duplicated keyboard activation")

    def test_theme_switch_preserves_local_and_cloud_routing(self):
        for mode in ("local", "cloud"):
            app = self.make_app(mode=mode, theme="light")
            app.config_data["theme"] = "dark"
            app.colors = local_ai.theme_palette("dark")
            app.rebuild_theme_colors()
            app.update_idletasks()

            self.assertEqual(app.config_data["mode"], mode,
                             "theme switch changed the Local/Cloud mode")

            config = copy.deepcopy(app.config_data)
            if mode == "cloud":
                with mock.patch.object(local_ai, "ask_cloud", return_value="cloud") as cloud, \
                        mock.patch.object(local_ai, "ask_ollama",
                                          side_effect=AssertionError("no local fallback")):
                    self.assertEqual(
                        local_ai.ask_model([{"role": "user", "content": "hi"}], "m", config),
                        "cloud",
                    )
                    cloud.assert_called_once()
            else:
                with mock.patch.object(local_ai, "ask_ollama", return_value="local") as ollama:
                    self.assertEqual(
                        local_ai.ask_model([{"role": "user", "content": "hi"}], "m", config),
                        "local",
                    )
                    ollama.assert_called_once()
            app.destroy()
            self.app = None

    def test_mode_label_is_unchanged_by_a_theme_switch(self):
        app = self.make_app(mode="local")
        before = app.mode_label()
        app.colors = local_ai.theme_palette("dark")
        app.rebuild_theme_colors()
        app.update_idletasks()
        self.assertEqual(app.mode_label(), before)

    def test_shift_enter_inserts_a_newline_without_sending(self):
        app = self.make_app()
        app.input_text.delete("1.0", "end")
        result = app.handle_shift_enter(None)
        self.assertEqual(result, "break")
        self.assertEqual(app.input_text.get("1.0", "end-1c"), "\n")


# --------------------------------------------------------------------------- #
# 6. scrollbar / scrollregion acceptance (second-pass audit)
# --------------------------------------------------------------------------- #
class _ResizeEvent:
    def __init__(self, width, height):
        self.width = width
        self.height = height


class ScrollbarAndScrollRegionTests(GuiTestCase):
    def _tall_canvas(self, app, bottom=8000):
        app.canvas.configure(scrollregion=(0, 0, 600, bottom))
        app.update_idletasks()

    def test_scrollbar_command_is_wired_to_the_canvas(self):
        app = self.make_app()
        command = app.scrollbar.cget("command")
        self.assertTrue(command, "the scrollbar has no command, so its thumb cannot drag")
        self.assertIn("yview", str(command))

    def test_canvas_yscrollcommand_is_wired_back_to_the_scrollbar(self):
        app = self.make_app()
        self.assertTrue(app.canvas.cget("yscrollcommand"),
                        "the canvas never reports its view back to the scrollbar")

    def test_thumb_drag_moves_the_canvas_and_the_thumb(self):
        app = self.make_app()
        self._tall_canvas(app)
        # The scrollbar's own command is exactly what a thumb drag invokes.
        command = app.scrollbar.cget("command")
        app.tk.call(command, "moveto", 0.5)
        app.update_idletasks()
        self.assertAlmostEqual(app.canvas.yview()[0], 0.5, places=3)
        self.assertAlmostEqual(app.scrollbar.get()[0], 0.5, places=3)

    def test_canvas_scroll_updates_the_thumb_position(self):
        app = self.make_app()
        self._tall_canvas(app)
        app.canvas.yview_moveto(0.4)
        app.update_idletasks()
        first, last = app.scrollbar.get()
        self.assertAlmostEqual(first, 0.4, places=3)
        self.assertLess(first, last)

    def test_scrollregion_grows_with_content_and_pins_to_the_bottom(self):
        app = self.make_app()
        app.content_height = 4000
        app.update_scroll_region()
        app.update_idletasks()
        region = [float(value) for value in str(app.canvas.cget("scrollregion")).split()]
        self.assertGreaterEqual(region[3], 4000, "scrollregion ignored the new content height")
        self.assertAlmostEqual(app.canvas.yview()[1], 1.0, places=3)

    def test_preserve_scroll_updates_the_region_without_jumping(self):
        app = self.make_app()
        app.content_height = 4000
        # Consume the render-time "pin to the newest message" restore first: while that
        # restore is still pending, preserving the position *means* staying at the
        # bottom, so the reader has to be moved off it before this test can mean
        # anything.
        app.update_scroll_region()
        app.update_idletasks()
        app.canvas.yview_moveto(0.4)
        app.update_idletasks()
        before = app.canvas.yview()[0]
        app.update_scroll_region_only()
        app.update_idletasks()
        region = [float(value) for value in str(app.canvas.cget("scrollregion")).split()]
        self.assertGreaterEqual(region[3], 4000)
        self.assertLess(app.canvas.yview()[1], 1.0,
                        "the preserve-scroll path jumped the view to the bottom")
        self.assertAlmostEqual(app.canvas.yview()[0], before, places=3,
                               msg="the preserve-scroll path moved the reader")

    def test_tiny_resizes_do_not_schedule_a_deferred_resize(self):
        app = self.make_app()
        app.last_canvas_width, app.last_canvas_height = 800, 600
        app.message_resize_job = None
        scheduled = []
        with mock.patch.object(
            app, "after", side_effect=lambda delay, fn=None, *a: (scheduled.append(delay), "job")[1]
        ):
            app.resize_messages_window(_ResizeEvent(804, 604))
            self.assertEqual(scheduled, [], "a sub-threshold resize scheduled deferred work")
            app.resize_messages_window(_ResizeEvent(900, 700))
        self.assertEqual(scheduled, [160], f"expected exactly one deferred resize, got {scheduled}")

    def test_successive_resizes_debounce_into_one_pending_job(self):
        app = self.make_app()
        app.last_canvas_width, app.last_canvas_height = 100, 100
        app.message_resize_job = None
        scheduled, cancelled = [], []
        app.after_cancel = lambda job: cancelled.append(job)
        with mock.patch.object(
            app, "after", side_effect=lambda delay, fn=None, *a: (scheduled.append(delay), "job")[1]
        ):
            app.resize_messages_window(_ResizeEvent(900, 700))
            app.resize_messages_window(_ResizeEvent(1000, 800))
        self.assertEqual(scheduled, [160, 160])
        self.assertEqual(cancelled, ["job"], "the superseded resize job was not cancelled")


# --------------------------------------------------------------------------- #
# 7. scroll binding idempotence and destruction cleanup
# --------------------------------------------------------------------------- #
class ScrollBindingLifecycleTests(GuiTestCase):
    def _record_chat_scrolls(self, app):
        recorded = []
        app.canvas.yview_scroll = lambda units, what: recorded.append((units, what))
        return recorded

    def test_binding_records_one_entry_per_sequence(self):
        app = self.make_app()
        app.bind_scroll_to_widget(app.canvas, app.canvas)
        entries = app.scroll_binding_ids[app.canvas]
        self.assertEqual(sorted(sequence for sequence, _cid in entries),
                         ["<Button-4>", "<Button-5>", "<Enter>", "<MouseWheel>"])
        self.assertIs(app.scroll_targets[app.canvas], app.canvas)

    def test_rebinding_a_widget_replaces_its_callbacks(self):
        app = self.make_app()
        first = [cid for _sequence, cid in app.scroll_binding_ids[app.canvas]]
        app.bind_scroll_to_widget(app.canvas, app.canvas)
        second = [cid for _sequence, cid in app.scroll_binding_ids[app.canvas]]
        self.assertEqual(len(first), len(second))
        self.assertNotEqual(first, second, "rebinding reused the previous callbacks")
        script = app.canvas.bind("<MouseWheel>") or ""
        self.assertEqual(script.count("handle_trackpad_scroll_binding"), 1)

    def test_rerendering_messages_does_not_accumulate_targets(self):
        app = self.make_app()
        app.chat_data["messages"] = [
            {"role": "user", "content": "a"},
            {"role": "ai", "content": "b"},
        ]
        app.render_messages()
        app.update_idletasks()
        baseline = len(app.scroll_binding_ids)
        self.assertGreater(baseline, 2, "message bubbles were not registered as scroll targets")
        for _ in range(3):
            app.render_messages()
            app.update_idletasks()
        self.assertEqual(len(app.scroll_binding_ids), baseline,
                         "re-rendering accumulated scroll targets")
        dead = [widget for widget in app.scroll_binding_ids if not widget.winfo_exists()]
        self.assertEqual(dead, [], "destroyed message widgets are still tracked as scroll targets")

    def test_nested_widget_scrolls_its_own_target_not_the_chat_canvas(self):
        app = self.make_app()
        dialog = self.make_toplevel()
        inner = tk.Canvas(dialog, bg="white")
        inner.configure(scrollregion=(0, 0, 200, 4000))
        inner_calls = []
        inner.yview_scroll = lambda units, what: inner_calls.append((units, what))
        chat_calls = self._record_chat_scrolls(app)
        app.bind_scroll_to_widget(inner, inner)
        app.last_trackpad_scroll_time = 0.0
        app.handle_trackpad_scroll(_WheelEvent(num=5, widget=inner), inner)
        self.assertEqual(len(inner_calls), 1, "the nested view never scrolled")
        self.assertEqual(chat_calls, [], "a nested widget's wheel event scrolled the chat canvas")

    def test_unbind_releases_bookkeeping_and_handlers(self):
        app = self.make_app()
        app.unbind_scroll_widgets()
        self.assertEqual(app.scroll_binding_ids, {})
        self.assertEqual(app.scroll_targets, {})
        self.assertFalse(app.scroll_bindings_active)
        for widget in (app.canvas, app.chat_panel):
            count = (widget.bind("<MouseWheel>") or "").count("handle_trackpad_scroll_binding")
            self.assertEqual(count, 0, f"{widget} kept its wheel handler after unbind")

    def test_unbinding_one_widget_keeps_the_others(self):
        app = self.make_app()
        app.unbind_scroll_widget(app.canvas)
        self.assertNotIn(app.canvas, app.scroll_binding_ids)
        self.assertIn(app.chat_panel, app.scroll_binding_ids)
        self.assertEqual((app.canvas.bind("<MouseWheel>") or "").count("handle_trackpad_scroll_binding"), 0)
        self.assertEqual((app.chat_panel.bind("<MouseWheel>") or "").count("handle_trackpad_scroll_binding"), 1)

    def test_enable_rebinds_after_unbind(self):
        app = self.make_app()
        app.unbind_scroll_widgets()
        app.enable_chat_scroll_bindings()
        self.assertTrue(app.scroll_bindings_active)
        self.assertEqual((app.canvas.bind("<MouseWheel>") or "").count("handle_trackpad_scroll_binding"), 1)
        self.assertIs(app.scroll_targets.get(app.canvas), app.canvas)

    def test_disable_helper_releases_every_target(self):
        app = self.make_app()
        app.disable_chat_scroll_bindings_if_needed()
        self.assertEqual(app.scroll_binding_ids, {})
        self.assertEqual(app.scroll_targets, {})

    def test_close_app_releases_scroll_bindings(self):
        app = self.make_app()
        with mock.patch.object(app, "after_idle", side_effect=lambda fn: None):
            app.close_app()
        self.assertTrue(app.closing)
        self.assertEqual(app.scroll_binding_ids, {})
        self.assertFalse(app.scroll_bindings_active)
        self.assertFalse(app.tk.call("bind", "all", "<MouseWheel>"))


# --------------------------------------------------------------------------- #
# 8. theme propagation to every button state and every Combobox
# --------------------------------------------------------------------------- #
class ThemePropagationTests(GuiTestCase):
    BUTTONS = (
        "logo_button", "new_button", "history_button", "agent_button",
        "web_button", "settings_button", "mode_button", "add_file_button",
    )

    def test_every_button_option_follows_the_theme(self):
        app = self.make_app(theme="light")
        app.config_data["theme"] = "dark"
        app.colors = local_ai.theme_palette("dark")
        app.rebuild_theme_colors()
        app.update_idletasks()
        palette = app.colors
        for name in self.BUTTONS:
            button = getattr(app, name, None)
            if button is None:
                continue
            self.assertEqual(button.button_bg, palette["surface"], name)
            self.assertEqual(button.fg, palette["text"], name)
            self.assertEqual(button.active_bg, palette["surface_hover"], name)
            self.assertEqual(button.pressed_bg, palette.get("border", palette["surface_hover"]), name)
            self.assertEqual(button.disabled_bg, palette.get("disabled", palette["surface"]), name)
            self.assertEqual(button.disabled_fg, palette.get("muted", palette["text"]), name)
            self.assertEqual(button.outline, palette.get("glass_border", palette["surface"]), name)

    def test_style_combobox_installs_the_themed_style_and_postcommand(self):
        app = self.make_app()
        combo = ttk.Combobox(app, values=["a", "b"], state="readonly")
        app.style_combobox(combo)
        self.assertEqual(str(combo.cget("style")), "LocalAI.TCombobox")
        self.assertTrue(combo.cget("postcommand"),
                        "no postcommand, so the popdown listbox is never restyled")

    def test_theme_switch_restyles_an_existing_combobox(self):
        app = self.make_app(theme="light")
        combo = ttk.Combobox(app, values=["a", "b"], state="readonly")
        app.style_combobox(combo)
        app.config_data["theme"] = "dark"
        app.colors = local_ai.theme_palette("dark")
        app.rebuild_theme_colors()
        app.update_idletasks()
        style = ttk.Style(app)
        self.assertEqual(style.lookup("LocalAI.TCombobox", "fieldbackground"), app.colors["input"])
        self.assertEqual(style.lookup("LocalAI.TCombobox", "foreground"), app.colors["text"])

    def test_combobox_readonly_and_disabled_maps_track_the_theme(self):
        for theme in ("light", "dark"):
            app = self.make_app(theme=theme)
            style = ttk.Style(app)
            field = dict(style.map("LocalAI.TCombobox", "fieldbackground"))
            self.assertEqual(field.get("readonly"), app.colors["input"], theme)
            self.assertEqual(field.get("disabled"), app.colors["disabled"], theme)
            app.destroy()
            self.app = None

    def test_combobox_popdown_listbox_tracks_the_theme(self):
        app = self.make_app(theme="dark")
        combo = ttk.Combobox(app, values=["a", "b"], state="readonly")
        app.style_combobox(combo)
        app.update_idletasks()
        popdown = app.tk.call("ttk::combobox::PopdownWindow", combo._w)
        listbox = f"{popdown}.f.l"
        self.assertEqual(str(app.tk.call(listbox, "cget", "-background")), app.colors["input"])
        self.assertEqual(str(app.tk.call(listbox, "cget", "-foreground")), app.colors["text"])
        self.assertEqual(str(app.tk.call(listbox, "cget", "-selectbackground")), app.colors["user_bubble"])


# --------------------------------------------------------------------------- #
# 9. button activation state machine (mouse + keyboard, single invocation)
# --------------------------------------------------------------------------- #
class ButtonActivationStateTests(GuiTestCase):
    def _button(self, app, **kwargs):
        calls = []
        button = GuiHarness.rounded_button()(app, text="go", command=lambda: calls.append(1), **kwargs)
        return button, calls

    def test_keyboard_press_and_release_invokes_once(self):
        app = self.make_app()
        button, calls = self._button(app)
        button.on_key_press()
        button.on_key_release()
        self.assertEqual(calls, [1], "keyboard activation fired the command more than once")

    def test_keyboard_release_without_press_does_not_invoke(self):
        app = self.make_app()
        button, calls = self._button(app)
        button.on_key_release()
        self.assertEqual(calls, [])

    def test_disabled_button_ignores_keyboard_activation(self):
        app = self.make_app()
        button, calls = self._button(app, state="disabled")
        button.on_key_press()
        button.on_key_release()
        self.assertEqual(calls, [])

    def test_mouse_press_and_release_invokes_exactly_once(self):
        app = self.make_app()
        button, calls = self._button(app)
        button.on_click()
        button.on_release()
        self.assertEqual(calls, [1], "press+release fired the command twice")

    def test_each_keyboard_cycle_invokes_once(self):
        app = self.make_app()
        button, calls = self._button(app)
        for _ in range(3):
            button.on_key_press()
            button.on_key_release()
        self.assertEqual(len(calls), 3)

    def test_invoke_respects_the_disabled_state(self):
        app = self.make_app()
        button, calls = self._button(app, state="disabled")
        button.invoke()
        self.assertEqual(calls, [])


# --------------------------------------------------------------------------- #
# 10. shutdown/cancellation during streaming
# --------------------------------------------------------------------------- #
class StreamingShutdownTests(GuiTestCase):
    def _fake_local(self, chunks, answer="late answer"):
        def fake_ask_local(prompt, model, size, image_paths=None, on_chunk=None):
            for chunk in chunks:
                if on_chunk:
                    on_chunk(chunk)
            return answer
        return fake_ask_local

    def _bound_worker(self, app):
        app.ask_in_background = GuiHarness.chat_app().ask_in_background.__get__(app)
        app.cloud_reply_queue = queue.Queue()
        return app

    def test_worker_emits_chunks_and_terminal_event_while_open(self):
        app = self._bound_worker(self.make_app())
        app.closing = False
        with mock.patch.dict(GuiHarness.namespace(), {"ask_local": self._fake_local(["a", "b"])}):
            app.ask_in_background("hello", [])
        events = []
        while not app.cloud_reply_queue.empty():
            events.append(app.cloud_reply_queue.get_nowait())
        self.assertEqual(events, [("chunk", "a"), ("chunk", "b"), ("done", "late answer")])

    def test_worker_suppresses_every_event_after_close(self):
        app = self._bound_worker(self.make_app())
        app.closing = True
        with mock.patch.dict(GuiHarness.namespace(), {"ask_local": self._fake_local(["a", "b"])}):
            app.ask_in_background("hello", [])
        self.assertTrue(app.cloud_reply_queue.empty(),
                        "the worker queued UI events after the window started closing")

    def test_worker_suppresses_events_when_closed_mid_stream(self):
        app = self._bound_worker(self.make_app())
        app.closing = False
        seen = []

        def fake_ask_local(prompt, model, size, image_paths=None, on_chunk=None):
            on_chunk("first")
            app.closing = True          # the window closes while the stream is open
            on_chunk("second")
            return "late answer"

        with mock.patch.dict(GuiHarness.namespace(), {"ask_local": fake_ask_local}):
            app.ask_in_background("hello", [])
        while not app.cloud_reply_queue.empty():
            seen.append(app.cloud_reply_queue.get_nowait())
        self.assertEqual(seen, [("chunk", "first")],
                         "events kept flowing after the window closed mid-stream")
        self.assertNotIn(("done", "late answer"), seen,
                         "the terminal event was queued after the window closed")


if __name__ == "__main__":
    unittest.main()
