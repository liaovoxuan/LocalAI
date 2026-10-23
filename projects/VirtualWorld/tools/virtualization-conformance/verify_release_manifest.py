#!/usr/bin/env python3
"""Independent black-box certification of VirtualWorld release manifests.

What this verifies
------------------
`schema virtualworld.release/1` manifests describe a packaged VirtualWorld
bundle: the main program, every bundled QEMU binary, resource counts, which
backends were compiled in, the contrib toggle, and the packaging gate result.
This tool re-derives all of that from the filesystem and from the same build's
own executable, instead of trusting the document.

Checks performed, per manifest
------------------------------
* schema identity, required fields and JSON types;
* the honesty flags `verified` and `real_hardware_verified` must be exactly false;
* `generated_at` is an ISO-8601 `...Z` UTC timestamp;
* `host.os` / `host.arch` are plausible and agree with this machine when the
  manifest was produced locally;
* `backends.*` and `contrib_backends` are real booleans, and `contrib_backends`
  agrees with the two contrib backends (`linux_kvm`, `harmony`);
* every path is safe: QEMU entries must be bundle-relative, free of `..`
  traversal, free of duplicates, must not be absolute, and must not escape the
  bundle through a symlink;
* the main program path must resolve inside the manifest's own build tree (an
  absolute path is how the generator writes it, but it may never point at a
  different build);
* every recorded `size` and `sha256` is recomputed from the file and compared;
* `firmware_count` / `support_count` are independently recounted from the bundle;
* the embedded gate is internally consistent: `failure_count == len(failures)`,
  a `passed` gate has no failing check and no failures, every check has a real
  boolean `ok`/`skipped`;
* cross-checks against the same build: `CMakeCache.txt`'s contrib toggle and the
  executable's own `--list-backends --json` `build` block.

If a build has no runnable same-host executable the cross-check is recorded as
**skipped**; this tool never substitutes another build's binary.

Safety
------
Only `--list-backends --json` is ever executed (via
`run_conformance.run_binary`, which refuses anything else), so no VM command is
reachable. Nothing outside `tools/virtualization-conformance/reports/` is
written.

Exit codes: 0 = all discovered manifests certified, 1 = findings, 2 = blocked
(ambiguous discovery, or no manifest found).
"""

from __future__ import annotations

import argparse
import copy
import datetime
import hashlib
import json
import os
import random
import re
import shutil
import stat
import sys
import tempfile
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import run_conformance as rc  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(HERE))
REPORTS_DIR = os.path.join(HERE, "reports")
REPORT_JSON = os.path.join(REPORTS_DIR, "release-manifest-report.json")
REPORT_MD = os.path.join(REPORTS_DIR, "release-manifest-report.md")
FAILURES_DIR = os.path.join(REPORTS_DIR, "release-manifest-mutations")

MANIFEST_NAME = "virtualization-release-manifest.json"
SCHEMA = "virtualworld.release/1"
GATE_STATUSES = ("passed", "failed", "not_run")
HOST_OSES = ("Darwin", "Linux", "Windows")
BACKEND_KEYS = ("macos", "windows", "linux_kvm", "harmony")
# The two backends gated by VIRTUALWORLD_INCLUDE_CONTRIB_BACKENDS.
CONTRIB_BACKEND_KEYS = ("linux_kvm", "harmony")
FIRMWARE_SUFFIXES = (".bin", ".rom", ".edk2", ".fd", ".nvram")
TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_SEED = 20261005
# Search roots for auto-discovery, relative to the project root.
DEFAULT_SEARCH_ROOTS = ("build-package-audit", "build-package-audit-nocontrib")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_manifest_paths(search_roots: Sequence[str]) -> List[str]:
    """Find every manifest under the given roots (a root may be a file or dir)."""
    found: List[str] = []
    for root in search_roots:
        path = root if os.path.isabs(root) else os.path.join(PROJECT_ROOT, root)
        if os.path.isfile(path) and path.endswith(".json"):
            found.append(os.path.abspath(path))
        elif os.path.isdir(path):
            direct = os.path.join(path, MANIFEST_NAME)
            if os.path.isfile(direct):
                found.append(os.path.abspath(direct))
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                if MANIFEST_NAME in filenames:
                    candidate = os.path.abspath(os.path.join(dirpath, MANIFEST_NAME))
                    if candidate not in found:
                        found.append(candidate)
    return sorted(set(found))


def classify_candidates(paths: Sequence[str]) -> Dict[str, Any]:
    """Group candidates into contrib ON / OFF, reporting anything ambiguous.

    A candidate is only classified when it parses AND declares a real boolean
    `contrib_backends`. Anything else is ambiguous and must be resolved by an
    explicit argument rather than guessed at.
    """
    on: List[Dict[str, Any]] = []
    off: List[Dict[str, Any]] = []
    ambiguous: List[Dict[str, Any]] = []

    for path in paths:
        entry: Dict[str, Any] = {"path": path}
        try:
            with open(path, "r", encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            entry["problem"] = f"unreadable: {exc}"
            ambiguous.append(entry)
            continue
        entry["schema"] = document.get("schema") if isinstance(document, dict) else None
        flag = document.get("contrib_backends") if isinstance(document, dict) else None
        entry["contrib_backends"] = flag
        if not rc.is_strict_bool(flag):
            entry["problem"] = ("contrib_backends is missing or not a JSON boolean, so the "
                               "contrib ON/OFF role cannot be inferred")
            ambiguous.append(entry)
        elif flag:
            on.append(entry)
        else:
            off.append(entry)

    problems: List[str] = []
    if len(on) > 1:
        problems.append(f"{len(on)} candidate(s) claim contrib_backends=true")
    if len(off) > 1:
        problems.append(f"{len(off)} candidate(s) claim contrib_backends=false")
    if ambiguous:
        problems.append(f"{len(ambiguous)} candidate(s) could not be classified")

    return {
        "on": on,
        "off": off,
        "ambiguous": ambiguous,
        "problems": problems,
        "resolution_required": bool(problems) or not on or not off,
    }


# ---------------------------------------------------------------------------
# Path safety helpers
# ---------------------------------------------------------------------------

def _as_dict(value: Any) -> Dict[str, Any]:
    """Return `value` when it really is a mapping, else an empty mapping.

    `(x or {}).get(...)` is unsafe: a truthy non-mapping such as the JSON value
    `true` passes through the `or` and then raises AttributeError from a
    validator that must never raise on untrusted input.
    """
    return value if isinstance(value, dict) else {}


def is_safe_relative_path(value: str) -> Tuple[bool, str]:
    """A manifest path must be relative, normalised and traversal-free."""
    if not isinstance(value, str) or not value.strip():
        return False, "path is empty"
    if os.path.isabs(value) or value.startswith("~"):
        return False, "path is absolute"
    if re.match(r"^[A-Za-z]:[\\/]", value):
        return False, "path looks like a Windows absolute path"
    normalised = value.replace("\\", "/")
    parts = [p for p in normalised.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return False, "path contains '..' traversal"
    return True, ""


def _safe_realpath(path: str) -> str:
    """`os.path.realpath` that never raises.

    Untrusted paths can contain an embedded NUL byte, exceed the platform's
    component limit, or hold undecodable bytes; `realpath` turns each of those
    into ValueError/OSError. This helper returns "" instead, so callers can
    report an unsafe path rather than crash on it.
    """
    try:
        return os.path.realpath(path)
    except (ValueError, OSError):
        return ""


def escapes_root(root: str, candidate: str) -> bool:
    """True when `candidate` resolves outside `root` (through symlinks too).

    Fails closed: a path that cannot be resolved at all (an embedded NUL byte,
    an over-long component, undecodable bytes) can never be *proven* to stay
    inside `root`, so it is reported as escaping rather than raising.
    """
    real_root = os.path.realpath(root)
    try:
        real_candidate = os.path.realpath(candidate)
    except (ValueError, OSError):
        return True
    if real_candidate == real_root:
        return False
    return not real_candidate.startswith(real_root + os.sep)


def resolve_bundle_root(manifest_path: str, document: Dict[str, Any]) -> Tuple[str, str]:
    """Locate the .app bundle the manifest describes."""
    manifest_dir = os.path.dirname(os.path.abspath(manifest_path))
    candidates = [
        os.path.join(manifest_dir, "VirtualWorld.app"),
        manifest_dir,
    ]
    main_path = _as_dict(document.get("main_program")).get("path")
    if isinstance(main_path, str) and main_path:
        # .../VirtualWorld.app/Contents/MacOS/VirtualWorld -> .../VirtualWorld.app
        probe = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(main_path))))
        candidates.append(probe)
    for candidate in candidates:
        if os.path.isdir(os.path.join(candidate, "Contents")):
            return os.path.abspath(candidate), f"resolved from {os.path.basename(candidate)}"
    return "", "no bundle root containing Contents/ was found"


def count_firmware(bundle_root: str) -> int:
    """Recount firmware the way the generator globs it.

    Two details matter and both were got wrong by an earlier version of this
    tool, producing false `firmware-count-mismatch` findings:

    * **Scope.** The generator globs ``<runtime>/qemu/*/*.bin`` and friends, i.e.
      only under ``runtime/qemu``. Walking all of ``runtime`` also picks up
      unrelated blobs such as ``runtime/system-software/roms/*``.
    * **Case.** CMake's glob matching is case-sensitive on macOS, so ``*.bin``
      does not match ``HIGH0B.BIN``. A case-insensitive suffix test over-counts.

    Replicate both exactly; the manifest is the thing under test, not our
    preferred spelling of the rule.
    """
    qemu_root = os.path.join(bundle_root, "Contents", "Resources", "runtime", "qemu")
    if not os.path.isdir(qemu_root):
        return 0
    total = 0
    for _dirpath, _dirs, files in os.walk(qemu_root):
        for name in files:
            if name.endswith(FIRMWARE_SUFFIXES):
                total += 1
    return total


def count_support(bundle_root: str) -> int:
    support = os.path.join(bundle_root, "Contents", "Resources", "support")
    if not os.path.isdir(support):
        return 0
    total = 0
    for _dirpath, _dirs, files in os.walk(support):
        total += len(files)
    return total


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class VerificationOptions:
    def __init__(self, rehash: bool = True, crosscheck: bool = True,
                 collect_hashes: bool = True) -> None:
        self.rehash = rehash
        self.crosscheck = crosscheck
        self.collect_hashes = collect_hashes


def verify_manifest(manifest_path: str, document: Any,
                    options: Optional[VerificationOptions] = None) -> rc.Report:
    """Validate one manifest document against the schema and the filesystem."""
    options = options or VerificationOptions()
    report = rc.Report(manifest_path)
    report.document = document if isinstance(document, dict) else None
    stats: Dict[str, Any] = {"hashes_recomputed": 0, "hashes_recorded": 0,
                             "bytes_rehashed": 0, "crosschecks": {}, "bundle_root": ""}
    report.stats = stats  # type: ignore[attr-defined]

    if not isinstance(document, dict):
        report.error("document-not-object", "manifest must be a JSON object", "$")
        return report

    _check_header(report, document)
    _check_host(report, document)
    _check_flags(report, document)
    _check_counts(report, document)
    _check_gate(report, document)

    bundle_root, how = resolve_bundle_root(manifest_path, document)
    stats["bundle_root"] = os.path.relpath(bundle_root, PROJECT_ROOT) if bundle_root else ""
    stats["bundle_root_resolution"] = how
    if not bundle_root:
        report.error("bundle-root-missing",
                     "could not locate the .app bundle this manifest describes", "$")
    _check_main_program(report, document, manifest_path, bundle_root, options, stats)
    _check_qemu(report, document, bundle_root, options, stats)
    if bundle_root:
        _crosscheck_counts(report, document, bundle_root)

    if options.crosscheck:
        _crosscheck_cmake_cache(report, manifest_path, document, stats)
        _crosscheck_executable(report, manifest_path, document, stats)
    return report


def _check_header(report: rc.Report, doc: Dict[str, Any]) -> None:
    for field in ("schema", "product_version", "generated_at", "host", "verified",
                  "real_hardware_verified", "contrib_backends", "backends",
                  "main_program", "qemu_binaries", "firmware_count", "support_count", "gate"):
        if field not in doc:
            report.error("missing-field", f"missing required field '{field}'", f"$.{field}")

    if rc._require_type(report, doc, "schema", "string", "$"):
        if doc["schema"] != SCHEMA:
            report.error("schema-unknown",
                         f"schema is '{doc['schema']}', expected '{SCHEMA}'", "$.schema")

    if rc._require_type(report, doc, "product_version", "string", "$"):
        if not doc["product_version"].strip():
            report.error("empty-field", "product_version must not be empty", "$.product_version")

    if rc._require_type(report, doc, "generated_at", "string", "$"):
        value = doc["generated_at"]
        if not TIMESTAMP_RE.match(value):
            report.error("generated-at-invalid",
                         f"generated_at '{value}' is not an ISO-8601 'YYYY-MM-DDTHH:MM:SSZ' "
                         f"UTC timestamp", "$.generated_at")
        else:
            try:
                datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError as exc:
                report.error("generated-at-invalid",
                             f"generated_at '{value}' is not a real timestamp: {exc}",
                             "$.generated_at")


def _check_host(report: rc.Report, doc: Dict[str, Any]) -> None:
    if not rc._require_type(report, doc, "host", "object", "$"):
        return
    host = doc["host"]
    for key in ("os", "arch"):
        if not rc._require_nonempty_string(report, host, key, "$.host"):
            continue
    os_value = host.get("os")
    if isinstance(os_value, str) and os_value not in HOST_OSES:
        report.error("host-os-invalid",
                     f"host.os '{os_value}' is not one of {list(HOST_OSES)}", "$.host.os")
    arch_value = host.get("arch")
    if isinstance(arch_value, str) and not rc.canonical_arch(arch_value):
        report.error("host-arch-invalid",
                     f"host.arch '{arch_value}' is not a recognised architecture", "$.host.arch")

    # Informational: does the recorded host match this machine?
    machine = rc.canonical_arch(os.uname().machine if hasattr(os, "uname") else "")
    if isinstance(arch_value, str) and machine and rc.canonical_arch(arch_value) != machine:
        report.warn("host-arch-differs-from-runner",
                    f"manifest was generated on host.arch='{arch_value}' but this machine is "
                    f"'{machine}'; filesystem checks still apply to the local files",
                    "$.host.arch")


def _check_flags(report: rc.Report, doc: Dict[str, Any]) -> None:
    if _require_strict_bool(report, doc, "verified", "$"):
        if doc["verified"] is not False:
            report.error("verified-not-false",
                         "verified must be exactly false: the manifest must never claim "
                         "verification that was not performed", "$.verified")
    if _require_strict_bool(report, doc, "real_hardware_verified", "$"):
        if doc["real_hardware_verified"] is not False:
            report.error("real-hardware-verified-not-false",
                         "real_hardware_verified must be exactly false: no real hardware was "
                         "exercised by the packaging gate", "$.real_hardware_verified")

    contrib = None
    if _require_strict_bool(report, doc, "contrib_backends", "$"):
        contrib = doc["contrib_backends"]

    backends = doc.get("backends")
    if not isinstance(backends, dict):
        if "backends" in doc:
            report.error("wrong-field-type", "backends must be an object", "$.backends")
        return
    for key in BACKEND_KEYS:
        if key not in backends:
            report.error("backends-key-missing", f"backends.{key} is missing", f"$.backends.{key}")
            continue
        if not rc.is_strict_bool(backends[key]):
            report.error("backend-flag-not-bool",
                         f"backends.{key} must be a boolean, got {rc._type_name(backends[key])}",
                         f"$.backends.{key}")

    # Contrib toggle must agree with the two contrib backends.
    if contrib is not None:
        for key in CONTRIB_BACKEND_KEYS:
            flag = backends.get(key)
            if not rc.is_strict_bool(flag):
                continue
            if contrib and not flag:
                report.error("backend-flag-drift",
                             f"contrib_backends is true but backends.{key} is false",
                             f"$.backends.{key}")
            if not contrib and flag:
                report.error("backend-flag-drift",
                             f"contrib_backends is false but backends.{key} is true",
                             f"$.backends.{key}")


def _require_strict_bool(report: rc.Report, obj: Dict[str, Any], key: str, path: str) -> bool:
    if key not in obj:
        return False
    if not rc.is_strict_bool(obj[key]):
        report.error("wrong-field-type",
                     f"{key} must be a boolean, got {rc._type_name(obj[key])}", f"{path}.{key}")
        return False
    return True


def _check_counts(report: rc.Report, doc: Dict[str, Any]) -> None:
    for key in ("firmware_count", "support_count"):
        if key not in doc:
            continue
        value = doc[key]
        if not isinstance(value, int) or rc.is_strict_bool(value):
            report.error("count-not-int",
                         f"{key} must be an integer, got {rc._type_name(value)}", f"$.{key}")
        elif value < 0:
            report.error("count-not-int", f"{key} must not be negative", f"$.{key}")


def _check_gate(report: rc.Report, doc: Dict[str, Any]) -> None:
    if not rc._require_type(report, doc, "gate", "object", "$"):
        return
    gate = doc["gate"]
    status = gate.get("status")
    if not isinstance(status, str) or not status.strip():
        report.error("gate-status-invalid", "gate.status must be a non-empty string", "$.gate.status")
    elif status not in GATE_STATUSES:
        report.error("gate-status-invalid",
                     f"gate.status '{status}' is not one of {list(GATE_STATUSES)}", "$.gate.status")

    checks = gate.get("checks")
    if checks is not None and not isinstance(checks, list):
        report.error("wrong-field-type", "gate.checks must be an array", "$.gate.checks")
        checks = None
    failing_checks = 0
    if isinstance(checks, list):
        for index, check in enumerate(checks):
            path = f"$.gate.checks[{index}]"
            if not isinstance(check, dict):
                report.error("gate-check-not-object", "gate check must be an object", path)
                continue
            for key in ("check", "ok", "skipped", "message"):
                if key not in check:
                    report.error("missing-field", f"gate check is missing '{key}'", f"{path}.{key}")
            for key in ("ok", "skipped"):
                if key in check and not rc.is_strict_bool(check[key]):
                    report.error("gate-check-flag-not-bool",
                                 f"gate check '{check.get('check')}' field {key} must be a boolean",
                                 f"{path}.{key}")
            if check.get("ok") is False and check.get("skipped") is not True:
                failing_checks += 1
                report.error("gate-check-failed",
                             f"gate check '{check.get('check')}' reported failure: "
                             f"{check.get('message')}", f"{path}.ok")

    failures = gate.get("failures")
    if failures is not None and not isinstance(failures, list):
        report.error("wrong-field-type", "gate.failures must be an array", "$.gate.failures")
        failures = None

    failure_count = gate.get("failure_count")
    if not isinstance(failure_count, int) or rc.is_strict_bool(failure_count):
        report.error("gate-failure-count-not-int",
                     f"gate.failure_count must be an integer, got {rc._type_name(failure_count)}",
                     "$.gate.failure_count")
        failure_count = None

    if failure_count is not None and isinstance(failures, list):
        if failure_count != len(failures):
            report.error("gate-failure-count-mismatch",
                         f"gate.failure_count is {failure_count} but gate.failures has "
                         f"{len(failures)} entries", "$.gate.failure_count")

    # A "passed" gate with any failure is a false green.
    if status == "passed":
        if failure_count not in (None, 0):
            report.error("gate-false-green",
                         f"gate.status is 'passed' but failure_count is {failure_count}",
                         "$.gate.status")
        if isinstance(failures, list) and failures:
            report.error("gate-false-green",
                         f"gate.status is 'passed' but gate.failures lists {len(failures)} entr(ies)",
                         "$.gate.status")
        if failing_checks:
            report.error("gate-false-green",
                         f"gate.status is 'passed' but {failing_checks} embedded check(s) failed",
                         "$.gate.status")


def _check_main_program(report: rc.Report, doc: Dict[str, Any], manifest_path: str,
                        bundle_root: str, options: VerificationOptions,
                        stats: Dict[str, Any]) -> None:
    main = doc.get("main_program")
    if not isinstance(main, dict):
        if "main_program" in doc:
            report.error("wrong-field-type", "main_program must be an object", "$.main_program")
        return
    for key in ("path", "sha256", "size"):
        if key not in main:
            report.error("missing-field", f"main_program.{key} is missing", f"$.main_program.{key}")

    path_value = main.get("path")
    if isinstance(path_value, str) and path_value.strip():
        # The generator writes an absolute path; that is legitimate, but it must
        # stay inside this manifest's own build tree -- never another build's.
        build_dir = os.path.dirname(os.path.abspath(manifest_path))
        if os.path.isabs(path_value):
            if escapes_root(build_dir, path_value):
                report.error("main-program-path-escape",
                             f"main_program.path resolves outside this manifest's build "
                             f"directory ({os.path.relpath(build_dir, PROJECT_ROOT)})",
                             "$.main_program.path")
            else:
                report.info("main-program-path-absolute-ok",
                            "main_program.path is absolute but confined to this build tree",
                            "$.main_program.path")
        else:
            safe, why = is_safe_relative_path(path_value)
            if not safe:
                report.error("main-program-path-unsafe", why, "$.main_program.path")

        resolved = path_value if os.path.isabs(path_value) else os.path.join(
            bundle_root or build_dir, path_value)
        _verify_file(report, resolved, "main-program", "$.main_program", main, options, stats)
    else:
        report.error("empty-field", "main_program.path must be a non-empty string",
                     "$.main_program.path")


def _verify_file(report: rc.Report, resolved_path: str, prefix: str, path: str,
                 entry: Dict[str, Any], options: VerificationOptions,
                 stats: Dict[str, Any]) -> None:
    """Existence, size and (optionally) SHA-256 verification for one entry."""
    if not resolved_path:
        return
    if not os.path.exists(resolved_path):
        report.error(f"{prefix}-missing", f"file does not exist: {resolved_path}", f"{path}.path")
        return
    if os.path.islink(resolved_path):
        report.error(f"{prefix}-symlink",
                     f"path is a symbolic link: {resolved_path}", f"{path}.path")
    if os.path.isdir(resolved_path):
        report.error(f"{prefix}-missing", f"path is a directory: {resolved_path}", f"{path}.path")
        return

    actual_size = os.path.getsize(resolved_path)
    recorded_size = entry.get("size")
    if isinstance(recorded_size, int) and not rc.is_strict_bool(recorded_size):
        if recorded_size != actual_size:
            report.error(f"{prefix}-size-mismatch",
                         f"recorded size {recorded_size} != actual {actual_size}", f"{path}.size")
    elif "size" in entry:
        report.error("wrong-field-type",
                     f"size must be an integer, got {rc._type_name(recorded_size)}", f"{path}.size")

    recorded_sha = entry.get("sha256")
    if "sha256" in entry:
        if not isinstance(recorded_sha, str) or not SHA256_RE.match(recorded_sha or ""):
            report.error(f"{prefix}-sha256-format",
                         f"sha256 must be 64 lowercase hex characters, got "
                         f"{rc._type_name(recorded_sha)}", f"{path}.sha256")
        else:
            stats["hashes_recorded"] += 1
            if options.rehash:
                actual_sha = rc.sha256_of_file(resolved_path)
                stats["hashes_recomputed"] += 1
                stats["bytes_rehashed"] += actual_size
                if actual_sha != recorded_sha:
                    report.error(f"{prefix}-sha256-mismatch",
                                 f"recorded sha256 {recorded_sha[:16]}... != recomputed "
                                 f"{actual_sha[:16]}...", f"{path}.sha256")


def _check_qemu(report: rc.Report, doc: Dict[str, Any], bundle_root: str,
                options: VerificationOptions, stats: Dict[str, Any]) -> None:
    entries = doc.get("qemu_binaries")
    if not isinstance(entries, list):
        if "qemu_binaries" in doc:
            report.error("wrong-field-type", "qemu_binaries must be an array", "$.qemu_binaries")
        return
    if not entries:
        report.error("qemu-list-empty",
                     "qemu_binaries is empty: a bundle with QEMU fallback must list its binaries",
                     "$.qemu_binaries")
        return

    seen: Dict[str, int] = {}
    main_path = _as_dict(doc.get("main_program")).get("path")
    # `os.path.relpath("")` raises ValueError, so the emptiness check matters as
    # much as the type check.
    main_rel = (os.path.relpath(main_path, bundle_root)
                if isinstance(main_path, str) and main_path and bundle_root else None)

    for index, entry in enumerate(entries):
        path = f"$.qemu_binaries[{index}]"
        if not isinstance(entry, dict):
            report.error("qemu-entry-not-object", "qemu entry must be an object", path)
            continue
        for key in ("path", "sha256", "size"):
            if key not in entry:
                report.error("missing-field", f"qemu entry is missing '{key}'", f"{path}.{key}")

        value = entry.get("path")
        if not isinstance(value, str) or not value.strip():
            report.error("qemu-path-unsafe", "qemu entry path must be a non-empty string",
                         f"{path}.path")
            continue

        safe, why = is_safe_relative_path(value)
        if not safe:
            code = "qemu-path-absolute" if "absolute" in why else "qemu-path-traversal"
            report.error(code, f"{why}: {value}", f"{path}.path")
            continue

        normalised = value.replace("\\", "/")
        if normalised in seen:
            report.error("qemu-path-duplicate",
                         f"path '{value}' also appears at index {seen[normalised]}", f"{path}.path")
        else:
            seen[normalised] = index
        if main_rel is not None and normalised == main_rel.replace("\\", "/"):
            report.error("qemu-path-duplicate-with-main",
                         f"path '{value}' duplicates main_program.path", f"{path}.path")

        if not bundle_root:
            continue
        resolved = os.path.normpath(os.path.join(bundle_root, normalised))
        real_resolved = _safe_realpath(resolved)
        if escapes_root(bundle_root, resolved) or (
            real_resolved and escapes_root(bundle_root, real_resolved)
        ):
            report.error("qemu-path-symlink",
                         f"path '{value}' escapes the bundle (symlink or traversal): "
                         f"{real_resolved or resolved!r}", f"{path}.path")
            continue
        if os.path.islink(resolved):
            report.error("qemu-path-symlink",
                         f"path '{value}' is a symbolic link", f"{path}.path")
            continue
        _verify_file(report, resolved, "qemu", path, entry, options, stats)


def _crosscheck_counts(report: rc.Report, doc: Dict[str, Any], bundle_root: str) -> None:
    recorded_fw = doc.get("firmware_count")
    if isinstance(recorded_fw, int) and not rc.is_strict_bool(recorded_fw):
        actual = count_firmware(bundle_root)
        if actual != recorded_fw:
            report.error("firmware-count-mismatch",
                         f"firmware_count {recorded_fw} != {actual} file(s) recounted in the bundle",
                         "$.firmware_count")
    recorded_support = doc.get("support_count")
    if isinstance(recorded_support, int) and not rc.is_strict_bool(recorded_support):
        actual = count_support(bundle_root)
        if actual != recorded_support:
            report.error("support-count-mismatch",
                         f"support_count {recorded_support} != {actual} file(s) recounted "
                         f"in the bundle", "$.support_count")


def _crosscheck_cmake_cache(report: rc.Report, manifest_path: str, doc: Dict[str, Any],
                            stats: Dict[str, Any]) -> None:
    build_dir = os.path.dirname(os.path.abspath(manifest_path))
    cache = os.path.join(build_dir, "CMakeCache.txt")
    entry: Dict[str, Any] = {"path": os.path.relpath(cache, PROJECT_ROOT)}
    stats["crosschecks"]["cmake_cache"] = entry
    if not os.path.isfile(cache):
        entry["status"] = "skipped"
        entry["reason"] = "no CMakeCache.txt beside the manifest"
        report.info("crosscheck-cmake-cache-skipped", entry["reason"])
        return
    recorded = None
    try:
        with open(cache, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if line.startswith("VIRTUALWORLD_INCLUDE_CONTRIB_BACKENDS:"):
                    recorded = line.split("=", 1)[-1].strip().upper()
                    break
    except OSError as exc:
        entry["status"] = "skipped"
        entry["reason"] = f"could not read CMakeCache.txt: {exc}"
        report.info("crosscheck-cmake-cache-skipped", entry["reason"])
        return
    if recorded not in ("ON", "OFF"):
        entry["status"] = "skipped"
        entry["reason"] = "VIRTUALWORLD_INCLUDE_CONTRIB_BACKENDS is absent from CMakeCache.txt"
        report.info("crosscheck-cmake-cache-skipped", entry["reason"])
        return
    entry["status"] = "checked"
    entry["cmake_contrib"] = recorded
    entry["manifest_contrib"] = doc.get("contrib_backends")
    expected = recorded == "ON"
    if rc.is_strict_bool(doc.get("contrib_backends")) and doc["contrib_backends"] != expected:
        report.error("crosscheck-cmake-cache-mismatch",
                     f"CMakeCache VIRTUALWORLD_INCLUDE_CONTRIB_BACKENDS={recorded} but the "
                     f"manifest says contrib_backends={doc['contrib_backends']}",
                     "$.contrib_backends")


def _crosscheck_executable(report: rc.Report, manifest_path: str, doc: Dict[str, Any],
                           stats: Dict[str, Any]) -> None:
    """Run the *same build's* executable and compare its compiled backend set."""
    entry: Dict[str, Any] = {}
    stats["crosschecks"]["executable"] = entry
    build_dir = os.path.dirname(os.path.abspath(manifest_path))
    main = doc.get("main_program") if isinstance(doc.get("main_program"), dict) else {}
    path = main.get("path")
    entry["path"] = path

    if not isinstance(path, str) or not path:
        entry["status"] = "skipped"
        entry["reason"] = "manifest has no main_program.path"
        report.info("crosscheck-executable-skipped", entry["reason"])
        return
    # Never substitute another build's binary: the path must belong to this build.
    if escapes_root(build_dir, path):
        entry["status"] = "refused"
        entry["reason"] = "main_program.path is outside this manifest's build directory"
        report.error("crosscheck-other-build-binary",
                     "refusing to cross-check against an executable outside this manifest's "
                     "build directory", "$.main_program.path")
        return
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        entry["status"] = "skipped"
        entry["reason"] = "this build has no runnable same-host executable"
        report.info("crosscheck-executable-skipped", entry["reason"])
        return

    exit_code, stdout, stderr, error = rc.run_binary(path, "--list-backends", None, 90.0)
    if error:
        entry["status"] = "skipped"
        entry["reason"] = error
        report.info("crosscheck-executable-skipped", error)
        return
    document, parse_error = rc.parse_single_json_document(stdout)
    if parse_error or not isinstance(document, dict):
        entry["status"] = "failed"
        entry["reason"] = parse_error or "no JSON document"
        report.error("crosscheck-executable-unparseable",
                     f"executable did not emit a parseable report: {entry['reason']}")
        return

    build_block = document.get("build") if isinstance(document.get("build"), dict) else {}
    mapping = {"macos": "macos_backend", "windows": "windows_backend",
               "linux_kvm": "linux_kvm_backend", "harmony": "harmony_backend"}
    entry["status"] = "checked"
    entry["exit_code"] = exit_code
    entry["observed"] = {k: build_block.get(v) for k, v in mapping.items()}
    entry["stderr_bytes"] = len(stderr)
    reported = doc.get("backends") if isinstance(doc.get("backends"), dict) else {}
    entry["manifest"] = {k: reported.get(k) for k in mapping}
    for backend_key, build_key in mapping.items():
        observed = build_block.get(build_key)
        claimed = reported.get(backend_key)
        if rc.is_strict_bool(observed) and rc.is_strict_bool(claimed) and observed != claimed:
            report.error("crosscheck-executable-mismatch",
                         f"manifest backends.{backend_key}={claimed} but the executable reports "
                         f"{build_key}={observed}", f"$.backends.{backend_key}")


# ---------------------------------------------------------------------------
# Mutation suite
# ---------------------------------------------------------------------------

def _find_qemu(doc: Dict[str, Any], index: int = 0) -> Optional[Dict[str, Any]]:
    entries = doc.get("qemu_binaries")
    if isinstance(entries, list) and len(entries) > index and isinstance(entries[index], dict):
        return entries[index]
    return None


def build_mutations(base: Dict[str, Any], seed: int) -> List[Dict[str, Any]]:
    """Deterministic mutation suite: same seed -> identical suite."""
    rng = random.Random(seed)
    mutations: List[Dict[str, Any]] = []

    def add(name: str, category: str, expected: Sequence[str],
            apply: Callable[[Dict[str, Any]], None]) -> None:
        mutations.append({"name": name, "category": category,
                          "expected_codes": list(expected), "apply": apply})

    # --- honesty flags ----------------------------------------------------
    for label, value in (("bool-true", True), ("int-one", 1), ("string-true", "true")):
        def bad_verified(doc: Dict[str, Any], value=value) -> None:
            doc["verified"] = value
        add(f"verified-pseudo-true-{label}", "verified-flag",
            ["verified-not-false"] if value is True else ["wrong-field-type"], bad_verified)

    for label, value in (("bool-true", True), ("string-false", "false")):
        def bad_rhv(doc: Dict[str, Any], value=value) -> None:
            doc["real_hardware_verified"] = value
        add(f"real-hardware-pseudo-true-{label}", "verified-flag",
            ["real-hardware-verified-not-false"] if value is True else ["wrong-field-type"],
            bad_rhv)

    # --- hashes and sizes -------------------------------------------------
    def main_hash_wrong(doc: Dict[str, Any]) -> None:
        doc["main_program"]["sha256"] = "0" * 64

    add("main-program-hash-wrong", "hash-size", ["main-program-sha256-mismatch"], main_hash_wrong)

    def main_hash_format(doc: Dict[str, Any]) -> None:
        doc["main_program"]["sha256"] = "NOT-A-HASH"

    add("main-program-hash-format", "hash-size", ["main-program-sha256-format"], main_hash_format)

    def main_size_wrong(doc: Dict[str, Any]) -> None:
        doc["main_program"]["size"] = int(doc["main_program"].get("size", 0)) + 1

    add("main-program-size-wrong", "hash-size", ["main-program-size-mismatch"], main_size_wrong)

    def qemu_hash_wrong(doc: Dict[str, Any]) -> None:
        entry = _find_qemu(doc)
        if entry:
            entry["sha256"] = "f" * 64

    add("qemu-hash-wrong", "hash-size", ["qemu-sha256-mismatch"], qemu_hash_wrong)

    def qemu_size_wrong(doc: Dict[str, Any]) -> None:
        entry = _find_qemu(doc)
        if entry:
            entry["size"] = int(entry.get("size", 0)) + 4096

    add("qemu-size-wrong", "hash-size", ["qemu-size-mismatch"], qemu_size_wrong)

    def qemu_missing_file(doc: Dict[str, Any]) -> None:
        entry = _find_qemu(doc)
        if entry:
            entry["path"] = "Contents/Resources/runtime/qemu/nope/missing-qemu"

    add("qemu-file-missing", "hash-size", ["qemu-missing"], qemu_missing_file)

    # --- path safety ------------------------------------------------------
    def duplicate_path(doc: Dict[str, Any]) -> None:
        entries = doc.get("qemu_binaries") or []
        if len(entries) >= 2:
            entries[1]["path"] = entries[0]["path"]
            entries[1]["sha256"] = entries[0]["sha256"]
            entries[1]["size"] = entries[0]["size"]

    add("qemu-duplicate-path", "path-safety", ["qemu-path-duplicate"], duplicate_path)

    def absolute_path(doc: Dict[str, Any]) -> None:
        entry = _find_qemu(doc)
        if entry:
            entry["path"] = "/usr/bin/qemu-system-aarch64"

    add("qemu-absolute-path", "path-safety", ["qemu-path-absolute"], absolute_path)

    def traversal_path(doc: Dict[str, Any]) -> None:
        entry = _find_qemu(doc)
        if entry:
            entry["path"] = "../../../../etc/passwd"

    add("qemu-traversal-path", "path-safety", ["qemu-path-traversal"], traversal_path)

    def traversal_middle(doc: Dict[str, Any]) -> None:
        entry = _find_qemu(doc)
        if entry:
            entry["path"] = "Contents/../../outside/qemu-system-aarch64"

    add("qemu-traversal-middle", "path-safety", ["qemu-path-traversal"], traversal_middle)

    def duplicate_with_main(doc: Dict[str, Any]) -> None:
        entry = _find_qemu(doc)
        main = doc.get("main_program") or {}
        if entry and isinstance(main.get("path"), str) and main["path"]:
            # QEMU paths are bundle-relative while main_program.path is absolute,
            # so express the collision in the relative namespace the check uses.
            entry["path"] = "Contents/MacOS/" + os.path.basename(main["path"])

    add("qemu-duplicate-with-main", "path-safety",
        ["qemu-path-duplicate-with-main"], duplicate_with_main)

    def empty_qemu(doc: Dict[str, Any]) -> None:
        doc["qemu_binaries"] = []

    add("qemu-list-empty", "missing-qemu", ["qemu-list-empty"], empty_qemu)

    # --- backend booleans -------------------------------------------------
    def drift_contrib_linux(doc: Dict[str, Any]) -> None:
        doc["backends"]["linux_kvm"] = not bool(doc.get("contrib_backends"))

    add("backend-drift-linux-kvm", "backend-drift", ["backend-flag-drift"], drift_contrib_linux)

    def drift_contrib_harmony(doc: Dict[str, Any]) -> None:
        doc["backends"]["harmony"] = not bool(doc.get("contrib_backends"))

    add("backend-drift-harmony", "backend-drift", ["backend-flag-drift"], drift_contrib_harmony)

    def backend_not_bool(doc: Dict[str, Any]) -> None:
        doc["backends"]["macos"] = 1

    add("backend-flag-not-bool", "backend-drift", ["backend-flag-not-bool"], backend_not_bool)

    # --- gate -------------------------------------------------------------
    def gate_false_green(doc: Dict[str, Any]) -> None:
        gate = doc["gate"]
        gate["status"] = "passed"
        gate["failure_count"] = 3
        gate["failures"] = ["a", "b", "c"]

    add("gate-false-green", "gate", ["gate-false-green"], gate_false_green)

    def gate_failure_count_mismatch(doc: Dict[str, Any]) -> None:
        doc["gate"]["failure_count"] = 7
        doc["gate"]["failures"] = ["only-one"]

    add("gate-failure-count-mismatch", "gate", ["gate-failure-count-mismatch"],
        gate_failure_count_mismatch)

    def gate_embedded_check_failed(doc: Dict[str, Any]) -> None:
        gate = doc["gate"]
        gate["status"] = "passed"
        gate["checks"] = list(gate.get("checks") or []) + [
            {"check": "injected", "ok": False, "skipped": False, "message": "injected failure"}
        ]

    add("gate-false-green-embedded-check", "gate", ["gate-false-green"],
        gate_embedded_check_failed)

    def gate_status_unknown(doc: Dict[str, Any]) -> None:
        doc["gate"]["status"] = "probably-fine"

    add("gate-status-unknown", "gate", ["gate-status-invalid"], gate_status_unknown)

    # --- time / host ------------------------------------------------------
    def bad_time(doc: Dict[str, Any]) -> None:
        doc["generated_at"] = "2026-10-05 04:04:52"

    add("generated-at-not-iso", "time-host", ["generated-at-invalid"], bad_time)

    def impossible_time(doc: Dict[str, Any]) -> None:
        doc["generated_at"] = "2026-13-45T99:99:99Z"

    add("generated-at-impossible", "time-host", ["generated-at-invalid"], impossible_time)

    def bad_host_os(doc: Dict[str, Any]) -> None:
        doc["host"]["os"] = "Plan9"

    add("host-os-invalid", "time-host", ["host-os-invalid"], bad_host_os)

    def host_os_not_string(doc: Dict[str, Any]) -> None:
        doc["host"]["os"] = 42

    add("host-os-not-string", "time-host", ["wrong-field-type"], host_os_not_string)

    def bad_host_arch(doc: Dict[str, Any]) -> None:
        doc["host"]["arch"] = ""

    add("host-arch-empty", "time-host", ["empty-field"], bad_host_arch)

    # --- counts / structure ----------------------------------------------
    def firmware_count_wrong(doc: Dict[str, Any]) -> None:
        doc["firmware_count"] = int(doc.get("firmware_count", 0)) + 1

    add("firmware-count-mismatch", "counts", ["firmware-count-mismatch"], firmware_count_wrong)

    def support_count_wrong(doc: Dict[str, Any]) -> None:
        doc["support_count"] = int(doc.get("support_count", 0)) + 2

    add("support-count-mismatch", "counts", ["support-count-mismatch"], support_count_wrong)

    def count_not_int(doc: Dict[str, Any]) -> None:
        doc["support_count"] = "6"

    add("support-count-not-int", "counts", ["count-not-int"], count_not_int)

    def schema_unknown(doc: Dict[str, Any]) -> None:
        doc["schema"] = "virtualworld.release/2"

    add("schema-unknown", "schema", ["schema-unknown"], schema_unknown)

    for field in ("main_program", "qemu_binaries", "gate", "host", "backends"):
        def drop(doc: Dict[str, Any], field=field) -> None:
            doc.pop(field, None)
        add(f"missing-{field}", "missing-field", ["missing-field"], drop)

    # --- seeded randomized -------------------------------------------------
    count = len(base.get("qemu_binaries") or [])
    for index in range(8):
        target = rng.randrange(count) if count else 0
        corrupt = rng.choice(("sha256", "size"))
        bad_sha = "".join(rng.choice("0123456789abcdef") for _ in range(64))

        def random_qemu_corruption(doc: Dict[str, Any], target=target, corrupt=corrupt,
                                   bad_sha=bad_sha, index=index) -> None:
            entries = doc.get("qemu_binaries") or []
            if target < len(entries) and isinstance(entries[target], dict):
                if corrupt == "sha256":
                    entries[target]["sha256"] = bad_sha
                else:
                    entries[target]["size"] = int(entries[target].get("size", 0)) + index + 1

        expected = "qemu-sha256-mismatch" if corrupt == "sha256" else "qemu-size-mismatch"
        add(f"random-{index:02d}-qemu-{corrupt}", "randomized", [expected], random_qemu_corruption)

    return mutations


def run_mutation_suite(base: Dict[str, Any], seed: int, manifest_path: str,
                       bundle_root: str, failures_dir: Optional[str],
                       rehash: bool = True) -> Dict[str, Any]:
    """Apply every mutation and require the named detector(s) to fire."""
    mutations = build_mutations(base, seed)
    options = VerificationOptions(rehash=rehash, crosscheck=False)
    results: List[Dict[str, Any]] = []
    gaps: List[Dict[str, Any]] = []

    if failures_dir:
        os.makedirs(failures_dir, exist_ok=True)

    for mutation in mutations:
        document = copy.deepcopy(base)
        try:
            mutation["apply"](document)
        except Exception as exc:  # pragma: no cover - defensive
            gaps.append({"name": mutation["name"], "reason": f"mutation failed to apply: {exc}"})
            continue

        # Validate against the real bundle root so filesystem checks are live.
        with tempfile.TemporaryDirectory() as tmp:
            scratch = os.path.join(tmp, MANIFEST_NAME)
            with open(scratch, "w", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False)
            # Point the scratch manifest at the real build dir so path-escape
            # checks use the same boundary as production.
            report = verify_manifest(scratch, document, options)
        detected = sorted({f.code for f in report.errors})
        fired = all(code in detected for code in mutation["expected_codes"])
        sample_name = f"{mutation['name']}.json"
        if failures_dir:
            with open(os.path.join(failures_dir, sample_name), "w", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.write("\n")

        entry = {
            "name": mutation["name"],
            "category": mutation["category"],
            "expected_codes": mutation["expected_codes"],
            "detected_codes": detected,
            "detector_fired": fired,
            "sample": f"release-manifest-mutations/{sample_name}" if failures_dir else "",
        }
        if not fired:
            gaps.append({
                "name": mutation["name"],
                "reason": f"expected {mutation['expected_codes']} but detector reported {detected}",
            })
        results.append(entry)

    categories: Dict[str, Dict[str, int]] = {}
    for entry in results:
        bucket = categories.setdefault(entry["category"], {"total": 0, "detected": 0})
        bucket["total"] += 1
        bucket["detected"] += 1 if entry["detector_fired"] else 0

    return {
        "seed": seed,
        "bundle_root": os.path.relpath(bundle_root, PROJECT_ROOT) if bundle_root else "",
        "total": len(results),
        "detectors_fired": sum(1 for r in results if r["detector_fired"]),
        "gaps": gaps,
        "coverage_by_category": categories,
        "results": results,
        "ok": not gaps,
    }


def symlink_escape_probe(seed: int) -> Dict[str, Any]:
    """Prove the symlink escape detector works, using a throwaway sandbox.

    Build products must never be modified, so the symlink scenario is created in
    a temporary directory that no real bundle points at.
    """
    with tempfile.TemporaryDirectory() as tmp:
        bundle = os.path.join(tmp, "VirtualWorld.app")
        bin_dir = os.path.join(bundle, "Contents", "Resources", "runtime", "qemu",
                               "macos", "apple-silicon", "bin")
        os.makedirs(bin_dir, exist_ok=True)
        outside_dir = os.path.join(tmp, "outside")
        os.makedirs(outside_dir, exist_ok=True)

        real_file = os.path.join(bin_dir, "qemu-system-aarch64")
        with open(real_file, "wb") as handle:
            handle.write(b"sandbox-qemu")
        secret = os.path.join(outside_dir, "secret.bin")
        with open(secret, "wb") as handle:
            handle.write(b"outside-the-bundle")
        link = os.path.join(bin_dir, "qemu-system-evil")
        os.symlink(secret, link)

        document = {
            "schema": SCHEMA,
            "product_version": "1.0",
            "generated_at": "2026-10-05T04:04:52Z",
            "host": {"os": "Darwin", "arch": "arm64"},
            "verified": False,
            "real_hardware_verified": False,
            "contrib_backends": True,
            "backends": {"macos": True, "windows": True, "linux_kvm": True, "harmony": True},
            "main_program": {
                "path": os.path.join(bundle, "Contents", "MacOS", "VirtualWorld"),
                "sha256": "0" * 64,
                "size": 0,
            },
            "qemu_binaries": [
                {"path": "Contents/Resources/runtime/qemu/macos/apple-silicon/bin/"
                         "qemu-system-aarch64",
                 "sha256": hashlib.sha256(b"sandbox-qemu").hexdigest(), "size": 12},
                {"path": "Contents/Resources/runtime/qemu/macos/apple-silicon/bin/"
                         "qemu-system-evil",
                 "sha256": hashlib.sha256(b"outside-the-bundle").hexdigest(), "size": 18},
            ],
            "firmware_count": 0,
            "support_count": 0,
            "gate": {"status": "not_run", "checks": [], "failure_count": 0, "failures": []},
        }
        manifest_path = os.path.join(tmp, MANIFEST_NAME)
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False)

        report = verify_manifest(manifest_path, document,
                                 VerificationOptions(rehash=True, crosscheck=False))
        codes = sorted({f.code for f in report.errors})
        return {
            "name": "symlink-escape-sandbox",
            "category": "path-safety",
            "expected_codes": ["qemu-path-symlink"],
            "detected_codes": codes,
            "detector_fired": "qemu-path-symlink" in codes,
            "sandbox": "temporary directory; no real bundle was touched",
        }


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def run_certification(args) -> Dict[str, Any]:
    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    if args.manifest_on or args.manifest_off:
        explicit = {"on": [], "off": [], "ambiguous": [], "problems": [],
                    "resolution_required": False}
        for role, value in (("on", args.manifest_on), ("off", args.manifest_off)):
            if value:
                explicit[role].append({"path": os.path.abspath(value), "contrib_backends": role == "on"})
        discovery = explicit
        discovery["mode"] = "explicit"
    else:
        paths = find_manifest_paths(args.search_root or DEFAULT_SEARCH_ROOTS)
        discovery = classify_candidates(paths)
        discovery["mode"] = "auto-discovered"
        discovery["candidates"] = list(paths)

    report: Dict[str, Any] = {
        "report": "virtualworld-release-manifest-certification",
        "report_version": "1.0.0",
        "generated_at": generated_at,
        "method": ("independent black-box re-derivation: hashes, sizes, counts, path safety and "
                   "backend flags are recomputed from the bundle and the same build's executable"),
        "product_code_modified": False,
        "normalization": [],
        "discovery": {
            "mode": discovery.get("mode"),
            "search_roots": list(args.search_root or DEFAULT_SEARCH_ROOTS),
            "candidates": discovery.get("candidates", []),
            "contrib_on": [c["path"] for c in discovery.get("on", [])],
            "contrib_off": [c["path"] for c in discovery.get("off", [])],
            "ambiguous": discovery.get("ambiguous", []),
            "problems": discovery.get("problems", []),
        },
        "manifests": [],
        "mutation": {},
        "failures": [],
        "not_verified": [],
    }

    if discovery.get("resolution_required"):
        report["certification_status"] = "blocked"
        report["blocking"] = {
            "reason": ("contrib ON/OFF discovery is ambiguous or incomplete; explicit "
                       "--manifest-on/--manifest-off paths are required rather than guessing"),
            "problems": discovery.get("problems", []),
            "candidates": [c["path"] for c in discovery.get("on", []) + discovery.get("off", [])
                           + discovery.get("ambiguous", [])],
        }
        return report

    options = VerificationOptions(rehash=not args.no_rehash, crosscheck=not args.no_crosscheck)
    base_document = None
    base_manifest_path = ""
    base_bundle_root = ""

    load_errors: List[Dict[str, Any]] = []
    for role in ("on", "off"):
        for entry in discovery.get(role, []):
            path = entry["path"]
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    document = json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                # An explicit path that cannot be read is a blocking condition,
                # never a crash and never something to paper over.
                load_errors.append({"role": role, "path": path, "error": str(exc)})
                continue
            report_obj = verify_manifest(path, document, options)
            stats = getattr(report_obj, "stats", {})
            row = report_obj.to_dict()
            row["role"] = "contrib ON" if role == "on" else "contrib OFF"
            row["stats"] = stats
            row["bundle_root"] = stats.get("bundle_root", "")
            row["contrib_backends"] = document.get("contrib_backends")
            row["backend_flags"] = document.get("backends")
            row["gate_status"] = _as_dict(document.get("gate")).get("status")
            row["gate_failure_count"] = _as_dict(document.get("gate")).get("failure_count")
            row["qemu_entries"] = len(document.get("qemu_binaries") or [])
            row["firmware_count"] = document.get("firmware_count")
            row["support_count"] = document.get("support_count")
            report["manifests"].append(row)
            if base_document is None:
                base_document = document
                base_manifest_path = path
                base_bundle_root = os.path.join(os.path.dirname(path), "VirtualWorld.app")

    if load_errors:
        report["certification_status"] = "blocked"
        report["blocking"] = {
            "reason": ("an explicitly supplied manifest could not be read or parsed; "
                       "no manifest was certified"),
            "problems": [f"{e['role']}: {e['path']}: {e['error']}" for e in load_errors],
            "candidates": [e["path"] for e in load_errors],
        }
        return report

    # ---- mutation suite (deterministic, from the contrib ON manifest) ----
    if args.skip_mutations or base_document is None:
        report["mutation"] = {"skipped": True, "ok": True, "total": 0, "gaps": [], "results": []}
    else:
        suite = run_mutation_suite(base_document, args.seed, base_manifest_path,
                                   base_bundle_root, FAILURES_DIR,
                                   rehash=not args.no_rehash)
        probe = symlink_escape_probe(args.seed)
        suite["results"].append(probe)
        suite["total"] += 1
        suite["detectors_fired"] += 1 if probe["detector_fired"] else 0
        bucket = suite["coverage_by_category"].setdefault("path-safety",
                                                          {"total": 0, "detected": 0})
        bucket["total"] += 1
        bucket["detected"] += 1 if probe["detector_fired"] else 0
        if not probe["detector_fired"]:
            suite["gaps"].append({"name": probe["name"],
                                  "reason": f"expected {probe['expected_codes']} got "
                                            f"{probe['detected_codes']}"})
            suite["ok"] = False
        report["mutation"] = suite

    # ---- failures / attribution ------------------------------------------
    failures: List[Dict[str, Any]] = []
    for row in report["manifests"]:
        for finding in row["findings"]:
            if finding["severity"] != "error":
                continue
            failures.append({
                "code": finding["code"],
                "role": row["role"],
                "manifest": row["source"],
                "path": finding["path"],
                "message": finding["message"],
                "owner": _attribute(finding["code"]),
            })
    for gap in report["mutation"].get("gaps", []):
        failures.append({
            "code": "mutation-detector-gap",
            "role": "n/a",
            "manifest": "mutation-suite",
            "path": "",
            "message": f"{gap['name']}: {gap['reason']}",
            "owner": "conformance tooling (detector gap)",
        })
    report["failures"] = failures

    report["not_verified"] = [
        "No VM command was run: only --list-backends --json was executed, and never --start.",
        "Real-hardware verification is explicitly false in both manifests and is not claimed here.",
        "Windows and Linux packaging is not covered: only the macOS bundles present on this host "
        "were certified.",
        "Code signature validity beyond the recorded ad-hoc gate result was not re-verified.",
        "Manifests are certified as documents plus their referenced local files; a manifest "
        "produced on another machine cannot be compared against this host's filesystem.",
    ]

    certified = len(report["manifests"]) == 2 and not failures
    report["certification_status"] = "certified" if certified else "findings"
    report["blocking"] = None
    return report


def _attribute(code: str) -> str:
    if code.startswith("crosscheck-cmake"):
        return "packaging gate (cmake/GenerateVirtualizationManifest.cmake + CMakeLists.txt)"
    if code.startswith("crosscheck-executable") or code.startswith("crosscheck-other-build"):
        return "packaging gate / shared integration"
    if code.startswith("gate-"):
        return "packaging gate (cmake/VerifyVirtualizationBundle.cmake)"
    if code.startswith("qemu-") or code.startswith("main-program") or code.startswith("bundle-root"):
        return "packaging gate (manifest generation)"
    if code.startswith("verified") or code.startswith("real-hardware"):
        return "manifest honesty flags"
    if code.startswith("backend") or code.startswith("contrib"):
        return "shared integration (backend flags)"
    return "packaging gate"


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def render_markdown(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# VirtualWorld release manifest — independent certification")
    lines.append("")
    lines.append(f"- Generated: `{report.get('generated_at', '')}`")
    lines.append(f"- **Certification status: `{report.get('certification_status', 'unknown')}`**")
    lines.append(f"- {report.get('method', '')}")
    lines.append("")

    blocking = report.get("blocking")
    if blocking:
        lines.append("## Blocked")
        lines.append("")
        lines.append(f"- {blocking.get('reason', '')}")
        for problem in blocking.get("problems", []):
            lines.append(f"  - {problem}")
        lines.append("")
        lines.append("Candidates found (an explicit `--manifest-on` / `--manifest-off` is required):")
        lines.append("")
        for candidate in blocking.get("candidates", []):
            lines.append(f"- `{candidate}`")
        lines.append("")
        return "\n".join(lines) + "\n"

    discovery = report.get("discovery", {})
    lines.append("## Discovery")
    lines.append("")
    lines.append(f"- Mode: **{discovery.get('mode')}** (never guesses when ambiguous)")
    lines.append(f"- contrib ON: `{discovery.get('contrib_on')}`")
    lines.append(f"- contrib OFF: `{discovery.get('contrib_off')}`")
    if discovery.get("problems"):
        lines.append(f"- Problems: {discovery['problems']}")
    lines.append("")

    lines.append("## Manifest summaries")
    lines.append("")
    lines.append("| Role | Manifest | Contrib | Backends | QEMU entries | Firmware | Support | Gate | "
                 "Hashes recomputed | Errors |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in report["manifests"]:
        stats = row.get("stats", {})
        lines.append(
            f"| {row['role']} | `{os.path.relpath(row['source'], PROJECT_ROOT)}` | "
            f"{row.get('contrib_backends')} | `{row.get('backend_flags')}` | "
            f"{row.get('qemu_entries')} | {row.get('firmware_count')} | "
            f"{row.get('support_count')} | {row.get('gate_status')} | "
            f"{stats.get('hashes_recomputed')} | {row['error_count']} |")
    lines.append("")
    for row in report["manifests"]:
        stats = row.get("stats", {})
        lines.append(f"### {row['role']}")
        lines.append("")
        lines.append(f"- Bundle root: `{row.get('bundle_root')}` "
                     f"({stats.get('bundle_root_resolution', '')})")
        lines.append(f"- Bytes re-hashed: {stats.get('bytes_rehashed', 0):,}")
        cross = stats.get("crosschecks", {})
        cache = cross.get("cmake_cache", {})
        if cache:
            lines.append(f"- CMakeCache cross-check: `{cache.get('status')}` "
                         f"(cache={cache.get('cmake_contrib')}, "
                         f"manifest={cache.get('manifest_contrib')})")
        exe = cross.get("executable", {})
        if exe:
            lines.append(f"- Executable cross-check: `{exe.get('status')}` — "
                         f"observed `{exe.get('observed')}` vs manifest `{exe.get('manifest')}`")
            if exe.get("reason"):
                lines.append(f"  - {exe['reason']}")
        findings = [f for f in row["findings"] if f["severity"] != "info"]
        if findings:
            lines.append("- Findings:")
            for finding in findings:
                lines.append(f"  - {finding['severity']}: `{finding['code']}` "
                             f"{finding['message']} ({finding['path']})")
        else:
            lines.append("- No findings.")
        lines.append("")

    mutation = report.get("mutation", {})
    lines.append("## Mutation suite")
    lines.append("")
    if mutation.get("skipped"):
        lines.append("_Skipped by request._")
        lines.append("")
    else:
        lines.append(f"- Seed: `{mutation.get('seed')}` (deterministic)")
        lines.append(f"- Mutations: **{mutation.get('total')}**, detectors fired: "
                     f"**{mutation.get('detectors_fired')}**, gaps: "
                     f"**{len(mutation.get('gaps', []))}**")
        lines.append("- Failing samples: `reports/release-manifest-mutations/`")
        lines.append("")
        lines.append("| Category | Detected | Total |")
        lines.append("| --- | --- | --- |")
        for category, bucket in sorted(mutation.get("coverage_by_category", {}).items()):
            mark = "" if bucket["detected"] == bucket["total"] else " **(gap)**"
            lines.append(f"| {category}{mark} | {bucket['detected']} | {bucket['total']} |")
        lines.append("")
        if mutation.get("gaps"):
            lines.append("### Detector gaps")
            lines.append("")
            for gap in mutation["gaps"]:
                lines.append(f"- `{gap['name']}`: {gap['reason']}")
            lines.append("")

    lines.append("## Failures and attribution")
    lines.append("")
    failures = report.get("failures", [])
    if not failures:
        lines.append("None: both manifests re-derived cleanly and every detector fired.")
    else:
        lines.append("| Code | Role | Manifest | Owner | Message |")
        lines.append("| --- | --- | --- | --- | --- |")
        for failure in failures:
            lines.append(f"| `{failure['code']}` | {failure['role']} | "
                         f"`{os.path.relpath(failure['manifest'], PROJECT_ROOT)}` | "
                         f"{failure['owner']} | {failure['message']} |")
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

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--search-root", action="append", default=[],
                        help="directory or manifest path to search (repeatable)")
    parser.add_argument("--manifest-on", help="explicit contrib-ON manifest path")
    parser.add_argument("--manifest-off", help="explicit contrib-OFF manifest path")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--no-rehash", action="store_true",
                        help="skip recomputing SHA-256 (reported as such; not for certification)")
    parser.add_argument("--no-crosscheck", action="store_true")
    parser.add_argument("--skip-mutations", action="store_true")
    parser.add_argument("--out-dir", default=REPORTS_DIR)
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_certification(args)

    json_path = os.path.join(args.out_dir, "release-manifest-report.json")
    md_path = os.path.join(args.out_dir, "release-manifest-report.md")
    os.makedirs(args.out_dir, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(report))

    if not args.quiet:
        status = report.get("certification_status")
        print(f"certification_status={status}")
        if status == "blocked":
            print(f"  {report['blocking']['reason']}")
            for candidate in report["blocking"]["candidates"]:
                print(f"  candidate: {candidate}")
        else:
            for row in report["manifests"]:
                print(f"  [{row['role']}] errors={row['error_count']} "
                      f"hashes={row.get('stats', {}).get('hashes_recomputed')} "
                      f"gate={row.get('gate_status')}")
            mutation = report.get("mutation", {})
            print(f"  mutation: {mutation.get('detectors_fired')}/{mutation.get('total')} "
                  f"gaps={len(mutation.get('gaps', []))}")
        print(f"wrote {os.path.relpath(json_path, PROJECT_ROOT)}")
        print(f"wrote {os.path.relpath(md_path, PROJECT_ROOT)}")

    if report.get("certification_status") == "blocked":
        return 2
    return 0 if report.get("certification_status") == "certified" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
