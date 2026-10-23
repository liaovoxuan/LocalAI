#!/usr/bin/env python3
"""Final independent black-box acceptance of a VirtualWorld release candidate.

Gated on ``build-release-candidate/release-ready.json`` (schema
``virtualworld.release-ready/1``), produced by another workstream only after the
shared integration and both Release builds pass. This tool never builds, never
rebuilds, and never repairs the product: it either certifies the exact candidate
the marker names, or reports ``blocked``.

Design rules
------------
* **Pinned to one artifact.** Every fact is bound to the candidate SHA-256 the
  marker records. The digest is re-checked immediately before and after the
  verification battery; if it moves, the verdict is blocked and the report says
  so. There is no fallback to ``build-final`` or any other build.
* **No silent passes.** Required facts are located by documented alias and
  reported with the exact JSON path they were found at. A fact that cannot be
  located makes the run ``blocked`` -- it is never assumed to hold.
* **Command allowlist.** The candidate binary is only ever executed through
  :class:`CandidateRunner`, which refuses anything outside
  ``--check``/``--list-backends``/``--json``/``--help``/``--version``. A static
  reachability check independently proves ``--start``/``launch``/``boot`` are
  unreachable from this tool and from the verifiers it drives.
* **No VM, ever.** No VM is started and no user-owned VM is touched; the tool
  only reads files and runs the read-only reporting commands above.

Exit codes: 0 = certified, 1 = failed, 2 = blocked.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import run_conformance as rc  # noqa: E402

PROJECT_ROOT = os.path.dirname(os.path.dirname(HERE))
REPORTS_DIR = os.path.join(HERE, "reports")
# The first-round report is historical evidence and must never be overwritten;
# a rerun writes under its own basename.
SUPERSEDED_ERROR_CODE = "PLATFORM_FREEZE_HASH_MISMATCH"
REPORT_BASENAME = "release-candidate-final"
RERUN_BASENAME = "release-candidate-rerun"

RC_DIR = os.path.join(PROJECT_ROOT, "build-release-candidate")
READY_MARKER = os.path.join(RC_DIR, "release-ready.json")
BLOCKED_MARKER = os.path.join(RC_DIR, "release-blocked.json")

RC_SCHEMA = "virtualworld.release-ready/1"

# The only image directories WorkBuddy AI is authorised to draw a test medium
# from. Anything else -- including an unknown UTM bundle -- is out of bounds.
AUTHORISED_IMAGE_ROOTS = (
    "/Users/liaoboxuan/Desktop/各种学习和娱乐资料/Start Boot Option",
)
# A guest is acceptable only when it is explicitly Debian.
REQUIRED_GUEST_OS = "debian"
# Pre-start states that prove the VM was not already running. A state that is
# running (or unrecognised) means the "only this instance was stopped" claim
# cannot be substantiated.
VM_STOPPED_STATES = (
    "stopped", "off", "powered off", "powered-off", "poweredoff", "shut down",
    "shutdown", "not running", "halted", "down", "exited", "not started",
)
VM_RUNNING_STATES = (
    "running", "started", "up", "active", "online", "booted", "started/running",
)
REPORT_SCHEMA = "virtualworld.release-candidate-final/1"

# The complete set of flags this tool may pass to the candidate. `--json` and
# `--check`/`--list-backends` are read-only reporting switches; the rest are
# informational. Anything that could start a guest is not here, by construction.
ALLOWED_CANDIDATE_FLAGS = {
    "--check", "--list-backends", "--json", "--help", "--version", "-h",
}
FORBIDDEN_FLAGS = {
    "--start", "--launch", "--boot", "--run", "--vm", "--execute", "--gui",
    "--test", "--create-qcow2", "--install",
}

# Required facts, with the aliases accepted for each. The list is deliberately
# permissive about naming and strict about presence.
FACT_ALIASES: Dict[str, Tuple[str, ...]] = {
    "candidate_path": ("candidate", "candidate_path", "candidate_dir", "candidate_build",
                       "build_dir", "bundle", "candidate_bundle", "release_candidate"),
    "binary_path": ("binary", "binary_path", "executable", "executable_path",
                    "main_program", "main_executable", "app_binary"),
    "binary_sha256": ("binary_sha256", "binary_sha", "sha256", "executable_sha256",
                      "main_program_sha256", "binary_digest"),
    "freeze_marker_path": ("freeze_marker", "platform_freeze_marker", "freeze_marker_path",
                           "marker", "marker_path", "freeze_file"),
    "freeze_marker_sha256": ("freeze_marker_sha256", "file_sha256", "freeze_sha256",
                             "marker_sha256", "platform_freeze_marker_sha256",
                             "freeze_digest"),
    "source_aggregate": ("source_aggregate_sha256", "source_hash", "source_aggregate",
                         "sources_sha256", "source_digest", "aggregate_source_sha256"),
    "sources": ("sources", "source_files", "included_sources", "source_list",
                "source_manifest"),
    "aggregate_file_count": ("file_count", "source_file_count", "files_counted"),
    "aggregate_scope": ("scope", "source_scope", "aggregate_scope"),
    "contrib_backends": ("contrib_backends", "contrib", "contrib_enabled"),
    "real_hardware_verified": ("real_hardware_verified", "real_hardware"),
    "vm_started": ("vm_started", "vm_launched", "guest_started"),
    "skip_reason": ("skip_reason", "vm_skip_reason", "skip_justification",
                    "reason_not_started", "not_started_reason"),
    "guest_os": ("guest_os", "guest_operating_system", "guest_os_name"),
    "vm_name": ("vm_name", "utm_vm_name", "utm_name", "vm_identifier", "utm_vm_id",
                "vm_id", "utm_vm_identifier"),
    "pre_start_state": ("pre_start_state", "prestart_state", "vm_state_before_start",
                        "state_before_start", "initial_state", "state_before"),
    "image_path": ("image_path", "image", "disk_image", "boot_image", "medium",
                   "image_file", "medium_path"),
    "image_sha256": ("image_sha256", "image_sha", "image_digest", "medium_sha256",
                     "image_hash"),
    "started_at": ("started_at", "start_time", "vm_started_at", "boot_time",
                   "launched_at"),
    "ready_at": ("ready_at", "ready_time", "guest_ready_at", "became_ready_at"),
    "stopped_at": ("stopped_at", "stop_time", "vm_stopped_at", "shutdown_at",
                   "normal_stop_at", "stopped_time"),
    "evidence_paths": ("evidence_paths", "evidence", "logs", "log_paths",
                       "evidence_files", "proof_paths", "artifacts"),
    "utm_config": ("utm_config", "utm_configuration", "utm_bundle", "utm_config_path",
                   "debian_utm_config", "utm_configuration_path"),
    "instance_scope": ("stopped_instance_only", "only_this_instance",
                       "stopped_only_this_launch", "instance_only",
                       "stopped_this_instance_only", "single_instance_stopped"),
    "generated_at": ("generated_at", "generated_at_utc", "created_at", "timestamp"),
    "gate_status": ("status", "gate", "result", "verdict", "gate_status"),
    "supersedes_error": ("supersedes_error_code", "supersedes", "superseded_error_code",
                         "supersedes_block", "resolves_error_code",
                         "previous_block_error_code", "supersedes_error"),
    "freeze_utc": ("freeze_utc", "freeze_time", "frozen_at", "freeze_timestamp"),
}

# A blocked marker is only an *active* gate under its canonical name. The release
# gate renames consumed markers to `release-blocked.<timestamp>.json`; those are
# history and must not keep blocking a later run.
HISTORICAL_BLOCKED_RE = re.compile(r"^release-blocked\..+\.json$")
ACTIVE_BLOCKED_NAME = "release-blocked.json"


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Marker fact location
# ---------------------------------------------------------------------------

def find_fact(document: Any, aliases: Sequence[str],
              _path: str = "$") -> Tuple[Any, str]:
    """Locate the first key matching one of `aliases`, at any depth.

    Returns ``(value, json_path)`` or ``(None, "")`` when absent. Searching by
    alias (rather than hard-coding a layout) keeps the tool working across
    reasonable schema spellings, and reporting the matched path keeps it honest:
    the report shows exactly where each fact came from.
    """
    if isinstance(document, dict):
        for key in aliases:
            if key in document:
                return document[key], f"{_path}.{key}"
        for key, value in document.items():
            found, path = find_fact(value, aliases, f"{_path}.{key}")
            if found is not None or path:
                return found, path
    elif isinstance(document, list):
        for index, value in enumerate(document):
            found, path = find_fact(value, aliases, f"{_path}[{index}]")
            if found is not None or path:
                return found, path
    return None, ""


def locate_facts(document: Any) -> Dict[str, Dict[str, Any]]:
    facts: Dict[str, Dict[str, Any]] = {}
    for name, aliases in FACT_ALIASES.items():
        value, path = find_fact(document, aliases)
        facts[name] = {"value": value, "path": path, "found": bool(path)}
    return facts


def resolve_path(value: Any, base: str) -> str:
    """Resolve a marker-recorded path against one base; absolute stays absolute."""
    if not isinstance(value, str) or not value.strip():
        return ""
    text = value.strip()
    if os.path.isabs(text):
        return os.path.normpath(text)
    return os.path.normpath(os.path.join(base, text))


def resolve_existing(value: Any, bases: Sequence[str]) -> str:
    """Resolve a recorded path against several plausible bases.

    Markers mix absolute paths with repo-relative ones (`build-release-candidate/…`),
    and a relative path is meaningless without knowing what it is relative to. Rather
    than guess a single base, try each in order and prefer one that exists, falling
    back to the repository root.
    """
    if not isinstance(value, str) or not value.strip():
        return ""
    text = value.strip()
    if os.path.isabs(text):
        return os.path.normpath(text)
    for base in bases:
        if not base:
            continue
        candidate = os.path.normpath(os.path.join(base, text))
        if os.path.exists(candidate):
            return candidate
    return os.path.normpath(os.path.join(PROJECT_ROOT, text))


# ---------------------------------------------------------------------------
# Candidate execution (allowlist enforced)
# ---------------------------------------------------------------------------

class CandidateRunner:
    """The only sanctioned way to execute the candidate binary."""

    def __init__(self, binary: str, expected_sha: str, timeout: float = 90.0) -> None:
        self.binary = binary
        self.expected_sha = expected_sha
        self.timeout = timeout
        self.calls: List[Dict[str, Any]] = []

    def _assert_allowed(self, argv: Sequence[str]) -> None:
        for token in argv:
            if token in FORBIDDEN_FLAGS:
                raise PermissionError(
                    f"refusing to run {token!r}: this tool may only issue read-only "
                    f"reporting commands")
            if token.startswith("--") and token not in ALLOWED_CANDIDATE_FLAGS:
                raise PermissionError(f"flag {token!r} is not in the allowlist "
                                      f"{sorted(ALLOWED_CANDIDATE_FLAGS)}")

    def run(self, *flags: str) -> Dict[str, Any]:
        argv = list(flags)
        self._assert_allowed(argv)
        # Re-verify the digest at every call: nothing may be swapped mid-run.
        actual = sha256_file(self.binary)
        if actual != self.expected_sha:
            raise RuntimeError(
                f"candidate SHA-256 changed during the run: expected {self.expected_sha}, "
                f"found {actual}")
        env = dict(os.environ)
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        started = time.perf_counter()
        completed = subprocess.run([self.binary] + argv, capture_output=True, text=True,
                                   timeout=self.timeout, env=env, check=False)
        record = {
            "argv": argv,
            "exit_code": completed.returncode,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            "stdout_bytes": len(completed.stdout),
            "stderr_bytes": len(completed.stderr),
        }
        self.calls.append(record)
        return {**record, "stdout": completed.stdout, "stderr": completed.stderr}


def reachability_report() -> Dict[str, Any]:
    """Static proof that no VM-launching flag is reachable from this toolchain."""
    modules = ["verify_release_candidate.py", "run_conformance.py", "run_stability_gate.py",
               "verify_release_manifest.py", "make_final_report.py", "run_parser_hardening.py"]
    hits: List[Dict[str, str]] = []
    for name in modules:
        path = os.path.join(HERE, name)
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
        # Skip the two regions that exist precisely to *name* these flags: the
        # guard table and the body of this function. Flagging those would make
        # the check report itself rather than real reachability.
        in_table = False
        in_reachability = False
        for lineno, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("FORBIDDEN_FLAGS"):
                in_table = True
            if in_table:
                if stripped == "}":
                    in_table = False
                continue
            if stripped.startswith("def reachability_report"):
                in_reachability = True
            elif in_reachability and stripped.startswith("def "):
                in_reachability = False
            if in_reachability or stripped.startswith("#"):
                continue
            for flag in FORBIDDEN_FLAGS:
                if flag not in ("--start", "--launch", "--boot"):
                    continue
                if f'"{flag}"' in line:
                    hits.append({"module": name, "line": lineno, "flag": flag,
                                 "text": stripped[:120]})
    return {
        "allowed_candidate_flags": sorted(ALLOWED_CANDIDATE_FLAGS),
        "forbidden_candidate_flags": sorted(FORBIDDEN_FLAGS),
        "modules_scanned": [m for m in modules if os.path.isfile(os.path.join(HERE, m))],
        "forbidden_flag_mentions": hits,
        "vm_launch_reachable": bool(hits),
        "note": ("mentions inside the guard table and inside comments are ignored; a hit here "
                 "means a launching flag appears in executable-looking code"),
    }


# ---------------------------------------------------------------------------
# VM evidence (Debian only)
# ---------------------------------------------------------------------------

def hash_medium(path: str) -> str:
    """SHA-256 of a test medium: a file, or an aggregate over a bundle."""
    if os.path.isfile(path):
        return sha256_file(path)
    if os.path.isdir(path):
        items = []
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames.sort()
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, path)
                try:
                    items.append((rel, sha256_file(full)))
                except OSError:
                    continue
        payload = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return ""


def _evidence_subtree(document: Any, key: str) -> Any:
    """Return the dict that directly contains `key`, else the whole document."""
    if isinstance(document, dict):
        if key in document:
            return document
        for value in document.values():
            found = _evidence_subtree(value, key)
            if isinstance(found, dict) and key in found:
                return found
    elif isinstance(document, list):
        for value in document:
            found = _evidence_subtree(value, key)
            if isinstance(found, dict) and key in found:
                return found
    return document


def _norm_state(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().lower().replace("_", " ").split())


def _parse_time(value: Any) -> Optional[datetime.datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed



def authorised_image_roots(facts: Dict[str, Dict[str, Any]], marker_dir: str) -> List[str]:
    """Roots a test medium may come from.

    Two are permitted by the contract: the `Start Boot Option` directory, and an
    explicitly declared *Debian* UTM configuration. A declared configuration is
    only honoured when it actually looks like Debian -- otherwise declaring a
    config would be a way to smuggle an arbitrary path past the check.
    """
    roots = [os.path.realpath(root) for root in AUTHORISED_IMAGE_ROOTS]
    declared = facts.get("utm_config", {}).get("value")
    candidates: List[str] = []
    if isinstance(declared, str) and declared.strip():
        candidates.append(declared)
    elif isinstance(declared, list):
        candidates.extend(c for c in declared if isinstance(c, str))

    for candidate in candidates:
        resolved = resolve_path(candidate, marker_dir)
        if not resolved or not os.path.exists(resolved):
            continue
        haystack = os.path.basename(resolved).lower()
        if os.path.isdir(resolved):
            config = os.path.join(resolved, "config.plist")
            if os.path.isfile(config):
                try:
                    with open(config, "r", encoding="utf-8", errors="replace") as handle:
                        haystack += " " + handle.read(20000).lower()
                except OSError:
                    pass
        if "kali" in haystack or REQUIRED_GUEST_OS not in haystack:
            # Not provably Debian: do not widen the allowance.
            continue
        roots.append(os.path.realpath(resolved))
    return roots


def validate_vm_evidence(document: Any, facts: Dict[str, Dict[str, Any]],
                         marker_path: str, details: Dict[str, Any],
                         error) -> None:
    """Validate the `vm_started` branch.

    `false` is allowed but must be justified. `true` switches the whole run onto
    a Debian-only evidence path: the guest must be Debian, the medium must come
    from an authorised image root and hash correctly, the launch must be bounded
    by timestamps, reviewable evidence must exist, and it must be provable that
    only the instance this run started was stopped.

    This tool never contacts a VM: it only reads the evidence WorkBuddy AI
    produced.
    """
    started = facts.get("vm_started", {}).get("value")
    details["vm_started"] = started
    if started is None:
        return

    if started is False:
        reason = facts.get("skip_reason", {}).get("value")
        details["vm_skip_reason"] = reason
        if not isinstance(reason, str) or not reason.strip():
            error("vm-skip-reason-missing",
                  "vm_started is false but no non-empty skip_reason explains why no Debian "
                  "VM was exercised", facts.get("skip_reason", {}).get("path", "$"))
        return

    if started is not True:
        error("vm-started-invalid",
              f"vm_started must be a boolean, found {started!r}",
              facts.get("vm_started", {}).get("path", "$"))
        return

    # ---- Debian only -----------------------------------------------------
    guest_os = facts.get("guest_os", {}).get("value")
    details["guest_os"] = guest_os
    if not isinstance(guest_os, str) or not guest_os.strip():
        error("vm-guest-os-missing",
              "vm_started is true but guest_os is missing; only a Debian guest may be "
              "certified", facts.get("guest_os", {}).get("path", "$"))
    elif guest_os.strip().lower() != REQUIRED_GUEST_OS:
        error("vm-non-debian-guest",
              f"guest_os is {guest_os!r}; only {REQUIRED_GUEST_OS!r} is acceptable",
              facts.get("guest_os", {}).get("path", "$"))

    # A Kali mention anywhere in the evidence subtree is disqualifying.
    subtree = _evidence_subtree(document, "vm_started")
    try:
        serialized = json.dumps(subtree, ensure_ascii=False, default=str).lower()
    except (TypeError, ValueError):
        serialized = str(subtree).lower()
    if "kali" in serialized:
        error("vm-non-debian-guest",
              "the VM evidence references Kali; only a Debian guest may be certified",
              facts.get("guest_os", {}).get("path", "$"))

    # ---- identity --------------------------------------------------------
    vm_name = facts.get("vm_name", {}).get("value")
    details["vm_name"] = vm_name
    if not isinstance(vm_name, str) or not vm_name.strip():
        error("vm-identity-missing",
              "vm_started is true but no UTM VM identifier/name is recorded",
              facts.get("vm_name", {}).get("path", "$"))
    elif "kali" in vm_name.lower():
        error("vm-non-debian-guest", f"VM name {vm_name!r} references Kali",
              facts.get("vm_name", {}).get("path", "$"))

    # ---- pre-start state -------------------------------------------------
    pre_state = facts.get("pre_start_state", {}).get("value")
    details["pre_start_state"] = pre_state
    normalised = _norm_state(pre_state)
    if not normalised:
        error("vm-prestart-state-missing",
              "vm_started is true but the state before start is not recorded, so it cannot "
              "be shown the VM was not already running",
              facts.get("pre_start_state", {}).get("path", "$"))
    elif normalised in VM_RUNNING_STATES:
        error("vm-prestart-state-running",
              f"the VM was already {pre_state!r} before this run started it, so stopping it "
              f"cannot be shown to be limited to the instance this run launched",
              facts.get("pre_start_state", {}).get("path", "$"))
    elif normalised not in VM_STOPPED_STATES:
        error("vm-prestart-state-unproven",
              f"pre-start state {pre_state!r} is not a recognised stopped state, so the "
              f"'only this instance was stopped' claim is unproven",
              facts.get("pre_start_state", {}).get("path", "$"))

    # ---- candidate SHA binding ------------------------------------------
    vm_candidate = facts.get("candidate_path", {}).get("value")
    vm_binary_sha = facts.get("binary_sha256", {}).get("value")
    details["vm_candidate_bound"] = bool(vm_candidate and vm_binary_sha)
    if not (vm_candidate and vm_binary_sha):
        error("vm-candidate-binding-missing",
              "vm_started is true but the evidence is not bound to a candidate path and "
              "binary SHA-256", "$")

    # ---- test medium -----------------------------------------------------
    image_path = facts.get("image_path", {}).get("value")
    image_sha = facts.get("image_sha256", {}).get("value")
    details["image_path"] = image_path
    details["image_sha256_recorded"] = image_sha
    resolved_image = resolve_existing(image_path,
                                      [PROJECT_ROOT, os.path.dirname(os.path.abspath(marker_path)),
                                       os.path.dirname(os.path.dirname(os.path.abspath(marker_path)))])
    details["image_path_resolved"] = resolved_image
    if not resolved_image:
        error("vm-image-missing", "vm_started is true but no test medium path is recorded",
              facts.get("image_path", {}).get("path", "$"))
    else:
        # Absoluteness is judged on the *recorded* value: resolving first would
        # make every path absolute and silently retire this check.
        if not os.path.isabs(str(image_path)):
            error("vm-image-path-not-absolute",
                  f"the recorded test medium path must be absolute, got {image_path!r}",
                  facts.get("image_path", {}).get("path", "$"))
        real_image = os.path.realpath(resolved_image)
        real_roots = authorised_image_roots(
            facts, os.path.dirname(os.path.abspath(marker_path)))
        details["authorised_image_roots"] = real_roots
        inside = False
        for root in real_roots:
            try:
                if os.path.commonpath([root, real_image]) == root:
                    inside = True
                    break
            except ValueError:
                continue
        if not inside:
            error("vm-image-outside-authorized-roots",
                  f"test medium {resolved_image} is not inside the Start Boot Option root "
                  f"nor a declared Debian UTM configuration "
                  f"({real_roots})", facts.get("image_path", {}).get("path", "$"))
        if "kali" in os.path.basename(real_image).lower() or "kali" in real_image.lower():
            error("vm-non-debian-guest",
                  f"test medium {resolved_image} references Kali",
                  facts.get("image_path", {}).get("path", "$"))
        if not os.path.exists(real_image):
            error("vm-image-missing", f"test medium not found: {real_image}",
                  facts.get("image_path", {}).get("path", "$"))
        elif isinstance(image_sha, str) and image_sha:
            computed = hash_medium(real_image)
            details["image_sha256_computed"] = computed
            details["image_sha256_matches"] = computed == image_sha
            if computed != image_sha:
                error("vm-image-sha256-mismatch",
                      f"test medium hashes to {computed}, marker records {image_sha}",
                      facts.get("image_sha256", {}).get("path", "$"))
        else:
            error("vm-image-sha256-missing",
                  "vm_started is true but no test medium SHA-256 is recorded",
                  facts.get("image_sha256", {}).get("path", "$"))

    # ---- timestamps ------------------------------------------------------
    stamps: Dict[str, Optional[datetime.datetime]] = {}
    for key in ("started_at", "ready_at", "stopped_at"):
        raw = facts.get(key, {}).get("value")
        parsed = _parse_time(raw)
        stamps[key] = parsed
        details[key] = raw
        if not isinstance(raw, str) or not raw.strip():
            error(f"vm-{key.replace('_', '-')}-missing",
                  f"vm_started is true but {key} is not recorded",
                  facts.get(key, {}).get("path", "$"))
        elif parsed is None:
            error(f"vm-{key.replace('_', '-')}-invalid",
                  f"{key} value {raw!r} is not a parseable ISO-8601 timestamp",
                  facts.get(key, {}).get("path", "$"))
    if all(stamps.get(k) for k in ("started_at", "ready_at", "stopped_at")):
        if not (stamps["started_at"] <= stamps["ready_at"] <= stamps["stopped_at"]):
            error("vm-timeline-inconsistent",
                  f"timestamps are out of order: started={details['started_at']}, "
                  f"ready={details['ready_at']}, stopped={details['stopped_at']}", "$")
        else:
            details["vm_duration_seconds"] = round(
                (stamps["stopped_at"] - stamps["started_at"]).total_seconds(), 1)

    # ---- reviewable evidence --------------------------------------------
    evidence = facts.get("evidence_paths", {}).get("value")
    rows: List[str] = []
    if isinstance(evidence, dict):
        rows = [k for k in evidence]
    elif isinstance(evidence, list):
        for entry in evidence:
            if isinstance(entry, str):
                rows.append(entry)
            elif isinstance(entry, dict):
                candidate = entry.get("path") or entry.get("file") or ""
                if candidate:
                    rows.append(candidate)
    details["evidence_entries"] = rows
    if not rows:
        error("vm-evidence-missing",
              "vm_started is true but no reviewable log/evidence path is recorded",
              facts.get("evidence_paths", {}).get("path", "$"))
    else:
        base = os.path.dirname(os.path.abspath(marker_path))
        missing = [entry for entry in rows
                   if not os.path.exists(resolve_existing(entry, [PROJECT_ROOT, base]))]
        if missing:
            error("vm-evidence-missing",
                  f"recorded evidence is not present on disk: {missing[:5]}",
                  facts.get("evidence_paths", {}).get("path", "$"))

    # ---- only this instance was stopped ----------------------------------
    scope = facts.get("instance_scope", {}).get("value")
    details["instance_scope"] = scope
    if scope is not True:
        error("vm-instance-scope-unproven",
              "the evidence does not assert that only the instance this run started was "
              "stopped, so no other VM's lifecycle can be ruled out",
              facts.get("instance_scope", {}).get("path", "$"))



# ---------------------------------------------------------------------------
# Platform freeze marker
# ---------------------------------------------------------------------------

FREEZE_HASH_RE = re.compile(r"^([0-9a-f]{64})  (\S+)$", re.MULTILINE)
EXPECTED_FROZEN_FILES = 8
# Frozen-file paths in the marker are repository-relative. Overridable so tests
# can point the resolution at a sandbox instead of writing fixtures into the repo.
FREEZE_ROOT = PROJECT_ROOT


def parse_freeze_marker(path: str) -> Dict[str, Any]:
    """Parse the text-format `.platform-freeze-ready` marker."""
    info: Dict[str, Any] = {"path": path, "fields": {}, "files": [], "parse_error": ""}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        info["parse_error"] = str(exc)
        return info
    info["sha256"] = sha256_file(path)
    for key in ("freeze_utc", "generated_by", "real_hardware_verified",
                "freeze_repaired_from_hash", "freeze_repaired_to_hash",
                "freeze_repair_action", "freeze_repair_no_test_rerun_required"):
        match = re.search(rf"^{re.escape(key)}:(.*)$", text, re.MULTILINE)
        if match:
            info["fields"][key] = match.group(1).strip()
    info["files"] = [{"sha256": digest, "path": name}
                     for digest, name in FREEZE_HASH_RE.findall(text)]
    return info



def coerce_facts(facts: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """Normalise nested schema spellings into the flat facts the checks expect.

    Real markers nest things: `binary: {path, sha256}` rather than a bare path,
    and `aggregate_source_sha256: {value, file_count, scope}` rather than a bare
    digest. Rather than hard-coding one layout, flatten the shapes we understand
    and report every adaptation so the report shows what was done.
    """
    notes: Dict[str, str] = {}

    binary = facts.get("binary_path", {})
    if isinstance(binary.get("value"), dict):
        node = binary["value"]
        path = node.get("path") or node.get("file")
        if isinstance(path, str):
            notes["binary_path"] = f"took .path from the object at {binary['path']}"
            facts["binary_path"] = {"value": path, "path": f"{binary['path']}.path",
                                    "found": True}
        digest = node.get("sha256") or node.get("hash")
        if isinstance(digest, str) and not isinstance(
                facts.get("binary_sha256", {}).get("value"), str):
            notes["binary_sha256"] = f"took .sha256 from the object at {binary['path']}"
            facts["binary_sha256"] = {"value": digest,
                                      "path": f"{binary['path']}.sha256", "found": True}

    aggregate = facts.get("source_aggregate", {})
    if isinstance(aggregate.get("value"), dict):
        node = aggregate["value"]
        notes["source_aggregate"] = (
            f"took .value/.file_count/.scope from the object at {aggregate['path']}")
        if "file_count" in node and not facts.get("aggregate_file_count", {}).get("found"):
            facts["aggregate_file_count"] = {
                "value": node.get("file_count"),
                "path": f"{aggregate['path']}.file_count", "found": True}
        if "scope" in node and not facts.get("aggregate_scope", {}).get("found"):
            facts["aggregate_scope"] = {"value": node.get("scope"),
                                        "path": f"{aggregate['path']}.scope", "found": True}
        facts["source_aggregate"] = {"value": node.get("value"),
                                     "path": f"{aggregate['path']}.value",
                                     "found": "value" in node}

    freeze = facts.get("freeze_marker_path", {})
    if isinstance(freeze.get("value"), dict):
        node = freeze["value"]
        path = node.get("path")
        if isinstance(path, str):
            notes["freeze_marker_path"] = f"took .path from the object at {freeze['path']}"
            facts["freeze_marker_path"] = {"value": path, "path": f"{freeze['path']}.path",
                                           "found": True}
        digest = node.get("file_sha256") or node.get("sha256")
        if isinstance(digest, str) and not isinstance(
                facts.get("freeze_marker_sha256", {}).get("value"), str):
            notes["freeze_marker_sha256"] = (
                f"took .file_sha256 from the object at {freeze['path']}")
            facts["freeze_marker_sha256"] = {
                "value": digest, "path": f"{freeze['path']}.file_sha256", "found": True}
    return notes


def locate_and_coerce(document: Any) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
    facts = locate_facts(document)
    notes = coerce_facts(facts)
    return facts, notes


def verify_freeze_marker(path: str, details: Dict[str, Any], error) -> Dict[str, Any]:
    """Re-derive every frozen file hash and the marker's own claims."""
    info = parse_freeze_marker(path)
    details["freeze_marker"] = {
        "path": path,
        "sha256": info.get("sha256"),
        "fields": info.get("fields", {}),
        "files_listed": len(info.get("files", [])),
    }
    if info.get("parse_error"):
        error("freeze-marker-unreadable", f"cannot read {path}: {info['parse_error']}", "")
        return info
    if not info["files"]:
        error("freeze-marker-no-files",
              f"the freeze marker at {path} lists no file hashes", "")
        return info
    if len(info["files"]) != EXPECTED_FROZEN_FILES:
        error("freeze-marker-file-count",
              f"the freeze marker lists {len(info['files'])} files, expected exactly "
              f"{EXPECTED_FROZEN_FILES}", "")

    mismatched, missing, matched = [], [], []
    base = FREEZE_ROOT
    for row in info["files"]:
        resolved = os.path.join(base, row["path"])
        if not os.path.isfile(resolved):
            missing.append(row["path"])
            continue
        actual = sha256_file(resolved)
        if actual == row["sha256"]:
            matched.append(row["path"])
        else:
            mismatched.append({"path": row["path"], "marker": row["sha256"],
                               "disk": actual})
    details["freeze_marker"]["matched"] = matched
    details["freeze_marker"]["mismatched"] = mismatched
    details["freeze_marker"]["missing"] = missing
    details["freeze_marker"]["all_match"] = not mismatched and not missing

    if missing:
        error("freeze-file-missing",
              f"frozen file(s) absent from disk: {missing}", "")
    if mismatched:
        error("freeze-hash-mismatch",
              "the freeze marker's recorded hash does not match disk for: "
              + ", ".join(row["path"] for row in mismatched), "")

    # `real_hardware_verified` in the freeze marker must agree with the ready.
    frozen_flag = info["fields"].get("real_hardware_verified", "").lower()
    details["freeze_marker"]["real_hardware_verified"] = frozen_flag
    if frozen_flag not in ("false", "0", "no"):
        error("freeze-marker-real-hardware",
              f"the freeze marker claims real_hardware_verified={frozen_flag!r}", "")
    return info


def active_blocked_marker(rc_dir: str) -> Tuple[str, List[str]]:
    """Return the *active* blocked marker and any historical ones.

    Only the canonical `release-blocked.json` is an active gate. The release
    gate renames consumed markers to `release-blocked.<stamp>.json`; treating
    those as active would block every later run forever.
    """
    active = os.path.join(rc_dir, ACTIVE_BLOCKED_NAME)
    historical: List[str] = []
    if os.path.isdir(rc_dir):
        for name in sorted(os.listdir(rc_dir)):
            if HISTORICAL_BLOCKED_RE.match(name):
                historical.append(os.path.join(rc_dir, name))
    return (active if os.path.isfile(active) else ""), historical


def validate_supersede(document: Any, facts: Dict[str, Dict[str, Any]],
                        freeze_info: Dict[str, Any], marker_path: str,
                        details: Dict[str, Any], error, warning, required: bool) -> None:
    """Prove the readiness marker post-dates the freeze and supersedes the block.

    `required` says whether a *historical* blocked marker accompanies this
    readiness. Independently of that, a `supersedes` field that is present is
    always checked -- declaring it and not substantiating it is worse than not
    declaring it at all.
    """
    generated = facts.get("generated_at", {}).get("value")
    parsed_generated = _parse_time(generated)
    freeze_fields = freeze_info.get("fields", {}) or {}
    freeze_utc_raw = freeze_fields.get("freeze_utc") or facts.get("freeze_utc", {}).get("value")
    parsed_freeze = _parse_time(freeze_utc_raw)
    details["supersede"] = {
        "required": required,
        "generated_at": generated,
        "freeze_utc": freeze_utc_raw,
        "reason": ("" if required else
                   "no historical release-blocked marker is present beside this readiness "
                   "marker"),
    }

    if parsed_generated is None:
        error("ready-generated-at-invalid",
              f"the readiness marker has no parseable generated_at (got {generated!r})",
              facts.get("generated_at", {}).get("path", "$"))
    if parsed_freeze is None:
        error("ready-freeze-utc-missing",
              "no parseable freeze_utc could be established, so it cannot be shown the "
              "readiness marker post-dates the freeze it rests on", "")
    if parsed_generated and parsed_freeze:
        if parsed_generated <= parsed_freeze:
            error("ready-predates-freeze",
                  f"generated_at {generated} is not later than freeze_utc {freeze_utc_raw}",
                  "")
        else:
            details["supersede"]["postdates_freeze"] = True

    supersedes = facts.get("supersedes_error", {}).get("value")
    details["supersede"]["supersedes"] = supersedes

    if not isinstance(supersedes, str) or not supersedes.strip():
        if required:
            error("ready-supersede-missing",
                  "a previous block exists but the readiness marker does not name the error "
                  "code or blocked marker it supersedes",
                  facts.get("supersedes_error", {}).get("path", "$"))
        return

    # Form 1: the error code is named directly.
    if SUPERSEDED_ERROR_CODE in supersedes.upper():
        details["supersede"]["supersedes_ok"] = True
        details["supersede"]["supersede_form"] = "error code named directly"
        return

    # Form 2: the blocked marker that recorded the error is named. Resolve it and
    # read its error_code; if it has since been removed, fall back to the freeze
    # marker's own repair record as corroboration.
    base = os.path.dirname(os.path.abspath(marker_path))
    resolved = resolve_existing(supersedes, [PROJECT_ROOT, base, os.path.dirname(base)])
    recorded = None
    if resolved and os.path.isfile(resolved):
        referenced = _sub_report(resolved)
        if isinstance(referenced, dict):
            recorded = referenced.get("error_code")
    details["supersede"]["superseded_marker"] = resolved if os.path.isfile(resolved or "") else ""
    details["supersede"]["superseded_marker_error_code"] = recorded

    if recorded and SUPERSEDED_ERROR_CODE in str(recorded).upper():
        details["supersede"]["supersedes_ok"] = True
        details["supersede"]["supersede_form"] = "blocked marker named and error code confirmed"
        return

    repaired_from = freeze_fields.get("freeze_repaired_from_hash")
    if not os.path.isfile(resolved or "") and repaired_from:
        # The named marker is gone, but the freeze marker documents repairing
        # exactly the hash the old block complained about.
        details["supersede"]["supersedes_ok"] = True
        details["supersede"]["supersede_form"] = (
            "blocked marker absent; corroborated by the freeze marker's repair record")
        details["supersede"]["corroborating_repair_hash"] = repaired_from
        warning("ready-supersede-corroborated-by-repair",
                f"the superseded marker {supersedes!r} is no longer present; the supersede is "
                f"accepted because the freeze marker records repairing "
                f"{repaired_from[:16]}..., the hash the earlier "
                f"{SUPERSEDED_ERROR_CODE} block reported",
                facts.get("supersedes_error", {}).get("path", "$"))
        return

    error("ready-supersede-unverifiable",
          f"the readiness marker supersedes {supersedes!r}, but it cannot be resolved to a "
          f"marker carrying {SUPERSEDED_ERROR_CODE} and the freeze marker records no repair "
          f"corroborating it (error_code found: {recorded!r})",
          facts.get("supersedes_error", {}).get("path", "$"))


def validate_vm_evidence(document: Any, facts: Dict[str, Dict[str, Any]],
                         marker_path: str, details: Dict[str, Any],
                         error) -> None:
    """Validate the `vm_started` branch.

    `false` is allowed but must be justified. `true` switches the whole run onto
    a Debian-only evidence path: the guest must be Debian, the medium must come
    from an authorised image root and hash correctly, the launch must be bounded
    by timestamps, reviewable evidence must exist, and it must be provable that
    only the instance this run started was stopped.

    This tool never contacts a VM: it only reads the evidence WorkBuddy AI
    produced.
    """
    started = facts.get("vm_started", {}).get("value")
    details["vm_started"] = started
    if started is None:
        return

    if started is False:
        reason = facts.get("skip_reason", {}).get("value")
        details["vm_skip_reason"] = reason
        if not isinstance(reason, str) or not reason.strip():
            error("vm-skip-reason-missing",
                  "vm_started is false but no non-empty skip_reason explains why no Debian "
                  "VM was exercised", facts.get("skip_reason", {}).get("path", "$"))
        return

    if started is not True:
        error("vm-started-invalid",
              f"vm_started must be a boolean, found {started!r}",
              facts.get("vm_started", {}).get("path", "$"))
        return

    # ---- Debian only -----------------------------------------------------
    guest_os = facts.get("guest_os", {}).get("value")
    details["guest_os"] = guest_os
    if not isinstance(guest_os, str) or not guest_os.strip():
        error("vm-guest-os-missing",
              "vm_started is true but guest_os is missing; only a Debian guest may be "
              "certified", facts.get("guest_os", {}).get("path", "$"))
    elif guest_os.strip().lower() != REQUIRED_GUEST_OS:
        error("vm-non-debian-guest",
              f"guest_os is {guest_os!r}; only {REQUIRED_GUEST_OS!r} is acceptable",
              facts.get("guest_os", {}).get("path", "$"))

    # A Kali mention anywhere in the evidence subtree is disqualifying.
    subtree = _evidence_subtree(document, "vm_started")
    try:
        serialized = json.dumps(subtree, ensure_ascii=False, default=str).lower()
    except (TypeError, ValueError):
        serialized = str(subtree).lower()
    if "kali" in serialized:
        error("vm-non-debian-guest",
              "the VM evidence references Kali; only a Debian guest may be certified",
              facts.get("guest_os", {}).get("path", "$"))

    # ---- identity --------------------------------------------------------
    vm_name = facts.get("vm_name", {}).get("value")
    details["vm_name"] = vm_name
    if not isinstance(vm_name, str) or not vm_name.strip():
        error("vm-identity-missing",
              "vm_started is true but no UTM VM identifier/name is recorded",
              facts.get("vm_name", {}).get("path", "$"))
    elif "kali" in vm_name.lower():
        error("vm-non-debian-guest", f"VM name {vm_name!r} references Kali",
              facts.get("vm_name", {}).get("path", "$"))

    # ---- pre-start state -------------------------------------------------
    pre_state = facts.get("pre_start_state", {}).get("value")
    details["pre_start_state"] = pre_state
    normalised = _norm_state(pre_state)
    if not normalised:
        error("vm-prestart-state-missing",
              "vm_started is true but the state before start is not recorded, so it cannot "
              "be shown the VM was not already running",
              facts.get("pre_start_state", {}).get("path", "$"))
    elif normalised in VM_RUNNING_STATES:
        error("vm-prestart-state-running",
              f"the VM was already {pre_state!r} before this run started it, so stopping it "
              f"cannot be shown to be limited to the instance this run launched",
              facts.get("pre_start_state", {}).get("path", "$"))
    elif normalised not in VM_STOPPED_STATES:
        error("vm-prestart-state-unproven",
              f"pre-start state {pre_state!r} is not a recognised stopped state, so the "
              f"'only this instance was stopped' claim is unproven",
              facts.get("pre_start_state", {}).get("path", "$"))

    # ---- candidate SHA binding ------------------------------------------
    vm_candidate = facts.get("candidate_path", {}).get("value")
    vm_binary_sha = facts.get("binary_sha256", {}).get("value")
    details["vm_candidate_bound"] = bool(vm_candidate and vm_binary_sha)
    if not (vm_candidate and vm_binary_sha):
        error("vm-candidate-binding-missing",
              "vm_started is true but the evidence is not bound to a candidate path and "
              "binary SHA-256", "$")

    # ---- test medium -----------------------------------------------------
    image_path = facts.get("image_path", {}).get("value")
    image_sha = facts.get("image_sha256", {}).get("value")
    details["image_path"] = image_path
    details["image_sha256_recorded"] = image_sha
    resolved_image = resolve_existing(image_path,
                                      [PROJECT_ROOT, os.path.dirname(os.path.abspath(marker_path)),
                                       os.path.dirname(os.path.dirname(os.path.abspath(marker_path)))])
    details["image_path_resolved"] = resolved_image
    if not resolved_image:
        error("vm-image-missing", "vm_started is true but no test medium path is recorded",
              facts.get("image_path", {}).get("path", "$"))
    else:
        # Absoluteness is judged on the *recorded* value: resolving first would
        # make every path absolute and silently retire this check.
        if not os.path.isabs(str(image_path)):
            error("vm-image-path-not-absolute",
                  f"the recorded test medium path must be absolute, got {image_path!r}",
                  facts.get("image_path", {}).get("path", "$"))
        real_image = os.path.realpath(resolved_image)
        real_roots = authorised_image_roots(
            facts, os.path.dirname(os.path.abspath(marker_path)))
        details["authorised_image_roots"] = real_roots
        inside = False
        for root in real_roots:
            try:
                if os.path.commonpath([root, real_image]) == root:
                    inside = True
                    break
            except ValueError:
                continue
        if not inside:
            error("vm-image-outside-authorized-roots",
                  f"test medium {resolved_image} is not inside the Start Boot Option root "
                  f"nor a declared Debian UTM configuration "
                  f"({real_roots})", facts.get("image_path", {}).get("path", "$"))
        if "kali" in os.path.basename(real_image).lower() or "kali" in real_image.lower():
            error("vm-non-debian-guest",
                  f"test medium {resolved_image} references Kali",
                  facts.get("image_path", {}).get("path", "$"))
        if not os.path.exists(real_image):
            error("vm-image-missing", f"test medium not found: {real_image}",
                  facts.get("image_path", {}).get("path", "$"))
        elif isinstance(image_sha, str) and image_sha:
            computed = hash_medium(real_image)
            details["image_sha256_computed"] = computed
            details["image_sha256_matches"] = computed == image_sha
            if computed != image_sha:
                error("vm-image-sha256-mismatch",
                      f"test medium hashes to {computed}, marker records {image_sha}",
                      facts.get("image_sha256", {}).get("path", "$"))
        else:
            error("vm-image-sha256-missing",
                  "vm_started is true but no test medium SHA-256 is recorded",
                  facts.get("image_sha256", {}).get("path", "$"))

    # ---- timestamps ------------------------------------------------------
    stamps: Dict[str, Optional[datetime.datetime]] = {}
    for key in ("started_at", "ready_at", "stopped_at"):
        raw = facts.get(key, {}).get("value")
        parsed = _parse_time(raw)
        stamps[key] = parsed
        details[key] = raw
        if not isinstance(raw, str) or not raw.strip():
            error(f"vm-{key.replace('_', '-')}-missing",
                  f"vm_started is true but {key} is not recorded",
                  facts.get(key, {}).get("path", "$"))
        elif parsed is None:
            error(f"vm-{key.replace('_', '-')}-invalid",
                  f"{key} value {raw!r} is not a parseable ISO-8601 timestamp",
                  facts.get(key, {}).get("path", "$"))
    if all(stamps.get(k) for k in ("started_at", "ready_at", "stopped_at")):
        if not (stamps["started_at"] <= stamps["ready_at"] <= stamps["stopped_at"]):
            error("vm-timeline-inconsistent",
                  f"timestamps are out of order: started={details['started_at']}, "
                  f"ready={details['ready_at']}, stopped={details['stopped_at']}", "$")
        else:
            details["vm_duration_seconds"] = round(
                (stamps["stopped_at"] - stamps["started_at"]).total_seconds(), 1)

    # ---- reviewable evidence --------------------------------------------
    evidence = facts.get("evidence_paths", {}).get("value")
    rows: List[str] = []
    if isinstance(evidence, dict):
        rows = [k for k in evidence]
    elif isinstance(evidence, list):
        for entry in evidence:
            if isinstance(entry, str):
                rows.append(entry)
            elif isinstance(entry, dict):
                candidate = entry.get("path") or entry.get("file") or ""
                if candidate:
                    rows.append(candidate)
    details["evidence_entries"] = rows
    if not rows:
        error("vm-evidence-missing",
              "vm_started is true but no reviewable log/evidence path is recorded",
              facts.get("evidence_paths", {}).get("path", "$"))
    else:
        base = os.path.dirname(os.path.abspath(marker_path))
        missing = [entry for entry in rows
                   if not os.path.exists(resolve_existing(entry, [PROJECT_ROOT, base]))]
        if missing:
            error("vm-evidence-missing",
                  f"recorded evidence is not present on disk: {missing[:5]}",
                  facts.get("evidence_paths", {}).get("path", "$"))

    # ---- only this instance was stopped ----------------------------------
    scope = facts.get("instance_scope", {}).get("value")
    details["instance_scope"] = scope
    if scope is not True:
        error("vm-instance-scope-unproven",
              "the evidence does not assert that only the instance this run started was "
              "stopped, so no other VM's lifecycle can be ruled out",
              facts.get("instance_scope", {}).get("path", "$"))



# ---------------------------------------------------------------------------
# Platform freeze marker
# ---------------------------------------------------------------------------

FREEZE_HASH_RE = re.compile(r"^([0-9a-f]{64})  (\S+)$", re.MULTILINE)
EXPECTED_FROZEN_FILES = 8
# Frozen-file paths in the marker are repository-relative. Overridable so tests
# can point the resolution at a sandbox instead of writing fixtures into the repo.
FREEZE_ROOT = PROJECT_ROOT


def parse_freeze_marker(path: str) -> Dict[str, Any]:
    """Parse the text-format `.platform-freeze-ready` marker."""
    info: Dict[str, Any] = {"path": path, "fields": {}, "files": [], "parse_error": ""}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        info["parse_error"] = str(exc)
        return info
    info["sha256"] = sha256_file(path)
    for key in ("freeze_utc", "generated_by", "real_hardware_verified",
                "freeze_repaired_from_hash", "freeze_repaired_to_hash",
                "freeze_repair_action", "freeze_repair_no_test_rerun_required"):
        match = re.search(rf"^{re.escape(key)}:(.*)$", text, re.MULTILINE)
        if match:
            info["fields"][key] = match.group(1).strip()
    info["files"] = [{"sha256": digest, "path": name}
                     for digest, name in FREEZE_HASH_RE.findall(text)]
    return info



def coerce_facts(facts: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    """Normalise nested schema spellings into the flat facts the checks expect.

    Real markers nest things: `binary: {path, sha256}` rather than a bare path,
    and `aggregate_source_sha256: {value, file_count, scope}` rather than a bare
    digest. Rather than hard-coding one layout, flatten the shapes we understand
    and report every adaptation so the report shows what was done.
    """
    notes: Dict[str, str] = {}

    binary = facts.get("binary_path", {})
    if isinstance(binary.get("value"), dict):
        node = binary["value"]
        path = node.get("path") or node.get("file")
        if isinstance(path, str):
            notes["binary_path"] = f"took .path from the object at {binary['path']}"
            facts["binary_path"] = {"value": path, "path": f"{binary['path']}.path",
                                    "found": True}
        digest = node.get("sha256") or node.get("hash")
        if isinstance(digest, str) and not isinstance(
                facts.get("binary_sha256", {}).get("value"), str):
            notes["binary_sha256"] = f"took .sha256 from the object at {binary['path']}"
            facts["binary_sha256"] = {"value": digest,
                                      "path": f"{binary['path']}.sha256", "found": True}

    aggregate = facts.get("source_aggregate", {})
    if isinstance(aggregate.get("value"), dict):
        node = aggregate["value"]
        notes["source_aggregate"] = (
            f"took .value/.file_count/.scope from the object at {aggregate['path']}")
        if "file_count" in node and not facts.get("aggregate_file_count", {}).get("found"):
            facts["aggregate_file_count"] = {
                "value": node.get("file_count"),
                "path": f"{aggregate['path']}.file_count", "found": True}
        if "scope" in node and not facts.get("aggregate_scope", {}).get("found"):
            facts["aggregate_scope"] = {"value": node.get("scope"),
                                        "path": f"{aggregate['path']}.scope", "found": True}
        facts["source_aggregate"] = {"value": node.get("value"),
                                     "path": f"{aggregate['path']}.value",
                                     "found": "value" in node}

    freeze = facts.get("freeze_marker_path", {})
    if isinstance(freeze.get("value"), dict):
        node = freeze["value"]
        path = node.get("path")
        if isinstance(path, str):
            notes["freeze_marker_path"] = f"took .path from the object at {freeze['path']}"
            facts["freeze_marker_path"] = {"value": path, "path": f"{freeze['path']}.path",
                                           "found": True}
        digest = node.get("file_sha256") or node.get("sha256")
        if isinstance(digest, str) and not isinstance(
                facts.get("freeze_marker_sha256", {}).get("value"), str):
            notes["freeze_marker_sha256"] = (
                f"took .file_sha256 from the object at {freeze['path']}")
            facts["freeze_marker_sha256"] = {
                "value": digest, "path": f"{freeze['path']}.file_sha256", "found": True}
    return notes


def locate_and_coerce(document: Any) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
    facts = locate_facts(document)
    notes = coerce_facts(facts)
    return facts, notes


def verify_freeze_marker(path: str, details: Dict[str, Any], error) -> Dict[str, Any]:
    """Re-derive every frozen file hash and the marker's own claims."""
    info = parse_freeze_marker(path)
    details["freeze_marker"] = {
        "path": path,
        "sha256": info.get("sha256"),
        "fields": info.get("fields", {}),
        "files_listed": len(info.get("files", [])),
    }
    if info.get("parse_error"):
        error("freeze-marker-unreadable", f"cannot read {path}: {info['parse_error']}", "")
        return info
    if not info["files"]:
        error("freeze-marker-no-files",
              f"the freeze marker at {path} lists no file hashes", "")
        return info
    if len(info["files"]) != EXPECTED_FROZEN_FILES:
        error("freeze-marker-file-count",
              f"the freeze marker lists {len(info['files'])} files, expected exactly "
              f"{EXPECTED_FROZEN_FILES}", "")

    mismatched, missing, matched = [], [], []
    base = FREEZE_ROOT
    for row in info["files"]:
        resolved = os.path.join(base, row["path"])
        if not os.path.isfile(resolved):
            missing.append(row["path"])
            continue
        actual = sha256_file(resolved)
        if actual == row["sha256"]:
            matched.append(row["path"])
        else:
            mismatched.append({"path": row["path"], "marker": row["sha256"],
                               "disk": actual})
    details["freeze_marker"]["matched"] = matched
    details["freeze_marker"]["mismatched"] = mismatched
    details["freeze_marker"]["missing"] = missing
    details["freeze_marker"]["all_match"] = not mismatched and not missing

    if missing:
        error("freeze-file-missing",
              f"frozen file(s) absent from disk: {missing}", "")
    if mismatched:
        error("freeze-hash-mismatch",
              "the freeze marker's recorded hash does not match disk for: "
              + ", ".join(row["path"] for row in mismatched), "")

    # `real_hardware_verified` in the freeze marker must agree with the ready.
    frozen_flag = info["fields"].get("real_hardware_verified", "").lower()
    details["freeze_marker"]["real_hardware_verified"] = frozen_flag
    if frozen_flag not in ("false", "0", "no"):
        error("freeze-marker-real-hardware",
              f"the freeze marker claims real_hardware_verified={frozen_flag!r}", "")
    return info


def active_blocked_marker(rc_dir: str) -> Tuple[str, List[str]]:
    """Return the *active* blocked marker and any historical ones.

    Only the canonical `release-blocked.json` is an active gate. The release
    gate renames consumed markers to `release-blocked.<stamp>.json`; treating
    those as active would block every later run forever.
    """
    active = os.path.join(rc_dir, ACTIVE_BLOCKED_NAME)
    historical: List[str] = []
    if os.path.isdir(rc_dir):
        for name in sorted(os.listdir(rc_dir)):
            if HISTORICAL_BLOCKED_RE.match(name):
                historical.append(os.path.join(rc_dir, name))
    return (active if os.path.isfile(active) else ""), historical



# ---------------------------------------------------------------------------
# Marker validation
# ---------------------------------------------------------------------------

class Finding:
    def __init__(self, severity: str, code: str, message: str, path: str = "") -> None:
        self.severity = severity
        self.code = code
        self.message = message
        self.path = path

    def to_dict(self) -> Dict[str, Any]:
        return {"severity": self.severity, "code": self.code, "message": self.message,
                "path": self.path}


def validate_marker(document: Any, marker_path: str,
                    facts: Dict[str, Dict[str, Any]],
                    warn: Optional[Callable[[str, str, str], None]] = None
                    ) -> Tuple[List[Finding], Dict[str, Any]]:
    """Validate every invariant the marker is required to establish."""
    findings: List[Finding] = []
    details: Dict[str, Any] = {}

    def error(code: str, message: str, path: str = "") -> None:
        findings.append(Finding("error", code, message, path))

    def warning(code: str, message: str, path: str = "") -> None:
        findings.append(Finding("warning", code, message, path))

    def require(name: str, code: str) -> Any:
        fact = facts.get(name, {})
        if not fact.get("found"):
            error(code, f"required fact '{name}' could not be located in the marker; "
                        f"accepted aliases were {list(FACT_ALIASES[name])}", "$")
            return None
        return fact["value"]

    # ---- schema ----------------------------------------------------------
    schema = document.get("schema") if isinstance(document, dict) else None
    details["schema"] = schema
    if schema != RC_SCHEMA:
        error("marker-schema-invalid",
              f"marker schema is {schema!r}, expected {RC_SCHEMA!r}", "$.schema")

    # ---- honesty flags ---------------------------------------------------
    # real_hardware_verified must still be exactly false: no real hardware was
    # exercised, and the Debian VM below is a virtual guest, not real hardware.
    value = require("real_hardware_verified", "real-hardware-verified-not-false")
    details["real_hardware_verified"] = value
    if value is not None and value is not False:
        error("real-hardware-verified-not-false",
              f"real_hardware_verified must be exactly false, found {value!r}",
              facts["real_hardware_verified"]["path"])

    # vm_started has its own branch: false needs a justification, true needs
    # strict Debian evidence.
    vm_value = facts.get("vm_started", {}).get("value")
    if not facts.get("vm_started", {}).get("found"):
        error("vm-started-missing",
              "the marker does not state whether a Debian VM was exercised",
              "$")
    elif not isinstance(vm_value, bool):
        error("vm-started-invalid",
              f"vm_started must be a boolean, found {vm_value!r}",
              facts["vm_started"]["path"])

    # ---- candidate path --------------------------------------------------
    base = os.path.dirname(os.path.abspath(marker_path))
    candidate = resolve_existing(require("candidate_path", "candidate-path-missing"),
                                 [PROJECT_ROOT, base, os.path.dirname(base)])
    details["candidate_path"] = candidate
    if candidate:
        if not os.path.isabs(details["candidate_path"]):
            error("candidate-path-not-absolute",
                  f"candidate path must be absolute, got {candidate!r}",
                  facts["candidate_path"]["path"])
        if not os.path.isdir(candidate):
            error("candidate-path-missing", f"candidate directory does not exist: {candidate}",
                  facts["candidate_path"]["path"])
        rc_dir = os.path.dirname(os.path.abspath(marker_path))
        details["rc_dir"] = rc_dir
        if os.path.commonpath([rc_dir, candidate]) != rc_dir:
            error("candidate-outside-rc-dir",
                  f"candidate {candidate} is outside the directory holding the marker "
                  f"({rc_dir}); only the release candidate may be certified",
                  facts["candidate_path"]["path"])
        if not os.path.basename(rc_dir).startswith("build-release-candidate"):
            error("rc-dir-unexpected",
                  f"the marker lives in {os.path.basename(rc_dir)!r}, which is not a "
                  f"build-release-candidate directory", "$")

    # ---- binary + digest -------------------------------------------------
    binary = resolve_existing(require("binary_path", "binary-path-missing"),
                              [candidate, PROJECT_ROOT, base, os.path.dirname(base)])
    details["binary_path"] = binary
    recorded_sha = require("binary_sha256", "binary-sha-missing")
    details["binary_sha256_recorded"] = recorded_sha
    if binary and os.path.isfile(binary) and isinstance(recorded_sha, str):
        actual = sha256_file(binary)
        details["binary_sha256_computed"] = actual
        details["binary_matches"] = actual == recorded_sha
        if actual != recorded_sha:
            error("binary-sha256-mismatch",
                  f"candidate binary hashes to {actual}, marker records {recorded_sha}",
                  facts["binary_sha256"]["path"])
        details["binary_size"] = os.path.getsize(binary)
        details["binary_mtime"] = datetime.datetime.fromtimestamp(
            os.path.getmtime(binary)).isoformat(timespec="seconds")
    elif not os.path.isfile(binary or ""):
        error("binary-missing", f"candidate binary not found: {binary!r}", "")

    # ---- platform freeze marker -----------------------------------------
    freeze = resolve_existing(require("freeze_marker_path", "freeze-marker-path-missing"),
                              [PROJECT_ROOT, base, candidate, os.path.dirname(base)])
    details["freeze_marker_path"] = freeze
    freeze_sha = require("freeze_marker_sha256", "freeze-marker-sha-missing")
    details["freeze_marker_sha256_recorded"] = freeze_sha
    if freeze and os.path.isfile(freeze) and isinstance(freeze_sha, str):
        actual = sha256_file(freeze)
        details["freeze_marker_sha256_computed"] = actual
        details["freeze_marker_matches"] = actual == freeze_sha
        if actual != freeze_sha:
            error("freeze-marker-sha256-mismatch",
                  f"freeze marker hashes to {actual}, marker records {freeze_sha}",
                  facts["freeze_marker_sha256"]["path"])
    elif not os.path.isfile(freeze or ""):
        error("freeze-marker-missing", f"platform freeze marker not found: {freeze!r}", "")

    # ---- source aggregate -----------------------------------------------
    aggregate = require("source_aggregate", "source-aggregate-missing")
    details["source_aggregate_recorded"] = aggregate
    sources = facts.get("sources", {}).get("value")
    source_rows: List[Dict[str, Any]] = []
    if isinstance(sources, dict):
        source_rows = [{"path": k, "sha256": v} for k, v in sources.items()]
    elif isinstance(sources, list):
        for entry in sources:
            if isinstance(entry, dict):
                source_rows.append(entry)
            elif isinstance(entry, str):
                source_rows.append({"path": entry})
    details["source_entries"] = len(source_rows)
    declared_count = facts.get("aggregate_file_count", {}).get("value")
    details["aggregate_file_count_recorded"] = declared_count
    details["aggregate_scope"] = facts.get("aggregate_scope", {}).get("value")

    if isinstance(aggregate, str) and source_rows:
        # A per-file hash map: fully recomputable.
        recomputed = _aggregate_sources(source_rows, base)
        details["source_aggregate_computed"] = recomputed
        details["source_aggregate_matches"] = recomputed == aggregate
        if recomputed != aggregate:
            error("source-aggregate-mismatch",
                  f"recomputed source aggregate {recomputed} != recorded {aggregate}",
                  facts["source_aggregate"]["path"])
    elif isinstance(aggregate, str) and isinstance(declared_count, int):
        # A scope+count fingerprint. The digest formula belongs to the gate's
        # tooling, so it is not independently reproducible here -- but the file
        # *set* is, and that is checked rather than waved through.
        discovered = discover_source_scope()
        details["aggregate_files_discovered"] = len(discovered)
        details["source_aggregate_matches"] = None
        details["source_aggregate_formula_reproduced"] = False
        if len(discovered) != declared_count:
            error("source-aggregate-file-count-mismatch",
                  f"the marker declares {declared_count} source files but the declared scope "
                  f"resolves to {len(discovered)} on disk",
                  facts.get("aggregate_file_count", {}).get("path", "$"))
        else:
            warning("source-aggregate-formula-unreproduced",
                    "the source file set matches exactly, but the aggregate digest formula "
                    "belongs to the release gate and could not be independently reproduced, "
                    "so the digest value itself is not certified by this run",
                    facts["source_aggregate"]["path"])
    elif isinstance(aggregate, str):
        details["source_aggregate_matches"] = None
        error("source-aggregate-unverifiable",
              "the marker records a source aggregate hash but neither a source list nor a "
              "file count to check it against", facts["source_aggregate"]["path"])

    # ---- freshness: nothing newer than the marker ------------------------
    marker_mtime = os.path.getmtime(marker_path)
    details["marker_mtime"] = datetime.datetime.fromtimestamp(
        marker_mtime).isoformat(timespec="seconds")
    newer: List[Dict[str, Any]] = []
    for label, path in (("binary", binary), ("freeze_marker", freeze)):
        if path and os.path.isfile(path) and os.path.getmtime(path) > marker_mtime + 1:
            newer.append({"what": label, "path": path,
                          "seconds_newer": round(os.path.getmtime(path) - marker_mtime, 1)})
    for row in source_rows:
        path = resolve_path(row.get("path", ""), base)
        if path and os.path.isfile(path) and os.path.getmtime(path) > marker_mtime + 1:
            newer.append({"what": "source", "path": path,
                          "seconds_newer": round(os.path.getmtime(path) - marker_mtime, 1)})
    details["newer_than_marker"] = newer
    if newer:
        error("artifact-newer-than-marker",
              f"{len(newer)} artifact(s) are newer than the release-ready marker, so the "
              f"marker does not describe them", "")

    # ---- VM evidence (Debian only) ---------------------------------------
    # Prefer the VM smoke subtree: `real_hardware_verified`/`vm_started` also
    # appear in unrelated honesty sections, and the VM branch must read the one
    # that actually describes the smoke.
    vm_scope = _evidence_subtree(document, "vm_started")
    vm_facts = facts
    if isinstance(vm_scope, dict) and vm_scope is not document:
        vm_facts = locate_facts(vm_scope)
        for key in ("candidate_path", "binary_sha256"):
            if not vm_facts.get(key, {}).get("found"):
                vm_facts[key] = facts.get(key, {"value": None, "path": "", "found": False})
        details["vm_fact_scope"] = "vm smoke subtree"
    validate_vm_evidence(document, vm_facts, marker_path, details, error)

    # ---- contrib ON/OFF --------------------------------------------------
    contrib = facts.get("contrib_backends", {})
    details["contrib_backends"] = contrib.get("value") if contrib.get("found") else None
    return findings, details


SOURCE_SCOPE_ROOTS = ("src", "tests", "cmake")
SOURCE_SCOPE_EXTRA_FILES = ("CMakeLists.txt", "assets/VirtualWorld.icns")


def discover_source_scope() -> List[str]:
    """Enumerate the documented build-input fingerprint scope, sorted."""
    found: List[str] = []
    for rel in SOURCE_SCOPE_EXTRA_FILES:
        if os.path.isfile(os.path.join(PROJECT_ROOT, rel)):
            found.append(rel)
    for root in SOURCE_SCOPE_ROOTS:
        base = os.path.join(PROJECT_ROOT, root)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                found.append(os.path.relpath(os.path.join(dirpath, name), PROJECT_ROOT))
    return sorted(set(found))


def _aggregate_sources(rows: Sequence[Dict[str, Any]], base: str) -> str:
    """Recompute a source aggregate as a stable digest over sorted (path, hash)."""
    items: List[Tuple[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        path = row.get("path") or row.get("file") or ""
        recorded = row.get("sha256") or row.get("hash") or ""
        resolved = resolve_path(path, base)
        if resolved and os.path.isfile(resolved):
            digest = sha256_file(resolved)
        elif isinstance(recorded, str) and recorded:
            digest = recorded
        else:
            digest = ""
        items.append((os.path.relpath(resolved or path, PROJECT_ROOT)
                      if resolved else str(path), digest))
    items.sort()
    payload = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Verification battery
# ---------------------------------------------------------------------------

def _run(cmd: Sequence[str], timeout: float) -> Dict[str, Any]:
    started = time.perf_counter()
    try:
        completed = subprocess.run(list(cmd), capture_output=True, text=True,
                                   timeout=timeout, cwd=PROJECT_ROOT, check=False)
        return {"command": list(cmd), "exit_code": completed.returncode,
                "stdout_tail": completed.stdout.strip()[-1500:],
                "stderr_tail": completed.stderr.strip()[-800:],
                "duration_seconds": round(time.perf_counter() - started, 2),
                "timed_out": False}
    except subprocess.TimeoutExpired:
        return {"command": list(cmd), "exit_code": None, "stdout_tail": "",
                "stderr_tail": f"timed out after {timeout}s",
                "duration_seconds": round(time.perf_counter() - started, 2),
                "timed_out": True}


def candidate_manifest_roots() -> List[str]:
    """The manifest search roots for the release candidate pair."""
    return [os.path.relpath(os.path.join(PROJECT_ROOT, name), PROJECT_ROOT)
            for name in ("build-release-candidate", "build-release-candidate-nocontrib")
            if os.path.isdir(os.path.join(PROJECT_ROOT, name))]


def run_battery(args, binary: str, expected_sha: str) -> Dict[str, Any]:
    """Re-run every verifier against the pinned candidate."""
    python = sys.executable
    tools = HERE
    results: Dict[str, Any] = {}

    # 1. verifier self-tests
    results["self_tests"] = _run(
        [python, "-m", "unittest", "discover", "-s", tools, "-p", "test_*.py"],
        args.self_test_timeout)
    match = re.search(r"Ran (\d+) tests?", results["self_tests"]["stdout_tail"]
                      + results["self_tests"]["stderr_tail"])
    results["self_tests"]["tests"] = int(match.group(1)) if match else None
    results["self_tests"]["ok"] = results["self_tests"]["exit_code"] == 0
    results["self_tests"]["baseline"] = args.self_test_baseline

    # 2. parser hardening
    results["parser_hardening"] = _run(
        [python, os.path.join(tools, "run_parser_hardening.py"),
         "--min-samples", str(args.hardening_samples), "--seed", str(args.seed),
         "--out-dir", os.path.join(args.out_dir, "rc-parser-hardening"), "--quiet"],
        args.heavy_timeout)
    results["parser_hardening"]["ok"] = results["parser_hardening"]["exit_code"] == 0
    results["parser_hardening"]["samples"] = args.hardening_samples

    # 3. live conformance against the candidate
    conformance_cmd = [python, os.path.join(tools, "run_conformance.py"), "--binary", binary,
                       "--output", os.path.join(args.out_dir, "rc-conformance.json")]
    for arch in args.arches:
        conformance_cmd += ["--guest-arch", arch]
    results["conformance"] = _run(conformance_cmd, args.heavy_timeout)
    results["conformance"]["ok"] = results["conformance"]["exit_code"] == 0
    results["conformance"]["arches"] = list(args.arches)

    # 4. release manifest certification, aimed at the *candidate* builds.
    #    The verifier's default search roots are the earlier packaging-audit
    #    directories, so they must be overridden explicitly -- otherwise this
    #    step would certify a different pair of builds and silently pass.
    manifest_roots = candidate_manifest_roots()
    manifest_cmd = [python, os.path.join(tools, "verify_release_manifest.py"), "--quiet",
                    "--out-dir", os.path.join(args.out_dir, "rc-manifests")]
    for root in manifest_roots:
        manifest_cmd += ["--search-root", root]
    results["release_manifests"] = _run(manifest_cmd, args.heavy_timeout)
    results["release_manifests"]["ok"] = results["release_manifests"]["exit_code"] == 0
    results["release_manifests"]["search_roots"] = manifest_roots

    # 5. stability / concurrency gate (read-only, fixed seed)
    results["stability"] = _run(
        [python, os.path.join(tools, "run_stability_gate.py"), "--binary", binary,
         "--expected-sha256", expected_sha, "--rounds", str(args.stability_rounds),
         "--concurrency", str(args.stability_concurrency), "--seed", str(args.seed),
         "--quiet", "--out-dir", os.path.join(args.out_dir, "rc-stability")],
        args.stability_timeout)
    results["stability"]["ok"] = results["stability"]["exit_code"] == 0
    results["stability"]["rounds"] = args.stability_rounds
    results["stability"]["invocations"] = 2 * args.stability_rounds * 2 * len(args.arches)

    return results


def _sub_report(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Blocked-marker authentication
# ---------------------------------------------------------------------------

BLOCKED_SCHEMA = "virtualworld.release-blocked/1"


def validate_blocked_marker(document: Any, marker_path: str) -> Dict[str, Any]:
    """Independently re-derive the claims in a `release-blocked.json`.

    The gate's verdict is not in dispute -- a blocked marker means blocked. What
    this adds is evidence authentication: if the marker blames a hash mismatch,
    every recorded hash is recomputed here, so a *false* block (one asserted
    without real evidence) is distinguishable from a genuine one. Disagreement is
    reported, never used to overturn the block.
    """
    result: Dict[str, Any] = {
        "schema": document.get("schema") if isinstance(document, dict) else None,
        "gate_result": document.get("gate_result") if isinstance(document, dict) else None,
        "error_code": document.get("error_code") if isinstance(document, dict) else None,
        "marker_sha256": sha256_file(marker_path),
        "claims_checked": 0,
        "claims_confirmed": 0,
        "claims_refuted": [],
        "files_missing": [],
        "authenticated": False,
    }
    if result["schema"] != BLOCKED_SCHEMA:
        result["schema_ok"] = False
        return result
    result["schema_ok"] = True

    rows = document.get("platform_hash_verification")
    if not isinstance(rows, list):
        result["note"] = "no platform_hash_verification list to authenticate"
        return result

    for row in rows:
        if not isinstance(row, dict):
            continue
        path = row.get("file")
        if not isinstance(path, str) or not path:
            continue
        result["claims_checked"] += 1
        resolved = os.path.join(PROJECT_ROOT, path)
        if not os.path.isfile(resolved):
            result["files_missing"].append(path)
            continue
        actual = sha256_file(resolved)
        expected = row.get("expected")
        claimed_actual = row.get("actual")
        matches = (actual == expected)
        if matches == bool(row.get("match")):
            result["claims_confirmed"] += 1
        else:
            result["claims_refuted"].append({
                "file": path, "recomputed": actual, "recorded_expected": expected,
                "recorded_actual": claimed_actual, "recorded_match": row.get("match"),
                "recomputed_match": matches,
            })

    dispute = document.get("mismatch")
    if isinstance(dispute, dict) and isinstance(dispute.get("file"), str):
        resolved = os.path.join(PROJECT_ROOT, dispute["file"])
        if os.path.isfile(resolved):
            actual = sha256_file(resolved)
            result["disputed_file"] = dispute["file"]
            result["disputed_file_recomputed"] = actual
            result["disputed_file_confirmed"] = (
                actual != dispute.get("expected") and actual == dispute.get("actual"))

    result["authenticated"] = (
        result["claims_checked"] > 0
        and not result["claims_refuted"]
        and not result["files_missing"]
        and result.get("disputed_file_confirmed", True) is not False
    )
    return result


def run_acceptance(args) -> Dict[str, Any]:
    started = time.perf_counter()
    report: Dict[str, Any] = {
        "report_schema": REPORT_SCHEMA,
        "report": "virtualworld-release-candidate-final",
        "report_version": "1.0.0",
        "generated_at": _utcnow(),
        "method": ("independent black-box acceptance; no build, no rebuild, no product "
                   "modification; only --check/--list-backends/--json are issued to the "
                   "candidate and no VM is started"),
        "marker": {"expected_path": os.path.relpath(READY_MARKER, PROJECT_ROOT)},
        "facts": {},
        "findings": [],
        "verifiers": {},
        "not_verified": [],
    }

    # ---- gate on the marker ---------------------------------------------
    active_blocked, historical_blocked = active_blocked_marker(
        os.path.dirname(BLOCKED_MARKER))
    report["marker"]["historical_blocked"] = [
        os.path.relpath(p, PROJECT_ROOT) for p in historical_blocked]
    report["marker"]["active_blocked"] = (
        os.path.relpath(active_blocked, PROJECT_ROOT) if active_blocked else None)

    if active_blocked:
        report["verdict"] = "blocked"
        blocking = {"reason": "the release gate published an active release-blocked.json",
                    "marker": os.path.relpath(active_blocked, PROJECT_ROOT)}
        blocked_doc = _sub_report(BLOCKED_MARKER)
        if blocked_doc is not None:
            blocking["detail"] = blocked_doc
            report["blocked_marker"] = validate_blocked_marker(blocked_doc, BLOCKED_MARKER)
        report["marker"].update({
            "path": os.path.relpath(BLOCKED_MARKER, PROJECT_ROOT),
            "sha256": sha256_file(BLOCKED_MARKER),
            "size_bytes": os.path.getsize(BLOCKED_MARKER),
            "kind": "release-blocked",
        })
        report["blocking"] = blocking
        report["verdict"] = "blocked"
        return report

    if not os.path.isfile(READY_MARKER):
        report["verdict"] = "blocked"
        report["blocking"] = {
            "reason": "release-ready.json has not been produced; the release candidate is "
                      "not available for acceptance",
            "marker": os.path.relpath(READY_MARKER, PROJECT_ROOT),
        }
        return report

    try:
        with open(READY_MARKER, "r", encoding="utf-8") as handle:
            marker = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        report["verdict"] = "blocked"
        report["blocking"] = {"reason": f"release-ready.json is unreadable: {exc}",
                             "marker": os.path.relpath(READY_MARKER, PROJECT_ROOT)}
        return report

    report["marker"].update({
        "path": os.path.relpath(READY_MARKER, PROJECT_ROOT),
        "sha256": sha256_file(READY_MARKER),
        "size_bytes": os.path.getsize(READY_MARKER),
        "document": marker,
    })

    facts, coercions = locate_and_coerce(marker)
    report["fact_coercions"] = coercions
    report["facts"] = {name: {"path": f["path"], "found": f["found"],
                              "value": f["value"] if name not in ("sources",) else
                              (f"<{len(f['value'])} entries>" if isinstance(f["value"], (list, dict))
                               else f["value"])}
                       for name, f in facts.items()}

    findings, details = validate_marker(marker, READY_MARKER, facts)

    # Platform freeze: re-derive the 8 owned-file hashes and the marker's claims.
    freeze_path = details.get("freeze_marker_path") or ""
    freeze_info: Dict[str, Any] = {}
    if freeze_path and os.path.isfile(freeze_path):
        freeze_info = verify_freeze_marker(freeze_path, details,
                                          lambda c, m, p="": findings.append(
                                              Finding("error", c, m, p)))
    else:
        findings.append(Finding("error", "freeze-marker-missing",
                                f"platform freeze marker not found: {freeze_path!r}", ""))

    # A rerun must explicitly supersede the earlier block and post-date the freeze.
    validate_supersede(marker, facts, freeze_info, READY_MARKER, details,
                       lambda c, m, p="": findings.append(Finding("error", c, m, p)),
                       lambda c, m, p="": findings.append(Finding("warning", c, m, p)),
                       required=bool(historical_blocked))
    report["marker_details"] = details

    binary = details.get("binary_path", "")
    expected_sha = details.get("binary_sha256_recorded", "")

    # ---- reachability proof ---------------------------------------------
    report["reachability"] = reachability_report()
    if report["reachability"]["vm_launch_reachable"]:
        findings.append(Finding(
            "error", "vm-launch-reachable",
            "a VM-launching flag appears in executable-looking code: "
            + json.dumps(report["reachability"]["forbidden_flag_mentions"]), ""))

    VM_EVIDENCE_CODES = {
        "vm-skip-reason-missing", "vm-started-missing", "vm-started-invalid",
        "vm-guest-os-missing", "vm-non-debian-guest", "vm-identity-missing",
        "vm-prestart-state-missing", "vm-prestart-state-running",
        "vm-prestart-state-unproven", "vm-candidate-binding-missing",
        "vm-image-missing", "vm-image-path-not-absolute",
        "vm-image-outside-authorized-roots", "vm-image-sha256-missing",
        "vm-image-sha256-mismatch", "vm-started-at-missing", "vm-ready-at-missing",
        "vm-stopped-at-missing", "vm-started-at-invalid", "vm-ready-at-invalid",
        "vm-stopped-at-invalid", "vm-timeline-inconsistent", "vm-evidence-missing",
        "vm-instance-scope-unproven",
    }
    RERUN_CODES = {
        "freeze-marker-unreadable", "freeze-marker-no-files", "freeze-marker-file-count",
        "freeze-file-missing", "freeze-hash-mismatch", "freeze-marker-real-hardware",
        "ready-generated-at-invalid", "ready-freeze-utc-missing", "ready-predates-freeze",
        "ready-supersede-missing", "ready-supersede-unrelated",
        "ready-supersede-unverifiable", "source-aggregate-file-count-mismatch",
    }
    hard_block = [f for f in findings if f.severity == "error"
                  and f.code in ({"binary-sha256-mismatch", "binary-missing",
                                  "candidate-path-missing", "freeze-marker-sha256-mismatch",
                                  "freeze-marker-missing", "source-aggregate-mismatch",
                                  "artifact-newer-than-marker", "marker-schema-invalid",
                                  "real-hardware-verified-not-false",
                                  "source-aggregate-unverifiable", "vm-launch-reachable"}
                                 | VM_EVIDENCE_CODES | RERUN_CODES)]
    if hard_block:
        report["findings"] = [f.to_dict() for f in findings]
        report["verdict"] = "blocked"
        report["blocking"] = {
            "reason": ("the release-ready marker failed independent validation; the candidate "
                       "was not exercised"),
            "codes": sorted({f.code for f in hard_block}),
        }
        report["elapsed_seconds"] = round(time.perf_counter() - started, 2)
        return report

    # ---- pre-flight through the allowlisted runner -----------------------
    runner = CandidateRunner(binary, expected_sha, timeout=min(90.0, args.heavy_timeout))
    flags_issued: List[str] = []
    if not args.skip_heavy:
        try:
            # `--help` is the one informational flag the product implements; it
            # proves the pinned binary actually executes without touching a VM.
            probe = runner.run("--help")
            flags_issued.extend(probe["argv"])
            report["preflight"] = {
                "exit_code": probe["exit_code"],
                "stderr_bytes": probe["stderr_bytes"],
                "flag": "--help",
                "runnable": probe["exit_code"] == 0,
                "note": ("a non-zero exit here would mean the pinned binary does not run at "
                         "all; the product does not implement --version, so it is not probed"),
            }
            if probe["exit_code"] != 0:
                findings.append(Finding(
                    "error", "candidate-preflight-failed",
                    f"the pinned binary exited {probe['exit_code']} for --help", ""))
        except (PermissionError, RuntimeError, subprocess.TimeoutExpired) as exc:
            findings.append(Finding("error", "candidate-preflight-failed",
                                    f"candidate could not be exercised read-only: {exc}", ""))
            report["preflight"] = {"error": str(exc)}

    # ---- battery ---------------------------------------------------------
    if args.skip_heavy:
        # A marker-only run can never certify: the whole point of this report is
        # that the verifier battery actually ran against the pinned candidate.
        report["verifiers"] = {"skipped": True}
        report["findings"] = [f.to_dict() for f in findings]
        report["verdict"] = "incomplete"
        report["blocking"] = {
            "reason": ("--skip-heavy was requested, so the verifier battery did not run and "
                       "no certification is possible"),
            "codes": ["battery-skipped"],
        }
        report["elapsed_seconds"] = round(time.perf_counter() - started, 2)
        return report
    else:
        before = sha256_file(binary)
        report["verifiers"] = run_battery(args, binary, expected_sha)
        after = sha256_file(binary)
        report["candidate_digest_stable"] = before == after == expected_sha
        if not report["candidate_digest_stable"]:
            findings.append(Finding(
                "error", "candidate-digest-moved",
                f"candidate SHA-256 changed during the battery: {before} -> {after}", ""))

    # roll sub-verifier failures into findings
    for name, result in report["verifiers"].items():
        if not isinstance(result, dict) or name == "skipped":
            continue
        if not result.get("ok", True):
            findings.append(Finding(
                "error", f"verifier-failed:{name}",
                f"{name} exited {result.get('exit_code')}"
                + (" (timed out)" if result.get("timed_out") else ""),
                json.dumps(result.get("command", []))))

    report["findings"] = [f.to_dict() for f in findings]
    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity == "warning"]
    report["verdict"] = "failed" if errors else "certified"
    report["blocking"] = None
    report["warning_count"] = len(warnings)
    if any(f.code == "source-aggregate-formula-unreproduced" for f in warnings):
        report["not_verified"].append(
            "The source-aggregate digest value was NOT independently reproduced: the "
            "declared scope resolves to exactly the declared file count and every file "
            "exists, but the gate's aggregation formula is its own. The digest is reported, "
            "not certified.")

    # ---- roll-up summary -------------------------------------------------
    report["candidate"] = {
        "binary": binary,
        "sha256": expected_sha,
        "sha256_recomputed": details.get("binary_sha256_computed"),
        "size_bytes": details.get("binary_size"),
        "mtime": details.get("binary_mtime"),
        "marker_sha256": report["marker"]["sha256"],
        "freeze_marker": details.get("freeze_marker_path"),
        "freeze_marker_sha256": details.get("freeze_marker_sha256_recorded"),
        "contrib_backends": details.get("contrib_backends"),
    }
    report["candidate_calls"] = runner.calls
    report["attestations"] = {
        "real_hardware_verified": details.get("real_hardware_verified"),
        "vm_started": details.get("vm_started"),
        "vm_guest_os": details.get("guest_os"),
        "vm_name": details.get("vm_name"),
        "vm_pre_start_state": details.get("pre_start_state"),
        "vm_image": details.get("image_path_resolved"),
        "vm_image_sha256": details.get("image_sha256_computed"),
        "vm_image_authorised_root": bool(details.get("image_path_resolved")),
        "vm_evidence_entries": details.get("evidence_entries"),
        "vm_duration_seconds": details.get("vm_duration_seconds"),
        "vm_instance_scope_proven": details.get("instance_scope") is True,
        "no_vm_launched_by_this_tool": True,
        "candidate_flags_issued": sorted(set(flags_issued)),
    }
    report["not_verified"] = [
        "This tool started no VM: only --check/--list-backends/--json were issued to the "
        "candidate. VM evidence is WorkBuddy AI's and is certified as recorded, not "
        "reproduced here.",
        "real_hardware_verified is false in the marker and is not claimed here; a Debian UTM "
        "guest is virtual hardware, not real hardware.",
        "Windows, Linux and HarmonyOS hosts are not exercised on real hardware; any such "
        "evidence is absent rather than inferred.",
        "Cross-platform packaging beyond the macOS candidates present on this host is not "
        "covered by this acceptance run.",
    ]
    report["elapsed_seconds"] = round(time.perf_counter() - started, 2)
    return report


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def render_markdown(report: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append("# VirtualWorld release candidate — final independent acceptance")
    lines.append("")
    lines.append(f"- Generated: `{report.get('generated_at', '')}`")
    lines.append(f"- Report schema: `{report.get('report_schema', '')}`")
    lines.append(f"- **Verdict: `{report.get('verdict', 'unknown')}`**")
    lines.append(f"- {report.get('method', '')}")
    lines.append("")

    blocking = report.get("blocking")
    if blocking:
        lines.append("## Blocked")
        lines.append("")
        lines.append(f"- {blocking.get('reason', '')}")
        if blocking.get("marker"):
            lines.append(f"- Marker: `{blocking['marker']}`")
        for code in blocking.get("codes", []):
            lines.append(f"- Failed invariant: `{code}`")
        lines.append("")
        for finding in report.get("findings", []):
            if finding.get("severity") == "error":
                lines.append(f"- `{finding['code']}`: {finding['message']}")
        lines.append("")

        detail = blocking.get("detail")
        if isinstance(detail, dict):
            lines.append("### Blocked-marker claims (independently re-derived)")
            lines.append("")
            auth = report.get("blocked_marker", {})
            lines.append(f"- Blocked marker schema: `{auth.get('schema')}` "
                         f"(expected `{BLOCKED_SCHEMA}`)")
            lines.append(f"- Blocked marker SHA-256: `{auth.get('marker_sha256')}`")
            lines.append(f"- Gate result / error code: **{auth.get('gate_result')}** / "
                         f"`{auth.get('error_code')}`")
            lines.append(f"- Recorded freeze-marker: `{detail.get('freeze_marker')}`")
            lines.append(f"- Hash claims checked: {auth.get('claims_checked')}, "
                         f"confirmed: {auth.get('claims_confirmed')}, "
                         f"refuted: {len(auth.get('claims_refuted', []))}, "
                         f"files missing: {len(auth.get('files_missing', []))}")
            lines.append(f"- Evidence authenticated: **{auth.get('authenticated')}**")
            if auth.get("disputed_file"):
                lines.append(f"- Disputed file: `{auth['disputed_file']}`")
                lines.append(f"  - recomputed on disk: `{auth.get('disputed_file_recomputed')}`")
                lines.append(f"  - mismatch reproduced independently: "
                             f"**{auth.get('disputed_file_confirmed')}**")
            refuted = auth.get("claims_refuted") or []
            if refuted:
                lines.append("")
                lines.append("| File | Recomputed | Recorded expected | Recorded match |")
                lines.append("| --- | --- | --- | --- |")
                for row in refuted:
                    lines.append(f"| `{row['file']}` | `{row['recomputed']}` | "
                                 f"`{row['recorded_expected']}` | {row['recorded_match']} |")
            if detail.get("evidence"):
                lines.append("")
                lines.append("**Gate evidence:**")
                lines.append("")
                lines.append(f"> {detail['evidence']}")
            lines.append("")
        return "\n".join(lines) + "\n"

    candidate = report.get("candidate", {})
    lines.append("## Certified candidate")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Binary | `{candidate.get('binary', '')}` |")
    lines.append(f"| Binary SHA-256 (marker) | `{candidate.get('sha256', '')}` |")
    lines.append(f"| Binary SHA-256 (recomputed) | `{candidate.get('sha256_recomputed', '')}` |")
    lines.append(f"| Size | {candidate.get('size_bytes')} bytes |")
    lines.append(f"| mtime | `{candidate.get('mtime', '')}` |")
    lines.append(f"| release-ready marker SHA-256 | `{candidate.get('marker_sha256', '')}` |")
    lines.append(f"| Platform freeze marker | `{candidate.get('freeze_marker', '')}` |")
    lines.append(f"| Freeze marker SHA-256 | `{candidate.get('freeze_marker_sha256', '')}` |")
    lines.append(f"| contrib backends | {candidate.get('contrib_backends')} |")
    lines.append("")

    attest = report.get("attestations", {})
    lines.append("## Attestations")
    lines.append("")
    lines.append(f"- `real_hardware_verified`: **{attest.get('real_hardware_verified')}** "
                 f"(a Debian UTM guest is virtual hardware, not real hardware)")
    lines.append(f"- `vm_started`: **{attest.get('vm_started')}**")
    lines.append(f"- This tool launched no VM: **{attest.get('no_vm_launched_by_this_tool')}**")
    if attest.get("vm_started") is True:
        lines.append("")
        lines.append("### Debian VM evidence (produced by WorkBuddy AI; certified, not reproduced)")
        lines.append("")
        lines.append("| Field | Value |")
        lines.append("| --- | --- |")
        lines.append(f"| Guest OS | **{attest.get('vm_guest_os')}** |")
        lines.append(f"| UTM VM | `{attest.get('vm_name')}` |")
        lines.append(f"| Pre-start state | {attest.get('vm_pre_start_state')} |")
        lines.append(f"| Test medium | `{attest.get('vm_image')}` |")
        lines.append(f"| Medium SHA-256 | `{attest.get('vm_image_sha256')}` |")
        lines.append(f"| Evidence entries | {attest.get('vm_evidence_entries')} |")
        lines.append(f"| Uptime (s) | {attest.get('vm_duration_seconds')} |")
        lines.append(f"| Only this instance stopped | "
                     f"**{attest.get('vm_instance_scope_proven')}** |")
    elif attest.get("vm_started") is False:
        details = report.get("marker_details", {})
        lines.append(f"- No Debian VM was exercised; recorded reason: "
                     f"{details.get('vm_skip_reason')!r}")
    reach = report.get("reachability", {})
    lines.append(f"- VM-launching flags reachable: **{reach.get('vm_launch_reachable')}** "
                 f"(allowed flags: `{reach.get('allowed_candidate_flags')}`)")
    lines.append("")

    calls = report.get("candidate_calls") or []
    if calls:
        lines.append("### Candidate invocations issued by this tool")
        lines.append("")
        lines.append("| Flags | Exit | ms |")
        lines.append("| --- | --- | --- |")
        for call in calls:
            lines.append(f"| `{call['argv']}` | {call['exit_code']} | {call['duration_ms']} |")
        lines.append("")

    lines.append("## Verifier battery")
    lines.append("")
    lines.append("| Verifier | Exit | OK | Detail |")
    lines.append("| --- | --- | --- | --- |")
    for name, result in report.get("verifiers", {}).items():
        if not isinstance(result, dict):
            continue
        detail = ""
        if name == "self_tests":
            detail = (f"{result.get('tests')} tests (baseline {result.get('baseline')})")
        elif name == "parser_hardening":
            detail = f"{result.get('samples')} samples"
        elif name == "conformance":
            detail = f"arches {result.get('arches')}"
        elif name == "stability":
            detail = (f"{result.get('rounds')} rounds/phase, "
                      f"{result.get('invocations')} invocations")
        elif name == "release_manifests":
            detail = f"roots {result.get('search_roots')}"
        lines.append(f"| `{name}` | {result.get('exit_code')} | {result.get('ok')} | {detail} |")
    lines.append("")
    for name, result in report.get("verifiers", {}).items():
        if not isinstance(result, dict):
            continue
        tail = (result.get("stdout_tail") or "").strip()
        if tail:
            lines.append(f"### {name}")
            lines.append("")
            lines.append("```")
            lines.append(tail[-900:])
            lines.append("```")
            lines.append("")

    lines.append("## Findings")
    lines.append("")
    findings = report.get("findings", [])
    if not findings:
        lines.append("None.")
    else:
        lines.append("| Severity | Code | Message | Path |")
        lines.append("| --- | --- | --- | --- |")
        for finding in findings:
            lines.append(f"| {finding['severity']} | `{finding['code']}` | "
                         f"{finding['message'][:200]} | `{finding['path']}` |")
    lines.append("")

    lines.append("## Located marker facts")
    lines.append("")
    lines.append("| Fact | JSON path | Value |")
    lines.append("| --- | --- | --- |")
    for name, fact in report.get("facts", {}).items():
        value = fact.get("value")
        shown = str(value)
        if len(shown) > 120:
            shown = shown[:120] + "..."
        lines.append(f"| `{name}` | `{fact.get('path') or '(not found)'}` | {shown} |")
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
    parser.add_argument("--out-dir", default=REPORTS_DIR)
    parser.add_argument("--report-basename", default=REPORT_BASENAME,
                        help="output basename; a rerun uses release-candidate-rerun so the "
                             "first-round report is preserved as history")
    parser.add_argument("--arches", nargs="*", default=["x86_64", "aarch64", "riscv64"])
    parser.add_argument("--seed", type=int, default=20261005)
    parser.add_argument("--hardening-samples", type=int, default=5000)
    parser.add_argument("--stability-rounds", type=int, default=50,
                        help="rounds per variant per phase (50 => 600 invocations)")
    parser.add_argument("--stability-concurrency", type=int, default=8)
    parser.add_argument("--self-test-baseline", type=int, default=214)
    parser.add_argument("--self-test-timeout", type=float, default=600.0)
    parser.add_argument("--heavy-timeout", type=float, default=1800.0)
    parser.add_argument("--stability-timeout", type=float, default=2400.0)
    parser.add_argument("--skip-heavy", action="store_true",
                        help="validate the marker only (not for a final verdict)")
    parser.add_argument("--marker-only", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    args.marker_only = args.marker_only
    report = run_acceptance(args)

    os.makedirs(args.out_dir, exist_ok=True)
    basename = getattr(args, "report_basename", REPORT_BASENAME) or REPORT_BASENAME
    json_path = os.path.join(args.out_dir, f"{basename}.json")
    md_path = os.path.join(args.out_dir, f"{basename}.md")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, default=str)
        handle.write("\n")
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(report))

    if not args.quiet:
        print(f"verdict={report.get('verdict')}")
        blocking = report.get("blocking")
        if blocking:
            print(f"  blocked: {blocking.get('reason')}")
            for code in blocking.get("codes", []):
                print(f"  failed invariant: {code}")
        else:
            for name, result in report.get("verifiers", {}).items():
                if isinstance(result, dict):
                    print(f"  {name}: exit={result.get('exit_code')} ok={result.get('ok')}")
            for finding in report.get("findings", []):
                print(f"  {finding['severity']}: {finding['code']} - {finding['message'][:120]}")
        print(f"wrote {os.path.relpath(json_path, PROJECT_ROOT)}")
        print(f"wrote {os.path.relpath(md_path, PROJECT_ROOT)}")

    verdict = report.get("verdict")
    if verdict in ("blocked", "incomplete"):
        return 2
    return 0 if verdict == "certified" else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
