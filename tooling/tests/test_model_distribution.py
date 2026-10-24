import ast
import contextlib
import importlib
import io
import json
from pathlib import Path
import queue
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
for directory in ("LocalAI", "LocalAI-SE"):
    sys.path.insert(0, str(ROOT / "projects" / directory / "desktop"))

import model_download_policy as policy


def lookup_result(data):
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(data).encode()
    return response


class ModelDistributionTests(unittest.TestCase):
    def test_mainland_and_unconfirmed_regions_cannot_download(self):
        for data in ({"country": "CN"}, {}, {"country": None}, {"country": "ZZ"},
                     {"country": "XX"}, {"country": "United States"}, []):
            with self.subTest(data=data), mock.patch.object(policy, "urlopen", return_value=lookup_result(data)):
                with self.assertRaises(policy.ModelDownloadUnavailable):
                    policy.require_model_download_available()
        with mock.patch.object(policy, "urlopen", side_effect=TimeoutError):
            with self.assertRaises(policy.ModelDownloadUnavailable):
                policy.require_model_download_available()

    def test_other_regions_can_download(self):
        for country in ("US", "JP", "HK", "MO", "TW"):
            with self.subTest(country=country), mock.patch.object(policy, "urlopen", return_value=lookup_result({"country": country})) as lookup:
                policy.require_model_download_available()
                self.assertEqual(lookup.call_args.kwargs["timeout"], 8)

    def test_blocked_downloads_never_fetch_weights_or_start_ollama(self):
        for name in ("local_ai", "local_ai_se"):
            module = importlib.import_module(name)
            with self.subTest(app=name), \
                 mock.patch.object(policy, "urlopen", return_value=lookup_result({"country": "CN"})), \
                 mock.patch.object(module.requests, "get") as transfer, \
                 mock.patch.object(module.subprocess, "run") as run, \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(policy.ModelDownloadUnavailable):
                    module.download_llamacpp_model("Qwen2.5-0.5B-Instruct-Q4_K_M.gguf")
                self.assertFalse(module.pull_model("test-model", {"language": "en_us"}))
                transfer.assert_not_called()
                run.assert_not_called()

    def test_gui_reports_the_block_and_never_starts_a_download(self):
        module = importlib.import_module("local_ai")
        tree = ast.parse(Path(module.__file__).read_text())
        method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "run_install")
        namespace = dict(module.__dict__)
        exec(compile(ast.Module(body=[method], type_ignores=[]), module.__file__, "exec"), namespace)
        for provider in ("llama_cpp", "ollama"):
            wizard = SimpleNamespace(config_data={}, install_queue=queue.Queue())
            with self.subTest(provider=provider), \
                 mock.patch.object(policy, "urlopen", return_value=lookup_result({"country": "CN"})), \
                 mock.patch.object(module.requests, "get") as transfer, \
                 mock.patch.object(module.subprocess, "Popen") as start:
                namespace["run_install"](wizard, provider, "Qwen2.5-0.5B-Instruct-Q4_K_M.gguf")
                kind, message = wizard.install_queue.get_nowait()
                self.assertEqual(kind, "blocked")
                self.assertIn("自备模型", message)
                transfer.assert_not_called()
                start.assert_not_called()

    def test_allowed_download_still_transfers_the_model(self):
        for name in ("local_ai", "local_ai_se"):
            module = importlib.import_module(name)
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.status_code = 200
            response.headers = {"content-length": "6"}
            response.iter_content.return_value = [b"model!"]
            with self.subTest(app=name), tempfile.TemporaryDirectory() as temp, \
                 mock.patch.object(policy, "urlopen", return_value=lookup_result({"country": "US"})), \
                 mock.patch.object(module.requests, "get", return_value=response) as transfer:
                path = module.download_llamacpp_model("Qwen2.5-0.5B-Instruct-Q4_K_M.gguf", {"llamacpp_model_dir": temp})
                self.assertEqual(Path(path).read_bytes(), b"model!")
                transfer.assert_called_once()

    def test_self_provided_models_run_without_a_region_lookup(self):
        for name in ("local_ai", "local_ai_se"):
            module = importlib.import_module(name)
            with self.subTest(app=name), tempfile.TemporaryDirectory() as temp, \
                 mock.patch.object(policy, "urlopen") as lookup, \
                 mock.patch.object(module, "get_llamacpp_binary_path", return_value="llama-cli"), \
                 mock.patch.object(module, "llamacpp_runtime_options", return_value=[]), \
                 mock.patch.object(module.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="local model reply", stderr="")):
                model = Path(temp) / "my-model.gguf"
                model.write_bytes(b"user model")
                result = module.ask_llama_cpp([{"role": "user", "content": "hello"}], str(model),
                                             {"provider": "llama_cpp", "llamacpp_model": str(model)})
                self.assertIn("local model reply", result)
                lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
