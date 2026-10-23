#!/usr/bin/env python3
"""Deterministically regenerate the synthetic conformance fixtures.

The fixtures under ``fixtures/`` are of two kinds, and the distinction is the
whole point:

* ``macos-apple-silicon-aarch64.json`` and
  ``macos-apple-silicon-x86_64-guest.json`` are **real captures** taken from a
  built VirtualWorld binary running on a real Apple Silicon Mac. They are marked
  ``synthetic: false`` in the manifest.

* Everything else in ``fixtures/`` is **synthetic**: hand-authored here, shaped
  against the shared contract and against the project's own documented support
  matrix (``support_matrix`` rows emitted by the real binary). They let CI on any
  host cover Windows x86_64/ARM64, Linux x86_64/ARM64/RISC-V64, macOS Intel and
  HarmonyOS/OpenHarmony. They are marked ``synthetic: true``, and the checker
  fails any synthetic fixture whose support matrix claims ``verified: true`` --
  only real hardware runs may ever set that flag.

Run this script from anywhere:

    python3 tools/virtualization-conformance/make_synthetic_fixtures.py

It is intentionally a plain, dependency-free generator so a reviewer can diff the
inputs and see exactly which expectations are asserted, rather than trusting an
opaque blob.
"""

from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE_DIR = os.path.join(HERE, "fixtures")

SCHEMA = "virtualworld.virtualization/3"

# Registration order of the current build (matches a real capture).
BACKEND_ORDER = [
    "macos-virtualization",
    "qemu-hardware",
    "harmony-virtualization",
    "linux-kvm",
    "qemu-tcg",
    "windows-hypervisor",
]

BACKEND_NAMES = {
    "macos-virtualization": "Apple Virtualization.framework",
    "windows-hypervisor": "Windows Hypervisor Platform (WHPX)",
    "linux-kvm": "Linux KVM/QEMU 兼容后端（KVM/TCG）",
    "harmony-virtualization": "HarmonyOS/OpenHarmony QEMU 兼容后端（KVM/TCG）",
    "qemu-hardware": "QEMU 硬件加速（HFV/WHPX/KVM）",
    "qemu-tcg": "QEMU TCG 软件模拟",
}

TECHNICAL_TIER = {
    "macos-virtualization": "native",
    "windows-hypervisor": "native",
    "linux-kvm": "hardware",       # QEMU + KVM: hardware assisted, never "native"
    "harmony-virtualization": "hardware",  # QEMU + KVM compat, never "native"
    "qemu-hardware": "hardware",
    "qemu-tcg": "emulation",
}

TIER_RANK = {"native": 0, "hardware": 1, "emulation": 2}

# Which backend owns which host platform. Everything else is host-unsupported
# there, which is exactly what the shared contract demands.
PLATFORM_NATIVE_BACKEND = {
    "macos": "macos-virtualization",
    "windows": "windows-hypervisor",
    "linux": "linux-kvm",
    "harmonyos": "harmony-virtualization",
}

HOST_NAMES = {
    "macos": "macOS",
    "windows": "Windows",
    "linux": "Linux",
    "harmonyos": "HarmonyOS (Linux-compatible mode)",
}

HOST_FAMILIES = {
    ("macos", "aarch64"): "macos-apple-silicon",
    ("macos", "x86_64"): "macos-intel",
    ("windows", "aarch64"): "windows",
    ("windows", "x86_64"): "windows",
    ("linux", "aarch64"): "linux",
    ("linux", "x86_64"): "linux",
    ("linux", "riscv64"): "linux",
    ("harmonyos", "aarch64"): "harmonyos-linux-compatible",
}

BUILD_FLAGS = {
    "macos_backend": True,
    "windows_backend": True,
    "linux_kvm_backend": True,
    "harmony_backend": True,
    "qemu_fallback": True,
}

# Documented support-matrix shape, transcribed from a real --list-backends run.
# Only the rows that differ per platform are listed; cross-architecture guests
# always fall to qemu-tcg.
MATRIX_GUEST_ARCHES = ("aarch64", "x86_64", "riscv64")


def matrix_rows(platform: str, host_arch: str) -> list:
    rows = []
    compat_backend = PLATFORM_NATIVE_BACKEND[platform]
    for guest_arch in MATRIX_GUEST_ARCHES:
        if guest_arch == host_arch:
            if platform == "macos":
                rows.append(_matrix_row(platform, host_arch, guest_arch, compat_backend,
                                        "native", "native",
                                        "同架构：宿主原生虚拟化接口（1:1），未在本机验证。"))
            elif platform == "windows":
                rows.append(_matrix_row(platform, host_arch, guest_arch, compat_backend,
                                        "native", "native",
                                        "同架构：WHPX 原生路径，未在本机验证。"))
            else:
                rows.append(_matrix_row(platform, host_arch, guest_arch, compat_backend,
                                        "hardware", "compat",
                                        "同架构：QEMU + KVM 兼容路径（非原生），"
                                        "取决于 /dev/kvm 是否暴露；不可用则回退 TCG。"))
            if platform == "macos":
                rows.append(_matrix_row(platform, host_arch, guest_arch, "qemu-hardware",
                                        "hardware", "hardware",
                                        "同架构硬件加速回退。"))
        rows.append(_matrix_row(platform, host_arch, guest_arch, "qemu-tcg",
                                "emulation", "emulation",
                                "最终兜底：QEMU TCG 始终可用。"
                                if guest_arch == host_arch
                                else "跨架构：只能软件模拟，不存在跨架构硬件加速。"))
    return rows


def _matrix_row(platform, host_arch, guest_arch, backend, tier, status, note) -> dict:
    return {
        "host_platform": platform,
        "host_arch": host_arch,
        "guest_arch": guest_arch,
        "backend": backend,
        "tier": tier,
        "status": status,
        "verified": False,  # synthetic data never claims real-hardware verification
        "note": note,
    }


def backend_entry(backend_id, *, host_supported, guest_arch_supported, config_accepted,
                  tier=None, accelerator="tcg", status=None, summary=None, issues=None):
    tier = tier or TECHNICAL_TIER[backend_id]
    if issues is None:
        if host_supported:
            issues = [{
                "level": "info",
                "code": "host-supported",
                "message": f"{backend_id} 在本宿主上可用。",
            }]
        else:
            issues = [{
                "level": "error",
                "code": "host-unsupported",
                "message": f"宿主不是 {backend_id} 支持的平台。",
            }]
    if summary is None:
        summary = (f"{BACKEND_NAMES[backend_id]} 可用。"
                   if host_supported
                   else f"{BACKEND_NAMES[backend_id]} 在本宿主上不可用。")
    if not host_supported:
        accelerator = ""
        guest_arch_supported = False
    _ = config_accepted
    return {
        "id": backend_id,
        "name": BACKEND_NAMES[backend_id],
        "tier": tier,
        "host_supported": bool(host_supported),
        "guest_arch_supported": bool(guest_arch_supported),
        "hardware_acceleration": bool(host_supported and tier in ("native", "hardware")
                                      and guest_arch_supported),
        "native_execution": tier == "native" and host_supported and backend_id in (
            "macos-virtualization", "windows-hypervisor"),
        "accelerator": accelerator,
        "status": status or ("available" if host_supported else "host-unsupported"),
        "status_class": (status or ("available" if host_supported else "host-unsupported")),
        "summary": summary,
        "issues": issues,
        "requirements": [f"{BACKEND_NAMES[backend_id]} 需要其宿主平台。"],
        "runs_in_process": backend_id == "macos-virtualization",
    }


def build_document(platform: str, host_arch: str, guest_arch: str, *, preference: str = "auto") -> dict:
    """Build a schema-valid document describing one host/guest scenario."""
    same_arch = host_arch == guest_arch
    owning_backend = PLATFORM_NATIVE_BACKEND[platform]
    owning_tier = TECHNICAL_TIER[owning_backend]

    backends = []
    for backend_id in BACKEND_ORDER:
        is_owner = backend_id == owning_backend
        host_supported = is_owner or backend_id in ("qemu-hardware", "qemu-tcg")
        if backend_id == "qemu-hardware":
            # Same-ISA only for the host architecture.
            guest_arch_supported = same_arch
            accelerator = "kvm" if platform == "linux" else (
                "hvf" if platform == "macos" else ("whpx" if platform == "windows" else "kvm"))
        elif backend_id == "qemu-tcg":
            guest_arch_supported = True
            accelerator = "tcg"
        else:
            guest_arch_supported = is_owner and host_supported
            accelerator = ("vz" if backend_id == "macos-virtualization"
                           else "whpx" if backend_id == "windows-hypervisor" else "kvm")
        tier = owning_tier if is_owner else TECHNICAL_TIER[backend_id]
        if backend_id == "qemu-tcg":
            tier = "emulation"
        if backend_id == owning_backend:
            tier = owning_tier
        backends.append(
            backend_entry(
                backend_id,
                host_supported=host_supported,
                guest_arch_supported=guest_arch_supported,
                config_accepted=host_supported and guest_arch_supported,
                tier=tier,
                accelerator=accelerator,
                status=("available" if (host_supported and guest_arch_supported)
                        else ("host-unsupported" if not host_supported else "guest-arch-unsupported")),
            )
        )

    # Eligible candidates, best first, exactly as the registry orders them.
    eligible = []
    for entry in backends:
        if entry["host_supported"] and entry["guest_arch_supported"]:
            eligible.append(entry)
    eligible.sort(key=lambda e: (TIER_RANK[e["tier"]], e["id"]))

    selected = eligible[0] if eligible else None
    selected_id = selected["id"] if selected else ""

    considered = []
    for backend_id in BACKEND_ORDER:
        entry = next(b for b in backends if b["id"] == backend_id)
        is_selected = backend_id == selected_id
        if is_selected:
            reason = f"可用：{BACKEND_NAMES[backend_id]}"
        elif not entry["host_supported"]:
            reason = f"宿主不是 {backend_id} 支持的平台，回退 QEMU 后端。"
        elif not entry["guest_arch_supported"]:
            reason = f"{backend_id} 不支持客户机架构 {guest_arch}，回退 QEMU TCG。"
        else:
            reason = f"{backend_id} 可用但优先级低于 {selected_id}。"
        considered.append({
            "id": backend_id,
            "tier": entry["tier"],
            "host_supported": entry["host_supported"],
            "guest_arch_supported": entry["guest_arch_supported"],
            "config_accepted": bool(entry["host_supported"] and entry["guest_arch_supported"]),
            "selected": is_selected,
            "status": "selected" if is_selected else (
                "host-unsupported" if not entry["host_supported"]
                else "guest-arch-unsupported" if not entry["guest_arch_supported"]
                else "lower-priority"),
            "reason": reason,
        })

    considered.sort(key=lambda c: (TIER_RANK[c["tier"]], c["id"]))

    fallback_from = ""
    fell_back = False
    for candidate in reversed(eligible):
        if candidate["id"] != selected_id:
            fallback_from = candidate["id"]
            fell_back = True
            break

    selection = {
        "ok": selected is not None,
        "backend": selected_id,
        "name": BACKEND_NAMES.get(selected_id, ""),
        "tier": selected["tier"] if selected else "emulation",
        "reason": (f"可用：{BACKEND_NAMES[selected_id]}" if selected
                   else "没有任何后端可用。"),
        "fell_back": fell_back,
        "fallback_from": fallback_from,
        "considered": considered,
    }

    return {
        "schema": SCHEMA,
        "build": dict(BUILD_FLAGS),
        "host": {
            "arch": host_arch,
            "family": HOST_FAMILIES.get((platform, host_arch), platform),
            "name": HOST_NAMES[platform],
            "platform": platform,
        },
        "guest_arch": guest_arch,
        "preference": preference,
        "backends": backends,
        "selection": selection,
        "support_matrix": matrix_rows(platform, host_arch),
    }


SCENARIOS = [
    # (file name, platform, host arch, guest arch, label, expected selected backend)
    ("windows-x86_64.json", "windows", "x86_64", "x86_64",
     "Windows x86_64 host, x86_64 guest (WHPX native)", "windows-hypervisor"),
    ("windows-aarch64.json", "windows", "aarch64", "aarch64",
     "Windows ARM64 host, aarch64 guest (WHPX native)", "windows-hypervisor"),
    ("linux-x86_64.json", "linux", "x86_64", "x86_64",
     "Linux x86_64 host, x86_64 guest (KVM compat)", "linux-kvm"),
    ("linux-aarch64.json", "linux", "aarch64", "aarch64",
     "Linux ARM64 host, aarch64 guest (KVM compat)", "linux-kvm"),
    ("linux-riscv64.json", "linux", "riscv64", "riscv64",
     "Linux RISC-V64 host, riscv64 guest (KVM compat)", "linux-kvm"),
    ("harmonyos-aarch64.json", "harmonyos", "aarch64", "aarch64",
     "HarmonyOS ARM64 host, aarch64 guest (QEMU+KVM compat, never native)",
     "harmony-virtualization"),
    ("macos-intel-x86_64.json", "macos", "x86_64", "x86_64",
     "Intel Mac, x86_64 guest (Virtualization.framework native)", "macos-virtualization"),
]


def main() -> int:
    os.makedirs(FIXTURE_DIR, exist_ok=True)
    written = []
    for file_name, platform, host_arch, guest_arch, _label, _selected in SCENARIOS:
        document = build_document(platform, host_arch, guest_arch)
        path = os.path.join(FIXTURE_DIR, file_name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        written.append(file_name)
    print(f"wrote {len(written)} synthetic fixtures to {FIXTURE_DIR}")
    for name in written:
        print(f"  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
