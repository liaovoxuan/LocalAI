#!/usr/bin/env python3
"""Reliable local packaging entry point for the desktop applications.

PyInstaller is deliberately invoked through the interpreter running this file.  This
avoids accidentally using a different virtual environment and makes the output
layout and verification rules identical on every supported build host.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Project:
    key: str
    label: str
    directory: str
    executable: str
    specs: dict[str, str]


PROJECTS: dict[str, Project] = {
    "localai": Project(
        "localai", "LocalAI", "LocalAI", "LocalAI", {
            "windows": "LocalAI.spec", "linux": "LocalAI_Linux.spec", "macos": "LocalAI_macOS.spec",
        }
    ),
    "localai-se": Project(
        "localai-se", "LocalAI SE", "LocalAI-SE", "LocalAI_SE", {
            "windows": "LocalAI_SE.spec", "linux": "LocalAI_SE_Linux.spec", "macos": "LocalAI_SE_macOS.spec",
        }
    ),
    "cloudai": Project(
        "cloudai", "CloudAI", "CloudAI", "CloudAI", {
            "windows": "CloudAI.spec", "linux": "CloudAI_Linux.spec", "macos": "CloudAI_macOS.spec",
        }
    ),
}


class PackageError(RuntimeError):
    """A user-actionable packaging failure."""


def host_target() -> str:
    system = platform.system()
    if system == "Darwin":
        return "macos"
    if system == "Windows":
        return "windows"
    if system == "Linux":
        return "linux"
    return system.lower()


def host_arch() -> str:
    value = platform.machine().lower()
    return {
        "amd64": "x86_64", "x86-64": "x86_64", "x64": "x86_64",
        "aarch64": "arm64", "armv8": "arm64", "arm64e": "arm64",
        "i386": "x86", "i686": "x86",
    }.get(value, value or "unknown")


def canonical_arch(value: str) -> str:
    value = value.strip().lower().replace("-", "_")
    aliases = {"native": host_arch(), "amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}
    return aliases.get(value, value)


def project_for(name: str) -> Project:
    key = name.lower().strip()
    if key not in PROJECTS:
        choices = ", ".join(PROJECTS)
        raise PackageError(f"unknown project {name!r}; choose one of: {choices}")
    return PROJECTS[key]


def spec_for(project: Project, target: str) -> Path:
    path = ROOT / "projects" / project.directory / "packaging" / project.specs[target]
    if not path.is_file():
        raise PackageError(f"packaging spec is missing: {path}")
    return path


def output_dir(project: Project, target: str, arch: str, root: Path) -> Path:
    return root / project.key / target / arch


def find_pyinstaller() -> list[str]:
    """Return a PyInstaller command tied to the current Python interpreter."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--version"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        raise PackageError(f"cannot start Python ({exc})") from exc
    if result.returncode:
        raise PackageError(
            "PyInstaller is not installed in this Python environment. "
            "Install the project dependencies first: python3 -m pip install -r requirements.txt"
        )
    return [sys.executable, "-m", "PyInstaller"]


def tkinter_available() -> bool:
    """Check the GUI runtime before PyInstaller can silently omit Tk modules."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import tkinter, tkinter.filedialog, tkinter.messagebox, tkinter.ttk",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def require_tkinter() -> None:
    if not tkinter_available():
        raise PackageError(
            "this desktop build needs a Python with Tkinter (_tkinter). "
            "On macOS, use the official python.org installer; on Linux install python3-tk; "
            "on Windows install the standard Python distribution."
        )


def command_available(name: str) -> bool:
    return shutil.which(name) is not None


def resolve_backend(target: str, requested: str) -> str:
    requested = requested.lower()
    if requested not in {"auto", "native", "wine", "docker"}:
        raise PackageError(f"unsupported backend {requested!r}; use auto, native, wine, or docker")
    if requested != "auto":
        return requested
    if target == host_target():
        return "native"
    if target == "windows" and command_available("wine"):
        return "wine"
    if target == "linux" and (command_available("docker") or command_available("podman")):
        return "docker"
    return "native"


def check_target(target: str, arch: str) -> None:
    current = host_target()
    if target not in {"macos", "windows", "linux"}:
        raise PackageError(f"unsupported target {target!r}; use macos, windows, or linux")
    if target != current:
        raise PackageError(
            f"cannot build {target} on {current}: PyInstaller is not a cross-compiler. "
            f"Run this command on a {target} host or its CI runner."
        )
    if target == "macos" and arch == "universal2":
        return
    if arch not in {"x86_64", "arm64", "x86"}:
        raise PackageError(f"unsupported architecture {arch!r}")
    if arch != host_arch():
        raise PackageError(
            f"architecture {arch} is not the current host architecture {host_arch()}; "
            "build it on a matching host (or use macOS universal2)."
        )


def wine_path(path: Path | str) -> str:
    """Translate a host path to a Wine Windows path."""
    value = str(path)
    if len(value) >= 2 and value[1] == ":":
        return value.replace("/", "\\")
    winepath = shutil.which("winepath")
    if winepath:
        result = subprocess.run([winepath, "-w", value], capture_output=True, text=True, check=False)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return "Z:" + value.replace("/", "\\")


def wine_python() -> str:
    configured = os.environ.get("LOCALAI_WINE_PYTHON") or os.environ.get("WINE_PYTHON")
    if not configured:
        raise PackageError(
            "Wine is available, but no Windows Python was configured. "
            "Install Python and PyInstaller inside a Wine prefix, then set "
            "LOCALAI_WINE_PYTHON=/path/to/python.exe (or pass --wine-python)."
        )
    return configured


def wine_executable() -> str:
    """Find Wine even when a wrapper (such as Whisky) is not on PATH."""
    configured = os.environ.get("LOCALAI_WINE") or os.environ.get("WINE")
    if configured:
        return configured
    for candidate in ("wine", "wine64"):
        if command_available(candidate):
            return candidate
    whisky_wine = Path.home() / "Library" / "Application Support" / "com.franke.Whisky" / "Libraries" / "Wine" / "bin" / "wine"
    if whisky_wine.is_file():
        return str(whisky_wine)
    raise PackageError("Wine backend requested but the wine command is not installed")


def container_engine() -> str:
    for candidate in ("docker", "podman"):
        if command_available(candidate):
            return candidate
    raise PackageError("Linux container build requires Docker or Podman")


def iter_files(path: Path) -> Iterable[Path]:
    if path.is_file():
        yield path
        return
    if path.is_dir():
        yield from (item for item in path.rglob("*") if item.is_file())


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    else:
        # Include relative names so two bundles with different contents cannot
        # collide merely because their files were concatenated in the same order.
        for item in sorted(iter_files(path), key=lambda value: value.relative_to(path).as_posix()):
            digest.update(item.relative_to(path).as_posix().encode("utf-8"))
            digest.update(b"\0")
            with item.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def artifact_size(path: Path) -> int:
    return sum(item.stat().st_size for item in iter_files(path))


def validate_layout(project: Project, target: str, artifact: Path) -> None:
    """Reject a directory/file that exists but cannot be a launchable package."""
    if target == "macos":
        entry = artifact / "Contents" / "MacOS" / project.executable
        if not entry.is_file():
            raise PackageError(f"macOS bundle is missing its executable: {entry}")
    elif target == "linux" and artifact.is_dir():
        entry = artifact / project.executable
        if not entry.is_file():
            raise PackageError(f"Linux bundle is missing its executable: {entry}")
    elif target == "windows" and artifact.is_file():
        with artifact.open("rb") as handle:
            if handle.read(2) != b"MZ":
                raise PackageError(f"Windows artifact is not a PE executable (missing MZ header): {artifact}")


def expected_artifacts(project: Project, target: str, dist: Path) -> list[Path]:
    if target == "macos":
        return [dist / f"{project.executable}.app"]
    if target == "windows":
        return [dist / f"{project.executable}.exe", dist / project.executable]
    return [dist / project.executable]


def verify_artifact(
    project: Project, target: str, dist: Path, arch: str | None = None,
    manifest_path: Path | None = None, check_existing: bool = False,
) -> Path:
    candidates = [path for path in expected_artifacts(project, target, dist) if path.exists()]
    if not candidates:
        names = ", ".join(path.name for path in expected_artifacts(project, target, dist))
        raise PackageError(f"no expected artifact found in {dist} (looked for {names})")
    artifact = max(candidates, key=artifact_size)
    if artifact_size(artifact) == 0:
        raise PackageError(f"artifact is empty: {artifact}")
    validate_layout(project, target, artifact)
    manifest = {
        "schema": 1,
        "project": project.key,
        "target": target,
        "architecture": arch or host_arch(),
        "artifact": artifact.name,
        "bytes": artifact_size(artifact),
        "sha256": sha256(artifact),
        "files": len(list(iter_files(artifact))),
        "verified_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if manifest_path is None:
        manifest_path = dist / "artifact-manifest.json"
    if check_existing and manifest_path.is_file():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PackageError(f"cannot read existing manifest: {manifest_path} ({exc})") from exc
        for field in ("project", "target", "artifact", "bytes", "sha256", "files"):
            if previous.get(field) != manifest[field]:
                raise PackageError(f"artifact differs from its manifest ({field}): {artifact}")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return artifact


def build(
    project: Project, target: str, arch: str, dist_root: Path, clean: bool, extra: list[str],
    backend: str = "auto", wine_python_path: str | None = None, linux_image: str | None = None,
) -> Path:
    backend = resolve_backend(target, backend)
    destination = output_dir(project, target, arch, dist_root)
    workpath = ROOT / "artifacts" / "work" / project.key / target / arch
    if clean:
        for path in (destination, workpath):
            if path.exists():
                shutil.rmtree(path)
    destination.mkdir(parents=True, exist_ok=True)
    workpath.mkdir(parents=True, exist_ok=True)
    if backend == "docker":
        if target != "linux":
            raise PackageError("Docker backend is only supported for Linux targets")
        engine = container_engine()
        image = linux_image or os.environ.get("LOCALAI_LINUX_IMAGE", "debian:bookworm-slim")
        inner = [
            "/tmp/localai-venv/bin/python", "tooling/package.py", "build", "--project", project.key,
            "--target", "linux", "--arch", arch, "--backend", "native", "--dist", "/out",
        ]
        if not clean:
            inner.append("--no-clean")
        for item in extra:
            inner.extend(["--pyinstaller-arg", item])
        platform_map = {"x86_64": "linux/amd64", "arm64": "linux/arm64", "x86": "linux/386"}
        platform_arg = ["--platform", platform_map[arch]] if arch in platform_map else []
        inner_command = " ".join(shlex.quote(part) for part in inner)
        container_script = (
            "apt-get update && apt-get install -y --no-install-recommends python3 python3-venv python3-tk "
            "ca-certificates && rm -rf /var/lib/apt/lists/* && "
            "python3 -m venv /tmp/localai-venv && "
            "/tmp/localai-venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt && "
            + inner_command
        )
        command = [
            engine, "run", "--rm", *platform_arg, "-v", f"{ROOT}:/src", "-v",
            f"{destination.parent.parent.parent}:/out", "-w", "/src", image, "sh", "-lc",
            container_script,
        ]
        print("$ " + " ".join(shlex.quote(part) for part in command))
        result = subprocess.run(command, cwd=ROOT)
        if result.returncode:
            raise PackageError(f"{engine} Linux build failed with exit code {result.returncode}")
        artifact = verify_artifact(project, target, destination, arch)
        print(f"Built: {artifact} ({artifact_size(artifact):,} bytes)")
        print(f"Manifest: {destination / 'artifact-manifest.json'}")
        return artifact
    if backend == "wine":
        if target != "windows":
            raise PackageError("Wine backend is only supported for Windows targets")
        wine = wine_executable()
        if arch not in {"x86", "x86_64"}:
            raise PackageError("Wine Windows builds require --arch x86_64 or --arch x86")
        python_exe = wine_python_path or wine_python()
        spec = spec_for(project, target)
        command = [
            wine, python_exe, "-m", "PyInstaller", "--clean", "--noconfirm",
            "--distpath", wine_path(destination), "--workpath", wine_path(workpath),
            *extra, wine_path(spec),
        ]
        print("$ " + " ".join(shlex.quote(part) for part in command))
        result = subprocess.run(command, cwd=ROOT, env=os.environ.copy())
        if result.returncode:
            raise PackageError(f"Wine PyInstaller failed with exit code {result.returncode}")
        artifact = verify_artifact(project, target, destination, arch)
        print(f"Built: {artifact} ({artifact_size(artifact):,} bytes)")
        print(f"Manifest: {destination / 'artifact-manifest.json'}")
        return artifact
    if backend != "native":
        raise PackageError(f"unsupported backend {backend!r}")
    check_target(target, arch)
    spec = spec_for(project, target)
    require_tkinter()
    pyinstaller = find_pyinstaller()
    env = os.environ.copy()
    if target == "macos":
        env["PYINSTALLER_TARGET_ARCH"] = "universal2" if arch == "universal2" else arch
    command = pyinstaller + [
        "--clean", "--noconfirm", "--distpath", str(destination), "--workpath", str(workpath),
        *extra, str(spec),
    ]
    print("$ " + " ".join(subprocess.list2cmdline([part]) for part in command))
    result = subprocess.run(command, cwd=ROOT, env=env)
    if result.returncode:
        raise PackageError(f"PyInstaller failed with exit code {result.returncode}")
    artifact = verify_artifact(project, target, destination, arch)
    print(f"Built: {artifact} ({artifact_size(artifact):,} bytes)")
    print(f"Manifest: {destination / 'artifact-manifest.json'}")
    return artifact


def doctor() -> int:
    print(f"Host: {platform.system()} {platform.release()} / {host_arch()}")
    print(f"Python: {sys.executable} ({platform.python_version()})")
    print(f"Wine: {'available' if command_available('wine') else 'not installed'}")
    print(f"Wine Python: {os.environ.get('LOCALAI_WINE_PYTHON') or os.environ.get('WINE_PYTHON') or 'not configured'}")
    print(f"Linux container: {'available' if (command_available('docker') or command_available('podman')) else 'not installed'}")
    print(f"Tkinter: {'available' if tkinter_available() else 'missing'}")
    try:
        command = find_pyinstaller()
        version = subprocess.run(command + ["--version"], cwd=ROOT, capture_output=True, text=True, check=False)
        print(f"PyInstaller: {version.stdout.strip() or version.stderr.strip()}")
    except PackageError as exc:
        print(f"PyInstaller: ERROR - {exc}")
        return 1
    for key, project in PROJECTS.items():
        states = []
        for target in ("macos", "windows", "linux"):
            states.append(f"{target}={'ok' if spec_for(project, target).is_file() else 'missing'}")
        print(f"{key}: " + ", ".join(states))
    if shutil.which("codesign"):
        print("codesign: available (signing is intentionally opt-in)")
    if host_target() == "macos" and shutil.which("hdiutil"):
        print("hdiutil: available (DMG creation is not implicit)")
    return 0


def list_projects() -> int:
    for project in PROJECTS.values():
        print(f"{project.key:10} {project.label:12} targets: macos, windows, linux")
    return 0


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project", "-p", default="localai", help="localai, localai-se, or cloudai")
    common.add_argument("--target", "-t", default="current", help="macos, windows, linux, or current")
    common.add_argument("--arch", default="native", help="native, arm64, x86_64, or macOS universal2")
    common.add_argument("--backend", default="auto", choices=("auto", "native", "wine", "docker"), help="build backend")
    common.add_argument("--wine-python", default=None, help="Windows python.exe path for Wine builds")
    common.add_argument("--linux-image", default=None, help="Docker/Podman image for Linux builds")
    common.add_argument("--dist", type=Path, default=ROOT / "artifacts" / "packages", help="package output root")
    command = argparse.ArgumentParser(prog="localai-pack", description=__doc__)
    sub = command.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list available projects")
    sub.add_parser("doctor", help="check host and packaging prerequisites")
    build_parser = sub.add_parser("build", parents=[common], help="build and verify one desktop package")
    build_parser.add_argument("--no-clean", action="store_true", help="keep the previous output directory")
    build_parser.add_argument("--pyinstaller-arg", action="append", default=[], help="extra argument passed to PyInstaller")
    verify_parser = sub.add_parser("verify", parents=[common], help="verify an existing package")
    verify_parser.add_argument("path", type=Path, help="artifact or its containing dist directory")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "list":
        return list_projects()
    if args.command == "doctor":
        return doctor()
    project = project_for(args.project)
    target = host_target() if args.target == "current" else args.target.lower()
    arch = canonical_arch(args.arch)
    try:
        if args.command == "build":
            build(
                project, target, arch, args.dist.resolve(), not args.no_clean, args.pyinstaller_arg,
                args.backend, args.wine_python, args.linux_image,
            )
            return 0
        if args.command == "verify":
            if target not in {"macos", "windows", "linux"}:
                raise PackageError(f"unsupported target {target!r}; use macos, windows, or linux")
            path = args.path.resolve()
            expected_names = {candidate.name for candidate in expected_artifacts(project, target, path.parent)}
            dist = path.parent if path.name in expected_names else path
            artifact = verify_artifact(project, target, dist, arch, check_existing=True)
            print(f"Verified: {artifact}")
            return 0
    except PackageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
