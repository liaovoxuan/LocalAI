#!/usr/bin/env python3
"""Self-tests for the release-manifest certification tool.

Run with either of:

    python3 tools/virtualization-conformance/test_verify_release_manifest.py
    python3 -m unittest discover -s tools/virtualization-conformance -p 'test_*.py'

Offline and standard-library only. Tests that need the real packaging output are
skipped when it is absent, so the suite is safe on any checkout.

Two tests here are regression tests for bugs found while building the tool:

* `test_firmware_count_matches_generator_scope_and_case` -- an earlier version
  walked all of `runtime/` instead of `runtime/qemu/` and matched suffixes
  case-insensitively, which invented `firmware-count-mismatch` findings.
* `test_symlink_escape_*` -- the symlink scenario is built in a temporary
  sandbox, because build products must never be modified to test them.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import run_conformance as rc  # noqa: E402
import verify_release_manifest as vrm  # noqa: E402

PROJECT_ROOT = vrm.PROJECT_ROOT
REAL_ON = os.path.join(PROJECT_ROOT, "build-package-audit", vrm.MANIFEST_NAME)
REAL_OFF = os.path.join(PROJECT_ROOT, "build-package-audit-nocontrib", vrm.MANIFEST_NAME)


def load(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def codes(report):
    return sorted({f.code for f in report.errors})


def make_bundle(tmpdir, *, firmware=(), support=()):
    """Create a minimal but structurally real bundle in a temp directory."""
    bundle = os.path.join(tmpdir, "VirtualWorld.app")
    qemu_bin = os.path.join(bundle, "Contents", "Resources", "runtime", "qemu",
                            "macos", "apple-silicon", "bin")
    os.makedirs(qemu_bin, exist_ok=True)
    os.makedirs(os.path.join(bundle, "Contents", "MacOS"), exist_ok=True)
    main = os.path.join(bundle, "Contents", "MacOS", "VirtualWorld")
    with open(main, "wb") as handle:
        handle.write(b"main-binary")
    binaries = []
    for name, payload in firmware:
        path = os.path.join(qemu_bin, name)
        with open(path, "wb") as handle:
            handle.write(payload)
        binaries.append({
            "path": os.path.relpath(path, bundle),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        })
    support_dir = os.path.join(bundle, "Contents", "Resources", "support")
    for name, payload in support:
        os.makedirs(support_dir, exist_ok=True)
        with open(os.path.join(support_dir, name), "wb") as handle:
            handle.write(payload)
    return bundle, main, binaries


def make_manifest(bundle, main, binaries, *, contrib=True, firmware_count=0,
                  support_count=0, gate=None):
    with open(main, "rb") as handle:
        payload = handle.read()
    return {
        "schema": vrm.SCHEMA,
        "product_version": "1.0",
        "generated_at": "2026-10-05T04:04:52Z",
        "host": {"os": "Darwin", "arch": "arm64"},
        "verified": False,
        "real_hardware_verified": False,
        "contrib_backends": contrib,
        "backends": {"macos": True, "windows": True, "linux_kvm": contrib, "harmony": contrib},
        "main_program": {"path": main,
                         "sha256": hashlib.sha256(payload).hexdigest(),
                         "size": len(payload)},
        "qemu_binaries": binaries,
        "firmware_count": firmware_count,
        "support_count": support_count,
        "gate": gate if gate is not None else {
            "status": "passed", "checks": [], "failure_count": 0, "failures": [],
        },
    }


class Sandbox(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def verify(self, document, **kwargs):
        options = vrm.VerificationOptions(**{"crosscheck": False, **kwargs})
        path = os.path.join(self.tmp, vrm.MANIFEST_NAME)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        return vrm.verify_manifest(path, document, options)


class TestDiscovery(unittest.TestCase):
    def test_finds_both_real_manifests(self):
        paths = vrm.find_manifest_paths(vrm.DEFAULT_SEARCH_ROOTS)
        self.assertEqual(len(paths), 2, paths)

    def test_classifies_on_and_off_by_declared_flag(self):
        paths = vrm.find_manifest_paths(vrm.DEFAULT_SEARCH_ROOTS)
        result = vrm.classify_candidates(paths)
        self.assertFalse(result["resolution_required"], result["problems"])
        self.assertEqual(len(result["on"]), 1)
        self.assertEqual(len(result["off"]), 1)
        self.assertTrue(result["on"][0]["path"].endswith("build-package-audit/" + vrm.MANIFEST_NAME))

    def test_duplicate_on_candidates_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for index in range(2):
                target = os.path.join(tmp, f"build{index}", vrm.MANIFEST_NAME)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "w", encoding="utf-8") as handle:
                    json.dump({"schema": vrm.SCHEMA, "contrib_backends": True}, handle)
                paths.append(target)
            result = vrm.classify_candidates(paths)
            self.assertTrue(result["resolution_required"])
            self.assertTrue(any("contrib_backends=true" in p for p in result["problems"]))

    def test_unclassifiable_candidate_is_ambiguous_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "x", vrm.MANIFEST_NAME)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                json.dump({"schema": vrm.SCHEMA}, handle)  # no contrib_backends
            result = vrm.classify_candidates([target])
            self.assertTrue(result["resolution_required"])
            self.assertEqual(len(result["ambiguous"]), 1)
            self.assertIn("cannot be inferred", result["ambiguous"][0]["problem"])

    def test_unreadable_candidate_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, vrm.MANIFEST_NAME)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("{ not json")
            result = vrm.classify_candidates([target])
            self.assertTrue(result["resolution_required"])
            self.assertEqual(len(result["ambiguous"]), 1)

    def test_duplicate_on_candidates_block_with_candidates_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            for index in range(2):
                target = os.path.join(tmp, f"b{index}", vrm.MANIFEST_NAME)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "w", encoding="utf-8") as handle:
                    json.dump({"schema": vrm.SCHEMA, "contrib_backends": True}, handle)
            args = vrm.build_parser().parse_args(
                ["--search-root", tmp, "--quiet", "--out-dir", tmp])
            report = vrm.run_certification(args)
            self.assertEqual(report["certification_status"], "blocked")
            self.assertTrue(report["blocking"]["candidates"])

    def test_unreadable_explicit_manifest_blocks_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = os.path.join(tmp, "broken.json")
            with open(bad, "w", encoding="utf-8") as handle:
                handle.write("{ not json")
            args = vrm.build_parser().parse_args(
                ["--manifest-on", bad, "--quiet", "--out-dir", tmp])
            report = vrm.run_certification(args)
            self.assertEqual(report["certification_status"], "blocked")
            self.assertIn("could not be read or parsed", report["blocking"]["reason"])


class TestPathSafety(unittest.TestCase):
    def test_safe_relative_paths(self):
        for value in ("Contents/Resources/runtime/qemu/a.bin", "a.bin", "./a/b.bin"):
            safe, why = vrm.is_safe_relative_path(value)
            self.assertTrue(safe, f"{value}: {why}")

    def test_absolute_paths_rejected(self):
        for value in ("/usr/bin/qemu-system-aarch64", "~/qemu", "C:\\qemu\\x.exe"):
            safe, why = vrm.is_safe_relative_path(value)
            self.assertFalse(safe, value)

    def test_traversal_rejected(self):
        for value in ("../x", "a/../../x", "a/..", "Contents/../../etc/passwd"):
            safe, why = vrm.is_safe_relative_path(value)
            self.assertFalse(safe, f"{value} should be rejected")

    def test_empty_rejected(self):
        for value in ("", "   "):
            safe, _ = vrm.is_safe_relative_path(value)
            self.assertFalse(safe)

    def test_escapes_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            inside = os.path.join(tmp, "bundle", "Contents")
            os.makedirs(inside)
            self.assertFalse(vrm.escapes_root(os.path.join(tmp, "bundle"), inside))
            self.assertTrue(vrm.escapes_root(os.path.join(tmp, "bundle"), tmp))

    def test_escapes_root_through_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = os.path.join(tmp, "bundle")
            os.makedirs(bundle)
            outside = os.path.join(tmp, "outside")
            os.makedirs(outside)
            secret = os.path.join(outside, "secret.bin")
            with open(secret, "wb") as handle:
                handle.write(b"x")
            link = os.path.join(bundle, "link.bin")
            os.symlink(secret, link)
            self.assertTrue(vrm.escapes_root(bundle, link))


class TestFirmwareCounting(Sandbox):
    """Regression tests for the scope/case bugs in the firmware recount."""

    def test_firmware_count_matches_generator_scope_and_case(self):
        bundle, _main, _bins = make_bundle(
            self.tmp,
            firmware=[("lower.bin", b"a"), ("upper.BIN", b"b"), ("code.fd", b"c")],
        )
        qemu_dir = os.path.join(bundle, "Contents", "Resources", "runtime", "qemu",
                                "macos", "apple-silicon", "bin")
        # A file outside runtime/qemu must not be counted, even with a .rom name.
        other = os.path.join(bundle, "Contents", "Resources", "runtime",
                             "system-software", "roms")
        os.makedirs(other, exist_ok=True)
        with open(os.path.join(other, "Mac128k.rom"), "wb") as handle:
            handle.write(b"d")
        # ...and neither must an uppercase-suffixed file inside the qemu tree.
        self.assertEqual(vrm.count_firmware(bundle), 2,
                         "only lower.bin and code.fd are in scope: *.BIN is case-sensitive, "
                         "and runtime/system-software is outside the glob")
        self.assertEqual(len(os.listdir(qemu_dir)), 3)

    def test_support_count_walks_the_support_tree(self):
        bundle, _main, _bins = make_bundle(self.tmp, support=[("a.qcow2", b"1"), ("b.json", b"2")])
        nested = os.path.join(bundle, "Contents", "Resources", "support", "nested")
        os.makedirs(nested, exist_ok=True)
        with open(os.path.join(nested, "c.txt"), "wb") as handle:
            handle.write(b"3")
        self.assertEqual(vrm.count_support(bundle), 3)

    def test_missing_directories_count_zero(self):
        bundle, _main, _bins = make_bundle(self.tmp)
        self.assertEqual(vrm.count_firmware(bundle), 0)
        self.assertEqual(vrm.count_support(bundle), 0)


class TestStructuralValidation(Sandbox):
    def _valid(self, **kwargs):
        bundle, main, binaries = make_bundle(
            self.tmp, firmware=[("q.bin", b"payload")], support=[("s.qcow2", b"s")])
        document = make_manifest(bundle, main, binaries, firmware_count=1, support_count=1,
                                 **kwargs)
        return document

    def test_valid_manifest_passes(self):
        report = self.verify(self._valid())
        self.assertTrue(report.ok, codes(report))

    def test_schema_mismatch(self):
        document = self._valid()
        document["schema"] = "virtualworld.release/2"
        self.assertIn("schema-unknown", codes(self.verify(document)))

    def test_missing_required_fields(self):
        for field in ("schema", "product_version", "generated_at", "host", "verified",
                      "real_hardware_verified", "contrib_backends", "backends",
                      "main_program", "qemu_binaries", "firmware_count", "support_count", "gate"):
            document = self._valid()
            document.pop(field, None)
            self.assertIn("missing-field", codes(self.verify(document)), field)

    def test_verified_must_be_false(self):
        for value in (True, 1, "false", None):
            document = self._valid()
            document["verified"] = value
            detected = codes(self.verify(document))
            self.assertTrue({"verified-not-false", "wrong-field-type"} & set(detected), value)

    def test_real_hardware_verified_must_be_false(self):
        document = self._valid()
        document["real_hardware_verified"] = True
        self.assertIn("real-hardware-verified-not-false", codes(self.verify(document)))

    def test_timestamp_forms(self):
        for value, expected in (("2026-10-05 04:04:52", "generated-at-invalid"),
                                ("2026-13-45T99:99:99Z", "generated-at-invalid"),
                                ("not-a-time", "generated-at-invalid"),
                                ("2026-10-05T04:04:52+00:00", "generated-at-invalid")):
            document = self._valid()
            document["generated_at"] = value
            self.assertIn(expected, codes(self.verify(document)), value)
        document = self._valid()
        self.assertTrue(self.verify(document).ok)

    def test_host_types(self):
        document = self._valid()
        document["host"]["os"] = "Plan9"
        self.assertIn("host-os-invalid", codes(self.verify(document)))

        document = self._valid()
        document["host"]["os"] = 7
        self.assertIn("wrong-field-type", codes(self.verify(document)))

        document = self._valid()
        document["host"]["arch"] = ""
        self.assertIn("empty-field", codes(self.verify(document)))

    def test_backend_flag_drift(self):
        document = self._valid()
        document["backends"]["linux_kvm"] = False  # contrib is True
        self.assertIn("backend-flag-drift", codes(self.verify(document)))

        document = self._valid(contrib=False)
        document["backends"]["harmony"] = True  # contrib is False
        self.assertIn("backend-flag-drift", codes(self.verify(document)))

    def test_backend_flag_type(self):
        document = self._valid()
        document["backends"]["macos"] = "yes"
        self.assertIn("backend-flag-not-bool", codes(self.verify(document)))

    def test_count_types(self):
        document = self._valid()
        document["support_count"] = "1"
        self.assertIn("count-not-int", codes(self.verify(document)))

    def test_count_recount_mismatch(self):
        document = self._valid()
        document["firmware_count"] = 99
        self.assertIn("firmware-count-mismatch", codes(self.verify(document)))
        document = self._valid()
        document["support_count"] = 99
        self.assertIn("support-count-mismatch", codes(self.verify(document)))


class TestGateValidation(Sandbox):
    def _base(self, gate):
        bundle, main, binaries = make_bundle(self.tmp, firmware=[("q.bin", b"p")])
        return make_manifest(bundle, main, binaries, firmware_count=1, gate=gate)

    def test_false_green_with_failure_count(self):
        document = self._base({"status": "passed", "checks": [], "failure_count": 2,
                               "failures": ["a", "b"]})
        self.assertIn("gate-false-green", codes(self.verify(document)))

    def test_false_green_with_failing_check(self):
        document = self._base({
            "status": "passed",
            "checks": [{"check": "x", "ok": False, "skipped": False, "message": "boom"}],
            "failure_count": 0, "failures": [],
        })
        detected = codes(self.verify(document))
        self.assertIn("gate-false-green", detected)
        self.assertIn("gate-check-failed", detected)

    def test_failure_count_mismatch(self):
        document = self._base({"status": "failed", "checks": [], "failure_count": 5,
                               "failures": ["only-one"]})
        self.assertIn("gate-failure-count-mismatch", codes(self.verify(document)))

    def test_failure_count_not_int(self):
        document = self._base({"status": "failed", "checks": [], "failure_count": "5",
                               "failures": []})
        self.assertIn("gate-failure-count-not-int", codes(self.verify(document)))

    def test_status_vocabulary(self):
        document = self._base({"status": "probably-fine", "checks": [], "failure_count": 0,
                               "failures": []})
        self.assertIn("gate-status-invalid", codes(self.verify(document)))

    def test_skipped_check_is_not_a_failure(self):
        document = self._base({
            "status": "passed",
            "checks": [{"check": "x", "ok": False, "skipped": True, "message": "skipped"}],
            "failure_count": 0, "failures": [],
        })
        detected = codes(self.verify(document))
        self.assertNotIn("gate-false-green", detected)
        self.assertNotIn("gate-check-failed", detected)

    def test_failed_status_with_failures_is_fine(self):
        document = self._base({"status": "failed", "checks": [], "failure_count": 1,
                               "failures": ["a"]})
        self.assertTrue(self.verify(document).ok, codes(self.verify(document)))


class TestFileValidation(Sandbox):
    def _valid(self):
        bundle, main, binaries = make_bundle(self.tmp, firmware=[("q.bin", b"payload")])
        return make_manifest(bundle, main, binaries, firmware_count=1), bundle

    def test_main_hash_mismatch(self):
        document, _ = self._valid()
        document["main_program"]["sha256"] = "0" * 64
        self.assertIn("main-program-sha256-mismatch", codes(self.verify(document)))

    def test_main_hash_format(self):
        document, _ = self._valid()
        document["main_program"]["sha256"] = "XYZ"
        self.assertIn("main-program-sha256-format", codes(self.verify(document)))

    def test_main_size_mismatch(self):
        document, _ = self._valid()
        document["main_program"]["size"] += 3
        self.assertIn("main-program-size-mismatch", codes(self.verify(document)))

    def test_main_missing_file(self):
        document, _ = self._valid()
        document["main_program"]["path"] = os.path.join(self.tmp, "nope", "VirtualWorld")
        self.assertIn("main-program-missing", codes(self.verify(document)))

    def test_main_path_escaping_its_build_tree(self):
        document, _ = self._valid()
        document["main_program"]["path"] = "/usr/bin/true"
        self.assertIn("main-program-path-escape", codes(self.verify(document)))

    def test_qemu_hash_and_size(self):
        document, _ = self._valid()
        document["qemu_binaries"][0]["sha256"] = "a" * 64
        self.assertIn("qemu-sha256-mismatch", codes(self.verify(document)))
        document, _ = self._valid()
        document["qemu_binaries"][0]["size"] += 1
        self.assertIn("qemu-size-mismatch", codes(self.verify(document)))

    def test_qemu_path_safety(self):
        for value, expected in (("/usr/bin/qemu", "qemu-path-absolute"),
                                ("../../etc/passwd", "qemu-path-traversal"),
                                ("Contents/../outside/qemu", "qemu-path-traversal")):
            document, _ = self._valid()
            document["qemu_binaries"][0]["path"] = value
            self.assertIn(expected, codes(self.verify(document)), value)

    def test_qemu_duplicate_paths(self):
        document, _ = self._valid()
        document["qemu_binaries"].append(dict(document["qemu_binaries"][0]))
        self.assertIn("qemu-path-duplicate", codes(self.verify(document)))

    def test_qemu_duplicate_with_main(self):
        document, _ = self._valid()
        document["qemu_binaries"][0]["path"] = "Contents/MacOS/" + os.path.basename(
            document["main_program"]["path"])
        self.assertIn("qemu-path-duplicate-with-main", codes(self.verify(document)))

    def test_qemu_empty_list(self):
        document, _ = self._valid()
        document["qemu_binaries"] = []
        self.assertIn("qemu-list-empty", codes(self.verify(document)))

    def test_qemu_symlink_escape(self):
        document, bundle = self._valid()
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside, exist_ok=True)
        secret = os.path.join(outside, "secret.bin")
        with open(secret, "wb") as handle:
            handle.write(b"secret")
        link = os.path.join(bundle, "Contents", "Resources", "runtime", "qemu",
                            "macos", "apple-silicon", "bin", "evil")
        os.symlink(secret, link)
        document["qemu_binaries"].append({
            "path": "Contents/Resources/runtime/qemu/macos/apple-silicon/bin/evil",
            "sha256": hashlib.sha256(b"secret").hexdigest(), "size": 6,
        })
        self.assertIn("qemu-path-symlink", codes(self.verify(document)))

    def test_qemu_missing_file(self):
        document, _ = self._valid()
        document["qemu_binaries"][0]["path"] = "Contents/Resources/runtime/qemu/nope/x"
        self.assertIn("qemu-missing", codes(self.verify(document)))

    def test_no_rehash_reports_but_does_not_recompute(self):
        document, _ = self._valid()
        document["qemu_binaries"][0]["sha256"] = "0" * 64
        report = self.verify(document, rehash=False)
        self.assertNotIn("qemu-sha256-mismatch", codes(report))
        self.assertEqual(report.stats["hashes_recomputed"], 0)  # type: ignore[attr-defined]


class TestCrossChecks(Sandbox):
    def _build_dir(self, *, contrib="ON", cache=True):
        bundle, main, binaries = make_bundle(self.tmp, firmware=[("q.bin", b"p")])
        document = make_manifest(bundle, main, binaries, firmware_count=1)
        if cache:
            with open(os.path.join(self.tmp, "CMakeCache.txt"), "w", encoding="utf-8") as handle:
                handle.write(f"VIRTUALWORLD_INCLUDE_CONTRIB_BACKENDS:BOOL={contrib}\n")
        return document

    def verify_with_crosscheck(self, document):
        path = os.path.join(self.tmp, vrm.MANIFEST_NAME)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        return vrm.verify_manifest(path, document,
                                   vrm.VerificationOptions(rehash=True, crosscheck=True))

    def test_cmake_cache_agrees(self):
        document = self._build_dir(contrib="ON")
        report = self.verify_with_crosscheck(document)
        self.assertTrue(report.ok, codes(report))
        self.assertEqual(report.stats["crosschecks"]["cmake_cache"]["status"], "checked")

    def test_cmake_cache_mismatch(self):
        document = self._build_dir(contrib="OFF")  # manifest says contrib True
        self.assertIn("crosscheck-cmake-cache-mismatch", codes(self.verify_with_crosscheck(document)))

    def test_cmake_cache_absent_is_skipped_not_failed(self):
        document = self._build_dir(cache=False)
        report = self.verify_with_crosscheck(document)
        self.assertNotIn("crosscheck-cmake-cache-mismatch", codes(report))
        self.assertEqual(report.stats["crosschecks"]["cmake_cache"]["status"], "skipped")

    def test_executable_skipped_when_not_runnable(self):
        document = self._build_dir()
        document["main_program"]["path"] = os.path.join(self.tmp, "VirtualWorld.app",
                                                        "Contents", "MacOS", "Gone")
        report = self.verify_with_crosscheck(document)
        self.assertEqual(report.stats["crosschecks"]["executable"]["status"], "skipped")
        self.assertIn("crosscheck-executable-skipped", [f.code for f in report.findings])

    def test_refuses_to_crosscheck_another_builds_binary(self):
        document = self._build_dir()
        document["main_program"]["path"] = "/usr/bin/true"
        report = self.verify_with_crosscheck(document)
        self.assertEqual(report.stats["crosschecks"]["executable"]["status"], "refused")
        self.assertIn("crosscheck-other-build-binary", codes(report))


class TestMutationSuite(unittest.TestCase):
    REQUIRED_CATEGORIES = {
        "verified-flag", "hash-size", "path-safety", "backend-drift",
        "gate", "missing-qemu", "time-host",
    }

    def setUp(self):
        if not os.path.isfile(REAL_ON):
            self.skipTest("real contrib-ON manifest not present")
        self.base = load(REAL_ON)
        self.bundle_root = os.path.join(os.path.dirname(REAL_ON), "VirtualWorld.app")

    def test_suite_is_deterministic(self):
        first = vrm.build_mutations(copy.deepcopy(self.base), 4321)
        second = vrm.build_mutations(copy.deepcopy(self.base), 4321)
        self.assertEqual([m["name"] for m in first], [m["name"] for m in second])
        other = vrm.build_mutations(copy.deepcopy(self.base), 999)
        self.assertNotEqual([m["name"] for m in first], [m["name"] for m in other])

    def test_required_categories_covered(self):
        categories = {m["category"] for m in vrm.build_mutations(self.base, 1)}
        self.assertEqual(self.REQUIRED_CATEGORIES - categories, set())

    def test_expected_codes_exist_in_the_tool(self):
        source = inspect.getsource(vrm)
        for mutation in vrm.build_mutations(self.base, vrm.DEFAULT_SEED):
            for code in mutation["expected_codes"]:
                self.assertIn(f'"{code}"', source,
                              f"{mutation['name']} expects unknown code {code}")

    def test_every_detector_fires_on_the_real_manifest(self):
        result = vrm.run_mutation_suite(self.base, vrm.DEFAULT_SEED, REAL_ON,
                                        self.bundle_root, None, rehash=True)
        self.assertGreaterEqual(result["total"], 30)
        self.assertEqual(result["gaps"], [], f"detector gaps: {result['gaps']}")

    def test_failing_samples_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = vrm.run_mutation_suite(self.base, vrm.DEFAULT_SEED, REAL_ON,
                                            self.bundle_root, tmp, rehash=True)
            self.assertEqual(len(os.listdir(tmp)), result["total"])

    def test_baseline_is_not_mutated(self):
        before = json.dumps(self.base, sort_keys=True)
        vrm.run_mutation_suite(self.base, vrm.DEFAULT_SEED, REAL_ON, self.bundle_root,
                               None, rehash=True)
        self.assertEqual(json.dumps(self.base, sort_keys=True), before)

    def test_symlink_probe_fires(self):
        probe = vrm.symlink_escape_probe(vrm.DEFAULT_SEED)
        self.assertTrue(probe["detector_fired"], probe)
        self.assertIn("qemu-path-symlink", probe["detected_codes"])

    def test_probe_does_not_touch_real_bundles(self):
        before = os.path.getmtime(REAL_ON)
        vrm.symlink_escape_probe(1)
        self.assertEqual(os.path.getmtime(REAL_ON), before)


class TestSafetyAndCli(unittest.TestCase):
    def test_only_reporting_command_is_used(self):
        source = inspect.getsource(vrm)
        self.assertIn('"--list-backends"', source)
        for forbidden in ('"--start"', "'--start'", '"--test"', '"--gui"'):
            self.assertNotIn(forbidden, source,
                             "the tool must never reference a VM-launching command")

    def test_cli_writes_both_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, os.path.join(HERE, "verify_release_manifest.py"),
                 "--skip-mutations", "--quiet", "--out-dir", tmp],
                capture_output=True, text=True, check=False)
            self.assertIn(result.returncode, (0, 1), result.stdout + result.stderr)
            self.assertTrue(os.path.isfile(os.path.join(tmp, "release-manifest-report.json")))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "release-manifest-report.md")))

    def test_cli_blocks_on_ambiguous_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            for index in range(2):
                target = os.path.join(tmp, f"b{index}")
                os.makedirs(target)
                with open(os.path.join(target, vrm.MANIFEST_NAME), "w", encoding="utf-8") as handle:
                    json.dump({"schema": vrm.SCHEMA, "contrib_backends": True}, handle)
            result = subprocess.run(
                [sys.executable, os.path.join(HERE, "verify_release_manifest.py"),
                 "--search-root", tmp, "--quiet", "--out-dir", tmp],
                capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            report = json.load(open(os.path.join(tmp, "release-manifest-report.json"),
                                    encoding="utf-8"))
            self.assertEqual(report["certification_status"], "blocked")
            self.assertTrue(report["blocking"]["candidates"])

    def test_markdown_renders_blocked_and_certified(self):
        blocked = {"generated_at": "now", "certification_status": "blocked", "method": "m",
                   "blocking": {"reason": "ambiguous", "problems": ["two ON"],
                                "candidates": ["/a", "/b"]}}
        text = vrm.render_markdown(blocked)
        self.assertIn("Blocked", text)
        self.assertIn("/a", text)


class TestAgainstRealManifests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (os.path.isfile(REAL_ON) and os.path.isfile(REAL_OFF)):
            raise unittest.SkipTest("real release manifests not present")

    def test_both_manifests_verify_cleanly(self):
        for path in (REAL_ON, REAL_OFF):
            document = load(path)
            report = vrm.verify_manifest(path, document,
                                         vrm.VerificationOptions(rehash=True, crosscheck=True))
            self.assertTrue(report.ok,
                            f"{path}: " + "; ".join(f"{f.code}: {f.message}"
                                                    for f in report.errors))
            self.assertEqual(report.stats["hashes_recomputed"], 11,
                             "main program plus ten QEMU entries")

    def test_flags_match_the_contrib_role(self):
        on = load(REAL_ON)
        off = load(REAL_OFF)
        self.assertTrue(on["contrib_backends"])
        self.assertFalse(off["contrib_backends"])
        for key in vrm.CONTRIB_BACKEND_KEYS:
            self.assertTrue(on["backends"][key], f"contrib ON should have {key}")
            self.assertFalse(off["backends"][key], f"contrib OFF should not have {key}")

    def test_honesty_flags_are_false_in_both(self):
        for path in (REAL_ON, REAL_OFF):
            document = load(path)
            self.assertIs(document["verified"], False, path)
            self.assertIs(document["real_hardware_verified"], False, path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
