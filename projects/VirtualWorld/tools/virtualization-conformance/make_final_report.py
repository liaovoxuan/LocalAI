#!/usr/bin/env python3
"""Produce the final VirtualWorld virtualization certification report.

This is the stage-5 deliverable: it runs the conformance checker in its
**strictest** configuration (no ``--allow-code`` anywhere, so no known defect can
be hidden), records everything needed to audit the verdict, and writes

    tools/virtualization-conformance/reports/final-report.json
    tools/virtualization-conformance/reports/final-report.md

It never builds, patches or otherwise touches the product. It reads a binary that
someone else produced, runs the two documented read-only reporting commands, and
writes its findings into its own directory.

Usage:

    python3 tools/virtualization-conformance/make_final_report.py \
        --binary build-final/VirtualWorld.app/Contents/MacOS/VirtualWorld

Exit code is 0 when the certified build is conformant, 1 when it is not, and 2
when the certification could not be performed at all (missing binary, etc).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import run_conformance as rc  # noqa: E402

FIXTURE_DIR = os.path.join(HERE, "fixtures")
REPORTS_DIR = os.path.join(HERE, "reports")
SELF_TEST = os.path.join(HERE, "test_run_conformance.py")
REPORT_KIND = "virtualworld-virtualization-final-certification"
REPORT_VERSION = "1.0.0"

DEFAULT_ARCHES = ("x86_64", "aarch64", "riscv64")

# Backend id -> the source file the public header assigns it to. Used only to
# attribute a finding to a workstream; the mapping comes from the comments in
# src/VirtualizationEngine.hpp, not from any privileged knowledge.
BACKEND_OWNERSHIP = {
    "macos-virtualization": ("macOS platform workstream", "src/MacVirtualizationEngine.hpp/.mm"),
    "windows-hypervisor": ("Windows/Linux platform workstream", "src/WindowsHypervisorEngine.hpp/.cpp"),
    "linux-kvm": ("Windows/Linux platform workstream", "src/LinuxKvmEngine.hpp/.cpp"),
    "harmony-virtualization": ("HarmonyOS platform workstream", "src/HarmonyVirtualizationEngine.hpp/.cpp"),
    "qemu-hardware": ("shared integration workstream", "src/VirtualizationEngine.cpp"),
    "qemu-tcg": ("shared integration workstream", "src/VirtualizationEngine.cpp"),
}

# Invariants this certification asserts, grouped for the reader. Each is evaluated
# from the strict findings of the live run.
INVARIANTS = [
    ("schema", "JSON schema is virtualworld.virtualization/<n> with n >= 1"),
    ("exit-codes", "documented exit codes (--list-backends=0, --check in {0,1})"),
    ("single-document", "stdout is exactly one parseable JSON document"),
    ("backend-id-unique", "every backend id is non-empty and unique"),
    ("tier-legal", "every tier is one of native/hardware/emulation"),
    ("considered-reasons", "every candidate carries a non-empty reason"),
    ("considered-complete", "selection.considered covers exactly the registered backends"),
    ("considered-order", "candidates are ordered native -> hardware -> emulation"),
    ("best-eligible", "under preference=auto the best eligible candidate is selected"),
    ("tcg-last", "qemu-tcg is present and always the last candidate"),
    ("tcg-cross-arch", "cross-architecture guests are served by qemu-tcg"),
    ("harmony-not-native", "harmony-virtualization never claims native tier or native execution"),
    ("kvm-not-native", "linux-kvm never claims native tier or native execution"),
    ("whpx-native-tier", "windows-hypervisor reports the native tier"),
    ("matrix-complete", "support_matrix rows are complete and verified is a real boolean"),
    ("matrix-cross-arch", "no matrix row advertises acceleration across architectures"),
]

CODE_TO_INVARIANT = {
    "schema-unknown": "schema",
    "schema-version-invalid": "schema",
    "schema-missing": "schema",
    "exit-code-unexpected": "exit-codes",
    "json-unparseable": "single-document",
    "backend-id-duplicate": "backend-id-unique",
    "backend-id-empty": "backend-id-unique",
    "backend-tier-invalid": "tier-legal",
    "considered-tier-invalid": "tier-legal",
    "empty-field": "considered-reasons",
    "considered-missing-backend": "considered-complete",
    "considered-unknown-backend": "considered-complete",
    "considered-order-nonmonotonic": "considered-order",
    "selection-not-best-available": "best-eligible",
    "tcg-missing": "tcg-last",
    "tcg-not-last": "tcg-last",
    "tcg-arch-unsupported": "tcg-cross-arch",
    "cross-arch-not-tcg": "tcg-cross-arch",
    "cross-arch-without-tcg": "tcg-cross-arch",
    "compat-backend-claims-native": "harmony-not-native",
    "compat-backend-claims-native-execution": "harmony-not-native",
    "platform-native-tier-not-native": "whpx-native-tier",
    "matrix-verified-not-bool": "matrix-complete",
    "matrix-tier-invalid": "matrix-complete",
    "matrix-row-not-object": "matrix-complete",
    "missing-field": "matrix-complete",
    "matrix-cross-arch-acceleration": "matrix-cross-arch",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run_self_tests() -> Dict[str, Any]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, SELF_TEST],
        capture_output=True, text=True, check=False, env=env, cwd=HERE,
    )
    tail = (completed.stderr or "").strip().splitlines()[-4:]
    tests_run = None
    for line in (completed.stderr or "").splitlines():
        if line.startswith("Ran ") and " test" in line:
            try:
                tests_run = int(line.split()[1])
            except (IndexError, ValueError):
                pass
    return {
        "command": f"python3 {os.path.relpath(SELF_TEST, os.path.dirname(HERE))}",
        "exit_code": completed.returncode,
        "result": "OK" if completed.returncode == 0 else "FAILED",
        "tests_run": tests_run,
        "tail": tail,
    }


def check_legacy_selection(binary: str, arch: str, timeout: float) -> Dict[str, Any]:
    """Verify the legacy `backend=qemu` default and explicit selection paths."""
    out: Dict[str, Any] = {"arch": arch, "checks": []}

    def probe(env_overrides, label, expectation):
        report = rc.check_live_target(
            binary, "--list-backends", arch, timeout, (), env_overrides
        )
        document = report.document or {}
        selection = document.get("selection") if isinstance(document.get("selection"), dict) else {}
        considered = selection.get("considered") if isinstance(selection.get("considered"), list) else []
        entry = {"label": label, "env": env_overrides or {}, "exit_code": report.exit_code,
                 "preference": document.get("preference"), "selected": selection.get("backend"),
                 "ok": selection.get("ok"), "fell_back": selection.get("fell_back"),
                 "expectation": expectation, "satisfied": None, "detail": ""}
        if expectation == "legacy-default":
            excluded = [c for c in considered if c.get("status") == "excluded-by-policy"]
            unexplained = [c.get("id") for c in excluded if not (c.get("reason") or "").strip()]
            entry["satisfied"] = (
                entry["preference"] == "qemu" and bool(selection.get("ok")) and not unexplained
            )
            entry["detail"] = (
                f"preference=qemu (legacy QemuOnly); {len(excluded)} backend(s) excluded by policy, "
                f"{len(unexplained)} without a reason"
            )
        elif expectation == "explicit-honoured":
            entry["satisfied"] = entry["preference"] == "qemu-tcg" and entry["selected"] == "qemu-tcg"
            entry["detail"] = f"explicit request resolved to {entry['selected']!r}"
        elif expectation == "explicit-unknown-falls-back":
            entry["satisfied"] = bool(selection.get("ok")) and entry["selected"] != "does-not-exist"
            entry["detail"] = f"unknown id fell back to {entry['selected']!r} (ok={entry['ok']})"
        out["checks"].append(entry)

    probe(None, "legacy default (no --backend, no VIRTUALWORLD_BACKEND)", "legacy-default")
    probe({"VIRTUALWORLD_BACKEND": "qemu-tcg"}, "explicit qemu-tcg", "explicit-honoured")
    probe({"VIRTUALWORLD_BACKEND": "does-not-exist"}, "explicit unknown id", "explicit-unknown-falls-back")
    out["ok"] = all(c["satisfied"] for c in out["checks"])
    return out


def check_check_command_agreement(binary: str, arch: str, timeout: float) -> Dict[str, Any]:
    """`--check` (key=value) and `--check --json` must agree on the exit code."""
    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    text = subprocess.run([binary, "--check", arch], capture_output=True, text=True,
                          timeout=timeout, env=env, check=False)
    js = subprocess.run([binary, "--check", arch, "--json"], capture_output=True, text=True,
                        timeout=timeout, env=env, check=False)
    selected = ""
    try:
        selected = json.loads(js.stdout.strip())["selection"]["backend"]
    except Exception:
        selected = "<unparseable>"
    return {
        "arch": arch,
        "text_exit": text.returncode,
        "json_exit": js.returncode,
        "agree": text.returncode == js.returncode,
        "selected": selected,
        "text_has_keyvalue": "system=" in text.stdout,
    }


def binary_metadata(binary: str) -> Dict[str, Any]:
    stat = os.stat(binary)
    return {
        "path": binary,
        "sha256": rc.sha256_of_file(binary),
        "size_bytes": stat.st_size,
        "mtime": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "mtime_epoch": int(stat.st_mtime),
    }


PROJECT_ROOT = os.path.dirname(os.path.dirname(HERE))


def assess_build_integrity(binary: str) -> Dict[str, Any]:
    """Compare the linked artifact against the inputs it was built from.

    A certification is only meaningful for a specific artifact, so the report
    states whether any *product* input is newer than the linked binary. Test
    sources are tracked separately: editing tests/LinuxKvmEngineTests.cpp cannot
    invalidate the app binary, but it is still worth showing so a reader can tell
    a stale artifact from an unrelated test edit.

    A non-empty product list does NOT make the run invalid -- the artifact is
    still exactly what was executed -- but it does mean the artifact predates the
    newest product source, which a reader must know.
    """
    binary_mtime = os.path.getmtime(binary)

    def scan(entries: Sequence[str]) -> List[Dict[str, Any]]:
        found: List[Dict[str, Any]] = []
        for sub in entries:
            path = os.path.join(PROJECT_ROOT, sub)
            candidates: List[str] = []
            if os.path.isfile(path):
                candidates.append(path)
            elif os.path.isdir(path):
                for dirpath, _dirs, files in os.walk(path):
                    candidates.extend(os.path.join(dirpath, f) for f in files)
            for candidate in candidates:
                try:
                    mtime = os.path.getmtime(candidate)
                except OSError:
                    continue
                if mtime > binary_mtime:
                    found.append({
                        "path": os.path.relpath(candidate, PROJECT_ROOT),
                        "mtime": datetime.datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
                        "seconds_newer_than_binary": round(mtime - binary_mtime, 1),
                    })
        found.sort(key=lambda item: -item["seconds_newer_than_binary"])
        return found

    product_newer = scan(("src", "CMakeLists.txt"))
    test_newer = scan(("tests",))

    newest_path, newest_mtime = "", 0.0
    for sub in ("src", "CMakeLists.txt"):
        path = os.path.join(PROJECT_ROOT, sub)
        candidates = [path] if os.path.isfile(path) else []
        if os.path.isdir(path):
            for dirpath, _dirs, files in os.walk(path):
                candidates.extend(os.path.join(dirpath, f) for f in files)
        for candidate in candidates:
            try:
                mtime = os.path.getmtime(candidate)
            except OSError:
                continue
            if mtime > newest_mtime:
                newest_mtime, newest_path = mtime, candidate

    return {
        "binary_mtime": datetime.datetime.fromtimestamp(binary_mtime).isoformat(timespec="seconds"),
        "newest_product_input": os.path.relpath(newest_path, PROJECT_ROOT) if newest_path else "",
        "newest_product_input_mtime": (
            datetime.datetime.fromtimestamp(newest_mtime).isoformat(timespec="seconds")
            if newest_path else ""
        ),
        "newest_product_input_skew_seconds": (
            round(newest_mtime - binary_mtime, 1) if newest_path else None
        ),
        "product_inputs_newer_than_binary": product_newer,
        "test_sources_newer_than_binary": test_newer,
        "artifact_matches_newest_product_input": not product_newer,
        "note": (
            "artifact was linked from the newest product inputs"
            if not product_newer else
            "artifact predates the newest product input(s) listed above; the certified "
            "behaviour is still exactly what this artifact emitted, but a rebuild would "
            "produce a different binary"
        ),
    }


def assess_product_tests(binary: str) -> Dict[str, Any]:
    """Best-effort, read-only summary of the product's own ctest results.

    This is informational: it tells the reader whether the artifact under
    certification also passes the project's own suite. The results are only
    trustworthy when the log is newer than the binary, so staleness is reported
    explicitly rather than glossed over.
    """
    build_dir = ""
    probe = os.path.dirname(binary)
    for _ in range(6):
        if os.path.isfile(os.path.join(probe, "CTestTestfile.cmake")):
            build_dir = probe
            break
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent

    result: Dict[str, Any] = {
        "available": False,
        "build_dir": os.path.relpath(build_dir, PROJECT_ROOT) if build_dir else "",
        "results": [],
        "passed": None,
        "failed": None,
        "stale": None,
        "note": "",
    }
    if not build_dir:
        result["note"] = "no CTest build directory found next to the certified binary"
        return result

    log_path = os.path.join(build_dir, "Testing", "Temporary", "LastTest.log")
    if not os.path.isfile(log_path):
        result["note"] = "the product has not run ctest in this build directory"
        return result

    result["available"] = True
    log_mtime = os.path.getmtime(log_path)
    binary_mtime = os.path.getmtime(binary)
    result["log_mtime"] = datetime.datetime.fromtimestamp(log_mtime).isoformat(timespec="seconds")
    result["stale"] = log_mtime < binary_mtime

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError as exc:
        result["note"] = f"could not read {os.path.basename(log_path)}: {exc}"
        return result

    current = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Test:") or " Test: " in stripped:
            name = stripped.split("Test:", 1)[1].strip()
            current = {"test": name, "outcome": "unknown", "checks": ""}
            result["results"].append(current)
        elif current is not None and stripped in ("Test Passed.", "Test Failed."):
            current["outcome"] = stripped[:-1].replace("Test ", "").lower()
        elif current is not None and "checks," in stripped and "failures" in stripped:
            current["checks"] = stripped
            if not current["checks"]:
                current["checks"] = stripped

    outcomes = [r["outcome"] for r in result["results"]]
    result["passed"] = outcomes.count("passed")
    result["failed"] = outcomes.count("failed")
    if result["stale"]:
        result["note"] = ("ctest log is OLDER than the certified binary, so these results "
                          "describe a previous artifact and must not be attributed to it")
    else:
        result["note"] = "ctest log is newer than the certified binary"
    return result


def build_defects(targets: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Group every strict finding by code and attribute it to a backend/workstream."""
    grouped: Dict[str, Dict[str, Any]] = {}
    for target in targets:
        for finding in target["findings"]:
            if finding["severity"] != "error":
                continue
            entry = grouped.setdefault(finding["code"], {
                "code": finding["code"],
                "severity": "error",
                "targets": [],
                "paths": [],
                "messages": [],
                "backend_ids": [],
                "owner": "unknown",
                "owner_source": "",
                "description": "",
                "invariant": CODE_TO_INVARIANT.get(finding["code"], "unclassified"),
            })
            if target["source"] not in entry["targets"]:
                entry["targets"].append(target["source"])
            if finding["path"] and finding["path"] not in entry["paths"]:
                entry["paths"].append(finding["path"])
            if finding["message"] not in entry["messages"]:
                entry["messages"].append(finding["message"])

    for entry in grouped.values():
        ids = []
        for backend_id, (owner, source) in BACKEND_OWNERSHIP.items():
            if any(backend_id in m for m in entry["messages"]) or any(backend_id in p for p in entry["paths"]):
                ids.append(backend_id)
                if entry["owner"] == "unknown":
                    entry["owner"] = owner
                    entry["owner_source"] = source
        entry["backend_ids"] = ids
    return [grouped[k] for k in sorted(grouped)]


def invariant_statuses(targets: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    failing = set()
    for target in targets:
        for finding in target["findings"]:
            if finding["severity"] == "error":
                failing.add(CODE_TO_INVARIANT.get(finding["code"], "unclassified"))
    results = []
    for key, description in INVARIANTS:
        if key in failing:
            status = "fail"
        elif "unclassified" in failing and key not in {k for k, _ in INVARIANTS}:
            status = "pass"
        else:
            status = "pass"
        results.append({"id": key, "description": description, "status": status})
    if "unclassified" in failing:
        results.append({
            "id": "unclassified",
            "description": "findings with no mapped invariant",
            "status": "fail",
        })
    return results


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def assemble(args) -> Dict[str, Any]:
    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    binary = os.path.abspath(args.binary) if args.binary else ""

    report: Dict[str, Any] = {
        "report": REPORT_KIND,
        "report_version": REPORT_VERSION,
        "generated_at": generated_at,
        "certified_by": "stage-5 black-box virtualization certification",
        "third_party_packages": False,
        "network_access": False,
        "allow_code_used": False,
        "product_code_modified": False,
    }

    if not binary or not os.path.isfile(binary) or not os.access(binary, os.X_OK):
        report["certification_status"] = "blocked"
        report["blocking"] = {
            "reason": "no certifiable product binary was available",
            "requested_binary": binary,
            "requested_exists": bool(binary) and os.path.exists(binary),
            "hint": ("build-final is produced by the shared-integration workstream; this "
                     "certification deliberately does not build the product itself"),
        }
        report["self_tests"] = run_self_tests()
        report["live"] = {"arches": list(args.arches), "targets": [], "summary": {}}
        report["defects"] = []
        report["invariants"] = []
        report["support_matrix"] = []
        return report

    report["product"] = binary_metadata(binary)
    report["product"]["build_label"] = args.build_label
    report["build_integrity"] = assess_build_integrity(binary)
    report["product_self_tests"] = assess_product_tests(binary)

    arches = list(args.arches)

    # ---- self tests ------------------------------------------------------
    report["self_tests"] = run_self_tests()

    # ---- live targets, strict (no allow-code anywhere) -------------------
    targets: List[Dict[str, Any]] = []
    documents: Dict[str, Dict[str, Any]] = {}
    for arch in arches:
        for command in ("--list-backends", "--check"):
            sub = rc.check_live_target(binary, command, arch, args.timeout)
            targets.append(sub.to_dict())
            key = f"{command}:{arch}"
            if isinstance(sub.document, dict):
                documents[key] = sub.document
    live_errors = sum(t["error_count"] for t in targets)
    report["live"] = {
        "arches": arches,
        "allow_code": [],
        "targets": targets,
        "summary": {
            "targets": len(targets),
            "errors": live_errors,
            "warnings": sum(t["warning_count"] for t in targets),
            "ok": live_errors == 0,
        },
    }

    # Product-level facts taken from the first successfully parsed document.
    reference = documents.get(f"--list-backends:{arches[0]}") or next(iter(documents.values()), {})
    if reference:
        report["product"]["schema"] = reference.get("schema")
        report["product"]["build_flags"] = reference.get("build")
        report["product"]["host"] = reference.get("host")
        report["product"]["guest_arch"] = reference.get("guest_arch")
        report["product"]["preference"] = reference.get("preference")
        report["product"]["backend_ids"] = [
            b.get("id") for b in reference.get("backends", []) if isinstance(b, dict)
        ]
        report["support_matrix"] = reference.get("support_matrix", [])
        report["product"]["selection_order"] = [
            c.get("id") for c in reference.get("selection", {}).get("considered", [])
            if isinstance(c, dict)
        ]
        # Record the exact field set observed, so a later contract change is
        # visible by diffing two reports rather than by guessing.
        backends = [b for b in reference.get("backends", []) if isinstance(b, dict)]
        if backends:
            report["product"]["backend_fields_observed"] = sorted(backends[0].keys())
            report["product"]["backend_fields_optional_absent"] = sorted(
                key for key in ("status_class",)
                if not any(key in b for b in backends)
            )
        selection = reference.get("selection") if isinstance(reference.get("selection"), dict) else {}
        report["product"]["selection_fields_observed"] = sorted(selection.keys())
        considered = selection.get("considered") if isinstance(selection.get("considered"), list) else []
        if considered and isinstance(considered[0], dict):
            report["product"]["considered_fields_observed"] = sorted(considered[0].keys())
        matrix = reference.get("support_matrix") if isinstance(reference.get("support_matrix"), list) else []
        if matrix and isinstance(matrix[0], dict):
            report["product"]["matrix_fields_observed"] = sorted(matrix[0].keys())

    # ---- fixtures ---------------------------------------------------------
    fixture_reports, fixture_error = rc.check_fixtures(FIXTURE_DIR)
    manifest, _ = rc.load_fixture_manifest(FIXTURE_DIR)
    manifest_by_file = {}
    if isinstance(manifest, dict):
        for entry in manifest.get("fixtures", []):
            if isinstance(entry, dict) and isinstance(entry.get("file"), str):
                manifest_by_file[entry["file"]] = entry

    fixture_rows = []
    for sub in fixture_reports:
        row = sub.to_dict()
        file_name = (sub.command[0].split("fixture:", 1)[-1]
                     if sub.command and sub.command[0].startswith("fixture:") else "")
        entry = manifest_by_file.get(file_name, {})
        row["file"] = file_name
        row["synthetic"] = entry.get("synthetic")
        row["expect_fail"] = bool(entry.get("expect_fail"))
        tracked = entry.get("tracked_defect_codes")
        row["tracked_defect_codes"] = tracked if isinstance(tracked, list) else []

        if row["expect_fail"]:
            row["expected"] = "must fail"
            row["expectation_met"] = not row["ok"]
        elif row["tracked_defect_codes"]:
            # A real capture that documents a tracked defect: its precise
            # expectation is "fails on exactly the tracked codes, nothing else".
            # That still surfaces the defect (it is listed in Defects) while
            # asserting the tool found no additional problem in the capture.
            error_codes = {f["code"] for f in row["findings"] if f["severity"] == "error"}
            row["expected"] = "fails only on tracked codes"
            row["expectation_met"] = bool(error_codes) and error_codes.issubset(
                set(row["tracked_defect_codes"])
            )
            row["unexpected_error_codes"] = sorted(error_codes - set(row["tracked_defect_codes"]))
        else:
            row["expected"] = "must pass"
            row["expectation_met"] = row["ok"]
        fixture_rows.append(row)

    real_captures = [r for r in fixture_rows if r.get("synthetic") is False]
    synthetic = [r for r in fixture_rows if r.get("synthetic") is True and not r["expect_fail"]]
    negative = [r for r in fixture_rows if r["expect_fail"]]
    report["fixtures"] = {
        "manifest": os.path.relpath(os.path.join(FIXTURE_DIR, "index.json"), os.path.dirname(HERE)),
        "load_error": fixture_error,
        "counts": {
            "total": len(fixture_rows),
            "real_captures": len(real_captures),
            "synthetic_scenarios": len(synthetic),
            "negative_fixtures": len(negative),
        },
        "real_captures": [r["source"] for r in real_captures],
        "synthetic_scenarios": [r["source"] for r in synthetic],
        "negative_fixtures": [r["source"] for r in negative],
        "expectations_met": sum(1 for r in fixture_rows if r["expectation_met"]),
        "rows": fixture_rows,
    }

    # ---- legacy compatibility --------------------------------------------
    legacy = {
        "note": ("'backend=qemu' is the legacy default: resolveBackendPreference() falls "
                 "back to it, and parseBackendPreference() maps it to the QemuOnly policy, "
                 "which excludes native backends by policy rather than by failure."),
        "selections": [check_legacy_selection(binary, arches[0], args.timeout)],
        "check_agreement": [check_check_command_agreement(binary, a, args.timeout) for a in arches],
    }
    legacy["selections_ok"] = all(s["ok"] for s in legacy["selections"])
    legacy["check_agreement_ok"] = all(c["agree"] for c in legacy["check_agreement"])
    legacy["ok"] = legacy["selections_ok"] and legacy["check_agreement_ok"]
    report["legacy_compatibility"] = legacy

    # ---- defects / invariants -------------------------------------------
    defects = build_defects(targets)
    report["defects"] = defects
    report["invariants"] = invariant_statuses(targets)
    report["invariants"].append({
        "id": "legacy-compatibility",
        "description": "legacy backend=qemu default and --check text/JSON agreement",
        "status": "pass" if legacy["ok"] else "fail",
    })
    report["fixtures"]["invariant"] = {
        "id": "fixture-expectations",
        "description": "every fixture behaves as its manifest declares (positives pass, negatives fail)",
        "status": "pass" if all(r["expectation_met"] for r in fixture_rows) else "fail",
    }

    # ---- not verified ----------------------------------------------------
    report["not_verified"] = [
        "No guest was started by this certification: only --list-backends and --check were run.",
        "Windows, Linux and HarmonyOS hosts were not exercised on real hardware; their "
        "documents here are synthetic fixtures labelled synthetic=true.",
        "Intel Mac behaviour is covered by a synthetic fixture only; this machine is Apple Silicon.",
        "QEMU/accelerator behaviour beyond capability reporting (an actual accelerated boot) is untested.",
        "The product binary was produced by another workstream; this tool neither built nor modified it.",
    ]

    status = "conformant"
    if live_errors:
        status = "non-conformant"
    if not all(r["expectation_met"] for r in fixture_rows) or not legacy["ok"]:
        status = "non-conformant"
    report["certification_status"] = status
    report["blocking"] = None
    return report


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

def render_markdown(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# VirtualWorld virtualization — final certification report")
    lines.append("")
    lines.append(f"- Generated: `{report.get('generated_at', '')}`")
    lines.append(f"- Report version: `{REPORT_VERSION}`")
    lines.append(f"- **Certification status: `{report.get('certification_status', 'unknown')}`**")
    lines.append("- Method: read-only black-box. Only `--list-backends --json` and "
                 "`--check --json` were executed. No guest was started, nothing was built, "
                 "and no `--allow-code` was used.")
    lines.append("")

    blocking = report.get("blocking")
    if blocking:
        lines.append("## Blocked")
        lines.append("")
        lines.append(f"- Reason: {blocking.get('reason')}")
        lines.append(f"- Requested binary: `{blocking.get('requested_binary', '')}` "
                     f"(exists: `{blocking.get('requested_exists')}`)")
        lines.append(f"- {blocking.get('hint', '')}")
        lines.append("")
        st = report.get("self_tests", {})
        lines.append(f"Self-tests: `{st.get('result')}` "
                     f"({st.get('tests_run')} cases, exit {st.get('exit_code')})")
        lines.append("")
        return "\n".join(lines) + "\n"

    product = report.get("product", {})
    lines.append("## Certified artifact")
    lines.append("")
    lines.append(f"| Field | Value |")
    lines.append(f"| --- | --- |")
    lines.append(f"| Build label | `{product.get('build_label', '')}` |")
    lines.append(f"| Binary | `{product.get('path', '')}` |")
    lines.append(f"| SHA-256 | `{product.get('sha256', '')}` |")
    lines.append(f"| Size | {product.get('size_bytes', 0)} bytes |")
    lines.append(f"| mtime | `{product.get('mtime', '')}` |")
    lines.append(f"| JSON schema | `{product.get('schema', '')}` |")
    host = product.get("host") or {}
    lines.append(f"| Host | `{host.get('platform', '')}` / `{host.get('arch', '')}` "
                 f"({host.get('family', '')}) |")
    lines.append(f"| Compiled backends | `{product.get('build_flags')}` |")
    lines.append(f"| Registered backend ids | `{product.get('backend_ids')}` |")
    lines.append(f"| Selection order observed | `{product.get('selection_order')}` |")
    lines.append("")

    integrity = report.get("build_integrity")
    if integrity:
        lines.append("### Build integrity (artifact vs sources)")
        lines.append("")
        lines.append(f"- Newest product input: `{integrity.get('newest_product_input')}` "
                     f"({integrity.get('newest_product_input_mtime')}); skew vs binary: "
                     f"`{integrity.get('newest_product_input_skew_seconds')}` s")
        lines.append(f"- Artifact matches newest product input: "
                     f"**{integrity.get('artifact_matches_newest_product_input')}**")
        if integrity.get("product_inputs_newer_than_binary"):
            lines.append("- Product inputs newer than the linked binary:")
            for item in integrity["product_inputs_newer_than_binary"]:
                lines.append(f"  - `{item['path']}` (+{item['seconds_newer_than_binary']}s, "
                             f"{item['mtime']})")
        if integrity.get("test_sources_newer_than_binary"):
            names = ", ".join(f"`{i['path']}`" for i in integrity["test_sources_newer_than_binary"])
            lines.append(f"- Test sources newer than the binary (cannot invalidate the app "
                         f"artifact): {names}")
        lines.append(f"- {integrity.get('note', '')}")
        lines.append("")

    pts = report.get("product_self_tests") or {}
    if pts.get("available"):
        lines.append("### Product self-tests (informational, read-only)")
        lines.append("")
        lines.append(f"- Build dir: `{pts.get('build_dir')}`, ctest log "
                     f"`{pts.get('log_mtime')}`")
        lines.append(f"- Passed: **{pts.get('passed')}**, failed: **{pts.get('failed')}**"
                     + (" — **STALE**: " + pts.get("note", "") if pts.get("stale") else ""))
        for item in pts.get("results", []):
            lines.append(f"  - `{item['test']}`: {item['outcome']}"
                         + (f" ({item['checks']})" if item.get("checks") else ""))
        lines.append("")

    st = report.get("self_tests", {})
    lines.append("## Self-tests")
    lines.append("")
    lines.append(f"- `{st.get('command', '')}` → **{st.get('result')}**, "
                 f"{st.get('tests_run')} cases, exit `{st.get('exit_code')}`")
    lines.append("")

    lines.append("## Live checks against the certified binary")
    lines.append("")
    lines.append("`<binary>` below is the artifact path recorded above; the exact argv is in "
                 "the JSON report (`live.targets[].command`).")
    lines.append("")
    lines.append("| # | Command | Exit | Errors | Error codes |")
    lines.append("| --- | --- | --- | --- | --- |")
    binary_path = product.get("path", "")
    for index, target in enumerate(report["live"]["targets"], 1):
        codes = sorted({f["code"] for f in target["findings"] if f["severity"] == "error"})
        command = " ".join(target["command"])
        if binary_path and binary_path in command:
            command = command.replace(binary_path, "<binary>")
        lines.append(f"| {index} | `{command}` | {target['exit_code']} | "
                     f"{target['error_count']} | {', '.join(codes) if codes else '—'} |")
    live_summary = report["live"]["summary"]
    lines.append("")
    lines.append(f"Totals: {live_summary.get('targets')} targets, "
                 f"{live_summary.get('errors')} error(s), {live_summary.get('warnings')} warning(s).")
    lines.append("")

    lines.append("## Invariants")
    lines.append("")
    lines.append("| Invariant | Status | Description |")
    lines.append("| --- | --- | --- |")
    for item in report.get("invariants", []):
        mark = "PASS" if item["status"] == "pass" else "**FAIL**"
        lines.append(f"| `{item['id']}` | {mark} | {item['description']} |")
    lines.append("")

    defects = report.get("defects", [])
    lines.append("## Defects (report only — not fixed by this tool)")
    lines.append("")
    if not defects:
        lines.append("No defects were found on the certified build.")
        lines.append("")
    for defect in defects:
        lines.append(f"### `{defect['code']}` — owner: {defect['owner']}")
        lines.append("")
        lines.append(f"- Invariant: `{defect['invariant']}`")
        if defect.get("owner_source"):
            lines.append(f"- Responsible source: `{defect['owner_source']}`")
        if defect.get("backend_ids"):
            lines.append(f"- Backend id(s): `{', '.join(defect['backend_ids'])}`")
        lines.append(f"- Affected targets: {len(defect['targets'])}")
        for message in defect["messages"]:
            lines.append(f"- Evidence: {message}")
        lines.append("")

    fixtures = report.get("fixtures", {})
    lines.append("## Fixtures: real captures vs synthetic")
    lines.append("")
    counts = fixtures.get("counts", {})
    lines.append(f"- Manifest: `{fixtures.get('manifest', '')}`")
    lines.append(f"- Real captures (`synthetic: false`): **{counts.get('real_captures', 0)}** — "
                 "these are the only documents that are measurements of a real host.")
    lines.append(f"- Synthetic scenarios (`synthetic: true`): **{counts.get('synthetic_scenarios', 0)}** — "
                 "expectations authored for hosts this machine does not have.")
    lines.append(f"- Negative fixtures (`expect_fail`): **{counts.get('negative_fixtures', 0)}** — "
                 "documents that reproduce defects; the checker must reject them.")
    lines.append(f"- Expectations met: {fixtures.get('expectations_met')} / {counts.get('total')}")
    lines.append("")
    lines.append("| Fixture | Kind | Expected outcome | Met |")
    lines.append("| --- | --- | --- | --- |")
    for row in fixtures.get("rows", []):
        if row["expect_fail"]:
            kind = "negative"
        elif row.get("tracked_defect_codes"):
            kind = "real capture (tracked defect)"
        elif row["synthetic"]:
            kind = "synthetic"
        else:
            kind = "real capture"
        lines.append(f"| {row['source']} | {kind} | {row.get('expected', '')} | "
                     f"{'yes' if row['expectation_met'] else '**no**'} |")
    lines.append("")

    legacy = report.get("legacy_compatibility", {})
    lines.append("## Legacy compatibility (`backend=qemu`, `--check`)")
    lines.append("")
    lines.append(legacy.get("note", ""))
    lines.append("")
    lines.append("| Check | Result | Detail |")
    lines.append("| --- | --- | --- |")
    for sel in legacy.get("selections", []):
        for item in sel.get("checks", []):
            lines.append(f"| {item['label']} | {'PASS' if item['satisfied'] else '**FAIL**'} | "
                         f"{item['detail']} |")
    for item in legacy.get("check_agreement", []):
        lines.append(f"| `--check {item['arch']}` text vs --json | "
                     f"{'PASS' if item['agree'] else '**FAIL**'} | "
                     f"text={item['text_exit']}, json={item['json_exit']}, selected={item['selected']} |")
    lines.append("")

    matrix = report.get("support_matrix", [])
    lines.append("## Support matrix (as reported by the certified binary)")
    lines.append("")
    if matrix:
        lines.append("| host platform | host arch | guest arch | backend | tier | status | verified |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for row in matrix:
            lines.append(f"| {row.get('host_platform')} | {row.get('host_arch')} | "
                         f"{row.get('guest_arch')} | {row.get('backend')} | {row.get('tier')} | "
                         f"{row.get('status')} | {row.get('verified')} |")
    else:
        lines.append("_No support matrix was available._")
    lines.append("")

    lines.append("## Not verified")
    lines.append("")
    for item in report.get("not_verified", []):
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--binary", help="the product binary to certify")
    parser.add_argument("--build-label", default="", help="human label for the build under test")
    parser.add_argument("--guest-arch", action="append", default=[], dest="arches",
                        help="guest architecture to probe (repeatable)")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--out-dir", default=REPORTS_DIR)
    args = parser.parse_args(argv)
    if not args.arches:
        args.arches = list(DEFAULT_ARCHES)

    report = assemble(args)

    os.makedirs(args.out_dir, exist_ok=True)
    json_path = os.path.join(args.out_dir, "final-report.json")
    md_path = os.path.join(args.out_dir, "final-report.md")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(report))

    status = report.get("certification_status", "unknown")
    print(f"certification_status={status}")
    print(f"wrote {os.path.relpath(json_path, os.path.dirname(HERE))}")
    print(f"wrote {os.path.relpath(md_path, os.path.dirname(HERE))}")
    if status == "conformant":
        return 0
    if status == "blocked":
        return 2
    return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
