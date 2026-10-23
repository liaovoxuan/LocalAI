#!/usr/bin/env python3
"""Deterministic parser / input-robustness hardening for the conformance tools.

This stresses the *pure parsing and validation entry points* of every
black-box verifier in this directory:

* ``run_conformance.py``          -- ``parse_single_json_document``, ``check_document``,
                                     ``canonical_arch``, ``is_strict_bool``
* ``run_stability_gate.py``       -- ``canonical_json``, ``project_semantics``
* ``verify_release_manifest.py``  -- ``is_safe_relative_path``, ``escapes_root``,
                                     ``resolve_bundle_root``, ``classify_candidates``,
                                     ``verify_manifest``
* ``make_final_report.py``        -- ``build_defects``, ``invariant_statuses``,
                                     ``render_markdown``

No product binary is executed and no VM command is reachable: every target above
is a pure function or a read-only filesystem query against a temp directory.

How "no defect is hidden" is enforced
-------------------------------------
A sample only counts as clean when the entry point **returned normally** and its
result is well-formed. Exceptions are deliberately *not* swallowed into a pass:
the worker catches them in order to classify them, and every caught exception is
recorded as a finding (`unhandled-exception`, `recursion-limit`, `memory-error`,
...). Nothing that raised, hung, or crashed can be reported as accepted.

Samples also carry an expectation. Categories that must be rejected are checked
for *silent acceptance*; categories that must be tolerated are checked for
over-rejection. Only categories where the contract genuinely does not care are
marked `any`.

Resource bounds
---------------
Every sample is bounded by ``MAX_SAMPLE_BYTES``, ``MAX_DEPTH`` and
``MAX_ARRAY_ITEMS``, and every batch runs in a subprocess under a wall-clock
timeout. A batch that times out or dies is bisected down to the individual
sample, so a hang or a hard crash is attributed to a concrete input.

Exit codes: 0 = hardened clean, 1 = gaps found, 2 = usage/lock failure.
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
import subprocess
import sys
import tempfile
import time
import traceback
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import make_final_report as mfr  # noqa: E402
import run_conformance as rc  # noqa: E402
import run_stability_gate as gate  # noqa: E402
import verify_release_manifest as vrm  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(HERE))
REPORTS_DIR = os.path.join(HERE, "reports")
REPORT_JSON = os.path.join(REPORTS_DIR, "parser-hardening-report.json")
REPORT_MD = os.path.join(REPORTS_DIR, "parser-hardening-report.md")
FAILURES_DIR = os.path.join(REPORTS_DIR, "parser-hardening-failures")

DEFAULT_SEED = 20261005
DEFAULT_MIN_SAMPLES = 5000

# ---- resource bounds -------------------------------------------------------
MAX_SAMPLE_BYTES = 256 * 1024      # 256 KiB per generated payload
MAX_DEPTH = 64                     # nesting depth cap for generated structures
MAX_ARRAY_ITEMS = 256              # array length cap
BATCH_SIZE = 40
DEFAULT_BATCH_TIMEOUT = 120.0
MINIMIZE_ATTEMPTS = 400

# Code-shape contract every finding code must satisfy.
CODE_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")

# Labels this harness itself attaches when it needs to say "the tool refused"
# for an entry point that does not emit finding codes of its own. They are
# deliberately prefixed and excluded from the tool-vocabulary check: inventing a
# code in the tools' namespace would misrepresent their contract.
HARNESS_CODES = {
    "harness-candidate-ambiguous",   # classify_candidates sets resolution_required
}

# Codes produced through an f-string family rather than a literal.
DYNAMIC_CODE_FAMILIES = {
    "verify_release_manifest": (
        "main-program-missing", "main-program-symlink", "main-program-size-mismatch",
        "main-program-sha256-format", "main-program-sha256-mismatch",
        "qemu-missing", "qemu-symlink", "qemu-size-mismatch",
        "qemu-sha256-format", "qemu-sha256-mismatch",
    ),
}

BASE_CONFORMANCE_DOC: Dict[str, Any] = {
    "schema": "virtualworld.virtualization/3",
    "build": {"harmony_backend": False, "linux_kvm_backend": False, "macos_backend": True,
              "qemu_fallback": True, "windows_backend": False},
    "host": {"platform": "macos", "arch": "aarch64",
             "family": "macos-apple-silicon", "name": "macOS (Apple Silicon)"},
    "guest_arch": "aarch64",
    "preference": "auto",
    "backends": [
        {"id": "macos-virtualization", "name": "macOS Virtualization", "tier": "native",
         "status": "available", "status_class": "available", "host_supported": True,
         "guest_arch_supported": True, "hardware_acceleration": True,
         "native_execution": True, "runs_in_process": False, "issues": [],
         "requirements": [], "summary": "native", "execution_tier": "native",
         "accelerator": "hvf"},
        {"id": "qemu-tcg", "name": "QEMU (TCG)", "tier": "emulation",
         "status": "available", "status_class": "available", "host_supported": True,
         "guest_arch_supported": True, "hardware_acceleration": False,
         "native_execution": False, "runs_in_process": False, "issues": [],
         "requirements": [], "summary": "tcg", "execution_tier": "emulation",
         "accelerator": "tcg"},
    ],
    "selection": {
        "ok": True, "backend": "macos-virtualization", "name": "macOS Virtualization",
        "tier": "native", "execution_tier": "native", "reason": "best",
        "fell_back": False, "fallback_from": "",
        "considered": [
            {"id": "macos-virtualization", "tier": "native", "host_supported": True,
             "guest_arch_supported": True, "config_accepted": True, "selected": True,
             "status": "available", "reason": "selected"},
            {"id": "qemu-tcg", "tier": "emulation", "host_supported": True,
             "guest_arch_supported": True, "config_accepted": True, "selected": False,
             "status": "available", "reason": "lower tier"},
        ],
    },
    "support_matrix": [
        {"host_platform": "macos", "host_arch": "aarch64", "guest_arch": "aarch64",
         "backend": "macos-virtualization", "tier": "native", "status": "native",
         "verified": False, "note": "native path"},
        {"host_platform": "macos", "host_arch": "aarch64", "guest_arch": "aarch64",
         "backend": "qemu-tcg", "tier": "emulation", "status": "emulation",
         "verified": False, "note": "software emulation"},
    ],
}

BASE_RELEASE_DOC: Dict[str, Any] = {
    "schema": "virtualworld.release/1",
    "product_version": "1.0",
    "generated_at": "2026-10-05T04:04:52Z",
    "host": {"os": "Darwin", "arch": "arm64"},
    "verified": False,
    "real_hardware_verified": False,
    "contrib_backends": True,
    "backends": {"macos": True, "windows": True, "linux_kvm": True, "harmony": True},
    "main_program": {"path": "", "sha256": "", "size": 0},
    "qemu_binaries": [],
    "firmware_count": 0,
    "support_count": 0,
    "gate": {"status": "passed", "checks": [], "failure_count": 0, "failures": []},
}

# Populated once per process by `_release_sandbox()`.
_RELEASE_SANDBOX: Dict[str, Any] = {}

# Sample payloads must be byte-identical for a given seed, in any process, so
# the generated data cannot embed a random temp directory. The release base
# carries this placeholder and the *executor* binds it to the real sandbox.
SANDBOX_PLACEHOLDER = "<sandbox>"
SANDBOX_MAIN_REL = "VirtualWorld.app/Contents/MacOS/VirtualWorld"
SANDBOX_QEMU_REL = "Contents/Resources/runtime/qemu/macos/apple-silicon/bin/qemu-system-aarch64"
SANDBOX_MAIN_BYTES = b"main-binary"
SANDBOX_QEMU_BYTES = b"qemu-binary"


def _release_sandbox(tmpdir: Optional[str] = None) -> Dict[str, Any]:
    """Create a minimal but *structurally real* .app bundle for the release tests.

    Without this, every release sample is rejected for the same environmental
    reason (no bundle on disk), which would drown out the schema behaviour the
    hardening run is supposed to probe.
    """
    if _RELEASE_SANDBOX:
        return _RELEASE_SANDBOX
    root = tmpdir or tempfile.mkdtemp(prefix="vw-release-sandbox-")
    bundle = os.path.join(root, "VirtualWorld.app")
    main = os.path.join(bundle, "Contents", "MacOS", "VirtualWorld")
    qemu = os.path.join(bundle, "Contents", "Resources", "runtime", "qemu",
                        "macos", "apple-silicon", "bin", "qemu-system-aarch64")
    for path, payload in ((main, SANDBOX_MAIN_BYTES), (qemu, SANDBOX_QEMU_BYTES)):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(payload)
    _RELEASE_SANDBOX.update({
        "root": root,
        "bundle": bundle,
        "main": main,
        "main_size": len(SANDBOX_MAIN_BYTES),
        "main_sha": hashlib.sha256(SANDBOX_MAIN_BYTES).hexdigest(),
        "qemu_rel": SANDBOX_QEMU_REL,
        "qemu_size": len(SANDBOX_QEMU_BYTES),
        "qemu_sha": hashlib.sha256(SANDBOX_QEMU_BYTES).hexdigest(),
    })
    return _RELEASE_SANDBOX


def release_base_document() -> Dict[str, Any]:
    """The release manifest base, with sandbox-independent placeholder paths.

    Byte-identical for every caller: reproducibility of the sample set must not
    depend on which temp directory a process happens to allocate.
    """
    document = copy.deepcopy(BASE_RELEASE_DOC)
    document["main_program"] = {
        "path": f"{SANDBOX_PLACEHOLDER}/{SANDBOX_MAIN_REL}",
        "sha256": hashlib.sha256(SANDBOX_MAIN_BYTES).hexdigest(),
        "size": len(SANDBOX_MAIN_BYTES),
    }
    document["qemu_binaries"] = [{
        "path": SANDBOX_QEMU_REL,
        "sha256": hashlib.sha256(SANDBOX_QEMU_BYTES).hexdigest(),
        "size": len(SANDBOX_QEMU_BYTES),
    }]
    return document


def bind_sandbox(payload: Any, root: str) -> Any:
    """Replace the sandbox placeholder with a real directory, recursively."""
    if isinstance(payload, str):
        return payload.replace(SANDBOX_PLACEHOLDER, root)
    if isinstance(payload, dict):
        return {k: bind_sandbox(v, root) for k, v in payload.items()}
    if isinstance(payload, list):
        return [bind_sandbox(v, root) for v in payload]
    return payload



# ---------------------------------------------------------------------------
# Defects found by this harness and fixed in these tools
# ---------------------------------------------------------------------------
# Recorded here (rather than only in a commit message) because a "clean" result
# on its own would hide the fact that hardening found real problems: by the time
# the report is written they are already fixed, so nothing is left to attribute
# as an outstanding gap.
FIXES_APPLIED: List[Dict[str, str]] = [
    {
        "module": "run_conformance.py",
        "site": "_check_selection_policy",
        "symptom": ("AttributeError: 'bool' object has no attribute 'get' when host was a "
                    "truthy non-mapping (e.g. JSON true)"),
        "cause": "(doc.get('host') or {}).get('arch') passes truthy non-mappings through",
        "fix": "added _as_dict() and used it for the host lookup",
        "regression_test": "TestFixedDefects.test_as_dict_guards_truthy_non_mappings",
    },
    {
        "module": "verify_release_manifest.py",
        "site": "resolve_bundle_root",
        "symptom": ("AttributeError: 'bool' object has no attribute 'get' when main_program "
                    "was a truthy non-mapping"),
        "cause": "(document.get('main_program') or {}).get('path')",
        "fix": "added _as_dict() and applied it at all five (x or {}).get(...) sites",
        "regression_test": "TestFixedDefects.test_release_resolve_bundle_root_survives_non_mapping_main_program",
    },
    {
        "module": "verify_release_manifest.py",
        "site": "escapes_root",
        "symptom": "ValueError: lstat: embedded null character in path",
        "cause": "os.path.realpath() raises on an unresolvable path",
        "fix": ("fails closed: an unresolvable candidate is reported as escaping rather "
                "than raising"),
        "regression_test": "TestFixedDefects.test_escapes_root_fails_closed_on_unresolvable_path",
    },
    {
        "module": "verify_release_manifest.py",
        "site": "_check_qemu",
        "symptom": "ValueError: no path specified",
        "cause": "os.path.relpath('', bundle_root) when main_program.path was empty",
        "fix": "require a non-empty main path before relativising it",
        "regression_test": "TestFixedDefects.test_release_verify_survives_empty_main_program_path",
    },
    {
        "module": "verify_release_manifest.py",
        "site": "_check_qemu",
        "symptom": "ValueError: lstat: embedded null character in path",
        "cause": "os.path.realpath() called unguarded while building the escape message",
        "fix": "added _safe_realpath() and used it in the check and the message",
        "regression_test": "TestFixedDefects.test_release_verify_survives_nul_in_qemu_path",
    },
    {
        "module": "run_stability_gate.py",
        "site": "project_semantics",
        "symptom": "TypeError: 'NoneType' object is not iterable",
        "cause": "dict.get(key, []) returns None when the key is present with JSON null",
        "fix": "added _as_list()/_as_dict() and used them for backends/selection/matrix",
        "regression_test": "TestFixedDefects.test_project_semantics_survives_null_containers",
    },
    {
        "module": "run_conformance.py",
        "site": "_check_guest_arch",
        "symptom": ("guest-arch-invalid was unreachable: unknown-but-non-empty architectures "
                    "are normalised, not rejected"),
        "cause": ("canonical_arch() intentionally lowercases what it does not recognise, so "
                  "the `if not guest` branch could never fire for a non-empty string"),
        "fix": ("documented as deliberate -- rejecting unknown architectures would fail the "
                "bundled loongarch64 target; bogus acceleration claims are still caught by "
                "cross-arch-not-tcg"),
        "regression_test": "TestFixedDefects.test_guest_arch_invalid_is_documented_dead_branch",
    },
]


# ---------------------------------------------------------------------------
# Stable error-code vocabulary (derived from the tool sources)
# ---------------------------------------------------------------------------

def _scan_codes(source: str) -> set:
    """Every finding code a module can emit, scanned from its source.

    A `code = "a" if cond else "b"` ternary would hide "b" from a naive
    `code = "..."` match, so any line that assigns or emits a code contributes
    all of its kebab-case literals.
    """
    found = set()
    for pattern in (r'(?:report\.error|report\.warn|report\.info)\(\s*"([a-z0-9-]+)"',
                    r'(?:report\.error|report\.warn|report\.info)\(\s*\'([a-z0-9-]+)\'',
                    r'fail\(\s*"([a-z0-9-]+)"',
                    r'\{"code":\s*"([a-z0-9-]+)"'):
        found.update(re.findall(pattern, source))
    for line in source.splitlines():
        if re.search(r'\bcode\s*[=:]', line):
            found.update(re.findall(r'"([a-z][a-z0-9]*-[a-z0-9-]+)"', line))
    return found


def known_codes() -> Dict[str, set]:
    """Every finding code the tools can emit, scanned from their sources."""
    table = {
        "run_conformance": _scan_codes(open(os.path.join(HERE, "run_conformance.py"),
                                            encoding="utf-8").read()),
        "verify_release_manifest": _scan_codes(open(os.path.join(HERE, "verify_release_manifest.py"),
                                                    encoding="utf-8").read()),
        "run_stability_gate": _scan_codes(open(os.path.join(HERE, "run_stability_gate.py"),
                                               encoding="utf-8").read()),
    }
    for module, codes in DYNAMIC_CODE_FAMILIES.items():
        table[module].update(codes)
    return table


# ---------------------------------------------------------------------------
# Target registry: pure entry points only
# ---------------------------------------------------------------------------

class Target:
    """A pure entry point under test.

    ``contract`` separates the two very different guarantees:

    * ``untrusted`` -- the entry point consumes attacker-controlled input
      (parsed JSON, raw text, a path from a document). It must *never* raise;
      any exception is a defect.
    * ``internal`` -- the entry point consumes structures this toolchain builds
      itself (a report dict from ``assemble``, a target list from
      ``Report.to_dict``). Out-of-contract garbage may legitimately raise, so an
      exception is only a defect when the payload is structurally in contract
      (``Sample.strict``).
    """

    def __init__(self, name: str, module: str, call: Callable[[Any], Any],
                 interpret: Callable[[Any], Tuple[bool, List[str]]],
                 contract: str = "untrusted") -> None:
        self.name = name
        self.module = module
        self.call = call
        self.interpret = interpret
        self.contract = contract


def _interpret_report(result: Any) -> Tuple[bool, List[str]]:
    codes = [f.code for f in result.findings]
    return (not result.errors), codes


def _interpret_parse(result: Any) -> Tuple[bool, List[str]]:
    document, error = result
    return (document is not None and error is None), ([] if error is None else ["json-unparseable"])


def _interpret_pathsafe(result: Any) -> Tuple[bool, List[str]]:
    safe, _why = result
    return bool(safe), ([] if safe else ["qemu-path-unsafe"])


def _interpret_classify(result: Any) -> Tuple[bool, List[str]]:
    """An empty or one-sided candidate set is a refusal, not a silent success:
    `classify_candidates` itself raises `resolution_required` for it."""
    ok = (not result.get("ambiguous") and not result.get("problems")
          and not result.get("resolution_required"))
    return ok, ([] if ok else ["harness-candidate-ambiguous"])


def _interpret_plain(result: Any) -> Tuple[bool, List[str]]:
    return True, []


def build_targets(tmpdir: str) -> List[Target]:
    """Entry points under test. All pure; the only I/O is temp-dir reads."""
    # The release bundle must sit in the same temp dir as the manifest that
    # describes it, otherwise every path is legitimately "escaping".
    _release_sandbox(tmpdir)
    sandbox_root = os.path.join(tmpdir, "sandbox-root")
    os.makedirs(sandbox_root, exist_ok=True)
    manifest_path = os.path.join(tmpdir, "hardening-manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as handle:
        json.dump(BASE_RELEASE_DOC, handle)

    def call_check(payload: Any):
        return rc.check_document(payload, source="hardening")

    def call_verify(payload: Any):
        # Parsing/validation only: hashing and cross-checks are out of scope here.
        bound = bind_sandbox(payload, tmpdir)
        return vrm.verify_manifest(manifest_path, bound,
                                   vrm.VerificationOptions(rehash=False, crosscheck=False))

    return [
        Target("json.parse", "run_conformance", rc.parse_single_json_document, _interpret_parse),
        Target("conformance.check", "run_conformance", call_check, _interpret_report),
        Target("conformance.arch", "run_conformance", rc.canonical_arch, _interpret_plain),
        Target("conformance.strictbool", "run_conformance", rc.is_strict_bool, _interpret_plain),
        Target("release.pathsafe", "verify_release_manifest",
               vrm.is_safe_relative_path, _interpret_pathsafe),
        Target("release.escapesroot", "verify_release_manifest",
               lambda p: vrm.escapes_root(sandbox_root, p if isinstance(p, str) else str(p)),
               _interpret_plain),
        Target("release.resolvebundle", "verify_release_manifest",
               lambda p: vrm.resolve_bundle_root(
                   manifest_path, p if isinstance(p, dict) else {}),
               _interpret_plain),
        Target("release.verify", "verify_release_manifest", call_verify, _interpret_report),
        Target("release.classify", "verify_release_manifest",
               lambda p: vrm.classify_candidates(p if isinstance(p, list) else [str(p)]),
               _interpret_classify),
        Target("stability.canonical", "run_stability_gate", gate.canonical_json, _interpret_plain),
        Target("stability.semantics", "run_stability_gate", gate.project_semantics,
               _interpret_plain),
        Target("report.defects", "make_final_report", mfr.build_defects, _interpret_plain,
               contract="internal"),
        Target("report.invariants", "make_final_report", mfr.invariant_statuses,
               _interpret_plain, contract="internal"),
        Target("report.render", "make_final_report", mfr.render_markdown, _interpret_plain,
               contract="internal"),
    ]


TARGET_BY_NAME: Dict[str, Target] = {}


def _target(name: str) -> Target:
    return TARGET_BY_NAME[name]


# ---------------------------------------------------------------------------
# Sample generation
# ---------------------------------------------------------------------------

class Sample:
    __slots__ = ("sid", "category", "target", "payload", "expect", "note", "strict",
                 "expect_code")

    def __init__(self, sid: str, category: str, target: str, payload: Any,
                 expect: str, note: str = "", strict: bool = True,
                 expect_code: Optional[str] = None) -> None:
        self.sid = sid
        self.category = category
        self.target = target
        self.payload = payload
        self.expect = expect
        self.note = note
        self.strict = strict
        # When set, the rejection must carry this exact code: "rejected somehow"
        # is a much weaker claim than "rejected for the documented reason".
        self.expect_code = expect_code

    def to_dict(self) -> Dict[str, Any]:
        return {"sid": self.sid, "category": self.category, "target": self.target,
                "payload": self.payload, "expect": self.expect, "note": self.note,
                "strict": self.strict, "expect_code": self.expect_code}


def _bound_json(value: Any) -> Any:
    """Clamp a generated structure to the documented resource bounds."""
    def clamp(node: Any, depth: int) -> Any:
        if depth > MAX_DEPTH:
            return None
        if isinstance(node, dict):
            return {k: clamp(v, depth + 1) for k, v in list(node.items())[:MAX_ARRAY_ITEMS]}
        if isinstance(node, list):
            return [clamp(v, depth + 1) for v in node[:MAX_ARRAY_ITEMS]]
        if isinstance(node, str) and len(node) > MAX_SAMPLE_BYTES:
            return node[:MAX_SAMPLE_BYTES]
        return node
    return clamp(value, 0)


CONTROL_CHARS = "".join(chr(c) for c in list(range(0x00, 0x20)) + [0x7F])
CONFUSABLES = {
    "nul": "\x00", "bell": "\x07", "esc": "\x1b", "del": "\x7f",
    "nbsp": "\u00a0", "zero-width": "\u200b", "bom": "\ufeff",
    "rtl-override": "\u202e", "line-sep": "\u2028", "bidi-isolate": "\u2066",
    "fullwidth-a": "\uff41", "cyrillic-a": "\u0430", "combining": "a\u0301",
    "surrogate-pair": "\U0001f600", "noncharacter": "\ufffe",
}
NUMERIC_EDGES = [
    "NaN", "Infinity", "-Infinity", "1e400", "-1e400", "1e-400",
    "1" + "0" * 400, "-" + "1" * 400, "0.0", "-0.0", "1E+2", "0x10",
    "1_000", ".5", "5.", "+1", "true", "null", "1.7976931348623157e309",
]
TIMESTAMP_EDGES = [
    "", " ", "not-a-time", "2026-10-05T04:04:52", "2026-10-05T04:04:52+00:00",
    "2026-10-05T04:04:52.123Z", "2026-13-45T99:99:99Z", "0000-00-00T00:00:00Z",
    "2026-02-30T00:00:00Z", "2026-10-05t04:04:52z", "20261005T040452Z",
    "2026-10-05T04:04:52Z\x00", " 2026-10-05T04:04:52Z ",
]
HASH_EDGES = [
    "", " ", "0" * 63, "0" * 65, "A" * 64, "g" * 64, "0" * 64 + "\x00",
    "z" * 64, "-" * 64, "0" * 64 + " ", "0" * 63 + "\n",
]
PATH_EDGES = [
    "", " ", ".", "..", "../", "../../etc/passwd", "/etc/passwd", "/", "//", "///",
    "C:\\Windows\\System32", "~/.ssh/id_rsa", "Contents/../../outside/x",
    "./Contents/./MacOS/x", "Contents//MacOS//x", "Contents\\MacOS\\x",
    "Contents/\x00MacOS/x", "Contents/MacOS/x\n", "\u202eContents/MacOS/x",
    "a" * 5000, "Contents/MacOS/\u0430pple", "file:///etc/passwd",
    "\\\\server\\share", "CON", "aux.txt ", "Contents/MacOS/.",
]


def _json_texts(rng: random.Random, base: Dict[str, Any]) -> List[Sample]:
    """Malformed and adversarial *text* inputs for the JSON parser."""
    out: List[Sample] = []
    serialized = json.dumps(base, ensure_ascii=False)

    def add(category: str, payload: str, expect: str, note: str = "") -> None:
        out.append(Sample("", category, "json.parse", payload, expect, note))

    # empty / whitespace
    for text in ("", "   ", "\n", "\t", "\x00", "\ufeff", CONTROL_CHARS[:8]):
        add("json-empty", text, "reject", "no document at all")

    # truncated at many offsets
    for fraction in (0.05, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99):
        cut = max(1, int(len(serialized) * fraction))
        add("json-truncated", serialized[:cut], "reject", f"cut at {fraction:.0%}")
    for cut in (1, 2, 3, 5, 8):
        add("json-truncated", serialized[:cut], "reject", f"cut at {cut} bytes")

    # duplicate keys: the last one wins, and this must not be a crash.
    add("json-duplicate-key", '{"schema":"a","schema":"b"}', "any",
        "duplicate keys are legal JSON text; last wins")
    add("json-duplicate-key", '{"backends":[],"backends":{}}', "any", "duplicate key, mixed types")
    add("json-duplicate-key", '{"a":1,"a":2,"a":3,"a":4}', "any", "repeated key")

    # trailing content / multiple documents
    add("json-trailing", serialized + " garbage", "reject", "trailing text")
    add("json-trailing", serialized + serialized, "reject", "two concatenated documents")
    add("json-trailing", serialized + "\n" + serialized, "reject", "two documents on two lines")
    add("json-trailing", "[]" + serialized, "reject", "prefix array then object")

    # non-object top level
    for text in ("[]", "null", "true", "false", "0", '""', '"str"'):
        add("json-non-object", text, "any", "valid JSON but not an object")

    # comments / non-standard syntax
    add("json-nonstandard", '{"a":1,}', "reject", "trailing comma")
    add("json-nonstandard", "{'a':1}", "reject", "single quotes")
    add("json-nonstandard", '{"a":/*c*/1}', "reject", "block comment")
    add("json-nonstandard", '{"a":1}//c', "reject", "line comment")
    add("json-nonstandard", '{"a":undefined}', "reject", "undefined literal")

    # numbers
    for literal in NUMERIC_EDGES:
        add("json-number-edge", '{"v":%s}' % literal, "any", f"number literal {literal!r}")

    # control characters and unicode
    for name, ch in CONFUSABLES.items():
        add("json-unicode", '{"v":"%s"}' % ch, "any", f"character {name}")
        add("json-unicode", '{"%s":1}' % ch, "any", f"key is {name}")
    for code in (0x00, 0x01, 0x08, 0x0b, 0x0c, 0x1f, 0x7f):
        add("json-control-char", '{"v":"%s"}' % chr(code), "any", f"raw control U+{code:04X}")
        add("json-control-char", '{"v":"\\u%04x"}' % code, "any", f"escaped control U+{code:04X}")

    # long strings and deep nesting
    for length in (1024, 8192, 65536, MAX_SAMPLE_BYTES):
        add("json-long-string", '{"v":"%s"}' % ("x" * min(length, MAX_SAMPLE_BYTES)),
            "any", f"string of {min(length, MAX_SAMPLE_BYTES)} chars")
    for depth in (8, 32, MAX_DEPTH, MAX_DEPTH + 8, MAX_DEPTH * 4):
        text = "[" * depth + "]" * depth
        add("json-deep-nesting", text, "any", f"array nesting depth {depth}")
        obj = '{"a":' * depth + "1" + "}" * depth
        add("json-deep-nesting", obj, "any", f"object nesting depth {depth}")
    for count in (16, 128, MAX_ARRAY_ITEMS, MAX_ARRAY_ITEMS * 2):
        add("json-long-array", "[" + ",".join("0" for _ in range(count)) + "]",
            "any", f"array of {count} items")

    # duplicate array entries
    dup = copy.deepcopy(base)
    dup["backends"] = [dup["backends"][0], copy.deepcopy(dup["backends"][0])]
    add("json-duplicate-array", json.dumps(dup), "accept",
        "duplicate backend object: valid JSON, validator must decide")

    # unknown fields
    extra = copy.deepcopy(base)
    extra["unknown_field"] = {"nested": [1, 2, 3]}
    extra["backends"][0]["unknown_backend_field"] = "x"
    add("json-unknown-field", json.dumps(extra), "accept",
        "unknown fields must not be rejected")

    # random byte-level fuzz around a valid document
    for _ in range(120):
        text = serialized
        for _ in range(rng.randint(1, 4)):
            kind = rng.random()
            pos = rng.randrange(len(text)) if text else 0
            if kind < 0.4 and text:
                text = text[:pos] + rng.choice("{}[],:\"\\\x00\x1b\u202e") + text[pos:]
            elif kind < 0.7 and text:
                text = text[:pos] + text[pos + 1:]
            else:
                text = text[:pos] + rng.choice(["null", "NaN", "1e999", "\"", "\\u"]) + text[pos:]
        if len(text) > MAX_SAMPLE_BYTES:
            text = text[:MAX_SAMPLE_BYTES]
        add("json-fuzz", text, "any", "random byte-level edit")

    # random structural fuzz
    for _ in range(120):
        add("json-structural-fuzz", json.dumps(_random_json(rng, 0)), "any",
            "random JSON structure")

    return out


def _full_report_fixture() -> Dict[str, Any]:
    """A structurally complete report, as `assemble()` would emit."""
    return {
        "report": "virtualworld-virtualization-final-certification",
        "report_version": "1.0.0",
        "generated_at": "2026-10-05T00:00:00+00:00",
        "certification_status": "conformant",
        "method": "m",
        "product": {"path": "/x", "sha256": "a" * 64, "size_bytes": 1,
                    "mtime": "2026-10-05T00:00:00", "build_label": "b", "schema": "s",
                    "host": {"platform": "macos", "arch": "aarch64"},
                    "registered_backend_ids": ["qemu-tcg"], "selection_order": ["qemu-tcg"]},
        "build_integrity": {"binary_mtime": "t", "newest_product_input": "p",
                            "newest_product_input_mtime": "t",
                            "newest_product_input_skew_seconds": 0.0,
                            "product_inputs_newer_than_binary": [],
                            "test_sources_newer_than_binary": [],
                            "artifact_matches_newest_product_input": True, "note": "n"},
        "build_integrity_note": "n",
        "product_self_tests": {"available": False, "results": [], "passed": None,
                               "failed": None, "stale": None, "note": ""},
        "self_tests": {"command": "python3 t.py", "exit_code": 0, "cases": 1, "ok": True,
                       "summary": "OK"},
        "live": {"summary": {"targets": 1, "errors": 0, "warnings": 0, "ok": True},
                 "targets": [{"source": "s", "command": [], "exit_code": 0, "ok": True,
                              "error_count": 0, "warning_count": 0, "findings": [],
                              "stderr_excerpt": ""}]},
        "fixtures": {"rows": [], "summary": {}, "manifest": ""},
        "legacy": {"selections": [], "check_agreement": []},
        "invariants": [{"id": "schema", "description": "d", "status": "pass"}],
        "defects": [],
        "support_matrix": [],
        "failures": [],
        "not_verified": ["n"],
    }


def _random_json(rng: random.Random, depth: int) -> Any:
    if depth > 4:
        return rng.choice([None, True, False, 0, 1, -1, "", "x", "\x00"])
    kind = rng.randrange(8)
    if kind == 0:
        return None
    if kind == 1:
        return rng.choice([True, False])
    if kind == 2:
        return rng.choice([0, -0.0, 1, -1, 2 ** 53, 1e308, -1e308])
    if kind == 3:
        return "".join(rng.choice("ab\x00\x1b\u202e ") for _ in range(rng.randint(0, 12)))
    if kind == 4:
        return {f"k{i}": _random_json(rng, depth + 1) for i in range(rng.randint(0, 5))}
    if kind == 5:
        return [_random_json(rng, depth + 1) for _ in range(rng.randint(0, 5))]
    if kind == 6:
        return rng.choice(NUMERIC_EDGES)
    return rng.choice(PATH_EDGES)


def _document_samples(rng: random.Random, base: Dict[str, Any], target: str,
                      prefix: str, reject_notes: Dict[str, str]) -> List[Sample]:
    """Systematic type-swap and value-swap across every field of a document."""
    out: List[Sample] = []

    def add(category: str, payload: Any, expect: str, note: str = "",
            expect_code: Optional[str] = None) -> None:
        out.append(Sample("", category, target, _bound_json(payload), expect, note,
                          expect_code=expect_code))

    wrong_values = [None, True, False, 0, 1, -1, 3.14, "", " ", "x", [], {}, [[]], {"a": 1},
                    [1, 2], "0", "false", "\x00"]

    for path in _field_paths(base):
        for value in wrong_values:
            document = copy.deepcopy(base)
            try:
                _set_path(document, path, copy.deepcopy(value))
            except Exception:
                continue
            # Robustness only: which fields are validated is the contract's
            # business, and asserting "must reject" for every field would encode
            # my assumptions rather than the schema. Contract claims live in
            # _targeted_swaps below, with an explicit expected code.
            add(f"{prefix}-type-swap", document, "any",
                f"{path} replaced with {_short(value)}")

    for path, value, expected_code, note in _targeted_swaps(base):
        document = copy.deepcopy(base)
        try:
            _set_path(document, path, value)
        except Exception:
            continue
        out.append(Sample("", f"{prefix}-{note}", target, _bound_json(document),
                          "reject" if expected_code else "any",
                          f"{path} = {_short(value)}", expect_code=expected_code))

    # remove required sections
    for key in list(base.keys()):
        document = copy.deepcopy(base)
        document.pop(key, None)
        add(f"{prefix}-missing-section", document, "reject", f"dropped {key}")

    # duplicate array entries
    for array_field in [k for k, v in base.items() if isinstance(v, list)]:
        document = copy.deepcopy(base)
        items = document[array_field]
        if items:
            document[array_field] = items + [copy.deepcopy(items[0])]
            duplicated = (array_field == "backends")
            add(f"{prefix}-duplicate-array", document,
                "reject" if duplicated else "any",
                f"{array_field} has a duplicated entry",
                expect_code="backend-id-duplicate" if duplicated else None)

    # unknown fields must be tolerated
    document = copy.deepcopy(base)
    document["__unknown__"] = {"a": [1, 2, {"b": None}]}
    add(f"{prefix}-unknown-field", document, "accept", "extra top-level field")
    for array_field in [k for k, v in base.items() if isinstance(v, list) and v
                        and isinstance(v[0], dict)]:
        document = copy.deepcopy(base)
        document[array_field][0]["__unknown__"] = 1
        add(f"{prefix}-unknown-field", document, "accept",
            f"extra field on {array_field}[0]")

    # non-object payloads for a document validator
    for value in ([], None, True, 0, "text", 3.5):
        add(f"{prefix}-not-object", value, "reject", f"top level {_short(value)}")

    # deep nesting inside the document
    deep: Any = {"leaf": True}
    for _ in range(MAX_DEPTH + 8):
        deep = {"n": deep}
    document = copy.deepcopy(base)
    document["__deep__"] = deep
    add(f"{prefix}-deep-unknown", document, "accept", "deeply nested unknown field")

    # long strings in string fields
    for path in _string_paths(base):
        document = copy.deepcopy(base)
        try:
            _set_path(document, path, "y" * 4096)
        except Exception:
            continue
        add(f"{prefix}-long-string", document, "any", f"{path} padded to 4096 chars")

    # corrupt timestamps and hashes wherever they appear
    for path in _paths_matching(base, ("generated_at",)):
        for value in TIMESTAMP_EDGES:
            document = copy.deepcopy(base)
            _set_path(document, path, value)
            add(f"{prefix}-timestamp", document, "reject", f"{path} = {value!r}")
    for path in _paths_matching(base, ("sha256",)):
        for value in HASH_EDGES:
            document = copy.deepcopy(base)
            _set_path(document, path, value)
            add(f"{prefix}-hash", document, "reject", f"{path} = {value[:16]!r}")

    # path-ish fields
    for path in _paths_matching(base, ("path",)):
        for value in PATH_EDGES:
            document = copy.deepcopy(base)
            _set_path(document, path, value)
            expect = "reject" if value not in ("", " ") else "reject"
            add(f"{prefix}-path", document, expect, f"{path} = {value[:24]!r}")

    return out


def _scalar_samples(rng: random.Random) -> List[Sample]:
    """Inputs for the small scalar helpers."""
    out: List[Sample] = []
    scalars: List[Any] = [None, True, False, 0, 1, -1, 0.0, -0.0, 3.14, float("inf"),
                          float("nan"), "", " ", "x", "\x00", "\u202e", [], {}, [1],
                          {"a": 1}, 2 ** 63, -(2 ** 63), 10 ** 100]
    scalars += list(PATH_EDGES)
    scalars += ["aarch64", "arm64", "x86_64", "amd64", "riscv64", "AARCH64", "aarch64 ",
                "aarch64\x00", "ааrch64"]

    for value in scalars:
        out.append(Sample("", "scalar-arch", "conformance.arch", value, "any",
                          f"canonical_arch({_short(value)})"))
        out.append(Sample("", "scalar-bool", "conformance.strictbool", value, "any",
                          f"is_strict_bool({_short(value)})"))
        out.append(Sample("", "scalar-pathsafe", "release.pathsafe", value, "any",
                          f"is_safe_relative_path({_short(value)})"))
        out.append(Sample("", "scalar-escapesroot", "release.escapesroot", value, "any",
                          f"escapes_root(sandbox, {_short(value)})"))
        out.append(Sample("", "scalar-canonical", "stability.canonical", value, "any",
                          f"canonical_json({_short(value)})"))

    # structures for the semantics / report renderers
    structures: List[Any] = [{}, [], None, True, 0, "", _random_json(rng, 0),
                             {"backends": []}, {"selection": {}}, {"support_matrix": []},
                             {"backends": None, "selection": "x", "support_matrix": 5},
                             {"selection": {"considered": None}}]
    for _ in range(120):
        structures.append(_random_json(rng, 0))
    for value in structures:
        # Untrusted validators: they must survive any of these.
        out.append(Sample("", "structure-semantics", "stability.semantics", value, "any",
                          "project_semantics"))
        out.append(Sample("", "structure-resolvebundle", "release.resolvebundle", value, "any",
                          "resolve_bundle_root"))
        # Internal consumers: arbitrary JSON is out of contract, so only
        # process-level failure counts against them for these payloads.
        for target in ("report.render", "report.defects", "report.invariants"):
            out.append(Sample("", "structure-report-outofcontract", target, value, "any",
                              target, strict=False))

    # In-contract inputs for the internal consumers: these *must* work, so an
    # exception here is a real defect.
    well_formed_targets = [{
        "source": "live:VirtualWorld --list-backends",
        "command": ["VirtualWorld", "--list-backends", "--json"],
        "exit_code": 0, "ok": False, "error_count": 1, "warning_count": 1,
        "findings": [
            {"severity": "error", "code": "tcg-not-last",
             "message": "'qemu-tcg' is not last", "path": "$.selection.considered",
             "backend": "qemu-tcg"},
            {"severity": "warning", "code": "empty-field", "message": "w", "path": "$.a"},
        ],
        "stderr_excerpt": "",
    }]
    for target in ("report.defects", "report.invariants"):
        out.append(Sample("", "report-incontract", target, well_formed_targets, "any",
                          f"{target} on a well-formed target list", strict=True))
        out.append(Sample("", "report-incontract", target, [], "any",
                          f"{target} on an empty target list", strict=True))

    valid_reports = [
        # The early-return "blocked" shape.
        {"generated_at": "2026-10-05T00:00:00+00:00", "certification_status": "blocked",
         "method": "m", "blocking": {"reason": "ambiguous", "problems": ["p"],
                                     "candidates": ["/a"]}},
        # A full report shape, mirroring what render_markdown consumes.
        _full_report_fixture(),
    ]
    for report_fixture in valid_reports:
        out.append(Sample("", "report-incontract", "report.render", report_fixture, "any",
                          "render_markdown on a well-formed report", strict=True))

    # report targets fed findings-like structures
    findings_shapes: List[Any] = [
        [], [{}], [{"severity": "error"}], [{"severity": "error", "code": "x"}],
        [{"severity": "error", "code": "x", "message": "m", "path": "p"}],
        [{"severity": None, "code": None, "message": None, "path": None}],
        [{"severity": "error", "code": 1, "message": [], "path": {}}],
        {"not": "a list"},
    ]
    for shape in findings_shapes:
        in_contract = (isinstance(shape, list) and all(
            isinstance(entry, dict) and {"severity", "code", "message", "path"} <= set(entry)
            for entry in shape))
        for target, label in (("report.defects", "build_defects"),
                              ("report.invariants", "invariant_statuses")):
            out.append(Sample("", "report-findings", target,
                              [{"source": "s", "command": [], "exit_code": 0, "ok": True,
                                "error_count": 0, "warning_count": 0,
                                "stderr_excerpt": "", "findings": shape}],
                              "any", label, strict=in_contract))

    # classify_candidates inputs (temp files only)
    out.append(Sample("", "release-classify", "release.classify", [], "reject",
                      "no candidates at all"))
    return out


def _short(value: Any, limit: int = 40) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _field_paths(node: Any, prefix: str = "") -> List[str]:
    paths: List[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            paths.append(path)
            paths.extend(_field_paths(value, path))
    elif isinstance(node, list) and node:
        paths.append(prefix + "[]")
        paths.extend(_field_paths(node[0], prefix + "[]"))
    return paths


def _string_paths(node: Any, prefix: str = "") -> List[str]:
    out: List[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, str):
                out.append(path)
            else:
                out.extend(_string_paths(value, path))
    elif isinstance(node, list) and node:
        out.extend(_string_paths(node[0], prefix + "[]"))
    return out


def _paths_matching(node: Any, keys: Iterable[str], prefix: str = "") -> List[str]:
    out: List[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if key in keys:
                out.append(path)
            out.extend(_paths_matching(value, keys, path))
    elif isinstance(node, list) and node:
        out.extend(_paths_matching(node[0], keys, prefix + "[]"))
    return out


def _set_path(document: Any, path: str, value: Any) -> None:
    parts = path.replace("[]", ".-1").split(".")
    node = document
    for part in parts[:-1]:
        if isinstance(node, list):
            index = int(part)
            node = node[index if index >= 0 else 0]
        elif isinstance(node, dict):
            node = node[part]
        else:
            raise KeyError(path)
    last = parts[-1]
    if isinstance(node, list):
        index = int(last)
        node[index if index >= 0 else 0] = value
    elif isinstance(node, dict):
        node[last] = value
    else:
        raise KeyError(path)


def _targeted_swaps(base: Dict[str, Any]) -> List[Tuple[str, Any, str, str]]:
    """Named contract violations, each with the code the contract implies.

    These are the assertions with teeth: the exact code must appear, so a
    detector that starts firing for the wrong reason is caught as well as one
    that stops firing.
    """
    if "verified" in base:
        return [
            ("verified", True, "verified-not-false", "honesty-verified-true"),
            ("real_hardware_verified", True, "real-hardware-verified-not-false",
             "honesty-realhardware-true"),
            ("generated_at", "2026-10-05 04:04:52", "generated-at-invalid",
             "timestamp-not-iso"),
            ("generated_at", "2026-13-45T99:99:99Z", "generated-at-invalid",
             "timestamp-impossible"),
            ("host.os", "Plan9", "host-os-invalid", "host-os-unknown"),
            ("host.arch", "", "empty-field", "host-arch-empty"),
            ("contrib_backends", False, "backend-flag-drift", "contrib-drift"),
            ("backends.linux_kvm", False, "backend-flag-drift", "contrib-drift-backend"),
            ("main_program.sha256", "z" * 64, "main-program-sha256-format", "hash-format"),
            ("main_program.size", -1, "main-program-size-mismatch", "size-wrong"),
            ("qemu_binaries[].path", "/usr/bin/qemu", "qemu-path-absolute", "path-absolute"),
            ("qemu_binaries[].path", "../../etc/passwd", "qemu-path-traversal",
             "path-traversal"),
            ("gate.status", "probably-fine", "gate-status-invalid", "gate-status-unknown"),
            ("gate.failure_count", 5, "gate-failure-count-mismatch", "gate-failure-count"),
            ("gate.status", "passed", None, "gate-still-passed"),
            ("schema", "virtualworld.release/2", "schema-unknown", "schema-unknown"),
            ("firmware_count", -3, "count-not-int", "count-negative"),
            ("support_count", "6", "count-not-int", "count-type"),
        ]
    return [
        ("schema", "not-a-schema", "schema-unknown", "schema-unknown"),
        # An unrecognised guest architecture is tolerated by design, but a
        # document claiming hardware acceleration for a cross-architecture
        # guest is still rejected -- via the cross-arch rule.
        ("guest_arch", "sparc", "cross-arch-not-tcg", "guest-arch-cross-arch"),
        ("guest_arch", "riscv64", "cross-arch-not-tcg", "guest-arch-cross-arch-real"),
        ("host.platform", "plan9", "host-platform-unknown", "host-platform-unknown"),
        ("selection.backend", "qemu-tcg", "selected-mismatch", "selection-mismatch"),
        ("selection.tier", "quantum", "selection-tier-invalid", "selection-tier-invalid"),
        ("selection.execution_tier", "quantum", "execution-tier-invalid",
         "execution-tier-invalid"),
        ("backends[].id", "", "backend-id-empty", "backend-id-empty"),
        ("backends[].tier", "quantum", "backend-tier-invalid", "backend-tier-invalid"),
        ("backends[].status_class", "made-up", "status-class-unknown", "status-class-unknown"),
        ("support_matrix[].verified", 1, "matrix-verified-not-bool", "verified-not-bool"),
        ("build.macos_backend", "yes", "build-value-not-bool", "build-not-bool"),
        ("selection.considered[].reason", "", "empty-field", "reason-empty"),
        ("backends[].id", "qemu-tcg", "backend-id-duplicate", "backend-id-duplicate"),
    ]


def generate_samples(seed: int, target_count: int = DEFAULT_MIN_SAMPLES) -> List[Sample]:
    """Deterministic sample set: identical for a given seed."""
    rng = random.Random(seed)
    samples: List[Sample] = []
    samples += _json_texts(rng, BASE_CONFORMANCE_DOC)
    samples += _document_samples(rng, BASE_CONFORMANCE_DOC, "conformance.check",
                                 "conformance", {})
    samples += _document_samples(rng, release_base_document(), "release.verify",
                                 "release", {})
    samples += _scalar_samples(rng)

    # Top up deterministically with structural fuzz until the floor is met.
    counter = 0
    while len(samples) < target_count:
        counter += 1
        choice = rng.randrange(4)
        if choice == 0:
            samples.append(Sample("", "topup-json-fuzz", "json.parse",
                                  json.dumps(_random_json(rng, 0)), "any", "top-up fuzz"))
        elif choice == 1:
            document = copy.deepcopy(BASE_CONFORMANCE_DOC)
            _scramble(rng, document)
            samples.append(Sample("", "topup-conformance", "conformance.check",
                                  _bound_json(document), "any", "top-up scramble"))
        elif choice == 2:
            document = release_base_document()
            _scramble(rng, document)
            samples.append(Sample("", "topup-release", "release.verify",
                                  _bound_json(document), "any", "top-up scramble"))
        else:
            samples.append(Sample("", "topup-structure", "stability.semantics",
                                  _random_json(rng, 0), "any", "top-up fuzz"))

    for index, sample in enumerate(samples):
        sample.sid = f"{sample.category}-{index:05d}"
    return samples


def _scramble(rng: random.Random, document: Any, depth: int = 0) -> None:
    """Randomly corrupt one leaf of a document in place."""
    if depth > 6:
        return
    if isinstance(document, dict):
        if not document:
            return
        key = rng.choice(list(document.keys()))
        if rng.random() < 0.5 or not isinstance(document[key], (dict, list)):
            document[key] = _random_json(rng, 3)
        else:
            _scramble(rng, document[key], depth + 1)
    elif isinstance(document, list) and document:
        index = rng.randrange(len(document))
        if rng.random() < 0.5 or not isinstance(document[index], (dict, list)):
            document[index] = _random_json(rng, 3)
        else:
            _scramble(rng, document[index], depth + 1)


# ---------------------------------------------------------------------------
# Worker: executes a batch in a subprocess so hangs and crashes are catchable
# ---------------------------------------------------------------------------

def _run_sample(sample: Dict[str, Any]) -> Dict[str, Any]:
    """Execute one sample. Exceptions are classified as failures, never as passes."""
    result: Dict[str, Any] = {"sid": sample["sid"], "category": sample["category"],
                              "target": sample["target"], "expect": sample["expect"],
                              "outcome": "ok", "codes": [], "detail": ""}
    target = TARGET_BY_NAME.get(sample["target"])
    if target is None:  # pragma: no cover - generator only emits known targets
        result["outcome"] = "unknown-target"
        return result
    try:
        raw = target.call(sample["payload"])
    except RecursionError as exc:
        result["outcome"] = "recursion-limit"
        result["detail"] = f"{type(exc).__name__}: {exc}"
        return result
    except MemoryError as exc:
        result["outcome"] = "memory-error"
        result["detail"] = f"{type(exc).__name__}: {exc}"
        return result
    except SystemExit as exc:
        result["outcome"] = "process-exit"
        result["detail"] = f"SystemExit({exc.code})"
        return result
    except KeyboardInterrupt as exc:  # pragma: no cover
        result["outcome"] = "interrupted"
        result["detail"] = str(exc)
        return result
    except Exception as exc:
        # Deliberately classified as a DEFECT for untrusted-input validators.
        # Swallowing this into a pass is exactly what this harness must not do.
        detail = (f"{type(exc).__name__}: {exc}\n"
                  + "".join(traceback.format_exc().splitlines(keepends=True)[-6:]))
        if target.contract == "untrusted" or sample.get("strict", True):
            result["outcome"] = "unhandled-exception"
        else:
            # Structurally out-of-contract input to an internal consumer: the
            # exception is recorded, but as informational rather than a defect.
            result["outcome"] = "out-of-contract-exception"
        result["detail"] = detail
        return result

    try:
        accepted, codes = target.interpret(raw)
    except Exception as exc:
        if target.contract == "untrusted" or sample.get("strict", True):
            result["outcome"] = "unhandled-exception"
        else:
            result["outcome"] = "out-of-contract-exception"
        result["detail"] = f"interpret({type(exc).__name__}): {exc}"
        return result

    result["codes"] = [c for c in codes if isinstance(c, str)]
    if accepted and sample["expect"] == "reject":
        # The contract says this input must be refused and it was not.
        result["outcome"] = "silently-accepted"
    elif not accepted and sample["expect"] == "accept":
        result["outcome"] = "over-rejected"
    elif accepted:
        result["outcome"] = "accepted"
    else:
        expected_code = sample.get("expect_code")
        if expected_code and expected_code not in result["codes"]:
            result["outcome"] = "missing-expected-code"
        else:
            result["outcome"] = "rejected"
    return result


def _worker_main() -> int:
    """Read a batch of samples as JSON on stdin, emit results as JSON on stdout."""
    raw = sys.stdin.read()
    try:
        batch = json.loads(raw)
    except json.JSONDecodeError as exc:  # pragma: no cover
        print(json.dumps({"fatal": f"batch unreadable: {exc}"}))
        return 3
    results = [_run_sample(sample) for sample in batch]
    sys.stdout.write(json.dumps({"results": results}))
    return 0


def run_batch(batch: Sequence[Dict[str, Any]], timeout: float) -> Tuple[str, List[Dict[str, Any]], str]:
    """Run one batch in a subprocess. Returns (status, results, detail)."""
    payload = json.dumps(list(batch))
    try:
        completed = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--worker"],
            input=payload, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return "timeout", [], f"batch exceeded {timeout}s"
    if completed.returncode != 0:
        return "crash", [], (f"worker exited {completed.returncode}; "
                             f"stderr tail: {completed.stderr.strip()[-400:]}")
    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError:
        # Not a traceback-as-pass: an unreadable worker result is a harness-level
        # crash that must be surfaced, not ignored.
        return "crash", [], f"worker produced unreadable output: {completed.stdout[:200]!r}"
    if "fatal" in parsed:
        return "crash", [], parsed["fatal"]
    return "ok", parsed["results"], ""


def execute_samples(samples: Sequence[Sample], batch_size: int,
                    timeout: float) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Run every sample, bisecting any batch that hangs or crashes."""
    results: List[Dict[str, Any]] = []
    incidents: List[Dict[str, Any]] = []
    payloads = [s.to_dict() for s in samples]

    def run_range(items: Sequence[Dict[str, Any]]) -> None:
        status, batch_results, detail = run_batch(items, timeout)
        if status == "ok":
            results.extend(batch_results)
            return
        if len(items) == 1:
            incidents.append({"sid": items[0]["sid"], "category": items[0]["category"],
                              "target": items[0]["target"], "kind": status, "detail": detail,
                              "payload": items[0]["payload"]})
            results.append({"sid": items[0]["sid"], "category": items[0]["category"],
                            "target": items[0]["target"], "expect": items[0]["expect"],
                            "outcome": status, "codes": [], "detail": detail})
            return
        midpoint = len(items) // 2
        run_range(items[:midpoint])
        run_range(items[midpoint:])

    for start in range(0, len(payloads), batch_size):
        run_range(payloads[start:start + batch_size])
    return results, incidents


# ---------------------------------------------------------------------------
# Analysis and minimization
# ---------------------------------------------------------------------------

def analyze(samples: Sequence[Dict[str, Any]], results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    by_sid = {r["sid"]: r for r in results}
    bad_outcomes = {"unhandled-exception", "recursion-limit", "memory-error", "process-exit",
                    "interrupted", "timeout", "crash", "unknown-target",
                    "missing-expected-code"}
    informational: List[Dict[str, Any]] = []
    gaps: List[Dict[str, Any]] = []
    code_counter: Dict[str, int] = {}
    shape_problems: List[Dict[str, Any]] = []

    for sample in samples:
        result = by_sid.get(sample["sid"])
        if result is None:
            gaps.append({"sid": sample["sid"], "category": sample["category"],
                         "target": sample["target"], "kind": "missing-result",
                         "detail": "no result was produced for this sample"})
            continue
        outcome = result["outcome"]
        for code in result.get("codes", []):
            code_counter[code] = code_counter.get(code, 0) + 1
            if not CODE_RE.match(code):
                shape_problems.append({"sid": sample["sid"], "code": code,
                                       "reason": "code does not match ^[a-z][a-z0-9-]*$"})
        if outcome == "out-of-contract-exception":
            informational.append({"sid": sample["sid"], "category": sample["category"],
                                  "target": sample["target"],
                                  "detail": result.get("detail", "")[:300]})
        elif outcome in bad_outcomes:
            gaps.append({"sid": sample["sid"], "category": sample["category"],
                         "target": sample["target"], "kind": outcome,
                         "detail": result.get("detail", "")[:600]})
        elif outcome == "silently-accepted":
            gaps.append({"sid": sample["sid"], "category": sample["category"],
                         "target": sample["target"], "kind": "silently-accepted",
                         "detail": "input that must be rejected was accepted"})
        elif outcome == "missing-expected-code":
            gaps.append({"sid": sample["sid"], "category": sample["category"],
                         "target": sample["target"], "kind": "missing-expected-code",
                         "detail": (f"expected code '{sample.get('expect_code')}' for "
                                    f"{sample.get('note')} but the validator reported "
                                    f"{result.get('codes')}")})
        elif outcome == "over-rejected":
            gaps.append({"sid": sample["sid"], "category": sample["category"],
                         "target": sample["target"], "kind": "over-rejected",
                         "detail": "input that must be tolerated was rejected"})

    categories: Dict[str, Dict[str, int]] = {}
    for sample in samples:
        bucket = categories.setdefault(sample["category"], {"total": 0, "gaps": 0})
        bucket["total"] += 1

    return {"gaps": gaps, "codes": code_counter, "shape_problems": shape_problems,
            "by_sid": by_sid, "categories": categories, "informational": informational}


def minimize_sample(sample: Dict[str, Any], timeout: float,
                    budget: int = MINIMIZE_ATTEMPTS) -> Dict[str, Any]:
    """Shrink a payload while the same failure still reproduces."""
    original = sample["payload"]
    reference = _reproduce(sample, timeout)
    if reference is None:
        return {"payload": original, "attempts": 0, "reproduced": False,
                "note": "failure did not reproduce during minimization"}

    def still_fails(payload: Any) -> bool:
        probe = dict(sample, payload=payload)
        outcome = _reproduce(probe, timeout)
        return outcome is not None and outcome["kind"] == reference["kind"]

    current = original
    attempts = 0
    if isinstance(original, str):
        # Delta-debug by removing chunks, then characters.
        chunk = max(1, len(current) // 2)
        while chunk >= 1 and attempts < budget:
            index = 0
            while index < len(current) and attempts < budget:
                candidate = current[:index] + current[index + chunk:]
                attempts += 1
                if candidate and still_fails(candidate):
                    current = candidate
                else:
                    index += chunk
            chunk //= 2
    elif isinstance(original, dict):
        keys = list(original.keys())
        for key in keys:
            if attempts >= budget:
                break
            candidate = {k: v for k, v in current.items() if k != key}
            attempts += 1
            if candidate and still_fails(candidate):
                current = candidate
        for key in list(current.keys()):
            if attempts >= budget:
                break
            if isinstance(current[key], (list, dict)) and current[key]:
                candidate = dict(current)
                candidate[key] = [] if isinstance(current[key], list) else {}
                attempts += 1
                if still_fails(candidate):
                    current = candidate
    elif isinstance(original, list):
        while len(current) > 1 and attempts < budget:
            attempts += 1
            candidate = current[:len(current) // 2]
            if candidate and still_fails(candidate):
                current = candidate
            else:
                break

    return {"payload": current, "attempts": attempts, "reproduced": True,
            "original_bytes": len(json.dumps(original, ensure_ascii=False, default=str)),
            "minimized_bytes": len(json.dumps(current, ensure_ascii=False, default=str))}


def _reproduce(sample: Dict[str, Any], timeout: float) -> Optional[Dict[str, Any]]:
    status, results, detail = run_batch([sample], timeout)
    if status != "ok" or not results:
        return {"kind": status, "detail": detail}
    outcome = results[0]["outcome"]
    if outcome in ("unhandled-exception", "recursion-limit", "memory-error", "process-exit",
                   "timeout", "crash", "silently-accepted", "over-rejected"):
        return {"kind": outcome, "detail": results[0].get("detail", "")}
    return None


def save_failure(sample: Dict[str, Any], gap: Dict[str, Any], minimization: Dict[str, Any],
                 directory: str) -> str:
    os.makedirs(directory, exist_ok=True)
    safe_category = re.sub(r"[^a-z0-9-]+", "-", gap["category"].lower())
    name = f"{safe_category}--{gap['kind']}--{gap['sid']}.json"
    path = os.path.join(directory, name)
    document = {
        "sid": gap["sid"],
        "category": gap["category"],
        "target": gap["target"],
        "kind": gap["kind"],
        "detail": gap["detail"],
        "original_payload": sample["payload"],
        "minimized_payload": minimization.get("payload"),
        "minimization": {k: v for k, v in minimization.items() if k != "payload"},
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")
    return os.path.relpath(path, PROJECT_ROOT)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def run_hardening(args) -> Dict[str, Any]:
    started = time.perf_counter()
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    tmpdir = tempfile.mkdtemp(prefix="vw-parser-hardening-")
    try:
        for target in build_targets(tmpdir):
            TARGET_BY_NAME[target.name] = target

        samples = generate_samples(args.seed, args.min_samples)
        sample_dicts = [s.to_dict() for s in samples]
        manifest_digest = hashlib.sha256(
            json.dumps(sample_dicts, ensure_ascii=False, sort_keys=True,
                       default=str).encode("utf-8")).hexdigest()

        results, incidents = execute_samples(samples, args.batch_size, args.timeout)
        analysis = analyze(sample_dicts, results)

        # Only *real* detector gaps are minimized and saved.
        saved: List[Dict[str, Any]] = []
        saved_keys = set()
        sample_by_sid = {s["sid"]: s for s in sample_dicts}
        for gap in analysis["gaps"]:
            if gap["kind"] == "missing-result":
                continue
            if gap["sid"] in saved_keys:
                continue
            sample = sample_by_sid.get(gap["sid"])
            if sample is None:
                continue
            minimization = (minimize_sample(sample, args.timeout)
                            if args.minimize else {"payload": sample["payload"],
                                                   "reproduced": False, "note": "not requested"})
            path = save_failure(sample, gap, minimization, FAILURES_DIR)
            saved.append({"sid": gap["sid"], "category": gap["category"], "target": gap["target"],
                          "kind": gap["kind"], "detail": gap["detail"][:300],
                          "minimized_bytes": minimization.get("minimized_bytes"),
                          "sample": path})
            saved_keys.add(gap["sid"])

        known = known_codes()
        unknown_codes = sorted({
            c for c in analysis["codes"]
            if c not in HARNESS_CODES
            and not any(c in codes for codes in known.values())
        })

        elapsed = time.perf_counter() - started
        peak_bytes = max((len(json.dumps(s["payload"], ensure_ascii=False, default=str))
                          for s in sample_dicts), default=0)

        category_coverage = {}
        gap_by_category: Dict[str, int] = {}
        for gap in analysis["gaps"]:
            gap_by_category[gap["category"]] = gap_by_category.get(gap["category"], 0) + 1
        for name, bucket in sorted(analysis["categories"].items()):
            category_coverage[name] = {"samples": bucket["total"],
                                       "gaps": gap_by_category.get(name, 0)}

        report: Dict[str, Any] = {
            "report": "virtualworld-parser-hardening",
            "report_version": "1.0.0",
            "generated_at": started_at,
            "method": ("pure parsing/validation entry points only; no product binary executed, "
                       "no VM command issued; exemptions are never used to convert a failure "
                       "into a pass"),
            "seed": args.seed,
            "reproducible": True,
            "sample_manifest_sha256": manifest_digest,
            "samples": len(sample_dicts),
            "targets": sorted(TARGET_BY_NAME),
            "limits": {
                "max_sample_bytes": MAX_SAMPLE_BYTES,
                "max_depth": MAX_DEPTH,
                "max_array_items": MAX_ARRAY_ITEMS,
                "batch_size": args.batch_size,
                "batch_timeout_seconds": args.timeout,
                "peak_payload_bytes": peak_bytes,
            },
            "elapsed_seconds": round(elapsed, 2),
            "category_coverage": category_coverage,
            "outcomes": _outcome_counts(results),
            "stable_error_codes": {
                "observed": dict(sorted(analysis["codes"].items())),
                "known_vocabulary_size": sum(len(v) for v in known.values()),
                "unknown_codes": unknown_codes,
                "malformed_codes": analysis["shape_problems"],
                "harness_owned_labels": sorted(HARNESS_CODES),
            },
            "incidents": incidents,
            "out_of_contract_exceptions": {
                "count": len(analysis["informational"]),
                "explanation": (
                    "Internal consumers (`make_final_report` renderers) were fed arbitrary "
                    "JSON, which is outside their contract: they only ever receive report "
                    "structures this toolchain builds. Exceptions there are recorded as "
                    "informational; a structurally in-contract payload that raises is still "
                    "counted as a gap, and no untrusted-input validator may appear here."),
                "targets": sorted({e["target"] for e in analysis["informational"]}),
                "samples": analysis["informational"][:20],
            },
            "gaps": saved,
            "gap_count": len(saved),
            "fixes_applied": FIXES_APPLIED,
            "findings": [],
            "not_verified": [
                "Only pure parsing/validation entry points are exercised; live binary runs and "
                "VM behaviour are out of scope.",
                "Exhaustive input-space coverage is impossible; coverage is bounded by the "
                "generator and its documented resource limits.",
                "Resource limits are enforced per sample; aggregate memory pressure across a "
                "whole batch is not measured.",
            ],
        }

        findings: List[Dict[str, Any]] = []
        for gap in saved:
            findings.append({
                "code": f"parser-{gap['kind']}",
                "category": gap["category"],
                "target": gap["target"],
                "sample": gap["sample"],
                "owner": _attribute(gap["target"]),
                "message": f"{gap['kind']} on {gap['target']}: {gap['detail'][:200]}",
            })
        for code in unknown_codes:
            findings.append({
                "code": "parser-unstable-error-code",
                "category": "error-code-stability",
                "target": "n/a",
                "sample": "",
                "owner": "conformance tooling (error-code contract)",
                "message": f"code '{code}' is not in the scanned stable vocabulary",
            })
        report["findings"] = findings
        report["hardening_status"] = "clean" if not findings else "gaps-found"
        return report
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


def _outcome_counts(results: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for result in results:
        counts[result["outcome"]] = counts.get(result["outcome"], 0) + 1
    return dict(sorted(counts.items()))


def _attribute(target: str) -> str:
    if target.startswith("json.") or target.startswith("conformance."):
        return "run_conformance.py"
    if target.startswith("release."):
        return "verify_release_manifest.py"
    if target.startswith("stability."):
        return "run_stability_gate.py"
    if target.startswith("report."):
        return "make_final_report.py"
    return "unknown"


def render_markdown(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# VirtualWorld conformance tools — parser / input robustness hardening")
    lines.append("")
    lines.append(f"- Generated: `{report.get('generated_at', '')}`")
    lines.append(f"- **Status: `{report.get('hardening_status', 'unknown')}`**")
    lines.append(f"- {report.get('method', '')}")
    lines.append("")
    lines.append("## Run")
    lines.append("")
    lines.append(f"- Seed: `{report.get('seed')}` (same seed → identical sample set)")
    lines.append(f"- Sample manifest SHA-256: `{report.get('sample_manifest_sha256')}`")
    lines.append(f"- Samples: **{report.get('samples')}** across {len(report.get('targets', []))} "
                 f"entry points")
    lines.append(f"- Elapsed: {report.get('elapsed_seconds')}s")
    limits = report.get("limits", {})
    lines.append(f"- Limits: ≤{limits.get('max_sample_bytes')} bytes/sample, depth ≤"
                 f"{limits.get('max_depth')}, arrays ≤{limits.get('max_array_items')}, "
                 f"batch {limits.get('batch_size')} at {limits.get('batch_timeout_seconds')}s")
    lines.append(f"- Peak payload: {limits.get('peak_payload_bytes')} bytes")
    lines.append("")
    lines.append("## Entry points under test")
    lines.append("")
    for target in report.get("targets", []):
        lines.append(f"- `{target}`")
    lines.append("")
    lines.append("## Outcomes")
    lines.append("")
    lines.append("| Outcome | Count |")
    lines.append("| --- | --- |")
    for outcome, count in report.get("outcomes", {}).items():
        lines.append(f"| `{outcome}` | {count} |")
    lines.append("")
    lines.append("## Category coverage")
    lines.append("")
    lines.append("| Category | Samples | Gaps |")
    lines.append("| --- | --- | --- |")
    for name, bucket in report.get("category_coverage", {}).items():
        mark = "" if bucket["gaps"] == 0 else " **(gap)**"
        lines.append(f"| `{name}`{mark} | {bucket['samples']} | {bucket['gaps']} |")
    lines.append("")
    codes = report.get("stable_error_codes", {})
    lines.append("## Error-code stability")
    lines.append("")
    lines.append(f"- Distinct codes observed: {len(codes.get('observed', {}))}")
    lines.append(f"- Scanned vocabulary size: {codes.get('known_vocabulary_size')}")
    lines.append(f"- Codes outside the vocabulary: {codes.get('unknown_codes') or 'none'}")
    lines.append(f"- Malformed codes: {codes.get('malformed_codes') or 'none'}")
    lines.append("")
    observed = codes.get("observed", {})
    if observed:
        lines.append("| Code | Occurrences |")
        lines.append("| --- | --- |")
        for code, count in list(observed.items())[:60]:
            lines.append(f"| `{code}` | {count} |")
        lines.append("")
    lines.append("## Harness incidents (crash / timeout bisection)")
    lines.append("")
    incidents = report.get("incidents", [])
    if not incidents:
        lines.append("None: no batch timed out or died.")
    else:
        lines.append("| Sample | Category | Target | Kind |")
        lines.append("| --- | --- | --- | --- |")
        for incident in incidents:
            lines.append(f"| `{incident['sid']}` | {incident['category']} | "
                         f"{incident['target']} | {incident['kind']} |")
    lines.append("")
    ooc = report.get("out_of_contract_exceptions", {})
    lines.append("## Out-of-contract exceptions (informational)")
    lines.append("")
    lines.append(f"- Count: **{ooc.get('count', 0)}** on targets {ooc.get('targets')}")
    lines.append(f"- {ooc.get('explanation', '')}")
    lines.append("")
    lines.append("## Gaps (minimized samples)")
    lines.append("")
    gaps = report.get("gaps", [])
    if not gaps:
        lines.append("None: every sample either parsed/validated cleanly or was rejected with a "
                     "stable code. No unhandled exception, hang, crash or silent acceptance.")
    else:
        lines.append("| Kind | Target | Category | Minimized bytes | Sample |")
        lines.append("| --- | --- | --- | --- | --- |")
        for gap in gaps:
            lines.append(f"| `{gap['kind']}` | `{gap['target']}` | {gap['category']} | "
                         f"{gap.get('minimized_bytes')} | `{gap['sample']}` |")
    lines.append("")
    lines.append("## Attribution")
    lines.append("")
    findings = report.get("findings", [])
    if not findings:
        lines.append("No outstanding gaps to attribute.")
    else:
        lines.append("| Code | Owner | Target | Message |")
        lines.append("| --- | --- | --- | --- |")
        for finding in findings:
            lines.append(f"| `{finding['code']}` | {finding['owner']} | `{finding['target']}` | "
                         f"{finding['message'][:160]} |")
    lines.append("")
    fixes = report.get("fixes_applied", [])
    lines.append("## Defects found by this harness and fixed in the tools")
    lines.append("")
    if not fixes:
        lines.append("None.")
    else:
        lines.append(f"{len(fixes)} defect(s) were found by this hardening run and fixed inside "
                     f"`tools/virtualization-conformance/`. Each has a regression test, so the "
                     f"same input cannot silently start crashing again.")
        lines.append("")
        lines.append("| Module | Site | Symptom | Fix | Regression test |")
        lines.append("| --- | --- | --- | --- | --- |")
        for fix in fixes:
            lines.append(f"| `{fix['module']}` | `{fix['site']}` | {fix['symptom']} | "
                         f"{fix['fix']} | `{fix['regression_test']}` |")
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
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--timeout", type=float, default=DEFAULT_BATCH_TIMEOUT)
    parser.add_argument("--no-minimize", action="store_true")
    parser.add_argument("--out-dir", default=REPORTS_DIR)
    parser.add_argument("--list-samples", action="store_true",
                        help="print the sample manifest digest and exit")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.worker:
        # The worker needs the target registry; build it against a scratch dir.
        scratch = tempfile.mkdtemp(prefix="vw-parser-worker-")
        for target in build_targets(scratch):
            TARGET_BY_NAME[target.name] = target
        return _worker_main()

    args.minimize = not args.no_minimize

    if args.list_samples:
        samples = generate_samples(args.seed, args.min_samples)
        digest = hashlib.sha256(json.dumps([s.to_dict() for s in samples], ensure_ascii=False,
                                           sort_keys=True, default=str).encode()).hexdigest()
        print(f"samples={len(samples)} sha256={digest}")
        return 0

    report = run_hardening(args)
    os.makedirs(args.out_dir, exist_ok=True)
    json_path = os.path.join(args.out_dir, "parser-hardening-report.json")
    md_path = os.path.join(args.out_dir, "parser-hardening-report.md")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(report))

    if not args.quiet:
        print(f"hardening_status={report['hardening_status']} samples={report['samples']} "
              f"gaps={report['gap_count']} elapsed={report['elapsed_seconds']}s")
        print(f"outcomes={report['outcomes']}")
        print(f"wrote {os.path.relpath(json_path, PROJECT_ROOT)}")
        print(f"wrote {os.path.relpath(md_path, PROJECT_ROOT)}")

    return 0 if report["hardening_status"] == "clean" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
