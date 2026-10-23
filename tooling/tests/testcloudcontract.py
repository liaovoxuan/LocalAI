"""Contract tests for the cloud provider / error / usage / attachment modules.

Covers: provider presets and aliases, URL validation, cloud error
classification (401/403 vs transport failures), usage URL probing with
404/405 fallback, stable usage summaries, bounded text/``.docx`` extraction
and the "no duplicated implementation" guard.

Run with::

    python3 -m unittest discover -s tooling/tests -p "test*.py"
"""
# PYTEST COLLECTION NOTE
# ----------------------
# These modules are intentionally named ``testcloud*.py`` (no underscore) so that
# WorkBuddy can import the cloud modules by their current names.  ``unittest``'s
# default discovery pattern is ``test*.py`` and therefore collects them; pytest's
# default ``python_files = test_*.py`` does *not*.  Collection cannot be fixed
# from inside a test module, so use one of::
#
#     python3 -m unittest discover -s tooling/tests -p "test*.py"
#     python3 -m pytest -o python_files="test_*.py test*.py" tooling/tests
#     python3 -m pytest tooling/tests/testcloudcontract.py   # explicit paths

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

_DESKTOP = Path(__file__).resolve().parents[2] / "projects" / "LocalAI" / "desktop"
if str(_DESKTOP) not in sys.path:
    sys.path.insert(0, str(_DESKTOP))

import cloudattachments as attachments  # noqa: E402
import clouderrors as errors  # noqa: E402
import cloudproviders as providers  # noqa: E402
import cloudusage as usage  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, status_code=200, payload=None, json_error=None):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def make_getter(routes, calls=None):
    """Build an injected ``http_get`` from ``{url: response_or_exception}``."""

    def getter(url, *, headers=None, timeout=None):
        if calls is not None:
            calls.append({"url": url, "headers": dict(headers or {}), "timeout": timeout})
        value = routes[url]
        if isinstance(value, BaseException):
            raise value
        return value

    return getter


def write_docx(path: Path, text: str) -> None:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)


# --------------------------------------------------------------------------- #
# clouderrors
# --------------------------------------------------------------------------- #
class CloudErrorTests(unittest.TestCase):
    def test_401_and_403_classify_as_auth_errors(self):
        for status in (401, 403):
            response = FakeResponse(status)
            exc = RuntimeError(f"HTTP {status}")
            exc.response = response  # type: ignore[attr-defined]
            self.assertEqual(errors.cloud_exception_key(exc), errors.CLOUD_AUTH_ERROR)

    def test_transport_and_server_failures_classify_as_request_errors(self):
        cases = [
            RuntimeError("Connection refused"),
            TimeoutError("timed out"),
            ConnectionError("network unreachable"),
            OSError("dns resolve failed"),
            RuntimeError("HTTP 500"),
        ]
        for exc in cases:
            with self.subTest(exc=repr(exc)):
                self.assertEqual(errors.cloud_exception_key(exc), errors.CLOUD_REQUEST_ERROR)

    def test_missing_api_key_forms_classify_as_no_key(self):
        cases = [
            PermissionError("missing_api_key"),
            RuntimeError("missing_api_key"),
            ValueError("OpenAI API Key is empty."),
            RuntimeError("no api key configured"),
        ]
        for exc in cases:
            with self.subTest(exc=repr(exc)):
                self.assertEqual(errors.cloud_exception_key(exc), errors.CLOUD_NO_KEY)

    def test_cloud_ai_error_is_idempotent(self):
        error = errors.missing_api_key()
        self.assertEqual(errors.cloud_exception_key(error), errors.CLOUD_NO_KEY)
        self.assertTrue(errors.is_missing_api_key(error))
        self.assertEqual(error.to_dict()["key"], errors.CLOUD_NO_KEY)

    def test_unknown_error_defaults_to_request_error(self):
        self.assertEqual(errors.cloud_exception_key(RuntimeError("weird")), errors.CLOUD_REQUEST_ERROR)

    def test_invalid_key_is_rejected(self):
        with self.assertRaises(ValueError):
            errors.CloudAIError("not_a_key")

    def test_error_keys_are_the_i18n_keys(self):
        self.assertEqual(
            errors.CLOUD_ERROR_KEYS,
            (errors.CLOUD_NO_KEY, errors.CLOUD_AUTH_ERROR, errors.CLOUD_REQUEST_ERROR),
        )

    def test_describe_is_log_safe(self):
        payload = errors.describe(RuntimeError("connection refused"))
        self.assertEqual(payload["key"], errors.CLOUD_REQUEST_ERROR)
        self.assertEqual(payload["type"], "RuntimeError")


# --------------------------------------------------------------------------- #
# cloudproviders
# --------------------------------------------------------------------------- #
class CloudProviderTests(unittest.TestCase):
    def test_presets_cover_the_expected_vendors(self):
        self.assertEqual(
            set(providers.CLOUD_PROVIDER_PRESETS),
            {"openai_official", "openai_compatible", "deepseek", "openrouter", "siliconflow"},
        )
        self.assertEqual(
            providers.CLOUD_PROVIDER_PRESETS["deepseek"].base_url, "https://api.deepseek.com/v1"
        )
        self.assertEqual(providers.CLOUD_PROVIDER_PRESETS["openai_compatible"].base_url, "")

    def test_aliases_normalise_to_canonical_ids(self):
        cases = {
            "openai": "openai_official",
            "OpenAI Official": "openai_official",
            "OPENAI_OFFICIAL": "openai_official",
            "custom": "openai_compatible",
            "custom_openai_compatible": "openai_compatible",
            "OpenAI-Compatible": "openai_compatible",
            "localhost": "openai_compatible",
            "deepseek": "deepseek",
            "DeepSeek": "deepseek",
            "open_router": "openrouter",
            "Open Router": "openrouter",
            "silicon": "siliconflow",
            "硅基流动": "siliconflow",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(providers.normalize_cloud_provider(value), expected)

    def test_unknown_or_empty_provider_falls_back_to_default(self):
        for value in (None, "", "   ", "moonshot", 42, object()):
            with self.subTest(value=repr(value)):
                self.assertEqual(
                    providers.normalize_cloud_provider(value), providers.DEFAULT_CLOUD_PROVIDER
                )

    def test_is_cloud_provider_distinguishes_unknowns(self):
        self.assertTrue(providers.is_cloud_provider("deepseek"))
        self.assertTrue(providers.is_cloud_provider("custom"))
        self.assertFalse(providers.is_cloud_provider("moonshot"))
        self.assertFalse(providers.is_cloud_provider(""))

    def test_url_validation_accepts_http_variants(self):
        valid = [
            "https://api.openai.com/v1",
            "http://localhost:8000/v1",
            "http://127.0.0.1:1234/v1",
            "http://192.168.1.10:8000/v1/chat/completions",
            "https://example.test",
        ]
        for value in valid:
            with self.subTest(value=value):
                self.assertTrue(providers.provider_url_is_valid(value))

    def test_url_validation_rejects_non_urls(self):
        invalid = [None, "", "   ", "not a url", "api.openai.com", "ftp://example.com", "//example.com"]
        for value in invalid:
            with self.subTest(value=repr(value)):
                self.assertFalse(providers.provider_url_is_valid(value))

    def test_normalize_base_url_strips_slashes_and_chat_suffix(self):
        self.assertEqual(providers.normalize_base_url(" https://a.test/v1/ "), "https://a.test/v1")
        self.assertEqual(
            providers.normalize_base_url("https://a.test/v1/chat/completions"), "https://a.test/v1"
        )
        self.assertEqual(providers.normalize_base_url(None), "")

    def test_chat_completions_url_matches_localai_helper(self):
        """Parity guard: this must never become a fork of LocalAI's helper."""
        try:
            import local_ai
        except ImportError:  # pragma: no cover - LocalAI source is present in-tree
            self.skipTest("local_ai is not importable in this environment")
        cases = [
            "",
            "   ",
            "https://api.openai.com/v1",
            "https://api.openai.com/v1/",
            "https://api.openai.com/v1/chat/completions",
            "http://localhost:8000/v1",
        ]
        for value in cases:
            with self.subTest(value=value):
                self.assertEqual(
                    providers.chat_completions_url(value), local_ai.openai_chat_url(value)
                )

    def test_resolve_active_provider_uses_preset_defaults(self):
        active = providers.resolve_active_provider({"provider": "deepseek"})
        self.assertEqual(active.provider, "deepseek")
        self.assertEqual(active.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(active.model, "deepseek-chat")
        self.assertFalse(active.has_api_key)

    def test_resolve_active_provider_honours_overrides_and_nesting(self):
        config = {
            "cloud": {
                "provider": "custom",
                "default_model": "ignored-default",
                "providers": {"openai_compatible": {"base_url": "http://box:9000/v1", "model": "m1"}},
            }
        }
        active = providers.resolve_active_provider(config, api_key="sk-x")
        self.assertEqual(active.provider, "openai_compatible")
        self.assertEqual(active.base_url, "http://box:9000/v1")
        self.assertEqual(active.model, "m1")
        self.assertTrue(active.has_api_key)
        self.assertTrue(active.url_is_valid)

    def test_resolve_active_provider_survives_garbage(self):
        for value in (None, "text", 42, [], {"provider": "moonshot"}):
            with self.subTest(value=repr(value)):
                active = providers.resolve_active_provider(value)
                self.assertEqual(active.provider, providers.DEFAULT_CLOUD_PROVIDER)
                self.assertTrue(active.model)

    def test_active_provider_dict_never_leaks_the_key(self):
        active = providers.resolve_active_provider({"provider": "deepseek"}, api_key="sk-secret-value")
        payload = active.to_dict()
        self.assertTrue(payload["has_api_key"])
        self.assertNotIn("api_key", payload)
        self.assertNotIn("sk-secret-value", str(payload))

    def test_default_provider_config_is_key_free(self):
        block = providers.default_provider_config("deepseek")
        self.assertEqual(block["api_key"], "")
        self.assertEqual(block["model"], "deepseek-chat")

    def test_provider_names_are_unique_and_complete(self):
        names = providers.provider_names()
        self.assertEqual(len(names), len(providers.CLOUD_PROVIDER_PRESETS))
        self.assertEqual(names["deepseek"], "DeepSeek")


# --------------------------------------------------------------------------- #
# cloudusage
# --------------------------------------------------------------------------- #
class CloudUsageTests(unittest.TestCase):
    def test_usage_urls_include_vendor_endpoints_and_generic_suffixes(self):
        urls = usage.cloud_usage_urls("deepseek", "https://api.deepseek.com/v1")
        self.assertEqual(urls[0], "https://api.deepseek.com/user/balance")
        self.assertIn("https://api.deepseek.com/v1/usage", urls)
        self.assertIn("https://api.deepseek.com/v1/dashboard/billing/usage", urls)
        self.assertIn("https://api.deepseek.com/v1/user/usage", urls)

    def test_usage_urls_are_deduplicated_and_ordered(self):
        urls = usage.cloud_usage_urls("openai_official", "https://api.openai.com/v1")
        self.assertEqual(len(urls), len(set(urls)))
        self.assertEqual(urls[0], "https://api.openai.com/v1/dashboard/billing/usage")
        self.assertEqual(urls[1], "https://api.openai.com/v1/usage")

    def test_usage_urls_accept_a_chat_endpoint_base(self):
        urls = usage.cloud_usage_urls("openrouter", "https://openrouter.ai/api/v1/chat/completions")
        self.assertIn("https://openrouter.ai/api/v1/credits", urls)
        self.assertIn("https://openrouter.ai/api/v1/usage", urls)
        self.assertNotIn("https://openrouter.ai/api/v1/chat/completions/usage", urls)

    def test_usage_urls_without_base_keep_only_vendor_endpoints(self):
        urls = usage.cloud_usage_urls("openai_compatible", "")
        self.assertEqual(urls, [])

    def test_summarize_handles_key_shapes(self):
        cases = [
            ({"total_usage": 12}, "total_usage=12"),
            ({"data": {"total_usage": 5}}, "total_usage=5"),
            ({"balance": "1.50"}, "balance=1.50"),
            ({"total_granted": 10, "total_used": 3}, "total_granted=10"),
            ({"limit": 1, "used": 2, "remaining": 3}, "limit=1"),
            ({"data": [1, 2, 3]}, "3 records"),
            ('{"total_usage": 9}', "total_usage=9"),
        ]
        for payload, expected in cases:
            with self.subTest(payload=payload):
                self.assertEqual(usage.summarize_usage_data(payload), expected)

    def test_summarize_returns_empty_for_unusable_payloads(self):
        for payload in (None, 42, [], {}, "not json", "{bad", {"unrelated": 1}):
            with self.subTest(payload=repr(payload)):
                self.assertEqual(usage.summarize_usage_data(payload), "")

    def test_fetch_requires_an_api_key(self):
        for key in ("", None, "   "):
            with self.subTest(key=repr(key)):
                with self.assertRaises(errors.CloudAIError) as caught:
                    usage.fetch_cloud_usage("deepseek", "https://api.deepseek.com/v1", key)
                self.assertEqual(caught.exception.key, errors.CLOUD_NO_KEY)

    def test_fetch_skips_404_and_405_then_succeeds(self):
        urls = usage.cloud_usage_urls("deepseek", "https://api.deepseek.com/v1")
        routes = {
            urls[0]: FakeResponse(404),
            urls[1]: FakeResponse(405),
            urls[2]: FakeResponse(200, {"balance": "42.00"}),
        }
        calls = []
        result = usage.fetch_cloud_usage(
            "deepseek", "https://api.deepseek.com/v1", "sk-x", http_get=make_getter(routes, calls)
        )
        self.assertEqual(result, "balance=42.00")
        self.assertEqual([call["url"] for call in calls][:3], urls[:3])
        self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer sk-x")
        self.assertEqual(calls[0]["timeout"], usage.DEFAULT_TIMEOUT)

    def test_fetch_returns_empty_when_every_endpoint_is_missing(self):
        urls = usage.cloud_usage_urls("siliconflow", "https://api.siliconflow.cn/v1")
        routes = {url: FakeResponse(404) for url in urls}
        self.assertEqual(
            usage.fetch_cloud_usage(
                "siliconflow", "https://api.siliconflow.cn/v1", "sk-x", http_get=make_getter(routes)
            ),
            "",
        )

    def test_fetch_probes_past_transport_failures(self):
        urls = usage.cloud_usage_urls("deepseek", "https://api.deepseek.com/v1")
        routes = {urls[0]: ConnectionError("refused")}
        routes[urls[1]] = FakeResponse(200, {"total_usage": 7})
        result = usage.fetch_cloud_usage(
            "deepseek", "https://api.deepseek.com/v1", "sk-x", http_get=make_getter(routes)
        )
        self.assertEqual(result, "total_usage=7")

    def test_fetch_reports_a_request_error_when_all_endpoints_fail(self):
        urls = usage.cloud_usage_urls("deepseek", "https://api.deepseek.com/v1")
        routes = {url: ConnectionError("refused") for url in urls}
        with self.assertRaises(errors.CloudAIError) as caught:
            usage.fetch_cloud_usage(
                "deepseek", "https://api.deepseek.com/v1", "sk-x", http_get=make_getter(routes)
            )
        self.assertEqual(caught.exception.key, errors.CLOUD_REQUEST_ERROR)
        self.assertIsInstance(caught.exception.__cause__, ConnectionError)

    def test_fetch_reports_http_failures_via_status_code(self):
        urls = usage.cloud_usage_urls("deepseek", "https://api.deepseek.com/v1")
        routes = {url: FakeResponse(500) for url in urls}
        with self.assertRaises(errors.CloudAIError) as caught:
            usage.fetch_cloud_usage(
                "deepseek", "https://api.deepseek.com/v1", "sk-x", http_get=make_getter(routes)
            )
        self.assertEqual(caught.exception.key, errors.CLOUD_REQUEST_ERROR)

    def test_fetch_returns_empty_for_a_readable_but_useless_payload(self):
        urls = usage.cloud_usage_urls("deepseek", "https://api.deepseek.com/v1")
        routes = {url: FakeResponse(200, {"unrelated": 1}) for url in urls}
        self.assertEqual(
            usage.fetch_cloud_usage(
                "deepseek", "https://api.deepseek.com/v1", "sk-x", http_get=make_getter(routes)
            ),
            "",
        )

    def test_fetch_does_not_call_the_network_without_urls(self):
        def explode(*_args, **_kwargs):  # pragma: no cover - must never run
            raise AssertionError("network must not be touched")

        self.assertEqual(
            usage.fetch_cloud_usage("openai_compatible", "", "sk-x", http_get=explode), ""
        )


# --------------------------------------------------------------------------- #
# cloudattachments
# --------------------------------------------------------------------------- #
class CloudAttachmentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_read_text_file_is_bounded_by_max_file_bytes(self):
        big = self.root / "big.txt"
        big.write_bytes(b"a" * (attachments.MAX_FILE_BYTES * 3))
        text = attachments.read_text_file(big)
        self.assertLessEqual(len(text), attachments.MAX_FILE_BYTES)
        self.assertGreater(len(text), 0)

    def test_read_text_file_reads_a_file_at_the_limit(self):
        exact = self.root / "exact.txt"
        exact.write_bytes(b"b" * attachments.MAX_FILE_BYTES)
        self.assertEqual(len(attachments.read_text_file(exact)), attachments.MAX_FILE_BYTES)

    def test_build_file_context_caps_chars_and_marks_truncation(self):
        big = self.root / "big.txt"
        big.write_bytes(b"c" * (attachments.MAX_FILE_BYTES * 2))
        context = attachments.build_file_context([big])
        self.assertIn("[File: big.txt]", context)
        self.assertTrue(context.endswith(attachments.TRUNCATION_MARKER))
        body = context.split("\n", 1)[1]
        self.assertEqual(len(body), attachments.MAX_FILE_CHARS + len(attachments.TRUNCATION_MARKER))

    def test_short_text_is_inlined_verbatim(self):
        small = self.root / "note.md"
        small.write_text("hello\n\n\n\nworld", encoding="utf-8")
        context = attachments.build_file_context([small])
        self.assertEqual(context, "[File: note.md]\nhello\n\nworld")

    def test_docx_text_is_extracted(self):
        docx = self.root / "doc.docx"
        write_docx(docx, "cloud attachment body")
        self.assertEqual(attachments.read_text_file(docx), "cloud attachment body")
        self.assertIn("cloud attachment body", attachments.build_file_context([docx]))

    def test_oversized_docx_is_bounded_without_raising(self):
        docx = self.root / "huge.docx"
        write_docx(docx, "d" * (attachments.MAX_FILE_BYTES * 2))
        text = attachments.read_text_file(docx)
        self.assertLessEqual(len(text), attachments.MAX_FILE_BYTES * 2)
        self.assertGreater(len(text), 0)

    def test_corrupt_docx_returns_empty(self):
        docx = self.root / "broken.docx"
        docx.write_bytes(b"not a zip archive at all")
        self.assertEqual(attachments.read_text_file(docx), "")

    def test_docx_without_document_entry_returns_empty(self):
        docx = self.root / "empty.docx"
        with zipfile.ZipFile(docx, "w") as archive:
            archive.writestr("word/other.xml", "<x/>")
        self.assertEqual(attachments.read_text_file(docx), "")

    def test_unsupported_and_missing_paths_return_empty(self):
        legacy = self.root / "old.doc"
        legacy.write_bytes(b"legacy")
        self.assertEqual(attachments.read_text_file(legacy), "")
        self.assertEqual(attachments.read_text_file(self.root / "nope.txt"), "")
        self.assertEqual(attachments.read_text_file(self.root), "")
        self.assertEqual(attachments.read_text_file(None), "")
        binary = self.root / "blob.bin"
        binary.write_bytes(b"\x00\x01\x02")
        self.assertEqual(attachments.read_text_file(binary), "")

    def test_binary_content_with_text_extension_does_not_raise(self):
        odd = self.root / "odd.txt"
        odd.write_bytes(b"\xff\xfe\x00\x01binary-ish")
        self.assertIsInstance(attachments.read_text_file(odd), str)

    def test_image_and_native_file_detection(self):
        self.assertTrue(attachments.is_image_file("a.PNG"))
        self.assertFalse(attachments.is_image_file("a.txt"))
        self.assertTrue(attachments.model_supports_native_files("gpt-4o-mini"))
        self.assertTrue(attachments.model_supports_native_files("qwen2.5-vl-7b"))
        self.assertFalse(attachments.model_supports_native_files("deepseek-chat"))

    def test_encode_file_data_url_is_bounded(self):
        big = self.root / "pic.png"
        big.write_bytes(b"\x89PNG" + b"z" * (attachments.MAX_FILE_BYTES * 2))
        url = attachments.encode_file_data_url(big)
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertLessEqual(len(url), (attachments.MAX_FILE_BYTES * 4 // 3) + 64)
        self.assertEqual(attachments.encode_file_data_url(self.root / "missing.png"), "")

    def test_messages_with_files_inlines_text_for_plain_models(self):
        text_file = self.root / "context.txt"
        text_file.write_text("project context", encoding="utf-8")
        messages = [{"role": "user", "content": "summarize"}]
        result = attachments.messages_with_files(messages, [text_file], "deepseek-chat")
        self.assertIn("project context", result[-1]["content"])
        self.assertIn(attachments.FILE_CONTEXT_HEADER, result[-1]["content"])
        self.assertEqual(result[-1]["role"], "user")

    def test_messages_with_files_does_not_mutate_the_input(self):
        text_file = self.root / "context.txt"
        text_file.write_text("data", encoding="utf-8")
        messages = [{"role": "user", "content": "original"}]
        attachments.messages_with_files(messages, [text_file], "deepseek-chat")
        self.assertEqual(messages[0]["content"], "original")

    def test_messages_with_files_without_paths_returns_copies(self):
        messages = [{"role": "user", "content": "hi"}]
        result = attachments.messages_with_files(messages, [], "deepseek-chat")
        self.assertEqual(result, messages)
        self.assertIsNot(result[0], messages[0])

    def test_messages_with_files_reports_unreadable_attachments(self):
        missing = self.root / "ghost.bin"
        messages = [{"role": "user", "content": "look"}]
        result = attachments.messages_with_files(messages, [missing], "deepseek-chat")
        self.assertIn("could not be read", result[-1]["content"])
        self.assertIn("ghost.bin", result[-1]["content"])

    def test_messages_with_files_uses_native_blocks_for_vision_models(self):
        image = self.root / "pic.png"
        image.write_bytes(b"\x89PNG-fake")
        text_file = self.root / "notes.txt"
        text_file.write_text("notes", encoding="utf-8")
        result = attachments.messages_with_files(
            [{"role": "user", "content": "describe"}], [image, text_file], "gpt-4o-mini"
        )
        blocks = result[-1]["content"]
        self.assertIsInstance(blocks, list)
        self.assertEqual(blocks[0]["type"], "text")
        self.assertEqual(blocks[1]["type"], "image_url")
        self.assertTrue(blocks[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(blocks[2]["type"], "file")
        self.assertEqual(blocks[2]["file"]["filename"], "notes.txt")

    def test_messages_with_files_skips_missing_native_attachments(self):
        result = attachments.messages_with_files(
            [{"role": "user", "content": "x"}], [self.root / "gone.png"], "gpt-4o-mini"
        )
        self.assertEqual([block["type"] for block in result[-1]["content"]], ["text"])

    def test_limits_are_the_contracted_values(self):
        self.assertEqual(attachments.MAX_FILE_BYTES, 256 * 1024)
        self.assertEqual(attachments.MAX_FILE_CHARS, 12_000)


# --------------------------------------------------------------------------- #
# guards
# --------------------------------------------------------------------------- #
class NoDuplicateImplementationTests(unittest.TestCase):
    FORBIDDEN = {
        "cloudproviders": ("def openai_chat_url", "def ask_openai_chat_completions", "AGENT_MAX_REQUESTS"),
        "cloudsecrets": ("def ask_openai_chat_completions", "AGENT_MAX_REQUESTS", "MODERATION_PATTERNS"),
        "cloudusage": ("def ask_openai_chat_completions", "MODERATION_PATTERNS"),
        "clouderrors": ("MODERATION_PATTERNS", "AGENT_MAX_REQUESTS"),
        "cloudattachments": ("def encode_secret", "MODERATION_PATTERNS", "AGENT_MAX_REQUESTS"),
    }

    def test_new_modules_do_not_redefine_owned_helpers(self):
        for module_name, needles in self.FORBIDDEN.items():
            source = (_DESKTOP / f"{module_name}.py").read_text(encoding="utf-8")
            for needle in needles:
                with self.subTest(module=module_name, needle=needle):
                    self.assertNotIn(needle, source)

    def test_modules_import_without_side_effects(self):
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as home:
            env = dict(os.environ)
            env["PYTHONPATH"] = str(_DESKTOP)
            env["HOME"] = home
            env.pop("XDG_DATA_HOME", None)
            env.pop("LOCALAPPDATA", None)
            env.pop("APPDATA", None)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import cloudproviders, cloudsecrets, cloudusage, clouderrors, cloudattachments",
                ],
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(os.listdir(cwd), [], "import created files in the working directory")
            self.assertEqual(os.listdir(home), [], "import created files under HOME")

    def test_public_api_surface_is_usable_by_integration_code(self):
        for module in (providers, errors, usage, attachments):
            self.assertTrue(getattr(module, "__file__", ""))
        self.assertTrue(callable(providers.resolve_active_provider))
        self.assertTrue(callable(errors.cloud_exception_key))
        self.assertTrue(callable(usage.fetch_cloud_usage))
        self.assertTrue(callable(attachments.messages_with_files))


# --------------------------------------------------------------------------- #
# config["cloud"] host contract: canonical shape + temporary cloud.public shape
# --------------------------------------------------------------------------- #
class CloudConfigShapeTests(unittest.TestCase):
    """The host standardises ``config["cloud"] = {provider, default_model, providers}``.

    The temporary ``cloud.public`` shape must keep working, canonical fields
    must win on every conflict, and no shape may leak a secret.
    """

    CANONICAL = {
        "cloud": {
            "provider": "deepseek",
            "default_model": "deepseek-reasoner",
            "providers": {
                "deepseek": {
                    "enabled": True,
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-reasoner",
                    "api_key": "********",
                }
            },
        }
    }

    LEGACY_PUBLIC = {
        "cloud": {
            "public": {
                "provider": "siliconflow",
                "default_model": "Qwen/Qwen2.5-7B-Instruct",
                "providers": {
                    "siliconflow": {
                        "base_url": "https://api.siliconflow.cn/v1",
                        "model": "Qwen/Qwen2.5-7B-Instruct",
                    }
                },
            }
        }
    }

    def test_canonical_shape_resolves(self):
        active = providers.resolve_active_provider(self.CANONICAL)
        self.assertEqual(active.provider, "deepseek")
        self.assertEqual(active.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(active.model, "deepseek-reasoner")

    def test_legacy_public_shape_resolves(self):
        active = providers.resolve_active_provider(self.LEGACY_PUBLIC)
        self.assertEqual(active.provider, "siliconflow")
        self.assertEqual(active.base_url, "https://api.siliconflow.cn/v1")
        self.assertEqual(active.model, "Qwen/Qwen2.5-7B-Instruct")

    def test_canonical_provider_beats_public_provider(self):
        config = {
            "cloud": {
                "provider": "deepseek",
                "public": {"provider": "openrouter"},
            }
        }
        self.assertEqual(providers.resolve_active_provider(config).provider, "deepseek")

    def test_canonical_default_model_beats_public_default_model(self):
        config = {
            "cloud": {
                "default_model": "deepseek-chat",
                "public": {"default_model": "deepseek-reasoner"},
            }
        }
        self.assertEqual(providers.resolve_active_provider(config).model, "deepseek-chat")

    def test_canonical_provider_entry_beats_public_entry_per_field(self):
        config = {
            "cloud": {
                "provider": "deepseek",
                "providers": {"deepseek": {"base_url": "https://canonical.test/v1"}},
                "public": {
                    "providers": {
                        "deepseek": {
                            "base_url": "https://legacy.test/v1",
                            "model": "legacy-model",
                        }
                    }
                },
            }
        }
        active = providers.resolve_active_provider(config)
        self.assertEqual(active.base_url, "https://canonical.test/v1")
        # The public entry still supplies the field canonical does not define.
        self.assertEqual(active.model, "legacy-model")

    def test_public_fills_fields_missing_from_canonical(self):
        config = {
            "cloud": {
                "public": {
                    "provider": "openrouter",
                    "default_model": "openai/gpt-4o-mini",
                }
            }
        }
        active = providers.resolve_active_provider(config)
        self.assertEqual(active.provider, "openrouter")
        self.assertEqual(active.base_url, "https://openrouter.ai/api/v1")
        self.assertEqual(active.model, "openai/gpt-4o-mini")

    def test_bare_cloud_section_is_accepted(self):
        active = providers.resolve_active_provider(self.CANONICAL["cloud"])
        self.assertEqual(active.provider, "deepseek")

    def test_bare_public_mapping_is_accepted(self):
        active = providers.resolve_active_provider(self.LEGACY_PUBLIC["cloud"]["public"])
        self.assertEqual(active.provider, "siliconflow")

    def test_config_api_keys_are_never_adopted(self):
        config = {
            "cloud": {
                "provider": "deepseek",
                "api_key": "sk-top-level-must-not-be-used",
                "providers": {"deepseek": {"api_key": "sk-provider-must-not-be-used"}},
                "public": {
                    "api_key": "sk-public-must-not-be-used",
                    "providers": {"deepseek": {"api_key": "sk-public-provider-must-not-be-used"}},
                },
            }
        }
        active = providers.resolve_active_provider(config)
        self.assertEqual(active.api_key, "")
        self.assertFalse(active.has_api_key)
        rendered = str(active.to_dict()) + str(active)
        for leaked in (
            "sk-top-level-must-not-be-used",
            "sk-provider-must-not-be-used",
            "sk-public-must-not-be-used",
            "sk-public-provider-must-not-be-used",
        ):
            self.assertNotIn(leaked, rendered)

    def test_explicit_api_key_still_wins_over_config_placeholders(self):
        active = providers.resolve_active_provider(self.CANONICAL, api_key="sk-explicit-0123456789")
        self.assertEqual(active.api_key, "sk-explicit-0123456789")

    def test_masked_api_key_argument_is_not_a_credential(self):
        for masked in ("********", "***", " ******** "):
            with self.subTest(masked=repr(masked)):
                active = providers.resolve_active_provider(self.CANONICAL, api_key=masked)
                self.assertEqual(active.api_key, "")
                self.assertFalse(active.has_api_key)

    def test_public_shape_with_garbage_still_resolves(self):
        for public in (None, "text", 42, [], {"providers": "nope"}):
            with self.subTest(public=repr(public)):
                active = providers.resolve_active_provider({"cloud": {"public": public}})
                self.assertEqual(active.provider, providers.DEFAULT_CLOUD_PROVIDER)
                self.assertTrue(active.model)

    def test_unset_url_is_reported_not_invented(self):
        active = providers.resolve_active_provider({"cloud": {"provider": "openai_compatible"}})
        self.assertEqual(active.base_url, "")
        self.assertFalse(active.url_is_valid)

    def test_invalid_configured_url_is_reported(self):
        config = {
            "cloud": {
                "provider": "openai_compatible",
                "providers": {"openai_compatible": {"base_url": "not a url"}},
            }
        }
        active = providers.resolve_active_provider(config)
        self.assertFalse(active.url_is_valid)
        self.assertEqual(active.base_url, "not a url")


# --------------------------------------------------------------------------- #
# usage hardening
# --------------------------------------------------------------------------- #
class CloudUsageHardeningTests(unittest.TestCase):
    def test_usage_urls_skip_generic_suffixes_for_an_invalid_base(self):
        urls = usage.cloud_usage_urls("deepseek", "not a url")
        self.assertEqual(urls, list(usage.PROVIDER_USAGE_URLS["deepseek"]))

    def test_usage_urls_skip_generic_suffixes_for_an_empty_base(self):
        self.assertEqual(usage.cloud_usage_urls("openrouter", ""), ["https://openrouter.ai/api/v1/credits"])

    def test_fetch_propagates_keyboard_interrupt(self):
        def interrupted(*_args, **_kwargs):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            usage.fetch_cloud_usage("deepseek", "https://api.deepseek.com/v1", "sk-x", http_get=interrupted)

    def test_fetch_propagates_system_exit(self):
        def exiting(*_args, **_kwargs):
            raise SystemExit(2)

        with self.assertRaises(SystemExit):
            usage.fetch_cloud_usage("deepseek", "https://api.deepseek.com/v1", "sk-x", http_get=exiting)

    def test_fetch_still_wraps_ordinary_transport_errors(self):
        def broken(*_args, **_kwargs):
            raise ConnectionError("refused")

        with self.assertRaises(errors.CloudAIError) as caught:
            usage.fetch_cloud_usage("deepseek", "https://api.deepseek.com/v1", "sk-x", http_get=broken)
        self.assertEqual(caught.exception.key, errors.CLOUD_REQUEST_ERROR)


# --------------------------------------------------------------------------- #
# attachment bound semantics
# --------------------------------------------------------------------------- #
class AttachmentBoundTests(unittest.TestCase):
    """The 12 000-char cap is PER FILE, matching the C++ and pure-Python cores."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _file(self, name, size):
        path = self.root / name
        path.write_bytes(b"x" * size)
        return path

    def test_per_file_cap_matches_the_shared_core_contract(self):
        context = attachments.build_file_context([self._file("one.txt", 20000)])
        body = context.split("\n", 1)[1]
        self.assertEqual(len(body), attachments.MAX_FILE_CHARS + len(attachments.TRUNCATION_MARKER))

    def test_aggregate_is_unbounded_by_default_documented_risk(self):
        """Pins today's behaviour so the residual prompt-growth risk is visible."""
        paths = [self._file(f"f{index}.txt", 20000) for index in range(3)]
        context = attachments.build_file_context(paths)
        self.assertGreater(len(context), attachments.MAX_FILE_CHARS * 2)
        for index in range(3):
            self.assertIn(f"[File: f{index}.txt]", context)

    def test_optional_total_limit_bounds_the_aggregate(self):
        paths = [self._file(f"f{index}.txt", 20000) for index in range(3)]
        context = attachments.build_file_context(paths, total_limit=attachments.MAX_FILE_CHARS)
        body = context.split("\n", 1)[1]
        self.assertLessEqual(len(body), attachments.MAX_FILE_CHARS + len(attachments.TRUNCATION_MARKER))
        self.assertTrue(context.endswith(attachments.TRUNCATION_MARKER))

    def test_total_limit_keeps_earlier_files_and_marks_truncation(self):
        paths = [self._file("keep.txt", 100), self._file("drop.txt", 20000)]
        context = attachments.build_file_context(paths, total_limit=500)
        self.assertIn("[File: keep.txt]", context)
        self.assertTrue(context.endswith(attachments.TRUNCATION_MARKER))

    def test_total_limit_is_applied_through_messages_with_files(self):
        paths = [self._file(f"m{index}.txt", 20000) for index in range(3)]
        result = attachments.messages_with_files(
            [{"role": "user", "content": "q"}], paths, "deepseek-chat", total_limit=1000
        )
        self.assertLessEqual(len(result[-1]["content"]), 1000 + len(attachments.TRUNCATION_MARKER) + 200)

    def test_total_limit_absent_keeps_backwards_compatible_signature(self):
        path = self._file("small.txt", 10)
        self.assertEqual(
            attachments.messages_with_files([{"role": "user", "content": "q"}], [path], "deepseek-chat")[-1]["content"],
            attachments.messages_with_files([{"role": "user", "content": "q"}], [path], "deepseek-chat")[-1]["content"],
        )



# --------------------------------------------------------------------------- #
# cross-module guards
# --------------------------------------------------------------------------- #
class MaskingParityTests(unittest.TestCase):
    """The local mask predicate must not drift from the vault's definition."""

    def test_mask_predicates_agree_across_modules(self):
        import cloudsecrets

        values = ["", None, "********", "***", " ******** ", "*", "*x*", "abc", "sk-real-0123456789", 42]
        for value in values:
            with self.subTest(value=repr(value)):
                self.assertEqual(
                    providers.is_masked_secret(value), cloudsecrets.is_masked_key(value)
                )

    def test_legacy_public_view_exposes_no_secret(self):
        canonical = {
            "cloud": {
                "provider": "deepseek",
                "default_model": "deepseek-chat",
                "providers": {
                    "deepseek": {
                        "base_url": "https://api.deepseek.com/v1",
                        "model": "deepseek-reasoner",
                        "api_key": "sk-must-not-leak-0123456789",
                    }
                },
            }
        }
        view = providers.legacy_public_view(canonical)
        rendered = str(view)
        self.assertNotIn("sk-must-not-leak", rendered)
        self.assertNotIn("api_key", rendered)
        self.assertEqual(view["provider"], "deepseek")

    def test_legacy_public_view_round_trips_to_the_same_target(self):
        canonical = {
            "cloud": {
                "provider": "deepseek",
                "providers": {
                    "deepseek": {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-reasoner"}
                },
            }
        }
        view = providers.legacy_public_view(canonical)
        active = providers.resolve_active_provider({"cloud": {"public": view}})
        self.assertEqual(active.provider, "deepseek")
        self.assertEqual(active.base_url, "https://api.deepseek.com/v1")
        self.assertEqual(active.model, "deepseek-reasoner")

    def test_legacy_public_view_normalises_alias_provider_keys(self):
        canonical = {
            "cloud": {
                "provider": "custom",
                "providers": {"custom": {"base_url": "http://box:9000/v1", "model": "m1"}},
            }
        }
        view = providers.legacy_public_view(canonical)
        self.assertIn("openai_compatible", view["providers"])
        active = providers.resolve_active_provider({"cloud": {"public": view}})
        self.assertEqual(active.provider, "openai_compatible")
        self.assertEqual(active.base_url, "http://box:9000/v1")
        self.assertEqual(active.model, "m1")



if __name__ == "__main__":
    unittest.main()
