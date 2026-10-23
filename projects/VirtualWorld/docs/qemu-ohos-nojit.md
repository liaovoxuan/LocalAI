# QEMU no-JIT (TCI) variant for HarmonyOS/HarmonyPC — status and evidence

Status: **BLOCKED** (2026-10-18).  The READY written on 2026-10-17 was **withdrawn** after an
observed intermittent teardown abort in the lifecycle acceptance (1 SIGABRT in 44 host runs:
`qemu_mutex_destroy` `assert(mutex->initialized)` from `migration_incoming_state_release`, plus a
SIGSEGV in `object_resolve_partial_path`/`fw_cfg_find()` under freed-memory poisoning).  Active
verdict: `build-qemu-nojit/.qemu-ohos-nojit-blocked`; withdrawn READY:
`.qemu-ohos-nojit-ready.withdrawn-2026-10-18T141633`; old blocked report kept as
`.qemu-ohos-nojit-blocked.superseded-2026-10-17`.  The static build conclusions below remain valid;
the runtime-lifecycle gate (zero test failures) is NOT met.  OHOS runtime verification remains
SKIPPED (no target attached).  Only `qemu-ohos-source/**`, `build-qemu-nojit/**` and this document
were changed; `qemu-source/**` is pristine and `build-qemu-inprocess/.qemu-core-embeddable-ready`
was not modified (see `build-qemu-nojit/evidence/inprocess-ready-audit.md` for the read-only audit
recommending its owner re-measures it against this defect family).

## What the variant is

An opt-in configuration (`vw_enable_qemu_ohos_nojit`, default **false**, requires
`vw_enable_qemu_ohos` and QEMU's upstream TCG interpreter) of the in-process core
that contains **no native TCG code generator** and never creates or switches to
executable memory.  Default builds and the native TCG JIT path are unchanged.

Three-layer proof:

1. **Compile configuration** — `CONFIG_TCG_INTERPRETER`; only `-iquote .../tcg/tci`
   on the include path (no `tcg/aarch64`); zero native-codegen symbols; TCI symbols
   present; `own` guard object `ohos/vw_nojit_guard.c` compiled only when the option
   is on.
2. **Link closure** — host harness and the OHOS AArch64 lifecycle binary contain
   zero `fork/exec*/posix_spawn/system/popen` symbols (the OHOS closure replaces the
   four non-core objects with structured stubs, plus a `tap_get_fd()` stub).
3. **Runtime** — every lifecycle round reports
   `execmem_requests=0 anon_exec_regions=0` from the allocator guard in
   `qemu_mprotect_rwx()` plus a Mach anonymous-executable-region walk.  Any
   executable mapping fails the run.

## Measured gates (host-built no-JIT variant, re-verified with the final sources)

- battery + 100 same-PID rounds: `failures=0 valid_inits=100 clean_stops=100
  retained=0`, `live_qom=10`, RSS drift **352 KiB** (gate 24 MiB).
- ASan+UBSan 100 rounds: **0 reports**, `failures=0`, drift 2 096 KiB.
- default-OFF regression: both options `NO` in the default configure, guard absent
  from `build.ninja`, changed files compile under the default-OFF configuration,
  pristine native macOS QMP smoke exit 0.
- OHOS AArch64 artifacts (real DevEco toolchain + sysroot + static OHOS deps):
  `libqemu-inprocess-aarch64.a`, `libqemu-aarch64-softmmu.a`, `libqemuutil.a`
  (ELF64 / Machine AArch64) and the full lifecycle binary
  `qemu-inprocess-lifecycle-aarch64` (104,139,160 B, sha256 6debe250110af0798d167efcde7607565e0fa4646ef60896ca74b1ac7e65d759, ELF64 AArch64, NEEDED libz.so/libc.so), with 0
  native-codegen and 0 spawn symbols in its link closure.

## Residual risk: intermittent teardown abort (recorded)

During re-verification with the final sources one host acceptance run aborted with
`Assertion failed: (mutex->initialized), function qemu_mutex_destroy,
qemu-thread-posix.c:96` immediately after the battery entry-8 cleanup; the following batches were
clean (15/15 full 100-round runs and 20/20 short runs, 43/44 runs in total).  This is the same
host-side teardown-race family already documented for the in-process core and does not invalidate
the static/build conclusions, but the "no host termination" property is not proven for every run.
Evidence: `evidence/ohos-runtime-abort-observation.md`.

## OHOS runtime verification: SKIPPED — no target attached (2026-10-17)

Read-only environment detection found no running emulator/device and `hdc list targets` returned
`[Empty]`; host memory was fine (free 56-57%, swap 958 MiB / 2048 MiB).  No emulator was started,
so the runtime phase is SKIPPED and **no OHOS-runtime claim is made**.  Reproduce on a connected
AArch64 target with `build-qemu-nojit/verify-on-ohos-target.sh --rounds 100`: it pushes the
lifecycle binary to `/data/local/tmp/vw-nojit-test`, runs the malformed battery + 100 same-PID
cycles, and requires `failures=0`, `retained=0`, `execmem_requests=0` and `anon_exec=0` from the
mapping audit before printing `OHOS-RUNTIME-VERIFIED: PASS`.

The guard now parses `/proc/self/maps` on OHOS each round and prints
`[nojit] maps: exec_total=N file_backed_rx=N anon_exec=N vdso=N`: file-backed RX is the program's
own code, while anonymous executable mappings (JIT / RWX / RW->RX) must be 0.  Evidence:
`evidence/ohos-runtime-verification-SKIPPED.md`.  No `.qemu-ohos-nojit-ohos-runtime-verified`
marker is created.

## OHOS runtime: NOT VERIFIED (historical wording, explicitly not claimed)

The macOS host cannot execute AArch64 OHOS binaries.  The run-time exec-memory
audit is measured on the host-built no-JIT variant, which shares the same TCI
configuration, guard source and harness.  The OHOS artifacts are verified
statically only (ELF machine, nm/objdump link closure, compile-time absence of the
native code generator).  No OHOS device, emulator, DevEco or HAP was used.

## Open defect: intermittent teardown corruption (root cause not proven yet)

Captured failure evidence (`build-qemu-nojit/evidence/ohos-runtime-abort-observation.md`):

- **Signature A (SIGSEGV)**: `object_resolve_partial_path` recursion -> `object_resolve_path_type`
  -> `fw_cfg_common_realize`/`fw_cfg_find` during battery entry-8 init.  `fw_cfg_find()` resolves
  TYPE_FW_CFG from the QOM root, so a child property still points at an object freed by an earlier
  failed-init teardown (dangling pointer in the process-lifetime QOM tree).
- **Signature B (SIGABRT)**: `qemu_mutex_destroy` `assert(mutex->initialized)` reached from
  `migration_incoming_state_release` via `qemu_cleanup`; the migration integrity probe shows
  `ms->rp_state.rp_sem.mutex.initialized == 48` (invalid) already at init-return and cleanup entry,
  i.e. the process-lifetime MigrationState memory was overwritten before cleanup.
- Both are freed/overwritten-memory reads in process-lifetime structures on the cycle after the 8
  failed-init teardowns (battery entry 8 is the first successful init).  Not TCI/no-JIT/OHOS-specific.
- Reproducer: `MallocScribble=1 MallocGuardEdges=1 QEMU_INPROCESS_ROUNDS=1` on the host no-JIT
  harness reproduces in ~1/7-1/25 short runs (two signatures); without poisoning the rate is low
  (~1/232 across this investigation), which is why it appeared intermittently.

Diagnostics added (env-gated, inert by default, no normal-path behaviour change):
`VW_TRACE_MUTEX` (mutex creation-site registry + symbol + backtrace + migration snapshot on bad
destroy), `VW_TRACE_QOM` (live-object check for every child property during path resolution),
`VW_VALIDATE_QOM` (QOM root child-tree validation after each battery entry),
`VW_BT_ON_CRASH` now covers SIGSEGV/SIGFPE, plus the existing focused
`VW_STRESS_BATTERY_IDX`/`VW_STRESS_BATTERY_REPS` reproducer.

Restoring READY requires: root-cause and fix the dangling reference producer (no skipped
destructors, no disabled tests, no swallowed asserts, no leaked objects, no global default-path
change), then on ONE new build: battery + 100 rounds repeated >=5x, ASan/UBSan 100 rounds,
RSS/retained/live_qom gates, no execmem/no spawn symbols, default-OFF regression - zero failures.

## Root-cause progress (round 2, 2026-10-18)

Object-lifecycle tracing (stable ids, parent-link/unparent/free with backtrace and battery-entry
stage, per-segment liveness check in `object_resolve_abs_path`) and a MigrationState watch probe
(`VW_TRACE_RPSEM`) were added; details in `build-qemu-nojit/evidence/qom-lifecycle-tracing-notes.md`.
The watch probe localises the corruption window: `ms->rp_state.rp_sem.mutex` bytes are consistent at
`phase0b[8] init` and already overwritten at `phase0b[8] cleanup+destroy`
(`evidence/rpsem-mutation-window.log`), so the overwrite happens **during the successful init that
follows the 8 failed-init cycles**, not in the failed-init teardown.  The two crash signatures (QOM
dead-child read; MigrationState field overwrite) are kept separate and are not attributed to one root
cause without evidence.  Producer not yet identified; no fix applied; BLOCKED remains, and the
instrumentation itself hides the race (80 probe-heavy poisoned runs were clean), so the next probe
must be low-overhead (phase subset or an mprotect write guard).

## Round-3 write-capture attempts (2026-10-18)
A shadow mutation detector over the per-init-phase probes found no mutation in 150 poisoned short runs
(instrumentation overhead hides the race).  An mprotect-based page write guard with an
async-signal-safe handler (illegal-write / legitimate-same-page / other-fault discrimination) was
implemented inside `qemu-ohos-source/` and is env-gated (`VW_GUARD_MS`), but needs robust
arm/disarm on every init exit path before it can capture the writer.  Producer not identified; no fix
applied; BLOCKED remains.  Details: `build-qemu-nojit/evidence/write-capture-attempts.md`.

## Round-4 write-guard hardening (2026-10-18)
Guard v2 now (a) disarms on every stage transition and every init trap/error return, and (b) emulates
same-page legitimate stores (AArch64 STR family) so the page is re-protected and monitoring continues
over them; undecodable same-page stores fail the run.  The synthetic sequence "same-page store ->
watched store" is provably caught (`evidence/guard-selftest.log`).  On the real mixed-battery workload
the guard itself makes runs hang right after arming (no fault logged), so the probe was stopped rather
than accumulating reproductions; it stays env-gated and inert by default.  Producer not identified, no
fix applied, BLOCKED remains.  Details: `evidence/write-capture-attempts.md`.

## Performance (lifecycle only — not Debian performance)

Guestless 20-round lifecycle on this host: no-JIT (TCI) 0.16 s vs native TCG JIT
0.72 s.  No guest or OS was booted, so these numbers must not be used to claim
Debian guest performance.

## OHOS toolchain facts (correction)

The earlier blocked report claimed no OHOS toolchain; that was wrong.  The real
toolchain and dependencies exist and were used:

- `/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/native/llvm/bin/aarch64-unknown-linux-ohos-clang` (clang 15.0.4)
- `/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/native/sysroot`
- `projects/VirtualWorld/build-qemu-ohos-deps/prefix` (glib/gio/libffi/pcre2/intl static, ELF64 AArch64)

The failure of a bare `clang --target=aarch64-linux-ohos` call only reflected a
missing `--sysroot`; commands are recorded in
`build-qemu-nojit/evidence/ohos-build-config-and-fixes.txt`.

## OHOS portability fixes (inside qemu-ohos-source/, default builds unaffected)

1. `hw/arm/fsl-imx8mm.c`, `fsl-imx8mp.c` — function-scope `static const` tables
   initialized from a static const memmap element are not C constant expressions;
   clang 15 rejects them.  Fix: drop `static` (automatic const table).
2. `qemu-inprocess/lifecycle_harness.c` — macOS-only Mach diagnostics guarded with
   `__APPLE__`, procfs fallbacks for RSS/VM/thread counts on OHOS/Linux.
3. `ohos/vw_embedded_stubs.c` — added `tap_get_fd()` stub.
4. OHOS configure: `-Dtcg_interpreter=true -Dvw_enable_qemu_ohos=true
   -Dvw_enable_qemu_ohos_nojit=true -Dpixman=disabled -Dplugins=false
   -Dkeyring=disabled -Dvhost_user=disabled -Ddbus_display=disabled`,
   `--extra-cflags="-DCONFIG_VW_OHOS_NOJIT --sysroot=<sdk sysroot>"`,
   `PKG_CONFIG_LIBDIR=<deps prefix>/lib/pkgconfig:build-qemu-nojit/ohos-pkgconfig`.
