#!/usr/bin/env python3
"""Package a verified, target-native CloudAI PyInstaller directory."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parents[2]
ARCHES = {"arm64": (183, "aarch64"), "riscv64": (243, "riscv64")}


def elf_machine(path: Path) -> int | None:
    with path.open("rb") as handle:
        header = handle.read(20)
    if not header.startswith(b"\x7fELF"):
        return None
    if len(header) != 20 or header[4] != 2 or header[5] not in (1, 2):
        raise ValueError(f"Expected a valid 64-bit ELF: {path}")
    return struct.unpack("<H" if header[5] == 1 else ">H", header[18:20])[0]


def verify_bundle(bundle: Path, arch: str) -> int:
    expected = ARCHES[arch][0]
    if elf_machine(bundle / "CloudAI") != expected:
        raise ValueError(f"CloudAI executable does not match {arch}")
    count = 0
    for path in bundle.rglob("*"):
        if not path.is_file():
            continue
        machine = elf_machine(path)
        if machine is not None:
            if machine != expected:
                raise ValueError(f"Wrong ELF architecture ({machine}, expected {expected}): {path}")
            count += 1
    for pattern in ("_internal/libpython*.so*", "_internal/**/_tkinter*.so", "_internal/PIL/_imaging*.so"):
        if not list(bundle.glob(pattern)):
            raise ValueError(f"Missing native runtime: {pattern}")
    return count


def verify_smoke(path: Path, arch: str) -> dict:
    result = json.loads(path.read_text(encoding="utf-8"))
    if result.get("status") != "ok" or result.get("arch") != arch or result.get("frozen") is not True:
        raise ValueError(f"Bundled GUI smoke test did not pass for {arch}: {result}")
    return result


def elf_hashes(bundle: Path, arch: str) -> dict[str, str]:
    """Identify every native file, including the actual application executable."""
    verify_bundle(bundle, arch)
    hashes = {}
    for path in sorted(bundle.rglob("*")):
        if path.is_file() and elf_machine(path) is not None:
            with path.open("rb") as handle:
                hashes[path.relative_to(bundle).as_posix()] = hashlib.file_digest(handle, "sha256").hexdigest()
    return hashes


def verify_packaged_bundle(bundle: Path, arch: str, expected_hashes: dict[str, str]) -> dict:
    actual_hashes = elf_hashes(bundle, arch)
    if actual_hashes != expected_hashes:
        raise ValueError(f"Packaged ELF files differ from the verified {arch} build: {bundle}")
    subprocess.run(["xvfb-run", "-a", "timeout", "120", str(bundle / "CloudAI"), "--smoke-test"], check=True)
    return {"elf_class": 64, "elf_machine": ARCHES[arch][0],
            "elf_arch": ARCHES[arch][1], "elf_files_verified": len(actual_hashes),
            "executable_sha256": actual_hashes["CloudAI"]}


def stage_root(bundle: Path, root: Path) -> None:
    shutil.copytree(bundle, root / "opt/cloudai", symlinks=True)
    launcher = root / "usr/bin/cloudai"
    launcher.parent.mkdir(parents=True)
    launcher.write_text('#!/bin/sh\nexec /opt/cloudai/CloudAI "$@"\n', encoding="utf-8")
    launcher.chmod(0o755)
    desktop = root / "usr/share/applications/cloudai.desktop"
    desktop.parent.mkdir(parents=True)
    desktop.write_text(
        "[Desktop Entry]\nType=Application\nName=CloudAI\nExec=cloudai\nIcon=cloudai\n"
        "Terminal=false\nCategories=Utility;Development;\n", encoding="utf-8")
    icon = root / "usr/share/icons/hicolor/256x256/apps/cloudai.png"
    icon.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "shared/assets/icons/localai_dark_256.png", icon)
    subprocess.run(["desktop-file-validate", str(desktop)], check=True)


def package(bundle: Path, arch: str, output: Path) -> None:
    elf_count = verify_bundle(bundle, arch)
    native_hashes = elf_hashes(bundle, arch)
    smoke = verify_smoke(output / "smoke-test.json", arch)
    version_data = json.loads((ROOT / "shared/resources/version.json").read_text(encoding="utf-8"))
    version = version_data["components"]["CloudAI"]
    rpm_arch = ARCHES[arch][1]
    work = ROOT / "build/cloudai-packaging" / arch
    if work.exists():
        shutil.rmtree(work)
    root = work / "root"
    stage_root(bundle, root)
    prefix = f"cloudai-{version}-linux-{arch}"

    control = root / "DEBIAN/control"
    control.parent.mkdir()
    control.write_text(
        f"Package: cloudai\nVersion: {version}\nArchitecture: {arch}\n"
        "Section: utils\nPriority: optional\nMaintainer: LocalAI <localai@example.invalid>\n"
        "Depends: libc6 (>= 2.39), libx11-6, libxext6, libxrender1, libxft2, libfontconfig1, fonts-dejavu-core\n"
        "Description: CloudAI desktop client\n OpenAI-compatible cloud AI desktop client.\n", encoding="utf-8")
    subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(root), str(output / f"{prefix}.deb")], check=True)
    # Packaging metadata must not become application payload in the RPM.
    shutil.rmtree(control.parent)
    rpm_top = work / "rpmbuild"
    for directory in ("BUILD", "BUILDROOT", "RPMS", "SOURCES", "SPECS", "SRPMS"):
        (rpm_top / directory).mkdir(parents=True)
    spec = rpm_top / "SPECS/cloudai.spec"
    spec.write_text(f"""Name: cloudai
Version: {version}
Release: 1
Summary: CloudAI desktop client
License: Proprietary
BuildArch: {rpm_arch}
AutoReqProv: no
Requires: glibc >= 2.39, libX11, libXext, libXrender, libXft, fontconfig

%description
OpenAI-compatible cloud AI desktop client with bundled Python, Tk and Pillow.

%install
mkdir -p %{{buildroot}}
cp -a {root}/opt {root}/usr %{{buildroot}}/

%files
/opt/cloudai
/usr/bin/cloudai
/usr/share/applications/cloudai.desktop
/usr/share/icons/hicolor/256x256/apps/cloudai.png
""", encoding="utf-8")
    subprocess.run(["rpmbuild", "-bb", str(spec), "--define", f"_topdir {rpm_top}",
                    "--define", "_build_id_links none", "--define", "__os_install_post %{nil}"], check=True)
    rpms = list((rpm_top / "RPMS" / rpm_arch).glob("cloudai-*.rpm"))
    if len(rpms) != 1:
        raise ValueError(f"Expected one {rpm_arch} RPM, found {rpms}")
    shutil.copy2(rpms[0], output / f"{prefix}.rpm")

    portable = work / "CloudAI"
    shutil.copytree(bundle, portable, symlinks=True)
    (portable / "README.txt").write_text(
        f"CloudAI {version} - Linux {arch}\n\n"
        "Run ./CloudAI from this directory. No separate Python installation is needed.\n"
        "Requires a Linux desktop with X11/XWayland, glibc >= 2.39 and system fonts.\n"
        "Built on Ubuntu 24.04. Keep the _internal directory beside the executable.\n"
        "Settings are saved in ~/.local/share/CloudAI (or $XDG_DATA_HOME/CloudAI).\n"
        "Offline runtime check: ./CloudAI --smoke-test\n", encoding="utf-8")
    with tarfile.open(output / f"{prefix}.tar.gz", "w:gz") as archive:
        archive.add(portable, arcname="CloudAI")

    # Verify the Debian metadata and its actual packaged executable, not only
    # the input tree. This catches architecture labels attached to wrong bytes.
    deb = output / f"{prefix}.deb"
    deb_arch = subprocess.check_output(["dpkg-deb", "-f", str(deb), "Architecture"], text=True).strip()
    rpm_arch_actual = subprocess.check_output(["rpm", "-qp", "--qf", "%{ARCH}", str(output / f"{prefix}.rpm")], text=True).strip()
    if deb_arch != arch or rpm_arch_actual != rpm_arch:
        raise ValueError("Package metadata architecture does not match target")
    unpacked = work / "unpacked-deb"
    subprocess.run(["dpkg-deb", "-x", str(deb), str(unpacked)], check=True)
    package_checks = {
        deb.name: {"package_arch": deb_arch,
                   **verify_packaged_bundle(unpacked / "opt/cloudai", arch, native_hashes)}
    }
    rpm = output / f"{prefix}.rpm"
    cpio = work / "rpm-payload.cpio"
    with cpio.open("wb") as handle:
        subprocess.run(["rpm2cpio", str(rpm)], stdout=handle, check=True)
    rpm_root = work / "unpacked-rpm"
    rpm_root.mkdir()
    with cpio.open("rb") as handle:
        subprocess.run(["cpio", "-idm", "--quiet", "--no-absolute-filenames"],
                       stdin=handle, cwd=rpm_root, check=True)
    package_checks[rpm.name] = {
        "package_arch": rpm_arch_actual,
        **verify_packaged_bundle(rpm_root / "opt/cloudai", arch, native_hashes)
    }
    tar = output / f"{prefix}.tar.gz"
    tar_root = work / "unpacked-tar"
    with tarfile.open(tar, "r:gz") as archive:
        archive.extractall(tar_root, filter="data")
    package_checks[tar.name] = verify_packaged_bundle(tar_root / "CloudAI", arch, native_hashes)
    manifest = {"app": "CloudAI", "version": version, "arch": arch,
                "source_revision": os.environ.get("SOURCE_REVISION", "unknown"),
                "build_os": "Ubuntu 24.04", "minimum_glibc": "2.39",
                "elf_files_verified": elf_count, "smoke_test": smoke,
                "packages": package_checks}
    (output / "build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    architecture_lines = [f"CloudAI {version} Linux {arch} / {rpm_arch}",
                          f"Every packaged executable and native library is ELF64, e_machine={ARCHES[arch][0]}.",
                          "Each DEB, RPM and tar.gz was extracted and its native files compared by SHA-256."]
    for name, result in package_checks.items():
        architecture_lines.append(f"{name}: {result['elf_arch']}, ELF64, "
                                  f"{result['elf_files_verified']} native files, executable SHA256={result['executable_sha256']}")
    (output / "ARCHITECTURE.txt").write_text("\n".join(architecture_lines) + "\n", encoding="utf-8")
    sums = []
    for path in sorted(output.iterdir()):
        if path.is_file() and path.name != "SHA256SUMS":
            with path.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
            sums.append(f"{digest}  {path.name}\n")
    (output / "SHA256SUMS").write_text("".join(sums), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", required=True, choices=ARCHES)
    parser.add_argument("--bundle", type=Path, default=ROOT / "dist/CloudAI")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    package(args.bundle.resolve(), args.arch, args.output.resolve())
