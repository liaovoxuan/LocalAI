"""Cross-implementation secret format + migration tests.

Verifies that the new vault agrees byte-for-byte with the golden vectors and
with the two existing CloudAI implementations (pure-Python core, C++ core
algorithm), and that legacy migration is lossless, idempotent and
permission-tolerant.

Run with::

    python3 -m unittest discover -s tooling/tests -p "test*.py"
"""
# PYTEST COLLECTION NOTE
# ----------------------
# This module keeps its dispatched name (``testcloudsecretinterop.py``) so
# WorkBuddy can import the vault module by its current name.  ``unittest``'s
# default pattern ``test*.py`` collects it; pytest's default
# ``python_files = test_*.py`` does not.  That cannot be fixed from inside a test
# module, so use::
#
#     python3 -m unittest discover -s tooling/tests -p "test*.py"
#     python3 -m pytest -o python_files="test_*.py test*.py" tooling/tests
#     python3 -m pytest tooling/tests/testcloudsecretinterop.py   # explicit paths

from __future__ import annotations

import base64
import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DESKTOP = _REPO_ROOT / "projects" / "LocalAI" / "desktop"
if str(_DESKTOP) not in sys.path:
    sys.path.insert(0, str(_DESKTOP))

import cloudsecrets as vault  # noqa: E402

VECTORS_PATH = Path(__file__).resolve().parent / "data" / "cloudaisecretvectors.json"
CORE_PATH = _REPO_ROOT / "projects" / "CloudAI" / "source" / "python" / "cloudai.py"


def load_vectors() -> dict:
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


def load_pure_python_core():
    """Import the CloudAI pure-Python core read-only (never modified).

    The reference module is registered only for the duration of ``exec_module`` and
    the previous ``sys.modules`` entry is restored afterwards, so loading the core
    leaves no import state behind for later tests.
    """
    if not CORE_PATH.is_file():
        return None
    spec = importlib.util.spec_from_file_location("_cloudai_core_reference", CORE_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        return None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(spec.name)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(spec.name, None)
        else:
            sys.modules[spec.name] = previous
    return module


def cpp_encode(value: str, salt: bytes = b"CloudAI") -> str:
    """Reproduce the CloudAI C++ core encoder (standard base64 + fixed salt)."""
    raw = value.encode("utf-8")
    mixed = bytes(byte ^ salt[index % len(salt)] for index, byte in enumerate(raw))
    return "v1:" + base64.b64encode(mixed).decode("ascii")


# --------------------------------------------------------------------------- #
# format interoperability
# --------------------------------------------------------------------------- #
class SecretFormatInteropTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.vectors = load_vectors()

    def test_vectors_file_is_well_formed(self):
        self.assertEqual(self.vectors["format"], "cloudai-secret-v1")
        self.assertEqual(self.vectors["canonical_salt_utf8"], "CloudAI")
        self.assertTrue(self.vectors["vectors"])
        self.assertTrue(self.vectors["legacy_hostname_vectors"])

    def test_encode_matches_every_golden_vector(self):
        for vector in self.vectors["vectors"]:
            with self.subTest(plaintext=vector["plaintext"]):
                self.assertEqual(vault.encode_secret(vector["plaintext"]), vector["blob"])

    def test_decode_matches_every_golden_vector(self):
        for vector in self.vectors["vectors"]:
            with self.subTest(blob=vector["blob"]):
                self.assertEqual(vault.decode_secret(vector["blob"]), vector["plaintext"])

    def test_cpp_core_algorithm_agrees_with_the_vault(self):
        for vector in self.vectors["vectors"]:
            with self.subTest(plaintext=vector["plaintext"]):
                self.assertEqual(cpp_encode(vector["plaintext"]), vector["blob"])
                self.assertEqual(vault.decode_secret(cpp_encode(vector["plaintext"])), vector["plaintext"])

    def test_pure_python_core_agrees_with_the_vault(self):
        core = load_pure_python_core()
        if core is None:  # pragma: no cover - core is present in-tree
            self.skipTest("CloudAI pure-Python core not available")
        for vector in self.vectors["vectors"]:
            with self.subTest(plaintext=vector["plaintext"]):
                encoded = core._encode_secret(vector["plaintext"])
                self.assertEqual(encoded, vector["blob"])
                self.assertEqual(core._decode_secret(vault.encode_secret(vector["plaintext"])), vector["plaintext"])
                self.assertEqual(vault.decode_secret(encoded), vector["plaintext"])

    def test_vault_does_not_use_the_legacy_hostname_salt(self):
        for vector in self.vectors["legacy_hostname_vectors"]:
            with self.subTest(blob=vector["blob"]):
                # Strict decode must reject it rather than return garbage.
                self.assertEqual(vault.decode_secret(vector["blob"]), "")
                # Explicit legacy salt still recovers it for migration.
                self.assertEqual(
                    vault.decode_with_salt(vector["blob"], vector["hostname_salt"].encode("utf-8")),
                    vector["plaintext"],
                )

    def test_strict_decode_never_echoes_the_input(self):
        for blob in ("", "plain-key-1234567890", "v1:", "v1:!!!!", None, 42):
            with self.subTest(blob=repr(blob)):
                self.assertEqual(vault.decode_secret(blob), "")

    def test_v1_has_no_integrity_check_known_limitation(self):
        """Documented limitation: a short all-zero payload decodes to text.

        ``v1`` is obfuscation without a checksum, so a garbage blob can decode
        to plausible text.  That is exactly why migration never *deletes* an
        undecodable value, and why a checksum is a v2 candidate.
        """
        self.assertEqual(vault.decode_secret("v1:AAAA"), "Clo")
        bad = "v1:" + base64.b64encode(b"\xff\xfe\xfd\xfc\xfb\xfa").decode("ascii")
        self.assertEqual(vault.decode_secret(bad), "")

    def test_encode_decode_round_trip(self):
        for value in ("sk-" + "a" * 40, "dummy", "密钥-unicode-测试", "x" * 300):
            with self.subTest(value=value[:16]):
                self.assertEqual(vault.decode_secret(vault.encode_secret(value)), value)

    def test_encode_of_empty_values_is_empty(self):
        for value in ("", None):
            self.assertEqual(vault.encode_secret(value), "")

    def test_masking_helpers(self):
        self.assertEqual(vault.mask_key("sk-real"), vault.MASK)
        self.assertEqual(vault.mask_key(""), "")
        self.assertTrue(vault.is_masked_key("********"))
        self.assertTrue(vault.is_masked_key("***"))
        self.assertFalse(vault.is_masked_key("sk-real-key-value"))
        self.assertFalse(vault.looks_like_plain_secret("********"))
        self.assertFalse(vault.looks_like_plain_secret("short"))
        self.assertTrue(vault.looks_like_plain_secret("sk-abcdefghijklmnop"))


# --------------------------------------------------------------------------- #
# malformed vaults
# --------------------------------------------------------------------------- #
class MalformedVaultTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name) / "LocalAI"
        vault.secret_store_path(self.data_dir).parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def _write_raw(self, text: str) -> Path:
        path = vault.secret_store_path(self.data_dir)
        path.write_text(text, encoding="utf-8")
        return path

    def test_invalid_json_loads_as_empty_without_raising(self):
        self._write_raw("{not json at all")
        self.assertEqual(vault.load_secrets(self.data_dir), {})

    def test_non_object_json_loads_as_empty(self):
        self._write_raw('["deepseek"]')
        self.assertEqual(vault.load_secrets(self.data_dir), {})

    def test_masked_and_empty_values_are_dropped(self):
        self._write_raw(json.dumps({"deepseek": "********", "openrouter": "", "x": None}))
        self.assertEqual(vault.load_secrets(self.data_dir), {})

    def test_unreadable_values_are_preserved_verbatim(self):
        good = vault.encode_secret("sk-good-key-0123456789")
        self._write_raw(json.dumps({"deepseek": good, "openrouter": "v1:!!!!not-base64", "siliconflow": 12345}))
        loaded = vault.load_secrets(self.data_dir)
        self.assertEqual(vault.read_secret(loaded["deepseek"]), "sk-good-key-0123456789")
        self.assertEqual(loaded["openrouter"], "v1:!!!!not-base64")
        self.assertEqual(loaded["siliconflow"], 12345)

    def test_normalize_reports_unrecoverable_entries(self):
        result = vault.normalize_secret_store({"a": "v1:!!!!", "b": {"nested": 1}, "c": "********"})
        self.assertIn("a", result.unrecoverable)
        self.assertIn("b", result.unrecoverable)
        self.assertEqual(result.dropped, ["c"])
        self.assertIn("b", result.entries)

    def test_wrong_salt_blob_is_preserved_not_silently_dropped(self):
        foreign = "v1:" + base64.b64encode(b"\x01\x02\x03\x04\x05\x06\x07\x08").decode("ascii")
        self._write_raw(json.dumps({"deepseek": foreign}))
        loaded = vault.load_secrets(self.data_dir)
        self.assertEqual(loaded["deepseek"], foreign)

    def test_plaintext_in_the_vault_is_upgraded_by_migration(self):
        self._write_raw(json.dumps({"deepseek": "sk-plaintext-key-0123456789"}))
        # Lenient reads keep hand-written plaintext working before migration.
        self.assertEqual(vault.get_api_key(self.data_dir, "deepseek"), "sk-plaintext-key-0123456789")
        report = vault.migrate_legacy_secrets(self.data_dir)
        self.assertTrue(report.changed)
        raw = json.loads(vault.secret_store_path(self.data_dir).read_text(encoding="utf-8"))
        self.assertTrue(str(raw["deepseek"]).startswith(vault.SECRET_PREFIX))
        self.assertEqual(vault.get_api_key(self.data_dir, "deepseek"), "sk-plaintext-key-0123456789")

    def test_load_secrets_is_read_only(self):
        self._write_raw(json.dumps({"deepseek": "sk-readonly-key-0123456789"}))
        before = vault.secret_store_path(self.data_dir).read_text(encoding="utf-8")
        vault.load_secrets(self.data_dir)
        vault.get_api_key(self.data_dir, "deepseek")
        self.assertEqual(vault.secret_store_path(self.data_dir).read_text(encoding="utf-8"), before)

    def test_corrupt_vault_is_quarantined_by_migration(self):
        path = self._write_raw("{broken")
        report = vault.migrate_legacy_secrets(
            self.data_dir, {"provider": "openai_official", "api_key": "sk-new-key-0123456789"}
        )
        self.assertTrue(Path(str(path) + ".corrupt").exists())
        self.assertTrue(report.changed)


# --------------------------------------------------------------------------- #
# migration
# --------------------------------------------------------------------------- #
class MigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name) / "LocalAI"

    def tearDown(self):
        self._tmp.cleanup()

    def test_plaintext_localai_key_is_migrated_and_reported(self):
        local_config = {"provider": "openai_official", "api_key": "sk-live-key-0123456789"}
        report = vault.migrate_legacy_secrets(self.data_dir, local_config)
        self.assertIn("openai_official", report.migrated)
        self.assertEqual(report.config_plaintext_keys, ["api_key"])
        self.assertEqual(vault.get_api_key(self.data_dir, "openai_official"), "sk-live-key-0123456789")
        self.assertTrue(report.changed)

    def test_legacy_openai_api_key_name_is_migrated(self):
        report = vault.migrate_legacy_secrets(
            self.data_dir, {"provider": "openai_compatible", "openai_api_key": "sk-legacy-key-0123456789"}
        )
        self.assertIn("openai_compatible", report.migrated)
        self.assertIn("openai_api_key", report.config_plaintext_keys)

    def test_lm_studio_key_maps_to_the_compatible_slot(self):
        report = vault.migrate_legacy_secrets(
            self.data_dir, {"provider": "lm_studio", "api_key": "sk-lmstudio-key-0123456789"}
        )
        self.assertIn("openai_compatible", report.migrated)

    def test_local_only_provider_key_is_not_misassigned(self):
        report = vault.migrate_legacy_secrets(
            self.data_dir, {"provider": "ollama", "api_key": "sk-unused-key-0123456789"}
        )
        self.assertEqual(report.migrated, [])
        self.assertEqual(report.config_plaintext_keys, [])
        self.assertEqual(vault.load_secrets(self.data_dir), {})

    def test_migration_is_idempotent(self):
        local_config = {"provider": "openai_official", "api_key": "sk-once-key-0123456789"}
        first = vault.migrate_legacy_secrets(self.data_dir, local_config)
        stored_after_first = vault.secret_store_path(self.data_dir).read_text(encoding="utf-8")

        second = vault.migrate_legacy_secrets(self.data_dir, local_config)
        self.assertIn("openai_official", first.migrated)
        self.assertEqual(second.migrated, [])
        self.assertFalse(second.changed)
        self.assertEqual(vault.secret_store_path(self.data_dir).read_text(encoding="utf-8"), stored_after_first)

    def test_migration_is_idempotent_when_repeated_many_times(self):
        local_config = {"provider": "openai_compatible", "api_key": "sk-repeat-key-0123456789"}
        vault.migrate_legacy_secrets(self.data_dir, local_config)
        for _ in range(3):
            report = vault.migrate_legacy_secrets(self.data_dir, local_config)
            self.assertFalse(report.changed)
            self.assertEqual(report.migrated, [])

    def test_legacy_public_config_providers_are_harvested(self):
        legacy = vault.legacy_public_config_path(self.data_dir)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(
            json.dumps(
                {
                    "provider": "deepseek",
                    "providers": {
                        "deepseek": {"api_key": "sk-legacy-public-0123456789"},
                        "openrouter": {"api_key": "********"},
                    },
                }
            ),
            encoding="utf-8",
        )
        report = vault.migrate_legacy_secrets(self.data_dir)
        self.assertIn("deepseek", report.migrated)
        self.assertEqual(vault.get_api_key(self.data_dir, "deepseek"), "sk-legacy-public-0123456789")
        self.assertEqual(vault.get_api_key(self.data_dir, "openrouter"), "")
        self.assertTrue(any("cloudai_config.json" in source for source in report.sources))

    def test_sibling_cloudai_install_is_harvested(self):
        sibling = self.data_dir.parent / "CloudAI" / vault.CONFIG_SUBDIR / vault.SECRET_FILENAME
        sibling.parent.mkdir(parents=True, exist_ok=True)
        sibling.write_text(
            json.dumps({"siliconflow": vault.encode_secret("sk-sibling-key-0123456789")}),
            encoding="utf-8",
        )
        report = vault.migrate_legacy_secrets(self.data_dir)
        self.assertIn("siliconflow", report.migrated)
        self.assertEqual(vault.get_api_key(self.data_dir, "siliconflow"), "sk-sibling-key-0123456789")

    def test_existing_vault_entries_win_over_legacy_sources(self):
        vault.set_api_key(self.data_dir, "deepseek", "sk-canonical-key-0123456789")
        legacy = vault.legacy_public_config_path(self.data_dir)
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({"providers": {"deepseek": {"api_key": "sk-stale-key-0123456789"}}}), encoding="utf-8")
        report = vault.migrate_legacy_secrets(self.data_dir)
        self.assertEqual(vault.get_api_key(self.data_dir, "deepseek"), "sk-canonical-key-0123456789")
        self.assertIn("deepseek", report.skipped)

    def test_legacy_hostname_blob_is_recovered(self):
        vector = load_vectors()["legacy_hostname_vectors"][0]
        path = vault.secret_store_path(self.data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"deepseek": vector["blob"]}), encoding="utf-8")

        report = vault.migrate_legacy_secrets(
            self.data_dir, hostname_salt=vector["hostname_salt"], hostname_salt_fallback=True
        )
        self.assertIn("deepseek", report.migrated)
        self.assertEqual(vault.get_api_key(self.data_dir, "deepseek"), vector["plaintext"])
        self.assertEqual(report.failed, [])
        self.assertNotIn("deepseek", report.unrecoverable)

    def test_hostname_fallback_can_be_disabled(self):
        vector = load_vectors()["legacy_hostname_vectors"][0]
        path = vault.secret_store_path(self.data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"deepseek": vector["blob"]}), encoding="utf-8")
        report = vault.migrate_legacy_secrets(
            self.data_dir, hostname_salt=vector["hostname_salt"], hostname_salt_fallback=False
        )
        self.assertEqual(report.migrated, [])
        self.assertIn("deepseek", report.unrecoverable)

    def test_unrecoverable_blob_is_never_lost(self):
        path = vault.secret_store_path(self.data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Not valid UTF-8 once un-XORed, so it can never be mistaken for a key.
        undecodable = "v1:" + base64.b64encode(b"\xff\xfe\xfd\xfc\xfb\xfa").decode("ascii")
        path.write_text(json.dumps({"deepseek": undecodable}), encoding="utf-8")
        report = vault.migrate_legacy_secrets(self.data_dir, hostname_salt="nope")
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["deepseek"], undecodable)
        self.assertTrue(report.failed or report.unrecoverable)

    def test_migration_marker_is_written_once(self):
        local_config = {"provider": "openai_official", "api_key": "sk-marker-key-0123456789"}
        first = vault.migrate_legacy_secrets(self.data_dir, local_config)
        self.assertTrue(first.marker_written)
        self.assertTrue(vault.migration_marker_path(self.data_dir).exists())
        second = vault.migrate_legacy_secrets(self.data_dir, local_config)
        self.assertFalse(second.marker_written)

    def test_migration_with_no_sources_is_a_no_op(self):
        report = vault.migrate_legacy_secrets(self.data_dir, {})
        self.assertEqual(report.migrated, [])
        self.assertFalse(report.changed)
        self.assertFalse(vault.secret_store_path(self.data_dir).exists())

    def test_report_is_json_serialisable(self):
        report = vault.migrate_legacy_secrets(
            self.data_dir, {"provider": "openai_official", "api_key": "sk-json-key-0123456789"}
        )
        payload = json.loads(json.dumps(report.to_dict()))
        self.assertIn("openai_official", payload["migrated"])
        self.assertEqual(payload["config_plaintext_keys"], ["api_key"])


# --------------------------------------------------------------------------- #
# storage, permissions and public-config hygiene
# --------------------------------------------------------------------------- #
class SecretStorageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name) / "LocalAI"

    def tearDown(self):
        self._tmp.cleanup()

    def test_set_and_get_api_key_round_trip(self):
        vault.set_api_key(self.data_dir, "deepseek", "sk-round-trip-0123456789")
        self.assertEqual(vault.get_api_key(self.data_dir, "deepseek"), "sk-round-trip-0123456789")
        self.assertEqual(vault.get_api_key(self.data_dir, "openai_official"), "")

    def test_set_api_key_accepts_aliases(self):
        vault.set_api_key(self.data_dir, "custom", "sk-alias-key-0123456789")
        self.assertEqual(vault.get_api_key(self.data_dir, "openai_compatible"), "sk-alias-key-0123456789")

    def test_empty_value_clears_only_that_provider(self):
        vault.set_api_key(self.data_dir, "deepseek", "sk-deep-0123456789")
        vault.set_api_key(self.data_dir, "openrouter", "sk-router-0123456789")
        vault.set_api_key(self.data_dir, "deepseek", "")
        self.assertEqual(vault.get_api_key(self.data_dir, "deepseek"), "")
        self.assertEqual(vault.get_api_key(self.data_dir, "openrouter"), "sk-router-0123456789")

    def test_masked_value_never_overwrites_a_real_key(self):
        vault.set_api_key(self.data_dir, "deepseek", "sk-real-key-0123456789")
        vault.set_api_key(self.data_dir, "deepseek", vault.MASK)
        self.assertEqual(vault.get_api_key(self.data_dir, "deepseek"), "sk-real-key-0123456789")

    def test_stored_values_are_always_prefixed(self):
        vault.set_api_key(self.data_dir, "deepseek", "sk-prefixed-0123456789")
        stored = json.loads(vault.secret_store_path(self.data_dir).read_text(encoding="utf-8"))
        self.assertTrue(stored["deepseek"].startswith(vault.SECRET_PREFIX))
        self.assertNotIn("sk-prefixed", vault.secret_store_path(self.data_dir).read_text(encoding="utf-8"))

    def test_vault_file_is_owner_only_on_posix(self):
        if os.name != "posix":  # pragma: no cover - platform dependent
            self.skipTest("POSIX only")
        vault.set_api_key(self.data_dir, "deepseek", "sk-mode-key-0123456789")
        mode = stat.S_IMODE(vault.secret_store_path(self.data_dir).stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_save_succeeds_when_chmod_fails(self):
        with patch.object(vault.os, "chmod", side_effect=OSError("not permitted")):
            path = vault.save_secrets(self.data_dir, {"deepseek": vault.encode_secret("sk-chmod-0123456789")})
        self.assertTrue(path.exists())
        self.assertEqual(vault.get_api_key(self.data_dir, "deepseek"), "sk-chmod-0123456789")

    def test_load_returns_empty_when_the_vault_is_unreadable(self):
        if os.name != "posix" or os.geteuid() == 0:  # pragma: no cover - needs a normal user
            self.skipTest("requires a non-root POSIX user")
        vault.set_api_key(self.data_dir, "deepseek", "sk-unreadable-0123456789")
        path = vault.secret_store_path(self.data_dir)
        os.chmod(path, 0o000)
        try:
            self.assertEqual(vault.load_secrets(self.data_dir), {})
        finally:
            os.chmod(path, 0o600)

    def test_public_config_projection_never_contains_a_real_key(self):
        vault.set_api_key(self.data_dir, "deepseek", "sk-must-not-leak-0123456789")
        public = {"providers": {"deepseek": {"api_key": vault.mask_key(vault.get_api_key(self.data_dir, "deepseek"))}}}
        rendered = json.dumps(public)
        self.assertIn(vault.MASK, rendered)
        self.assertNotIn("sk-must-not-leak", rendered)


# --------------------------------------------------------------------------- #
# concurrent-host hardening: legacy vaults must survive migration intact
# --------------------------------------------------------------------------- #
class LegacyVaultHardeningTests(unittest.TestCase):
    """Regression tests for loss discovered by auditing migration sources."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name) / "LocalAI"

    def tearDown(self):
        self._tmp.cleanup()

    def _sibling_vault(self, payload) -> Path:
        path = self.data_dir.parent / "CloudAI" / vault.CONFIG_SUBDIR / vault.SECRET_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_hostname_blob_from_a_sibling_vault_is_recovered(self):
        """A 1.0.1 vault living in the sibling CloudAI dir must not be dropped."""
        vector = load_vectors()["legacy_hostname_vectors"][0]
        self._sibling_vault({"deepseek": vector["blob"]})

        report = vault.migrate_legacy_secrets(
            self.data_dir, hostname_salt=vector["hostname_salt"], hostname_salt_fallback=True
        )
        self.assertIn("deepseek", report.migrated)
        self.assertEqual(vault.get_api_key(self.data_dir, "deepseek"), vector["plaintext"])

    def test_unreadable_legacy_blob_is_carried_into_the_vault(self):
        """Even when recovery fails, the raw value must reach the canonical vault."""
        undecodable = "v1:" + base64.b64encode(b"\xff\xfe\xfd\xfc\xfb\xfa").decode("ascii")
        self._sibling_vault({"siliconflow": undecodable})

        report = vault.migrate_legacy_secrets(
            self.data_dir, hostname_salt="definitely-not-the-host", hostname_salt_fallback=True
        )
        stored = json.loads(vault.secret_store_path(self.data_dir).read_text(encoding="utf-8"))
        self.assertEqual(stored["siliconflow"], undecodable)
        self.assertTrue(report.failed or report.unrecoverable)

    def test_readable_legacy_vault_still_beats_nothing(self):
        self._sibling_vault({"openrouter": vault.encode_secret("sk-router-key-0123456789")})
        report = vault.migrate_legacy_secrets(self.data_dir)
        self.assertIn("openrouter", report.migrated)
        self.assertEqual(vault.get_api_key(self.data_dir, "openrouter"), "sk-router-key-0123456789")

    def test_sibling_vault_never_overwrites_the_canonical_vault(self):
        vault.set_api_key(self.data_dir, "deepseek", "sk-canonical-key-0123456789")
        self._sibling_vault({"deepseek": vault.encode_secret("sk-stale-sibling-0123456789")})
        vault.migrate_legacy_secrets(self.data_dir)
        self.assertEqual(vault.get_api_key(self.data_dir, "deepseek"), "sk-canonical-key-0123456789")

    def test_set_api_key_with_a_mask_does_not_create_a_vault(self):
        vault.set_api_key(self.data_dir, "deepseek", vault.MASK)
        self.assertFalse(vault.secret_store_path(self.data_dir).exists())

    def test_clearing_an_absent_provider_does_not_create_a_vault(self):
        vault.set_api_key(self.data_dir, "deepseek", "")
        self.assertFalse(vault.secret_store_path(self.data_dir).exists())

    def test_set_api_key_is_stable_when_repeated(self):
        vault.set_api_key(self.data_dir, "deepseek", "sk-stable-key-0123456789")
        first = vault.secret_store_path(self.data_dir).read_text(encoding="utf-8")
        vault.set_api_key(self.data_dir, "deepseek", "sk-stable-key-0123456789")
        self.assertEqual(vault.secret_store_path(self.data_dir).read_text(encoding="utf-8"), first)

    def test_v1_interoperability_is_unchanged_by_this_batch(self):
        """Guard: the hardening must not alter the v1 wire format."""
        for vector in load_vectors()["vectors"]:
            with self.subTest(plaintext=vector["plaintext"]):
                self.assertEqual(vault.encode_secret(vector["plaintext"]), vector["blob"])
                self.assertEqual(vault.decode_secret(vector["blob"]), vector["plaintext"])
        self.assertEqual(vault.SECRET_SALT, b"CloudAI")
        self.assertEqual(vault.SECRET_PREFIX, "v1:")



if __name__ == "__main__":
    unittest.main()
