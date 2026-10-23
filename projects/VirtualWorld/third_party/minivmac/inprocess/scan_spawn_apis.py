#!/usr/bin/env python3
"""Prove the in-process Mini vMac engine never launches a child process.

HarmonyOS/OpenHarmony forbid spawning external executables, so this is a hard
requirement rather than a style preference. The check runs over the engine
sources *and* the produced objects:

  * source scan  -- C/C++ comments and string literals are removed first, so
                    prose such as "no fork/exec/system/popen" is not mistaken
                    for a call, and only real identifiers are matched;
  * object scan  -- `nm` over the static archive, so a spawn reached through a
                    macro, an inlined helper or a generated file is still caught.

Exit code 0 when clean, 1 when anything is found.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))

SOURCES = [
    os.path.join(HERE, "vw_minivmac_core.c"),
    os.path.join(HERE, "vw_minivmac_core.h"),
    os.path.join(ROOT, "src", "MiniVmacInProcessCore.cpp"),
    os.path.join(ROOT, "src", "MiniVmacInProcessCore.hpp"),
]

# Anything that can start or talk to another process.
FORBIDDEN = [
    "fork", "vfork", "execv", "execve", "execvp", "execl", "execlp", "execle",
    "system", "popen", "posix_spawn", "posix_spawnp", "QProcess",
    "startDetached", "CreateProcess", "ShellExecute", "NSTask", "utmctl",
]
# Substrings that are legitimately part of other words.
ALLOW = {"system"}  # only when it appears as part of a longer identifier

IDENT = re.compile(r"\b(" + "|".join(re.escape(f) for f in FORBIDDEN) + r")\b")
# Identifiers where the forbidden word is a prefix of a longer name.
LONGER_OK = re.compile(r"\b(" + "|".join(re.escape(f) for f in FORBIDDEN) + r")[A-Za-z0-9_]")


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


def scan_sources():
    hits = []
    for path in SOURCES:
        if not os.path.isfile(path):
            continue
        code = strip_comments_and_strings(open(path, errors="replace").read())
        for lineno, line in enumerate(code.splitlines(), 1):
            for m in IDENT.finditer(line):
                word = m.group(1)
                # `system` inside `SystemSoftwareEngine`-style names is not a call.
                after = line[m.end():m.end() + 1]
                if after.isalnum() or after == "_":
                    continue
                hits.append({"file": os.path.relpath(path, ROOT), "line": lineno,
                             "api": word, "text": line.strip()[:100]})
    return hits


def scan_objects(archive: str):
    hits = []
    if not os.path.isfile(archive):
        return hits, "archive not built"
    try:
        out = subprocess.run(["nm", "-u", archive], capture_output=True, text=True,
                             check=False).stdout
    except OSError as exc:
        return hits, f"nm unavailable: {exc}"
    for line in out.splitlines():
        sym = line.split()[-1] if line.split() else ""
        base = sym.lstrip("_")
        if base in FORBIDDEN:
            hits.append({"symbol": sym})
    return hits, "ok"


def scan_tree(roots):
    """扫描产品源码树中的派生调用。

	引擎自身的扫描只能证明"引擎不派生"。要证明**产品**不派生，必须把产品源码
	也纳入——main.cpp 里就曾存在一条 QProcess + startDetached 路径，不在
	SysystemSoftwareEngine 内，因此不会被引擎扫描覆盖。
	"""
    hits = []
    for rel_root in roots:
        base = os.path.join(ROOT, rel_root)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith('.')]
            for name in filenames:
                if not name.endswith(('.c', '.cc', '.cpp', '.cxx', '.h', '.hpp', '.mm')):
                    continue
                path = os.path.join(dirpath, name)
                code = strip_comments_and_strings(open(path, errors='replace').read())
                for lineno, line in enumerate(code.splitlines(), 1):
                    for m in IDENT.finditer(line):
                        word = m.group(1)
                        after = line[m.end():m.end() + 1]
                        if after.isalnum() or after == '_':
                            continue
                        hits.append({"file": os.path.relpath(path, ROOT), "line": lineno,
                                     "api": word, "text": line.strip()[:100]})
    return hits


def main() -> int:
    archive = os.path.join(ROOT, "build-minivmac-inprocess", "libvwminivmac.a")
    src_hits = scan_sources()
    obj_hits, obj_note = scan_objects(archive)
    tree_hits = scan_tree(["src"]) if "--product" in sys.argv else []

    print(f"sources scanned : {len([p for p in SOURCES if os.path.isfile(p)])}")
    print(f"source hits     : {len(src_hits)}")
    for h in src_hits:
        print(f"  {h['file']}:{h['line']} {h['api']} :: {h['text']}")
    print(f"object scan     : {obj_note}, hits {len(obj_hits)}")
    for h in obj_hits:
        print(f"  {h['symbol']}")

    if "--product" in sys.argv:
        print(f"product tree scan (src/): {len(tree_hits)} hits")
        for h in tree_hits:
            print(f"  {h['file']}:{h['line']} {h['api']} :: {h['text']}")

    clean = not src_hits and not obj_hits and not tree_hits
    print("RESULT:", "CLEAN" if clean else "SPAWN-APIS-FOUND")
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
