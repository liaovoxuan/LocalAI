# HarmonyOS / HarmonyPC no-JIT runtime prototype

**Status: isolated feasibility prototype; not a product runtime, not a QEMU build, and not ready to integrate or release.**

This experiment measures two separate ideas: (1) sparse guest-physical RAM to avoid committing the full configured RAM size, and (2) an interpreter that executes a small AArch64 subset without generating executable code. It does not modify QEMU, the HarmonyOS HAP, or any platform product code.

## 1. What is implemented

### Sparse guest RAM (`experiments/harmony-nojit/src/guestmem.c`)

- Fixed logical guest-physical address space; 8 GiB is exercised by the host test.
- Preferred mode reserves a `MAP_NORESERVE` virtual range and relies on first-touch physical commitment; no full-RAM `memset`, `mlock`, or eager physical commitment.
- If a large reservation is unavailable, a two-level table lazily allocates 2 MiB chunks.
- Bounds-checked guest reads/writes/maps; committed-chunk, dirty-page and OOB counters; discard/reclaim; a reserved free-page/balloon reporting callback.
- The module installs no signal handlers. The signal-safe statistics path only reads atomics; allocation and locking happen in normal context.
- Reclaim behavior is runtime-probed. On this macOS host `MADV_DONTNEED` did not prove effective, so the test used `MAP_FIXED` re-anonymous mapping instead. This is host evidence, not a HarmonyOS guarantee.

### No-JIT CPU prototype (`experiments/harmony-nojit/src/nojit_cpu.c`)

- A compact micro-op interpreter with portable `switch`, GNU computed-goto when supported, and a superinstruction engine.
- Direct-mapped basic-block decode cache and software TLB. After guest RAM is discarded/reclaimed, the embedding layer must call `nj_tlb_flush()` before reusing the CPU.
- The decoder currently accepts only this **64-bit AArch64 instruction subset**:
  - `MOVZ`, `MOVN` (not `MOVK`);
  - `ADD/ADDS/SUB/SUBS` immediate and shifted-register forms (`LSL #0` only);
  - `AND/ORR/EOR` shifted-register forms (`N=0`, `LSL #0` only);
  - `MUL` (`MADD` with `XZR`), `LSLV`, `LSRV`, `ASRV`;
  - 64-bit `LDR/STR` unsigned-offset and `LDUR/STUR` unscaled;
  - `NOP`, `B`, `B.cond`, `CBZ`, `CBNZ`, `RET`.
- Unsupported encodings and invalid guest memory accesses trap. This is not a complete AArch64 CPU: no MMU/EL, exception model, atomics/exclusives, FP/SIMD, system registers, interrupts, devices, firmware, or operating-system boot path is implemented.
- The superinstructions fuse selected `LOAD+ADD` and `ADD+STORE` pairs while preserving the interpreter's register/memory semantics. Fusion is not guaranteed to improve every workload.

## 2. Host validation and measurements

All execution measurements below are from **native macOS ARM64**, not HarmonyOS. They are short synthetic microbenchmarks and should not be extrapolated to a full guest OS or treated as target-device performance numbers.

### Sparse RAM

`tests/test_guestmem.c`: **83 checks, 0 failures**.

- 8 GiB logical address space; 64 MiB touched. RSS went from 1,687,552 to 68,861,952 bytes (delta **67,174,400 bytes**), rather than approaching 8 GiB.
- A subsequent random-write phase reported 160 committed 2 MiB chunks.
- Discard/reclaim reduced reported RSS from 336,822,272 to 1,753,088 bytes and committed chunks from 160 to 0. The tested path was `MAP_FIXED` re-anon because the macOS `madvise` probe did not demonstrate effective discard.
- Also exercised bounds failures, concurrent disjoint writers, chunk fallback, balloon-report callback, and atomic statistics.

### Interpreter

`tests/test_interp.c`: **109 checks, 0 failures** in both computed-goto and forced switch-only builds. AddressSanitizer and UndefinedBehaviorSanitizer build: **109 checks, 0 failures**.

Coverage includes three-engine determinism, array summation and memory output, `NZCV`/condition branches and branch fall-through, `MOVN/AND/MUL/SUB`, `RET`, cache reuse across reset, software-TLB hit/invalidation after reclaim, cross-chunk 64-bit access, unsupported-op traps, and guest OOB traps. The repeated-store workload's observed RSS increase was **16,777,216 bytes** (about 16 MiB); the test asserts total RSS remains below 1.5 GiB.

One native optimized run produced these approximate rates; each row is a different synthetic program:

| Workload | Switch | Computed-goto | Superinstruction |
|---|---:|---:|---:|
| Array sum / loads | 356 Mguest-instr/s | 208 Mguest-instr/s | 237 Mguest-instr/s |
| Repeated stores / conditional loop | 469 Mguest-instr/s | 567 Mguest-instr/s | 356 Mguest-instr/s |
| Pure ALU loop | 884 Mguest-instr/s | 693 Mguest-instr/s | 457 Mguest-instr/s |

These single-run timings are noisy and the workloads are tiny, hand-authored instruction streams. They show that dispatch/fusion trade-offs are workload-dependent—not that the prototype can boot Debian, run applications at those rates, or approach native execution. In particular, superinstructions were slower on two of the three samples.

## 3. OHOS cross-compilation evidence

The DevEco Native SDK clang wrapper (`aarch64-unknown-linux-ohos-clang`, clang 15.0.4) compiled `guestmem.c`, `nojit_cpu.c`, and `test_interp.c` as relocatable objects with `-Wall -Wextra -Werror`. `file` and `llvm-readelf -h` identify the outputs as **ELF64, little-endian, AArch64, relocatable**.

The objects were **not executed**, linked into a HAP, or tested on a HarmonyOS/HarmonyPC device. Cross-compilation only verifies source/toolchain compatibility; it does not establish runtime availability or permission behavior for large virtual mappings, `MAP_NORESERVE`, `mmap`, reclaim, RSS accounting, pthreads, or the HAP sandbox.

A static scan of 8 C/header files found **zero code references** to process spawn/exec APIs, `QProcess`, executable-memory permissions/APIs (`PROT_EXEC`, JIT mapping, `mprotect`, `vm_protect`), or generated-code cache flushing. The scanner removes comments and string literals before checking code tokens.

## 4. Feasibility conclusion

- **Sparse-RAM mechanism:** promising on the measured macOS host for keeping resident memory proportional to touched pages. HarmonyOS virtual-address limits, commit accounting and reclaim semantics remain unverified.
- **No-JIT interpreter mechanics:** a small AArch64 subset can execute deterministic workloads correctly under the native tests and cross-compile to OHOS ARM64 objects. The measured dispatch results are mixed; there is no credible target performance estimate yet.
- **“Usable Debian ARM64 light workload” target:** **not established**. A bootable system would require a much broader CPU/exception/MMU model, devices, interrupt/timer behavior, firmware and OS integration. The prototype does not boot any guest.
- **x86 guest compatibility:** not implemented. Supporting x86 would require a separate decoder/interpreter; no x86 performance claim is made, and an interpreter-only path should be expected to be slower than native execution.
- **QEMU/HAP integration:** deliberately untouched. Sparse RAM can reduce host resident-memory use for untouched guest pages; it does **not** improve CPU execution speed or solve QEMU TCI throughput. RAM occupancy and TCI speed are separate bottlenecks. Future integration would need a QEMU-native RAM backing and a separate CPU execution decision; neither is grafted into the current QEMU/HAP line.
- **HarmonyOS APIs and permissions:** require a device-side probe before any product decision. No AGC permission or platform API is assumed here.

## 5. First-pass RAM compatibility review (superseded; qemu-master 11.0.93 reference only)

**Decision: do not bridge the current `guestmem` object directly into a QEMU RAMBlock.** This review found a conditional pointer-model match but not a safe, policy-compliant bridge with the current API. No QEMU source, public product layer, or platform integration file was modified.

This was the earlier, superseded API-shape review of repository copy `qemu-master/` (`VERSION` **11.0.93**). Its findings are retained as historical reference only; see the next section for the actual `qemu-ohos-source/` 11.1.50 source audit. The old reference should not be treated as the final target-revision finding.

### Why the existing model is not a drop-in RAMBlock

- QEMU's `RAMBlock` has one `host` base plus `used_length` / `max_length`; normal RAM offset resolution is `host + offset` (`qemu-master/include/system/ramblock.h:25-47,121-130`). The `GM_MODE_CHUNK` fallback stores separately `mmap`'d 2 MiB buffers (`experiments/harmony-nojit/src/guestmem.c:77-98,156-163`). It cannot supply a single host base for that arithmetic. Splitting RAM into many QEMU regions would alter guest RAM topology and is not a transparent bridge.
- The reserve mode keeps a continuous virtual mapping, but the current API does not expose a retained backing base or a pin/lease that prevents `gm_discard()` / `gm_destroy()` from invalidating it (`guestmem.c:121-167,327-340`; `guestmem.h:81-112`). `MAP_NORESERVE` is only a virtual reservation: a successful map does not prove physical capacity for later first touches. Returning that success as proof that the full `-m` allocation passed host-memory admission would violate the no-fake-capacity/no-bypassed-check requirement. Actual first-touch allocation failure can occur after initialization.
- Direct pointer access bypasses `gm_write()` accounting: that function sets the prototype dirty bits before copying (`guestmem.c:258-299`), but a QEMU/TCG/DMA store through a RAMBlock pointer does not call it. The current per-prototype dirty count therefore cannot be QEMU's migration bitmap. In the reference tree, migration maintains `RAMBlock::bmap`, initializes it dirty for all pages, and exposes per-client snapshot-and-clear dirty APIs (`qemu-master/migration/ram.c:2832-2849`; `qemu-master/include/system/memory.h:1880-1904`). QEMU must own dirty logging and snapshot-and-clear behavior.
- RAM pointer initialization is direct host-memory access; its API documentation states that migration registration is the caller's responsibility (`qemu-master/include/system/memory.h:1235-1253`; implementation `system/memory.c:1682-1689`). `qemu_ram_alloc_from_ptr()` tags the backing as preallocated, and the RAMBlock teardown does not release that external allocation (`system/physmem.c:2546-2550,2569-2590`). Any future bridge must keep the backing alive until the MemoryRegion/RAMBlock is fully destroyed.
- DMA must retain `AddressSpace` transaction, IOMMU/error, and paired map/unmap semantics. `dma_memory_map()` may return a shorter range or fail, and `dma_memory_unmap()` performs dirty notification for device writes (`qemu-master/include/system/dma.h:192-237`). Concrete examples: AHCI rejects a short PRDT mapping before treating it as an array (`hw/ide/ahci.c:931-943`); Virtio's remap path reports an unexpected split (`hw/virtio/virtio.c:1690-1705`). A chunk-per-region scheme can make cross-boundary requests short/split and cannot be assumed compatible.
- RAM migration is block/page based and reads/writes backing by block offset (`qemu-master/migration/ram.c:1266-1285,1303-1310,4351-4384`). A host pointer alone does not register a savevm section or preserve RAMBlock identity.

The current in-process runtime supplies system memory through QEMU's `-m` argument (`src/QemuInProcessRuntime.cpp:195-205`); it does not currently install a guestmem bridge. The QEMU reference also defines `RAM_NORESERVE` to skip swap/huge-page reservation (`qemu-master/include/system/memory.h:245-250`). Do not enable it merely to make a large configured RAM size appear to fit: it weakens host allocation guarantees and is not a substitute for a policy-approved capacity check.

### Minimum safe alternative

1. **For this route, leave guest RAM allocation to QEMU's existing `-m` / RAMBlock allocator.** Keep the configured guest size and normal host allocation/error path honest; do not attach `GM_MODE_CHUNK` as one block and do not treat a `MAP_NORESERVE` VA reservation as committed host RAM.
2. If sparse backing is reconsidered, implement it inside the QEMU RAM allocator for the exact target revision: one stable contiguous host VA per RAMBlock, explicit host admission/overcommit policy (not a fake capacity success), QEMU-owned DMA and dirty tracking, RAM migration registration, and destruction only after all QEMU mappings/references are gone. If those guarantees cannot be met, retain QEMU's native allocation.
3. Do not split RAM into 2 MiB MemoryRegions as the quick fix. That requires a full audit of cross-region DMA, device mapping lengths, RAMBlock identity, snapshot/migration and dirty logging; it is a materially larger QEMU change.

### Device/test evidence status for this review

- **macOS native tests:** `test_guestmem.c` and `test_interp.c` are host tests only; see the counts/results in `experiments/harmony-nojit/build/nojit-validation-evidence.txt`. They do not test QEMU bridge behavior.
- **OHOS cross-compilation:** the earlier clang run produced AArch64 relocatable objects only; it is compile evidence, not a linked HAP or device execution.
- **OHOS device execution:** `hdc list targets` returned an empty target list on 2026-10-23. No probe was run; there was no connected device, and no simulator was started.
- **Bridge-specific lifetime / OOM / dirty-bitmap tests:** not run because the compatibility review rejected a bridge under the current constraints. No success is inferred for those behaviors. In particular, prototype `gm_stats().dirty_pages` is not a QEMU dirty-log test.

## 6. Actual target-source review: qemu-ohos-source 11.1.50

**Status: read-only source audit; conclusions may be stale. No bridge implementation, build, emulator, or guest was run.** The separate detailed audit and file snapshot are at `experiments/harmony-nojit/build/qemu-ohos-ram-target-review-2026-10-23.txt` and `qemu-ohos-source-audit-snapshot-2026-10-23.txt`.

### Version, configuration, and diff basis

- Actual target: `qemu-ohos-source/VERSION` = **11.1.50**; base `qemu-source/VERSION` = **11.1.50**. Both Git trees report `HEAD=d49f87606ac1a6e15701b26c1b16c5d7e948ffcb`. `qemu-master/` and the earlier reference are **11.0.93**, not the integration target.
- `qemu-ohos-source` had **62 tracked modified QEMU files** plus untracked `ohos/`, `qemu-inprocess/`, manifest and helper header at audit time; `qemu-source` was clean. Source docs describe `vw_enable_qemu_ohos` default OFF and `vw_enable_qemu_ohos_nojit` default OFF; TCI is a separate Meson option default OFF. These are source defaults, **not proof of active generated config**. The target build directory only contains `build/config.log` from a `configure --help` call on 2026-10-11; no Meson `coredata.dat`, `config-host.h` or `build.ninja` was found, so actual host/target/configuration is unknown.
- Audited RAMBlock, MemoryRegion, DMA, mmap allocator, hostmem backend, AHCI/Virtio and TCI files mostly have matching hashes in `qemu-source` / `qemu-master`. Target-only patches are significant in lifecycle-owned files, including `system/physmem.c`, `system/memory.c`, `system/runstate.c`, `migration/ram.c`, `migration/savevm.c`, `include/system/memory.h`, `include/system/ramblock.h`, `hw/core/machine.c`, and `system/vl.c`. The material difference for this decision is the in-process RAM lifecycle/reuse policy; the single-host-pointer RAMBlock model and core DMA contracts remain.

### Actual target findings

- **RAMBlock host pointer:** `qemu-ohos-source/include/system/ramblock.h:25-47` stores one `host` pointer and migration bitmap; `:121-130` implements `ramblock_ptr()` as `host + offset`. Independent 2 MiB `GM_MODE_CHUNK` mappings are incompatible with one target RAMBlock. Reserve mode resembles the contiguous pointer shape but is not currently exposed as a leased/pinned guestmem base.
- **`-m` / allocation:** `src/QemuInProcessRuntime.cpp:195-205` constructs `-m`; target `system/vl.c:3566-3571` parses it. `hw/core/machine.c:1026-1056,1632-1672` creates and validates the default backend; `backends/hostmem.c:285-296` defaults `reserve=true`; `backends/hostmem-ram.c:30-36` only applies `RAM_NORESERVE` when reserve is off. Allocation reaches `system/physmem.c:2203-2238` and `qemu_anon_ram_alloc()` via `util/oslib-posix.c:208-235` / `util/mmap-alloc.c:247-294`; allocation/mapping failure is surfaced as failure. This is the target's actual QEMU path and should stay authoritative.
- **Real capacity vs virtual reservation:** `RAM_NORESERVE` skips swap/huge-page reservation (`include/system/memory.h:245-250`). Target `util/mmap-alloc.c:85-145,184-208` rejects cases where no-reserve is ineffective/unsupported. Even a successful contiguous mapping is not a proof that every future physical page can be touched. Do not report a sparse VA reserve as physical-memory admission or automatically switch off reservation.
- **MemoryRegion/AddressSpace and direct maps:** `memory_region_init_ram_ptr()` documents direct mutation and caller-owned migration responsibility (`include/system/memory.h:1235-1253`); actual code routes it through preallocated `qemu_ram_alloc_from_ptr()` (`system/memory.c:1704-1720`). `AddressSpace` map/unmap is FlatView-based (`system/physmem.c:3873-3974`). No target chunk-indirection hook exists.
- **DMA:** `include/system/dma.h:192-237` allows a short/failed map and pairs it with unmap/dirty notification. `system/physmem.c:3873-3941` implements those operations. AHCI's short PRDT check is at `hw/ide/ahci.c:931-943`; Virtio's remap split rejection is at `hw/virtio/virtio.c:1690-1705`. Per-chunk MemoryRegions or direct guestmem access cannot be assumed to preserve IOMMU, bus errors, short-range handling or dirty notification.
- **Dirty pages and snapshots/migration:** QEMU tracks dirtiness in `RAMBlock::bmap`; migration setup initializes the block bitmap dirty (`migration/ram.c:2832-2849`), then saves pages via `block->host + offset` (`:1214-1217,1310-1314`) and loads by RAMBlock offset (`:3569,3754`). RAM registration is `memory_region_register_ram()` → `vmstate_register_ram()` (`system/memory.c:3703-3726`, `migration/savevm.c:3593-3619`). Guestmem's `gm_write()` dirty counter cannot see TCG/DMA stores through direct pointers and is not a substitute.
- **Destruction across in-process cycles:** normal RAM-region destructor calls `qemu_ram_free()` unless `qemu_inprocess_keep_ram` is set (`system/memory.c:1206-1214`). `qemu_ram_free()` defers final backing reclamation via RCU, and preallocated backing is not freed by QEMU (`system/physmem.c:2623-2669`). Target teardown sets keep-RAM true (`system/runstate.c:1337-1340`); `machine_memory_cleanup()` empties memory wiring but explicitly documents an open issue about RAMBlocks while the machine tree is alive (`system/physmem.c:3716-3774`). `system/runstate.c:1413-1567` later disconnects regions, drains RCU and finalizes the tree. These target-only lifetimes mean any external backing would need a cross-cycle ownership/lease protocol absent from guestmem.
- **No-JIT route:** target `meson_options.txt:98-99` labels upstream `tcg_interpreter` “slow”; `tcg/meson.build:18-23` includes `tci.c`; `meson.build:923-940` selects it. `vw_enable_qemu_ohos_nojit` is separately default OFF and requires the interpreter (`meson_options.txt:399-406`, `meson.build:4128-4135`). This is the in-repository no-native-JIT execution path worth evaluating behind its opt-in/no-spawn gate, **not** a fix for TCI speed or for RAMBlock compatibility. The absent generated configuration means not proven enabled in the target build.

### Actual target recommendation

**Yes: retaining QEMU's native `-m` allocation remains the safest current option.** It preserves QEMU's own allocation/error behavior, stable RAMBlock host base, DMA/AddressSpace, dirty bitmap, VMState/migration IDs and in-process RAM lifetime rules. It does not mean QEMU can guarantee physical RAM beyond host policy; retain truthful size and errors. Sparse RAM can at most reduce resident use for untouched guest pages; it cannot guarantee future faults or improve TCI throughput.

Bounded no-JIT/no-child-process opportunities are: use the target's opt-in TCI/no-JIT build and measure it separately; set a truthful lower guest RAM size where workload permits; use guest-cooperative balloon/free-page discard only when guest support and QEMU dirty/snapshot rules remain intact. Do not force `reserve=off` / `RAM_NORESERVE` as a generic workaround. No measured claim is made for HarmonyOS device reclaim behavior.

### Test and freshness status

- Existing macOS small prototype tests only: guest RAM **83 checks / 0 failures**; interpreter **109 checks / 0 failures**. They do not exercise QEMU RAMBlock bridging.
- OHOS cross-compilation is three AArch64 relocatable object outputs; no HAP link/device runtime claim. `hdc list targets` was empty; no simulator/guest started.
- QEMU bridge-only cross-block, mapping lifetime, actual allocator OOM, reclaim/re-touch and dirty-bitmap tests: **not run (0 attempted; failures N/A)** because no bridge was implemented.
- During this review, `qemu-ohos-source/system/vl.c` reported mtime **2026-10-23 19:19:25 +08:00**, later than the user turn's start time. Together with the target tree's many work-in-progress changes, this is evidence the target may be changing concurrently. The report includes file hash/mtime snapshots; **recheck before relying on these conclusions after DeepSeek edits**.

## 7. Reproduction

Run from the VirtualWorld repository root. Native tests do not start a VM or execute a guest OS.

```sh
N=experiments/harmony-nojit
clang -std=c11 -D_POSIX_C_SOURCE=200809L -O2 -Wall -Wextra -Werror -pthread \
  -I "$N/include" "$N/src/guestmem.c" "$N/tests/test_guestmem.c" -o "$N/build/test_guestmem.native"
"$N/build/test_guestmem.native"

clang -std=c11 -D_POSIX_C_SOURCE=200809L -O2 -Wall -Wextra -Werror -pthread \
  -I "$N/include" "$N/src/guestmem.c" "$N/src/nojit_cpu.c" \
  "$N/tests/test_interp.c" -o "$N/build/test_interp.native"
"$N/build/test_interp.native"

clang -std=c11 -D_POSIX_C_SOURCE=200809L -DNJ_NO_COMPUTED_GOTO -O2 \
  -Wall -Wextra -Werror -pthread -I "$N/include" "$N/src/guestmem.c" \
  "$N/src/nojit_cpu.c" "$N/tests/test_interp.c" -o "$N/build/test_interp.fallback"
"$N/build/test_interp.fallback"
python3 "$N/tests/scan_nojit.py"
```

For OHOS compile-only verification, set `OHOS_NDK` to the installed OpenHarmony Native SDK root and invoke its `llvm/bin/aarch64-unknown-linux-ohos-clang` with `-std=c11 -D_POSIX_C_SOURCE=200809L -D_GNU_SOURCE -O1 -Wall -Wextra -Werror -pthread -I "$N/include" -c` on each of `src/guestmem.c`, `src/nojit_cpu.c`, and `tests/test_interp.c`. Do not run the resulting objects on macOS.
