#!/usr/bin/env python3
"""Self-tests for the parser / input-robustness hardening harness.

Run with either of:

    python3 tools/virtualization-conformance/test_run_parser_hardening.py
    python3 -m unittest discover -s tools/virtualization-conformance -p 'test_*.py'

`TestFixedDefects` is the important class: each test there pins a real defect the
hardening run found in these tools, so the same input can never silently start
crashing again.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import make_final_report as mfr  # noqa: E402
import run_conformance as rc  # noqa: E402
import run_parser_hardening as hard  # noqa: E402
import run_stability_gate as gate  # noqa: E402
import verify_release_manifest as vrm  # noqa: E402


class HarnessCase(unittest.TestCase):
    """Builds the target registry once per test against a private temp dir."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        hard.TARGET_BY_NAME.clear()
        hard._RELEASE_SANDBOX.clear()      # each test gets its own sandbox
        for target in hard.build_targets(self.tmp):
            hard.TARGET_BY_NAME[target.name] = target

    def tearDown(self):
        self._tmp.cleanup()

    def run_one(self, target: str, payload, expect: str = "any",
                expect_code=None, strict: bool = True):
        sample = hard.Sample("t-1", "selftest", target, payload, expect,
                             strict=strict, expect_code=expect_code)
        return hard._run_sample(sample.to_dict())


class TestTargetRegistry(HarnessCase):
    def test_registry_covers_all_four_tools(self):
        modules = {t.module for t in hard.TARGET_BY_NAME.values()}
        self.assertEqual(modules, {"run_conformance", "run_stability_gate",
                                   "verify_release_manifest", "make_final_report"})

    def test_untrusted_validators_are_declared_untrusted(self):
        for name in ("json.parse", "conformance.check", "release.verify",
                     "release.pathsafe", "stability.semantics", "release.classify"):
            self.assertEqual(hard.TARGET_BY_NAME[name].contract, "untrusted", name)

    def test_internal_consumers_are_declared_internal(self):
        for name in ("report.render", "report.defects", "report.invariants"):
            self.assertEqual(hard.TARGET_BY_NAME[name].contract, "internal", name)

    def test_no_target_executes_the_product(self):
        """The harness must never reach a product binary or a VM.

        Note the check is about *calls*, not about the substring appearing:
        fixture data legitimately contains strings like "--list-backends".
        """
        source = open(os.path.join(HERE, "run_parser_hardening.py"), encoding="utf-8").read()
        # No path into the product runner at all.
        for forbidden in ("run_binary(", "check_live_target", "run_conformance.run_binary",
                          "subprocess.Popen", "--start", "os.system", "shutil.which"):
            self.assertNotIn(forbidden, source, f"harness references {forbidden}")
        # The only subprocess invocation re-invokes this file as its own worker.
        runs = [line.strip() for line in source.splitlines() if "subprocess.run(" in line]
        self.assertEqual(len(runs), 1, runs)
        self.assertIn("sys.executable", source)
        self.assertIn('"--worker"', source)


class TestGeneration(HarnessCase):
    def _fingerprint(self, samples):
        # Not a plain list comparison: payloads deliberately include NaN, and
        # `nan != nan`, which would make an identical sample set look different.
        return json.dumps([s.to_dict() for s in samples], ensure_ascii=False,
                          sort_keys=True, default=str)

    def test_generation_is_deterministic(self):
        self.assertEqual(self._fingerprint(hard.generate_samples(4242, 200)),
                         self._fingerprint(hard.generate_samples(4242, 200)))

    def test_generation_is_deterministic_across_processes(self):
        """The sample set must not depend on a randomly named temp directory."""
        digests = []
        for _ in range(2):
            result = subprocess.run(
                [sys.executable, os.path.join(HERE, "run_parser_hardening.py"),
                 "--list-samples", "--min-samples", "200", "--seed", "4242"],
                capture_output=True, text=True, check=False)
            digests.append(result.stdout.strip())
        self.assertEqual(digests[0], digests[1], digests)

    def test_different_seed_differs(self):
        self.assertNotEqual(self._fingerprint(hard.generate_samples(1, 200)),
                            self._fingerprint(hard.generate_samples(2, 200)))

    def test_meets_the_sample_floor(self):
        self.assertGreaterEqual(len(hard.generate_samples(hard.DEFAULT_SEED, 5000)), 5000)

    def test_required_categories_are_present(self):
        categories = {s.category for s in hard.generate_samples(hard.DEFAULT_SEED, 5000)}
        required = {
            "json-empty", "json-truncated", "json-duplicate-key", "json-deep-nesting",
            "json-long-string", "json-control-char", "json-unicode", "json-number-edge",
            "json-nonstandard", "json-long-array", "json-duplicate-array",
            "conformance-type-swap", "release-type-swap", "release-path", "release-hash",
            "release-timestamp", "scalar-arch", "scalar-pathsafe", "structure-semantics",
            "report-incontract",
        }
        self.assertEqual(required - categories, set())

    def test_every_sample_respects_the_resource_bounds(self):
        for sample in hard.generate_samples(hard.DEFAULT_SEED, 5000):
            encoded = len(json.dumps(sample.payload, ensure_ascii=False, default=str))
            self.assertLessEqual(encoded, hard.MAX_SAMPLE_BYTES * 2,
                                 f"{sample.sid} exceeds the byte bound")
            depth = _depth(sample.payload)
            self.assertLessEqual(depth, hard.MAX_DEPTH + 16,
                                 f"{sample.sid} exceeds the depth bound")
            size = len(sample.payload) if isinstance(sample.payload, list) else 0
            self.assertLessEqual(size, hard.MAX_ARRAY_ITEMS * 2,
                                 f"{sample.sid} exceeds the array bound")

    def test_every_target_is_exercised(self):
        used = {s.target for s in hard.generate_samples(hard.DEFAULT_SEED, 5000)}
        self.assertEqual(used, set(hard.TARGET_BY_NAME))

    def test_expected_codes_are_real_codes(self):
        known: set = set()
        for codes in hard.known_codes().values():
            known |= codes
        for sample in hard.generate_samples(hard.DEFAULT_SEED, 5000):
            if sample.expect_code:
                self.assertIn(sample.expect_code, known,
                              f"{sample.sid} expects unknown code {sample.expect_code}")


def _depth(value, current=0):
    if current > 200:
        return current
    if isinstance(value, dict):
        return max([current] + [_depth(v, current + 1) for v in value.values()])
    if isinstance(value, list):
        return max([current] + [_depth(v, current + 1) for v in value])
    return current


class TestContractClasses(HarnessCase):
    def test_untrusted_validator_exception_is_a_failure(self):
        """A raising untrusted validator must be reported, never counted as a pass."""
        original = hard.TARGET_BY_NAME["conformance.check"]

        def boom(_payload):
            raise RuntimeError("simulated validator crash")

        hard.TARGET_BY_NAME["conformance.check"] = hard.Target(
            "conformance.check", "run_conformance", boom, hard._interpret_report)
        try:
            result = self.run_one("conformance.check", {})
            self.assertEqual(result["outcome"], "unhandled-exception")
        finally:
            hard.TARGET_BY_NAME["conformance.check"] = original

    def test_internal_consumer_on_out_of_contract_input_is_informational(self):
        result = self.run_one("report.render", None, strict=False)
        self.assertEqual(result["outcome"], "out-of-contract-exception")

    def test_internal_consumer_on_in_contract_input_is_a_failure(self):
        original = hard.TARGET_BY_NAME["report.render"]

        def boom(_payload):
            raise RuntimeError("simulated renderer crash")

        hard.TARGET_BY_NAME["report.render"] = hard.Target(
            "report.render", "make_final_report", boom, hard._interpret_plain,
            contract="internal")
        try:
            result = self.run_one("report.render", {}, strict=True)
            self.assertEqual(result["outcome"], "unhandled-exception")
        finally:
            hard.TARGET_BY_NAME["report.render"] = original

    def test_missing_expected_code_is_a_gap(self):
        # Rejected -- but for a different reason than the one we demand.
        result = self.run_one("release.pathsafe", "/etc/passwd", expect="reject",
                              expect_code="definitely-not-emitted")
        self.assertEqual(result["outcome"], "missing-expected-code")

    def test_silent_acceptance_is_a_gap(self):
        result = self.run_one("release.pathsafe", "safe/relative", expect="reject")
        self.assertEqual(result["outcome"], "silently-accepted")

    def test_over_rejection_is_a_gap(self):
        result = self.run_one("release.pathsafe", "/etc/passwd", expect="accept")
        self.assertEqual(result["outcome"], "over-rejected")


class TestBaseDocuments(HarnessCase):
    """The bases must be contract-valid, or every sample is rejected for the
    wrong reason and the whole run becomes meaningless."""

    def test_conformance_base_passes_cleanly(self):
        report = rc.check_document(copy.deepcopy(hard.BASE_CONFORMANCE_DOC), source="base")
        self.assertTrue(report.ok, [(f.code, f.message) for f in report.errors])

    def test_release_base_passes_cleanly(self):
        document = hard.bind_sandbox(hard.release_base_document(), self.tmp)
        manifest_path = os.path.join(self.tmp, "base-manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        report = vrm.verify_manifest(manifest_path, document,
                                     vrm.VerificationOptions(rehash=False, crosscheck=False))
        self.assertTrue(report.ok, [(f.code, f.message) for f in report.errors])


class TestFixedDefects(HarnessCase):
    """Regression tests for defects this stage found and fixed in the tools.

    Each one previously raised out of a validator that must never raise on
    untrusted input.
    """

    def test_as_dict_guards_truthy_non_mappings(self):
        # run_conformance: `(doc.get("host") or {}).get("arch")` raised
        # AttributeError when host was the JSON value `true`.
        document = copy.deepcopy(hard.BASE_CONFORMANCE_DOC)
        document["host"] = True
        report = rc.check_document(document, source="regression")
        self.assertIsInstance(report, rc.Report)  # did not raise
        self.assertTrue(report.errors)

    def test_selection_policy_survives_non_mapping_host(self):
        document = copy.deepcopy(hard.BASE_CONFORMANCE_DOC)
        document["host"] = 1
        report = rc.check_document(document, source="regression")
        self.assertTrue(report.errors)

    def test_release_resolve_bundle_root_survives_non_mapping_main_program(self):
        document = copy.deepcopy(hard.release_base_document())
        document["main_program"] = True
        _root, why = vrm.resolve_bundle_root(
            os.path.join(self.tmp, "m.json"), document)
        self.assertIsInstance(why, str)

    def test_release_verify_survives_non_mapping_gate(self):
        document = copy.deepcopy(hard.release_base_document())
        document["gate"] = True
        report = vrm.verify_manifest(os.path.join(self.tmp, "m.json"), document,
                                     vrm.VerificationOptions(rehash=False, crosscheck=False))
        self.assertTrue(report.errors)

    def test_escapes_root_fails_closed_on_unresolvable_path(self):
        # An embedded NUL made os.path.realpath raise ValueError.
        self.assertTrue(vrm.escapes_root(self.tmp, "bad\x00path"))
        self.assertTrue(vrm.escapes_root(self.tmp, os.path.join(self.tmp, "x\x00y")))

    def test_escapes_root_still_answers_normal_paths(self):
        inside = os.path.join(self.tmp, "inside")
        self.assertFalse(vrm.escapes_root(self.tmp, inside))
        self.assertTrue(vrm.escapes_root(self.tmp, "/etc"))

    def test_release_verify_survives_empty_main_program_path(self):
        # `os.path.relpath("", root)` raised ValueError: no path specified.
        document = copy.deepcopy(hard.release_base_document())
        document["main_program"]["path"] = ""
        report = vrm.verify_manifest(os.path.join(self.tmp, "m.json"), document,
                                     vrm.VerificationOptions(rehash=False, crosscheck=False))
        self.assertTrue(report.errors)

    def test_release_verify_survives_nul_in_qemu_path(self):
        # os.path.realpath on the message path raised ValueError.
        document = copy.deepcopy(hard.release_base_document())
        document["qemu_binaries"][0]["path"] = "\x00"
        report = vrm.verify_manifest(os.path.join(self.tmp, "m.json"), document,
                                     vrm.VerificationOptions(rehash=False, crosscheck=False))
        self.assertTrue(report.errors)

    def test_project_semantics_survives_null_containers(self):
        # `dict.get(key, [])` returns None when the key is present with null.
        self.assertEqual(gate.project_semantics({"backends": None})["backend_ids"], [])
        self.assertEqual(
            gate.project_semantics({"selection": {"considered": None}})["selection_order"], [])
        self.assertEqual(gate.project_semantics({"support_matrix": None})["support_matrix_digest"],
                         gate.project_semantics({})["support_matrix_digest"])

    def test_project_semantics_survives_wrong_container_types(self):
        projection = gate.project_semantics({"backends": "x", "selection": 5,
                                             "support_matrix": {}})
        self.assertEqual(projection["backend_ids"], [])
        self.assertEqual(projection["selection_order"], [])

    def test_build_mutations_survives_null_backends(self):
        mutations = gate.build_mutations({"backends": None, "qemu_binaries": None}, 1)
        self.assertTrue(mutations)

    def test_classify_candidates_flags_an_empty_candidate_set(self):
        result = vrm.classify_candidates([])
        self.assertTrue(result["resolution_required"])

    def test_guest_arch_invalid_is_documented_dead_branch(self):
        """Unknown-but-non-empty architectures are tolerated by design; the
        cross-architecture rule is what rejects a bogus acceleration claim."""
        document = copy.deepcopy(hard.BASE_CONFORMANCE_DOC)
        document["guest_arch"] = "sparc"
        codes = {f.code for f in rc.check_document(document, source="t").errors}
        self.assertIn("cross-arch-not-tcg", codes)
        self.assertNotIn("guest-arch-invalid", codes)


class TestMinimization(HarnessCase):
    def test_minimization_shrinks_a_reproducing_failure(self):
        sample = {"sid": "m-1", "category": "selftest", "target": "conformance.check",
                  "payload": {"schema": "x", "pad": "y" * 400},
                  "expect": "reject", "note": "", "strict": True, "expect_code": None}
        result = hard.minimize_sample(sample, timeout=60.0, budget=60)
        self.assertLessEqual(len(json.dumps(result["payload"])), len(json.dumps(sample["payload"])))

    def test_save_failure_writes_a_reproducible_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            sample = {"sid": "s-1", "category": "json-empty", "target": "json.parse",
                      "payload": "", "expect": "any"}
            gap = {"sid": "s-1", "category": "json-empty", "target": "json.parse",
                   "kind": "unhandled-exception", "detail": "boom"}
            path = hard.save_failure(sample, gap, {"payload": "", "reproduced": True}, tmp)
            self.assertTrue(os.path.isfile(path))
            record = json.load(open(path, encoding="utf-8"))
            self.assertEqual(record["sid"], "s-1")
            self.assertEqual(record["kind"], "unhandled-exception")


class TestWorkerProtocol(HarnessCase):
    def test_worker_round_trips_a_batch(self):
        samples = [hard.Sample("w-1", "selftest", "json.parse", "{}", "any").to_dict(),
                   hard.Sample("w-2", "selftest", "release.pathsafe", "/etc", "any").to_dict()]
        status, results, detail = hard.run_batch(samples, 120.0)
        self.assertEqual(status, "ok", detail)
        self.assertEqual(len(results), 2)
        self.assertEqual({r["sid"] for r in results}, {"w-1", "w-2"})

    def test_batch_records_an_unexpected_exception(self):
        samples = [hard.Sample("w-3", "selftest", "report.render", None, "any",
                               strict=False).to_dict()]
        status, results, _ = hard.run_batch(samples, 120.0)
        self.assertEqual(status, "ok")
        self.assertEqual(results[0]["outcome"], "out-of-contract-exception")

    def test_bisection_attributes_a_crash_to_one_sample(self):
        """A worker that dies must be narrowed to the offending sample."""
        samples = [hard.Sample(f"c-{i}", "selftest", "json.parse", "{}", "any")
                   for i in range(4)]
        payloads = [s.to_dict() for s in samples]
        original = hard.run_batch

        def flaky(batch, timeout):
            if any(item["sid"] == "c-2" for item in batch):
                return "crash", [], "simulated worker death"
            return "ok", [{"sid": i["sid"], "category": i["category"], "target": i["target"],
                           "expect": "any", "outcome": "accepted", "codes": [], "detail": ""}
                          for i in batch], ""

        hard.run_batch = flaky
        try:
            results, incidents = hard.execute_samples(samples, 1, 5.0)
        finally:
            hard.run_batch = original
        self.assertEqual([i["sid"] for i in incidents], ["c-2"])
        self.assertEqual(len(results), 4)


class TestReport(unittest.TestCase):
    def test_known_codes_are_scanned_from_every_tool(self):
        known = hard.known_codes()
        self.assertIn("tcg-not-last", known["run_conformance"])
        self.assertIn("gate-false-green", known["verify_release_manifest"])
        self.assertIn("semantic-drift", known["run_stability_gate"])
        # f-string generated codes are included explicitly
        self.assertIn("main-program-sha256-mismatch", known["verify_release_manifest"])

    def test_markdown_renders_a_clean_and_a_gapped_report(self):
        base = {
            "generated_at": "now", "hardening_status": "clean", "method": "m",
            "seed": 1, "sample_manifest_sha256": "a" * 64, "samples": 10,
            "targets": ["json.parse"], "elapsed_seconds": 1.0,
            "limits": {"max_sample_bytes": 1, "max_depth": 1, "max_array_items": 1,
                       "batch_size": 1, "batch_timeout_seconds": 1, "peak_payload_bytes": 1},
            "category_coverage": {"json-empty": {"samples": 1, "gaps": 0}},
            "outcomes": {"rejected": 1},
            "stable_error_codes": {"observed": {"json-unparseable": 1},
                                   "known_vocabulary_size": 10, "unknown_codes": [],
                                   "malformed_codes": []},
            "incidents": [], "out_of_contract_exceptions": {"count": 0, "targets": [],
                                                            "explanation": "e"},
            "gaps": [], "findings": [], "not_verified": ["n"],
        }
        text = hard.render_markdown(base)
        self.assertIn("Status: `clean`", text)
        self.assertIn("Out-of-contract exceptions", text)

        gapped = dict(base, hardening_status="gaps-found",
                      gaps=[{"kind": "unhandled-exception", "target": "json.parse",
                             "category": "json-fuzz", "minimized_bytes": 3, "sample": "p"}],
                      findings=[{"code": "parser-unhandled-exception", "owner": "o",
                                 "target": "json.parse", "message": "m"}])
        text = hard.render_markdown(gapped)
        self.assertIn("gaps-found", text)
        self.assertIn("parser-unhandled-exception", text)

    def test_cli_lists_samples_deterministically(self):
        outputs = []
        for _ in range(2):
            result = subprocess.run(
                [sys.executable, os.path.join(HERE, "run_parser_hardening.py"),
                 "--list-samples", "--min-samples", "6000", "--seed", "99"],
                capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            outputs.append(result.stdout.strip())
        self.assertEqual(outputs[0], outputs[1])
        self.assertIn("samples=6000", outputs[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
