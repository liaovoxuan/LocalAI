# QEMU in-process core → HarmonyOS HAP : OHOS cross-build & product link

Owner: QEMU/OHOS cross-build + product-link role (Hy3 free model).
Device: HarmonyOS 6.1.1 (API 24) MateBook Pro ARM64, HDC 127.0.0.1:5555.
Source project: `projects/VirtualWorld` (this repo).
DevEco project: `/Users/liaoboxuan/VirtualWorld` (this machine's environment
note only — **not** a script default; `link-libvirtualworld.sh` requires
`--deveco-root` / `VW_DEVECO` explicitly).

> **Status (2026-10-17): BLOCKED** — see `build-qemu-ohos/.qemu-ohos-linked-blocked`.
> DeepSeek's `.qemu-core-embeddable-ready` gate is not present, so no real
> `aarch64-linux-ohos` QEMU core archive can be built/linked here. All
> in-scope static dependencies and the ABI glue are done and verified; the link
> step waits on the gate.

## Exclusive scope (this role may write)
- `third_party/ohos-ports/**`
- `ci/ohos/**`
- `build-qemu-ohos/**`, `build-qemu-ohos-deps/**`
- `docs/qemu-ohos-link.md`
- DevEco: `entry/src/main/cpp/CMakeLists.txt`, `entry/src/main/cpp/virtualization/qemu/**`,
  `entry/src/main/cpp/types/libvirtualworld/**`

## Read-only / forbidden (never modified by this role)
`qemu-source/**`, `build-qemu-inprocess/**`, `src/QemuInProcessRuntime.*`, the QEMU
lifecycle gate, Mini vMac, ArkUI pages, README, and the existing Windows/macOS/Linux
build paths & tests.

## Platform-isolation contract (added 2026-10-17)
HarmonyOS-specific QEMU may be refactored, but it MUST NOT affect other VirtualWorld
platforms.
- All OHOS deps / glue / build targets are independent and live only under the
  paths above.
- `VW_ENABLE_QEMU_OHOS` (CMake) **defaults to OFF**. When OFF, NO QEMU source is
  compiled or linked; the built `libvirtualworld.so` / HAP is byte-for-byte the
  current Mini vMac build (no change to other-platform paths, runtime logic, or tests).
- Only when the switch is ON (and a real archive is supplied) is the OHOS static
  core linked. If the switch is ON but no real archive is given, CMake fails
  loudly — it refuses to fake integration.

## What is real and verified
1. **OHOS aarch64 toolchain** at
   `/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/native`.
   A trivial cross-compile emits a genuine `aarch64-linux-ohos` ELF
   (`interpreter /lib/ld-musl-aarch64.so.1`).
2. **Real static dependencies** in `build-qemu-ohos-deps/prefix/lib` — now `.a`
   only (the previous `.so` impersonators were removed). Built with the OHOS
   aarch64 clang wrapper via `build-qemu-ohos/build-deps-static.sh`:
   - `libffi.a` (libffi-3.4.4)
   - `libpcre2-8.a` (pcre2-10.42)
   - `libglib-2.0.a`, `libgobject-2.0.a`, `libgmodule-2.0.a`, `libgthread-2.0.a`,
     `libgio-2.0.a` (glib-2.0 2.78.0)
   - `libintl.a`, `libpcre2-posix.a`
   - **Proof** (member extracted from each `.a`, `llvm-readelf -h`):
     `ELF 64-bit LSB relocatable, ARM aarch64, version 1 (SYSV)` — genuine
     `aarch64-linux-ohos`, not host Mach-O.
3. **aarch64-softmmu minimal cross-configure SUCCEEDED** (`CONFIGURE_EXIT=0`,
   out-of-tree under `build-qemu-ohos-deps/qemu-config-probe`, read-only on
   `qemu-source`). Toolchain + deps + config are correct for OHOS.
4. **DevEco HAP pipeline works** today: it builds an `arm64-v8a`
   `libvirtualworld.so` (MiniVmac-only, 0 QEMU symbols) and a HAP via
   `build-hap.sh`.

## ABI glue (rewritten to the REAL contract)
`entry/src/main/cpp/virtualization/qemu/qemu_inprocess_glue.{h,cpp}` adapts
**only** the public API declared in `qemu-source/qemu-inprocess/qemu_inprocess.h`
(read-only, reference only):
- `qemu_inprocess_session_create` / `qemu_inprocess_session_destroy`
- `qemu_inprocess_init(session, argc, argv, callbacks)`
- `qemu_inprocess_request_stop`, `qemu_inprocess_cleanup`
- `qemu_inprocess_last_error`
- `qemu_inprocess_run` exists in the contract but is **intentionally NOT exposed**
  (it blocks on the guest main loop and starts guest execution).
- **No** `start()` / `configure()` / `status()` entry points are declared — those
  do not exist in the contract and were removed after the prior round's mistake.
- The NAPI layer exposes a minimal object-free lifecycle:
  `qemuCreateSession` / `qemuInitSession` / `qemuStopSession` / `qemuDestroySession`
  (+ `qemuStatus`). A failure ALWAYS returns `{"ok":false,"error":...}` — never a
  fake `{"ok":true}`.
- Self-registers a distinct NAPI module `virtualworldqemu` so it NEVER collides
  with the existing Mini vMac module `virtualworld` in `virtualworld_napi.cpp`
  (which is outside this role's writable scope). **REACHABILITY / NOT
  INTEGRATED:** the standard ArkTS `import ... from 'libvirtualworld'` resolves
  only the `virtualworld` module, so this `virtualworldqemu` module is NOT
  reachable from ArkTS EVEN WHEN the QEMU core is linked. Making it reachable
  requires a single guarded `napi_property_descriptor` line in the `Init()` of
  `virtualworld_napi.cpp` (not in the authorized edit set). **Therefore the NAPI
  interface is scaffolding only — it must NOT be described as integrated** until
  that line lands and the gate opens. No fake success; when unlinked it returns
  genuine "method not found".

## Dependency gate (must pass before any QEMU link/read)
Linking (and even reading the independent source tree) happens ONLY when **all** hold.
DeepSeek published `build-qemu-ohos/.qemu-core-embeddable-ready` explicitly stating:
1. `source_tree=qemu-ohos-source`  — the core comes from the INDEPENDENT
   `qemu-ohos-source` tree, NEVER the shared `qemu-source`.
2. `qemu-source_clean=true`.
3. `no_permanent_per_cycle_residue=true`.
4. `spawn_symbols=0`  — no fork/execve/system/popen/posix_spawn in the core
   (also re-verified with `llvm-nm` on the archive and on the final `.so`).
If the gate is not satisfied, this role completes the static deps + ABI glue and
writes/keeps `.qemu-ohos-linked-blocked` — it does NOT read `qemu-ohos-source`,
does NOT build/link any QEMU object, and never uses the shared-QEMU or macOS
Mach-O archives.

**Current gate state (2026-10-17): CLOSED.** DeepSeek formally BLOCKED this round
and **withdrew** the earlier `README`:
`build-qemu-inprocess/.qemu-core-embeddable-ready.withdrawn-2026-10-17T102010`;
`.qemu-core-embeddable-blocked` is active, with
`no_permanent_per_cycle_residue: false` (teardown RCU-drain SIGSEGV; 4 core
objects carry `fork/execv/execvp/system`). Therefore there is **no valid**
`.qemu-core-embeddable-ready` and this role stays BLOCKED.

## Pre-delivery checklist (added 2026-10-17)
Before claiming READY, this role MUST:
- **Switch-OFF regression build**: `build-qemu-ohos/regress-switch-off.sh` builds
  the default (switch-OFF) HAP and asserts **symbol/build isolation** — build
  succeeds and the arm64-v8a `libvirtualworld.so` carries **0** `qemu_inprocess_*`
  symbols. It does NOT claim byte-for-byte identity (no trusted pre-change
  baseline); it persists evidence under `build-qemu-ohos/evidence/`
  (`switch-off-report.txt`, `switch-off-build.log`, `libvirtualworld.so`,
  `switch-off-reference.sha256`) with HAP/.so SHA256 fingerprints. Any
  regression ⇒ BLOCKED.
- **Non-OHOS zero-change list**: `git` is absent in both repos, so the list is
  established by explicit enumeration of protected paths (see the marker). Any
  stray change to `qemu-source/**`, `qemu-ohos-source/**`, `build-qemu-inprocess/**`,
  `src/QemuInProcessRuntime.*`, Mini vMac, `virtualworld_napi.cpp`, ArkUI, README,
  or other-platform CMake/tests ⇒ BLOCKED.

## Product-link procedure (runs only when the gate passes)
**Portability:** `link-libvirtualworld.sh` contains **no personal/absolute path
defaults**. The DevEco project root MUST be supplied explicitly via
`--deveco-root <dir>` (or `export VW_DEVECO=<dir>`); missing/invalid ⇒ hard
error, never a silent fallback.
```
build-qemu-ohos/link-libvirtualworld.sh \
    --deveco-root <DevEco project root> \
    --qemu-core-archive <ohos>/libqemu-aarch64-softmmu.a \
    --inprocess-core-archive <ohos>/libqemu-inprocess-aarch64.a
```
`sh -n` syntax + 8 negative/portability cases (no args, `--help`, unknown arg,
nonexistent root, non-DevEco dir, missing archive, `VW_DEVECO` env path) all
PASS — persisted at `build-qemu-ohos/evidence/link-script-portability-tests.txt`.
Statically links the real QEMU archives + OHOS static deps into
`libvirtualworld.so`, rebuilds the HAP, then proves integration via THREE
independent methods: `llvm-readelf` (architecture + entry symbols), `llvm-nm`
(QEMU + in-process symbols present, no spawn symbols), and **unpacking the HAP**
(confirm the `.so` inside truly contains the QEMU objects). A HAP-internal,
object-free `create → init → stop → destroy` smoke (NO `run`, no guest) is run.
It writes `.qemu-ohos-linked-ready` (`real_guest_started=false`) only if every
proof passes; otherwise `.qemu-ohos-linked-blocked` with the first real error.

## Constraints honoured
No external `qemu-system`; no `fork`/`exec`/`system`/`popen`/`QProcess` in
integrated code. No guest started. Concurrent compiles capped at 2; single-process
RSS budget 1.5 GiB. No local LLM; no deletion of other builds; no commit/push.
Model kept as Hy3 (free).
