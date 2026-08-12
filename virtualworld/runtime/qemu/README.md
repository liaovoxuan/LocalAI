# QEMU Runtime

VirtualWorld treats QEMU as a backend runtime, not as code to reimplement.
The application always asks `QemuEngine` and `RuntimeManager` for QEMU paths;
GUI code should not call `qemu-system-*` or `qemu-img` directly.

Lookup order:

1. `VIRTUALWORLD_QEMU_RUNTIME`, when set.
2. Bundled runtime under this directory.
3. Runtime copied next to the app or into app resources.
4. QEMU tools available in the user's system `PATH`.
5. If none exists, VirtualWorld reports the missing runtime path.

Recommended layout:

```text
runtime/qemu/
  windows/
    x86_64/bin/qemu-system-x86_64.exe
    x86_64/bin/qemu-img.exe
    arm64/bin/...
  macos/
    apple-silicon/bin/...
    x86_64/bin/...
  linux/
    x86_64/bin/...
    aarch64/bin/...
    riscv64/bin/...
  harmonyos/
    aarch64/bin/...
```

VirtualWorld does not force users to visit the QEMU website. Distributors can
ship a tested runtime here, and advanced users can still use the QEMU already
available in `PATH`.

## Building From Bundled Source

If `virtualworld/qemu-master` is present, CMake can build it into this runtime
tree:

```bash
cmake -S virtualworld -B build/virtualworld -DVIRTUALWORLD_BUILD_BUNDLED_QEMU=ON
cmake --build build/virtualworld --target qemu-runtime --config Release
```

This keeps VirtualWorld self-contained for packaging while respecting QEMU's
own build system and GPL licensing model.
