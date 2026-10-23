#!/usr/bin/env python3
"""Static policy scan for the isolated no-JIT prototype sources."""
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = (ROOT / "include", ROOT / "src", ROOT / "tests")
COMMENT_OR_STRING = re.compile(
    r"/\*[\s\S]*?\*/|//[^\n]*|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'"
)
FORBIDDEN = {
    "process creation / exec": re.compile(
        r"\b(?:fork|vfork|posix_spawn|posix_spawnp|system|popen|"
        r"execl|execle|execlp|execv|execve|execvp|execvpe)\s*\("
    ),
    "QProcess": re.compile(r"\bQProcess\b"),
    "executable permission / JIT mapping": re.compile(
        r"\b(?:PROT_EXEC|MAP_JIT|JIT_WRITE_PROTECT|"
        r"pthread_jit_write_protect_np|PAGE_EXECUTE[A-Z_]*|"
        r"VirtualProtect|vm_protect)\b"
    ),
    "mprotect": re.compile(r"\bmprotect\s*\("),
    "generated-code cache flush": re.compile(r"__builtin___clear_cache\s*\("),
}


def strip_non_code(text: str) -> str:
    def blank(match: re.Match[str]) -> str:
        return "".join("\n" if char == "\n" else " " for char in match.group(0))

    return COMMENT_OR_STRING.sub(blank, text)


def main() -> int:
    files = sorted(
        path
        for directory in SOURCE_DIRS
        for path in directory.rglob("*")
        if path.is_file() and path.suffix in {".c", ".h"}
    )
    hits: list[tuple[Path, int, str]] = []
    for path in files:
        source = strip_non_code(path.read_text(encoding="utf-8", errors="replace"))
        for label, pattern in FORBIDDEN.items():
            for match in pattern.finditer(source):
                line = source.count("\n", 0, match.start()) + 1
                hits.append((path.relative_to(ROOT), line, label))

    if hits:
        for path, line, label in hits:
            print(f"FAIL {path}:{line}: {label}")
        print(f"FAIL: {len(hits)} forbidden code reference(s) in {len(files)} C/header files")
        return 1

    print(f"PASS: {len(files)} C/header files scanned; 0 process-spawn, executable-memory, or JIT references")
    return 0


if __name__ == "__main__":
    sys.exit(main())
