# VirtualWorld

[English](README.md) | [简体中文](README.zh-CN.md)

**Author / Maintainer:** 廖博轩 (Liao Boxuan)

**License:** [GNU General Public License v2.0 only](LICENSE)

VirtualWorld is the C++ core migration of the former QEMU Bridge project.
It manages QEMU as a backend runtime and does not reimplement QEMU.

> **Project status:** active development. The architecture matrix describes
> implemented selection and fallback behavior; a row marked `Verified = No`
> is not a claim of successful testing on that physical host.

Initialize the QEMU source submodule before requesting a bundled QEMU build:

```bash
git submodule update --init projects/VirtualWorld/qemu-source
```

Current phase: C++ core plus a lightweight Qt Widgets manager UI.
QEMU is managed as an internal runtime. The project can ship prebuilt QEMU
files under `runtime/qemu`, or build the bundled `qemu-source` source into that
runtime directory.

The GUI starts virtual machines in a VirtualWorld-owned window. QEMU runs as a
background backend process with its native window disabled; VirtualWorld captures
and displays the guest screen itself. This first display backend is intended for
boot/installer visibility and stability checks. Full keyboard/mouse integration
should later move to a SPICE or VNC display backend.

## Architecture

```text
VirtualWorld C++ Core
  |-- VMManager
  |     |-- VirtualizationEngineRegistry   (auto-selection)
  |           |-- macos-virtualization     (Apple Virtualization.framework)
  |           |-- windows-hypervisor       (WHPX)
  |           |-- linux-kvm                (KVM)
  |           |-- harmony-virtualization   (Linux-compatible fallback)
  |           |-- qemu-hardware            (QEMU + hvf/whpx/kvm)
  |           `-- qemu-tcg                 (QEMU TCG, always available)
  |-- QemuEngine
  |-- ImageManager
  |-- ConfigManager
  |-- RuntimeManager
  |
  v
QEMU Runtime
```

## Virtualization Backends

VirtualWorld selects a backend automatically. The order is fixed and never
reordered:

1. **Native virtualization, same architecture** — the host hypervisor API
   (Virtualization.framework on macOS, WHPX on Windows).
2. **Real hardware acceleration** — `linux-kvm` / `harmony-virtualization` when
   the platform genuinely exposes KVM, and QEMU with `hvf` / `whpx` / `kvm`.
3. **QEMU TCG software emulation** — always present, any guest architecture.

`qemu-tcg` is pinned to the very last position explicitly, not merely by tier
sorting: several platform backends legitimately degrade into the emulation tier,
and ordering them by id could otherwise place one of them *after* TCG. The
registry therefore moves `qemu-tcg` to the end of the candidate list after
sorting, and a public test asserts `ordered.last() == "qemu-tcg"`.

A backend is only chosen when it passes capability probing *and* validates the
VM configuration without errors. Anything else falls through to the next tier,
and the CLI reports why.

### Host / guest support matrix

"Native" means the host hypervisor runs the guest 1:1. "Hardware" means QEMU
driving the host accelerator. "Emulation" means QEMU TCG. **There is no
cross-architecture hardware acceleration** — a foreign ISA is always TCG.

| Host platform | Host arch | Guest arch | Backend | Tier | Status | Verified |
| --- | --- | --- | --- | --- | --- | --- |
| Windows | x86_64 | x86_64 | `windows-hypervisor` | native | WHPX | No |
| Windows | x86_64 | aarch64 | `qemu-tcg` | emulation | TCG | No |
| Windows | aarch64 | aarch64 | `windows-hypervisor` | native | WHPX | No |
| Windows | aarch64 | x86_64 | `qemu-tcg` | emulation | TCG | No |
| macOS | x86_64 | x86_64 | `macos-virtualization` | native | VZ.framework | No |
| macOS | x86_64 | aarch64 | `qemu-tcg` | emulation | TCG | No |
| macOS | aarch64 | aarch64 | `macos-virtualization` | native | VZ.framework | No |
| macOS | aarch64 | x86_64 | `qemu-tcg` | emulation | TCG | No |
| Linux | x86_64 | x86_64 | `linux-kvm` | hardware | QEMU + KVM (not native) | No |
| Linux | x86_64 | aarch64 | `qemu-tcg` | emulation | TCG | No |
| Linux | aarch64 | aarch64 | `linux-kvm` | hardware | QEMU + KVM (not native) | No |
| Linux | aarch64 | x86_64 | `qemu-tcg` | emulation | TCG | No |
| Linux | riscv64 | riscv64 | `linux-kvm` | hardware | QEMU + KVM (not native) | No |
| Linux | riscv64 | x86_64 | `qemu-tcg` | emulation | TCG | No |
| HarmonyOS / OpenHarmony | x86_64 / aarch64 / riscv64 | same | `harmony-virtualization` | hardware | QEMU + KVM (not native) | No |
| HarmonyOS / OpenHarmony | any | different | `qemu-tcg` | emulation | TCG | No |

**Why Linux and HarmonyOS are `hardware`, not `native`:** neither backend
drives a host hypervisor API directly. `linux-kvm` is a QEMU compatibility
backend (`-machine …,accel=kvm`) and `harmony-virtualization` runs QEMU on a
Linux-compatible layer. A QEMU process that happens to be accelerated by KVM is
hardware acceleration, not native execution, so both report
`VirtualizationTier::HardwareAccelerated` (or `SoftwareEmulation` when `/dev/kvm`
is not exposed) and never `Native`. Only `macos-virtualization` (which drives
Virtualization.framework itself) and `windows-hypervisor` are native-tier
candidates.

**What "supported" means here:** the backend is compiled in, the host exposes
the required interface, and the guest architecture matches what that backend can
actually run. It does **not** mean every guest OS boots; a guest still needs
firmware, media and drivers that work. `Verified = No` means the combination has
not been exercised on real hardware — only this repository's build and unit
tests ran (on macOS/Apple Silicon).

`linux-kvm` and `harmony-virtualization` are wired into CMake but their sources
are owned by other workstreams. When they are missing — or built with
`-DVIRTUALWORLD_INCLUDE_CONTRIB_BACKENDS=OFF` — those rows fall back to
`qemu-hardware` / `qemu-tcg`, and the build stays green. The machine-readable
report always says which backends this binary actually contains:

```text
"build": { "macos_backend": …, "windows_backend": …, "linux_kvm_backend": …,
           "harmony_backend": …, "qemu_fallback": true }
```

### Stable status codes

Every backend and the selector emit exactly these strings, so scripts can match
on them without parsing prose:

| Code | Meaning |
| --- | --- |
| `available` | the backend can run this configuration |
| `host-unsupported` | this host cannot use this backend at all |
| `guest-arch-unsupported` | the backend cannot run that guest architecture |
| `accelerator-unavailable` | the host accelerator (hvf / whpx / kvm) is unusable |
| `runtime-missing` | the QEMU runtime for that architecture was not found |
| `config-invalid` | `validate()` produced errors |
| `backend-mismatch` | the configuration belongs to a different backend |
| `excluded-by-policy` | skipped on purpose (for example `backend: qemu`) |
| `no-backend` | nothing is registered |
| `not-registered` | the backend id is not in the catalogue |

Backends may additionally emit **extended** codes that describe their own
platform in more detail. They are never invented for a platform that cannot
produce them:

| Code | Meaning |
| --- | --- |
| `degraded` | usable, but without the accelerator or with reduced capability |
| `runtime-not-runnable` | the runtime exists but cannot be executed here |
| `whpx-api-missing` | `winhvplatform.dll` / `WHvGetCapability` is not available |
| `whpx-hypervisor-not-present` | the hypervisor is not enabled on this host |
| `whpx-degraded` | WHPX is usable but not fully capable |

Because scripts should not have to know every backend-specific string, each
report also carries **`status_class`** — the extended code folded back into the
canonical vocabulary by `vw::statusClass()`. `degraded` and `whpx-degraded`
fold to `available`; `runtime-not-runnable` to `runtime-missing`;
`whpx-api-missing` / `whpx-hypervisor-not-present` to `accelerator-unavailable`.
An unknown or empty code never folds to `available` — unknown becomes
`config-invalid` and empty becomes `not-registered`, so a backend cannot become
selectable by inventing a status string.

### Per-backend dependencies, permissions and fallback

| Backend | Dependency | Permission | Fallback when unavailable |
| --- | --- | --- | --- |
| `macos-virtualization` | macOS 11+, Virtualization.framework; Linux guests need a kernel image (+ optional initrd) | non-sandboxed process, or the `com.apple.security.virtualization` entitlement | `qemu-hardware`, then `qemu-tcg` |
| `windows-hypervisor` | Windows 10 1803+, "Virtual Machine Platform" enabled, `winhvplatform.dll` present | standard user; the hypervisor must be enabled by the administrator | `qemu-hardware`, then `qemu-tcg` |
| `linux-kvm` | `/dev/kvm` | membership of the `kvm` group (or root) | `qemu-hardware`, then `qemu-tcg` |
| `harmony-virtualization` | HarmonyOS/OpenHarmony Linux-compatible layer; KVM when the kernel exposes it | platform-dependent | `qemu-hardware`, then `qemu-tcg` |
| `qemu-hardware` | QEMU runtime; host accelerator `hvf` / `whpx` / `kvm` | platform-dependent | `qemu-tcg` |
| `qemu-tcg` | QEMU runtime only | none | none — this is the last resort |

Notes that are deliberately explicit:

- **Windows backend execution mode.** `windows-hypervisor` performs the WHPX
  capability probe, validation and diagnostics, then runs the guest through QEMU
  with `-accel whpx`. It does not replace QEMU's device model, and it says so in
  `--list-backends`.
- **macOS backend execution mode.** `macos-virtualization` drives
  Virtualization.framework directly. The VM runs *inside* the VirtualWorld
  process, so it ends when that process ends; the CLI warns about this unless
  `--wait` is used. macOS IPSW restore/install is not wired, so macOS guests fall
  back to QEMU.
- **HarmonyOS/OpenHarmony** is treated as a Linux-compatible target. KVM is used
  only when the platform actually exposes it; otherwise QEMU TCG.

### Selecting a backend

- VM config field `backend`: `qemu` (legacy, QEMU only — unchanged behaviour),
  `auto` (full auto-selection), or a concrete backend id.
- Environment variable `VIRTUALWORLD_BACKEND` overrides the config field.
- CLI `--start <vm.json> --backend <id|auto|qemu> [--wait]`.

The Qt GUI keeps launching through QEMU (`VirtualMachineWindow`), so GUI
behaviour is unchanged.

## Runtime Layout

VirtualWorld looks for QEMU in this order:

1. `VIRTUALWORLD_QEMU_RUNTIME` when that environment variable is set.
2. `runtime/qemu/<platform>/<host-arch>/bin`
3. `runtime/qemu/<platform>/<host-arch>`
4. `runtime/qemu/<platform>/bin`
5. `runtime/qemu/<platform>`
6. application bundle resource paths, such as macOS `Contents/Resources`.
7. system `PATH`

Supported runtime roots:

```text
runtime/qemu/windows/
runtime/qemu/macos/
runtime/qemu/linux/
runtime/qemu/harmonyos/
```

HarmonyOS and OpenHarmony are treated as Linux-compatible runtime targets.

The build copies `projects/VirtualWorld/runtime` next to the `VirtualWorld` executable,
so a packaged app can run without depending on an external terminal-installed
QEMU.

## System Software 0-6 runtime

System Software 0 through 6 uses a separate Mini vMac-compatible runtime. It
does not go through QEMU argument translation, QEMU image creation, or the
Virtualization.framework export path used by other configurations.

Place the runtime at `runtime/system-software/minivmac` (or
`minivmac.exe` on Windows), or set `VIRTUALWORLD_SYSTEM_SOFTWARE_RUNTIME`.
**No ROM or guest disk image is distributed by this project.** Supply a
legally obtained ROM explicitly, or point `VIRTUALWORLD_SYSTEM_SOFTWARE_ROMS`
at a private directory containing the required four-part Twiggy dumps. When
those four parts are available and the ROM field is empty, VirtualWorld can
interleave them into a 64 KB `System Software Twiggy.ROM` inside the VM's own
storage directory. An explicitly selected ROM path is preserved unchanged.
System Software 0-3 may be created and launched with no hard disk; the creation
wizard leaves the startup disk optional for those versions. System Software
4-6 still requires at least one user-supplied startup disk image. The generated
command is equivalent to:

```text
minivmac -r <rom> [<startup-disk> ...]
```

## Hardware Acceleration

Set `accelerator` in a VM config to `auto`, `hvf`, `whpx`, `kvm`, or `tcg`.
The default is `auto`.

VirtualWorld chooses the safest available accelerator for the host system:

- macOS: `hvf` for compatible native guests, otherwise `tcg`.
- Windows: `whpx` for compatible native guests, otherwise `tcg`.
- Linux: `kvm` when `/dev/kvm` is available and the guest architecture is compatible, otherwise `tcg`.
- HarmonyOS/OpenHarmony: treated as Linux-compatible; uses `kvm` when available, otherwise `tcg`.

Cross-architecture guests such as x86 on Apple Silicon, PowerPC, or RISC-V use `tcg`.
`VirtualWorld --check <arch>` prints the selected accelerator and candidate list.

`--check` output is unchanged; add `--json` to get the full engine report:

```bash
VirtualWorld --check aarch64 --json
VirtualWorld --list-backends aarch64           # key=value, one block per backend
VirtualWorld --list-backends aarch64 --json    # nested JSON: backends, selection, matrix
```

The JSON report is versioned (`schema`). The current version is
`virtualworld.virtualization/3`. Version 2 changed the HarmonyOS/OpenHarmony
rows from `native` to `hardware` because that backend runs QEMU, not a host
hypervisor API. Version 3 adds `status_class`, pins `qemu-tcg` to the last
candidate slot, and documents the extended backend status codes. The key set is
stable on every platform — a key is always present, only its value changes:

- top level: `schema`, `build`, `host`, `guest_arch`, `preference`, `backends`,
  `selection`, `support_matrix`
- `build`: `macos_backend`, `windows_backend`, `linux_kvm_backend`,
  `harmony_backend`, `qemu_fallback`
- `host`: `platform`, `family`, `arch`, `name`
- `backends[]`: `id`, `name`, `tier`, `execution_tier`, `host_supported`,
  `guest_arch_supported`, `hardware_acceleration`, `native_execution`,
  `accelerator`, `summary`, `status`, `status_class`, `runs_in_process`,
  `requirements`, `issues`
- `selection`: `ok`, `backend`, `name`, `tier`, `execution_tier`, `reason`,
  `fell_back`, `fallback_from`, `considered[]`
- `selection.considered[]`: `id`, `tier`, `host_supported`,
  `guest_arch_supported`, `config_accepted`, `selected`, `status`,
  `status_class`, `reason`

`selection.considered[].status` is always one of the canonical codes, so a
selector trace can be consumed without knowing any backend's private vocabulary;
`backends[].status` may be an extended code and `status_class` is its canonical
fold. `--list-backends` prints the same pair as `status=` / `status_class=`.

### Mechanism tier vs execution tier

Two tiers appear side by side, and they deliberately differ:

| Field | Question it answers | Depends on the host? |
| --- | --- | --- |
| `tier` | which mechanism this backend wraps | no — WHPX is a native mechanism everywhere |
| `execution_tier` | what *this guest* would really be run with *here* | yes |

The gap is where honest degradation lives. WHPX is a native mechanism, but on a
Windows host whose hypervisor is switched off — or for a guest of another ISA —
the guest really runs under QEMU TCG, so `execution_tier` is `emulation` while
`tier` stays `native`. The default implementation is honest too: a backend that
is not supported on this host, or that rejects the guest architecture, always
reports `emulation`, because that guest is handed to `qemu-tcg`.

**Scripts should read `execution_tier`.** `--list-backends` prints
`execution_tier=` per backend plus `selected_execution_tier=` for the winner.
- `support_matrix[]`: `host_platform`, `host_arch`, `guest_arch`, `backend`,
  `tier`, `status`, `verified`, `note`

Every matrix row carries a `verified` flag.

## Build

Install CMake and Qt Core, then run:

```bash
cmake -S projects/VirtualWorld -B build/virtualworld
cmake --build build/virtualworld --config Release
```

### Conditional backend compilation

Platform backends are picked up automatically when their sources exist, and the
build (plus every fallback) stays intact when they do not:

| CMake definition | Set when |
| --- | --- |
| `VIRTUALWORLD_MACOS_BACKEND` | `APPLE` **and** `src/MacVirtualizationEngine.hpp` + `.mm` exist |
| `VIRTUALWORLD_HAVE_WINDOWS_BACKEND` | always (plain C++ on every host) |
| `VIRTUALWORLD_HAVE_LINUX_KVM_BACKEND` | `src/LinuxKvmEngine.hpp` + `.cpp` exist |
| `VIRTUALWORLD_HAVE_HARMONY_BACKEND` | `src/HarmonyVirtualizationEngine.hpp` + `.cpp` exist |

The same definitions are applied to the app and to every backend test target, so
a shared translation unit can never compile an adapter whose platform sources are
not linked. `-DVIRTUALWORLD_INCLUDE_CONTRIB_BACKENDS=OFF` drops the Linux KVM and
Harmony backends entirely; QEMU hardware acceleration and TCG remain.

## Tests

```bash
ctest --test-dir build/virtualworld --output-on-failure
```

| Test | Runs when |
| --- | --- |
| `VirtualWorldTranslatorTests` | always |
| `VirtualWorldVirtualizationEngineTests` | always — shared contract, registry, selection |
| `VirtualWorldVirtualizationIntegrationTests` | always — missing backends, unavailable factories, duplicate registration, cross-arch honesty, JSON field stability, Mac/Harmony fallback |
| `VirtualWorldMacVirtualizationEngineTests` | Apple only (compiles `MacVirtualizationEngine.mm`, links the Apple frameworks) |
| `VirtualWorldHarmonyVirtualizationEngineTests` | whenever the Harmony sources are compiled in; pure Qt/C++ logic, so it runs on any host |
| `VirtualWorldLinuxKvmEngineTests` | whenever the Linux KVM sources are compiled in |
| `VirtualWorldWindowsHypervisorEngineTests` | always (plain C++) |

No suite starts a real virtual machine. Backend tests run out of the build
directory, so CMake prepends the shipped `runtime/qemu/<platform>/<host-arch>/bin`
to their `PATH` — otherwise "QEMU runtime missing" would be a false negative.

## Build With Bundled QEMU

The repository contains `projects/VirtualWorld/qemu-source` as the QEMU source reference.
QEMU is not linked into the VirtualWorld executable because QEMU is its own
runtime program, not a small library. Instead, VirtualWorld builds or ships QEMU
inside its own `runtime/qemu` tree and launches it through `QemuEngine`.

HarmonyOS/OpenHarmony is the exception: its sandbox-sensitive backend is not
allowed to launch that external runtime. It now fails closed until the real
per-architecture `libqemu-inprocess-<arch>.a` core is safely connected; see
`docs/qemu-inprocess.md`. Mini vMac/System Software already runs in-process.

To compile QEMU from the bundled source into the runtime directory:

```bash
cmake -S projects/VirtualWorld -B build/virtualworld -DVIRTUALWORLD_BUILD_BUNDLED_QEMU=ON
cmake --build build/virtualworld --target qemu-runtime --config Release
cmake --build build/virtualworld --config Release
```

The default QEMU target list is:

```text
x86_64-softmmu,i386-softmmu,aarch64-softmmu,arm-softmmu,ppc-softmmu,ppc64-softmmu,m68k-softmmu,riscv64-softmmu,loongarch64-softmmu
```

You can reduce build time by overriding it:

```bash
cmake -S projects/VirtualWorld -B build/virtualworld \
  -DVIRTUALWORLD_BUILD_BUNDLED_QEMU=ON \
  -DVIRTUALWORLD_QEMU_TARGET_LIST=x86_64-softmmu,aarch64-softmmu \
  -DVIRTUALWORLD_QEMU_BUILD_JOBS=4
```

If you already have a tested QEMU build, place the binaries here instead:

```text
projects/VirtualWorld/runtime/qemu/macos/apple-silicon/bin/
projects/VirtualWorld/runtime/qemu/macos/x86_64/bin/
projects/VirtualWorld/runtime/qemu/windows/x86_64/bin/
projects/VirtualWorld/runtime/qemu/windows/aarch64/bin/
projects/VirtualWorld/runtime/qemu/linux/x86_64/bin/
projects/VirtualWorld/runtime/qemu/linux/aarch64/bin/
projects/VirtualWorld/runtime/qemu/linux/riscv64/bin/
```

At minimum, include `qemu-img` and the required `qemu-system-*` executables.
On macOS and Linux, make sure those files are executable.

## Core Commands

```bash
VirtualWorld --gui
VirtualWorld --check x86_64
VirtualWorld --check x86_64 --json
VirtualWorld --list-backends aarch64
VirtualWorld --list-backends aarch64 --json
VirtualWorld --test x86_64
VirtualWorld --create-qcow2 ./disk.qcow2 32768
VirtualWorld --import-qemu ./run.sh --save-config ./vm.json
VirtualWorld --import-utm ./Example.utm --save-config ./vm.json
VirtualWorld --import-virtualworld ./old.json --save-config ./vm.json
VirtualWorld --print-command ./vm.json
VirtualWorld --export-qemu ./vm.json --output ./run.sh
VirtualWorld --export-utm ./vm.json --output ./Example.utm
VirtualWorld --start ./vm.json
VirtualWorld --start ./vm.json --backend auto
VirtualWorld --start ./vm.json --backend qemu-tcg --wait
VirtualWorld --stop <pid>
```

Without arguments, VirtualWorld opens the Qt GUI.

## Conversion Formats

VirtualWorld can convert between:

- QEMU command/script -> VirtualWorld JSON
- UTM `.utm` package or `config.plist` -> VirtualWorld JSON
- VirtualWorld JSON -> QEMU script
- VirtualWorld JSON -> UTM `.utm` package with `config.plist`

The GUI exposes the same conversion path and keeps the C++ core as the source
of truth.

## Support Disk Trust

Support Disk updates are verified by the application layer using an official
Ed25519 signature, signed metadata, expiry checks, source validation, and a
SHA-256 payload digest. Windows, Linux, and macOS Support Disks are separate
signed products with distinct payload names and public keys. The macOS Support
Disk is created only for macOS hosts. A Linux guest receives a
read-only `linux-recovery-guard` disk; it is kept outside normal guest boot
order and is used by the same VirtualWorld self-check and recovery workflow.
The verifier is directly callable:

```bash
VirtualWorld --verify-support-disk <manifest.json> <support-disk>
```

High-risk host operations require a separately signed, short-lived
authorization token bound to the operation and resource, plus explicit user
confirmation. See `SECURITY.md`.

QEMU remains an independent runtime executable. Its source and third-party
license obligations are tracked separately in `LICENSE-AUDIT.md`.

## Display And GPU

The VM config `graphics` section supports:

- `adapter`: QEMU display device, such as `virtio-vga`, `virtio-vga-gl`, `qxl-vga`, or `VGA`.
- `openGl`: enables QEMU display OpenGL when supported.
- `autoResize`: asks supported QEMU display backends to fit the guest window.
- `dynamicResolution`: preserved for UTM conversion.
- `retina`: preserves HiDPI/Retina intent for macOS and UTM.

Compatibility notes:

- OpenGL requires support from the QEMU build, host display backend, and guest drivers.
- Linux guests can be unstable with OpenGL in some configurations; UTM export warns and keeps OpenGL disabled for Linux by default.
- PPC/RISC-V guests fall back to safer display devices and TCG-oriented behavior.

## Image Download And VM Creation

The GUI includes a basic image download/import workflow:

- macOS: fetches IPSW candidates, keeps only Apple HTTPS direct URLs, verifies the
  final redirect and an available SHA-256 value, then downloads the selected IPSW.
  If a macOS VM has no manually imported image when starting, VirtualWorld prompts to
  fetch the newest compatible IPSW candidate.
  The current QEMU backend uses the verified IPSW as boot/recovery media. A
  physical-device-equivalent native IPSW restore requires a future
  `Virtualization.framework` backend and is not claimed by this QEMU path.
- Windows: opens the official Microsoft Windows download page. Microsoft ISO
  links are generated by the official page flow, so VirtualWorld does not hardcode
  temporary ISO URLs.
- Linux/BSD/DOS/other: manual import only.

Creation options include:

- Memory entry in KB, MB, or GB.
- qcow2 disk creation in KB, MB, or GB.
- Disk interfaces: VirtIO, IDE, SATA, SCSI, NVMe, USB, floppy, and CD/DVD.
- DOS and Old Windows use a legacy PC preset: i386, PC machine, TCG,
  single-core CPU, Cirrus VGA, floppy/IDE-friendly storage, no TPM, and no
  modern default network device.
- Linux direct kernel boot with `kernel`, `initrd`, and kernel append arguments.
- Windows TPM 2.0 intent. A working `swtpm` socket is required before QEMU TPM
  arguments are emitted.
- macOS VM command generation attempts to use the host serial number on macOS.

## Open-source license

Copyright (C) 2026 廖博轩.

VirtualWorld's original application code is released under the **GNU General
Public License, version 2 only** (`GPL-2.0-only`). You may use, study, modify,
and redistribute it under the conditions in [LICENSE](LICENSE).

This repository also integrates or can distribute separate components:

- **QEMU** is executed as a separate runtime. QEMU as a whole is GPLv2, while
  individual source files, firmware, libraries, and data files can carry other
  compatible licenses. Its authoritative notices remain in `qemu-source/`.
- **Mini vMac-compatible runtimes** used for System Software 0-6 are GPLv2.
  Their license and corresponding source archive are under
  `runtime/system-software/licenses/`.
- **ROMs, guest operating systems, installation media, firmware, and user VM
  images are not automatically covered by VirtualWorld's GPL license.** A
  distributor must have independent permission to redistribute every such
  file included in a source archive or binary package.
- Qt, OpenSSL, platform SDKs, and other build/runtime dependencies retain their
  own licenses.

Read [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md),
[LICENSE-AUDIT.md](LICENSE-AUDIT.md), and
[OPEN_SOURCE_RELEASE_CHECKLIST.md](OPEN_SOURCE_RELEASE_CHECKLIST.md) before
publishing a release. These files are compliance aids, not legal advice.

## Contributing and security

Contributions are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md) and
keep platform-specific work within the documented backend boundary. By
submitting a contribution, you agree that it may be distributed under
`GPL-2.0-only`.

Please do not publish exploitable security details in a public issue. Follow
[SECURITY.md](SECURITY.md) for responsible reporting.

## Repository hygiene

Build directories, generated packages, local runtime downloads, logs, and VM
images are not source. Keep them out of commits. In particular, never commit
private signing keys, credentials, personal VM data, or guest media for which
you do not have redistribution rights.
