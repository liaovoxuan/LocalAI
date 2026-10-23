# QEMU 进程内前端 ABI v2 —— 设计与可应用补丁方案

- 产物：`.deepseek/patches/qemu-inprocess-frontends-v2.patch`（519 行）+ 本设计说明
- 基线：**当前磁盘源码**（`qemu-source` HEAD `d49f8760`，含 WorkBuddy 与 lifecycle 所有者的未提交改动）
- 只读约束遵守：`qemu-source/**`、`src/**`、`tests/**`、CMake、`build-qemu-inprocess/**` **均未修改**；全部工作在 `/tmp/vwpatch` 完成。未 commit/push，未启动 guest/UTM/VM。

> **本文件是设计稿，不是已接线声明。**
> patch 应用后，display / input / audio / block 四类前端**仍然是未接通的**：
> audio 与 block 的 install 函数**故意返回 `-ENOTSUP`**，input 的转换分支同样返回 `-ENOTSUP`。
> 只有 display connector 含真实实现骨架。任何"已接线"的说法都必须等验收命令全绿之后才能提出。

---

## 1. 补丁能否应用（实测）

```bash
# 对当前磁盘基线检查
$ cd qemu-source
$ git --no-optional-locks apply --check --verbose \
      ../.deepseek/patches/qemu-inprocess-frontends-v2.patch
EXIT=0

# 真实应用到 /tmp 副本
$ git apply --verbose .deepseek/patches/qemu-inprocess-frontends-v2.patch
Applied patch qemu-inprocess/qemu_inprocess.h            cleanly.
Applied patch qemu-inprocess/qemu_inprocess_frontends.h  cleanly.
Applied patch qemu-inprocess/qemu_inprocess_ui.c         cleanly.
Applied patch qemu-inprocess/qemu_inprocess_input.c      cleanly.
Applied patch qemu-inprocess/qemu_inprocess_audio.c      cleanly.
Applied patch qemu-inprocess/qemu_inprocess_block.c      cleanly.
APPLY EXIT=0
```

**6/6 hunk 干净应用。** 补丁只触及 `qemu-inprocess/` 下的文件：一个既有头文件 + 五个新文件。**不触碰 WorkBuddy 正在编辑的 `util/qemu-config.c`、`system/vl.c`、`include/system/system.h`、`meson.build`。**

---

## 2. 六项要求的落实

### 2.1 host→guest 强类型 input API（修正方向性错误）

v1 把 `input_event` 放进 `QemuInProcessCallbacks` —— 那是"**QEMU 调 host**"的方向，而 guest 输入必须 host→guest，**且整个 v1 C ABI 没有任何注入函数**。v2 的修法是把它变成**导出函数**而非回调：

```c
typedef struct QemuInProcessInputEvent {
    uint32_t size;         /* 自描述：必须等于 sizeof(本结构) */
    uint32_t abi_version;  /* 必须等于 QEMU_INPROCESS_ABI_VERSION */
    uint32_t type;         /* KEY | POINTER_MOVE | POINTER_BUTTON */
    uint32_t reserved;
    union {
        struct { uint32_t key_kind; uint32_t key_code; uint32_t down; uint32_t r; } key;
        struct { int32_t x, y; uint32_t has_abs; uint32_t r; } ptr;
        struct { uint32_t button; uint32_t down; uint32_t r2, r3; } button;
    } u;
} QemuInProcessInputEvent;

int qemu_inprocess_input_send(QemuInProcessSession *, const QemuInProcessInputEvent *);
```

`size` + `abi_version` 两个字段**消灭了 v1 的类型擦除问题**：两侧独立实现同一隐形字节格式时，任一侧改动都会在**运行时立刻**返回 `-EINVAL`，而不是静默不兼容。`key_kind` 显式区分 QKeyCode 与 Linux keycode。返回值 `0 / -EINVAL / -ENOTSUP / -ENODEV / -EAGAIN` 取代了 v1 无法表达的"静默丢弃"。

### 2.2 Display：格式、stride、行方向、缓冲区生命周期

```c
void (*display_mode)(void *, int32_t w, int32_t h, uint32_t pixel_format);
void (*display_present)(void *, const void *pixels, int32_t w, int32_t h,
                        int64_t stride, uint32_t pixel_format, uint64_t seq);
```

| 项 | v1 | v2 |
| --- | --- | --- |
| pixel format | `int format`，注释写 `0 = RGBA8888` | **枚举 `QemuInProcessPixelFormat`**，含 `X8R8G8B8`（QEMU 默认）、`A8R8G8B8`、`R8G8B8A8`、`RGB565`、`BGR565` |
| stride | `int stride` | `int64_t stride`，**注释明确：字节/行，负值 = bottom-up** |
| 生命周期 | 未提及 | **明确"仅在本调用内有效，宿主必须同步拷贝"** —— 依据 `ui/console.c:797-798`：`gfx_switch` 返回后立即释放旧 surface |
| 格式变化 | 无通知 | 新增 `display_mode()` 前置通知 |

**关键更正**：v1 的 `format=0 = RGBA8888` 与 QEMU 真实默认冲突 —— `qemu_default_pixman_format(32, true)`（`ui/qemu-pixman.c:68-93`）返回 `PIXMAN_x8r8g8b8`，其小端内存字节序是 **B,G,R,X**。v2 用枚举把这个事实摆在 ABI 表面，glue 侧 `vw_map_pixman()` 做显式 switch，**未知格式直接丢弃而不猜测**。

**本 patch 的 display connector 是唯一含真实实现的**：它注册 `QemuConsoleNotifier`（因为本配置 `CONFIG_GTK/SDL/CURSES/COCOA/VNC/SPICE/OPENGL` 全 `#undef`，`qemu_display_find_default()` 无候选、`DisplayState.listeners` 永远为空 —— **只挂一个 dcl ops 永远不会被调用**）。

### 2.3 Block：byte offset/length + 0/-errno + 非 512 对齐

```c
int     (*block_read )(void *, uint32_t disk_id, uint64_t offset, uint64_t length, void *buf);
int     (*block_write)(void *, uint32_t disk_id, uint64_t offset, uint64_t length, const void *buf);
int     (*block_flush)(void *, uint32_t disk_id);
int64_t (*block_get_length)(void *, uint32_t disk_id);
uint32_t(*block_get_align)(void *, uint32_t disk_id);
```

- **byte offset/length**：v1 用 `uint64_t sector` + `size_t len`，语义（sector 单位、len 是字节还是 sector 数）从未定义。v2 统一为字节。
- **0 / -errno**：QEMU 要求 `0` 或 `-errno`（`include/block/block-io.h:327`）。v1 的 trampoline 在"无回调"时返回 `-1`（= `-EPERM`）—— 语义错误，会把"未接线"伪装成权限错误。v2 把它明确为 `-ENOTSUP`。
- **非 512 对齐**：实现 `bdrv_co_preadv` 会让 QEMU 把 `request_alignment` 降为 **1**（`block/io.c:196-198`），`offset/bytes` **不保证 512 对齐**。v2 因此新增 `block_get_align()`，让 glue 能声明它是按扇区对齐（并做 read-modify-write）还是真支持任意对齐。
- **`block_get_length()` 必须真实**：`blk_check_byte_request`（`block-backend.c:1352`）依赖它。
- **coroutine 上下文**：`bdrv_co_*` 在 coroutine + BQL 下运行，同步调用阻塞型宿主回调会**冻结 vCPU 主循环**；正确做法是 `thread_pool_submit_co()`（参照 `block/file-posix.c:2501`）。这一约束写在 connector 的头注释里。

### 2.4 Audio：协商格式、线程/BQL、背压

```c
int      (*audio_setup_out)(void *, const QemuInProcessAudioParams *wanted,
                                    QemuInProcessAudioParams *granted);
void     (*audio_release_out)(void *);
uint32_t (*audio_out)(void *, const void *samples, uint32_t frames);   /* 返回实际消耗帧数 */
int      (*audio_setup_in )(void *, const QemuInProcessAudioParams *wanted,
                                    QemuInProcessAudioParams *granted);
void     (*audio_release_in)(void *);
uint32_t (*audio_in )(void *, void *samples, uint32_t frames);
```

| 项 | v1 | v2 |
| --- | --- | --- |
| 格式协商 | 每次调用传 `channels`/`rate`，与 QEMU "init 时协商一次" 的模型冲突 | **`setup` 协商一次**，`QemuInProcessAudioParams` 携带 format/channels/rate/period |
| 采样格式 | 硬编码 `int16_t` | **枚举**：`S16 / S32 / F32`（本构建 macOS 可能定义 `FLOAT_MIXENG`，不能假定 S16） |
| 背压 | `audio_out` 返回 `void`，**无法回报失败** | **返回实际消耗帧数**；不足即背压信号 |
| 线程/BQL | 头文件笼统写 "worker/IO threads" | 明确：**主循环线程 + 持 BQL**（依据 `qemu_inprocess.c:147-150` 先 `bql_lock()` 再 `qemu_main_loop()`；`util/main-loop.c:298-320` 只在 poll 时放锁） |
| underrun | 无通道 | `audio_in` 返回实际产生帧数（短读即 underrun） |

**QOM 而非 audio_pcm_ops**：v1 审计稿中的 `struct audio_pcm_ops` 在 QEMU 11.1.50 **全树 0 命中**。真实模型是 QOM `AudioBackendClass` 子类（`.parent = TYPE_AUDIO_MIXENG_BACKEND`，类型名 `"audio-<driver>"`）。**选中它必须改 `qapi/audio.json:474-487` 的封闭 enum**，加两处 `audio/audio.c` 的 abort 级 switch —— 这属**独占 QAPI 变更窗口**，本 patch **故意不含**，install 函数返回 `-ENOTSUP`。

### 2.5 四类 glue 只进 `qemu-inprocess` target

**硬约束（实测）**：

```bash
$ grep -n "libqemuui" build-qemu-inprocess/qemu-build/build.ninja
build.ninja:13531  qemu-system-aarch64-unsigned: ... ui/libqemuui.a.p/<all .o> ...
```

`meson.build:4471-4475` 用 `extract_all_objects(recursive: true)` 把 UI 对象**整体**并入 standalone。**放在 `ui/`、`audio/`、`block/` 下的 connector 都会进入 standalone 二进制**，其 `qemu_inprocess_*` 引用在那里未定义 → **破坏普通 `qemu-system`**。同理适用于 `audio/` 与 `block/`。

因此本 patch 的**全部新文件都放在 `qemu-inprocess/` 下**，只进 `meson.build:4413-4421` 的 `qemu-inprocess-<target>` 静态库（仅被 in-process 二进制链接）。

### 2.6 应用后的构建与运行验收命令

```bash
# ---- 0) 应用补丁（在 qemu-source 内）----
cd qemu-source
git apply --check ../.deepseek/patches/qemu-inprocess-frontends-v2.patch   # 期望 EXIT=0
git apply         ../.deepseek/patches/qemu-inprocess-frontends-v2.patch

# ---- 1) 暂缓 hunk A：把新文件加入 in-process 静态库 ----
#     qemu-source/meson.build:4415 的 qemu-inprocess-<target> sources 列表
#     追加：qemu_inprocess_ui.c / qemu_inprocess_input.c /
#           qemu_inprocess_audio.c / qemu_inprocess_block.c
#     ⚠ 与 lifecycle 所有者的 meson hunk 是同一处，须先协调后再改

# ---- 2) 暂缓 hunk B：init 成功后安装前端 ----
#     qemu-source/qemu-inprocess/qemu_inprocess.c，在 init 成功返回前调用：
#         qemu_inprocess_install_frontends(session);
#     ⚠ 该文件归 lifecycle 所有者；须由其执行或显式移交

# ---- 3) 重新配置与编译 ----
./configure --target-list=aarch64-softmmu --disable-werror --disable-docs
ninja -C build libsystem.a.p/system_vl.c.o
ninja -C build qemu-system-aarch64        # 必须仍然成功

# ---- 4) 关键回归：standalone 未被污染 ----
#     connector 若误入 ui/audio/block，此命令会因 qemu_inprocess_* 未定义而失败
nm -u build/qemu-system-aarch64 | grep -c qemu_inprocess   # 期望 0

# ---- 5) 运行验收（需真实核心测试二进制，见下一节）----
#     display：DisplayState.listeners 非空；display_present 在
#              主循环线程与 vCPU 线程各至少触发一次
#     input  ：运行态必须产生 trace input_event_key_qcode（ui/input.c:229-271）
#              非运行态必须返回 -ENOTSUP 而非静默
#     audio/block：install 返回 -ENOTSUP 属预期；接线完成前不得声称可用

# ---- 6) 防 green-wash 断言 ----
#     非 qemu-inprocess/ 目录必须出现调用点，否则只是又一次"存储但未调用"
```

**trace 佐证命令**（接线后）：

```bash
ninja -C build qemu-system-aarch64
# 用 --trace 打开相关事件后运行 in-process harness
./build/tests/qemu_inprocess_frontends --trace 'displaychangelistener_register' \
                                        --trace 'input_event_key_qcode'
```

---

## 3. 因 WorkBuddy 未完成而**暂缓**的 hunk

补丁**故意不含**以下两处，原因是它们与正在进行的生命周期修复**落在同一文件/同一行段**：

| # | 位置 | 需要的改动 | 暂缓原因 |
| --- | --- | --- | --- |
| **A** | `qemu-source/meson.build:4415`（`qemu-inprocess-<target>` 的 sources 列表） | 追加 4 个 connector 源文件 | 该处正是 lifecycle 所有者此前加入 `qemu-inprocess/` 的同一 hunk；`git status` 显示 `meson.build` 已是 `M`（未提交修改中） |
| **B** | `qemu-source/qemu-inprocess/qemu_inprocess.c`（init 成功后） | 调用 `qemu_inprocess_install_frontends(session)`（约 2 行） | 该文件归 lifecycle 所有者；且 `git status` 显示 `qemu-inprocess/` 为 untracked 且正被活跃修改（`headless_ui_stubs.c`、`lifecycle_harness.c` 为近期创建） |

**另有一处需要协调的签名**：本 patch 在 `qemu-inprocess/qemu_inprocess_frontends.h` 中声明了**无参** accessor

```c
const QemuInProcessCallbacks *qemu_inprocess_active_callbacks(void);
```

原因是现有 `qemu_inprocess_callbacks(session)` **需要 session 句柄，而 QEMU 侧的 glue 拿不到它**。建议与 lifecycle 所有者**先冻结这个签名**，再让 audio/block 只读调用，避免两端争用同一符号。

**当前 `git status`（说明基线为何是变动的）**：

```
 M include/qemu/config-file.h
 M include/system/system.h
 M meson.build
 M system/vl.c
 M util/qemu-config.c
?? qemu-inprocess/
```

---

## 4. 本设计的明确边界

1. **这不是已接线**。应用 patch 只得到：ABI v2 头文件 + 5 个新文件，其中
   **display 有实现骨架，input/audio/block 的 install 或转换分支故意返回 `-ENOTSUP`**。
2. **未做任何运行时验证**，未启动 guest/UTM/VM。patch 只经过 `git apply --check` 与一次 `/tmp` 真实 apply。
3. **未编译**：本 patch 新增的 C 文件需要一个把它们加入 `qemu-inprocess-<target>` 的 meson hunk（暂缓项 A）才可能被编译。在该 hunk 落地前，**无法声称它们可编译**。
4. **audio 的 QAPI 变更（`qapi/audio.json`）不在本 patch 内**，需要独立排期与 QAPI 代码重生成。
5. `qemu_inprocess_ui.c` 引用的 `qemu_console_add_notifier` / `qemu_console_register_listener` / `surface_*` 均为 QEMU 已导出符号，但**其确切签名未在本轮逐行复核**（`include/ui/console.h` 可读，`ui/input.h` 在本机 dataless 时曾不可读）。首次编译时应以编译器诊断为准。
