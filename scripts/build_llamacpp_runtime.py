#!/usr/bin/env python3
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "third_party" / "llama.cpp"
RUNTIME = ROOT / "runtime" / "llama.cpp"
LLAMA_CPP_REPO = os.environ.get("LOCALAI_LLAMA_CPP_REPO", "https://github.com/ggml-org/llama.cpp.git")
LLAMA_CPP_REF = os.environ.get("LOCALAI_LLAMA_CPP_REF", "master")


def host_platform():
    name = platform.system().lower()
    if name == "darwin":
        return "macos"
    if name == "windows":
        return "windows"
    if "harmony" in name or "ohos" in name:
        return "harmonyos"
    return "linux"


def host_arch():
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    if machine in ("amd64", "x86_64"):
        return "x64"
    if "riscv" in machine:
        return "riscv64"
    if "loongarch" in machine:
        return "loongarch64"
    return machine or "unknown"


def cmake_options(system_name, arch):
    options = [
        "-DCMAKE_BUILD_TYPE=Release",
        "-DGGML_NATIVE=OFF",
        "-DLLAMA_BUILD_TESTS=OFF",
        "-DLLAMA_BUILD_EXAMPLES=ON",
        "-DLLAMA_BUILD_SERVER=ON",
    ]
    if system_name == "macos":
        options.append("-DGGML_ACCELERATE=ON")
        if arch == "arm64":
            options.append("-DGGML_METAL=ON")
    elif system_name == "windows":
        options.append("-DGGML_VULKAN=ON")
    elif system_name in ("linux", "harmonyos"):
        if arch == "loongarch64":
            options.extend([
                "-DGGML_CPU=ON",
                "-DGGML_OPENMP=ON",
                "-DGGML_LASX=ON",
                "-DGGML_LSX=ON",
                "-DGGML_VULKAN=OFF",
            ])
            return options
        options.append("-DGGML_BLAS=ON")
        options.append("-DGGML_BLAS_VENDOR=OpenBLAS")
        if arch in ("x64", "arm64"):
            options.append("-DGGML_VULKAN=ON")
    return options


def run(command, cwd=None):
    print("+", " ".join(str(part) for part in command))
    subprocess.check_call([str(part) for part in command], cwd=str(cwd) if cwd else None)


def ensure_source():
    if (SOURCE / "CMakeLists.txt").exists():
        return
    git = shutil.which("git")
    if not git:
        raise SystemExit(f"llama.cpp source not found and git is unavailable: {SOURCE}")
    SOURCE.parent.mkdir(parents=True, exist_ok=True)
    run([git, "clone", "--depth", "1", "--branch", LLAMA_CPP_REF, LLAMA_CPP_REPO, SOURCE])


def executable_names(system_name):
    suffix = ".exe" if system_name == "windows" else ""
    return [f"llama-cli{suffix}", f"llama-server{suffix}"]


def shared_library_patterns(system_name):
    if system_name == "windows":
        return ["*.dll"]
    if system_name == "macos":
        return ["*.dylib"]
    return ["*.so", "*.so.*"]


def find_built_binary(build_dir, name):
    for candidate in (
        build_dir / "bin" / name,
        build_dir / "bin" / "Release" / name,
        build_dir / "examples" / "main" / name,
        build_dir / name,
    ):
        if candidate.exists():
            return candidate
    for candidate in build_dir.rglob(name):
        if candidate.is_file():
            return candidate
    return None


def main():
    ensure_source()

    system_name = os.environ.get("LOCALAI_TARGET_OS") or host_platform()
    arch = os.environ.get("LOCALAI_TARGET_ARCH") or host_arch()
    build_dir = ROOT / "build" / "llama.cpp" / system_name / arch
    output_bin = RUNTIME / system_name / arch / "bin"
    build_dir.mkdir(parents=True, exist_ok=True)
    output_bin.mkdir(parents=True, exist_ok=True)

    configure = ["cmake", "-S", SOURCE, "-B", build_dir, *cmake_options(system_name, arch)]
    try:
        run(configure)
    except subprocess.CalledProcessError:
        fallback = [
            "cmake", "-S", SOURCE, "-B", build_dir,
            "-DCMAKE_BUILD_TYPE=Release",
            "-DGGML_NATIVE=OFF",
            "-DLLAMA_BUILD_TESTS=OFF",
            "-DLLAMA_BUILD_EXAMPLES=ON",
            "-DLLAMA_BUILD_SERVER=ON",
        ]
        run(fallback)

    run(["cmake", "--build", build_dir, "--config", "Release", "--target", "llama-cli"])
    try:
        run(["cmake", "--build", build_dir, "--config", "Release", "--target", "llama-server"])
    except subprocess.CalledProcessError:
        pass

    copied = []
    for name in executable_names(system_name):
        binary = find_built_binary(build_dir, name)
        if not binary:
            continue
        target = output_bin / name
        shutil.copy2(binary, target)
        target.chmod(target.stat().st_mode | 0o111)
        copied.append(target)

    for pattern in shared_library_patterns(system_name):
        for library in (build_dir / "bin").glob(pattern):
            target = output_bin / library.name
            shutil.copy2(library, target)
            copied.append(target)

    if not copied:
        raise SystemExit("No llama.cpp runtime binaries were produced.")
    print("llama.cpp runtime:")
    for path in copied:
        print(path)


if __name__ == "__main__":
    main()
