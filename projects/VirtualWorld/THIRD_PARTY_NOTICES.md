# Third-Party Notices

This file describes license boundaries; the authoritative terms are the
license files shipped with each component.

## VirtualWorld application

Copyright (C) 2026 廖博轩 (Liao Boxuan).

VirtualWorld's original application code is licensed under the GNU General
Public License, version 2 only (`GPL-2.0-only`). See `LICENSE`.

## QEMU

VirtualWorld launches QEMU as a separate executable and may build it from the
`qemu-source` Git submodule. QEMU states that the emulator as a whole is
released under GPLv2. Individual files, libraries, firmware, ROMs, and data
distributed with QEMU can use different GPL-compatible or independent
licenses. Consult, at minimum:

- `qemu-source/LICENSE`
- `qemu-source/COPYING`
- `qemu-source/COPYING.LIB`
- license headers and notices inside the exact QEMU build

QEMU is a trademark of Fabrice Bellard. VirtualWorld is not an official QEMU
project.

## Mini vMac-compatible runtime

The Mini vMac-compatible executables under `runtime/system-software` are GPLv2
software. The included license and source archive are:

- `runtime/system-software/licenses/Mini-vMac-COPYING.txt`
- `runtime/system-software/licenses/Mini-vMac-VirtualWorld-Source.tar.gz`

A release maintainer must verify that the source archive exactly corresponds
to each distributed executable and contains the build scripts/configuration
needed to reproduce it.

## Qt, OpenSSL, and platform SDKs

VirtualWorld uses Qt Core, Widgets, and Network, plus OpenSSL Crypto. Their
license obligations depend on the exact versions and whether they are linked
or bundled by a distributor. Apple Virtualization/Foundation/Security,
Windows WHPX, and Linux KVM interfaces remain subject to their platform terms.
Include the authoritative notices for the versions actually shipped.

## ROMs, firmware, and guest software

VirtualWorld's GPL license does **not** license Apple ROMs, guest operating
systems, installer media, firmware, recovery images, or user VM images.
VirtualWorld deliberately does not distribute ROMs or guest disks, and the
Mini vMac source archive excludes its former example ROM/disk directories.
Users must supply legally obtained copies privately. A distributor must keep
those assets out of public source and binary packages unless it has separate,
documented permission.

## No warranty

This notice is an engineering inventory, not legal advice. It does not replace
the applicable license texts or a release-specific legal review.
