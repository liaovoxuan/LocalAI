#!/usr/bin/env python3
"""Self-tests for the VirtualWorld stability / concurrency / mutation gate.

Run with either of:

    python3 tools/virtualization-conformance/test_run_stability_gate.py
    python3 -m unittest discover -s tools/virtualization-conformance -p 'test_*.py'

Everything here is offline and standard-library only. The tests that need a
*certified* artifact are skipped unless the pinned binary is present and hashes to
the pinned digest, so the suite is safe anywhere.
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
import run_stability_gate as gate  # noqa: E402

FIXTURE_DIR = os.path.join(HERE, "fixtures")
BASELINE_FIXTURE = os.path.join(FIXTURE_DIR, "macos-apple-silicon-aarch64.json")


def baseline_document():
    with open(BASELINE_FIXTURE, "r", encoding="utf-8") as handle:
        return json.load(handle)


def make_invocation(command, arch, stdout, returncode=0, stderr="", phase="sequential",
                    round_index=1, timed_out=False):
    inv = gate.Invocation(command, arch, phase, round_index)
    inv.returncode = returncode
    inv.stdout = stdout
    inv.stderr = stderr
    inv.timed_out = timed_out
    inv.duration_ms = 12.5
    if not timed_out:
        inv.document, inv.parse_error = rc.parse_single_json_document(stdout)
    return inv


class TestArtifactPinning(unittest.TestCase):
    """The gate must never run against an unverified artifact."""

    def test_matching_sha_is_accepted(self):
        with tempfile.NamedTemporaryFile("wb", delete=False) as handle:
            handle.write(b"certified payload")
            path = handle.name
        os.chmod(path, 0o755)
        digest = hashlib.sha256(b"certified payload").hexdigest()
        try:
            result = gate.verify_artifact(path, digest)
            self.assertTrue(result["ok"])
            self.assertEqual(result["actual_sha256"], digest)
        finally:
            os.unlink(path)

    def test_mismatched_sha_is_rejected(self):
        with tempfile.NamedTemporaryFile("wb", delete=False) as handle:
            handle.write(b"something else")
            path = handle.name
        os.chmod(path, 0o755)  # executable, so the digest check is what rejects it
        try:
            result = gate.verify_artifact(path, "0" * 64)
            self.assertFalse(result["ok"])
            self.assertIn("refusing to run against a different artifact", result["reason"])
        finally:
            os.unlink(path)

    def test_missing_binary_is_rejected(self):
        result = gate.verify_artifact("/nonexistent/VirtualWorld", "0" * 64)
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["reason"])

    def test_non_executable_binary_is_rejected(self):
        with tempfile.NamedTemporaryFile("wb", delete=False) as handle:
            handle.write(b"x")
            path = handle.name
        os.chmod(path, 0o644)
        try:
            result = gate.verify_artifact(path, hashlib.sha256(b"x").hexdigest())
            self.assertFalse(result["ok"])
            self.assertIn("not executable", result["reason"])
        finally:
            os.unlink(path)

    def test_sha_mismatch_produces_a_blocked_report_and_never_runs(self):
        with tempfile.NamedTemporaryFile("wb", delete=False) as handle:
            handle.write(b"wrong build")
            path = handle.name
        os.chmod(path, 0o755)
        try:
            args = gate.build_parser().parse_args(
                ["--binary", path, "--expected-sha256", "f" * 64, "--rounds", "1",
                 "--arch", "aarch64", "--quiet", "--out-dir", tempfile.mkdtemp()])
            report = gate.run_gate(args)
            self.assertEqual(report["gate_status"], "blocked")
            self.assertIn("blocking", report)
            self.assertIn("does not rebuild", report["blocking"]["policy"])
            # No invocation may have happened.
            self.assertEqual(report["stability"]["variants"], [])
        finally:
            os.unlink(path)

    def test_only_reporting_commands_are_ever_issued(self):
        self.assertEqual(set(gate.COMMANDS), {"--list-backends", "--check"})
        for forbidden in ("--start", "--test", "--create-qcow2", "--gui"):
            self.assertNotIn(forbidden, gate.COMMANDS)
        with self.assertRaises(ValueError):
            rc.run_binary("/bin/echo", "--start", None, 5.0)


class TestComparisonPrimitives(unittest.TestCase):
    def test_canonical_json_ignores_key_order(self):
        a = {"x": 1, "y": [1, 2]}
        b = {"y": [1, 2], "x": 1}
        self.assertEqual(gate.canonical_json(a), gate.canonical_json(b))

    def test_canonical_json_detects_value_changes(self):
        self.assertNotEqual(gate.canonical_json({"x": 1}), gate.canonical_json({"x": 2}))
        self.assertNotEqual(gate.canonical_json({"x": [1]}), gate.canonical_json({"x": [1, 1]}))

    def test_normalization_is_empty_by_default(self):
        # If this ever becomes non-empty it must be a deliberate, documented
        # decision -- an empty list is the strongest determinism claim.
        self.assertEqual(gate.NORMALIZED_FIELDS, ())

    def test_project_semantics_extracts_every_drift_field(self):
        projection = gate.project_semantics(baseline_document())
        for field in gate.DRIFT_FIELDS:
            self.assertIn(field, projection, f"drift field {field} must be projected")
        self.assertEqual(projection["selection_order"][-1], rc.TCG_BACKEND_ID)

    def test_support_matrix_digest_is_order_sensitive_but_key_order_insensitive(self):
        doc = baseline_document()
        first = gate.project_semantics(doc)["support_matrix_digest"]
        reordered_keys = copy.deepcopy(doc)
        reordered_keys["support_matrix"] = [
            {k: row[k] for k in reversed(list(row.keys()))} for row in doc["support_matrix"]
        ]
        self.assertEqual(gate.project_semantics(reordered_keys)["support_matrix_digest"], first)
        changed = copy.deepcopy(doc)
        changed["support_matrix"][0]["note"] = "changed"
        self.assertNotEqual(gate.project_semantics(changed)["support_matrix_digest"], first)


class TestVariantAnalysis(unittest.TestCase):
    def _stable_runs(self, count=5):
        stdout = json.dumps(baseline_document(), ensure_ascii=False)
        return [make_invocation("--list-backends", "aarch64", stdout, round_index=i + 1)
                for i in range(count)]

    def test_stable_runs_pass(self):
        analysis = gate.analyze_variant("--list-backends aarch64", self._stable_runs(), (0,))
        self.assertTrue(analysis["ok"], analysis["findings"])
        self.assertTrue(analysis["byte_identical"])
        self.assertTrue(analysis["canonical_json_identical"])
        self.assertEqual(analysis["drift_fields"], [])

    def test_unexpected_exit_code(self):
        runs = self._stable_runs()
        runs[0].returncode = 3
        analysis = gate.analyze_variant("v", runs, (0,))
        self.assertIn("unexpected-exit-code", [f["code"] for f in analysis["findings"]])

    def test_check_may_exit_one(self):
        runs = self._stable_runs()
        for run in runs:
            run.returncode = 1
        analysis = gate.analyze_variant("--check aarch64", runs, (0, 1))
        self.assertTrue(analysis["ok"], analysis["findings"])

    def test_stderr_leak_is_detected(self):
        runs = self._stable_runs()
        runs[2].stderr = "warning: something\n"
        analysis = gate.analyze_variant("v", runs, (0,))
        self.assertIn("stderr-not-empty", [f["code"] for f in analysis["findings"]])
        self.assertEqual(analysis["stderr_nonempty"], 1)

    def test_non_single_json_is_detected(self):
        runs = self._stable_runs()
        runs[0] = make_invocation("--list-backends", "aarch64", '{"a":1}\n{"b":2}')
        analysis = gate.analyze_variant("v", runs, (0,))
        self.assertIn("stdout-not-single-json", [f["code"] for f in analysis["findings"]])

    def test_value_change_is_detected(self):
        runs = self._stable_runs()
        mutated = baseline_document()
        mutated["selection"]["backend"] = "qemu-tcg"
        runs[1] = make_invocation("--list-backends", "aarch64", json.dumps(mutated))
        analysis = gate.analyze_variant("v", runs, (0,))
        codes = [f["code"] for f in analysis["findings"]]
        self.assertIn("stdout-json-differs", codes)
        self.assertIn("stdout-bytes-differ", codes)
        self.assertIn("semantic-drift", codes)

    def test_key_reordering_counts_as_instability_not_value_change(self):
        runs = self._stable_runs()
        # The product emits keys already sorted, so build an explicitly reordered
        # document: same values, different byte sequence.
        doc = baseline_document()
        reordered = json.dumps({k: doc[k] for k in reversed(list(doc.keys()))},
                               ensure_ascii=False)
        self.assertNotEqual(reordered, runs[0].stdout)
        runs[1] = make_invocation("--list-backends", "aarch64", reordered)
        analysis = gate.analyze_variant("v", runs, (0,))
        codes = [f["code"] for f in analysis["findings"]]
        self.assertIn("stdout-key-order-unstable", codes)
        self.assertNotIn("stdout-json-differs", codes)

    def test_semantic_drift_is_detected_for_each_field(self):
        cases = {
            "selection_order": lambda d: d["selection"]["considered"].reverse(),
            "backend_ids": lambda d: d["backends"].reverse(),
            "status_class_by_backend": lambda d: d["backends"][0].__setitem__(
                "status_class",
                "available" if d["backends"][0]["status_class"] != "available" else "config-invalid"),
            "execution_tier_by_backend": lambda d: d["backends"][0].__setitem__("execution_tier", "emulation"),
            "tier_by_backend": lambda d: d["backends"][0].__setitem__("tier", "emulation"),
            "reason_by_considered": lambda d: d["selection"]["considered"][0].__setitem__("reason", "changed"),
        }
        for field, mutate in cases.items():
            with self.subTest(field=field):
                runs = self._stable_runs()
                mutated = baseline_document()
                mutate(mutated)
                runs[1] = make_invocation("--list-backends", "aarch64", json.dumps(mutated))
                analysis = gate.analyze_variant("v", runs, (0,))
                self.assertIn("semantic-drift", [f["code"] for f in analysis["findings"]])
                self.assertIn(field, analysis["drift_fields"])

    def test_signal_is_detected_as_a_crash(self):
        runs = self._stable_runs()
        runs[0].returncode = -11
        analysis = gate.analyze_variant("v", runs, (0,))
        codes = [f["code"] for f in analysis["findings"]]
        self.assertIn("invocation-crashed", codes)
        self.assertIn("crashes-detected", codes)
        self.assertEqual(analysis["signals"], [11])

    def test_timeout_is_detected(self):
        runs = self._stable_runs()
        runs[0] = make_invocation("--list-backends", "aarch64", "", returncode=None, timed_out=True)
        analysis = gate.analyze_variant("v", runs, (0,))
        codes = [f["code"] for f in analysis["findings"]]
        self.assertIn("invocation-timeout", codes)
        self.assertIn("timeouts-detected", codes)

    def test_duration_percentiles_are_reported(self):
        runs = self._stable_runs(20)
        for index, run in enumerate(runs):
            run.duration_ms = float(index + 1)
        analysis = gate.analyze_variant("v", runs, (0,))
        stats = analysis["duration_ms"]
        self.assertEqual(stats["min"], 1.0)
        self.assertEqual(stats["max"], 20.0)
        self.assertLessEqual(stats["p50"], stats["p95"])


class TestMutationSuite(unittest.TestCase):
    REQUIRED_CATEGORIES = {
        "duplicate-id", "missing-field", "wrong-type", "unknown-tier",
        "unknown-status-class", "tcg-ordering", "cross-arch", "harmony-honesty",
        "empty-reason", "selection-inconsistency", "verified-type",
    }

    def test_suite_is_deterministic_for_a_fixed_seed(self):
        base = baseline_document()
        first = gate.build_mutations(copy.deepcopy(base), 1234)
        second = gate.build_mutations(copy.deepcopy(base), 1234)
        self.assertEqual([m["name"] for m in first], [m["name"] for m in second])
        self.assertEqual([m["expected_codes"] for m in first],
                         [m["expected_codes"] for m in second])

    def test_different_seeds_produce_different_suites(self):
        base = baseline_document()
        first = gate.build_mutations(copy.deepcopy(base), 1234)
        other = gate.build_mutations(copy.deepcopy(base), 9999)
        self.assertNotEqual([m["name"] for m in first], [m["name"] for m in other])

    def test_required_categories_are_covered(self):
        categories = {m["category"] for m in gate.build_mutations(baseline_document(), 7)}
        missing = self.REQUIRED_CATEGORIES - categories
        self.assertEqual(missing, set(), f"mutation suite is missing categories: {missing}")

    def test_every_expected_code_exists_in_the_checker(self):
        """Guards against typos: a mutation must name a code the checker can emit."""
        source = inspect.getsource(rc)
        for mutation in gate.build_mutations(baseline_document(), 20261005):
            for code in mutation["expected_codes"]:
                self.assertIn(f'"{code}"', source,
                              f"mutation '{mutation['name']}' expects unknown code '{code}'")

    def test_expected_codes_are_unique_per_mutation(self):
        for mutation in gate.build_mutations(baseline_document(), 20261005):
            self.assertTrue(mutation["expected_codes"],
                            f"mutation '{mutation['name']}' has no expected code")
            self.assertEqual(len(set(mutation["expected_codes"])),
                             len(mutation["expected_codes"]))

    def test_every_detector_fires_on_the_real_baseline(self):
        result = gate.run_mutation_suite(baseline_document(), 20261005, None)
        self.assertGreaterEqual(result["total"], 50)
        self.assertEqual(result["gaps"], [],
                         f"detector gaps: {result['gaps']}")
        self.assertEqual(result["detectors_fired"], result["total"])

    def test_coverage_accounting_adds_up(self):
        result = gate.run_mutation_suite(baseline_document(), 20261005, None)
        total = sum(b["total"] for b in result["coverage_by_category"].values())
        self.assertEqual(total, result["total"])
        detected = sum(b["detected"] for b in result["coverage_by_category"].values())
        self.assertEqual(detected, result["detectors_fired"])

    def test_failing_samples_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = gate.run_mutation_suite(baseline_document(), 20261005, tmp)
            written = sorted(os.listdir(tmp))
            self.assertEqual(len(written), result["total"])
            sample = json.loads(open(os.path.join(tmp, written[0]), encoding="utf-8").read())
            self.assertIn("schema", sample)

    def test_mutation_does_not_mutate_the_caller_baseline(self):
        base = baseline_document()
        before = gate.canonical_json(base)
        gate.run_mutation_suite(base, 20261005, None)
        self.assertEqual(gate.canonical_json(base), before,
                         "the mutation suite must deep-copy the baseline")

    def test_a_gap_is_reported_when_a_detector_cannot_fire(self):
        # Point a mutation at an impossible expectation and confirm it is surfaced
        # as a gap rather than silently passing.
        original = gate.build_mutations

        def patched(base, seed):
            mutations = original(base, seed)
            mutations.append({
                "name": "impossible-expectation",
                "category": "synthetic",
                "expected_codes": ["no-such-detector-code"],
                "apply": lambda doc: None,
            })
            return mutations

        gate.build_mutations = patched
        try:
            result = gate.run_mutation_suite(baseline_document(), 1, None)
            self.assertFalse(result["ok"])
            self.assertTrue(any(g["name"] == "impossible-expectation" for g in result["gaps"]))
        finally:
            gate.build_mutations = original


class TestReportAndCli(unittest.TestCase):
    def test_markdown_renders_a_blocked_report(self):
        report = {
            "generated_at": "now", "gate_status": "blocked", "method": "m",
            "artifact": {"path": "/x", "expected_sha256": "a", "actual_sha256": "b", "ok": False},
            "blocking": {"reason": "sha mismatch", "policy": "pinned"},
        }
        text = gate.render_markdown(report)
        self.assertIn("Blocked", text)
        self.assertIn("sha mismatch", text)

    def test_markdown_renders_a_full_report(self):
        analysis = gate.analyze_variant(
            "--list-backends aarch64",
            [make_invocation("--list-backends", "aarch64",
                             json.dumps(baseline_document(), ensure_ascii=False))],
            (0,))
        report = {
            "generated_at": "now", "gate_status": "passed", "method": "m",
            "artifact": {"path": "/x", "expected_sha256": "a", "actual_sha256": "a", "ok": True},
            "settings": {"commands": ["--list-backends"], "arches": ["aarch64"],
                         "sequential_rounds": 1, "concurrent_rounds": 1, "concurrency": 2,
                         "timeout_seconds": 5, "seed": 1},
            "normalization": {"fields": [], "rationale": "none needed"},
            "stability": {"variants": [dict(analysis, phase="sequential")],
                          "summary": {"invocations": 1, "sequential_invocations": 1,
                                      "concurrent_invocations": 0, "variants": 1,
                                      "variants_ok": 1, "findings": 0, "crashes": 0,
                                      "timeouts": 0, "stderr_nonempty": 0,
                                      "byte_identical_variants": 1,
                                      "duration_ms": {"min": 1, "p50": 1, "p95": 1, "p99": 1,
                                                      "max": 1}},
                          "findings": []},
            "mutation": {"seed": 1, "total": 1, "detectors_fired": 1, "gaps": [],
                         "coverage_by_category": {"dup": {"total": 1, "detected": 1}},
                         "results": [], "ok": True},
            "failures": [], "not_verified": ["nothing"],
            "blocking": None,
        }
        text = gate.render_markdown(report)
        self.assertIn("Gate status: `passed`", text)
        self.assertIn("Mutation suite", text)
        self.assertIn("Not verified", text)

    def test_cli_reports_blocked_for_a_wrong_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = os.path.join(tmp, "VirtualWorld")
            with open(fake, "wb") as handle:
                handle.write(b"not the certified build")
            os.chmod(fake, 0o755)
            result = subprocess.run(
                [sys.executable, os.path.join(HERE, "run_stability_gate.py"),
                 "--binary", fake, "--expected-sha256", "a" * 64,
                 "--rounds", "1", "--arch", "aarch64", "--quiet", "--out-dir", tmp],
                capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            report = json.loads(open(os.path.join(tmp, "stability-report.json"), encoding="utf-8").read())
            self.assertEqual(report["gate_status"], "blocked")

    def test_cli_writes_both_report_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = os.path.join(tmp, "VirtualWorld")
            with open(fake, "wb") as handle:
                handle.write(b"x")
            os.chmod(fake, 0o755)
            subprocess.run(
                [sys.executable, os.path.join(HERE, "run_stability_gate.py"),
                 "--binary", fake, "--expected-sha256", "b" * 64,
                 "--rounds", "1", "--quiet", "--out-dir", tmp],
                capture_output=True, text=True, check=False)
            self.assertTrue(os.path.isfile(os.path.join(tmp, "stability-report.json")))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "stability-report.md")))


class TestAgainstCertifiedBinary(unittest.TestCase):
    """Only runs when the pinned artifact is present and matches its digest."""

    @classmethod
    def setUpClass(cls):
        cls.binary = os.path.abspath(gate.DEFAULT_BINARY)
        if not os.path.isfile(cls.binary):
            raise unittest.SkipTest("certified binary not present")
        if gate.sha256_file(cls.binary) != gate.DEFAULT_SHA256:
            raise unittest.SkipTest("binary does not match the pinned digest")

    def test_single_invocation_is_deterministic_and_clean(self):
        invocation = gate.run_variant_once(self.binary, "--list-backends", "aarch64",
                                           "selftest", 1, 90.0)
        self.assertFalse(invocation.timed_out)
        self.assertEqual(invocation.returncode, 0)
        self.assertIsNone(invocation.parse_error)
        self.assertEqual(invocation.stderr, "")
        self.assertEqual(invocation.document["schema"], gate.DEFAULT_SCHEMA)

    def test_two_runs_are_byte_identical(self):
        first = gate.run_variant_once(self.binary, "--check", "x86_64", "selftest", 1, 90.0)
        second = gate.run_variant_once(self.binary, "--check", "x86_64", "selftest", 2, 90.0)
        self.assertEqual(first.stdout, second.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
