#!/usr/bin/env python3
"""Deterministic stability, concurrency and mutation gate for the certified CLI.

This gate answers three questions about one *pinned* artifact:

1. **Stability** -- does the same read-only command produce the same thing every
   time, run sequentially and under controlled concurrency?
2. **Determinism** -- specifically: identical exit codes, always exactly one JSON
   document on stdout, empty stderr, and no drift in the semantic sequence
   (selection order, backend ids, `status_class`, `execution_tier`, tiers and
   reasons).
3. **Mutation resistance** -- if the document is corrupted in each of the ways the
   contract forbids, does a detector fire with the expected code?

Scope and safety
----------------
* Only two commands are ever executed: ``--list-backends --json`` and
  ``--check --json``. ``run_conformance.run_binary`` refuses anything else, so
  ``--start`` (or any VM-launching command) is unreachable from here.
* The artifact is pinned by SHA-256. If the binary on disk does not hash to the
  expected digest, the gate writes a **blocked** report and exits without running
  anything -- it never falls back to another binary and never rebuilds.
* Nothing is written outside ``tools/virtualization-conformance/reports/``.

Normalization policy
--------------------
The gate can, in principle, ignore volatile fields before comparing runs. It does
not, by default: the certified schema v3 document was found to be **byte-identical
across runs**, so ``NORMALIZED_FIELDS`` is intentionally empty. That is the
strongest available determinism claim, and it is re-verified every run: if bytes
diverge the gate reports it rather than filtering it. A field may only be listed
here if it is a generation timestamp, and doing so is recorded in the report.

Exit codes: 0 = gate passed, 1 = gate found a problem, 2 = blocked (SHA mismatch,
missing binary, usage error).
"""

from __future__ import annotations

import argparse
import copy
import datetime
import hashlib
import json
import os
import random
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import run_conformance as rc  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(HERE))
DEFAULT_BINARY = os.path.join(
    PROJECT_ROOT, "build-final/VirtualWorld.app/Contents/MacOS/VirtualWorld"
)
DEFAULT_SHA256 = "238f603d3d1d1db3c8b574f9617d0632f0f6bcbfbe4c44d923917b6dda60b80f"
DEFAULT_SCHEMA = "virtualworld.virtualization/3"
DEFAULT_ARCHES = ("x86_64", "aarch64", "riscv64")
COMMANDS = ("--list-backends", "--check")
DEFAULT_ROUNDS = 50
DEFAULT_CONCURRENCY = 8
DEFAULT_TIMEOUT = 90.0
DEFAULT_SEED = 20261005

REPORTS_DIR = os.path.join(HERE, "reports")
FAILURES_DIR = os.path.join(REPORTS_DIR, "stability-failures")
STABILITY_JSON = os.path.join(REPORTS_DIR, "stability-report.json")
STABILITY_MD = os.path.join(REPORTS_DIR, "stability-report.md")

# Deliberately empty: schema v3 output is byte-stable, so nothing may be filtered.
# See the module docstring -- any entry here must be a generation timestamp and is
# reported verbatim in the output so a reviewer can challenge it.
NORMALIZED_FIELDS: Tuple[str, ...] = ()

# Fields whose *semantic sequence* must not drift between runs.
DRIFT_FIELDS = (
    "selection.backend",
    "selection.tier",
    "selection.execution_tier",
    "selection_order",
    "backend_ids",
    "tier_by_backend",
    "execution_tier_by_backend",
    "status_class_by_backend",
    "status_by_backend",
    "reason_by_considered",
    "support_matrix_digest",
)


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------

class Invocation:
    __slots__ = ("command", "arch", "phase", "round_index", "returncode", "duration_ms",
                 "stdout", "stderr", "timed_out", "document", "parse_error", "started_at")

    def __init__(self, command: str, arch: str, phase: str, round_index: int) -> None:
        self.command = command
        self.arch = arch
        self.phase = phase
        self.round_index = round_index
        self.returncode: Optional[int] = None
        self.duration_ms: float = 0.0
        self.stdout: str = ""
        self.stderr: str = ""
        self.timed_out = False
        self.document: Optional[Any] = None
        self.parse_error: Optional[str] = None
        self.started_at: str = ""

    @property
    def variant(self) -> str:
        return f"{self.command} {self.arch}"

    @property
    def signal(self) -> Optional[int]:
        """Signal number when the child was killed by a signal, else None."""
        if self.returncode is not None and self.returncode < 0:
            return -self.returncode
        return None

    @property
    def crashed(self) -> bool:
        return self.signal is not None

    def to_dict(self, include_payload: bool = False) -> Dict[str, Any]:
        data = {
            "variant": self.variant,
            "phase": self.phase,
            "round": self.round_index,
            "returncode": self.returncode,
            "signal": self.signal,
            "timed_out": self.timed_out,
            "duration_ms": round(self.duration_ms, 2),
            "stdout_bytes": len(self.stdout),
            "stderr_bytes": len(self.stderr),
            "parse_error": self.parse_error,
        }
        if include_payload:
            data["stderr"] = self.stderr[:2000]
        return data


def run_variant_once(binary: str, command: str, arch: str, phase: str, round_index: int,
                     timeout: float) -> Invocation:
    """Execute one read-only invocation and capture everything about it."""
    invocation = Invocation(command, arch, phase, round_index)
    invocation.started_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    env = dict(os.environ)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")

    argv = [binary, command, arch, "--json"]
    if command not in COMMANDS:  # pragma: no cover - defended by callers
        raise ValueError(f"refusing to run non-reporting command {command!r}")

    started = time.perf_counter()
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                   env=env, check=False)
        invocation.returncode = completed.returncode
        invocation.stdout = completed.stdout
        invocation.stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        invocation.timed_out = True
        invocation.stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        invocation.stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
    except OSError as exc:  # pragma: no cover - environment specific
        invocation.parse_error = f"failed to execute: {exc}"
    finally:
        invocation.duration_ms = (time.perf_counter() - started) * 1000.0

    if not invocation.timed_out and invocation.parse_error is None:
        document, parse_error = rc.parse_single_json_document(invocation.stdout)
        invocation.document = document
        invocation.parse_error = parse_error
    return invocation


# ---------------------------------------------------------------------------
# Semantic projection
# ---------------------------------------------------------------------------

def _as_dict(value: Any) -> Dict[str, Any]:
    """Return `value` when it really is a mapping, else an empty mapping."""
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[Any]:
    """Return `value` when it really is a list, else an empty list.

    `dict.get(key, [])` only defaults when the key is *absent*; a key present
    with JSON `null` yields None and breaks any iteration over it.
    """
    return value if isinstance(value, list) else []


def canonical_json(value: Any) -> str:
    """Key-order-insensitive canonical form, so key reordering is not mistaken
    for a value change (and value changes are never hidden)."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def project_semantics(document: Any) -> Dict[str, Any]:
    """Extract the sequence/identity facts that must not drift between runs."""
    if not isinstance(document, dict):
        return {}
    backends = [b for b in _as_list(document.get("backends")) if isinstance(b, dict)]
    selection = _as_dict(document.get("selection"))
    considered = [c for c in _as_list(selection.get("considered")) if isinstance(c, dict)]
    matrix = _as_list(document.get("support_matrix"))
    return {
        "selection.backend": selection.get("backend"),
        "selection.tier": selection.get("tier"),
        "selection.execution_tier": selection.get("execution_tier"),
        "selection_order": [c.get("id") for c in considered],
        "backend_ids": [b.get("id") for b in backends],
        "tier_by_backend": {b.get("id"): b.get("tier") for b in backends},
        "execution_tier_by_backend": {b.get("id"): b.get("execution_tier") for b in backends},
        "status_class_by_backend": {b.get("id"): b.get("status_class") for b in backends},
        "status_by_backend": {b.get("id"): b.get("status") for b in backends},
        "reason_by_considered": {c.get("id"): c.get("reason") for c in considered},
        "support_matrix_digest": hashlib.sha256(canonical_json(matrix).encode("utf-8")).hexdigest(),
    }


# ---------------------------------------------------------------------------
# Per-variant determinism analysis
# ---------------------------------------------------------------------------

def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile, so a small sample never invents a value."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return round(ordered[index], 2)


def duration_stats(values: Sequence[float]) -> Dict[str, float]:
    if not values:
        return {"min": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    return {
        "min": round(min(values), 2),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": round(max(values), 2),
    }


def analyze_variant(variant: str, runs: Sequence[Invocation],
                    expected_exit_codes: Sequence[int]) -> Dict[str, Any]:
    """Reduce a set of runs of one variant to stability findings."""
    findings: List[Dict[str, Any]] = []

    def fail(code: str, message: str, evidence: Any = None) -> None:
        entry = {"code": code, "message": message}
        if evidence is not None:
            entry["evidence"] = evidence
        findings.append(entry)

    if not runs:
        fail("no-runs", "variant produced no invocations")
        return {"variant": variant, "runs": 0, "ok": False, "findings": findings}

    stats = {
        "variant": variant,
        "runs": len(runs),
        "ok": True,
        "exit_codes": sorted({r.returncode for r in runs}, key=lambda x: (x is None, x)),
        "timed_out": sum(1 for r in runs if r.timed_out),
        "crashed": sum(1 for r in runs if r.crashed),
        "signals": sorted({r.signal for r in runs if r.signal is not None}),
        "single_json_failures": sum(1 for r in runs if r.parse_error),
        "stderr_nonempty": sum(1 for r in runs if r.stderr.strip()),
        "duration_ms": duration_stats([r.duration_ms for r in runs]),
    }

    for run in runs:
        if run.timed_out:
            fail("invocation-timeout",
                 f"round {run.round_index} exceeded the timeout", run.to_dict(True))
        if run.crashed:
            fail("invocation-crashed",
                 f"round {run.round_index} died from signal {run.signal}", run.to_dict(True))
        if run.returncode is not None and run.returncode not in expected_exit_codes:
            fail("unexpected-exit-code",
                 f"round {run.round_index} exited {run.returncode}, "
                 f"expected one of {list(expected_exit_codes)}", run.to_dict())
        if run.parse_error:
            fail("stdout-not-single-json",
                 f"round {run.round_index}: {run.parse_error}", run.to_dict(True))
        if run.stderr.strip():
            fail("stderr-not-empty",
                 f"round {run.round_index} wrote {len(run.stderr)} bytes to stderr",
                 run.to_dict(True))

    if stats["crashed"]:
        fail("crashes-detected", f"{stats['crashed']} crashed invocation(s)")
    if stats["timed_out"]:
        fail("timeouts-detected", f"{stats['timed_out']} timed-out invocation(s)")

    # ---- byte level ------------------------------------------------------
    byte_forms = {hashlib.sha256(r.stdout.encode("utf-8")).hexdigest() for r in runs
                  if r.parse_error is None}
    stats["distinct_stdout_hashes"] = len(byte_forms)
    stats["byte_identical"] = len(byte_forms) == 1
    if len(byte_forms) > 1:
        fail("stdout-bytes-differ",
             f"{len(byte_forms)} distinct stdout byte sequences across {len(runs)} runs; "
             f"with NORMALIZED_FIELDS={list(NORMALIZED_FIELDS)} no field is exempt")

    # ---- canonical JSON level -------------------------------------------
    canonical_forms = {canonical_json(r.document) for r in runs if r.document is not None}
    stats["distinct_canonical_hashes"] = len(canonical_forms)
    stats["canonical_json_identical"] = len(canonical_forms) == 1
    if len(canonical_forms) > 1:
        fail("stdout-json-differs",
             f"{len(canonical_forms)} distinct canonical JSON documents "
             f"(key-order-insensitive); a real value changed between runs")
    elif not stats["byte_identical"]:
        # Canonically equal but byte-different means key ordering moved.
        fail("stdout-key-order-unstable",
             "canonical JSON matches but byte output differs: key ordering is not stable")

    # ---- semantic sequence drift ----------------------------------------
    projections = [project_semantics(r.document) for r in runs if r.document is not None]
    drift: Dict[str, List[Any]] = {}
    for field in DRIFT_FIELDS:
        values = {canonical_json(p.get(field)) for p in projections}
        if len(values) > 1:
            drift[field] = sorted(values)
    stats["drift_fields"] = sorted(drift)
    if drift:
        fail("semantic-drift",
             f"these fields changed between runs: {sorted(drift)}",
             {k: v[:3] for k, v in drift.items()})

    stats["findings"] = findings
    stats["ok"] = not findings
    return stats


# ---------------------------------------------------------------------------
# Mutation suite
# ---------------------------------------------------------------------------

def _find_backend(doc: Dict[str, Any], backend_id: str) -> Optional[Dict[str, Any]]:
    for backend in _as_list(doc.get("backends")):
        if isinstance(backend, dict) and backend.get("id") == backend_id:
            return backend
    return None


def _reorder_selection(doc: Dict[str, Any]) -> None:
    considered = _as_list(_as_dict(doc.get("selection")).get("considered"))
    considered.sort(key=lambda c: (rc.TIER_RANK.get(c.get("tier"), 9), str(c.get("id"))))


def build_mutations(base: Dict[str, Any], seed: int) -> List[Dict[str, Any]]:
    """Build the deterministic mutation suite.

    Both hand-specified mutations (one per required contract violation) and
    seeded random mutations (to widen coverage across backends and arch rows)
    are produced. Same seed -> identical suite, always.
    """
    rng = random.Random(seed)
    mutations: List[Dict[str, Any]] = []

    def add(name: str, category: str, expected: Sequence[str],
            apply: Callable[[Dict[str, Any]], None]) -> None:
        mutations.append({"name": name, "category": category,
                          "expected_codes": list(expected), "apply": apply})

    # --- required categories ---------------------------------------------
    def duplicate_id(doc: Dict[str, Any]) -> None:
        if len(doc["backends"]) >= 2:
            doc["backends"][1]["id"] = doc["backends"][0]["id"]

    add("duplicate-backend-id", "duplicate-id", ["backend-id-duplicate"], duplicate_id)

    for field in ("schema", "host", "guest_arch", "build", "backends", "selection", "support_matrix"):
        def drop(doc: Dict[str, Any], field=field) -> None:
            doc.pop(field, None)
        add(f"missing-document-field-{field}", "missing-field", ["missing-field"], drop)

    def drop_backend_field(doc: Dict[str, Any]) -> None:
        for backend in doc["backends"]:
            backend.pop("status_class", None)
        doc["backends"][0].pop("tier", None)

    add("missing-backend-field-tier", "missing-field", ["missing-field"], drop_backend_field)

    def wrong_type_bool(doc: Dict[str, Any]) -> None:
        doc["backends"][0]["host_supported"] = 1

    add("wrong-type-boolean-as-int", "wrong-type", ["wrong-field-type"], wrong_type_bool)

    def wrong_type_issues(doc: Dict[str, Any]) -> None:
        doc["backends"][0]["issues"] = "not-an-array"

    add("wrong-type-issues-as-string", "wrong-type", ["backend-issues-invalid"], wrong_type_issues)

    def unknown_tier(doc: Dict[str, Any]) -> None:
        doc["backends"][0]["tier"] = "quantum"

    add("unknown-backend-tier", "unknown-tier", ["backend-tier-invalid"], unknown_tier)

    def unknown_tier_considered(doc: Dict[str, Any]) -> None:
        doc["selection"]["considered"][0]["tier"] = "quantum"

    add("unknown-considered-tier", "unknown-tier", ["considered-tier-invalid"], unknown_tier_considered)

    def unknown_status_class(doc: Dict[str, Any]) -> None:
        doc["backends"][0]["status_class"] = "definitely-not-a-class"

    add("unknown-status-class", "unknown-status-class", ["status-class-unknown"], unknown_status_class)

    def tcg_not_last(doc: Dict[str, Any]) -> None:
        """Insert a decoy candidate *after* qemu-tcg.

        Note the baseline already has qemu-tcg last, so "move it to the end" is a
        no-op and would prove nothing. Appending an extra emulation-tier candidate
        whose id sorts after "qemu-tcg" violates only the "TCG must be last"
        invariant while keeping id uniqueness, membership and tier ordering
        intact -- so the expected code is unambiguous.
        """
        decoy_id = "zz-decoy-after-tcg"
        doc["backends"].append({
            "id": decoy_id,
            "name": "decoy",
            "tier": "emulation",
            "execution_tier": "emulation",
            "host_supported": True,
            "guest_arch_supported": True,
            "hardware_acceleration": False,
            "native_execution": False,
            "accelerator": "tcg",
            "status": "available",
            "status_class": "available",
            "summary": "decoy inserted after qemu-tcg",
            "issues": [{"level": "info", "code": "decoy", "message": "decoy"}],
            "requirements": [],
            "runs_in_process": False,
        })
        doc["selection"]["considered"].append({
            "id": decoy_id,
            "tier": "emulation",
            "host_supported": True,
            "guest_arch_supported": True,
            "config_accepted": False,
            "selected": False,
            "status": "lower-priority",
            "reason": "decoy candidate placed after qemu-tcg",
        })

    add("tcg-not-last", "tcg-ordering", ["tcg-not-last"], tcg_not_last)

    def tcg_missing(doc: Dict[str, Any]) -> None:
        doc["backends"] = [b for b in doc["backends"] if b.get("id") != rc.TCG_BACKEND_ID]
        doc["selection"]["considered"] = [
            c for c in doc["selection"]["considered"] if c.get("id") != rc.TCG_BACKEND_ID
        ]

    add("tcg-missing", "tcg-ordering", ["tcg-missing"], tcg_missing)

    def cross_arch_acceleration(doc: Dict[str, Any]) -> None:
        doc["support_matrix"].append({
            "host_platform": "macos", "host_arch": "aarch64", "guest_arch": "x86_64",
            "backend": "macos-virtualization", "tier": "native", "status": "native",
            "verified": False, "note": "mutated: cross-architecture acceleration",
        })

    add("cross-arch-acceleration", "cross-arch", ["matrix-cross-arch-acceleration"],
        cross_arch_acceleration)

    def cross_arch_selected(doc: Dict[str, Any]) -> None:
        doc["preference"] = "auto"
        doc["host"]["arch"] = "aarch64"
        doc["guest_arch"] = "riscv64"
        for entry in doc["selection"]["considered"]:
            entry["selected"] = entry.get("id") == "qemu-hardware"
        doc["selection"]["backend"] = "qemu-hardware"
        doc["selection"]["tier"] = "hardware"

    add("cross-arch-not-tcg", "cross-arch", ["cross-arch-not-tcg"], cross_arch_selected)

    def harmony_native(doc: Dict[str, Any]) -> None:
        backend = _find_backend(doc, "harmony-virtualization")
        if backend is not None:
            backend["tier"] = "native"
            backend["native_execution"] = True

    add("harmony-claims-native", "harmony-honesty",
        ["compat-backend-claims-native", "compat-backend-claims-native-execution"], harmony_native)

    def harmony_native_matrix(doc: Dict[str, Any]) -> None:
        for row in doc["support_matrix"]:
            if row.get("backend") == "harmony-virtualization":
                row["tier"] = "native"
                break

    add("harmony-matrix-native", "harmony-honesty", ["matrix-compat-row-claims-native"],
        harmony_native_matrix)

    def empty_reason(doc: Dict[str, Any]) -> None:
        for entry in doc["selection"]["considered"]:
            if not entry.get("selected"):
                entry["reason"] = ""
                break

    add("empty-candidate-reason", "empty-reason", ["empty-field"], empty_reason)

    def selection_mismatch(doc: Dict[str, Any]) -> None:
        doc["selection"]["backend"] = "qemu-tcg"
        for entry in doc["selection"]["considered"]:
            entry["selected"] = entry.get("id") == "qemu-hardware"

    add("selection-backend-mismatch", "selection-inconsistency", ["selected-mismatch"],
        selection_mismatch)

    def selection_none(doc: Dict[str, Any]) -> None:
        for entry in doc["selection"]["considered"]:
            entry["selected"] = False

    add("selection-none-selected", "selection-inconsistency", ["considered-none-selected"],
        selection_none)

    def selection_multiple(doc: Dict[str, Any]) -> None:
        for entry in doc["selection"]["considered"][:2]:
            entry["selected"] = True

    add("selection-multiple-selected", "selection-inconsistency", ["considered-multiple-selected"],
        selection_multiple)

    def selection_unsupported(doc: Dict[str, Any]) -> None:
        for entry in doc["selection"]["considered"]:
            if entry.get("selected"):
                entry["host_supported"] = False

    add("selection-unsupported-backend", "selection-inconsistency",
        ["selected-backend-unsupported"], selection_unsupported)

    def selection_order_broken(doc: Dict[str, Any]) -> None:
        considered = doc["selection"]["considered"]
        considered.sort(key=lambda c: -rc.TIER_RANK.get(c.get("tier"), 0))

    add("selection-order-nonmonotonic", "selection-ordering",
        ["considered-order-nonmonotonic"], selection_order_broken)

    for label, value in (("int-one", 1), ("string-true", "true"), ("null", None)):
        def bad_verified(doc: Dict[str, Any], value=value) -> None:
            doc["support_matrix"][0]["verified"] = value
        add(f"verified-pseudo-true-{label}", "verified-type",
            ["matrix-verified-not-bool"], bad_verified)

    def schema_unknown(doc: Dict[str, Any]) -> None:
        doc["schema"] = "virtualworld.virtualization/99x"

    add("schema-version-not-numeric", "schema", ["schema-version-invalid"], schema_unknown)

    def build_flag_not_bool(doc: Dict[str, Any]) -> None:
        doc["build"]["harmony_backend"] = "yes"

    add("build-flag-not-bool", "wrong-type", ["build-value-not-bool"], build_flag_not_bool)

    # --- seeded random mutations -----------------------------------------
    # The seed chooses *which backend* a mutation targets, never *whether* the
    # injected value is invalid: every randomized mutation below injects a value
    # that is unambiguously illegal for its field, so the expected detector code
    # is deterministic and a gap can only ever mean a real detection failure.
    backend_ids = [b.get("id") for b in _as_list(base.get("backends")) if isinstance(b, dict)]
    invalid_tiers = ("quantum", "TURBO", "", "native ", "Native")
    invalid_status_classes = ("made-up-class", "available ", "AVAILABLE", "degraded")

    for index in range(12):
        target = rng.choice(backend_ids) if backend_ids else None
        invalid_tier = rng.choice(invalid_tiers)
        invalid_status = rng.choice(invalid_status_classes)

        def random_tier(doc: Dict[str, Any], target=target, value=invalid_tier) -> None:
            backend = _find_backend(doc, target) if target else None
            if backend is not None:
                backend["tier"] = value

        add(f"random-{index:02d}-tier", "randomized", ["backend-tier-invalid"], random_tier)

        def random_execution_tier(doc: Dict[str, Any], target=target, value=invalid_tier) -> None:
            backend = _find_backend(doc, target) if target else None
            if backend is not None:
                backend["execution_tier"] = value

        add(f"random-{index:02d}-execution-tier", "randomized",
            ["execution-tier-invalid"], random_execution_tier)

        def random_status_class(doc: Dict[str, Any], target=target, value=invalid_status) -> None:
            backend = _find_backend(doc, target) if target else None
            if backend is not None:
                backend["status_class"] = value

        add(f"random-{index:02d}-status-class", "randomized",
            ["status-class-unknown"], random_status_class)

    # Type-confusion variants, kept separate so each has exactly one expected code.
    for index in range(6):
        target = rng.choice(backend_ids) if backend_ids else None
        field = rng.choice(("status_class", "execution_tier"))
        bad_value = rng.choice((7, None, [], {}))

        def random_wrong_type(doc: Dict[str, Any], target=target, field=field,
                              bad_value=bad_value) -> None:
            backend = _find_backend(doc, target) if target else None
            if backend is not None:
                backend[field] = bad_value

        expected = "status-class-invalid" if field == "status_class" else "execution-tier-invalid"
        add(f"random-{index:02d}-{field}-wrong-type", "randomized", [expected], random_wrong_type)

    return mutations


def run_mutation_suite(base_document: Dict[str, Any], seed: int,
                       failures_dir: Optional[str]) -> Dict[str, Any]:
    """Apply every mutation and require the named detector(s) to fire."""
    mutations = build_mutations(base_document, seed)
    results: List[Dict[str, Any]] = []
    gaps: List[Dict[str, Any]] = []

    if failures_dir:
        os.makedirs(failures_dir, exist_ok=True)

    for mutation in mutations:
        document = copy.deepcopy(base_document)
        try:
            mutation["apply"](document)
        except Exception as exc:  # pragma: no cover - defensive
            results.append({
                "name": mutation["name"], "category": mutation["category"],
                "expected_codes": mutation["expected_codes"], "detected_codes": [],
                "detector_fired": False, "apply_error": str(exc),
            })
            gaps.append({"name": mutation["name"], "reason": f"mutation failed to apply: {exc}"})
            continue

        report = rc.check_document(document, source=f"mutation:{mutation['name']}")
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
            "sample": f"stability-failures/{sample_name}" if failures_dir else "",
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
        "total": len(results),
        "detectors_fired": sum(1 for r in results if r["detector_fired"]),
        "gaps": gaps,
        "coverage_by_category": categories,
        "results": results,
        "ok": not gaps,
        "normalization": list(NORMALIZED_FIELDS),
    }


# ---------------------------------------------------------------------------
# Gate orchestration
# ---------------------------------------------------------------------------

def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_artifact(binary: str, expected_sha: str) -> Dict[str, Any]:
    if not binary:
        return {"ok": False, "reason": "no binary path was provided"}
    if not os.path.isfile(binary):
        return {"ok": False, "reason": f"binary not found: {binary}", "actual_sha256": ""}
    if not os.access(binary, os.X_OK):
        return {"ok": False, "reason": f"binary is not executable: {binary}"}
    actual = sha256_file(binary)
    return {
        "ok": actual == expected_sha,
        "reason": "" if actual == expected_sha else (
            f"binary SHA-256 is {actual} but the certified digest is {expected_sha}; "
            f"refusing to run against a different artifact"
        ),
        "actual_sha256": actual,
        "expected_sha256": expected_sha,
        "path": binary,
        "size_bytes": os.path.getsize(binary),
        "mtime": datetime.datetime.fromtimestamp(os.path.getmtime(binary)).isoformat(timespec="seconds"),
    }


def run_gate(args) -> Dict[str, Any]:
    started_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    binary = os.path.abspath(args.binary) if args.binary else ""
    artifact = verify_artifact(binary, args.expected_sha256)

    report: Dict[str, Any] = {
        "report": "virtualworld-virtualization-stability-gate",
        "report_version": "1.0.0",
        "generated_at": started_at,
        "method": "read-only black-box; only --list-backends/--check are executed; no VM is started",
        "normalization": {
            "fields": list(NORMALIZED_FIELDS),
            "rationale": ("schema v3 output was verified byte-identical across runs, so no field "
                          "is exempted from comparison; any divergence is reported rather than "
                          "filtered"),
        },
        "artifact": artifact,
        "settings": {
            "commands": list(COMMANDS),
            "arches": list(args.arches),
            "sequential_rounds": args.rounds,
            "concurrent_rounds": args.rounds,
            "concurrency": args.concurrency,
            "timeout_seconds": args.timeout,
            "seed": args.seed,
        },
    }

    if not artifact["ok"]:
        report["gate_status"] = "blocked"
        report["blocking"] = {
            "reason": artifact["reason"],
            "policy": ("the gate is pinned to one certified SHA-256; it does not fall back to "
                       "another binary and does not rebuild the product"),
        }
        report["stability"] = {"variants": [], "summary": {}}
        report["mutation"] = {"total": 0, "gaps": [], "results": []}
        report["failures"] = []
        report["not_verified"] = []
        return report

    # ---------------- sequential phase ----------------------------------
    sequential: List[Invocation] = []
    for command in COMMANDS:
        for arch in args.arches:
            for round_index in range(1, args.rounds + 1):
                sequential.append(run_variant_once(binary, command, arch, "sequential",
                                                   round_index, args.timeout))
    report["stability"] = {"variants": [], "summary": {}}

    # ---------------- concurrent phase ----------------------------------
    concurrent: List[Invocation] = []
    lock = threading.Lock()
    tasks: List[Tuple[str, str, int]] = []
    for command in COMMANDS:
        for arch in args.arches:
            for round_index in range(1, args.rounds + 1):
                tasks.append((command, arch, round_index))

    def worker(task: Tuple[str, str, int]) -> Invocation:
        command, arch, round_index = task
        return run_variant_once(binary, command, arch, "concurrent", round_index, args.timeout)

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        for invocation in pool.map(worker, tasks):
            with lock:
                concurrent.append(invocation)

    # ---------------- reduce --------------------------------------------
    variants: List[Dict[str, Any]] = []
    for command in COMMANDS:
        for arch in args.arches:
            expected_codes = (0,) if command == "--list-backends" else (0, 1)
            for phase, runs in (("sequential", sequential), ("concurrent", concurrent)):
                subset = [r for r in runs if r.command == command and r.arch == arch]
                analysis = analyze_variant(f"{command} {arch}", subset, expected_codes)
                analysis["phase"] = phase
                analysis["expected_exit_codes"] = list(expected_codes)
                variants.append(analysis)

    total_runs = len(sequential) + len(concurrent)
    all_findings = [dict(f, variant=v["variant"], phase=v["phase"])
                    for v in variants for f in v["findings"]]
    summary = {
        "invocations": total_runs,
        "sequential_invocations": len(sequential),
        "concurrent_invocations": len(concurrent),
        "variants": len(variants),
        "variants_ok": sum(1 for v in variants if v["ok"]),
        "findings": len(all_findings),
        "crashes": sum(1 for r in sequential + concurrent if r.crashed),
        "timeouts": sum(1 for r in sequential + concurrent if r.timed_out),
        "stderr_nonempty": sum(1 for r in sequential + concurrent if r.stderr.strip()),
        # Pooled percentiles across phases would be misleading: the concurrent
        # phase deliberately runs `concurrency` processes at once. Report both.
        "duration_ms": duration_stats([r.duration_ms for r in sequential + concurrent]),
        "duration_ms_by_phase": {
            "sequential": duration_stats([r.duration_ms for r in sequential]),
            "concurrent": duration_stats([r.duration_ms for r in concurrent]),
        },
        "byte_identical_variants": sum(1 for v in variants if v.get("byte_identical")),
        "ok": not all_findings,
    }
    report["stability"] = {
        "variants": variants,
        "summary": summary,
        "findings": all_findings,
    }

    # ---------------- mutation suite ------------------------------------
    if args.skip_mutations:
        report["mutation"] = {"skipped": True, "ok": True, "total": 0, "gaps": [], "results": []}
    else:
        base_document = None
        for invocation in sequential:
            if invocation.command == "--list-backends" and invocation.document is not None:
                base_document = invocation.document
                break
        if base_document is None:
            report["mutation"] = {
                "ok": False, "total": 0, "results": [], "gaps": [
                    {"name": "<baseline>", "reason": "no parseable baseline document was captured"}
                ],
            }
        else:
            report["mutation"] = run_mutation_suite(base_document, args.seed, FAILURES_DIR)

    # ---------------- failures / attribution ----------------------------
    failures: List[Dict[str, Any]] = []
    for finding in all_findings:
        failures.append({
            "code": finding["code"],
            "message": finding["message"],
            "variant": finding["variant"],
            "phase": finding["phase"],
            "owner": "product under test",
        })
    for gap in report["mutation"].get("gaps", []):
        failures.append({
            "code": "mutation-detector-gap",
            "message": f"{gap['name']}: {gap['reason']}",
            "variant": "mutation-suite",
            "phase": "mutation",
            "owner": "conformance tooling (detector gap)",
        })
    report["failures"] = failures

    report["not_verified"] = [
        "No guest was started: only --list-backends and --check were executed.",
        "Stability is demonstrated only on this macOS/Apple Silicon host; Windows, Linux "
        "and HarmonyOS hosts remain covered by synthetic fixtures, not real runs.",
        "Byte-level determinism across different machines, locales or Qt builds is untested.",
        "The artifact is pinned by SHA-256; a rebuild invalidates this report by design.",
    ]

    gate_ok = summary["ok"] and report["mutation"].get("ok", True) and not failures
    report["gate_status"] = "passed" if gate_ok else "failed"
    report["blocking"] = None
    return report


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def render_markdown(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# VirtualWorld virtualization — stability, concurrency and mutation gate")
    lines.append("")
    lines.append(f"- Generated: `{report.get('generated_at', '')}`")
    lines.append(f"- **Gate status: `{report.get('gate_status', 'unknown')}`**")
    lines.append(f"- {report.get('method', '')}")
    lines.append("")

    artifact = report.get("artifact", {})
    lines.append("## Pinned artifact")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Path | `{artifact.get('path', '')}` |")
    lines.append(f"| SHA-256 (expected) | `{artifact.get('expected_sha256', '')}` |")
    lines.append(f"| SHA-256 (observed) | `{artifact.get('actual_sha256', '')}` |")
    lines.append(f"| Digest match | **{artifact.get('ok')}** |")
    lines.append(f"| Size | {artifact.get('size_bytes', 0)} bytes |")
    lines.append(f"| mtime | `{artifact.get('mtime', '')}` |")
    lines.append("")

    blocking = report.get("blocking")
    if blocking:
        lines.append("## Blocked")
        lines.append("")
        lines.append(f"- {blocking.get('reason', '')}")
        lines.append(f"- Policy: {blocking.get('policy', '')}")
        lines.append("")
        return "\n".join(lines) + "\n"

    settings = report.get("settings", {})
    lines.append("## Settings")
    lines.append("")
    lines.append(f"- Commands: `{settings.get('commands')}`")
    lines.append(f"- Architectures: `{settings.get('arches')}`")
    lines.append(f"- Rounds per variant: sequential **{settings.get('sequential_rounds')}**, "
                 f"concurrent **{settings.get('concurrent_rounds')}** "
                 f"at concurrency **{settings.get('concurrency')}**")
    lines.append(f"- Timeout: {settings.get('timeout_seconds')}s, mutation seed: "
                 f"`{settings.get('seed')}`")
    norm = report.get("normalization", {})
    lines.append(f"- Normalized fields: `{norm.get('fields')}` — {norm.get('rationale', '')}")
    lines.append("")

    summary = report["stability"]["summary"]
    lines.append("## Stability")
    lines.append("")
    lines.append(f"- Invocations: **{summary.get('invocations')}** "
                 f"({summary.get('sequential_invocations')} sequential + "
                 f"{summary.get('concurrent_invocations')} concurrent)")
    lines.append(f"- Variants OK: {summary.get('variants_ok')} / {summary.get('variants')}")
    lines.append(f"- Crashes: {summary.get('crashes')}, timeouts: {summary.get('timeouts')}, "
                 f"non-empty stderr: {summary.get('stderr_nonempty')}")
    lines.append(f"- Byte-identical variants: {summary.get('byte_identical_variants')} / "
                 f"{summary.get('variants')}")
    d = summary.get("duration_ms", {})
    lines.append(f"- Duration ms (pooled): min {d.get('min')}, p50 {d.get('p50')}, "
                 f"p95 {d.get('p95')}, p99 {d.get('p99')}, max {d.get('max')}")
    for phase, stats in (summary.get("duration_ms_by_phase") or {}).items():
        lines.append(f"  - {phase}: min {stats.get('min')}, p50 {stats.get('p50')}, "
                     f"p95 {stats.get('p95')}, max {stats.get('max')}")
    lines.append("")
    lines.append("| Variant | Phase | Runs | Exit codes | p50 ms | p95 ms | Byte-identical | "
                 "Distinct stdout | Drift | Stderr | Crashes | Timeouts |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for variant in report["stability"]["variants"]:
        lines.append(
            f"| `{variant['variant']}` | {variant.get('phase')} | {variant.get('runs')} | "
            f"{variant.get('exit_codes')} | {variant.get('duration_ms', {}).get('p50')} | "
            f"{variant.get('duration_ms', {}).get('p95')} | {variant.get('byte_identical')} | "
            f"{variant.get('distinct_stdout_hashes')} | "
            f"{variant.get('drift_fields') or '—'} | {variant.get('stderr_nonempty')} | "
            f"{variant.get('crashed')} | {variant.get('timed_out')} |")
    lines.append("")

    mutation = report.get("mutation", {})
    lines.append("## Mutation suite")
    lines.append("")
    if mutation.get("skipped"):
        lines.append("_Skipped by request._")
        lines.append("")
    else:
        lines.append(f"- Seed: `{mutation.get('seed')}` (deterministic; same seed → same suite)")
        lines.append(f"- Mutations: **{mutation.get('total')}**, detectors fired: "
                     f"**{mutation.get('detectors_fired')}**, gaps: "
                     f"**{len(mutation.get('gaps', []))}**")
        lines.append(f"- Failing samples written to `reports/stability-failures/`")
        lines.append("")
        lines.append("| Category | Detected | Total |")
        lines.append("| --- | --- | --- |")
        for category, bucket in sorted(mutation.get("coverage_by_category", {}).items()):
            mark = "" if bucket["detected"] == bucket["total"] else " **(gap)**"
            lines.append(f"| {category}{mark} | {bucket['detected']} | {bucket['total']} |")
        lines.append("")
        gaps = mutation.get("gaps", [])
        if gaps:
            lines.append("### Detector gaps")
            lines.append("")
            for gap in gaps:
                lines.append(f"- `{gap['name']}`: {gap['reason']}")
            lines.append("")

    failures = report.get("failures", [])
    lines.append("## Failures and attribution")
    lines.append("")
    if not failures:
        lines.append("None: every variant was stable and every detector fired as expected.")
    else:
        lines.append("| Code | Variant | Phase | Owner | Message |")
        lines.append("| --- | --- | --- | --- | --- |")
        for failure in failures:
            lines.append(f"| `{failure['code']}` | `{failure['variant']}` | {failure['phase']} | "
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
    parser.add_argument("--binary", default=DEFAULT_BINARY)
    parser.add_argument("--expected-sha256", default=DEFAULT_SHA256,
                        help="certified digest; the gate refuses to run against anything else")
    parser.add_argument("--expected-schema", default=DEFAULT_SCHEMA)
    parser.add_argument("--arch", action="append", default=[], dest="arches")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS,
                        help=f"rounds per command/arch per phase (default {DEFAULT_ROUNDS})")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--skip-mutations", action="store_true")
    parser.add_argument("--out-dir", default=REPORTS_DIR)
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.arches:
        args.arches = list(DEFAULT_ARCHES)

    global STABILITY_JSON, STABILITY_MD, FAILURES_DIR  # noqa: PLW0603 - CLI override
    if os.path.abspath(args.out_dir) != os.path.abspath(REPORTS_DIR):
        STABILITY_JSON = os.path.join(args.out_dir, "stability-report.json")
        STABILITY_MD = os.path.join(args.out_dir, "stability-report.md")
        FAILURES_DIR = os.path.join(args.out_dir, "stability-failures")

    report = run_gate(args)
    os.makedirs(args.out_dir, exist_ok=True)
    with open(STABILITY_JSON, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    with open(STABILITY_MD, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(report))

    if not args.quiet:
        status = report.get("gate_status")
        if status == "blocked":
            print(f"BLOCKED: {report['blocking']['reason']}")
        else:
            s = report["stability"]["summary"]
            print(f"gate_status={status} invocations={s['invocations']} "
                  f"variants_ok={s['variants_ok']}/{s['variants']} "
                  f"crashes={s['crashes']} timeouts={s['timeouts']} "
                  f"mutation_gaps={len(report['mutation'].get('gaps', []))}")
        print(f"wrote {os.path.relpath(STABILITY_JSON, PROJECT_ROOT)}")
        print(f"wrote {os.path.relpath(STABILITY_MD, PROJECT_ROOT)}")

    if report.get("gate_status") == "blocked":
        return 2
    return 0 if report.get("gate_status") == "passed" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
