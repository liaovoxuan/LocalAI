# Open-Source Release Checklist

Complete this checklist for every public source tag and binary package.

## Source tree

- [ ] `LICENSE` is the unmodified GNU GPL version 2 text.
- [ ] README language links work and the support matrix matches the code.
- [ ] Copyright identifies 廖博轩 and the release year is current.
- [ ] No credentials, signing keys, tokens, personal paths, VM data, or logs
      are present.
- [ ] Generated `build*`, `dist`, CMake cache, and editor files are excluded.
- [ ] Git submodules resolve to public, immutable commits.
- [ ] `git -C projects/VirtualWorld/qemu-source status --porcelain` is empty,
      or every intentional QEMU modification is published as source/patches;
      local sync metadata and downloaded wheels are excluded.

## QEMU

- [ ] Record the exact `qemu-source` commit and configure arguments.
- [ ] Provide complete corresponding source (including distributor patches and
      build scripts) or another GPLv2-compliant source delivery method.
- [ ] Ship `COPYING`, `COPYING.LIB`, and all firmware/library notices used by
      the exact runtime build.
- [ ] Inventory every bundled QEMU binary, firmware image, library, and data
      file with its SHA-256 and license.

## Mini vMac

- [ ] Map every distributed `minivmac-*` binary to its exact source/config and
      reproducible build command.
- [ ] Confirm the source archive is complete corresponding source for those
      binaries and ship `Mini-vMac-COPYING.txt`.
- [ ] Confirm the archive does not contain `extras/roms`, `extras/disks`, or
      equivalent ROM/guest-disk payloads.

## ROMs and guest media

- [ ] Confirm `runtime/system-software/roms` is absent or empty.
- [ ] Scan source archives and packages to confirm that no ROM, System Software,
      recovery image, installer, or guest disk has been included.
- [ ] Require the user to supply a legally obtained ROM/guest image privately.
- [ ] Do not publish operating-system installers, recovery media, firmware, or
      user images merely because VirtualWorld itself is GPLv2.

## Dependencies and packaging

- [ ] Record exact Qt, OpenSSL, compiler, SDK, and system-library versions.
- [ ] Include all notices and relinking/source obligations required by the
      chosen Qt/OpenSSL distribution model.
- [ ] Build cleanly with contrib backends ON and OFF.
- [ ] Run the complete CTest suite and preserve the test log.
- [ ] Verify release manifests and artifact SHA-256 values.
- [ ] Mark untested physical hosts as unverified; never convert CI simulation
      into a real-hardware claim.
- [ ] Sign/tag artifacts only after every release blocker is resolved.

This checklist is not legal advice.
