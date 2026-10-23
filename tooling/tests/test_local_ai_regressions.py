import importlib
import os
import tempfile
import unittest
from unittest.mock import patch


MODULES = ("local_ai", "local_ai_se")


class LocalAIRegressionTests(unittest.TestCase):
    def test_se_uses_a_separate_app_name(self):
        standard = importlib.import_module("local_ai")
        se = importlib.import_module("local_ai_se")
        self.assertEqual(standard.APP_NAME, "LocalAI")
        self.assertEqual(se.APP_NAME, "LocalAI SE")

    def test_agent_includes_recent_chat_history(self):
        for name in MODULES:
            with self.subTest(module=name):
                module = importlib.import_module(name)
                captured = []

                def fake_ask(messages, _model, _config):
                    captured.extend(messages)
                    return '{"action":"final","content":"done"}'

                history = [{"role": "user", "content": "remember this"}]
                with patch.object(module, "ask_model", side_effect=fake_ask):
                    answer = module.AgentSession(
                        "test-model", "small", {"language": "en_us"}, history
                    ).run("continue")
                self.assertTrue(answer.startswith("done"))
                self.assertIn({"role": "user", "content": "remember this"}, captured)

    def test_remote_provider_failure_does_not_fall_back_to_ollama(self):
        module = importlib.import_module("local_ai")
        with patch.object(module, "ask_lm_studio", side_effect=RuntimeError("offline")), \
                patch.object(module, "ask_ollama", side_effect=AssertionError("unexpected fallback")):
            answer = module.ask_model(
                [{"role": "user", "content": "hello"}],
                "test-model",
                {"provider": "lm_studio"},
            )
        self.assertIn("LM Studio request failed", answer)

    def test_local_and_private_web_urls_are_rejected(self):
        for name in MODULES:
            with self.subTest(module=name):
                module = importlib.import_module(name)
                self.assertFalse(module.is_safe_web_url("http://127.0.0.1/private"))
                self.assertFalse(module.is_safe_web_url("http://localhost/private"))
                self.assertFalse(module.is_safe_web_url("http://192.168.1.1/private"))

    def test_agent_command_uses_argument_execution(self):
        for name in MODULES:
            with self.subTest(module=name):
                module = importlib.import_module(name)
                result = module.agent_run_command(["__localai_missing_command__"])
                self.assertFalse(result["ok"])
                self.assertEqual(result["command"], ["__localai_missing_command__"])

    def test_agent_returns_tool_result_when_model_stops_after_an_action(self):
        for name in MODULES:
            with self.subTest(module=name):
                module = importlib.import_module(name)
                answers = iter([
                    '{"action":"read_file","path":"~/Desktop"}',
                    "",
                ])
                with patch.object(module, "ask_model", side_effect=lambda *_args: next(answers)), \
                        patch.object(module, "agent_read_path", return_value={"ok": True, "path": "/tmp", "content": "found-file.ppt"}):
                    answer = module.AgentSession("test-model", "small", {"language": "en_us"}).run("find a file")
                self.assertIn("found-file.ppt", answer)
                self.assertNotIn("No valid", answer)

    def test_agent_lists_nested_directory_entries(self):
        for name in MODULES:
            with self.subTest(module=name):
                module = importlib.import_module(name)
                with tempfile.TemporaryDirectory(dir=os.getcwd()) as root:
                    nested = os.path.join(root, "nested")
                    os.mkdir(nested)
                    with open(os.path.join(nested, "report.ppt"), "w", encoding="utf-8") as handle:
                        handle.write("test")
                    result = module.agent_read_path(root)
                self.assertTrue(result["ok"])
                self.assertIn("nested/report.ppt", result["content"])

    def test_agent_keeps_original_task_after_reading_a_large_file(self):
        for name in MODULES:
            with self.subTest(module=name):
                module = importlib.import_module(name)
                answers = iter([
                    '{"action":"read_file","path":"/tmp/source.py"}',
                    '{"action":"final","content":"analysis complete"}',
                ])
                captured = []

                def fake_ask(messages, *_args):
                    captured.append(messages[-1]["content"])
                    return next(answers)

                large_source = "x" * (module.AGENT_MODEL_RESULT_LIMIT * 3)
                with patch.object(module, "ask_model", side_effect=fake_ask), \
                        patch.object(module, "agent_read_path", return_value={"ok": True, "path": "/tmp/source.py", "content": large_source}):
                    answer = module.AgentSession("test-model", "small", {"language": "en_us"}).run("analyze the source code")
                self.assertIn("analysis complete", answer)
                self.assertIn("Original task to complete: analyze the source code", captured[-1])
                self.assertIn("content_shortened_for_model", captured[-1])
                self.assertLess(len(captured[-1]), module.AGENT_MODEL_RESULT_LIMIT + 1100)

    def test_cpp_bridge_can_force_llama_cpp(self):
        for name in MODULES:
            with self.subTest(module=name):
                module = importlib.import_module(name)
                with patch.dict(os.environ, {"LOCALAI_FORCE_LLAMA_CPP": "1"}, clear=False):
                    self.assertTrue(module.runtime_prefers_llamacpp_only())
                    config = module.normalize_provider_config({"provider": "ollama"})
                self.assertEqual(config["provider"], "llama_cpp")


if __name__ == "__main__":
    unittest.main()
