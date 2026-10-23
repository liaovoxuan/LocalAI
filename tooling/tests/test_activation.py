import ast
import copy
import inspect
import queue
import textwrap
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import local_ai
from local_ai import (
    DEFAULT_CONFIG,
    EDITION_PRO,
    EDITION_ULTRA,
    apply_activation_config,
    normalize_provider,
    validate_activation_code,
)


class ActivationTests(unittest.TestCase):
    def test_activation_code_rule(self):
        self.assertTrue(validate_activation_code(EDITION_ULTRA, "9999990"))
        self.assertTrue(validate_activation_code(EDITION_PRO, "8888886"))
        self.assertTrue(validate_activation_code("unknown", "9999999"))
        self.assertTrue(validate_activation_code("", "999999"))
        self.assertTrue(validate_activation_code(None, "abcdefg"))

    def test_activation_config_normalizes_to_current_runtime(self):
        config = DEFAULT_CONFIG.copy()
        config.update({"edition": EDITION_PRO, "activation_code": "1234567"})
        apply_activation_config(config)
        self.assertEqual(config["edition"], EDITION_ULTRA)
        self.assertEqual(config["activation_code"], "")

    def test_activation_config_clears_code_for_all_inputs(self):
        for edition, code in ((EDITION_PRO, "8888886"), (EDITION_ULTRA, "9999990")):
            config = DEFAULT_CONFIG.copy()
            config.update({"edition": edition, "activation_code": code})
            apply_activation_config(config)
            self.assertEqual(config["edition"], EDITION_ULTRA)
            self.assertEqual(config["activation_code"], "")


# --------------------------------------------------------------------------- #
# First-start wizard: model-option identity for already-installed models
# --------------------------------------------------------------------------- #
class _FakeWidget:
    """Records what a real Tk widget would have been created/configured with."""

    created = []

    def __init__(self, kind, *args, **kwargs):
        self.kind = kind
        self.options = dict(kwargs)
        _FakeWidget.created.append(self)

    def pack(self, *args, **kwargs):
        return None

    def config(self, **kwargs):
        self.options.update(kwargs)
        return None

    def configure(self, **kwargs):
        return self.config(**kwargs)

    def cget(self, key):
        return self.options.get(key)


def _widget_factory(kind):
    def build(*args, **kwargs):
        return _FakeWidget(kind, *args, **kwargs)
    return build


class _FakeVar:
    """Stands in for ``tk.StringVar`` so no Tk interpreter is ever created."""

    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _FakeMessagebox:
    shown = []

    @classmethod
    def showinfo(cls, title, message, **kwargs):
        cls.shown.append((title, message))

    @classmethod
    def showerror(cls, title, message, **kwargs):
        cls.shown.append((title, message))


class _FakeTkRoot:
    def __init__(self, *args, **kwargs):
        pass


#: Substituted for ``tkinter`` inside the extracted wizard class.
_FAKE_TK = SimpleNamespace(
    Tk=_FakeTkRoot,
    Label=_widget_factory("label"),
    Frame=_widget_factory("frame"),
    Radiobutton=_widget_factory("radiobutton"),
    StringVar=_FakeVar,
)


class WizardModelOptionTests(unittest.TestCase):
    """The model step must accept an already-installed model and keep one identity.

    The wizard is a ``tk.Tk`` subclass nested in ``run_first_start_gui_wizard``, so the
    class is extracted with ``ast`` and driven with stub widgets: no Tk interpreter, no
    window and no mainloop are ever created.
    """

    @classmethod
    def setUpClass(cls):
        source = textwrap.dedent(inspect.getsource(local_ai.run_first_start_gui_wizard))
        tree = ast.parse(source)
        classes = [node for node in ast.walk(tree)
                   if isinstance(node, ast.ClassDef) and node.name == "Wizard"]
        namespace = vars(local_ai).copy()
        namespace.update({
            "tk": _FAKE_TK,
            "ttk": SimpleNamespace(),
            "messagebox": _FakeMessagebox,
            "queue": queue,
            "threading": threading,
            # Colour constants are closure locals of run_first_start_gui_wizard.
            "BG": "#ffffff", "CARD": "#ffffff", "TEXT": "#000000", "MUTED": "#666666",
            "BLUE": "#2563eb", "BLUE_DARK": "#1d4ed8", "BORDER": "#d0d5dd",
            "SUCCESS": "#16a34a", "WARNING": "#d97706", "ERROR": "#dc2626",
        })
        exec(compile(ast.Module(body=classes, type_ignores=[]),
                     "<WizardModelOptionHarness>", "exec"), namespace)
        cls.wizard_class = namespace["Wizard"]
        cls.namespace = namespace

    def setUp(self):
        _FakeWidget.created = []
        _FakeMessagebox.shown = []

    def _new_wizard(self, recommendation=None, installed=(), available=(),
                    provider_recommendations=(), selected_model="", selected_provider="ollama"):
        # __new__ bypasses tk.Tk.__init__, so no Tk root or window is created.
        wizard = self.wizard_class.__new__(self.wizard_class)
        wizard.config_data = copy.deepcopy(DEFAULT_CONFIG)
        wizard.language = "en_us"
        wizard.selected_provider = selected_provider
        wizard.selected_model = selected_model
        wizard.selected_theme = "auto"
        wizard.recommendation = dict(recommendation or {"model": "qwen2.5:7b"})
        wizard.installed_models = list(installed)
        wizard.available_models = list(available)
        wizard.provider_recommendations = [dict(item) for item in provider_recommendations]
        wizard.installing = False
        wizard.model_options = {}
        wizard._model_var = _FakeVar("")
        wizard.model_next_button = _FakeWidget("button")
        wizard.install_button = _FakeWidget("button")
        wizard.trial_button = _FakeWidget("button")
        wizard.status_label = _FakeWidget("label")
        return wizard

    @staticmethod
    def _radios():
        return [widget for widget in _FakeWidget.created if widget.kind == "radiobutton"]

    @staticmethod
    def _next_state(wizard):
        return wizard.model_next_button.options.get("state")

    def test_selecting_an_installed_ollama_model_enables_the_next_step(self):
        wizard = self._new_wizard(installed=["llama3.2:3b"])
        wizard.model_radio(_FakeWidget("frame"), "llama3.2:3b", installed=True, provider="ollama")
        selection = wizard.make_model_selection("ollama", "llama3.2:3b")
        self.assertIn(selection, wizard.model_options)
        wizard.select_model_option(selection)
        self.assertEqual(wizard.parse_model_selection(), ("ollama", "llama3.2:3b"))
        self.assertEqual(self._next_state(wizard), "normal",
                         "an already-installed Ollama model must allow the next step")

    def test_duplicate_model_in_recommendations_and_installed_resolves_to_one_identity(self):
        wizard = self._new_wizard(installed=["llama3.2:3b"], available=["llama3.2:3b"],
                                  provider_recommendations=[{"provider": "ollama",
                                                             "model": "llama3.2:3b"}])
        wizard.render_models(_FakeWidget("frame"), recommended="llama3.2:3b")
        values = [radio.options.get("value") for radio in self._radios()]
        self.assertEqual(values.count("ollama::llama3.2:3b"), 1,
                         f"the same model was rendered as duplicate options: {values}")
        self.assertEqual(wizard.model_options["ollama::llama3.2:3b"], ("ollama", "llama3.2:3b"))
        wizard.select_model_option("ollama::llama3.2:3b")
        self.assertEqual(wizard.parse_model_selection(), ("ollama", "llama3.2:3b"))
        self.assertEqual(self._next_state(wizard), "normal")

    def test_selecting_the_installed_row_copy_works(self):
        wizard = self._new_wizard(installed=["gemma2:2b"])
        wizard.render_models(_FakeWidget("frame"), recommended="qwen2.5:7b")
        values = [radio.options.get("value") for radio in self._radios()]
        self.assertEqual(values, ["ollama::gemma2:2b"])
        wizard.select_model_option("ollama::gemma2:2b")
        self.assertEqual(wizard.parse_model_selection(), ("ollama", "gemma2:2b"))
        self.assertEqual(self._next_state(wizard), "normal",
                         "the installed-row copy must select its own model")

    def test_unregistered_selection_does_not_enable_the_next_step(self):
        wizard = self._new_wizard()
        wizard._model_var = _FakeVar("ollama::not-in-the-registry")
        self.assertEqual(wizard.parse_model_selection(), ("ollama", ""))
        wizard.refresh_model_controls()
        self.assertEqual(self._next_state(wizard), "disabled",
                         "an unknown option must not be treated as a real model")

    def test_chosen_ollama_backend_and_model_are_persisted(self):
        wizard = self._new_wizard(installed=["llama3.2:3b"])
        wizard.model_radio(_FakeWidget("frame"), "llama3.2:3b", installed=True, provider="ollama")
        wizard.select_model_option("ollama::llama3.2:3b")
        saved, advanced = [], []
        wizard.show_theme_step = lambda: advanced.append(True)
        with mock.patch.dict(self.namespace,
                             {"save_config": lambda cfg: saved.append(copy.deepcopy(cfg))}):
            wizard.finish_model_step()
        self.assertEqual(wizard.config_data["provider"], "ollama")
        self.assertEqual(wizard.config_data["last_model"], "llama3.2:3b")
        self.assertEqual(wizard.selected_model, "llama3.2:3b")
        self.assertEqual(wizard.selected_provider, "ollama")
        self.assertEqual(len(saved), 1, "the wizard must persist the chosen option once")
        self.assertEqual(saved[0]["last_model"], "llama3.2:3b")
        self.assertTrue(advanced, "a valid choice must advance the wizard")

    def test_unselected_model_step_is_refused_without_persisting(self):
        wizard = self._new_wizard()
        saved, advanced = [], []
        wizard.show_theme_step = lambda: advanced.append(True)
        with mock.patch.dict(self.namespace,
                             {"save_config": lambda cfg: saved.append(cfg)}):
            wizard.finish_model_step()
        self.assertEqual(saved, [], "an empty selection must not be persisted")
        self.assertEqual(advanced, [], "an empty selection must not advance")
        self.assertTrue(_FakeMessagebox.shown, "the wizard must explain the refusal")

    def test_cloud_api_choice_keeps_its_existing_behaviour(self):
        provider = normalize_provider("openai")
        wizard = self._new_wizard(provider_recommendations=[{"provider": "openai",
                                                             "model": "gpt-4o-mini"}])
        wizard.render_models(_FakeWidget("frame"), recommended="qwen2.5:7b")
        selection = wizard.make_model_selection("openai", "gpt-4o-mini")
        self.assertEqual(wizard.model_options[selection], (provider, "gpt-4o-mini"))
        wizard.select_model_option(selection)
        self.assertEqual(wizard.parse_model_selection(), (provider, "gpt-4o-mini"))
        self.assertEqual(self._next_state(wizard), "normal",
                         "a cloud/API choice needs no local install to continue")
        saved, advanced = [], []
        wizard.show_theme_step = lambda: advanced.append(True)
        with mock.patch.dict(self.namespace,
                             {"save_config": lambda cfg: saved.append(copy.deepcopy(cfg))}):
            wizard.finish_model_step()
        self.assertEqual(wizard.config_data["provider"], provider)
        self.assertEqual(wizard.config_data["openai_model"], "gpt-4o-mini")
        self.assertEqual(saved[0]["openai_model"], "gpt-4o-mini")
        self.assertTrue(advanced)


if __name__ == "__main__":
    unittest.main()
