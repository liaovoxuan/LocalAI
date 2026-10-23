#!/usr/bin/env python3
"""VirtualWorld virtualization-backend black-box conformance checker.

What this tool is
-----------------
An *independent, read-only* conformance harness for an already-built VirtualWorld
executable. It never imports the project's C++ code, never inspects src/, and
never starts a guest. It only runs the two documented read-only reporting
commands and validates their JSON against the shared contract:

    <binary> --list-backends [arch] --json
    <binary> --check [arch] --json

Both commands are pure reporting: they enumerate backends, probe the host and
print a machine-readable document. ``--check`` additionally exits non-zero when
the QEMU runtime it resolved cannot run, which this tool accounts for.

What it checks
--------------
1. Process-level: exit code is in the documented set, stdout is exactly one
   parseable JSON document, and the document declares a known schema version.
2. Shape: host / guest_arch / build / backends / selection / support_matrix have
   every required field with the right JSON type (``verified`` must be a real
   boolean, not 0/1 or "false").
3. Selection invariants:
     * backend ids are unique, tiers are legal, exactly one candidate is selected
       and it matches ``selection.backend``;
     * every non-selected candidate carries a non-empty reason;
     * an unsupported backend is never selected;
     * candidates are ordered by tier rank (native -> hardware -> emulation);
     * ``qemu-tcg`` is present, cross-architecture capable, and always last;
     * a QEMU-compatibility backend (harmony-virtualization, linux-kvm) never
       claims native tier or native execution;
     * for ``preference=auto`` the best eligible candidate wins, and a
       cross-architecture guest is always served by ``qemu-tcg``.
4. Support-matrix hygiene: field completeness, ``verified`` strictly boolean, and
   no row may advertise hardware acceleration across architectures.

Offline fixtures
----------------
``--fixtures DIR`` replays captured/synthetic documents instead of running a
binary, so CI on any host can cover Windows x86_64/ARM64, Linux
x86_64/ARM64/RISC-V64, HarmonyOS/OpenHarmony and Intel/Apple Silicon scenarios.
Fixtures are explicitly labelled ``synthetic`` or captured in the manifest, and a
synthetic fixture that claims ``verified: true`` is a finding.

Exit codes: 0 = conformant, 1 = findings, 2 = usage/IO error.

Standard library only; no network access.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

TOOL_NAME = "virtualworld-virtualization-conformance"
TOOL_VERSION = "1.0.0"

SCHEMA_PREFIX = "virtualworld.virtualization"
FIXTURE_SCHEMA_PREFIX = "virtualworld.conformance.fixtures"

TIERS = ("native", "hardware", "emulation")
TIER_RANK = {"native": 0, "hardware": 1, "emulation": 2}

ISSUE_LEVELS = ("info", "warning", "error")
HOST_PLATFORMS = ("macos", "windows", "linux", "harmonyos")

# The canonical values `status_class` is allowed to take. This is the *output*
# vocabulary of statusClass() in the shared capability header: extended codes
# such as "degraded", "runtime-not-runnable" or "whpx-api-missing" are inputs
# that fold into these classes, so they must not appear as a class value.
# Keeping the list here (rather than importing it) preserves the tool's
# independence; it is transcribed from the documented vocabulary.
STATUS_CLASSES = (
    "available",
    "host-unsupported",
    "guest-arch-unsupported",
    "accelerator-unavailable",
    "runtime-missing",
    "config-invalid",
    "backend-mismatch",
    "excluded-by-policy",
    "not-registered",
)

# Canonical architecture spellings used by the shared capability header. The
# tool deliberately keeps its own small alias table rather than importing project
# code, so it stays a genuinely independent black-box check.
ARCH_ALIASES = {
    "aarch64": "aarch64",
    "arm64": "aarch64",
    "arm64e": "aarch64",
    "armv8": "aarch64",
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "x64": "x86_64",
    "i386": "i386",
    "i686": "i386",
    "riscv64": "riscv64",
    "riscv64gc": "riscv64",
}

# Backends that drive QEMU (with or without a host accelerator). They must never
# be presented as host-native virtualization.
QEMU_COMPATIBILITY_BACKENDS = ("harmony-virtualization", "linux-kvm")

# Backends that wrap a real host hypervisor API 1:1 and are therefore required to
# report the native tier.
PLATFORM_NATIVE_BACKENDS = ("macos-virtualization", "windows-hypervisor")

TCG_BACKEND_ID = "qemu-tcg"
HARDWARE_BACKEND_ID = "qemu-hardware"

REQUIRED_HOST_FIELDS = ("arch", "family", "name", "platform")
REQUIRED_BUILD_KEYS = (
    "macos_backend",
    "windows_backend",
    "linux_kvm_backend",
    "harmony_backend",
    "qemu_fallback",
)
REQUIRED_BACKEND_FIELDS = (
    "id",
    "name",
    "tier",
    "host_supported",
    "guest_arch_supported",
    "hardware_acceleration",
    "native_execution",
    "accelerator",
    "status",
    "summary",
    "issues",
    "requirements",
    "runs_in_process",
)
REQUIRED_BACKEND_BOOLS = (
    "host_supported",
    "guest_arch_supported",
    "hardware_acceleration",
    "native_execution",
    "runs_in_process",
)
REQUIRED_CONSIDERED_FIELDS = (
    "id",
    "tier",
    "host_supported",
    "guest_arch_supported",
    "config_accepted",
    "selected",
    "status",
    "reason",
)
REQUIRED_CONSIDERED_BOOLS = (
    "host_supported",
    "guest_arch_supported",
    "config_accepted",
    "selected",
)
REQUIRED_SELECTION_FIELDS = (
    "ok",
    "backend",
    "name",
    "tier",
    "reason",
    "fell_back",
    "fallback_from",
    "considered",
)
REQUIRED_MATRIX_FIELDS = (
    "host_platform",
    "host_arch",
    "guest_arch",
    "backend",
    "tier",
    "status",
    "verified",
    "note",
)
# Rows whose tier implies host hardware is driving the guest CPU.
ACCELERATED_MATRIX_TIERS = ("native", "hardware")

DOCUMENT_REQUIRED_FIELDS = (
    "schema",
    "build",
    "host",
    "guest_arch",
    "preference",
    "backends",
    "selection",
    "support_matrix",
)


# ---------------------------------------------------------------------------
# Findings / report model
# ---------------------------------------------------------------------------

class Severity:
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Finding:
    __slots__ = ("severity", "code", "message", "path")

    def __init__(self, severity: str, code: str, message: str, path: str = "") -> None:
        self.severity = severity
        self.code = code
        self.message = message
        self.path = path

    def to_dict(self) -> Dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "path": self.path,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Finding({self.severity}, {self.code}, {self.path})"


class Report:
    """Collects findings for one checked target (a live run or a fixture)."""

    def __init__(self, source: str, command: Optional[Sequence[str]] = None) -> None:
        self.source = source
        self.command = list(command) if command else []
        self.findings: List[Finding] = []
        self.exit_code: Optional[int] = None
        self.stderr_excerpt: str = ""
        self.document: Optional[Dict[str, Any]] = None

    # -- recording ---------------------------------------------------------
    def add(self, severity: str, code: str, message: str, path: str = "") -> None:
        self.findings.append(Finding(severity, code, message, path))

    def error(self, code: str, message: str, path: str = "") -> None:
        self.add(Severity.ERROR, code, message, path)

    def warn(self, code: str, message: str, path: str = "") -> None:
        self.add(Severity.WARNING, code, message, path)

    def info(self, code: str, message: str, path: str = "") -> None:
        self.add(Severity.INFO, code, message, path)

    # -- queries -----------------------------------------------------------
    @property
    def errors(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == Severity.ERROR]

    @property
    def warnings(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == Severity.WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def codes(self) -> List[str]:
        return [f.code for f in self.findings]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "command": self.command,
            "exit_code": self.exit_code,
            "ok": self.ok,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "findings": [f.to_dict() for f in self.findings],
            "stderr_excerpt": self.stderr_excerpt,
        }


# ---------------------------------------------------------------------------
# Small validation helpers
# ---------------------------------------------------------------------------

def is_strict_bool(value: Any) -> bool:
    """True only for real JSON booleans.

    ``isinstance(True, int)`` is True in Python, so a plain isinstance check would
    silently accept 1/0 for a boolean field. The contract requires real booleans.
    """
    return type(value) is bool


def canonical_arch(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return ARCH_ALIASES.get(value.strip().lower(), value.strip().lower())


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if is_strict_bool(value):
        return "boolean"
    return {
        str: "string",
        int: "number",
        float: "number",
        list: "array",
        dict: "object",
    }.get(type(value), type(value).__name__)


def _require_field(report: Report, obj: Any, key: str, path: str) -> bool:
    if not isinstance(obj, dict):
        return False
    if key not in obj:
        report.error("missing-field", f"missing required field '{key}'", f"{path}.{key}")
        return False
    return True


def _as_dict(value: Any) -> Dict[str, Any]:
    """Return `value` when it really is a mapping, else an empty mapping.

    Guards the `(x or {}).get(...)` idiom, which is only safe when falsy values
    are the only non-mappings that can appear. A truthy non-mapping (e.g. the
    JSON value `true` where an object was expected) used to reach `.get` and
    raise AttributeError out of a validator that must never raise.
    """
    return value if isinstance(value, dict) else {}


def _require_type(report: Report, obj: Any, key: str, expected: str, path: str) -> bool:
    if not _require_field(report, obj, key, path):
        return False
    value = obj[key]
    if expected == "boolean":
        good = is_strict_bool(value)
    elif expected == "string":
        good = isinstance(value, str)
    elif expected == "array":
        good = isinstance(value, list)
    elif expected == "object":
        good = isinstance(value, dict)
    else:  # pragma: no cover - programming error
        raise ValueError(f"unknown expected type {expected}")
    if not good:
        report.error(
            "wrong-field-type",
            f"field '{key}' must be a {expected}, got {_type_name(value)}",
            f"{path}.{key}",
        )
        return False
    return True


def _require_nonempty_string(report: Report, obj: Any, key: str, path: str) -> bool:
    if not _require_type(report, obj, key, "string", path):
        return False
    if not obj[key].strip():
        report.error("empty-field", f"field '{key}' must not be empty", f"{path}.{key}")
        return False
    return True


# ---------------------------------------------------------------------------
# Document checks
# ---------------------------------------------------------------------------

def check_document(
    doc: Any,
    *,
    source: str,
    command: Optional[Sequence[str]] = None,
    requested_arch: Optional[str] = None,
    exit_code: Optional[int] = None,
    allow_codes: Iterable[str] = (),
) -> Report:
    """Validate one ``--list-backends --json`` / ``--check --json`` document."""

    report = Report(source, command)
    report.exit_code = exit_code

    if not isinstance(doc, dict):
        report.error("document-not-object", f"top level must be a JSON object, got {_type_name(doc)}", "$")
        return _apply_allow_codes(report, allow_codes)
    report.document = doc

    for key in DOCUMENT_REQUIRED_FIELDS:
        _require_field(report, doc, key, "$")

    _check_schema(report, doc)
    _check_host(report, doc)
    _check_guest_arch(report, doc, requested_arch)
    _check_build(report, doc)
    backend_ids = _check_backends(report, doc)
    _check_selection(report, doc, backend_ids)
    _check_matrix(report, doc)

    return _apply_allow_codes(report, allow_codes)


def _apply_allow_codes(report: Report, allow_codes: Iterable[str]) -> Report:
    allowed = {c for c in allow_codes if c}
    if not allowed:
        return report
    kept: List[Finding] = []
    for finding in report.findings:
        if finding.code in allowed:
            kept.append(
                Finding(
                    Severity.INFO,
                    finding.code,
                    f"[allowed] {finding.message}",
                    finding.path,
                )
            )
        else:
            kept.append(finding)
    report.findings = kept
    return report


def _check_schema(report: Report, doc: Dict[str, Any]) -> None:
    if not _require_type(report, doc, "schema", "string", "$"):
        return
    schema = doc["schema"]
    if not schema.startswith(SCHEMA_PREFIX + "/"):
        report.error(
            "schema-unknown",
            f"schema '{schema}' does not start with '{SCHEMA_PREFIX}/'",
            "$.schema",
        )
        return
    version_text = schema[len(SCHEMA_PREFIX) + 1 :]
    if not version_text.isdigit():
        report.error("schema-version-invalid", f"schema version '{version_text}' is not an integer", "$.schema")
        return
    if int(version_text) < 1:
        report.error("schema-version-invalid", f"schema version {version_text} is not >= 1", "$.schema")


def _check_host(report: Report, doc: Dict[str, Any]) -> None:
    if not _require_type(report, doc, "host", "object", "$"):
        return
    host = doc["host"]
    for key in REQUIRED_HOST_FIELDS:
        _require_nonempty_string(report, host, key, "$.host")
    platform = host.get("platform")
    if isinstance(platform, str) and platform not in HOST_PLATFORMS:
        report.error(
            "host-platform-unknown",
            f"host.platform '{platform}' is not one of {list(HOST_PLATFORMS)}",
            "$.host.platform",
        )
    arch = host.get("arch")
    if isinstance(arch, str) and not canonical_arch(arch):
        report.error("host-arch-empty", "host.arch canonicalises to an empty string", "$.host.arch")


def _check_guest_arch(report: Report, doc: Dict[str, Any], requested_arch: Optional[str]) -> None:
    if not _require_nonempty_string(report, doc, "guest_arch", "$"):
        return
    guest = canonical_arch(doc["guest_arch"])
    # `canonical_arch` is a normaliser, not a validator: it lowercases anything it
    # does not recognise instead of returning "". That is deliberate -- the alias
    # table is intentionally small and independent, and this bundle ships QEMU
    # targets (e.g. loongarch64) that the table does not list. Rejecting an
    # unknown-but-non-empty architecture here would therefore fail a legitimate
    # report. This branch is consequently reachable only if `canonical_arch`
    # ever starts returning "" for a non-empty string; `_require_nonempty_string`
    # above already covers the empty and non-string cases with `empty-field` and
    # `wrong-field-type`. A document that claims acceleration for an unrecognised
    # guest architecture is still caught, by the cross-architecture rules in
    # `_check_selection_policy` (`cross-arch-not-tcg`).
    if not guest:
        report.error("guest-arch-invalid", f"guest_arch '{doc['guest_arch']}' is not a known architecture", "$.guest_arch")
        return
    if requested_arch:
        want = canonical_arch(requested_arch)
        if want and guest != want:
            report.error(
                "guest-arch-mismatch",
                f"requested '{requested_arch}' but document reports guest_arch '{guest}'",
                "$.guest_arch",
            )


def _check_build(report: Report, doc: Dict[str, Any]) -> None:
    if not _require_type(report, doc, "build", "object", "$"):
        return
    build = doc["build"]
    for key in REQUIRED_BUILD_KEYS:
        if key not in build:
            report.error("build-key-missing", f"build.{key} is missing", f"$.build.{key}")
            continue
        if not is_strict_bool(build[key]):
            report.error(
                "build-value-not-bool",
                f"build.{key} must be a boolean, got {_type_name(build[key])}",
                f"$.build.{key}",
            )
    if is_strict_bool(build.get("qemu_fallback")) and build["qemu_fallback"] is False:
        report.error(
            "qemu-fallback-not-built",
            "build.qemu_fallback is false: the guaranteed last-resort backend is missing",
            "$.build.qemu_fallback",
        )


def _check_backends(report: Report, doc: Dict[str, Any]) -> List[str]:
    if not _require_type(report, doc, "backends", "array", "$"):
        return []
    backends = doc["backends"]
    if not backends:
        report.error("backends-empty", "backends must not be empty", "$.backends")
        return []

    seen: Dict[str, int] = {}
    for index, backend in enumerate(backends):
        path = f"$.backends[{index}]"
        if not isinstance(backend, dict):
            report.error("backend-not-object", f"backend entry must be an object, got {_type_name(backend)}", path)
            continue
        for key in REQUIRED_BACKEND_FIELDS:
            _require_field(report, backend, key, path)
        for key in REQUIRED_BACKEND_BOOLS:
            _require_type(report, backend, key, "boolean", path)
        _require_nonempty_string(report, backend, "name", path)
        _require_nonempty_string(report, backend, "status", path)
        _require_nonempty_string(report, backend, "summary", path)

        backend_id = backend.get("id")
        if isinstance(backend_id, str):
            if not backend_id.strip():
                report.error("backend-id-empty", "backend id must not be empty", f"{path}.id")
            elif backend_id in seen:
                report.error(
                    "backend-id-duplicate",
                    f"backend id '{backend_id}' appears more than once "
                    f"(indices {seen[backend_id]} and {index})",
                    f"{path}.id",
                )
            else:
                seen[backend_id] = index
        else:
            report.error("backend-id-missing", "backend id must be a string", f"{path}.id")

        tier = backend.get("tier")
        if isinstance(tier, str) and tier not in TIERS:
            report.error("backend-tier-invalid", f"tier '{tier}' is not one of {list(TIERS)}", f"{path}.tier")

        _check_issues(report, backend, path)
        _check_string_array(report, backend, "requirements", path)
        _check_optional_status_class(report, backend, path)
        _check_optional_execution_tier(report, backend, path)
        _check_backend_honesty(report, backend, path)

    return list(seen.keys())


def _check_issues(report: Report, backend: Dict[str, Any], path: str) -> None:
    issues = backend.get("issues")
    if not isinstance(issues, list):
        if "issues" in backend:
            report.error("backend-issues-invalid", "issues must be an array", f"{path}.issues")
        return
    for index, issue in enumerate(issues):
        issue_path = f"{path}.issues[{index}]"
        if not isinstance(issue, dict):
            report.error("backend-issue-not-object", "issue must be an object", issue_path)
            continue
        for key in ("level", "code", "message"):
            _require_nonempty_string(report, issue, key, issue_path)
        level = issue.get("level")
        if isinstance(level, str) and level not in ISSUE_LEVELS:
            report.error(
                "backend-issue-level-invalid",
                f"issue level '{level}' is not one of {list(ISSUE_LEVELS)}",
                f"{issue_path}.level",
            )


def _check_string_array(report: Report, obj: Dict[str, Any], key: str, path: str) -> None:
    if key not in obj:
        return
    value = obj[key]
    if not isinstance(value, list):
        report.error("field-not-array", f"{key} must be an array", f"{path}.{key}")
        return
    for index, item in enumerate(value):
        if not isinstance(item, str):
            report.error(
                "array-item-not-string",
                f"{key}[{index}] must be a string, got {_type_name(item)}",
                f"{path}.{key}[{index}]",
            )


def _check_optional_execution_tier(report: Report, backend: Dict[str, Any], path: str) -> None:
    """`execution_tier` is optional: the tier this guest really gets on this host.

    `tier` is the backend's mechanism tier; `execution_tier` is what actually
    happens (e.g. WHPX declares `native` but on a non-Windows host executes
    `emulation`). Validated only when present so a build that predates the field
    still certifies.
    """
    if "execution_tier" not in backend:
        return
    value = backend["execution_tier"]
    if not isinstance(value, str):
        report.error(
            "execution-tier-invalid",
            f"execution_tier must be a string when present, got {_type_name(value)}",
            f"{path}.execution_tier",
        )
    elif value not in TIERS:
        report.error(
            "execution-tier-invalid",
            f"execution_tier '{value}' is not one of {list(TIERS)}",
            f"{path}.execution_tier",
        )


def _check_optional_status_class(report: Report, backend: Dict[str, Any], path: str) -> None:
    """`status_class` is optional: validate it only when present.

    The registry added this canonicalised companion to `status` (a subset of
    backends report backend-specific status codes, so `status_class` is what
    scripts are told to branch on). It is validated leniently rather than added to
    the required-field set, so a build that predates it still certifies and a
    build that emits it empty or non-string is still caught.
    """
    if "status_class" not in backend:
        return
    value = backend["status_class"]
    if not isinstance(value, str):
        report.error(
            "status-class-invalid",
            f"status_class must be a string when present, got {_type_name(value)}",
            f"{path}.status_class",
        )
    elif not value.strip():
        report.error(
            "status-class-invalid",
            "status_class must not be empty when present",
            f"{path}.status_class",
        )
    elif value not in STATUS_CLASSES:
        report.error(
            "status-class-unknown",
            f"status_class '{value}' is not one of the canonical classes "
            f"{list(STATUS_CLASSES)}; extended codes must fold into a class",
            f"{path}.status_class",
        )


def _check_backend_honesty(report: Report, backend: Dict[str, Any], path: str) -> None:
    backend_id = backend.get("id")
    tier = backend.get("tier")
    native_execution = backend.get("native_execution")
    host_supported = backend.get("host_supported")

    # A QEMU-compatibility backend must never be dressed up as native.
    if backend_id in QEMU_COMPATIBILITY_BACKENDS:
        if tier == "native":
            report.error(
                "compat-backend-claims-native",
                f"'{backend_id}' runs the guest through QEMU and must not report tier 'native'",
                f"{path}.tier",
            )
        if native_execution is True:
            report.error(
                "compat-backend-claims-native-execution",
                f"'{backend_id}' must not claim native_execution: the device model is always QEMU",
                f"{path}.native_execution",
            )

    # A backend that wraps a host hypervisor API must report the native tier.
    if backend_id in PLATFORM_NATIVE_BACKENDS and isinstance(tier, str) and tier != "native":
        report.error(
            "platform-native-tier-not-native",
            f"'{backend_id}' wraps a host hypervisor API and must report tier 'native', got '{tier}'",
            f"{path}.tier",
        )

    # native_execution and the native tier are two views of the same claim.
    if native_execution is True and isinstance(tier, str) and tier != "native":
        report.error(
            "native-execution-tier-mismatch",
            f"native_execution is true but tier is '{tier}'",
            f"{path}.native_execution",
        )

    # An unsupported backend has to explain itself to the caller.
    if host_supported is False:
        issues = backend.get("issues")
        explaining = False
        if isinstance(issues, list):
            explaining = any(
                isinstance(i, dict)
                and i.get("level") in ("warning", "error")
                and isinstance(i.get("message"), str)
                and i["message"].strip()
                for i in issues
            )
        summary = backend.get("summary")
        if not explaining and not (isinstance(summary, str) and summary.strip()):
            report.error(
                "unsupported-backend-unexplained",
                f"'{backend_id}' is host-unsupported but carries no warning/error issue and no summary",
                path,
            )


def _check_selection(report: Report, doc: Dict[str, Any], backend_ids: Sequence[str]) -> None:
    if not _require_type(report, doc, "selection", "object", "$"):
        return
    selection = doc["selection"]
    for key in REQUIRED_SELECTION_FIELDS:
        _require_field(report, selection, key, "$.selection")

    if _require_type(report, selection, "ok", "boolean", "$.selection") and selection["ok"] is False:
        reason = selection.get("reason")
        detail = f": {reason}" if isinstance(reason, str) and reason else ""
        report.error("selection-not-ok", f"selection.ok is false{detail}", "$.selection.ok")

    _require_type(report, selection, "fell_back", "boolean", "$.selection")
    _require_nonempty_string(report, selection, "name", "$.selection")
    _require_nonempty_string(report, selection, "reason", "$.selection")

    considered = selection.get("considered")
    if not isinstance(considered, list):
        report.error("considered-not-array", "selection.considered must be an array", "$.selection.considered")
        return
    if not considered:
        report.error("considered-empty", "selection.considered must not be empty", "$.selection.considered")
        return

    ordered_ids: List[str] = []
    selected_ids: List[str] = []
    eligible: List[Tuple[int, str]] = []
    for index, entry in enumerate(considered):
        path = f"$.selection.considered[{index}]"
        if not isinstance(entry, dict):
            report.error("considered-not-object", "considered entry must be an object", path)
            continue
        for key in REQUIRED_CONSIDERED_FIELDS:
            _require_field(report, entry, key, path)
        for key in REQUIRED_CONSIDERED_BOOLS:
            _require_type(report, entry, key, "boolean", path)

        entry_id = entry.get("id")
        if isinstance(entry_id, str):
            ordered_ids.append(entry_id)
        else:
            report.error("considered-id-missing", "considered id must be a string", f"{path}.id")
            continue

        tier = entry.get("tier")
        if isinstance(tier, str) and tier not in TIERS:
            report.error("considered-tier-invalid", f"tier '{tier}' is not one of {list(TIERS)}", f"{path}.tier")

        _require_nonempty_string(report, entry, "status", path)
        _require_nonempty_string(report, entry, "reason", path)

        if entry.get("selected") is True:
            selected_ids.append(entry_id)
            if entry.get("host_supported") is not True:
                report.error(
                    "selected-backend-unsupported",
                    f"'{entry_id}' is selected but host_supported is not true",
                    f"{path}.selected",
                )
            if entry.get("guest_arch_supported") is not True:
                report.error(
                    "selected-backend-arch-unsupported",
                    f"'{entry_id}' is selected but guest_arch_supported is not true",
                    f"{path}.selected",
                )

        if (
            entry.get("host_supported") is True
            and entry.get("guest_arch_supported") is True
            and entry.get("config_accepted") is True
            and isinstance(tier, str)
            and tier in TIER_RANK
        ):
            eligible.append((TIER_RANK[tier], entry_id))

    _check_considered_membership(report, ordered_ids, backend_ids)
    _check_considered_order(report, considered)
    _check_selected_consistency(report, selection, selected_ids)
    _check_tcg_position(report, considered, ordered_ids)
    _check_selection_policy(report, doc, selection, eligible, ordered_ids)


def _check_considered_membership(report: Report, ordered_ids: Sequence[str], backend_ids: Sequence[str]) -> None:
    if not backend_ids:
        return
    reported = set(backend_ids)
    audited = set(ordered_ids)
    missing = sorted(reported - audited)
    extra = sorted(audited - reported)
    if missing:
        report.error(
            "considered-missing-backend",
            f"selection.considered omits registered backends: {missing}",
            "$.selection.considered",
        )
    if extra:
        report.error(
            "considered-unknown-backend",
            f"selection.considered names backends that are not registered: {extra}",
            "$.selection.considered",
        )


def _check_considered_order(report: Report, considered: Sequence[Any]) -> None:
    ranks: List[Tuple[str, int]] = []
    for entry in considered:
        if not isinstance(entry, dict):
            continue
        tier = entry.get("tier")
        if isinstance(tier, str) and tier in TIER_RANK:
            ranks.append((str(entry.get("id")), TIER_RANK[tier]))
    for previous, current in zip(ranks, ranks[1:]):
        if current[1] < previous[1]:
            report.error(
                "considered-order-nonmonotonic",
                f"candidates must be ordered native -> hardware -> emulation, "
                f"but '{current[0]}' (rank {current[1]}) follows '{previous[0]}' (rank {previous[1]})",
                "$.selection.considered",
            )
            return


def _check_selected_consistency(report: Report, selection: Dict[str, Any], selected_ids: Sequence[str]) -> None:
    if len(selected_ids) == 0:
        report.error("considered-none-selected", "no candidate is marked selected", "$.selection.considered")
    elif len(selected_ids) > 1:
        report.error(
            "considered-multiple-selected",
            f"more than one candidate is marked selected: {list(selected_ids)}",
            "$.selection.considered",
        )

    backend = selection.get("backend")
    if isinstance(backend, str):
        if not backend.strip():
            report.error("selection-backend-empty", "selection.backend must not be empty", "$.selection.backend")
        elif selected_ids and backend not in selected_ids:
            report.error(
                "selected-mismatch",
                f"selection.backend is '{backend}' but the selected candidate is {list(selected_ids)}",
                "$.selection.backend",
            )
        elif not selected_ids and backend:
            report.error(
                "selected-mismatch",
                f"selection.backend is '{backend}' but no candidate is marked selected",
                "$.selection.backend",
            )

    tier = selection.get("tier")
    if isinstance(tier, str) and tier not in TIERS:
        report.error("selection-tier-invalid", f"selection.tier '{tier}' is not one of {list(TIERS)}", "$.selection.tier")

    if "execution_tier" in selection:
        execution_tier = selection["execution_tier"]
        if not isinstance(execution_tier, str):
            report.error(
                "execution-tier-invalid",
                f"selection.execution_tier must be a string, got {_type_name(execution_tier)}",
                "$.selection.execution_tier",
            )
        elif execution_tier and execution_tier not in TIERS:
            # Empty is legitimate when no backend could be selected.
            report.error(
                "execution-tier-invalid",
                f"selection.execution_tier '{execution_tier}' is not one of {list(TIERS)}",
                "$.selection.execution_tier",
            )

    if selection.get("fell_back") is True:
        if not isinstance(selection.get("fallback_from"), str) or not selection["fallback_from"].strip():
            report.error(
                "fallback-from-missing",
                "fell_back is true but fallback_from is empty",
                "$.selection.fallback_from",
            )


def _check_tcg_position(report: Report, considered: Sequence[Any], ordered_ids: Sequence[str]) -> None:
    if TCG_BACKEND_ID not in ordered_ids:
        report.error(
            "tcg-missing",
            f"'{TCG_BACKEND_ID}' must always be a candidate: it is the guaranteed fallback",
            "$.selection.considered",
        )
        return

    last_id = ordered_ids[-1]
    if last_id != TCG_BACKEND_ID:
        after = ordered_ids[ordered_ids.index(TCG_BACKEND_ID) + 1 :]
        report.error(
            "tcg-not-last",
            f"'{TCG_BACKEND_ID}' must be the last candidate but {after} follow it "
            f"(last is '{last_id}')",
            "$.selection.considered",
        )

    # Cross-architecture guests can only ever be served by TCG.
    for entry in considered:
        if isinstance(entry, dict) and entry.get("id") == TCG_BACKEND_ID:
            if entry.get("guest_arch_supported") is not True:
                report.error(
                    "tcg-arch-unsupported",
                    f"'{TCG_BACKEND_ID}' must support every guest architecture",
                    "$.selection.considered",
                )
            break


def _check_selection_policy(
    report: Report,
    doc: Dict[str, Any],
    selection: Dict[str, Any],
    eligible: Sequence[Tuple[int, str]],
    ordered_ids: Sequence[str],
) -> None:
    preference = doc.get("preference")
    selected = selection.get("backend")
    host_arch = canonical_arch(_as_dict(doc.get("host")).get("arch"))
    guest_arch = canonical_arch(doc.get("guest_arch"))
    cross_arch = bool(host_arch and guest_arch and host_arch != guest_arch)

    if preference != "auto":
        # A policy-restricted run legitimately excludes backends; the only thing
        # the tool insists on is that TCG is available for cross-arch guests.
        if cross_arch and TCG_BACKEND_ID not in ordered_ids:
            report.error(
                "cross-arch-without-tcg",
                f"guest '{guest_arch}' differs from host '{host_arch}' and '{TCG_BACKEND_ID}' is not a candidate",
                "$.selection.considered",
            )
        return

    if cross_arch:
        if selected != TCG_BACKEND_ID:
            report.error(
                "cross-arch-not-tcg",
                f"guest '{guest_arch}' is cross-architecture against host '{host_arch}', "
                f"so '{TCG_BACKEND_ID}' must be selected, not '{selected}'",
                "$.selection.backend",
            )
        return

    if eligible:
        best_rank = min(rank for rank, _ in eligible)
        best_ids = [bid for rank, bid in eligible if rank == best_rank]
        if selected not in best_ids:
            report.error(
                "selection-not-best-available",
                f"preference is 'auto' and the best eligible candidate(s) {best_ids} "
                f"(rank {best_rank}) were not selected; got '{selected}'",
                "$.selection.backend",
            )


def _check_matrix(report: Report, doc: Dict[str, Any]) -> None:
    if not _require_type(report, doc, "support_matrix", "array", "$"):
        return
    rows = doc["support_matrix"]
    if not rows:
        report.error("matrix-empty", "support_matrix must not be empty", "$.support_matrix")
        return

    for index, row in enumerate(rows):
        path = f"$.support_matrix[{index}]"
        if not isinstance(row, dict):
            report.error("matrix-row-not-object", f"matrix row must be an object, got {_type_name(row)}", path)
            continue
        for key in REQUIRED_MATRIX_FIELDS:
            _require_field(report, row, key, path)
        for key in ("host_platform", "host_arch", "guest_arch", "backend", "tier", "status", "note"):
            _require_nonempty_string(report, row, key, path)

        # `verified` must be a genuine boolean: 0/1/"false" must not pass.
        if "verified" in row and not is_strict_bool(row["verified"]):
            report.error(
                "matrix-verified-not-bool",
                f"verified must be a boolean, got {_type_name(row['verified'])}",
                f"{path}.verified",
            )

        tier = row.get("tier")
        if isinstance(tier, str) and tier not in TIERS:
            report.error("matrix-tier-invalid", f"tier '{tier}' is not one of {list(TIERS)}", f"{path}.tier")

        platform = row.get("host_platform")
        if isinstance(platform, str) and platform not in HOST_PLATFORMS:
            report.error(
                "matrix-platform-unknown",
                f"host_platform '{platform}' is not one of {list(HOST_PLATFORMS)}",
                f"{path}.host_platform",
            )

        # No row may advertise acceleration across architectures.
        row_host = canonical_arch(row.get("host_arch"))
        row_guest = canonical_arch(row.get("guest_arch"))
        if (
            isinstance(tier, str)
            and tier in ACCELERATED_MATRIX_TIERS
            and row_host
            and row_guest
            and row_host != row_guest
        ):
            report.error(
                "matrix-cross-arch-acceleration",
                f"row advertises tier '{tier}' for host '{row_host}' running guest '{row_guest}': "
                f"cross-architecture hardware acceleration does not exist",
                f"{path}.tier",
            )

        if row.get("backend") in QEMU_COMPATIBILITY_BACKENDS and tier == "native":
            report.error(
                "matrix-compat-row-claims-native",
                f"support matrix marks '{row['backend']}' as tier 'native'",
                f"{path}.tier",
            )


# ---------------------------------------------------------------------------
# Running the binary
# ---------------------------------------------------------------------------

ALLOWED_COMMANDS = ("--list-backends", "--check")


def sha256_of_file(path: str) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def run_binary(
    binary: str,
    command: str,
    guest_arch: Optional[str],
    timeout: float,
    env_overrides: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[int], str, str, Optional[str]]:
    """Run one read-only reporting command.

    Returns ``(exit_code, stdout, stderr, error)``. ``error`` is set when the
    process could not be run at all. Only the two whitelisted reporting commands
    are ever executed, so this tool cannot start a VM.
    """
    if command not in ALLOWED_COMMANDS:  # pragma: no cover - guarded by argparse
        raise ValueError(f"refusing to run non-reporting command {command!r}")

    argv = [binary, command]
    if guest_arch:
        argv.append(guest_arch)
    argv.append("--json")

    env = dict(os.environ)
    # Keep the GUI from trying to open a window if the binary was built with Qt.
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    if env_overrides:
        env.update(env_overrides)

    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except FileNotFoundError:
        return None, "", "", f"binary not found: {binary}"
    except PermissionError:
        return None, "", "", f"binary is not executable: {binary}"
    except subprocess.TimeoutExpired:
        return None, "", "", f"'{' '.join(argv)}' timed out after {timeout:g}s"
    except OSError as exc:  # pragma: no cover - platform specific
        return None, "", "", f"failed to run '{' '.join(argv)}': {exc}"

    return completed.returncode, completed.stdout, completed.stderr, None


def parse_single_json_document(stdout: str) -> Tuple[Optional[Any], Optional[str]]:
    """Parse stdout as exactly one JSON document.

    ``json.loads`` already rejects trailing content, which is what we want: a
    report that prints progress text around its JSON is not machine-readable.
    """
    text = stdout.strip()
    if not text:
        return None, "stdout was empty: expected one JSON document"
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, f"stdout is not a single valid JSON document: {exc}"


def check_live_target(
    binary: str,
    command: str,
    guest_arch: Optional[str],
    timeout: float,
    allow_codes: Iterable[str] = (),
    env_overrides: Optional[Dict[str, str]] = None,
) -> Report:
    label = f"{os.path.basename(binary)} {command}" + (f" {guest_arch}" if guest_arch else "")
    exit_code, stdout, stderr, run_error = run_binary(
        binary, command, guest_arch, timeout, env_overrides
    )
    report = Report(label, [binary, command] + ([guest_arch] if guest_arch else []) + ["--json"])

    if run_error:
        report.error("binary-run-failed", run_error, "$")
        return _apply_allow_codes(report, allow_codes)

    report.exit_code = exit_code
    report.stderr_excerpt = stderr.strip()[:500]

    _check_exit_code(report, command, exit_code)

    doc, parse_error = parse_single_json_document(stdout)
    if parse_error:
        report.error("json-unparseable", parse_error, "$")
        if stderr.strip():
            report.error("json-unparseable", f"stderr: {stderr.strip()[:300]}", "$")
        return _apply_allow_codes(report, allow_codes)

    checked = check_document(
        doc,
        source=label,
        command=report.command,
        requested_arch=guest_arch,
        exit_code=exit_code,
        allow_codes=allow_codes,
    )
    checked.stderr_excerpt = report.stderr_excerpt
    return checked


def _check_exit_code(report: Report, command: str, exit_code: Optional[int]) -> None:
    if exit_code is None:
        return
    if command == "--list-backends":
        if exit_code != 0:
            report.error(
                "exit-code-unexpected",
                f"'--list-backends --json' is a pure report and must exit 0, got {exit_code}",
                "$.exit_code",
            )
    else:  # --check
        if exit_code not in (0, 1):
            report.error(
                "exit-code-unexpected",
                f"'--check --json' must exit 0 (runtime runnable) or 1 (runtime missing), got {exit_code}",
                "$.exit_code",
            )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def load_fixture_manifest(fixture_dir: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    manifest_path = os.path.join(fixture_dir, "index.json")
    if not os.path.isfile(manifest_path):
        return None, f"fixture manifest not found: {manifest_path}"
    try:
        with open(manifest_path, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"could not read fixture manifest: {exc}"
    if not isinstance(manifest, dict):
        return None, "fixture manifest must be a JSON object"
    schema = manifest.get("schema")
    if not isinstance(schema, str) or not schema.startswith(FIXTURE_SCHEMA_PREFIX + "/"):
        return None, f"fixture manifest schema must start with '{FIXTURE_SCHEMA_PREFIX}/', got {schema!r}"
    if not isinstance(manifest.get("fixtures"), list):
        return None, "fixture manifest must contain a 'fixtures' array"
    return manifest, None


def check_fixtures(
    fixture_dir: str,
    allow_codes: Iterable[str] = (),
) -> Tuple[List[Report], Optional[str]]:
    manifest, error = load_fixture_manifest(fixture_dir)
    if error:
        return [], error
    assert manifest is not None

    reports: List[Report] = []
    for index, entry in enumerate(manifest["fixtures"]):
        if not isinstance(entry, dict):
            report = Report(f"fixtures[{index}]")
            report.error("fixture-entry-not-object", "manifest entry must be an object", "$.fixtures")
            reports.append(report)
            continue
        reports.append(_check_one_fixture(fixture_dir, entry, allow_codes))
    return reports, None


def _check_one_fixture(
    fixture_dir: str,
    entry: Dict[str, Any],
    allow_codes: Iterable[str],
) -> Report:
    file_name = entry.get("file")
    label = entry.get("label") or file_name or "<unnamed fixture>"
    report = Report(f"fixture:{label}")
    report.command = [f"fixture:{file_name}"]

    if not isinstance(file_name, str) or not file_name:
        report.error("fixture-file-missing", "manifest entry has no 'file'", "$.fixtures")
        return report

    path = os.path.join(fixture_dir, file_name)
    if not os.path.isfile(path):
        report.error("fixture-file-not-found", f"fixture file not found: {path}", "$.fixtures")
        return report

    try:
        with open(path, "r", encoding="utf-8") as handle:
            doc = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        report.error("fixture-unparseable", f"fixture is not valid JSON: {exc}", file_name)
        return report

    synthetic = entry.get("synthetic")
    if not is_strict_bool(synthetic):
        report.error(
            "fixture-synthetic-flag-missing",
            "every fixture must declare synthetic true/false so the report never "
            "confuses a hand-written document with a captured one",
            "$.fixtures.synthetic",
        )
    elif synthetic:
        _check_synthetic_honesty(report, doc, file_name)

    expect_fail = entry.get("expect_fail")
    if not is_strict_bool(expect_fail):
        expect_fail = False
    expected_codes = entry.get("expected_codes")
    if not isinstance(expected_codes, list):
        expected_codes = []

    # Deliberately NOT passing allow_codes here: a negative fixture's whole job is
    # to prove the detector still fires, so its expectation has to be evaluated
    # against the strict result. allow_codes is applied at the very end, after the
    # expectation has been judged.
    checked = check_document(
        doc,
        source=report.source,
        command=[f"fixture:{file_name}"],
        requested_arch=entry.get("requested_arch") if isinstance(entry.get("requested_arch"), str) else None,
    )
    checked.source = report.source
    checked.command = report.command

    expectation = _check_fixture_expectations(entry, checked)
    for finding in report.findings:
        checked.findings.insert(0, finding)
    for finding in expectation:
        checked.findings.append(finding)

    # A negative fixture documents a *known* defect: the checker must reject it.
    if expect_fail:
        if checked.ok:
            checked.error(
                "negative-fixture-unexpectedly-passed",
                "fixture is marked expect_fail but the checker found no errors; the "
                "detector no longer reproduces the documented defect",
                file_name,
            )
        for code in expected_codes:
            if isinstance(code, str) and code and code not in checked.codes():
                checked.error(
                    "negative-fixture-missing-expected-code",
                    f"expected finding code '{code}' was not produced",
                    file_name,
                )
    else:
        for code in expected_codes:
            if isinstance(code, str) and code and code not in checked.codes():
                checked.error(
                    "fixture-missing-expected-code",
                    f"expected finding code '{code}' was not produced",
                    file_name,
                )

    # A real capture that still exhibits a tracked defect is reported as such, so
    # the manifest and the tool agree about what is known.
    tracked = entry.get("tracked_defect_codes")
    if isinstance(tracked, list):
        strict_codes = checked.codes()
        for code in tracked:
            if isinstance(code, str) and code and code in strict_codes:
                checked.info(
                    "tracked-known-defect",
                    f"fixture still exhibits the tracked defect '{code}'",
                    file_name,
                )

    return _apply_allow_codes(checked, allow_codes)


def _check_synthetic_honesty(report: Report, doc: Any, file_name: str) -> None:
    """A hand-written fixture must not claim real-hardware verification."""
    if not isinstance(doc, dict):
        return
    rows = doc.get("support_matrix")
    if isinstance(rows, list):
        verified_rows = [
            row for row in rows if isinstance(row, dict) and row.get("verified") is True
        ]
        if verified_rows:
            report.error(
                "synthetic-fixture-claims-verified",
                f"{len(verified_rows)} support-matrix row(s) claim verified=true in a fixture "
                f"that is explicitly synthetic; verified must only come from real hardware",
                file_name,
            )


def _check_fixture_expectations(entry: Dict[str, Any], report: Report) -> List[Finding]:
    findings: List[Finding] = []
    expect = entry.get("expect")
    if not isinstance(expect, dict):
        return findings

    doc = report.document
    if not isinstance(doc, dict):
        return findings

    def expect_field(path: str, actual: Any, expected: Any) -> None:
        if actual != expected:
            findings.append(
                Finding(
                    Severity.ERROR,
                    "fixture-field-mismatch",
                    f"{path}: manifest expects {expected!r}, document has {actual!r}",
                    path,
                )
            )

    host = doc.get("host") if isinstance(doc.get("host"), dict) else {}
    selection = doc.get("selection") if isinstance(doc.get("selection"), dict) else {}

    if "host_platform" in expect:
        expect_field("$.host.platform", host.get("platform"), expect["host_platform"])
    if "host_arch" in expect:
        expect_field("$.host.arch", canonical_arch(host.get("arch")), canonical_arch(expect["host_arch"]))
    if "guest_arch" in expect:
        expect_field("$.guest_arch", canonical_arch(doc.get("guest_arch")), canonical_arch(expect["guest_arch"]))
    if "selected_backend" in expect:
        expect_field("$.selection.backend", selection.get("backend"), expect["selected_backend"])
    if "selected_tier" in expect:
        expect_field("$.selection.tier", selection.get("tier"), expect["selected_tier"])
    if "expect_fail" in expect and is_strict_bool(expect["expect_fail"]):
        if report.ok == expect["expect_fail"]:
            findings.append(
                Finding(
                    Severity.ERROR,
                    "fixture-expectation-mismatch",
                    f"manifest expected ok={not expect['expect_fail']}, checker produced ok={report.ok}",
                    "$",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def build_report_document(
    reports: Sequence[Report],
    *,
    binary: Optional[str],
    binary_sha256: str,
    fixture_dir: Optional[str],
    started_at: str,
) -> Dict[str, Any]:
    total_errors = sum(len(r.errors) for r in reports)
    total_warnings = sum(len(r.warnings) for r in reports)
    return {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "generated_at": started_at,
        "binary": binary or "",
        "binary_sha256": binary_sha256,
        "fixture_dir": fixture_dir or "",
        "written_by": "stage-4 virtualization conformance harness (read-only, no VM is started)",
        "summary": {
            "targets": len(reports),
            "errors": total_errors,
            "warnings": total_warnings,
            "ok": total_errors == 0,
        },
        "targets": [r.to_dict() for r in reports],
    }


def print_human_summary(reports: Sequence[Report], stream: Any = None) -> None:
    stream = stream or sys.stdout
    print("VirtualWorld virtualization conformance", file=stream)
    print("=" * 60, file=stream)
    for report in reports:
        status = "PASS" if report.ok else "FAIL"
        exit_note = "" if report.exit_code is None else f" exit={report.exit_code}"
        print(f"[{status}] {report.source}{exit_note}", file=stream)
        for finding in report.findings:
            if finding.severity == Severity.INFO:
                continue
            location = f" ({finding.path})" if finding.path else ""
            print(f"    {finding.severity}: {finding.code}: {finding.message}{location}", file=stream)
    errors = sum(len(r.errors) for r in reports)
    warnings = sum(len(r.warnings) for r in reports)
    print("-" * 60, file=stream)
    print(
        f"{len(reports)} target(s) checked, {errors} error(s), {warnings} warning(s) -> "
        f"{'CONFORMANT' if errors == 0 else 'NON-CONFORMANT'}",
        file=stream,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_conformance.py",
        description=(
            "Read-only black-box conformance checks for a built VirtualWorld binary. "
            "Only --list-backends/--check are invoked; no VM is ever started."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  run_conformance.py --binary build/VirtualWorld.app/Contents/MacOS/VirtualWorld\n"
            "  run_conformance.py --binary ... --guest-arch aarch64 --guest-arch x86_64\n"
            "  run_conformance.py --binary ... --output report.json --format both\n"
            "  run_conformance.py --fixtures fixtures --output fixtures-report.json\n"
        ),
    )
    parser.add_argument("--binary", help="path to the built VirtualWorld executable")
    parser.add_argument(
        "--guest-arch",
        action="append",
        default=[],
        metavar="ARCH",
        help="guest architecture to probe (repeatable); default: aarch64 and x86_64",
    )
    parser.add_argument("--output", metavar="PATH", help="write the machine-readable JSON report here")
    parser.add_argument(
        "--format",
        choices=("text", "json", "both"),
        default="both",
        help="what to print on stdout (default: both)",
    )
    parser.add_argument("--fixtures", metavar="DIR", help="replay offline fixtures from DIR instead of running a binary")
    parser.add_argument("--timeout", type=float, default=60.0, help="per-invocation timeout in seconds (default: 60)")
    parser.add_argument(
        "--allow-code",
        action="append",
        default=[],
        metavar="CODE",
        help="downgrade a known finding code to informational (repeatable)",
    )
    parser.add_argument(
        "--backend-env",
        metavar="NAME=VALUE",
        action="append",
        default=[],
        help="extra environment variable for the invoked binary (repeatable), e.g. VIRTUALWORLD_BACKEND=auto",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress the human summary")
    return parser


def parse_env_overrides(pairs: Sequence[str]) -> Tuple[Dict[str, str], Optional[str]]:
    overrides: Dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            return {}, f"--backend-env expects NAME=VALUE, got {pair!r}"
        name, value = pair.split("=", 1)
        name = name.strip()
        if not name:
            return {}, f"--backend-env expects a non-empty variable name, got {pair!r}"
        overrides[name] = value
    return overrides, None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    if not args.binary and not args.fixtures:
        parser.error("either --binary or --fixtures is required")

    env_overrides, env_error = parse_env_overrides(args.backend_env)
    if env_error:
        print(f"error: {env_error}", file=sys.stderr)
        return 2

    reports: List[Report] = []
    binary_sha = ""

    if args.fixtures:
        fixture_reports, fixture_error = check_fixtures(args.fixtures, args.allow_code)
        if fixture_error:
            print(f"error: {fixture_error}", file=sys.stderr)
            return 2
        reports.extend(fixture_reports)

    if args.binary:
        binary = os.path.abspath(args.binary)
        if not os.path.isfile(binary):
            print(f"error: binary not found: {binary}", file=sys.stderr)
            return 2
        if not os.access(binary, os.X_OK):
            print(f"error: binary is not executable: {binary}", file=sys.stderr)
            return 2
        binary_sha = sha256_of_file(binary)

        arches = args.guest_arch or ["aarch64", "x86_64"]
        for arch in arches:
            reports.append(
                check_live_target(binary, "--list-backends", arch, args.timeout, args.allow_code, env_overrides)
            )
            reports.append(
                check_live_target(binary, "--check", arch, args.timeout, args.allow_code, env_overrides)
            )

    document = build_report_document(
        reports,
        binary=os.path.abspath(args.binary) if args.binary else None,
        binary_sha256=binary_sha,
        fixture_dir=os.path.abspath(args.fixtures) if args.fixtures else None,
        started_at=started_at,
    )

    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        except OSError as exc:
            print(f"error: could not write {args.output}: {exc}", file=sys.stderr)
            return 2

    if not args.quiet:
        if args.format in ("text", "both"):
            print_human_summary(reports)
        if args.format in ("json", "both"):
            print(json.dumps(document, ensure_ascii=False, indent=2))

    return 0 if document["summary"]["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
