# Host × Guest Architecture Selection Matrix

This document describes the host×guest architecture selection matrix contract
tested by `VirtualizationArchitectureMatrixTests`.

## Overview

The virtualization backend selection system must satisfy invariants across every
host platform × guest architecture combination. The matrix tests verify these
invariants using only `FakeEngine` stubs — no real VMs are started.

## Matrix Dimensions

### Host Platforms

| Platform  | Architecture | Native Backend | Native is Honest |
|-----------|-------------|----------------|-----------------|
| windows   | x86_64      | WHPX           | No (QEMU process) |
| windows   | aarch64     | WHPX           | No (QEMU process) |
| macos     | x86_64      | Apple HV       | Yes              |
| macos     | aarch64     | Apple HV       | Yes              |
| linux     | x86_64      | KVM            | No (HardwareAccelerated) |
| linux     | aarch64     | KVM            | No (HardwareAccelerated) |
| linux     | riscv64     | KVM            | No (HardwareAccelerated) |
| harmonyos | aarch64     | Harmony Virt   | No (HardwareAccelerated) |

### Guest Architectures

x86_64, aarch64, riscv64, i386, arm

## Selection Rules

### Priority Order

1. **Native** — same-ISA hardware virtualization (Apple HV, WHPX)
2. **HardwareAccelerated** — same-ISA with hypervisor assist (KVM, Harmony)
3. **SoftwareEmulation** — cross-ISA via QEMU TCG (always available)

### Cross-ISA Handling

Native and hardware-accelerated backends **reject** foreign ISA guests.
QEMU TCG accepts all guest architectures and serves as the universal fallback.

### Tier Honesty

- **Windows**: names its backend "Native" (WHPX is a hypervisor API), but
  `executionTierFor()` reports the honest tier for each guest arch.
- **Linux/HarmonyOS**: never claim Native tier; they report
  HardwareAccelerated.
- **RISC-V64**: support matrix rows are never `verified` until exercised on
  real hardware.

## Test Suite (19 tests)

| # | Test | What it verifies |
|---|------|-----------------|
| 1 | `testHostGuestMatrixSelectionOrder` | 8 hosts × 5 guests: selection tier matches ISA rules |
| 2 | `testCrossIsaRejection` | Native/hardware reject foreign ISA; TCG accepts all |
| 3 | `testWindowsTierHonesty` | WHPX `executionTierFor()` is honest; cross-ISA falls to TCG |
| 4 | `testLinuxHarmonyNeverNative` | KVM and Harmony never report Native tier |
| 5 | `testRiscv64UnverifiedMaturity` | RISC-V64 matrix rows are never verified |
| 6 | `testSchemaV3BackendFields` | Every backend JSON has all required v3 fields |
| 7 | `testSchemaV3SelectionFields` | Selection JSON has ok/backend/tier/considered etc. |
| 8 | `testSchemaV3SupportMatrix` | Support matrix JSON rows have all required fields |
| 9 | `testRejectionReasonsNonEmpty` | Every rejected backend carries a non-empty reason |
| 10 | `testDuplicateIdReplacesNotDuplicates` | Re-registering same id replaces, does not duplicate |
| 11 | `testMissingTcgSelectionFailsGracefully` | Without TCG, selection fails with explanation |
| 12 | `testUnknownGuestArch` | Unknown arch (mipsel) falls back to TCG |
| 13 | `testEmptyProbe` | Unavailable engine probe reports hostUnsupported |
| 14 | `testUnavailableFactoryIsNull` | Null engine registration is a no-op |
| 15 | `testExplicitBackendPolicy` | QemuOnly never selects native; explicit id works |
| 16 | `testDeterminalityShuffledRegistration` | 1000 scenarios: result depends on priority, not insertion order |
| 17 | `testTcgAlwaysLast` | TCG is always the last backend in preference order |
| 18 | `testDefaultSupportMatrixInvariants` | Cross-ISA rows are emulation; Linux/Harmony never native |
| 19 | `testQemuOnlyNeverNative` | QemuOnly policy never selects a native-tier backend |

## Schema V3 Fields

### Backend Object

`id`, `name`, `tier`, `execution_tier`, `host_supported`,
`guest_arch_supported`, `hardware_acceleration`, `native_execution`,
`accelerator`, `summary`, `status`, `status_class`, `runs_in_process`,
`requirements`, `issues`

### Selection Object

`ok`, `backend`, `name`, `tier`, `execution_tier`, `reason`,
`fell_back`, `fallback_from`, `considered`

### Considered Entry

`backend_id`, `tier`, `host_supported`, `guest_arch_supported`,
`config_accepted`, `selected`, `status_code`, `reason`

### Support Matrix Row

`host_platform`, `host_arch`, `guest_arch`, `backend`, `tier`,
`status`, `verified`, `note`

## Canonical Status Codes

`available`, `host-unsupported`, `guest-arch-unsupported`,
`accelerator-unavailable`, `runtime-missing`, `config-invalid`,
`backend-mismatch`, `excluded-by-policy`, `no-backend`, `not-registered`

## Running

```bash
cmake -B build-architecture-matrix -DCMAKE_BUILD_TYPE=Debug
cmake --build build-architecture-matrix --target VirtualWorldVirtualizationArchitectureMatrixTests
./build-architecture-matrix/VirtualWorldVirtualizationArchitectureMatrixTests
```

## Scope

This suite tests the **public contract** of the selection and matrix system.
It does not test platform-specific runtime behavior (KVM open, WHPX session,
Apple HV VM creation). Those are covered by the per-platform test suites
(`MacVirtualizationEngineTests`, `LinuxKvmEngineTests`,
`WindowsHypervisorEngineTests`, `HarmonyVirtualizationEngineTests`).