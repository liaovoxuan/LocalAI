# Release License Audit

VirtualWorld's original application code is distributed under
`GPL-2.0-only`. The complete license text is in `LICENSE`; the project author
and maintainer is 廖博轩 (Liao Boxuan).

VirtualWorld launches QEMU as an independent executable runtime. It does not
statically link QEMU into the VirtualWorld application.

The QEMU source and any bundled runtime are distributed under the licenses stated
by their files, including the QEMU GPLv2 terms and the separate licenses for
firmware, libraries, and TCG components. See:

- `qemu-source/LICENSE`
- `qemu-source/COPYING`
- `qemu-source/COPYING.LIB`
- `runtime/qemu/README.md`

Before an official release, maintainers must:

1. Record the exact QEMU commit and build configuration.
2. Ship the corresponding source or a written offer/source-access procedure
   required by the applicable license.
3. Include notices and license texts for bundled firmware and libraries.
4. Record whether each runtime file is QEMU, firmware, or a third-party
   dependency and identify its license.
5. Re-run the audit whenever the runtime binaries or build options change.

## Mini vMac-compatible runtime

System Software 0-6 uses a separate Mini vMac-compatible runtime. The tracked
macOS ARM64 executables are GPLv2 binaries. A distributor must:

1. Map each binary to the exact configuration and corresponding source in
   `runtime/system-software/licenses/Mini-vMac-VirtualWorld-Source.tar.gz`.
2. Ship `Mini-vMac-COPYING.txt` and the complete corresponding source/build
   information required by GPLv2.
3. Confirm the public source archive still excludes `extras/roms` and
   `extras/disks`.

## ROMs, firmware, and guest software

The public project intentionally provides no ROM or guest disk image. Before a
public release, verify that `runtime/system-software/roms` is absent/empty and
that no ROM/disk-like payload has re-entered a source archive or binary package.
Users must supply legally obtained copies privately.

VirtualWorld's GPLv2 license does not license guest operating systems,
installation media, recovery images, firmware, ROMs, or user VM data.

## Other dependencies

Record the exact Qt, OpenSSL, platform SDK, compiler, and runtime-library
versions used for every binary release. Include the authoritative notices and
source/relinking materials required by those exact distributions.

The Support Disk trust implementation is part of the GPLv2 VirtualWorld
application. Private signing keys and issued private material are not source
code and must never be committed.

This document and `OPEN_SOURCE_RELEASE_CHECKLIST.md` are engineering aids, not
legal advice.
