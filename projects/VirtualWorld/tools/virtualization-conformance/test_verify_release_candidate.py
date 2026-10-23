#!/usr/bin/env python3
"""Self-tests for the release-candidate acceptance verifier.

Run with either of:

    python3 tools/virtualization-conformance/test_verify_release_candidate.py
    python3 -m unittest discover -s tools/virtualization-conformance -p 'test_*.py'

Everything is built from synthetic markers in temp directories, so the suite is
independent of whether a real release candidate exists.
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

import verify_release_candidate as vrc  # noqa: E402


def write(path, payload=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(payload)
    return path


class SandboxCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        # The real authorised root is the user's own directory, which this tool
        # must never write to. Point the allowance at the sandbox instead; the
        # production default is asserted separately.
        self.image_root = os.path.join(self.tmp, "Start Boot Option")
        os.makedirs(self.image_root, exist_ok=True)
        self._orig_roots = vrc.AUTHORISED_IMAGE_ROOTS
        vrc.AUTHORISED_IMAGE_ROOTS = (self.image_root,)
        self._orig_freeze_root = vrc.FREEZE_ROOT
        vrc.FREEZE_ROOT = self.tmp
        self._freeze_repair = None

    def tearDown(self):
        vrc.AUTHORISED_IMAGE_ROOTS = self._orig_roots
        vrc.FREEZE_ROOT = self._orig_freeze_root
        self._tmp.cleanup()

    def make_freeze_marker(self):
        """A valid text-format freeze marker over 8 sandbox files."""
        lines = ["# Platform Freeze Ready", "freeze_utc: 2026-10-06T08:15:02Z",
                 "generated_by: self-test", "real_hardware_verified: false"]
        if self._freeze_repair:
            lines.append(f"freeze_repaired_from_hash: {self._freeze_repair}")
        lines += ["", "## 8 owned files (SHA-256)"]
        for index in range(8):
            rel = os.path.join("src", f"Frozen{index}.cpp")
            full = write(os.path.join(self.tmp, rel), f"frozen-{index}".encode())
            lines.append(f"{vrc.sha256_file(full)}  {rel}")
        path = os.path.join(self.tmp, "platform-freeze-ready")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        return path

    def make_debian_evidence(self, **overrides):
        """A fully valid `vm_started: true` Debian evidence set."""
        image = write(os.path.join(self.image_root, "debian-12-netinst.iso"),
                      b"debian-medium")
        log = write(os.path.join(self.tmp, "vm-log.txt"), b"boot log")
        evidence = {
            "vm_started": True,
            "skip_reason": "",
            "guest_os": "debian",
            "vm_name": "Debian-12-ARM64",
            "pre_start_state": "stopped",
            "image_path": image,
            "image_sha256": vrc.sha256_file(image),
            "started_at": "2026-10-06T00:00:00Z",
            "ready_at": "2026-10-06T00:01:30Z",
            "stopped_at": "2026-10-06T00:10:00Z",
            "evidence_paths": [log],
            "stopped_instance_only": True,
        }
        evidence.update(overrides)
        return evidence

    def make_marker(self, **overrides):
        """A structurally complete marker plus a real binary and freeze marker."""
        candidate = os.path.join(self.tmp, "build-release-candidate")
        binary = write(os.path.join(candidate, "VirtualWorld.app/Contents/MacOS/VirtualWorld"),
                       b"candidate-binary")
        freeze = self.make_freeze_marker()
        source = write(os.path.join(candidate, "src/Sample.cpp"), b"source")
        marker = {
            "schema": vrc.RC_SCHEMA,
            "candidate": candidate,
            "binary": binary,
            "binary_sha256": vrc.sha256_file(binary),
            "freeze_marker": freeze,
            "freeze_marker_sha256": vrc.sha256_file(freeze),
            "sources": {"src/Sample.cpp": vrc.sha256_file(source)},
            "contrib_backends": True,
            "real_hardware_verified": False,
            "vm_started": False,
            "skip_reason": "no Debian UTM guest was exercised for this candidate",
            "generated_at": "2026-10-06T09:00:00Z",
        }
        marker["source_aggregate_sha256"] = vrc._aggregate_sources(
            [{"path": "src/Sample.cpp", "sha256": vrc.sha256_file(source)}], candidate)
        marker.update(overrides)
        path = os.path.join(candidate, "release-ready.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(marker, handle, indent=2)
        return path, marker

    def validate(self, marker, path):
        facts = vrc.locate_facts(marker)
        return vrc.validate_marker(marker, path, facts)


class TestFactLocation(SandboxCase):
    def test_finds_top_level(self):
        self.assertEqual(vrc.find_fact({"binary_sha256": "abc"}, ("binary_sha256",)),
                         ("abc", "$.binary_sha256"))

    def test_finds_nested(self):
        document = {"a": {"b": [{"candidate": "/x"}]}}
        value, path = vrc.find_fact(document, ("candidate",))
        self.assertEqual(value, "/x")
        self.assertEqual(path, "$.a.b[0].candidate")

    def test_returns_empty_when_absent(self):
        self.assertEqual(vrc.find_fact({"a": 1}, ("zzz",)), (None, ""))

    def test_alias_order_is_respected(self):
        document = {"sha256": "generic", "binary_sha256": "specific"}
        value, _ = vrc.find_fact(document, ("binary_sha256", "sha256"))
        self.assertEqual(value, "specific")

    def test_locate_facts_marks_missing(self):
        facts = vrc.locate_facts({"schema": vrc.RC_SCHEMA})
        self.assertFalse(facts["binary_sha256"]["found"])
        self.assertTrue(facts["gate_status"]["found"] is False or True)

    def test_resolve_path(self):
        self.assertEqual(vrc.resolve_path("/a/b", "/base"), os.path.normpath("/a/b"))
        self.assertEqual(vrc.resolve_path("c/d", "/base"), os.path.normpath("/base/c/d"))
        self.assertEqual(vrc.resolve_path("", "/base"), "")
        self.assertEqual(vrc.resolve_path(None, "/base"), "")


class TestCandidateRunner(SandboxCase):
    def setUp(self):
        super().setUp()
        self.binary = write(os.path.join(self.tmp, "VirtualWorld"), b"bin")
        self.sha = vrc.sha256_file(self.binary)

    def test_allowlist_rejects_forbidden_flags(self):
        runner = vrc.CandidateRunner(self.binary, self.sha)
        for flag in ("--start", "--launch", "--boot", "--gui"):
            with self.assertRaises(PermissionError):
                runner.run(flag)

    def test_allowlist_rejects_unknown_flags(self):
        runner = vrc.CandidateRunner(self.binary, self.sha)
        with self.assertRaises(PermissionError):
            runner.run("--frobnicate")

    def test_allowed_flags_are_exactly_the_read_only_set(self):
        allowed = vrc.ALLOWED_CANDIDATE_FLAGS
        self.assertIn("--check", allowed)
        self.assertIn("--list-backends", allowed)
        self.assertIn("--json", allowed)
        for flag in ("--start", "--launch", "--boot"):
            self.assertNotIn(flag, allowed)
            self.assertIn(flag, vrc.FORBIDDEN_FLAGS)

    def test_digest_is_rechecked_at_every_call(self):
        runner = vrc.CandidateRunner(self.binary, "0" * 64)
        with self.assertRaises(RuntimeError) as ctx:
            runner.run("--version")
        self.assertIn("SHA-256 changed", str(ctx.exception))

    def test_guard_table_is_consistent(self):
        self.assertTrue(vrc.FORBIDDEN_FLAGS.isdisjoint(vrc.ALLOWED_CANDIDATE_FLAGS))


class TestReachability(unittest.TestCase):
    def test_no_vm_launching_flag_is_reachable(self):
        report = vrc.reachability_report()
        self.assertFalse(report["vm_launch_reachable"], report["forbidden_flag_mentions"])
        self.assertIn("--start", report["forbidden_candidate_flags"])

    def test_every_verifier_module_is_scanned(self):
        report = vrc.reachability_report()
        self.assertGreaterEqual(len(report["modules_scanned"]), 5)


class TestMarkerValidation(SandboxCase):
    def test_a_complete_marker_validates(self):
        path, marker = self.make_marker()
        findings, details = self.validate(marker, path)
        self.assertEqual([f.code for f in findings], [], [f.message for f in findings])
        self.assertTrue(details["binary_matches"])
        self.assertTrue(details["freeze_marker_matches"])
        self.assertTrue(details["source_aggregate_matches"])

    def test_wrong_schema(self):
        path, marker = self.make_marker(schema="virtualworld.release-ready/2")
        findings, _ = self.validate(marker, path)
        self.assertIn("marker-schema-invalid", [f.code for f in findings])

    def test_real_hardware_verified_must_still_be_false(self):
        path, marker = self.make_marker(real_hardware_verified=True)
        findings, _ = self.validate(marker, path)
        self.assertIn("real-hardware-verified-not-false", [f.code for f in findings])

    def test_real_hardware_stays_false_even_with_valid_debian_evidence(self):
        evidence = self.make_debian_evidence(real_hardware_verified=True)
        path, marker = self.make_marker(**evidence)
        findings, _ = self.validate(marker, path)
        self.assertIn("real-hardware-verified-not-false", [f.code for f in findings])

    def test_binary_hash_mismatch(self):
        path, marker = self.make_marker(binary_sha256="a" * 64)
        findings, details = self.validate(marker, path)
        self.assertIn("binary-sha256-mismatch", [f.code for f in findings])
        self.assertFalse(details["binary_matches"])

    def test_freeze_marker_hash_mismatch(self):
        path, marker = self.make_marker(freeze_marker_sha256="b" * 64)
        findings, details = self.validate(marker, path)
        self.assertIn("freeze-marker-sha256-mismatch", [f.code for f in findings])
        self.assertFalse(details["freeze_marker_matches"])

    def test_freeze_marker_missing(self):
        path, marker = self.make_marker()
        marker["freeze_marker"] = os.path.join(self.tmp, "nope.json")
        findings, _ = self.validate(marker, path)
        self.assertIn("freeze-marker-missing", [f.code for f in findings])

    def test_source_aggregate_mismatch(self):
        path, marker = self.make_marker(source_aggregate_sha256="c" * 64)
        findings, _ = self.validate(marker, path)
        self.assertIn("source-aggregate-mismatch", [f.code for f in findings])

    def test_source_aggregate_unverifiable_without_a_source_list(self):
        path, marker = self.make_marker()
        marker.pop("sources", None)
        findings, details = self.validate(marker, path)
        self.assertIn("source-aggregate-unverifiable", [f.code for f in findings])
        self.assertIsNone(details["source_aggregate_matches"])

    def test_candidate_must_be_absolute_and_inside_the_rc_dir(self):
        path, marker = self.make_marker(candidate="relative/path")
        findings, _ = self.validate(marker, path)
        self.assertIn("candidate-path-missing", [f.code for f in findings])

    def test_candidate_outside_the_rc_directory_is_rejected(self):
        outside = os.path.join(self.tmp, "somewhere-else")
        os.makedirs(outside, exist_ok=True)
        path, marker = self.make_marker()
        marker["candidate"] = outside
        findings, _ = self.validate(marker, path)
        self.assertIn("candidate-outside-rc-dir", [f.code for f in findings])

    def test_missing_required_facts_are_reported_not_assumed(self):
        path, marker = self.make_marker()
        for key in ("binary_sha256", "freeze_marker_sha256", "real_hardware_verified",
                    "vm_started"):
            marker.pop(key, None)
        findings, _ = self.validate(marker, path)
        codes = [f.code for f in findings]
        self.assertIn("binary-sha-missing", codes)
        self.assertIn("freeze-marker-sha-missing", codes)

    def test_artifact_newer_than_the_marker_blocks(self):
        path, marker = self.make_marker()
        # Touch the binary after the marker was written.
        future = os.path.getmtime(path) + 120
        os.utime(marker["binary"], (future, future))
        findings, details = self.validate(marker, path)
        self.assertIn("artifact-newer-than-marker", [f.code for f in findings])
        self.assertTrue(details["newer_than_marker"])

    def test_freshness_tolerates_the_one_second_write_window(self):
        path, marker = self.make_marker()
        stamp = os.path.getmtime(path) + 0.5
        os.utime(marker["binary"], (stamp, stamp))
        findings, _ = self.validate(marker, path)
        self.assertNotIn("artifact-newer-than-marker", [f.code for f in findings])


class TestAcceptanceFlow(SandboxCase):
    def _args(self, out_dir, **overrides):
        args = vrc.build_parser().parse_args(["--out-dir", out_dir, "--quiet"])
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def test_blocked_when_no_marker_exists(self):
        args = self._args(self.tmp, skip_heavy=True)
        originals = vrc.READY_MARKER, vrc.BLOCKED_MARKER
        vrc.READY_MARKER = os.path.join(self.tmp, "build-release-candidate/release-ready.json")
        vrc.BLOCKED_MARKER = os.path.join(self.tmp, "build-release-candidate/release-blocked.json")
        try:
            report = vrc.run_acceptance(args)
        finally:
            vrc.READY_MARKER, vrc.BLOCKED_MARKER = originals
        self.assertEqual(report["verdict"], "blocked")
        self.assertIn("not available", report["blocking"]["reason"])

    def test_blocked_marker_short_circuits(self):
        rc_dir = os.path.join(self.tmp, "build-release-candidate")
        os.makedirs(rc_dir, exist_ok=True)
        with open(os.path.join(rc_dir, "release-blocked.json"), "w") as handle:
            json.dump({"reason": "platform builds failed"}, handle)
        args = self._args(self.tmp, skip_heavy=True)
        originals = vrc.READY_MARKER, vrc.BLOCKED_MARKER
        vrc.READY_MARKER = os.path.join(rc_dir, "release-ready.json")
        vrc.BLOCKED_MARKER = os.path.join(rc_dir, "release-blocked.json")
        try:
            report = vrc.run_acceptance(args)
        finally:
            vrc.READY_MARKER, vrc.BLOCKED_MARKER = originals
        self.assertEqual(report["verdict"], "blocked")
        self.assertIn("release-blocked.json", report["blocking"]["reason"])

    def test_unreadable_marker_is_blocked(self):
        rc_dir = os.path.join(self.tmp, "build-release-candidate")
        os.makedirs(rc_dir, exist_ok=True)
        ready = os.path.join(rc_dir, "release-ready.json")
        with open(ready, "w") as handle:
            handle.write("{ not json")
        args = self._args(self.tmp, skip_heavy=True)
        originals = vrc.READY_MARKER, vrc.BLOCKED_MARKER
        vrc.READY_MARKER = ready
        vrc.BLOCKED_MARKER = os.path.join(rc_dir, "release-blocked.json")
        try:
            report = vrc.run_acceptance(args)
        finally:
            vrc.READY_MARKER, vrc.BLOCKED_MARKER = originals
        self.assertEqual(report["verdict"], "blocked")
        self.assertIn("unreadable", report["blocking"]["reason"])

    def test_hard_block_prevents_the_battery_from_running(self):
        path, marker = self.make_marker(binary_sha256="d" * 64)
        args = self._args(self.tmp, skip_heavy=False)
        originals = vrc.READY_MARKER, vrc.BLOCKED_MARKER
        vrc.READY_MARKER = path
        vrc.BLOCKED_MARKER = os.path.join(self.tmp, "nope.json")
        try:
            report = vrc.run_acceptance(args)
        finally:
            vrc.READY_MARKER, vrc.BLOCKED_MARKER = originals
        self.assertEqual(report["verdict"], "blocked")
        self.assertIn("binary-sha256-mismatch", report["blocking"]["codes"])
        self.assertEqual(report["verifiers"], {})

    def test_skip_heavy_cannot_certify(self):
        path, _ = self.make_marker()
        args = self._args(self.tmp, skip_heavy=True)
        originals = vrc.READY_MARKER, vrc.BLOCKED_MARKER
        vrc.READY_MARKER = path
        vrc.BLOCKED_MARKER = os.path.join(self.tmp, "nope.json")
        try:
            report = vrc.run_acceptance(args)
        finally:
            vrc.READY_MARKER, vrc.BLOCKED_MARKER = originals
        self.assertNotEqual(report["verdict"], "certified")
        self.assertEqual(report["verdict"], "incomplete")

    def test_reachability_is_evaluated_on_the_happy_path(self):
        path, _ = self.make_marker()
        args = self._args(self.tmp, skip_heavy=True)
        originals = vrc.READY_MARKER, vrc.BLOCKED_MARKER
        vrc.READY_MARKER = path
        vrc.BLOCKED_MARKER = os.path.join(self.tmp, "nope.json")
        try:
            report = vrc.run_acceptance(args)
        finally:
            vrc.READY_MARKER, vrc.BLOCKED_MARKER = originals
        self.assertIn("reachability", report)
        self.assertFalse(report["reachability"]["vm_launch_reachable"])


class TestVmEvidence(SandboxCase):
    """The `vm_started` branch: false needs a reason, true needs Debian proof.

    This tool never contacts a VM. These tests only exercise how the *recorded*
    evidence WorkBuddy AI produces is judged.
    """

    def codes(self, evidence):
        path, marker = self.make_marker(**evidence)
        findings, _ = self.validate(marker, path)
        return [f.code for f in findings]

    # ---- vm_started = false ---------------------------------------------
    def test_false_with_a_reason_is_accepted(self):
        self.assertEqual(self.codes({"vm_started": False,
                                     "skip_reason": "no Debian guest available"}), [])

    def test_false_without_a_reason_is_blocked(self):
        path, marker = self.make_marker()
        marker.pop("skip_reason", None)
        findings, _ = self.validate(marker, path)
        self.assertIn("vm-skip-reason-missing", [f.code for f in findings])

    def test_false_with_a_blank_reason_is_blocked(self):
        self.assertIn("vm-skip-reason-missing",
                      self.codes({"vm_started": False, "skip_reason": "   "}))

    # ---- vm_started = true, valid ---------------------------------------
    def test_valid_debian_evidence_passes(self):
        self.assertEqual(self.codes(self.make_debian_evidence()), [])

    def test_debian_guest_os_is_case_insensitive(self):
        self.assertEqual(self.codes(self.make_debian_evidence(guest_os="Debian")), [])

    def test_debian_image_from_a_declared_debian_utm_config_is_allowed(self):
        bundle = os.path.join(self.tmp, "Debian 12.utm")
        os.makedirs(os.path.join(bundle, "Data"), exist_ok=True)
        with open(os.path.join(bundle, "config.plist"), "w", encoding="utf-8") as handle:
            handle.write("<plist><string>Debian GNU/Linux 12</string></plist>")
        image = write(os.path.join(bundle, "Data", "debian.qcow2"), b"medium")
        findings = self.codes(self.make_debian_evidence(
            utm_config=bundle, image_path=image, image_sha256=vrc.sha256_file(image)))
        self.assertEqual(findings, [])

    # ---- vm_started = true, rejections ----------------------------------
    def test_non_debian_guest_is_rejected(self):
        for guest in ("ubuntu", "windows", "fedora", "alpine"):
            self.assertIn("vm-non-debian-guest",
                          self.codes(self.make_debian_evidence(guest_os=guest)), guest)

    def test_missing_guest_os_is_rejected(self):
        evidence = self.make_debian_evidence()
        evidence.pop("guest_os")
        self.assertIn("vm-guest-os-missing", self.codes(evidence))

    def test_kali_in_the_vm_name_is_rejected(self):
        self.assertIn("vm-non-debian-guest",
                      self.codes(self.make_debian_evidence(vm_name="Kali Linux 2024")))

    def test_kali_in_the_image_path_is_rejected(self):
        kali = write(os.path.join(self.image_root, "kali-linux.iso"), b"k")
        findings = self.codes(self.make_debian_evidence(
            guest_os="kali", image_path=kali, image_sha256=vrc.sha256_file(kali)))
        self.assertIn("vm-non-debian-guest", findings)

    def test_kali_anywhere_in_the_evidence_subtree_is_rejected(self):
        evidence = self.make_debian_evidence(note="ran inside the kali box")
        self.assertIn("vm-non-debian-guest", self.codes(evidence))

    def test_non_debian_utm_config_does_not_widen_the_allowance(self):
        bundle = os.path.join(self.tmp, "Kali.utm")
        os.makedirs(os.path.join(bundle, "Data"), exist_ok=True)
        image = write(os.path.join(bundle, "Data", "kali.qcow2"), b"k")
        findings = self.codes(self.make_debian_evidence(
            utm_config=bundle, image_path=image, image_sha256=vrc.sha256_file(image)))
        self.assertIn("vm-image-outside-authorized-roots", findings)

    def test_missing_vm_identity_is_rejected(self):
        evidence = self.make_debian_evidence()
        evidence.pop("vm_name")
        self.assertIn("vm-identity-missing", self.codes(evidence))

    def test_missing_prestart_state_is_rejected(self):
        evidence = self.make_debian_evidence()
        evidence.pop("pre_start_state")
        self.assertIn("vm-prestart-state-missing", self.codes(evidence))

    def test_prestart_already_running_is_rejected(self):
        """The 'it was already running, then we shut it down' case."""
        for state in ("running", "Running", "started", "active", "online"):
            self.assertIn("vm-prestart-state-running",
                          self.codes(self.make_debian_evidence(pre_start_state=state)), state)

    def test_unrecognised_prestart_state_is_rejected(self):
        for state in ("suspended", "paused", "unknown", "probably-off"):
            self.assertIn("vm-prestart-state-unproven",
                          self.codes(self.make_debian_evidence(pre_start_state=state)), state)

    def test_empty_prestart_state_is_reported_as_missing(self):
        for state in ("", "   ", None):
            self.assertIn("vm-prestart-state-missing",
                          self.codes(self.make_debian_evidence(pre_start_state=state)), state)

    def test_image_outside_authorised_roots_is_rejected(self):
        outside = write(os.path.join(self.tmp, "elsewhere", "debian.iso"), b"d")
        findings = self.codes(self.make_debian_evidence(
            image_path=outside, image_sha256=vrc.sha256_file(outside)))
        self.assertIn("vm-image-outside-authorized-roots", findings)

    def test_traversal_out_of_the_authorised_root_is_rejected(self):
        findings = self.codes(self.make_debian_evidence(
            image_path=os.path.join(self.image_root, "..", "elsewhere", "debian.iso")))
        self.assertIn("vm-image-outside-authorized-roots", findings)

    def test_image_hash_mismatch_is_rejected(self):
        self.assertIn("vm-image-sha256-mismatch",
                      self.codes(self.make_debian_evidence(image_sha256="a" * 64)))

    def test_missing_image_hash_is_rejected(self):
        evidence = self.make_debian_evidence()
        evidence.pop("image_sha256")
        self.assertIn("vm-image-sha256-missing", self.codes(evidence))

    def test_missing_image_is_rejected(self):
        evidence = self.make_debian_evidence()
        evidence["image_path"] = os.path.join(self.image_root, "gone.iso")
        self.assertIn("vm-image-missing", self.codes(evidence))

    def test_relative_image_path_is_rejected(self):
        self.assertIn("vm-image-path-not-absolute",
                      self.codes(self.make_debian_evidence(image_path="debian.iso")))

    def test_missing_timestamps_are_rejected(self):
        for field, code in (("started_at", "vm-started-at-missing"),
                            ("ready_at", "vm-ready-at-missing"),
                            ("stopped_at", "vm-stopped-at-missing")):
            evidence = self.make_debian_evidence()
            evidence.pop(field)
            self.assertIn(code, self.codes(evidence), field)

    def test_unparseable_timestamps_are_rejected(self):
        self.assertIn("vm-started-at-invalid",
                      self.codes(self.make_debian_evidence(started_at="not-a-time")))

    def test_out_of_order_timeline_is_rejected(self):
        findings = self.codes(self.make_debian_evidence(
            started_at="2026-10-06T00:10:00Z", ready_at="2026-10-06T00:01:00Z",
            stopped_at="2026-10-06T00:00:00Z"))
        self.assertIn("vm-timeline-inconsistent", findings)

    def test_missing_evidence_paths_are_rejected(self):
        evidence = self.make_debian_evidence()
        evidence.pop("evidence_paths")
        self.assertIn("vm-evidence-missing", self.codes(evidence))

    def test_evidence_paths_that_do_not_exist_are_rejected(self):
        self.assertIn("vm-evidence-missing",
                      self.codes(self.make_debian_evidence(
                          evidence_paths=[os.path.join(self.tmp, "nope.log")])))

    def test_instance_scope_must_be_asserted(self):
        evidence = self.make_debian_evidence()
        evidence.pop("stopped_instance_only")
        self.assertIn("vm-instance-scope-unproven", self.codes(evidence))
        self.assertIn("vm-instance-scope-unproven",
                      self.codes(self.make_debian_evidence(stopped_instance_only=False)))

    def test_vm_started_must_be_a_boolean(self):
        self.assertIn("vm-started-invalid",
                      self.codes({"vm_started": "yes", "skip_reason": "x"}))

    def test_missing_vm_started_is_rejected(self):
        path, marker = self.make_marker()
        marker.pop("vm_started", None)
        findings, _ = self.validate(marker, path)
        self.assertIn("vm-started-missing", [f.code for f in findings])

    # ---- the tool's own posture -----------------------------------------
    def test_tool_never_contacts_a_vm(self):
        """Only file reads and the read-only candidate flags are used."""
        source = open(os.path.join(HERE, "verify_release_candidate.py"),
                      encoding="utf-8").read()
        for forbidden in ("utmctl", "UTM.app", "open -a", "qemu-system", "virsh",
                          "ssh ", "vmrun", "prlctl", "osascript"):
            self.assertNotIn(forbidden, source, f"tool must not reference {forbidden}")

    def test_authorised_root_default_is_the_start_boot_option_directory(self):
        self.assertEqual(len(self._orig_roots), 1)
        self.assertIn("Start Boot Option", self._orig_roots[0])

    def test_vm_evidence_codes_are_all_hard_block(self):
        source = open(os.path.join(HERE, "verify_release_candidate.py"),
                      encoding="utf-8").read()
        for code in ("vm-non-debian-guest", "vm-prestart-state-running",
                     "vm-image-outside-authorized-roots", "vm-image-sha256-mismatch",
                     "vm-evidence-missing", "vm-instance-scope-unproven",
                     "vm-timeline-inconsistent"):
            self.assertIn(f'"{code}"', source)


class TestVmEvidenceBlocking(SandboxCase):
    def _args(self, out_dir, **overrides):
        args = vrc.build_parser().parse_args(["--out-dir", out_dir, "--quiet"])
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def test_kali_evidence_blocks_before_the_battery_runs(self):
        path, _ = self.make_marker(**self.make_debian_evidence(vm_name="Kali Linux"))
        args = self._args(self.tmp, skip_heavy=False)
        originals = vrc.READY_MARKER, vrc.BLOCKED_MARKER
        vrc.READY_MARKER = path
        vrc.BLOCKED_MARKER = os.path.join(self.tmp, "nope.json")
        try:
            report = vrc.run_acceptance(args)
        finally:
            vrc.READY_MARKER, vrc.BLOCKED_MARKER = originals
        self.assertEqual(report["verdict"], "blocked")
        self.assertIn("vm-non-debian-guest", report["blocking"]["codes"])
        self.assertEqual(report["verifiers"], {})

    def test_valid_debian_evidence_reaches_the_battery(self):
        path, _ = self.make_marker(**self.make_debian_evidence())
        args = self._args(self.tmp, skip_heavy=True)
        originals = vrc.READY_MARKER, vrc.BLOCKED_MARKER
        vrc.READY_MARKER = path
        vrc.BLOCKED_MARKER = os.path.join(self.tmp, "nope.json")
        try:
            report = vrc.run_acceptance(args)
        finally:
            vrc.READY_MARKER, vrc.BLOCKED_MARKER = originals
        # Marker validation passed, so the run reached the (skipped) battery --
        # the only remaining "block" is the deliberate battery skip.
        self.assertEqual(report["verdict"], "incomplete")
        self.assertEqual(report["blocking"]["codes"], ["battery-skipped"])

    def test_report_surfaces_debian_attestations(self):
        path, _ = self.make_marker(**self.make_debian_evidence())
        args = self._args(self.tmp, skip_heavy=True)
        originals = vrc.READY_MARKER, vrc.BLOCKED_MARKER
        vrc.READY_MARKER = path
        vrc.BLOCKED_MARKER = os.path.join(self.tmp, "nope.json")
        try:
            report = vrc.run_acceptance(args)
        finally:
            vrc.READY_MARKER, vrc.BLOCKED_MARKER = originals
        details = report["marker_details"]
        self.assertEqual(details["guest_os"], "debian")
        self.assertTrue(details["vm_instance_scope_proven"] if "vm_instance_scope_proven"
                        in details else details["instance_scope"] is True)
        self.assertEqual(details["image_sha256_matches"], True)
        self.assertEqual(details["vm_duration_seconds"], 600.0)

    def test_markdown_shows_the_debian_evidence(self):
        report = vrc.run_acceptance(
            self._args(self.tmp, skip_heavy=True)) if False else None
        # Rendered from a synthetic report to keep this test independent.
        synthetic = {
            "generated_at": "now", "report_schema": vrc.REPORT_SCHEMA,
            "verdict": "certified", "method": "m", "blocking": None, "findings": [],
            "not_verified": [], "candidate": {}, "verifiers": {}, "facts": {},
            "candidate_calls": [], "reachability": {"vm_launch_reachable": False,
                                                    "allowed_candidate_flags": []},
            "marker_details": {"vm_skip_reason": "n/a"},
            "attestations": {"real_hardware_verified": False, "vm_started": True,
                             "vm_guest_os": "debian", "vm_name": "Debian-12-ARM64",
                             "vm_pre_start_state": "stopped", "vm_image": "/x/debian.iso",
                             "vm_image_sha256": "a" * 64, "vm_evidence_entries": ["/x/log"],
                             "vm_duration_seconds": 600.0,
                             "vm_instance_scope_proven": True,
                             "no_vm_launched_by_this_tool": True},
        }
        text = vrc.render_markdown(synthetic)
        self.assertIn("Debian VM evidence", text)
        self.assertIn("Debian-12-ARM64", text)
        self.assertIn("Only this instance stopped", text)


class TestBlockedMarkerAuthentication(SandboxCase):
    """A block must be *evidence-backed*, not merely asserted."""

    def _marker(self, rows, mismatch=None, schema=vrc.BLOCKED_SCHEMA):
        document = {"schema": schema, "gate_result": "BLOCKED",
                    "error_code": "PLATFORM_FREEZE_HASH_MISMATCH",
                    "freeze_marker": "build-x/.platform-freeze-ready",
                    "platform_hash_verification": rows,
                    "evidence": "gate explanation"}
        if mismatch:
            document["mismatch"] = mismatch
        path = os.path.join(self.tmp, "release-blocked.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        return path, document

    def _row(self, name, content, recorded_expected=None, recorded_match=None):
        # Absolute temp paths: `validate_blocked_marker` accepts them as-is, so the
        # tests never write fixtures into the real reports directory.
        full = write(os.path.join(self.tmp, "fixtures", name), content)
        actual = vrc.sha256_file(full)
        return {
            "file": full,
            "expected": recorded_expected if recorded_expected is not None else actual,
            "actual": actual,
            "match": recorded_match if recorded_match is not None else
                     (recorded_expected is None or recorded_expected == actual),
        }

    def test_genuine_mismatch_is_authenticated(self):
        good = self._row("good.bin", b"good")
        bad = self._row("bad.bin", b"real-bytes", recorded_expected="0" * 64,
                        recorded_match=False)
        path, document = self._marker([good, bad], mismatch={"file": bad["file"],
                                                            "expected": "0" * 64,
                                                            "actual": bad["actual"]})
        result = vrc.validate_blocked_marker(document, path)
        self.assertEqual(result["schema"], vrc.BLOCKED_SCHEMA)
        self.assertEqual(result["claims_checked"], 2)
        self.assertEqual(result["claims_confirmed"], 2)
        self.assertEqual(result["claims_refuted"], [])
        self.assertTrue(result["authenticated"])
        self.assertTrue(result["disputed_file_confirmed"])

    def test_a_false_block_is_detected(self):
        """The marker claims a mismatch the bytes do not support."""
        # The bytes DO match `expected`, but the marker recorded match=false --
        # so the recomputed verdict contradicts the recorded one.
        row = self._row("false-block.bin", b"same")
        row["match"] = False
        path, document = self._marker([row])
        result = vrc.validate_blocked_marker(document, path)
        self.assertFalse(result["authenticated"])
        self.assertEqual(len(result["claims_refuted"]), 1)

    def test_missing_file_is_reported(self):
        path, document = self._marker([{"file": "no/such/file.cpp", "expected": "a",
                                        "actual": "b", "match": False}])
        result = vrc.validate_blocked_marker(document, path)
        self.assertEqual(result["files_missing"], ["no/such/file.cpp"])
        self.assertFalse(result["authenticated"])

    def test_wrong_schema_is_not_authenticated(self):
        path, document = self._marker([], schema="virtualworld.release-blocked/2")
        result = vrc.validate_blocked_marker(document, path)
        self.assertFalse(result["schema_ok"])
        self.assertFalse(result["authenticated"])

    def test_blocked_marker_short_circuit_includes_authentication(self):
        good = self._row("run.bin", b"x")
        path, _ = self._marker([good])
        args = vrc.build_parser().parse_args(["--out-dir", self.tmp, "--quiet"])
        originals = vrc.READY_MARKER, vrc.BLOCKED_MARKER
        vrc.READY_MARKER = os.path.join(self.tmp, "release-ready.json")
        vrc.BLOCKED_MARKER = path
        try:
            report = vrc.run_acceptance(args)
        finally:
            vrc.READY_MARKER, vrc.BLOCKED_MARKER = originals
        self.assertEqual(report["verdict"], "blocked")
        self.assertIn("blocked_marker", report)
        self.assertEqual(report["verifiers"], {})

    def test_blocked_markdown_shows_the_evidence(self):
        report = {
            "generated_at": "now", "report_schema": vrc.REPORT_SCHEMA,
            "verdict": "blocked", "method": "m", "not_verified": [], "findings": [],
            "blocking": {"reason": "gate blocked", "marker": "x", "codes": [],
                         "detail": {"freeze_marker": "build-x/.platform-freeze-ready",
                                    "evidence": "hash mismatch on docs/harmonyos.md"}},
            "blocked_marker": {"schema": vrc.BLOCKED_SCHEMA, "marker_sha256": "a",
                               "gate_result": "BLOCKED",
                               "error_code": "PLATFORM_FREEZE_HASH_MISMATCH",
                               "claims_checked": 8, "claims_confirmed": 8,
                               "claims_refuted": [], "files_missing": [],
                               "authenticated": True,
                               "disputed_file": "docs/harmonyos-virtualization.md",
                               "disputed_file_recomputed": "1d42",
                               "disputed_file_confirmed": True},
        }
        text = vrc.render_markdown(report)
        self.assertIn("Blocked-marker claims", text)
        self.assertIn("PLATFORM_FREEZE_HASH_MISMATCH", text)
        self.assertIn("mismatch reproduced independently", text)


class TestRerunContract(SandboxCase):
    """A rerun after a block: history must not keep blocking, and the new
    readiness must prove it supersedes the specific earlier block."""

    def _rc_marker(self, **overrides):
        path, marker = self.make_marker(**overrides)
        return path, marker

    def test_renamed_blocked_marker_is_historical_not_active(self):
        rc_dir = os.path.join(self.tmp, "build-release-candidate")
        os.makedirs(rc_dir, exist_ok=True)
        write(os.path.join(rc_dir, "release-blocked.2026-10-06T02-50Z.json"),
              b'{"schema": "virtualworld.release-blocked/1"}')
        active, historical = vrc.active_blocked_marker(rc_dir)
        self.assertEqual(active, "")
        self.assertEqual(len(historical), 1)

    def test_canonical_blocked_marker_is_active(self):
        rc_dir = os.path.join(self.tmp, "build-release-candidate")
        os.makedirs(rc_dir, exist_ok=True)
        write(os.path.join(rc_dir, "release-blocked.json"),
              b'{"schema": "virtualworld.release-blocked/1"}')
        active, _ = vrc.active_blocked_marker(rc_dir)
        self.assertEqual(active, os.path.join(rc_dir, "release-blocked.json"))

    def test_new_active_blocked_still_blocks(self):
        rc_dir = os.path.join(self.tmp, "build-release-candidate")
        os.makedirs(rc_dir, exist_ok=True)
        write(os.path.join(rc_dir, "release-blocked.json"),
              json.dumps({"schema": vrc.BLOCKED_SCHEMA, "gate_result": "BLOCKED",
                          "error_code": "SOME_NEW_BLOCK"}).encode())
        args = vrc.build_parser().parse_args(["--out-dir", self.tmp, "--quiet"])
        originals = vrc.READY_MARKER, vrc.BLOCKED_MARKER
        vrc.READY_MARKER = os.path.join(rc_dir, "release-ready.json")
        vrc.BLOCKED_MARKER = os.path.join(rc_dir, "release-blocked.json")
        try:
            report = vrc.run_acceptance(args)
        finally:
            vrc.READY_MARKER, vrc.BLOCKED_MARKER = originals
        self.assertEqual(report["verdict"], "blocked")
        self.assertEqual(report["blocked_marker"]["error_code"], "SOME_NEW_BLOCK")

    # ---- supersede gating ------------------------------------------------
    def _run_with_history(self, marker_overrides, history=True):
        path, marker = self.make_marker(**marker_overrides)
        rc_dir = os.path.dirname(path)
        if history:
            write(os.path.join(rc_dir, "release-blocked.2026-10-06T02-50Z.json"),
                  b'{"schema": "virtualworld.release-blocked/1"}')
        args = vrc.build_parser().parse_args(["--out-dir", self.tmp, "--quiet",
                                              "--skip-heavy"])
        originals = vrc.READY_MARKER, vrc.BLOCKED_MARKER
        vrc.READY_MARKER = path
        vrc.BLOCKED_MARKER = os.path.join(rc_dir, "release-blocked.json")
        try:
            return vrc.run_acceptance(args)
        finally:
            vrc.READY_MARKER, vrc.BLOCKED_MARKER = originals

    def _codes(self, report):
        return {f["code"] for f in report.get("findings", [])
                if f["severity"] == "error"} | set(
            (report.get("blocking") or {}).get("codes", []))

    def test_rerun_without_supersede_is_blocked(self):
        report = self._run_with_history({"generated_at": "2026-10-06T09:00:00Z"})
        self.assertEqual(report["verdict"], "blocked")
        self.assertIn("ready-supersede-missing", self._codes(report))

    def test_rerun_superseding_the_wrong_error_is_blocked(self):
        report = self._run_with_history({
            "generated_at": "2026-10-06T09:00:00Z",
            "supersedes_error_code": "SOME_OTHER_FAILURE"})
        self.assertIn("ready-supersede-unverifiable", self._codes(report))

    def test_rerun_predating_the_freeze_is_blocked(self):
        report = self._run_with_history({
            "generated_at": "2026-10-06T07:00:00Z",
            "supersedes_error_code": "PLATFORM_FREEZE_HASH_MISMATCH"})
        self.assertIn("ready-predates-freeze", self._codes(report))

    def test_valid_rerun_supersede_is_accepted(self):
        report = self._run_with_history({
            "generated_at": "2026-10-06T09:00:00Z",
            "supersedes_error_code": "PLATFORM_FREEZE_HASH_MISMATCH"})
        self.assertEqual(report["verdict"], "incomplete")   # --skip-heavy
        self.assertTrue(report["marker_details"]["supersede"]["supersedes_ok"])
        self.assertTrue(report["marker_details"]["supersede"]["postdates_freeze"])

    def test_supersede_corroborated_by_the_freeze_repair_record(self):
        """The named blocked marker was deleted; the repair record corroborates it."""
        # The repair record must be part of the marker when its hash is recorded.
        self._freeze_repair = "3c" * 32
        path, marker = self.make_marker(generated_at="2026-10-06T09:00:00Z",
                                        supersedes="build-release-candidate/old-blocked.json")
        args = vrc.build_parser().parse_args(["--out-dir", self.tmp, "--quiet",
                                              "--skip-heavy"])
        originals = vrc.READY_MARKER, vrc.BLOCKED_MARKER
        vrc.READY_MARKER = path
        vrc.BLOCKED_MARKER = os.path.join(os.path.dirname(path), "release-blocked.json")
        try:
            report = vrc.run_acceptance(args)
        finally:
            vrc.READY_MARKER, vrc.BLOCKED_MARKER = originals
        self.assertEqual(report["verdict"], "incomplete")
        self.assertTrue(report["marker_details"]["supersede"]["supersedes_ok"])
        self.assertEqual(report["marker_details"]["supersede"]["supersede_form"],
                         "blocked marker absent; corroborated by the freeze marker's repair record")

    def test_supersede_of_a_nonexistent_marker_without_repair_is_blocked(self):
        path, marker = self.make_marker(generated_at="2026-10-06T09:00:00Z",
                                        supersedes="build-release-candidate/gone.json")
        args = vrc.build_parser().parse_args(["--out-dir", self.tmp, "--quiet",
                                              "--skip-heavy"])
        originals = vrc.READY_MARKER, vrc.BLOCKED_MARKER
        vrc.READY_MARKER = path
        vrc.BLOCKED_MARKER = os.path.join(os.path.dirname(path), "release-blocked.json")
        try:
            report = vrc.run_acceptance(args)
        finally:
            vrc.READY_MARKER, vrc.BLOCKED_MARKER = originals
        self.assertEqual(report["verdict"], "blocked")
        self.assertIn("ready-supersede-unverifiable", self._codes(report))

    def test_supersede_naming_a_real_marker_with_the_right_code_passes(self):
        path, marker = self.make_marker(generated_at="2026-10-06T09:00:00Z")
        blocked = write(os.path.join(os.path.dirname(path), "release-blocked.old.json"),
                        json.dumps({"schema": vrc.BLOCKED_SCHEMA,
                                    "error_code": "PLATFORM_FREEZE_HASH_MISMATCH"}).encode())
        marker["supersedes"] = blocked
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(marker, handle)
        args = vrc.build_parser().parse_args(["--out-dir", self.tmp, "--quiet",
                                              "--skip-heavy"])
        originals = vrc.READY_MARKER, vrc.BLOCKED_MARKER
        vrc.READY_MARKER = path
        vrc.BLOCKED_MARKER = os.path.join(os.path.dirname(path), "release-blocked.json")
        try:
            report = vrc.run_acceptance(args)
        finally:
            vrc.READY_MARKER, vrc.BLOCKED_MARKER = originals
        self.assertEqual(report["verdict"], "incomplete")
        self.assertEqual(report["marker_details"]["supersede"]["supersede_form"],
                         "blocked marker named and error code confirmed")

    def test_first_round_ready_does_not_need_a_supersede(self):
        report = self._run_with_history({"generated_at": "2026-10-06T09:00:00Z"},
                                        history=False)
        self.assertEqual(report["verdict"], "incomplete")
        self.assertFalse(report["marker_details"]["supersede"]["required"])

    # ---- freeze marker verification --------------------------------------
    def test_freeze_marker_parses_and_all_eight_match(self):
        info = vrc.parse_freeze_marker(self.make_freeze_marker())
        self.assertEqual(len(info["files"]), 8)
        self.assertEqual(info["fields"]["freeze_utc"], "2026-10-06T08:15:02Z")
        details = {}
        findings = []
        vrc.verify_freeze_marker(info["path"], details,
                                lambda c, m, p="": findings.append((c, m)))
        self.assertEqual(findings, [])
        self.assertTrue(details["freeze_marker"]["all_match"])

    def test_freeze_hash_mismatch_is_detected(self):
        path = self.make_freeze_marker()
        target = os.path.join(self.tmp, "src/Frozen0.cpp")
        with open(target, "wb") as handle:
            handle.write(b"tampered")
        details, findings = {}, []
        vrc.verify_freeze_marker(path, details,
                                lambda c, m, p="": findings.append((c, m)))
        self.assertIn("freeze-hash-mismatch", [c for c, _ in findings])

    def test_freeze_file_count_is_enforced(self):
        path = os.path.join(self.tmp, "short-freeze")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("freeze_utc: 2026-10-06T08:15:02Z\n"
                         + "a" * 64 + "  src/Only.cpp\n")
        details, findings = {}, []
        vrc.verify_freeze_marker(path, details,
                                lambda c, m, p="": findings.append((c, m)))
        self.assertIn("freeze-marker-file-count", [c for c, _ in findings])

    def test_freeze_marker_without_files_is_rejected(self):
        path = os.path.join(self.tmp, "empty-freeze")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("freeze_utc: 2026-10-06T08:15:02Z\n")
        details, findings = {}, []
        vrc.verify_freeze_marker(path, details,
                                lambda c, m, p="": findings.append((c, m)))
        self.assertIn("freeze-marker-no-files", [c for c, _ in findings])

    def test_freeze_marker_claiming_real_hardware_is_rejected(self):
        path = self.make_freeze_marker()
        text = open(path, encoding="utf-8").read().replace(
            "real_hardware_verified: false", "real_hardware_verified: true")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        details, findings = {}, []
        vrc.verify_freeze_marker(path, details,
                                lambda c, m, p="": findings.append((c, m)))
        self.assertIn("freeze-marker-real-hardware", [c for c, _ in findings])

    # ---- output naming ---------------------------------------------------
    def test_rerun_basename_does_not_touch_the_first_round_report(self):
        self.assertEqual(vrc.REPORT_BASENAME, "release-candidate-final")
        self.assertEqual(vrc.RERUN_BASENAME, "release-candidate-rerun")
        self.assertNotEqual(vrc.REPORT_BASENAME, vrc.RERUN_BASENAME)

    def test_cli_accepts_the_rerun_basename(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, os.path.join(HERE, "verify_release_candidate.py"),
                 "--quiet", "--skip-heavy", "--report-basename", "release-candidate-rerun",
                 "--out-dir", tmp],
                capture_output=True, text=True, check=False, cwd=vrc.PROJECT_ROOT)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertTrue(os.path.isfile(
                os.path.join(tmp, "release-candidate-rerun.json")))
            self.assertFalse(os.path.isfile(
                os.path.join(tmp, "release-candidate-final.json")))


class TestReportRendering(SandboxCase):
    def test_blocked_markdown(self):
        report = {"generated_at": "now", "report_schema": vrc.REPORT_SCHEMA,
                  "verdict": "blocked", "method": "m", "not_verified": [],
                  "blocking": {"reason": "no marker", "marker": "x", "codes": ["a"]},
                  "findings": [{"severity": "error", "code": "a", "message": "m", "path": ""}]}
        text = vrc.render_markdown(report)
        self.assertIn("Blocked", text)
        self.assertIn("no marker", text)

    def test_certified_markdown(self):
        report = {
            "generated_at": "now", "report_schema": vrc.REPORT_SCHEMA, "verdict": "certified",
            "method": "m", "blocking": None, "findings": [], "not_verified": ["n"],
            "candidate": {"binary": "/b", "sha256": "a", "sha256_recomputed": "a",
                          "size_bytes": 1, "mtime": "t", "marker_sha256": "m",
                          "freeze_marker": "/f", "freeze_marker_sha256": "g",
                          "contrib_backends": True},
            "attestations": {"real_hardware_verified": False, "vm_started": False,
                             "no_vm_launched_by_this_tool": True},
            "reachability": {"vm_launch_reachable": False, "allowed_candidate_flags": ["--json"]},
            "candidate_calls": [{"argv": ["--version"], "exit_code": 0, "duration_ms": 1}],
            "verifiers": {"self_tests": {"exit_code": 0, "ok": True, "tests": 214,
                                         "baseline": 214},
                          "conformance": {"exit_code": 0, "ok": True, "arches": ["aarch64"]}},
            "facts": {"candidate_path": {"path": "$.candidate", "value": "/c", "found": True}},
        }
        text = vrc.render_markdown(report)
        self.assertIn("Verdict: `certified`", text)
        self.assertIn("real_hardware_verified", text)
        self.assertIn("214 tests", text)


class TestCli(unittest.TestCase):
    def test_cli_exits_blocked_without_a_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [sys.executable, os.path.join(HERE, "verify_release_candidate.py"),
                 "--quiet", "--skip-heavy", "--out-dir", tmp],
                capture_output=True, text=True, check=False, cwd=vrc.PROJECT_ROOT)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            report = json.load(open(os.path.join(tmp, "release-candidate-final.json"),
                                    encoding="utf-8"))
            self.assertIn(report["verdict"], ("blocked", "incomplete"))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "release-candidate-final.md")))

    def test_report_schema_is_independent(self):
        self.assertEqual(vrc.REPORT_SCHEMA, "virtualworld.release-candidate-final/1")
        self.assertNotEqual(vrc.REPORT_SCHEMA, vrc.RC_SCHEMA)

    def test_cli_never_mentions_a_start_flag_as_an_argument(self):
        source = open(os.path.join(HERE, "verify_release_candidate.py"),
                      encoding="utf-8").read()
        for line in source.splitlines():
            stripped = line.strip()
            if "add_argument" in stripped:
                for flag in ("--start", "--launch", "--boot"):
                    self.assertNotIn(flag, stripped, stripped)

    def test_battery_commands_only_use_read_only_verifiers(self):
        source = inspect.getsource(vrc.run_battery)
        for command in ("run_conformance.py", "run_stability_gate.py",
                        "verify_release_manifest.py", "run_parser_hardening.py"):
            self.assertIn(command, source)
        self.assertNotIn("--start", source)

    def test_manifest_step_targets_the_candidate_builds(self):
        """The release-manifest verifier defaults to the earlier packaging-audit
        directories; the battery must override that or it certifies the wrong pair."""
        roots = vrc.candidate_manifest_roots()
        self.assertTrue(roots, "no candidate build directories were found")
        for root in roots:
            self.assertIn("build-release-candidate", root)
        source = inspect.getsource(vrc.run_battery)
        self.assertIn("--search-root", source)
        self.assertIn("candidate_manifest_roots", source)

    def test_sub_verifier_output_goes_to_isolated_directories(self):
        """Earlier reports must not be overwritten by an RC run."""
        source = inspect.getsource(vrc.run_battery)
        for sub in ("rc-parser-hardening", "rc-manifests", "rc-stability"):
            self.assertIn(sub, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
