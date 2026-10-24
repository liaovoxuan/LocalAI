"""Select the llama.cpp executables and libraries, without model weights."""

from pathlib import Path
import re


def runtime_datas(runtime_dir):
    runtime_dir = Path(runtime_dir)
    result = []
    for source in sorted(runtime_dir.rglob("*")):
        relative = source.relative_to(runtime_dir)
        if not source.is_file() or any(part.lower() in {"model", "models", "model_backups"} for part in relative.parts):
            continue
        name = source.name.lower()
        is_executable = name in {"llama-cli", "llama-server", "llama-cli.exe", "llama-server.exe"}
        is_library = name.endswith((".dll", ".dylib")) or re.fullmatch(r".+\.so(?:\.\d+)*", name)
        if is_executable or is_library:
            result.append((str(source), (Path("runtime/llama.cpp") / relative.parent).as_posix()))
    return result
