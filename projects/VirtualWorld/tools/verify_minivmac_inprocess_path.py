#!/usr/bin/env python3
"""Verify the Mini vMac in-process launch path has no spawn/PID semantics.

This tool performs FUNCTION/BRANCH-LEVEL scanning of VirtualMachineWindow.cpp,
main.cpp, and SystemSoftwareEngine.cpp — the three files that contain both
Mini vMac (isSystemSoftwareConfig) and QEMU (QProcess/process_) code paths.

The key insight: VirtualMachineWindow.cpp has both QEMU branches (which
legitimately use QProcess/process_) and Mini vMac branches (which must NOT).
A whole-file allow-list would blind the gate to forbidden APIs creeping into
the Mini vMac path. This script parses the source to distinguish them.

Exit codes:
  0 — Mini vMac path is clean (no forbidden APIs in isSystemSoftwareConfig branches)
  1 — HARD CONSTRAINT VIOLATION (forbidden API found in Mini vMac path)
  2 — usage error

Usage:
  python3 tools/verify_minivmac_inprocess_path.py [--src-dir DIR] [--verbose]
"""

from __future__ import annotations

import os
import re
import sys

# --- Configuration ---------------------------------------------------------

# APIs that are FORBIDDEN in the Mini vMac (isSystemSoftwareConfig) path.
# These represent process spawning or PID semantics which violate the
# HarmonyOS/OpenHarmony in-process constraint.
FORBIDDEN_IN_MINIVMAC = [
    "QProcess",
    "process_",          # QProcess member variable
    "startDetached",
    "fork",
    "vfork",
    "execv",
    "execve",
    "execvp",
    "execl",
    "execlp",
    "execle",
    "system",
    "popen",
    "posix_spawn",
    "posix_spawnp",
    "processId",         # PID semantics — Mini vMac has no PID
    "waitForStarted",    # QProcess lifecycle
    "waitForFinished",   # QProcess lifecycle
    "waitForReadyRead",  # QProcess I/O
    "waitForBytesWritten",  # QProcess I/O
    "readAllStandardError",  # QProcess I/O
    "readAllStandardOutput",  # QProcess I/O
    "sendQmp",           # QMP commands
    "sendMonitorCommand",  # QEMU monitor commands
    "screendump",        # QEMU screendump
]

# APIs that are advisory-only in the Mini vMac path — they reference QEMU
# infrastructure but are harmless cleanup (no-op when QMP was never opened).
# Reported but do not fail the gate.
ADVISORY_IN_MINIVMAC = [
    "qmpSocket_",        # QEMU Monitor Protocol — Mini vMac has no QMP
    "qmpCapabilitiesReady_",  # QMP state
    "qmpConnectTimer_",  # QMP connection timer
    "connectQmp",        # QMP connection
    "closeQmp",          # QMP close (harmless cleanup when socket was never opened)
    "processQmpData",    # QMP data processing
]

ALL_CHECKED = FORBIDDEN_IN_MINIVMAC + ADVISORY_IN_MINIVMAC

# Regex for all checked APIs (word boundary matching)
CHECKED_RE = re.compile(
    r"\b(" + "|".join(re.escape(f) for f in ALL_CHECKED) + r")\b"
)

FORBIDDEN_SET = set(FORBIDDEN_IN_MINIVMAC)
ADVISORY_SET = set(ADVISORY_IN_MINIVMAC)

# Pattern that identifies a Mini vMac branch guard (must be in an if-context).
# We only match isSystemSoftwareConfig when preceded by 'if' to avoid
# false matches in return statements, assignments, etc.
#   if (isSystemSoftwareConfig(...))  →  match
#   return isSystemSoftwareConfig(...)  →  NO match
ISSC_IF_RE = re.compile(r"if\s*\(\s*isSystemSoftwareConfig\s*\(")

# Pattern that identifies a QEMU signal handler (connect(&process_, ...))
QEMU_HANDLER_RE = re.compile(r"connect\s*\(\s*&process_")

# Files that contain both Mini vMac and QEMU paths and need branch-level scanning
DUAL_PATH_FILES = [
    "VirtualMachineWindow.cpp",
]

# Files that are exclusively Mini vMac path — any forbidden API is a violation
MINIVMAC_ONLY_FILES = [
    "SystemSoftwareEngine.cpp",
    "SystemSoftwareEngine.hpp",
]

# main.cpp: the system-software branch is Mini vMac path
MAIN_CPP = "main.cpp"


def strip_comments_and_strings(text: str) -> str:
    """Remove // and /* */ comments plus string/char literals."""
    out = []
    i, n = 0, len(text)
    state = "code"
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if state == "code":
            if c == "/" and nxt == "/":
                state = "line"
                i += 2
                continue
            if c == "/" and nxt == "*":
                state = "block"
                i += 2
                continue
            if c == '"':
                state = "string"
                i += 1
                out.append('""')
                continue
            if c == "'":
                state = "char"
                i += 1
                out.append("''")
                continue
            out.append(c)
            i += 1
        elif state == "line":
            if c == "\n":
                state = "code"
                out.append(c)
            i += 1
        elif state == "block":
            if c == "*" and nxt == "/":
                state = "code"
                i += 2
                continue
            i += 1
        elif state == "string":
            if c == "\\":
                i += 2
                continue
            if c == '"':
                state = "code"
            i += 1
        elif state == "char":
            if c == "\\":
                i += 2
                continue
            if c == "'":
                state = "code"
            i += 1
    return "".join(out)


def find_issc_blocks(code: str) -> list[dict]:
    """Find isSystemSoftwareConfig if-blocks with precise range tracking.

    Returns list of dicts with keys:
      start_line: first line of the if-block body (1-based)
      end_line: last line of the if-block body (1-based, inclusive)
      is_nested_in_qemu_handler: True if this ISSC check is inside a
          connect(&process_, ...) lambda (QEMU signal handler context)

    Handles:
      - Brace-delimited blocks: if (issc(..)) { ... }
      - Single-statement (early-return): if (issc(..)) return expr;
      - } else if: the else-if branch is NOT included in the ISSC range
    """
    lines = code.splitlines()
    blocks = []
    i = 0

    # Pre-scan: find lines that are inside QEMU signal handler lambdas
    # (connect(&process_, ...) lambdas). These are QEMU context even if
    # they contain isSystemSoftwareConfig sub-checks.
    qemu_handler_lines = set()
    in_qemu_handler = False
    qemu_handler_depth = 0
    for idx, line in enumerate(lines):
        if QEMU_HANDLER_RE.search(line):
            in_qemu_handler = True
            qemu_handler_depth = 0
            # Count opening braces on this line
            for ch in line:
                if ch == "{":
                    qemu_handler_depth += 1
            continue
        if in_qemu_handler:
            for ch in line:
                if ch == "{":
                    qemu_handler_depth += 1
                elif ch == "}":
                    qemu_handler_depth -= 1
            if qemu_handler_depth <= 0:
                in_qemu_handler = False
            else:
                qemu_handler_lines.add(idx + 1)  # 1-based

    while i < len(lines):
        line = lines[i]
        if not ISSC_IF_RE.search(line):
            i += 1
            continue

        # Found an isSystemSoftwareConfig guard. Determine the extent.
        is_in_qemu_handler = (i + 1) in qemu_handler_lines

        # Check for single-statement form on same line:
        #   if (isSystemSoftwareConfig(..)) return expr;
        rest = line[line.index("isSystemSoftwareConfig"):]
        if "return " in rest and ";" in rest and "{" not in rest:
            blocks.append({
                "start_line": i + 1,
                "end_line": i + 1,
                "is_nested_in_qemu_handler": is_in_qemu_handler,
            })
            i += 1
            continue

        # Find opening brace (may be on same line or next few lines)
        j = i
        found_brace = False
        while j < len(lines) and j < i + 5:
            if "{" in lines[j]:
                found_brace = True
                break
            j += 1

        if not found_brace:
            # Single-statement form spanning lines
            k = i
            while k < len(lines) and k < i + 5:
                if ";" in lines[k]:
                    blocks.append({
                        "start_line": i + 1,
                        "end_line": k + 1,
                        "is_nested_in_qemu_handler": is_in_qemu_handler,
                    })
                    i = k + 1
                    break
                k += 1
            else:
                i += 1
            continue

        # Track brace depth from the line with opening brace.
        # IMPORTANT: We need to find where the ISSC if-block ends,
        # NOT where the outer brace structure ends.
        # The ISSC if-block ends when we hit the matching } for the
        # opening { of the if-block. If the line with the closing }
        # also has "else", the ISSC block ends at the }, and the
        # code after "} else" is NOT in the ISSC branch.
        depth = 0
        start_line = i + 1  # 1-based
        end_line = j + 1
        found_end = False

        for k in range(j, len(lines)):
            for ch in lines[k]:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        # The closing } of the if-block.
                        # Check if this line also has "else" after the }.
                        # If so, the ISSC block ends at this } and the
                        # code after "} else" is NOT in the ISSC branch.
                        end_line = k + 1
                        blocks.append({
                            "start_line": start_line,
                            "end_line": end_line,
                            "is_nested_in_qemu_handler": is_in_qemu_handler,
                        })
                        found_end = True
                        i = k + 1
                        break
            if found_end:
                break
        else:
            # Unmatched braces — shouldn't happen in valid code
            blocks.append({
                "start_line": start_line,
                "end_line": len(lines),
                "is_nested_in_qemu_handler": is_in_qemu_handler,
            })
            i = len(lines)

        if not found_end:
            i += 1

    return blocks


def is_hit_in_issc_branch(lineno: int, api: str, line_text: str,
                           issc_blocks: list[dict]) -> tuple[bool, bool]:
    """Determine if a forbidden API hit at the given line is in an ISSC branch.

    Returns (in_issc, is_qemu_nested):
      in_issc: True if the hit is inside an isSystemSoftwareConfig if-block
      is_qemu_nested: True if the ISSC block is nested inside a QEMU handler

    Handles the "} else if" case: if the hit is on the same line as the
    closing } of the ISSC block and appears after "}", it's NOT in the ISSC
    branch (it's in the else/else-if path).
    """
    for block in issc_blocks:
        if block["start_line"] <= lineno <= block["end_line"]:
            # Check for the "} else" case: if the hit line is the end_line
            # and the line contains "} else", the hit might be after the }
            # and thus in the else path.
            if lineno == block["end_line"]:
                line_stripped = line_text.strip()
                if line_stripped.startswith("}") and "else" in line_stripped:
                    # The line starts with } and has else — this is the
                    # closing } of the ISSC block followed by else.
                    # Check if the API hit is BEFORE or AFTER the }.
                    brace_pos = line_text.index("}")
                    api_pos = line_text.find(api)
                    if api_pos > brace_pos:
                        # API is after the closing }, in the else path.
                        # NOT in the ISSC branch.
                        continue
            return True, block["is_nested_in_qemu_handler"]
    return False, False


def scan_dual_path_file(filepath: str, verbose: bool = False) -> tuple[list, list, list, int]:
    """Scan a file that has both Mini vMac and QEMU paths.

    Returns (violations, advisories, qemu_hits, total_issc_blocks) where:
      violations: list of (line, api, text) for HARD forbidden APIs in Mini vMac branches
      advisories: list of (line, api, text) for advisory APIs in Mini vMac branches
      qemu_hits:  list of (line, api, text) for checked APIs in QEMU branches
      total_issc_blocks: count of isSystemSoftwareConfig blocks found
    """
    if not os.path.isfile(filepath):
        return [], [], [], 0

    raw = open(filepath, errors="replace").read()
    code = strip_comments_and_strings(raw)
    issc_blocks = find_issc_blocks(code)

    violations = []
    advisories = []
    qemu_hits = []
    lines = code.splitlines()

    for lineno_0, line in enumerate(lines):
        lineno = lineno_0 + 1
        for m in CHECKED_RE.finditer(line):
            api = m.group(1)
            # Skip if the forbidden word is part of a longer identifier
            after = line[m.end():m.end() + 1]
            if after.isalnum() or after == "_":
                continue
            # Determine if this line is inside an isSystemSoftwareConfig branch
            in_issc, is_qemu_nested = is_hit_in_issc_branch(lineno, api, line, issc_blocks)
            hit = (lineno, api, line.strip()[:100])
            if in_issc:
                if is_qemu_nested:
                    # This ISSC check is inside a QEMU signal handler.
                    # The QEMU context takes precedence — the forbidden API
                    # is in the QEMU handler, not in the Mini vMac path.
                    qemu_hits.append(hit)
                    if verbose:
                        print(f"  QEMU-NESTED: {os.path.basename(filepath)}:{lineno} {api} (ISSC inside QEMU handler)")
                elif api in ADVISORY_SET:
                    advisories.append(hit)
                    if verbose:
                        print(f"  ADVISORY: {os.path.basename(filepath)}:{lineno} {api} (in ISSC branch, harmless cleanup)")
                else:
                    violations.append(hit)
                    if verbose:
                        print(f"  VIOLATION: {os.path.basename(filepath)}:{lineno} {api} (in ISSC branch)")
            else:
                qemu_hits.append(hit)

    return violations, advisories, qemu_hits, len(issc_blocks)


def scan_minivmac_only_file(filepath: str, verbose: bool = False) -> list:
    """Scan a file that should have ZERO forbidden APIs anywhere."""
    if not os.path.isfile(filepath):
        return []
    raw = open(filepath, errors="replace").read()
    code = strip_comments_and_strings(raw)
    violations = []
    for lineno_0, line in enumerate(code.splitlines()):
        lineno = lineno_0 + 1
        for m in CHECKED_RE.finditer(line):
            api = m.group(1)
            after = line[m.end():m.end() + 1]
            if after.isalnum() or after == "_":
                continue
            if api in ADVISORY_SET:
                continue  # advisory APIs are OK in Mini vMac only files
            violations.append((lineno, api, line.strip()[:100]))
            if verbose:
                print(f"  VIOLATION: {os.path.basename(filepath)}:{lineno} {api}")
    return violations


def scan_main_cpp(filepath: str, verbose: bool = False) -> tuple[list, list]:
    """Scan main.cpp — the system-software branch is Mini vMac path.

    Returns (violations, qemu_hits).
    """
    if not os.path.isfile(filepath):
        return [], []
    raw = open(filepath, errors="replace").read()
    code = strip_comments_and_strings(raw)
    issc_blocks = find_issc_blocks(code)
    violations = []
    qemu_hits = []
    lines = code.splitlines()

    for lineno_0, line in enumerate(lines):
        lineno = lineno_0 + 1
        for m in CHECKED_RE.finditer(line):
            api = m.group(1)
            after = line[m.end():m.end() + 1]
            if after.isalnum() or after == "_":
                continue
            in_issc, is_qemu_nested = is_hit_in_issc_branch(lineno, api, line, issc_blocks)
            hit = (lineno, api, line.strip()[:100])
            if in_issc and not is_qemu_nested:
                if api not in ADVISORY_SET:
                    violations.append(hit)
                    if verbose:
                        print(f"  VIOLATION: {os.path.basename(filepath)}:{lineno} {api} (in system-software branch)")
            else:
                qemu_hits.append(hit)

    return violations, qemu_hits


def run_scan(src_dir: str, verbose: bool = False, debug: bool = False) -> int:
    """Run the Mini vMac branch-level scan.

    Args:
        src_dir:  Root source directory containing the target .cpp/.hpp files.
        verbose:  Print per-hit details.
        debug:    Print ISSC block ranges.

    Returns:
        0 — Mini vMac path is clean (no forbidden APIs in ISSC branches)
        1 — HARD CONSTRAINT VIOLATION
    """
    all_violations: list[tuple] = []
    all_advisories: list[tuple] = []
    all_qemu_hits: list[tuple] = []
    total_issc = 0

    # 1. Scan dual-path files (VirtualMachineWindow.cpp)
    for fname in DUAL_PATH_FILES:
        fpath = os.path.join(src_dir, fname)
        v, a, q, n_issc = scan_dual_path_file(fpath, verbose)
        all_violations.extend([(fname,) + h for h in v])
        all_advisories.extend([(fname,) + h for h in a])
        all_qemu_hits.extend([(fname,) + h for h in q])
        total_issc += n_issc
        print(f"  {fname}: {n_issc} isSystemSoftwareConfig branches, "
              f"{len(v)} violations, {len(a)} advisories, {len(q)} QEMU-path hits")
        if debug:
            if os.path.isfile(fpath):
                raw = open(fpath, errors="replace").read()
                code = strip_comments_and_strings(raw)
                blocks = find_issc_blocks(code)
                for b in blocks:
                    tag = " [QEMU-NESTED]" if b["is_nested_in_qemu_handler"] else ""
                    print(f"    ISSC block: lines {b['start_line']}-{b['end_line']}{tag}")

    # 2. Scan Mini v Mac-only files (SystemSoftwareEngine.cpp/.hpp)
    for fname in MINIVMAC_ONLY_FILES:
        fpath = os.path.join(src_dir, fname)
        v = scan_minivmac_only_file(fpath, verbose)
        all_violations.extend([(fname,) + h for h in v])
        print(f"  {fname}: {len(v)} violations (any forbidden API is a violation)")

    # 3. Scan main.cpp
    fpath = os.path.join(src_dir, MAIN_CPP)
    v, q = scan_main_cpp(fpath, verbose)
    all_violations.extend([(MAIN_CPP,) + h for h in v])
    all_qemu_hits.extend([(MAIN_CPP,) + h for h in q])
    print(f"  {MAIN_CPP}: {len(v)} violations, {len(q)} QEMU-path hits")

    # Summary
    print()
    print(f"Total isSystemSoftwareConfig blocks scanned: {total_issc}")
    print(f"Mini vMac path violations (HARD): {len(all_violations)}")
    print(f"Mini vMac path advisories (benign cleanup): {len(all_advisories)}")
    print(f"QEMU-path hits (allowed, outside isSystemSoftwareConfig): {len(all_qemu_hits)}")

    if all_advisories:
        print()
        print("Advisories (harmless QMP cleanup in Mini vMac branch — no gate failure):")
        for fname, lineno, api, text in all_advisories:
            print(f"  {fname}:{lineno} {api} :: {text}")

    if all_violations:
        print()
        print("HARD CONSTRAINT VIOLATIONS (forbidden API in Mini vMac path):")
        for fname, lineno, api, text in all_violations:
            print(f"  {fname}:{lineno} {api} :: {text}")
        print()
        print("RESULT: VIOLATION — Mini vMac path contains forbidden spawn/PID APIs")
        return 1

    print()
    print("RESULT: CLEAN — Mini vMac path has no forbidden spawn/PID APIs")
    return 0


def main() -> int:
    src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
    verbose = "--verbose" in sys.argv
    debug = "--debug" in sys.argv

    if "--src-dir" in sys.argv:
        idx = sys.argv.index("--src-dir")
        if idx + 1 < len(sys.argv):
            src_dir = sys.argv[idx + 1]

    return run_scan(src_dir, verbose=verbose, debug=debug)


if __name__ == "__main__":
    sys.exit(main())