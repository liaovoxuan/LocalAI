# Mini vMac in-process engine

Status: **ready** — see `build-minivmac-inprocess/.minivmac-inprocess-ready`.
The earlier blocked marker is preserved as `.minivmac-inprocess-blocked.initial.json`.

Verified: the engine compiles and links, the lifecycle suite passes with **139
checks / 0 failures** under the normal build, AddressSanitizer and
ThreadSanitizer, and the spawn-API scan is clean.

**No guest was started and no real Macintosh ROM was used.** The synthetic ROMs
in the tests are not Macintosh ROMs and boot nothing; `guest_started` is `false`
and `real_hardware_verified` is `false`.

## Why this exists

VirtualWorld previously shipped prebuilt Mini vMac binaries
(`runtime/system-software/macos/apple-silicon/bin/minivmac-*`) and launched them
as separate processes. Spawning an external executable is forbidden on
HarmonyOS/OpenHarmony, so the emulator has to run **inside** the host process.

## Layout

```
third_party/minivmac/
  upstream/                     upstream Mini vMac source (GPLv2, COPYING.txt kept)
  inprocess/
    cfg/                        headless configuration (no Cocoa/SDL/GTK/Win32)
    vw_minivmac_core.{c,h}      headless platform layer + engine control surface
    build_engine.sh             standalone build (does not touch project CMake)
src/MiniVmacInProcessCore.{hpp,cpp}   stable C++ API (opaque handle)
tests/MiniVmacInProcessCoreTests.cpp  lifecycle / concurrency / containment tests
build-minivmac-inprocess/       build output (not committed)
```

## How the core and the shell are separated

Upstream Mini vMac is a portable emulator core plus a per-platform shell
(`OSGLUCCO.m` for Cocoa, `OSGLUSDL.c` for SDL, …). The core leaves a small,
well-defined set of symbols to the shell; this module supplies them headlessly in
`vw_minivmac_core.c`:

| Area | Provided |
| --- | --- |
| Machine arena | two-pass `ReserveAllocBigBlock` sizing, then placement |
| ROM | caller-supplied bytes copied into the engine's ROM buffer |
| Disks | six in-memory drives (`vSonyTransfer` / `Eject` / `GetSize` / `GetName`) |
| Video | frame published from `Screen_OutputFrame` on `DoneWithDrawingForTick` |
| Audio | scratch buffer + published block counter, no device opened |
| Timing | monotonic clock tick accounting |
| Input | `Keyboard_UpdateKeyMap` (header-local) and `CurMouseH/V` + `Mouse_Update` |
| Parameter buffers | upstream `PBUFSTDC.h` |

`COMOSGLU.h` (upstream, header-only) supplies the remainder of the interface, so
this layer only implements what is genuinely platform-specific.

## Containment

The engine never launches anything. There is no `fork`, `exec*`, `system`,
`popen`, `posix_spawn`, `QProcess` or `startDetached` anywhere in the engine
sources, and no external executable is referenced. Media are attached from
memory, so there is no host file handle to leak either. This is what makes the
module usable where spawning is prohibited.

## API

```cpp
vw::MiniVmacInProcessCore core;
core.create();                       // owns the process-global core
core.loadRom(romBytes, romSize);     // caller-supplied ROM; never distributed
core.attachDisk(0, imgBytes, size);  // in-memory medium
core.setFrameCallback(cb);           // fires exactly once per completed frame
core.start();                        // worker thread runs the emulator
core.setKey(0x24, true);             // input
core.setMouse(x, y);
core.stop();                         // idempotent; joins the worker
```

* **Opaque handle.** The implementation is a pImpl; callers never see the
  emulator's globals.
* **No PID semantics.** Nothing is spawned; `stop()` acts on the core's
  `ForceMacOff` flag rather than on a process.
* **Repeated `stop()`/destruction are safe.** `stop()` is idempotent and the
  destructor joins the worker.
* **Callbacks exactly once.** The frame callback is delivered under a mutex and
  guarded by a sequence counter, so it cannot fire twice for one frame nor after
  `stop()` returns.
* **Single instance, honestly reported.** The upstream core keeps machine state
  in process globals, so only one instance can be live. A second `create()`
  returns `MvStatus::Busy` instead of corrupting the first.

## ROM policy

No ROM or system disk is distributed with the project, and none is used by the
tests. Every test synthesises its own byte pattern in memory. The synthetic ROM
is not a Macintosh ROM and boots nothing, so no real guest is ever started.

## Build and test

```bash
bash third_party/minivmac/inprocess/build_engine.sh
clang++ -std=c++17 -O1 -g -DVW_INPROCESS_TEST_HOOKS \
    -Ithird_party/minivmac/inprocess -Isrc \
    src/MiniVmacInProcessCore.cpp tests/MiniVmacInProcessCoreTests.cpp \
    build-minivmac-inprocess/libvwminivmac-test.a \
    -o build-minivmac-inprocess/minivmac-inprocess-tests
./build-minivmac-inprocess/minivmac-inprocess-tests
```

## Defects found and fixed during bring-up

Each of these was a real defect, confirmed by a repro or a sanitizer before
being fixed. None were worked around.

1. **Concurrent `join()` — the cause of the original `SIGABRT`.** Four threads
   joining one `std::thread` produce
   `std::__1::system_error: thread::join failed: Invalid argument`, which escapes
   and calls `std::terminate`. A mutex/condition-variable state machine
   (`Created`/`Starting`/`Running`/`Stopping`/`Exited`) now guarantees exactly one
   joiner; the rest wait on the same predicate and observe the same result. A
   joinable thread is never overwritten.
   *The first report blamed the synthetic ROM for this; that attribution was
   wrong and is retracted.*
2. **The stop request was swallowed.** `vw_core_run()` cleared `ForceMacOff` on
   entry. If `stop()` fired before the worker was scheduled, the request was
   overwritten and the loop never terminated — `stop()` then waited forever. The
   clear now happens synchronously in `start()`, before the thread exists.
3. **`stopAndJoin()` could never return.** It waited on `!worker_joinable`, a flag
   only the joiner clears. It now waits on the worker's terminal state.
4. **Destructor skipped `vw_core_dispose()`**, so a later instance inherited stale
   state. The order is now `stop`/`join` → `vw_core_dispose()` → reset media,
   timing, counters, `ForceMacOff`, `vw_running`/`vw_prepared` → release
   `g_core_owned`.
5. **Real data races on the control flags.** ThreadSanitizer reported three races
   on `ForceMacOff` and the test gate. Cross-thread signalling now uses
   `_Atomic` flags only; `ForceMacOff` is written *inside the worker thread* from
   `WaitForNextTick()`, so the upstream global is single-threaded by
   construction. **Upstream sources were left byte-identical** — no patch, and no
   validation logic touched.
6. **`loadRom` accepted short images** and silently zero-padded them. It now
   requires exactly `kROM_Size` (`0x20000`).

## Concurrency semantics

* `start()` enters `Starting` synchronously *before* the thread is created, so a
  racing `start()` cannot slip past; an immediate double-start is refused.
* Only one caller ever joins. Concurrent `stop()`, and `stop()` racing
  destruction, are safe.
* The worker's top level catches everything and records an exit code; no
  exception escapes the thread entry.
* The frame callback fires exactly once per completed frame and never after
  `stop()` has returned.
* A worker that is `Exited` but still joinable is reaped before a restart, never
  overwritten.

## Test coverage

22 test groups, 139 checks, 0 failures. Beyond the lifecycle basics:

| Case | What it proves |
| --- | --- |
| `test_second_instance_is_busy_while_first_lives` | a, then b and c all refused with `Busy` — the *correct* result |
| `test_reacquire_only_after_first_is_destroyed` | reacquisition works once the owner is gone |
| `test_reacquire_repeatedly` | 30/30 create/destroy cycles |
| `test_immediate_double_start` | back-to-back starts, before the worker is scheduled |
| `test_restart_after_stop_does_not_overwrite_joinable_thread` | restart never overwrites a joinable `std::thread` |
| `test_stop_storm_eight_threads` | 8 threads stopping at once |
| `test_destroy_races_stop` | destruction racing `stop()` |
| `test_invalid_rom_yields_stable_status` | a synthetic ROM is a controlled outcome, never an abort |
| `test_hold_gate_lifecycle` | worker parked at the test-only gate: same-PID worker, stop and callback safety |
| `test_stop_racing_start_is_not_silently_ignored` | **B1 regression**: 283 start-before-stop rounds, 0 violations |

The `pre-ProgramMain` hold gate is compiled into `libvwminivmac-test.a` only
(`-DVW_INPROCESS_TEST_HOOKS`); the production archive contains no such symbols.
It proves worker/stop/destroy behaviour in one process — **it does not prove that
Mac OS boots.**

## Mouse input

`setMouse(x, y)` positions the pointer. `setMouseButton(down)` presses/releases the
button — Mac 128K/512Ke/Plus have exactly one, so there is no button index.
`setMouseWheel(dx, dy)` follows Mini vMac's own convention (`OSGLUSDL.c`
`SDL_MOUSEWHEEL`): the wheel is emitted as **arrow-key pulses**
(`dx<0` left / `dx>0` right / `dy<0` down / `dy>0` up). Upstream has no wheel concept.

**All input is queued, never applied on the caller's thread.** The queue is
mutex-protected and drained by the worker inside `WaitForNextTick`, because the
upstream entry points (`Keyboard_UpdateKeyMap`, `MyMouseButtonSet`, `Mouse_Update`)
touch device state that the emulation thread owns. Applying them from the caller
thread is a genuine data race — ThreadSanitizer caught exactly that before this
design was adopted.

That the queue is *shared* with the core is not accidental: in the produced archive
`MyEvtQA` and `MyEvtQOutP` are **defined only in the engine's own object file**
(`GLOBGLUE.o` and `MOUSEMDV.o` reference them as undefined), so the core's
`Mouse_Update()` / `FindKeyEvent()` consume the very queue this engine owns.

### Not verified

Input **delivery into a guest is not verified**: no real Macintosh ROM is used, the
synthetic ROM boots nothing, so nothing observes the key/button state. The tests
prove the calls are safe, validated, queue-bounded and do not stall the engine —
they do **not** prove a guest received the input.

## Destruction contract

`~MiniVmacInProcessCore()` stops and joins the worker, so destroying without calling
`stop()` is safe. It is **not** safe to destroy the object while another thread is
calling a member function on it — that is undefined behaviour in C++ (the mutex and
condition variable cease to exist), and no library can paper over it. Callers must
synchronise destruction against concurrent member calls. Repeated and concurrent
`stop()` on a *live* object is fully supported.

## Not verified

* No real Macintosh ROM was used, so no Mac OS behaviour is claimed, and
  `guest_started` is `false`.
* No real guest was started, and no VM/UTM/QEMU was touched.
* `real_hardware_verified` is `false`.
* AddressSanitizer and ThreadSanitizer both pass (124 checks, 0 failures) on
  macOS arm64. LeakSanitizer is unavailable on this platform, so leak detection
  is not part of the sanitizer verdict.

---

## 交接与集成指引（给接收方）

本节是交接契约。引擎已就绪并自证，但**接入公共构建、替换外部可执行文件属于产品改动，不在
本模块作者的文件范围内**，必须由下列接手方完成。

### 产物与复现命令

```bash
# 构建引擎（产出生产归档与测试归档）
bash third_party/minivmac/inprocess/build_engine.sh
#   build-minivmac-inprocess/libvwminivmac.a       生产（不含任何测试钩子）
#   build-minivmac-inprocess/libvwminivmac-test.a  仅测试（+VW_INPROCESS_TEST_HOOKS）

# 自证
clang++ -std=c++17 -O1 -g -DVW_INPROCESS_TEST_HOOKS \
    -Ithird_party/minivmac/inprocess -Isrc \
    src/MiniVmacInProcessCore.cpp tests/MiniVmacInProcessCoreTests.cpp \
    build-minivmac-inprocess/libvwminivmac-test.a \
    -o build-minivmac-inprocess/minivmac-inprocess-tests
./build-minivmac-inprocess/minivmac-inprocess-tests      # 期望 139 checks / 0 failures

python3 third_party/minivmac/inprocess/scan_spawn_apis.py  # 期望 RESULT: CLEAN
```

当前证据见 `build-minivmac-inprocess/.minivmac-inprocess-ready`
（历史阻塞证据保留在 `.minivmac-inprocess-blocked.initial.json`）。

### 多机型变体

上游各机型的 ROM 尺寸与 RAM 不同，**必须用对应机型的完整 cfg 构建**（只改 ROM 尺寸宏不够，
因为屏幕缓冲地址由 `kRAM_Size` 决定）：

| 变体 | 上游 setup 脚本 | `kROM_Size` | RAM |
| --- | --- | --- | --- |
| `plus`（默认，`cfg/`） | `setup-vw-plus.sh` | 0x20000 (128 KiB) | 1 MiB |
| `512ke` | `setup-vw-512ke.sh` | 0x20000 (128 KiB) | 512 KiB |
| `128k` | `setup-vw-128k.sh` | 0x10000 (64 KiB) | 128 KiB |
| `twiggy` | `setup-vw-twiggy.sh` | 0x10000 (64 KiB) | 512 KiB |

构建任一变体：

```bash
bash third_party/minivmac/inprocess/build_engine.sh                # 默认 plus -> libvwminivmac.a
VW_VARIANT=128k bash third_party/minivmac/inprocess/build_engine.sh # -> libvwminivmac-128k.a
```

产物带变体后缀（`libvwminivmac-<variant>.a` / `libvwminivmac-test-<variant>.a`），
目标文件目录同理（`obj-<variant>/`）。变体的 cfg 位于
`third_party/minivmac/inprocess/variants/<variant>/cfg/`，由上游 setup 脚本生成，
其中 `CNFUIOSG.h` 已替换为本模块的 headless 版本。

四个变体均已实测：各自 **139 checks / 0 failures**，`romSize()` 分别返回 65536 或 131072 字节。

#### 能力边界：一个进程一次只能用一个机型

每个变体定义 192 个全局符号，其中 plus 与 128k 之间**冲突 176 个**。
静态库语义下同时链接两个归档**不会报错**——链接器取"先满足者"，
因此**机型由链接顺序静默决定**，实测：

```
plus 在前  -> vw_core_rom_size() = 131072
128k 在前  -> vw_core_rom_size() = 65536
```

所以在同一进程内**不支持运行期切换机型**。若要同时支持多个机型，需要：

1. 用 `llvm-objcopy --redefine-syms=<map>` 给每个变体**已定义**的全局符号加唯一前缀
   （只映射定义符号，保留对 libc 的未定义引用不动），否则会给 `_malloc` 等也加前缀而破坏链接；
2. C++ 层用可配置命名空间宏编译多份，使 `vw::MiniVmacInProcessCore` 也随变体隔离。

这两步尚未实现，因此**不要**假设可以一次链接多个变体。

### ROM 尺寸：调用方必须先查询

引擎**不会**补零，也**不会**接受尺寸不符的 ROM。调用方必须在 `loadRom` 之前查询：

```cpp
size_t need = vw::MiniVmacInProcessCore::romSize();   // 静态，create() 之前即可用
// 或 core.romSizeForThisBuild()
```

该值是**编译期**常量，等于上游的 `kROM_Size`，取决于构建所用机型配置：

| 机型配置 | kROM_Size | 字节数 |
| --- | --- | --- |
| Mac Plus（当前引擎所用配置） | `0x20000` | 131072 |
| Mac 128K / 512Ke | `0x10000` | 65536 |

如需支持 64 KiB ROM 的机型，必须用对应机型的 cfg **另行构建一份引擎**；
一个引擎二进制只服务一个 ROM 尺寸。`build_engine.sh` 当前基于 `setup-vw-plus.sh`
的配置（Mac Plus，128 KiB）。

### 依赖与编译要求

* **C11**：引擎用 `<stdatomic.h>` 做跨线程握手，必须 `-std=c11`（构建脚本已设置）。
* **无外部依赖**：不需要 Cocoa / SDL / GTK / Win32 / Qt；只用 libc 与 pthread。
* 接入公共构建时，把 17 个编译单元（15 个上游核心 + `vw_minivmac_core.c` + `vw_screen_probe.c`）编成一个静态库，
  测试目标额外需要 `libvwminivmac-test.a`。

### 公开 API（`src/MiniVmacInProcessCore.hpp`）

`create` / `loadRom` / `attachDisk` / `detachDisk` / `start` / `stop` /
`setKey` / `setMouse` / `latestFrame` / `stats` / `setFrameCallback`。

必须遵守的约束：

1. **单实例**。上游核心状态在进程全局，第二个存活实例返回 `MvStatus::Busy`。
   调用方应把 `Busy` 当作可预期的结果，而不是错误。
2. **ROM 尺寸必须恰好 `kROM_Size`（0x20000）**。短于该值会被拒绝；引擎**不会**补零，
   也不会放宽上游校验去接受伪 ROM。
3. **本模块不随项目分发 ROM**。ROM 由调用方提供；测试只用内存中合成的合成字节。
4. **绝不能**为了启动 Mini vMac 而使用 QProcess / fork / exec / system / popen /
   startDetached / NSTask / CreateProcess。HarmonyOS/OpenHarmony 上这是硬性禁止。
   若将来有人加回外部进程路径，`scan_spawn_apis.py` 必须失败。

### 待办（按归属）

| 归属 | 待办 | 验收方式 | 状态 |
| --- | --- | --- | --- |
| 公共集成 / 构建负责人 | 把 `third_party/minivmac/inprocess` 编入公共构建并纳入发布验证 | 公共构建产出 `libvwminivmac.a`；发布链路跑通引擎自证 | 待办 |
| Mac 平台负责人 | 用进程内 API 替换 4 个外部 `minivmac-*` 可执行文件与 `SystemSoftwareEngine` 的派生调用 | 旧二进制删除或不再被调用；`scan_spawn_apis.py` 对产品路径仍 CLEAN | ✅ 已完成（见下） |
| HarmonyOS / OpenHarmony 平台负责人 | 在平台侧确认无进程派生并接入进程内 API | 平台代码中不存在 spawn 调用；NDK 侧构建通过 | 待办 |

#### Mac 侧集成记录（2026-10-06）

- `src/SystemSoftwareEngine.{hpp,cpp}`：删除 `QProcess` 依赖与 `prepareProcess(QProcess&, …)`，
  改为持有 `MiniVmacInProcessCore` 实例，提供进程内生命周期 `start/stop/isRunning/lastStatus`、
  输入 `setKey/setMouse`、帧面 `latestFrame/stats/setFrameCallback`。`validate` 不再要求外部二进制；
  `detect` 返回 in-process 描述符；`renderCommand` 仅作命令预览说明（不派生进程）。
- `src/VirtualMachineWindow.cpp`：`start()` 的 system-software 分支改为调用
  `systemSoftwareEngine_.start(config_, error)`（不再 `process_.start()`）；`processId()` 对
  system-software 返回 0；`isRunning()`、`rebootVm`、`replaceSystemSoftwareFloppy`/
  `ejectSystemSoftwareFloppy`、`stopVm`、`activateRuntimeWindow`、`captureFrame` 全部改为走
  进程内引擎；键/鼠输入经 `qtKeyToMacKeyCode` 与坐标映射路由到 `setKey`/`setMouse`。
- 已删除 `runtime/system-software/macos/apple-silicon/bin/` 下 4 个外部可执行文件
  （`minivmac-128k / -512ke / -plus / -twiggy`）。它们不再被任何代码路径调用。
- `python3 third_party/minivmac/inprocess/scan_spawn_apis.py` → **RESULT: CLEAN**
  （4 源文件 0 命中；对象扫描 0 命中）。

#### 已知缺口（诚实记录，非假绿）

- **ROM 尺寸：已按契约修复**。`SystemSoftwareRomGenerator` 现在调用
  `vw::MiniVmacInProcessCore::romSize()`（当前 Mac Plus 构建为 `0x20000` = 131072 字节）
  生成对应尺寸的 ROM，不再硬编码 `0x10000`。引擎依旧不补零、不放松校验；若调用方传入真实 ROM，
  必须恰好为该尺寸。`kROM_Size` 是编译期常量，一个引擎二进制只服务一个 ROM 尺寸
  （128K/512Ke 的 64KiB ROM 需另行构建引擎）。**合成 ROM 仍非 Macintosh ROM，不会启动 Mac OS**——
  尺寸对了只是过了引擎的形状契约，不代表能引导。
- **窗口呈现（帧画面）：已接通**。引擎自 revision 3 起 `setFrameCallback` 每帧回传真实帧
  （512×342、1-bit、21888 字节、带 sequence，直接读模拟 RAM 的当前显示页）。本集成已把
  `setFrameCallback` 接到 `VirtualMachineWindow`：工作线程产帧 → 跨线程 marshalling（合并多帧）→
  主线程绘制到 `displayLabel_`，并显示首帧 `firstFrameReady`。不再显示占位文本。帧数据是真的，
  但因合成 ROM 不是 Mac ROM，画面上不会出现 Mac OS。
- **鼠标按键 / 滚轮（待补）**：进程内公开 API 目前仅 `setKey/setMouse`，无独立按钮/滚轮接口；
  按键位置已路由、按钮与滚轮事件被消费但未传达给引擎。**在 `setMouseButton`/`setMouseWheel`
  补齐前，不要以按钮/滚轮做端到端判断。** 该缺口在引擎负责人（廖廖）范围内，将另行补齐并重跑全套验证。
- 合成 ROM 非 Macintosh ROM，不会启动 Mac OS；未启动任何真实客户机。

### 明确的非目标

* 本模块**不**声称 Mac OS 能启动。合成 ROM 不是 Macintosh ROM，`guest_started=false`，
  `real_hardware_verified=false`。
* 本模块**不**做 GUI 呈现。`latestFrame` 给出的是像素数据，窗口由宿主决定。
