# In-Process QEMU Core for HarmonyOS / OpenHarmony

## Goal

Run the QEMU system emulator **inside the VirtualWorld application process** on
HarmonyOS / OpenHarmony, as a statically linked native library on a worker
thread — **never** as a `qemu-system` child process.

This replaces the subprocess model used elsewhere (e.g. `QemuEngine`, which
builds a command line and launches QEMU via `QProcess`) with a library model:

| Property | Subprocess model (forbidden here) | In-process model (this work) |
|---|---|---|
| How QEMU runs | child process (`QProcess`/fork/exec) | static lib on a host worker thread |
| Identity | PID | opaque `QemuSession*` handle |
| Lifecycle | signals / PID bookkeeping | `init`/`run`/`request_stop`/`cleanup` |
| Shutdown | kill the PID | `qemu_system_shutdown_request()` |
| Cross-arch | exec a different binary | single core, no exec |

Hard constraints (non-negotiable):
- No `QProcess`, `fork`/`exec`, `system`, `popen`, `startDetached`, or shell to
  launch QEMU. Enforced by a static scan in the build.
- No fake runner, no re-skinned param layer, no design-doc-only deliverable.
- No bundled guest ROM / OS image. Disk contents come from the host at runtime
  through block callbacks.

## QEMU version & license

- Source tree: `qemu-source/` — **QEMU 11.1.50** (`cat qemu-source/VERSION`).
- License: **GPLv2**. `qemu-source/COPYING` is the GNU GPL version 2. The
  in-process entry layer added under `qemu-source/qemu-inprocess/` is part of
  the QEMU distribution and is distributed under the same GPLv2 terms.
- The `qemu-system` entry points we reuse are real and present in this tree:
  - `void qemu_init(int argc, char **argv);` — `include/system/system.h:108`
  - `int qemu_main_loop(void);` — `include/system/system.h:109`
  - `void qemu_cleanup(int status);` — `include/system/system.h:110`
  - `void qemu_system_shutdown_request(ShutdownCause);` — `include/system/runstate.h:141`
  - `bool kvm_enabled(void);` — `include/system/kvm.h`
  - `tcg_interpreter` meson option — `meson_options.txt:98` (the W^X / JIT
    fallback, see below)
  - KVM opens `/dev/kvm` — `accel/kvm/kvm-all.c:2929`

## Architecture

```
 Application process (single PID)
 └─ QemuInProcessRuntime  (C++ wrapper, src/QemuInProcessRuntime.*)
      └─ libqemu-inprocess-<arch>.a  (real target archive, no main())
           └─ qemu_init / qemu_main_loop / qemu_cleanup  (real QEMU)
      worker thread ── runs qemu_main_loop(), emits frames/audio/block IO
      host callbacks ── display / input / audio / block  (forwarded to the app)
```

- **Opaque handle:** the VM is addressed by `QemuSession*` (a pimpl). No PID is
  ever stored or compared.
- **Callback contract (`QemuInProcessCallbacks`):** display / input / audio /
  block. All are plain C function pointers so the core (C) can call into C++ via
  a `userdata` trampoline. No guest image is embedded; the block callbacks let
  the host supply disk sectors on demand.

## Lifecycle

```
session = qemu_inprocess_session_create();        // opaque handle
qemu_inprocess_init(session, argc, argv, &cb);    // wraps qemu_init
// worker thread:
status = qemu_inprocess_run(session);            // wraps qemu_main_loop (blocks)
qemu_inprocess_request_stop(session);            // wraps qemu_system_shutdown_request
qemu_inprocess_cleanup(session);                 // wraps qemu_cleanup
```

The C++ wrapper (`QemuInProcessRuntime`) adds: single-instance registry,
reentrant `stop()`/`destroy()`, failed-init resource release, and the
accelerator policy.

## Single-instance enforcement

QEMU keeps process-global state (machine, accelerator, BQL, AIO context). A
second `qemu_init()` before `qemu_cleanup()` is unsafe. Therefore the core
enforces **exactly one active in-process instance per process** via an atomic
guard. A second `create()` with an already-active tag returns a stable
`QemuErrorCode::Busy`. Multi-guest support must come from multiple processes or
QEMU's own multi-machine support, never from concurrent `qemu_init()`.

## Accelerator policy (KVM / TCG / W^X)

Decision function `decideAccel(hostIsa, targetIsa, kvmNodeAccessible)`:

| host ISA | target ISA | /dev/kvm | Decision | Hardware accelerated? |
|---|---|---|---|---|
| same | same | yes | **KVM** | yes |
| same | same | no  | TCG | no |
| diff | diff | any | **TCG** | **no (never)** |

- **Same-ISA + accessible `/dev/kvm`** → KVM.
- **Same-ISA, no `/dev/kvm`** (or KVM compiled out) → TCG.
- **Cross-ISA** → TCG only; the decision MUST NOT report hardware acceleration.
- **W^X / JIT restricted:** QEMU's Tiny Code Interpreter (`--enable-tcg-interpreter`,
  meson option `tcg_interpreter`) compiles TCG to a portable bytecode interpreter
  with **no executable JIT page**. When the platform forbids RWX/JIT, build with
  `tcg_interpreter=true` and select `accel=tcg`. This is a compilable, supported
  interpreter path — no exec, no JIT.
- If `/dev/kvm` exists but KVM cannot actually be initialised, QEMU transparently
  falls back to TCG; the policy only gates whether KVM is *attempted*.

## Multi-architecture symbol isolation

QEMU's many objects define global symbols that collide across targets. To avoid
runtime `exec` of a per-arch binary, isolate at link time:

- **Per-architecture static libraries:** build one `libqemu-inprocess-aarch64.a`,
  `libqemu-inprocess-x86_64.a`, … each containing only that target's objects, and
  link the one matching the host/target pair. Because the in-process model allows
  exactly one active instance, a single per-arch lib per process is sufficient.
- **or symbol prefixing:** build QEMU with a name prefix (`--with-pkgversion` /
  objcopy `--prefix-symbols`) so multiple arch cores could coexist without
  clashes. This is a build-time choice, never a runtime exec.

## Build

`build-qemu-inprocess/CMakeLists.txt` is a self-contained build. It only reads:
`src/QemuInProcessRuntime.*`, `qemu-source/qemu-inprocess/*`,
`tests/QemuInProcessRuntimeTests.cpp`, `tests/qemu_inprocess_mock.c`.

```bash
# 1) Wrapper logic tests against the transparent contract mock (no QEMU deps):
mkdir -p build-qemu-inprocess/_build && cd build-qemu-inprocess/_build
cmake .. -DQEMU_INPROCESS_REAL_CORE=OFF
cmake --build . && ctest --output-on-failure

# 2) Real core (requires QEMU build deps + target toolchain, e.g. OHOS NDK):
#    a) build libqemu-inprocess-<arch>.a from qemu-source (see recipe below)
#    b) link it:
cmake .. -DQEMU_INPROCESS_REAL_CORE=ON \
         -DQEMU_INPROCESS_CORE_LIB=/path/to/libqemu-inprocess-aarch64.a \
         -DQEMU_INPROCESS_CORE_LINK_LIBS='...QEMU external dependency list...'
cmake --build . && ctest --output-on-failure
```

The real main-loop harness is disabled unless
`-DQEMU_INPROCESS_RUN_REAL_VM_TESTS=ON` is explicitly supplied, because that
test starts a virtual machine. Merely selecting `REAL_CORE` performs build/link
coverage without silently violating a host's VM-start policy.

### Building the real target archive from qemu-source

```bash
cd qemu-source
./configure --target-list=aarch64-softmmu \
            --disable-docs --disable-tools --disable-user \
            --disable-plugins --disable-guest-agent
ninja -C build libqemu-inprocess-aarch64.a
ninja -C build qemu-inprocess-linkcheck-aarch64
```

The integration is in QEMU's root `meson.build`, inside its per-target loop.
The archive therefore contains the exact common/system/target object set used
by `qemu-system-<arch>`, plus `qemu_inprocess.c`, and deliberately omits
`system/main.c`. The linkcheck resolves QEMU's complete dependency graph but
does not initialise QEMU or start a VM. On macOS 27 SDKs, configure with
`--disable-pvg` because QEMU 11.1.50's optional Apple PVG backend uses APIs
removed by that SDK.

Requirements: `meson`, `ninja`, `pkg-config`, `glib-2.0`, `pixman-1`, a C
toolchain, and — for the actual product — the **HarmonyOS NDK / OHOS sysroot**.

### Static scan (no subprocess APIs)

Configure-time, a comment/string-aware scanner checks the wrapper + core entry for
`QProcess fork( vfork( execl* execv* system( popen startDetached posix_spawn`.
If any appears, configuration fails on purpose.

## Verification plan

| Check | Status in this environment |
|---|---|
| Static scan: no process-spawn APIs in runtime + Harmony adapter | **config-time enforced** (6 files, 0 hits) |
| Wrapper logic tests (policy, single-instance, reentrant stop/destroy, concurrent stop, init-failure) | **runnable against the mock** (see `.qemu-inprocess-blocked`) |
| Real QEMU aarch64 core archive | **PASS on macOS arm64** — 112 MB archive, real QEMU 11.1.50 target objects |
| Real-core dependency linkcheck (no VM start) | **PASS on macOS arm64**, exit 0 |
| Wrapper mock tests | **PASS**, normal + ASan + TSan; 2/2 CTest each |
| Same-PID QEMU main-loop run | **NOT RUN** — no non-Debian VM was started |
| HarmonyOS device build/run | **BLOCKED** — OHOS NDK/device unavailable |

The same-PID end-to-end harness (`test_real_core_same_pid_harness`, compiled
only with both `QEMU_INPROCESS_REAL_CORE` and
`QEMU_INPROCESS_RUN_REAL_VM_TESTS`) is the definitive proof that QEMU executes
inside the application process rather than as a child. It is **not** run here.

## Remaining blocked status

See `build-qemu-inprocess/.qemu-inprocess-blocked` for the authoritative,
machine-readable record. Summary of blockers:

1. **No HarmonyOS NDK / OHOS sysroot/device:** the actual product target cannot
   yet be cross-compiled or exercised on HarmonyOS.
2. **`qemu_init()` has process-terminating failure paths:** upstream QEMU's
   option/configuration code contains direct `exit()` and `error_fatal` paths.
   An exit notifier does not intercept `exit()`. The public wrapper must not be
   declared release-ready until those paths are converted to an embeddable
   error boundary or all accepted configurations are proven safe.
3. **Frontend callbacks are a contract, not yet QEMU devices/backends:** the C
   ABI stores display/input/audio/block callbacks, but real QEMU frontend glue
   has not yet been registered to invoke them.
4. **Product Harmony adapter is fail-closed, not yet connected:** its former
   external-QEMU probe/start path has been removed, so it cannot violate the
   no-child-process rule. It now reports unavailable until
   `QemuInProcessRuntime` and the target archive are safely linked.

Consequence: compilation and dependency linkage are now demonstrated, but an
actual QEMU main-loop run, Harmony product integration, and frontend delivery
are not. `.qemu-inprocess-ready` is deliberately **not** written.

## File inventory (this work)

- `qemu-source/qemu-inprocess/qemu_inprocess.h` — C ABI contract
- `qemu-source/qemu-inprocess/qemu_inprocess.c` — real in-process core impl
- `qemu-source/meson.build` — per-target real-core archive + linkcheck targets
- `qemu-source/qemu-inprocess/linkcheck.c` — no-VM dependency/link smoke test
- `src/QemuInProcessRuntime.hpp` / `.cpp` — C++ wrapper
- `tests/QemuInProcessRuntimeTests.cpp` — test suite
- `tests/qemu_inprocess_mock.c` — transparent contract mock (NOT QEMU)
- `build-qemu-inprocess/CMakeLists.txt` — independent build + static scan
- `docs/qemu-inprocess.md` — this document
- `build-qemu-inprocess/.qemu-inprocess-blocked` — honest blocked record
