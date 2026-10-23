# System Software runtime

The macOS Apple Silicon package includes separate Mini vMac-compatible kernels
for Twiggy, Macintosh 128K, Macintosh 512Ke, and Macintosh Plus under
`macos/apple-silicon/bin`. VirtualWorld selects the kernel from the bound
machine/ROM preset. Other platforms may place a compatible executable in this
directory as `minivmac` (`minivmac.exe` on Windows), or set
`VIRTUALWORLD_SYSTEM_SOFTWARE_RUNTIME` to an executable path.

No ROM or guest disk is distributed by VirtualWorld. Supply a legally obtained
ROM explicitly, or set `VIRTUALWORLD_SYSTEM_SOFTWARE_ROMS` to a private
directory containing the four required 16 KB Twiggy bus dumps. When those files
are available and a System Software VM has an empty ROM field, VirtualWorld
interleaves them into `System Software Twiggy.ROM` in that VM's storage
directory.

The bundled kernels receive the selected ROM through a private process
environment variable and receive Floppy A then Floppy B as disk image paths.
External compatible runtimes continue to receive `-r <rom>` followed by the
configured disks. System Software 0-3 may run with ROM only; versions 4-6
require at least one startup disk. This runtime is intentionally separate from
`runtime/qemu` and is used only by System Software 0-6 configurations.

Mini vMac is licensed under GPL-2.0. Its license and the source archive intended
to correspond to the bundled arm64 kernels are in `licenses`. Release builders
must verify the source/configuration-to-binary mapping before distribution.

The emulator license does not grant rights to Apple ROMs, System Software,
guest disks, or other third-party assets. They are intentionally excluded from
the runtime tree and source archive; users must supply legally obtained copies.
See the repository-level `THIRD_PARTY_NOTICES.md` and
`OPEN_SOURCE_RELEASE_CHECKLIST.md`.
