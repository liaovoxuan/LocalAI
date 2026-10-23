"""Release merge-integrity contracts for the merged LocalAI application.

These cover product-level behaviour that the cloud-contract, vault-interop, host
integration and GUI suites do not: the absence of any runtime coupling to the legacy
CloudAI *application*, packaging-facing import shape, the full config lifecycle
(cold start, malformed/partial input, repeated save/reload), and the robustness
boundary (timeouts, shutdown, log/UI redaction, corrupted legacy data).

Everything runs against isolated temporary directories and loopback stubs; no real
provider, no real user data.
"""
from __future__ import annotations

import ast
import copy
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import requests

import local_ai
import cloudattachments
import clouderrors
import cloudproviders
import cloudsecrets
import cloudusage


REPO_ROOT = Path(__file__).resolve().parents[2]
DESKTOP = Path(local_ai.__file__).resolve().parent
HOST_PATH = Path(local_ai.__file__).resolve()
CLOUD_MODULES = ("cloudattachments", "clouderrors", "cloudproviders", "cloudsecrets", "cloudusage")
# Read the modules that were actually imported, so a source contract always
# describes the code under test rather than a hard-coded repository path.
CLOUD_FILES = tuple(
    Path(module.__file__).resolve()
    for module in (cloudattachments, clouderrors, cloudproviders, cloudsecrets, cloudusage)
)
PRODUCTION_FILES = (HOST_PATH,) + CLOUD_FILES

#: The only third-party packages the host is allowed to require at import time.
EXPECTED_HOST_THIRD_PARTY = {"requests", "psutil"}

SECRET_MARKER = "sk-MERGE9INTEGRITY0001"
ATTACHMENT_MARKER = "ATTACHMENT_MARKER_MERGE9"


def _import_roots(nodes) -> set[str]:
    names: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def _top_level_imports(path: Path) -> set[str]:
    """Import-time dependencies: only module-level import statements."""
    return _import_roots(ast.parse(path.read_text(encoding="utf-8")).body)


def _all_imports(path: Path) -> set[str]:
    """Every import anywhere in the file, including lazy and conditional ones."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return _import_roots(node for node in ast.walk(tree)
                         if isinstance(node, (ast.Import, ast.ImportFrom)))


class IsolatedDataCase(unittest.TestCase):
    """Every test writes only inside its own temporary application-data directory."""

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

    def tearDown(self):
        self.patches.stop()
        self._tmp.cleanup()

    def write_config(self, payload):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        (self.root / "config.json").write_text(text, encoding="utf-8")

    def log_text(self) -> str:
        log = self.root / "logs" / "error.log"
        return log.read_text(encoding="utf-8") if log.exists() else ""


# --------------------------------------------------------------------------- #
# 1. no runtime coupling to the legacy CloudAI application
# --------------------------------------------------------------------------- #
class ReleaseDependencyIntegrityTests(IsolatedDataCase):
    def test_host_and_cloud_modules_import_no_cloudai_application_module(self):
        for path in PRODUCTION_FILES:
            for name in _all_imports(path):
                self.assertFalse(
                    name.lower().startswith("cloudai"),
                    f"{path.name} imports the legacy CloudAI application module {name!r}",
                )

    def test_host_only_requires_the_expected_third_party_packages(self):
        imports = _top_level_imports(HOST_PATH)
        local = set(CLOUD_MODULES)
        third_party = {
            name for name in imports
            if name not in sys.stdlib_module_names and name not in local
        }
        self.assertEqual(third_party, EXPECTED_HOST_THIRD_PARTY,
                         f"unexpected host dependencies: {sorted(third_party)}")

    def test_cloud_modules_require_no_third_party_package(self):
        for path in CLOUD_FILES:
            for name in _top_level_imports(path):
                self.assertTrue(
                    name in sys.stdlib_module_names
                    or name == "__future__"
                    or name in CLOUD_MODULES,
                    f"{path.name} unexpectedly depends on {name!r}",
                )

    def test_cloud_modules_import_without_pulling_optional_packages(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(DESKTOP)
        code = (
            "import cloudattachments, clouderrors, cloudproviders, cloudsecrets, cloudusage, sys;"
            "print(sorted(m for m in "
            "('requests','psutil','PIL','numpy','tkinter','keyring') if m in sys.modules))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=180,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), "[]",
                         "importing the cloud modules pulled in optional packages")

    def test_no_keyring_dependency_anywhere_in_production(self):
        for path in PRODUCTION_FILES:
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("keyring", source,
                             f"{path.name} references keyring; the vault must stay file based")

    def test_no_production_file_references_the_cloudai_project_tree(self):
        for path in PRODUCTION_FILES:
            source = path.read_text(encoding="utf-8")
            for needle in ("projects/CloudAI", "projects\\CloudAI"):
                self.assertNotIn(needle, source,
                                 f"{path.name} references the legacy CloudAI project tree")

    def test_sibling_legacy_probe_stays_inside_data_directories(self):
        data_dir = Path("/home/user/.local/share/LocalAI")
        candidates = cloudsecrets._candidate_vault_files(data_dir, [])
        self.assertTrue(candidates, "the migration probe returned no candidates")
        for candidate in candidates:
            self.assertTrue(
                candidate.is_relative_to(data_dir) or candidate.is_relative_to(data_dir.parent),
                f"legacy probe escaped the data directory tree: {candidate}",
            )

    def test_all_five_cloud_modules_are_reachable_through_the_host(self):
        for module in (cloudattachments, clouderrors, cloudproviders, cloudsecrets, cloudusage):
            exposed = getattr(local_ai, module.__name__, None)
            self.assertIs(exposed, module,
                          f"local_ai.{module.__name__} is not the imported module")

    def test_host_helpers_use_the_same_module_objects(self):
        self.assertIs(local_ai.cloudsecrets.LOCAL_PROVIDER_KEY_SLOTS,
                      cloudsecrets.LOCAL_PROVIDER_KEY_SLOTS)
        self.assertIs(local_ai.cloudproviders.CLOUD_PROVIDER_PRESETS,
                      cloudproviders.CLOUD_PROVIDER_PRESETS)
        self.assertIs(local_ai.clouderrors.CLOUD_ERROR_KEYS, clouderrors.CLOUD_ERROR_KEYS)


# --------------------------------------------------------------------------- #
# 2. configuration lifecycle: cold start, malformed input, repeated cycles
# --------------------------------------------------------------------------- #
class ConfigLifecycleTests(IsolatedDataCase):
    def _assert_canonical(self, config):
        self.assertIsInstance(config, dict)
        self.assertIn(config.get("mode"), {"local", "cloud"})
        self.assertIsInstance(config.get("cloud"), dict)
        self.assertIn("provider", config["cloud"])
        self.assertIsInstance(config["cloud"].get("providers"), dict)
        self.assertIn(config.get("provider"), local_ai.SUPPORTED_PROVIDERS)

    def test_cold_start_writes_a_canonical_default_config(self):
        config = local_ai.load_config()
        self._assert_canonical(config)
        self.assertEqual(config["mode"], "local")
        self.assertTrue((self.root / "config.json").exists(),
                        "cold start did not persist a config file")
        persisted = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        self._assert_canonical(persisted)

    def test_invalid_json_config_falls_back_to_defaults(self):
        self.write_config("{definitely not json")
        config = local_ai.load_config()
        self._assert_canonical(config)

    def test_non_object_config_falls_back_to_defaults(self):
        for payload in ('[1, 2, 3]', '"a string"', "42", "null", "true"):
            with self.subTest(payload=payload):
                self.write_config(payload)
                self._assert_canonical(local_ai.load_config())

    def test_partial_config_is_completed_with_defaults(self):
        self.write_config({"language": "en_us"})
        config = local_ai.load_config()
        self._assert_canonical(config)
        self.assertEqual(config["language"], "en_us")
        self.assertEqual(config["mode"], "local")

    def test_wrongly_typed_cloud_section_is_repaired(self):
        for payload in ({"cloud": "nope"},
                        {"cloud": {"providers": "x"}},
                        {"cloud": {"provider": 42, "providers": {"deepseek": "x"}}},
                        {"cloud": None}):
            with self.subTest(payload=payload):
                self.write_config(payload)
                self._assert_canonical(local_ai.load_config())

    def test_repeated_save_reload_cycles_are_stable(self):
        config = local_ai.load_config()
        config["theme"] = "dark"
        config["last_model"] = "qwen2.5:7b"
        local_ai.save_config(config)

        second = local_ai.load_config()
        local_ai.save_config(copy.deepcopy(second))
        third = local_ai.load_config()
        self.assertEqual(second, third, "config drifted between save/reload cycles")

        before = (self.root / "config.json").read_bytes()
        local_ai.save_config(copy.deepcopy(third))
        after = (self.root / "config.json").read_bytes()
        self.assertEqual(before, after, "config.json is not byte-stable across cycles")

    def test_saved_config_never_contains_the_api_key(self):
        cloudsecrets.set_api_key(str(self.root), "openai_compatible", SECRET_MARKER)
        config = local_ai.load_config()
        config["mode"] = "cloud"
        config["provider"] = "openai_compatible"
        config["api_key"] = SECRET_MARKER
        local_ai.save_config(config)

        raw = (self.root / "config.json").read_text(encoding="utf-8")
        self.assertNotIn(SECRET_MARKER, raw, "the API key was persisted into config.json")
        self.assertEqual(cloudsecrets.get_api_key(str(self.root), "openai_compatible"),
                         SECRET_MARKER)

    def test_unreadable_config_file_falls_back_to_defaults(self):
        if os.geteuid() == 0:
            self.skipTest("root can read a 000-mode file")
        self.write_config({"language": "en_us", "mode": "cloud"})
        os.chmod(self.root / "config.json", 0o000)
        try:
            self._assert_canonical(local_ai.load_config())
        finally:
            os.chmod(self.root / "config.json", 0o600)

    def test_corrupted_sibling_legacy_data_does_not_break_load(self):
        sibling = self.root.parent / "CloudAI" / "config"
        sibling.mkdir(parents=True, exist_ok=True)
        (sibling / "cloudai_secrets.json").write_text("{corrupt", encoding="utf-8")
        (sibling / "cloudai_config.json").write_text("[not, an, object]", encoding="utf-8")
        try:
            self._assert_canonical(local_ai.load_config())
        finally:
            for path in (sibling / "cloudai_secrets.json", sibling / "cloudai_config.json"):
                path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# 3. robustness: timeouts, shutdown, redaction
# --------------------------------------------------------------------------- #
class _EchoStubHandler(http.server.BaseHTTPRequestHandler):
    """Loopback provider that echoes the request body back in its error response."""

    def log_message(self, *args):
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        payload = json.dumps({"error": "upstream rejected", "echo": body[:600]}).encode("utf-8")
        self.send_response(500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class _EchoStub:
    def __init__(self):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _EchoStubHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class ReleaseRobustnessTests(IsolatedDataCase):
    def _cloud_config(self, base_url):
        config = copy.deepcopy(local_ai.DEFAULT_CONFIG)
        config.update({
            "mode": "cloud",
            "cloud": {
                "provider": "openai_compatible",
                "default_model": "stub-model",
                "providers": {"openai_compatible": {
                    "base_url": base_url, "model": "stub-model", "api_key": "",
                }},
            },
        })
        return local_ai.normalize_provider_config(config)

    def test_cloud_timeout_is_normalised_and_never_falls_back_to_ollama(self):
        config = self._cloud_config("http://127.0.0.1:9/v1")
        with mock.patch.object(local_ai, "resolve_cloud_target",
                               side_effect=requests.exceptions.Timeout("timed out")), \
                mock.patch.object(local_ai, "ask_ollama",
                                  side_effect=AssertionError("cloud timeout fell back to Ollama")):
            with self.assertRaises(clouderrors.CloudAIError) as caught:
                local_ai.ask_model([{"role": "user", "content": "hi"}], "m", config)
        self.assertEqual(caught.exception.key, clouderrors.CLOUD_REQUEST_ERROR)

    def test_usage_timeout_is_normalised(self):
        def raising_getter(*_args, **_kwargs):
            raise requests.exceptions.Timeout("timed out")

        with self.assertRaises(clouderrors.CloudAIError) as caught:
            cloudusage.fetch_cloud_usage("deepseek", "https://api.deepseek.com/v1",
                                         "sk-usage", http_get=raising_getter)
        self.assertEqual(caught.exception.key, clouderrors.CLOUD_REQUEST_ERROR)
        self.assertNotIn("sk-usage", str(caught.exception))

    def test_log_cloud_failure_redacts_a_credential_in_the_exception(self):
        # Direct unit contract: the logger must scrub a credential no matter which
        # caller passes it, even though no normal path puts one in the message.
        local_ai.log_cloud_failure(
            RuntimeError(f"upstream rejected key={SECRET_MARKER}"), context="unit")
        logged = self.log_text()
        self.assertTrue(logged, "log_cloud_failure wrote nothing")
        self.assertNotIn(SECRET_MARKER, logged, "log_cloud_failure logged a raw credential")
        self.assertIn("********", logged, "the credential was not replaced by a mask")

    def test_cloud_failure_log_redacts_key_and_attachment_text(self):
        # Keep the loopback stub immune to any proxy the developer has configured,
        # then restore the whole environment so the suite stays order independent.
        proxy_environment = {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
        with mock.patch.dict(os.environ, proxy_environment, clear=False):
            for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                os.environ.pop(var, None)

            with _EchoStub() as stub:
                cloudsecrets.set_api_key(str(self.root), "openai_compatible", SECRET_MARKER)
                config = self._cloud_config(stub.base_url)
                attachment = self.root / "context.txt"
                attachment.write_text(f"secret attachment {ATTACHMENT_MARKER}", encoding="utf-8")
                with self.assertRaises(clouderrors.CloudAIError):
                    local_ai.ask_cloud([{"role": "user", "content": "go"}], config, [attachment])

            logged = self.log_text()
            self.assertTrue(logged, "the cloud failure was not logged at all")
            self.assertNotIn(SECRET_MARKER, logged, "the API key reached error.log")
            self.assertNotIn(ATTACHMENT_MARKER, logged, "attachment content reached error.log")
            self.assertIn("cloud failure", logged)

    def test_ui_error_text_never_carries_key_or_attachment(self):
        for key in clouderrors.CLOUD_ERROR_KEYS:
            with self.subTest(key=key):
                exc = clouderrors.CloudAIError(
                    key, detail=f"{SECRET_MARKER} {ATTACHMENT_MARKER}")
                text = local_ai.cloud_error_text({"language": "en_us"}, exc)
                self.assertTrue(text)
                self.assertNotIn(SECRET_MARKER, text)
                self.assertNotIn(ATTACHMENT_MARKER, text)

    def test_cloud_error_text_is_localised_for_every_ui_language(self):
        exc = clouderrors.CloudAIError(clouderrors.CLOUD_NO_KEY)
        for code in local_ai.CHAT_GUI_TEXT:
            with self.subTest(language=code):
                text = local_ai.cloud_error_text({"language": code}, exc)
                self.assertTrue(text, f"missing cloud error text for {code}")

    def test_attachment_bounds_survive_a_failing_request(self):
        attachment = self.root / "big.txt"
        attachment.write_text("x" * 200_000, encoding="utf-8")
        context = cloudattachments.build_file_context([attachment],
                                                      total_limit=local_ai.CLOUD_ATTACHMENT_TOTAL_CHARS)
        self.assertLessEqual(len(context),
                             local_ai.CLOUD_ATTACHMENT_TOTAL_CHARS + len(cloudattachments.TRUNCATION_MARKER))
        self.assertNotIn(ATTACHMENT_MARKER, context)


if __name__ == "__main__":
    unittest.main()
