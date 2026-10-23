#!/usr/bin/env python3
"""Self-tests for the VirtualWorld virtualization conformance checker.

Run them with either of:

    python3 tools/virtualization-conformance/test_run_conformance.py
    python3 -m unittest discover -s tools/virtualization-conformance -p 'test_*.py'

Every test is offline and uses only the standard library. Where a test needs a
*valid* document it starts from a real fixture and mutates exactly one thing, so a
failure points at the mutation rather than at the baseline.

The negative cases the task calls out are covered explicitly:

    bad JSON                     -> TestStructuralDefects.test_unparseable_json
    duplicate backend id         -> TestStructuralDefects.test_duplicate_backend_id
    wrong ordering               -> TestStructuralDefects.test_non_monotonic_order
    missing TCG                  -> TestSelectionInvariants.test_tcg_missing
    Harmony mislabelled native   -> TestHonestyInvariants.test_compat_backend_claims_native
    empty fallback reason        -> TestSelectionInvariants.test_empty_reason
    verified wrong type          -> TestMatrixInvariants.test_verified_not_bool
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import run_conformance as rc  # noqa: E402  (path juggling above is deliberate)

FIXTURE_DIR = os.path.join(HERE, "fixtures")
BASELINE_FIXTURE = os.path.join(FIXTURE_DIR, "windows-x86_64.json")

# Candidate locations of a built binary, most specific first. A live check is
# skipped (never failed) when none of them exist, so the suite is reliable on a
# machine that has not built the project.
BINARY_CANDIDATES = (
    os.environ.get("VW_CONFORMANCE_BINARY", ""),
    os.path.join(HERE, "..", "..", "build", "VirtualWorld.app", "Contents", "MacOS", "VirtualWorld"),
    os.path.join(HERE, "..", "..", "build-release", "VirtualWorld.app", "Contents", "MacOS", "VirtualWorld"),
    os.path.join(HERE, "..", "..", "build", "VirtualWorld"),
)


def baseline_document():
    with open(BASELINE_FIXTURE, "r", encoding="utf-8") as handle:
        return json.load(handle)


def check(document, **kwargs):
    return rc.check_document(document, source="test", **kwargs)


def codes(report):
    return report.codes()


class TestStructuralDefects(unittest.TestCase):
    def test_clean_baseline_passes(self):
        report = check(baseline_document())
        self.assertTrue(report.ok, f"baseline fixture must be conformant, got {codes(report)}")

    def test_unparseable_json(self):
        document, error = rc.parse_single_json_document("{not json")
        self.assertIsNone(document)
        self.assertIsNotNone(error)
        self.assertIn("not a single valid JSON document", error)

    def test_trailing_garbage_is_rejected(self):
        # A report that prints two documents is not machine-readable.
        _, error = rc.parse_single_json_document('{"a": 1}\n{"b": 2}')
        self.assertIsNotNone(error)

    def test_empty_stdout_is_rejected(self):
        _, error = rc.parse_single_json_document("   \n")
        self.assertIsNotNone(error)

    def test_duplicate_backend_id(self):
        doc = baseline_document()
        doc["backends"][1]["id"] = doc["backends"][0]["id"]
        report = check(doc)
        self.assertFalse(report.ok)
        self.assertIn("backend-id-duplicate", codes(report))

    def test_empty_backend_id(self):
        doc = baseline_document()
        doc["backends"][0]["id"] = "   "
        report = check(doc)
        self.assertIn("backend-id-empty", codes(report))

    def test_non_monotonic_order(self):
        doc = baseline_document()
        # Force an emulation-tier candidate ahead of a native-tier one.
        considered = doc["selection"]["considered"]
        considered.sort(key=lambda c: (0 if c["tier"] == "emulation" else 1))
        report = check(doc)
        self.assertIn("considered-order-nonmonotonic", codes(report))

    def test_missing_required_document_field(self):
        for field in rc.DOCUMENT_REQUIRED_FIELDS:
            doc = baseline_document()
            doc.pop(field)
            report = check(doc)
            self.assertFalse(report.ok, f"removing {field} must be a finding")
            self.assertIn("missing-field", codes(report))

    def test_schema_prefix_and_version(self):
        doc = baseline_document()
        doc["schema"] = "something-else/2"
        self.assertIn("schema-unknown", codes(check(doc)))

        doc = baseline_document()
        doc["schema"] = "virtualworld.virtualization/notanumber"
        self.assertIn("schema-version-invalid", codes(check(doc)))

        doc = baseline_document()
        doc["schema"] = "virtualworld.virtualization/0"
        self.assertIn("schema-version-invalid", codes(check(doc)))

    def test_document_not_object(self):
        report = check(["not", "an", "object"])
        self.assertIn("document-not-object", codes(report))

    def test_host_platform_unknown(self):
        doc = baseline_document()
        doc["host"]["platform"] = "plan9"
        self.assertIn("host-platform-unknown", codes(check(doc)))

    def test_guest_arch_mismatch(self):
        doc = baseline_document()
        report = check(doc, requested_arch="riscv64")
        self.assertIn("guest-arch-mismatch", codes(report))

    def test_build_flag_not_bool(self):
        doc = baseline_document()
        doc["build"]["harmony_backend"] = 1  # int, not a real boolean
        self.assertIn("build-value-not-bool", codes(check(doc)))

    def test_qemu_fallback_must_be_built(self):
        doc = baseline_document()
        doc["build"]["qemu_fallback"] = False
        self.assertIn("qemu-fallback-not-built", codes(check(doc)))

    def test_status_class_optional_but_validated_when_present(self):
        # Absent: must not be a finding (older builds predate the field).
        doc = baseline_document()
        for entry in doc["backends"]:
            entry.pop("status_class", None)
        self.assertTrue(check(doc).ok, "status_class must stay optional")

        # Present and well-formed: fine.
        doc = baseline_document()
        for entry in doc["backends"]:
            entry["status_class"] = "available"
        self.assertTrue(check(doc).ok)

        # Present but empty or non-string: a finding.
        for bad in ("", "   ", 0, None, []):
            doc = baseline_document()
            doc["backends"][0]["status_class"] = bad
            self.assertIn("status-class-invalid", codes(check(doc)),
                          f"status_class={bad!r} must be rejected")

    def test_backend_boolean_not_bool(self):
        doc = baseline_document()
        doc["backends"][0]["host_supported"] = "true"
        self.assertIn("wrong-field-type", codes(check(doc)))


class TestSelectionInvariants(unittest.TestCase):
    def test_tcg_missing(self):
        doc = baseline_document()
        doc["backends"] = [b for b in doc["backends"] if b["id"] != rc.TCG_BACKEND_ID]
        doc["selection"]["considered"] = [
            c for c in doc["selection"]["considered"] if c["id"] != rc.TCG_BACKEND_ID
        ]
        report = check(doc)
        self.assertFalse(report.ok)
        self.assertIn("tcg-missing", codes(report))

    def test_tcg_not_last(self):
        doc = baseline_document()
        doc["selection"]["considered"].append({
            "id": "zzz-late-backend",
            "tier": "emulation",
            "host_supported": False,
            "guest_arch_supported": False,
            "config_accepted": False,
            "selected": False,
            "status": "host-unsupported",
            "reason": "not supported",
        })
        # Register it too, so the only complaint is the ordering.
        doc["backends"].append(rc_backend_stub("zzz-late-backend"))
        report = check(doc)
        self.assertIn("tcg-not-last", codes(report))

    def test_tcg_arch_unsupported(self):
        doc = baseline_document()
        for entry in doc["selection"]["considered"]:
            if entry["id"] == rc.TCG_BACKEND_ID:
                entry["guest_arch_supported"] = False
        report = check(doc)
        self.assertIn("tcg-arch-unsupported", codes(report))

    def test_empty_reason(self):
        doc = baseline_document()
        for entry in doc["selection"]["considered"]:
            if not entry["selected"]:
                entry["reason"] = ""
                break
        report = check(doc)
        self.assertFalse(report.ok)
        self.assertIn("empty-field", codes(report))

    def test_fallback_from_required_when_fell_back(self):
        doc = baseline_document()
        doc["selection"]["fell_back"] = True
        doc["selection"]["fallback_from"] = ""
        self.assertIn("fallback-from-missing", codes(check(doc)))

    def test_selection_backend_mismatch(self):
        doc = baseline_document()
        doc["selection"]["backend"] = "qemu-tcg"
        report = check(doc)
        self.assertIn("selected-mismatch", codes(report))

    def test_no_candidate_selected(self):
        doc = baseline_document()
        for entry in doc["selection"]["considered"]:
            entry["selected"] = False
        report = check(doc)
        self.assertIn("considered-none-selected", codes(report))

    def test_multiple_candidates_selected(self):
        doc = baseline_document()
        doc["selection"]["considered"][0]["selected"] = True
        doc["selection"]["considered"][1]["selected"] = True
        report = check(doc)
        self.assertIn("considered-multiple-selected", codes(report))

    def test_unsupported_backend_must_not_be_selected(self):
        doc = baseline_document()
        for entry in doc["selection"]["considered"]:
            if entry["selected"]:
                entry["host_supported"] = False
        report = check(doc)
        self.assertIn("selected-backend-unsupported", codes(report))

    def test_considered_must_cover_every_backend(self):
        doc = baseline_document()
        dropped = doc["selection"]["considered"].pop()
        report = check(doc)
        self.assertIn("considered-missing-backend", codes(report))
        self.assertIn(dropped["id"], codes(report) and [dropped["id"]] or [dropped["id"]])

    def test_considered_must_not_invent_backends(self):
        doc = baseline_document()
        doc["backends"] = [b for b in doc["backends"] if b["id"] != "harmony-virtualization"]
        report = check(doc)
        self.assertIn("considered-unknown-backend", codes(report))

    def test_auto_preference_must_pick_best_available(self):
        doc = baseline_document()
        doc["preference"] = "auto"
        # windows-hypervisor is native and eligible; demote it in the outcome.
        for entry in doc["selection"]["considered"]:
            entry["selected"] = entry["id"] == rc.TCG_BACKEND_ID
        doc["selection"]["backend"] = rc.TCG_BACKEND_ID
        doc["selection"]["tier"] = "emulation"
        report = check(doc)
        self.assertIn("selection-not-best-available", codes(report))

    def test_cross_arch_must_select_tcg(self):
        doc = baseline_document()
        doc["preference"] = "auto"
        doc["host"]["arch"] = "aarch64"
        doc["guest_arch"] = "x86_64"
        for entry in doc["selection"]["considered"]:
            entry["selected"] = entry["id"] == "qemu-hardware"
        doc["selection"]["backend"] = "qemu-hardware"
        doc["selection"]["tier"] = "hardware"
        report = check(doc)
        self.assertIn("cross-arch-not-tcg", codes(report))

    def test_policy_restricted_run_is_not_penalised(self):
        # preference != auto legitimately excludes native backends.
        doc = baseline_document()
        doc["preference"] = "qemu"
        report = check(doc)
        self.assertTrue(report.ok, f"policy-restricted run must pass, got {codes(report)}")

    def test_right_ordering_passes(self):
        doc = baseline_document()
        ranks = [rc.TIER_RANK[c["tier"]] for c in doc["selection"]["considered"]]
        self.assertEqual(ranks, sorted(ranks), "baseline must already be ordered")
        self.assertTrue(check(doc).ok)


class TestHonestyInvariants(unittest.TestCase):
    def test_compat_backend_claims_native(self):
        doc = baseline_document()
        for where in (doc["backends"], doc["selection"]["considered"]):
            for entry in where:
                if entry["id"] == "harmony-virtualization":
                    entry["tier"] = "native"
        report = check(doc)
        self.assertFalse(report.ok)
        self.assertIn("compat-backend-claims-native", codes(report))

    def test_compat_backend_claims_native_execution(self):
        doc = baseline_document()
        for entry in doc["backends"]:
            if entry["id"] == "linux-kvm":
                entry["native_execution"] = True
        report = check(doc)
        self.assertIn("compat-backend-claims-native-execution", codes(report))

    def test_platform_native_backend_must_be_native_tier(self):
        # This is the defect currently observed on the real build.
        doc = baseline_document()
        for where in (doc["backends"], doc["selection"]["considered"]):
            for entry in where:
                if entry["id"] == "windows-hypervisor":
                    entry["tier"] = "emulation"
        report = check(doc)
        self.assertIn("platform-native-tier-not-native", codes(report))

    def test_native_execution_tier_mismatch(self):
        doc = baseline_document()
        for entry in doc["backends"]:
            if entry["id"] == "qemu-tcg":
                entry["native_execution"] = True
        report = check(doc)
        self.assertIn("native-execution-tier-mismatch", codes(report))

    def test_unsupported_backend_must_explain_itself(self):
        doc = baseline_document()
        for entry in doc["backends"]:
            if entry["id"] == "macos-virtualization":
                entry["host_supported"] = False
                entry["issues"] = [{
                    "level": "info", "code": "nothing", "message": "no explanation"
                }]
                entry["summary"] = ""
        report = check(doc)
        self.assertIn("unsupported-backend-unexplained", codes(report))

    def test_harmony_conformant_fixture_is_honest(self):
        path = os.path.join(FIXTURE_DIR, "harmonyos-aarch64.json")
        with open(path, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
        report = check(doc)
        self.assertTrue(report.ok, f"harmony fixture must be conformant, got {codes(report)}")
        harmony = next(b for b in doc["backends"] if b["id"] == "harmony-virtualization")
        self.assertNotEqual(harmony["tier"], "native")
        self.assertFalse(harmony["native_execution"])


class TestMatrixInvariants(unittest.TestCase):
    def test_verified_not_bool(self):
        for bad in (0, 1, "false", "true", None, []):
            doc = baseline_document()
            doc["support_matrix"][0]["verified"] = bad
            report = check(doc)
            self.assertIn(
                "matrix-verified-not-bool",
                codes(report),
                f"verified={bad!r} must be rejected as a non-boolean",
            )

    def test_matrix_missing_field(self):
        for field in rc.REQUIRED_MATRIX_FIELDS:
            doc = baseline_document()
            doc["support_matrix"][0].pop(field)
            report = check(doc)
            self.assertIn("missing-field", codes(report), f"missing {field} must be reported")

    def test_matrix_cross_arch_acceleration_rejected(self):
        doc = baseline_document()
        doc["support_matrix"].append({
            "host_platform": "macos",
            "host_arch": "aarch64",
            "guest_arch": "x86_64",
            "backend": "macos-virtualization",
            "tier": "native",
            "status": "native",
            "verified": False,
            "note": "dishonest row",
        })
        report = check(doc)
        self.assertIn("matrix-cross-arch-acceleration", codes(report))

    def test_matrix_compat_row_cannot_be_native(self):
        # The Windows baseline has no HarmonyOS rows, so start from the Harmony
        # fixture where such a row actually exists.
        path = os.path.join(FIXTURE_DIR, "harmonyos-aarch64.json")
        with open(path, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
        candidates = [r for r in doc["support_matrix"] if r["backend"] == "harmony-virtualization"]
        self.assertTrue(candidates, "harmony fixture must document a harmony row")
        candidates[0]["tier"] = "native"
        report = check(doc)
        self.assertIn("matrix-compat-row-claims-native", codes(report))

    def test_matrix_empty(self):
        doc = baseline_document()
        doc["support_matrix"] = []
        self.assertIn("matrix-empty", codes(check(doc)))

    def test_matrix_tier_invalid(self):
        doc = baseline_document()
        doc["support_matrix"][0]["tier"] = "turbo"
        self.assertIn("matrix-tier-invalid", codes(check(doc)))


class TestFixturesHarness(unittest.TestCase):
    def test_fixture_manifest_is_wellformed(self):
        manifest, error = rc.load_fixture_manifest(FIXTURE_DIR)
        self.assertIsNone(error)
        self.assertIsNotNone(manifest)
        self.assertTrue(manifest["fixtures"])
        for entry in manifest["fixtures"]:
            self.assertIn("synthetic", entry, f"{entry.get('file')} must declare synthetic")
            self.assertIs(type(entry["synthetic"]), bool)

    def test_synthetic_fixture_claiming_verified_is_rejected(self):
        doc = baseline_document()
        doc["support_matrix"][0]["verified"] = True
        report = rc.Report("test")
        rc._check_synthetic_honesty(report, doc, "synthetic.json")
        self.assertIn("synthetic-fixture-claims-verified", [f.code for f in report.findings])

    def test_negative_fixtures_are_detected(self):
        reports, error = rc.check_fixtures(FIXTURE_DIR)
        self.assertIsNone(error)
        # Key by fixture file, not by human label: labels are documentation and
        # have been reworded before, the file is the stable identifier.
        by_file = {}
        for report in reports:
            command = report.command[0] if report.command else ""
            by_file[command.split("fixture:", 1)[-1]] = report

        windows = by_file["negative/windows-native-tier-regression.json"]
        self.assertFalse(windows.ok, "the historical Windows regression must still be detected")
        self.assertIn("platform-native-tier-not-native", windows.codes())
        self.assertIn("tcg-not-last", windows.codes())

        harmony = by_file["negative/harmony-claims-native.json"]
        self.assertFalse(harmony.ok)
        self.assertIn("compat-backend-claims-native", harmony.codes())

        broken = by_file["negative/broken-json.json"]
        self.assertFalse(broken.ok)
        self.assertIn("fixture-unparseable", broken.codes())

    def test_positive_fixtures_all_pass_on_the_current_contract(self):
        reports, error = rc.check_fixtures(FIXTURE_DIR)
        self.assertIsNone(error)
        manifest, _ = rc.load_fixture_manifest(FIXTURE_DIR)
        expect_fail = {e["file"] for e in manifest["fixtures"] if e.get("expect_fail")}
        for report in reports:
            file_name = (report.command[0].split("fixture:", 1)[-1]
                         if report.command else "")
            if file_name in expect_fail:
                continue
            self.assertTrue(report.ok,
                            f"positive fixture {file_name} regressed: {report.codes()}")

    def test_negative_fixture_that_stops_failing_is_reported(self):
        # If a fixture marked expect_fail starts passing, the harness must complain
        # rather than silently lose its detector coverage.
        report = rc.Report("test")
        report.ok  # noqa: B018 - documented no-op, kept for readability
        entry = {
            "file": "windows-x86_64.json",
            "label": "positive fixture wrongly marked expect_fail",
            "synthetic": True,
            "expect_fail": True,
            "expected_codes": [],
        }
        result = rc._check_one_fixture(FIXTURE_DIR, entry, ())
        self.assertIn("negative-fixture-unexpectedly-passed", result.codes())

    def test_allow_code_downgrades_but_does_not_hide(self):
        # Reproduce the real defect faithfully: the mis-tiered Windows backend also
        # stops claiming native execution, and the ordering consequences follow.
        doc = baseline_document()
        for entry in doc["backends"]:
            if entry["id"] == "windows-hypervisor":
                entry["tier"] = "emulation"
                entry["native_execution"] = False
                entry["hardware_acceleration"] = False
        for entry in doc["selection"]["considered"]:
            if entry["id"] == "windows-hypervisor":
                entry["tier"] = "emulation"
        doc["selection"]["considered"].sort(key=lambda c: (rc.TIER_RANK[c["tier"]], c["id"]))
        # The mis-tiering also changes which backend wins on a Windows host.
        for entry in doc["selection"]["considered"]:
            entry["selected"] = entry["id"] == "qemu-hardware"
        doc["selection"]["backend"] = "qemu-hardware"
        doc["selection"]["tier"] = "hardware"

        strict = check(doc)
        self.assertFalse(strict.ok)
        self.assertIn("platform-native-tier-not-native", strict.codes())
        self.assertIn("tcg-not-last", strict.codes())

        relaxed = check(doc, allow_codes=["platform-native-tier-not-native", "tcg-not-last"])
        self.assertTrue(relaxed.ok, f"allow-codes should clear the run, got {relaxed.codes()}")
        # The findings are still visible, just no longer fatal.
        self.assertIn("platform-native-tier-not-native", relaxed.codes())
        self.assertTrue(all(f.severity == rc.Severity.INFO
                            for f in relaxed.findings
                            if f.code == "platform-native-tier-not-native"))


class TestReportAndCli(unittest.TestCase):
    def test_report_document_shape(self):
        report = check(baseline_document())
        report.exit_code = 0
        document = rc.build_report_document(
            [report], binary="/tmp/fake", binary_sha256="abc", fixture_dir=None, started_at="now"
        )
        self.assertEqual(document["tool"], rc.TOOL_NAME)
        self.assertTrue(document["summary"]["ok"])
        self.assertEqual(document["summary"]["targets"], 1)
        self.assertEqual(len(document["targets"]), 1)
        self.assertIn("findings", document["targets"][0])

    def test_cli_fixture_mode_exit_codes(self):
        result = subprocess.run(
            [sys.executable, os.path.join(HERE, "run_conformance.py"),
             "--fixtures", FIXTURE_DIR, "--quiet"],
            capture_output=True, text=True, check=False,
        )
        # Strict mode must be non-zero while the tracked Windows defect exists.
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

        relaxed = subprocess.run(
            [sys.executable, os.path.join(HERE, "run_conformance.py"),
             "--fixtures", FIXTURE_DIR, "--quiet",
             "--allow-code", "platform-native-tier-not-native",
             "--allow-code", "tcg-not-last"],
            capture_output=True, text=True, check=False,
        )
        # ... and still non-zero, because the negative fixtures must keep failing.
        self.assertEqual(relaxed.returncode, 1, relaxed.stdout + relaxed.stderr)

    def test_cli_writes_machine_readable_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "report.json")
            result = subprocess.run(
                [sys.executable, os.path.join(HERE, "run_conformance.py"),
                 "--fixtures", FIXTURE_DIR, "--quiet", "--output", out],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 1)
            with open(out, "r", encoding="utf-8") as handle:
                document = json.load(handle)
            self.assertEqual(document["tool"], rc.TOOL_NAME)
            self.assertFalse(document["summary"]["ok"])
            self.assertGreater(document["summary"]["errors"], 0)

    def test_cli_requires_a_target(self):
        result = subprocess.run(
            [sys.executable, os.path.join(HERE, "run_conformance.py")],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 2)

    def test_cli_missing_binary_is_usage_error(self):
        result = subprocess.run(
            [sys.executable, os.path.join(HERE, "run_conformance.py"),
             "--binary", "/nonexistent/VirtualWorld", "--quiet"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 2)

    def test_refuses_to_run_a_non_reporting_command(self):
        with self.assertRaises(ValueError):
            rc.run_binary("/bin/echo", "--start", None, 5.0)

    def test_exit_code_rules(self):
        report = rc.Report("t")
        rc._check_exit_code(report, "--list-backends", 0)
        self.assertTrue(report.ok)
        rc._check_exit_code(report, "--list-backends", 1)
        self.assertFalse(report.ok)

        report = rc.Report("t")
        rc._check_exit_code(report, "--check", 1)
        self.assertTrue(report.ok, "--check exit 1 is documented (runtime missing)")
        rc._check_exit_code(report, "--check", 3)
        self.assertFalse(report.ok)

    def test_env_overrides(self):
        overrides, error = rc.parse_env_overrides(["VIRTUALWORLD_BACKEND=auto"])
        self.assertIsNone(error)
        self.assertEqual(overrides, {"VIRTUALWORLD_BACKEND": "auto"})
        _, error = rc.parse_env_overrides(["BROKEN"])
        self.assertIsNotNone(error)


class TestAgainstRealBinary(unittest.TestCase):
    """Runs only when a built binary is present; otherwise skipped."""

    @classmethod
    def setUpClass(cls):
        cls.binary = next(
            (os.path.abspath(p) for p in BINARY_CANDIDATES
             if p and os.path.isfile(p) and os.access(p, os.X_OK)),
            None,
        )
        if cls.binary is None:
            raise unittest.SkipTest(
                "no built VirtualWorld binary found; set VW_CONFORMANCE_BINARY to run this"
            )

    def test_live_targets_parse_and_report(self):
        report = rc.check_live_target(self.binary, "--list-backends", "aarch64", 60.0)
        self.assertIsNotNone(report.document, "live run must produce a parsed document")
        self.assertEqual(report.exit_code, 0)
        self.assertIn("backends", report.document)
        self.assertIn("selection", report.document)
        self.assertIn("support_matrix", report.document)

    def test_json_is_a_single_document_on_stdout(self):
        exit_code, stdout, _stderr, error = rc.run_binary(
            self.binary, "--list-backends", "aarch64", 60.0
        )
        self.assertIsNone(error)
        self.assertEqual(exit_code, 0)
        document, parse_error = rc.parse_single_json_document(stdout)
        self.assertIsNone(parse_error)
        self.assertEqual(document["schema"], rc.SCHEMA_PREFIX + "/2")

    def test_no_findings_outside_the_tracked_windows_defect(self):
        # The tool must not produce unrelated noise on a real host; if the Windows
        # backend is fixed this becomes fully clean and the allow-list is unused.
        report = rc.check_live_target(self.binary, "--list-backends", "aarch64", 60.0)
        unexpected = [
            f.code for f in report.errors
            if f.code not in ("platform-native-tier-not-native", "tcg-not-last")
        ]
        self.assertEqual(unexpected, [], f"unexpected findings on the real build: {unexpected}")


class TestFinalReportGenerator(unittest.TestCase):
    """Covers make_final_report.py's pure assembly logic (no product needed)."""

    @classmethod
    def setUpClass(cls):
        try:
            import make_final_report as mfr  # noqa: F401
        except Exception as exc:  # pragma: no cover
            raise unittest.SkipTest(f"report generator not importable: {exc}")
        cls.mfr = mfr

    def _target(self, code, severity="error", message="m", path="$.backends[5].tier"):
        return {
            "source": "VirtualWorld --list-backends aarch64",
            "command": ["binary", "--list-backends", "aarch64", "--json"],
            "exit_code": 0,
            "ok": severity != "error",
            "error_count": 1 if severity == "error" else 0,
            "warning_count": 0,
            "findings": [{"severity": severity, "code": code, "message": message, "path": path}],
            "stderr_excerpt": "",
        }

    def test_defect_grouping_attributes_the_windows_backend(self):
        targets = [
            self._target("platform-native-tier-not-native",
                         message="'windows-hypervisor' ... must report tier 'native', got 'emulation'"),
            self._target("tcg-not-last",
                         message="'qemu-tcg' must be the last candidate but ['windows-hypervisor'] follow it"),
        ]
        defects = self.mfr.build_defects(targets)
        codes = {d["code"] for d in defects}
        self.assertEqual(codes, {"platform-native-tier-not-native", "tcg-not-last"})
        for defect in defects:
            self.assertIn("windows-hypervisor", defect["backend_ids"])
            self.assertEqual(defect["owner"], "Windows/Linux platform workstream")
            self.assertEqual(defect["owner_source"], "src/WindowsHypervisorEngine.hpp/.cpp")
            self.assertEqual(defect["targets"], ["VirtualWorld --list-backends aarch64"])

    def test_defect_grouping_ignores_warnings(self):
        defects = self.mfr.build_defects([self._target("some-warning", severity="warning")])
        self.assertEqual(defects, [])

    def test_invariant_status_reflects_failures(self):
        passing = self.mfr.invariant_statuses([])
        self.assertTrue(all(i["status"] == "pass" for i in passing))

        failing = self.mfr.invariant_statuses([self._target("tcg-not-last")])
        by_id = {i["id"]: i["status"] for i in failing}
        self.assertEqual(by_id["tcg-last"], "fail")
        self.assertEqual(by_id["schema"], "pass")

    def test_unmapped_code_is_surfaced_not_ignored(self):
        results = self.mfr.invariant_statuses([self._target("brand-new-unknown-code")])
        by_id = {i["id"]: i["status"] for i in results}
        self.assertEqual(by_id.get("unclassified"), "fail",
                         "an unmapped finding must never be silently treated as passing")

    def test_every_invariant_code_maps_to_a_declared_invariant(self):
        declared = {key for key, _ in self.mfr.INVARIANTS}
        for code, invariant in self.mfr.CODE_TO_INVARIANT.items():
            self.assertIn(invariant, declared, f"{code} maps to undeclared invariant {invariant}")

    def test_binary_metadata_shape(self):
        meta = self.mfr.binary_metadata(sys.executable)
        self.assertEqual(len(meta["sha256"]), 64)
        self.assertGreater(meta["size_bytes"], 0)
        self.assertIn("mtime", meta)

    def test_markdown_handles_a_blocked_report(self):
        blocked = {
            "generated_at": "now",
            "certification_status": "blocked",
            "blocking": {"reason": "no binary", "requested_binary": "/x", "requested_exists": False,
                         "hint": "wait for build-final"},
            "self_tests": {"command": "c", "result": "OK", "tests_run": 1, "exit_code": 0},
        }
        text = self.mfr.render_markdown(blocked)
        self.assertIn("Blocked", text)
        self.assertIn("no binary", text)

    def test_markdown_reports_pass_and_fail_invariants(self):
        report = {
            "generated_at": "now",
            "certification_status": "non-conformant",
            "product": {"path": "/p", "build_flags": {}, "backend_ids": [], "selection_order": []},
            "self_tests": {"command": "c", "result": "OK", "tests_run": 1, "exit_code": 0},
            "live": {"targets": [self._target("tcg-not-last")],
                     "summary": {"targets": 1, "errors": 1, "warnings": 0}},
            "invariants": [{"id": "tcg-last", "description": "d", "status": "fail"}],
            "defects": [{"code": "tcg-not-last", "owner": "o", "invariant": "tcg-last",
                         "backend_ids": ["windows-hypervisor"], "targets": ["t"], "messages": ["m"]}],
            "fixtures": {"counts": {}, "rows": []},
            "legacy_compatibility": {"selections": [], "check_agreement": []},
            "support_matrix": [],
            "not_verified": [],
        }
        text = self.mfr.render_markdown(report)
        self.assertIn("**FAIL**", text)
        self.assertIn("Defects", text)
        self.assertIn("windows-hypervisor", text)


def rc_backend_stub(backend_id: str) -> dict:
    return {
        "id": backend_id,
        "name": backend_id,
        "tier": "emulation",
        "host_supported": False,
        "guest_arch_supported": False,
        "hardware_acceleration": False,
        "native_execution": False,
        "accelerator": "",
        "status": "host-unsupported",
        "summary": "stub",
        "issues": [{"level": "error", "code": "host-unsupported", "message": "stub"}],
        "requirements": [],
        "runs_in_process": False,
    }


if __name__ == "__main__":
    unittest.main(verbosity=2)
