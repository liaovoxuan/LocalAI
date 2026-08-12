# VirtualWorld

VirtualWorld is the C++ core migration of the former QEMU Bridge project.
It manages QEMU as a backend runtime and does not reimplement QEMU.

Current phase: C++ core plus a lightweight Qt Widgets manager UI.
QEMU is managed as an internal runtime. The project can ship prebuilt QEMU
files under `runtime/qemu`, or build the bundled `qemu-master` source into that
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
  |-- QemuEngine
  |-- ImageManager
  |-- ConfigManager
  |-- RuntimeManager
  |
  v
QEMU Runtime
```

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

The build copies `virtualworld/runtime` next to the `VirtualWorld` executable,
so a packaged app can run without depending on an external terminal-installed
QEMU.

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

## Build

Install CMake and Qt Core, then run:

```bash
cmake -S virtualworld -B build/virtualworld
cmake --build build/virtualworld --config Release
```

## Build With Bundled QEMU

The repository contains `virtualworld/qemu-master` as the QEMU source reference.
QEMU is not linked into the VirtualWorld executable because QEMU is its own
runtime program, not a small library. Instead, VirtualWorld builds or ships QEMU
inside its own `runtime/qemu` tree and launches it through `QemuEngine`.

To compile QEMU from the bundled source into the runtime directory:

```bash
cmake -S virtualworld -B build/virtualworld -DVIRTUALWORLD_BUILD_BUNDLED_QEMU=ON
cmake --build build/virtualworld --target qemu-runtime --config Release
cmake --build build/virtualworld --config Release
```

The default QEMU target list is:

```text
x86_64-softmmu,aarch64-softmmu,arm-softmmu,i386-softmmu,ppc-softmmu,ppc64-softmmu,riscv64-softmmu
```

You can reduce build time by overriding it:

```bash
cmake -S virtualworld -B build/virtualworld \
  -DVIRTUALWORLD_BUILD_BUNDLED_QEMU=ON \
  -DVIRTUALWORLD_QEMU_TARGET_LIST=x86_64-softmmu,aarch64-softmmu \
  -DVIRTUALWORLD_QEMU_BUILD_JOBS=4
```

If you already have a tested QEMU build, place the binaries here instead:

```text
virtualworld/runtime/qemu/macos/apple-silicon/bin/
virtualworld/runtime/qemu/macos/x86_64/bin/
virtualworld/runtime/qemu/windows/x86_64/bin/
virtualworld/runtime/qemu/windows/aarch64/bin/
virtualworld/runtime/qemu/linux/x86_64/bin/
virtualworld/runtime/qemu/linux/aarch64/bin/
virtualworld/runtime/qemu/linux/riscv64/bin/
```

At minimum, include `qemu-img` and the required `qemu-system-*` executables.
On macOS and Linux, make sure those files are executable.

## Core Commands

```bash
VirtualWorld --gui
VirtualWorld --check x86_64
VirtualWorld --test x86_64
VirtualWorld --create-qcow2 ./disk.qcow2 32768
VirtualWorld --import-qemu ./run.sh --save-config ./vm.json
VirtualWorld --import-utm ./Example.utm --save-config ./vm.json
VirtualWorld --import-virtualworld ./old.json --save-config ./vm.json
VirtualWorld --print-command ./vm.json
VirtualWorld --export-qemu ./vm.json --output ./run.sh
VirtualWorld --export-utm ./vm.json --output ./Example.utm
VirtualWorld --start ./vm.json
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

- macOS: fetches Apple Virtual Machine IPSW candidates and downloads the selected IPSW.
  If a macOS VM has no manually imported image when starting, VirtualWorld prompts to
  fetch the newest compatible IPSW candidate.
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
