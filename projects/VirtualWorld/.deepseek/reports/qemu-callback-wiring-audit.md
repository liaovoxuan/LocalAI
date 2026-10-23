# QEMU 进程内 Display / Input / Audio / Block callback ABI 接线审计

- 审计性质：**只读**。本次只新建本文件，临时探针仅写 `/tmp`。未修改任何仓库文件（尤其未触碰 `qemu-source/qemu-inprocess/**`、`runstate`/`qemu-config`、`src/**`、CMake、Mini VMac、JoyCode 文件）。未 commit/push，未启动 guest/UTM/VM/QEMU 进程。
- QEMU 上游：`qemu-source/` VERSION **11.1.50**
- 结论口径：**静态存在 ≠ 已接通**。结构体被存储、mock 通过、getter 存在，都**不算** VERIFIED。

---

## 0. 总结论

| 类别 | 判定 | 一句话依据 |
| --- | --- | --- |
| **Display** | **NOT VERIFIED** | callback 从未被调用；QEMU 侧无任何 glue |
| **Input** | **NOT VERIFIED** | 同上 |
| **Audio** | **BLOCKED** | 接线所需的真实 QEMU 对象已**全部定位**，但连接件一行都不存在 |
| **Block** | **BLOCKED** | 同上 |

> **BLOCKED 的语义**（本报告口径）：不是"环境不允许验证"，而是"依赖对象已定位、连接件零实现，未接线的状态无法通过任何运行手段变绿"。

**"stored in the core but is not yet wired to real QEMU frontend/device backends" 这一结论成立，且比它描述的更彻底** —— 不是"未接线"，而是**连构成接线前提的 QEMU 侧 glue 一行都不存在**（§2.3）。

---

## 1. 三类硬证据

### 1.1 callback 在 core 里 0 次调用

```bash
$ cd qemu-source/qemu-inprocess
$ grep -n "cb\.\|display_update\|input_event\|audio_out\|audio_in\|block_read\|block_write" qemu_inprocess.c
238: * to the host-provided callbacks. Returns NULL when no session is active. */
239:const QemuInProcessCallbacks *qemu_inprocess_callbacks(

$ grep -cE "session->cb\.(display_update|input_event|audio_out|audio_in|block_read|block_write)\(" qemu_inprocess.c
0
```

即：这六个回调名在 `qemu_inprocess.c` 中**只出现在注释与 getter 名字的上下文里**，**没有任何一处实际调用**。

### 1.2 结构体确实被存储（所以"stored"是真的）

```c
/* qemu_inprocess.c:78 */
session->cb = *cb;
```

存储路径存在，仅此而已。

### 1.3 getter 的注释自认需要一层"glue"，而该 glue 不存在

```c
/* qemu_inprocess.c:237-244 */
/* Accessor used by the QEMU UI/audio/block glue that forwards guest activity
 * to the host-provided callbacks. Returns NULL when no session is active. */
const QemuInProcessCallbacks *qemu_inprocess_callbacks(
    QemuInProcessSession *session)
{
    return session ? &session->cb : NULL;
}
```

注释描述的调用方（"the QEMU UI/audio/block glue"）**在仓库中不存在**：

```bash
# 全仓搜索 getter 的调用者（排除定义本身与 mock）
$ grep -rn "qemu_inprocess_callbacks" --include=*.c --include=*.cpp --include=*.h --include=*.hpp \
    src/ tests/ qemu-source/qemu-inprocess/
tests/qemu_inprocess_mock.c:111:const QemuInProcessCallbacks *qemu_inprocess_callbacks(
```

**唯一出现处是 mock 对同一符号的定义**。生产代码零调用。

### 1.4 QEMU 源码零引用 core；core 也未接入 QEMU 构建

```bash
$ cd qemu-source
$ grep -rn "qemu_inprocess" --include=*.c --include=*.h --include=*.m system/ ui/ audio/ block/ include/ util/
system/vl.c:36:static __thread bool qemu_inprocess_checked = false;      # 生命周期修复引入的私有变量
system/vl.c:37:static __thread Error *qemu_inprocess_err_slot = NULL;
... (均为 checked-init 的私有状态，与 callback 无关)

$ git --no-optional-locks cat-file -p HEAD:meson.build | grep -c "qemu-inprocess"
0
```

**没有任何 QEMU 翻译单元 `#include "qemu_inprocess.h"`，且 `qemu-inprocess/` 不在 QEMU 的 meson 体系里。**

**这条比"未接线"更强**：即使有人现在写了 glue，它也无法通过 QEMU 的构建被编译进去 —— 因为该目录未被 `subdir()` 引入。

---

## 2. 每类回调的真实挂接点、生命周期与线程上下文

以下挂接点均已用只读命令定位（文件:行号为 QEMU 11.1.50 实测）。

### 2.1 Display

**真实入口**（实测）：

```bash
$ git --no-optional-locks cat-file -p HEAD:include/ui/console.h | grep -n "DisplayChangeListenerOps\|dpy_gfx_update"
167:typedef struct DisplayChangeListenerOps {
174:    void (*dpy_gfx_update)(DisplayChangeListener *dcl,
178:                           struct DisplaySurface *new_surface);
230:} DisplayChangeListenerOps;
234:    const DisplayChangeListenerOps *ops;
```

**要真接线必须挂接的对象**（已更正：比初稿说的复杂一层）：

> **初稿写的 `register_displaychangelistener()` 在 11.1.50 不存在** —— `git grep --cached` 证实该名与 `qemu_console_get_surface` 均已改名。真名是
> `ui/console.c:631 qemu_console_register_listener(con, dcl, ops)` 与 `ui/console.c:1375 qemu_console_surface()`。

**仅写一个 dcl ops 结构体拿不到帧**。实测本配置的硬事实：

```bash
$ grep CONFIG_ build-qemu-inprocess/qemu-build/config-host.h
CONFIG_GTK / SDL / CURSES / COCOA / VNC / SPICE / OPENGL  → 全部 #undef
CONFIG_PIXMAN                                             → define
```

→ `ui/console.c:1393-1423 qemu_display_find_default()` **无候选** → `dpy.type = NONE` → `ui/console.c:1448-1456 qemu_display_init()` **直接 return** → **`DisplayState.listeners` 永远为空**。

因此接线必须**同时新增一个 QemuDisplay 后端（或等价的 dcl 装配点）**。规避 QAPI 变更的做法：不新增 `DisplayType`（`qapi/ui.json:1522-1534` 无 in-process 成员），改用 `qemu_console_add_notifier()` + `QEMU_CONSOLE_ADDED`（`include/ui/console.h:432-434`，实现 `ui/console.c:1087`，notify 于 `:1159`；现有用户 `ui/gtk.c:2767`、`ui/dbus.c:315`）。

**唯一派发点**（实测）：`ui/console.c:744-752`
```c
QLIST_FOREACH(dcl, &s->listeners) { dcl->ops->dpy_gfx_update(dcl, x, y, w, h); }
```
现存注册点（= 参照实现）：`ui/sdl2.c:978`、`ui/gtk.c:2415`、`ui/curses.c:813`、`ui/vnc.c:4267`、`ui/dbus-console.c:624`、`ui/egl-headless.c:242`、`ui/spice-display.c:1425`。

数据转换参照：`ui/sdl2-2d.c:31-57` 与 `:92-119`（按 `surface_format` **显式 switch**）。surface 访问器 `include/ui/surface.h:17-25, 51-86`。

**生命周期**：`QemuConsole` 在 machine/device realize 阶段创建；`dpy_gfx_update` 在整个 VM 运行期被反复调用；注销需 `unregister_displaychangelistener()`。display 的创建时机晚于 `qemu_init`，因此 glue 必须在 console 出现**之后**注册，而不是在 `init` 返回时。

**线程上下文 —— 是两种，不是一种**：

| 路径 | 线程 | 证据 |
| --- | --- | --- |
| 定时刷新 | 主循环线程 | `ui/console.c:137` `timer_new_ms(QEMU_CLOCK_REALTIME, gui_update, ds)` → `dpy_refresh` → `qemu_console_hw_update`（`:153-161`）；`:168-177` 用 `aio_bh_schedule_oneshot(qemu_get_aio_context(), ...)` |
| guest 扫描输出 | **vCPU 线程（持 BQL）** | `hw/display/virtio-gpu.c:656-737 virtio_gpu_do_set_scanout` ← guest virtio 命令 → `:734 qemu_console_set_surface` → 同步 `dpy_gfx_switch` + 整帧 update |

跨线程抓 BQL 的既有范式：`ui/spice-core.c:214-227`、`ui/cocoa.m:113-142 with_bql()`。反向约束：`ui/sdl2.c:1017` 注释 "SDL's event polling (in dpy_refresh) must happen on the main thread"。

→ 因此头文件 `qemu_inprocess.h:53` 的 "invoked from QEMU's worker/IO threads" **既不精确也不充分**；host 侧 trampoline（`src/QemuInProcessRuntime.cpp:47-57`）只锁 `callbackMutex` 拷 `std::function`，**不构成任何线程归属保证**。

**潜在 ABI 问题**：

| # | 问题 |
| --- | --- |
| **D1** | **`format = 0 "RGBA8888"` 与 QEMU 的真实默认格式冲突**：`ui/qemu-pixman.c:68-93` 的 `qemu_default_pixman_format(32, true)` 返回 **`PIXMAN_x8r8g8b8`**，`ui/console.c:816-820` 的默认白名单就是它。pixman 格式是 **native endian**（`include/ui/qemu-pixman.h:18-55`），所以在小端机器上内存字节序是 **B, G, R, X** —— 不是 RGBA。真正的 RGBA8888 是 `PIXMAN_r8g8b8a8`，既非默认也**无协商机制**。参考实现全都显式分支（`ui/sdl2-2d.c:148-165` 列举 10 个格式；`ui/gtk.c:522` 特判 `x8r8g8b8`）。 |
| D2 | `stride` 与 pixman 的 `linesize` 语义一致（都是字节/行），但 callback 未声明行方向（top-down vs bottom-up）。 |
| **D3** | **`pixels` 的所有权未定义，且后果是悬垂指针**：`ui/console.c:797-798` 在 `gfx_switch` 返回后**立即 destroy/free 旧 surface**。回调返回 `void`、没有任何所有权转移语义 → **宿主必须在本调用内同步拷贝**，异步保留指针必然悬垂。 |
| D5 | `pixels` 还可能**直指 guest RAM**（`hw/display/bochs-display.c:222-232` 用 `memory_region_get_ram_ptr + mode.offset`），也可能是**占位 surface**（`ui/console.c:276-289` "no graphic display device"、`:766-784` "output is not active"）。 |
| D4 | 无 dirty-rect 信息，只有整帧，跨进程内边界可能造成大量冗余拷贝。 |

### 2.2 Input

**真实入口**（实测）：

```bash
$ git --no-optional-locks cat-file -p HEAD:include/ui/input.h | grep -n "qemu_input_event_send\|QemuInputEvent"
26:typedef struct QemuInputEvent {
27:    InputEventKind type;
35:} QemuInputEvent;
57:void qemu_input_event_send(QemuConsole *src, QemuInputEvent *evt);
58:void qemu_input_event_send_impl(QemuConsole *src, QemuInputEvent *evt);
59:void qemu_input_event_sync(void);
```

**要真接线必须挂接的对象**：
- 把宿主的事件序列化成 QEMU 的 `QemuInputEvent` / QAPI `InputEvent`（`qapi/ui.json`）
- 调用 `qemu_input_event_send(console, &evt)`，必要时 `qemu_input_event_sync()`

**生命周期**：需要一个 `QemuConsole`（或缺省 console）；可在 VM 运行期随时注入。

**线程上下文**：输入注入要求持有 BQL；从非 BQL 线程直接调用需要 `bql_lock()`。宿主的 UI 线程通常是**另一个线程**，所以 glue 必须做 BQL 交接或投递到主循环。

**潜在 ABI 问题**：

| # | 问题 |
| --- | --- |
| **I0** | **ABI 方向是反的 —— 这是最严重的一条。** `input_event` 被放在 `QemuInProcessCallbacks` 里，即"**QEMU 调用 host**"的方向（头文件 `:53` 明说 host supplies implementations）。但 guest 输入在语义上必须走 **host → QEMU**。而**整个 C ABI 的导出函数只有** `session_create/session_destroy/last_error/init/run/request_stop/cleanup/kvm_usable/hardware_accel_active/callbacks` —— **没有任何 host→guest 注入函数**。因此**在当前 ABI 下，guest 输入根本无法送达**。这不是"待实现"，而是签名层面的设计错误：接收器被放在了发送端。 |
| I1 | **`input_event(userdata, const void *event, size_t len)` 是彻底类型擦除的**：没有任何字段说明 `event` 是序列化的 QAPI、还是自定义二进制、还是指针。这是**结构性 ABI 缺陷** —— 该回调在没有附加契约的情况下无法被任何实现正确消费。 |
| I2 | `size_t len` 暗示是"字节流"，但 `qemu_input_event_send` 接受的是**结构化对象**。要么 glue 反序列化（需要自定 schema），要么 ABI 必须改。 |
| I3 | 无返回值语义约定（返回 0 表示接受？EAGAIN 表示队列满？）。且 "Returns 0 if accepted" 的措辞与 QEMU→host 方向不匹配。 |
| I4 | 无 `device`/`head` 路由（QAPI 的 `input-send-event` 有 `*device`/`*head`）；无键值语义约定（Linux keycode vs `KeyValue.number`/`qcode`）。 |
| I5 | replay 的字节编码**不可复用**：`replay_save_input_event` 在私有头 `replay/replay-internal.h:174-176`，且写的是 replay 日志流而非调用者缓冲区。 |

**正确的挂接目标（实测）**：`include/ui/input.h:57-88`；派发 `ui/input.c:344-357 qemu_input_event_send_impl`。QAPI 型通道可用 `qmp_marshal_input_send_event(QDict*, QObject**, Error**)`（`build-qemu-inprocess/qemu-build/qapi/qapi-commands-ui.h:49`，**非 static、可直接链接**）。

### 2.3 Audio

> **本报告初稿在此处有两处事实错误，已更正。**
> (a) 初稿称"QEMU 后端通过 `struct audio_pcm_ops` 提供 init_out/write/..." ——
> **`audio_pcm_ops` 在 QEMU 11.1.50 全树 0 命中**，`include/audio/` 目录**不存在**。那是旧版本的模型。
> (b) 初稿称"音频回调很可能不在 BQL 上" —— **实测相反，见下**。

**核实命令与结果**：

```bash
$ git --no-optional-locks grep -c "audio_pcm_ops" HEAD | wc -l
0                                          # 全树零命中
$ git --no-optional-locks ls-tree HEAD include/ | grep -c audio
0                                          # include/audio/ 不存在
$ git --no-optional-locks grep -l "AudioBackendClass" HEAD
HEAD:audio/alsa.c  HEAD:audio/alsaaudio.c  HEAD:audio/audio-be.c   # 真实模型是 QOM 类
```

**11.1.50 的真实模型是 QOM**：
- 后端基类 `AudioBackendClass`（`include/qemu/audio.h:66-110`），QOM 名 `audio-backend`
- 只需实现 **Mixeng 子类**槽位，参照 macOS 后端 `audio/coreaudio.m:653-685`
  （`init_out` `:570`、`fini_out` `:622`、`write` `:665`、`get_buffer_out`/`put_buffer_out`/`enable_out`）
- `.parent = TYPE_AUDIO_MIXENG_BACKEND`，类型名必须是 `"audio-<driver>"`（`audio/coreaudio.m:36`）

**后端选择链**：`audio/audio-be.c:244-262` 用 `g_strconcat("audio-", AudiodevDriver_str(dev->driver))` → `object_new()` → `realize()`。

**QAPI 是硬门槛**：`qapi/audio.json:474-487` 的 `AudiodevDriver` 是**封闭 enum**。不改 QAPI，新 driver 字符串会被 visitor **直接拒绝**。另有两处 `abort()` 级 switch 必改：`audio/audio.c:173-227`、`:269-385`。

**生命周期**：`realize` → `open_out/open_in` → **`init_out`/`init_in`（采样率/声道/格式的唯一确定点）** → `write`/`read` → `fini_out`/`fini_in`。
默认 44100Hz / 2ch / `AUDIO_FORMAT_S16`（`audio/audio.c:251-266`），与 callback 的 `int16_t` 吻合，**但必须在 `init_out` 里强制 `as->fmt = AUDIO_FORMAT_S16`**，否则 voice 被 guest 以 U8/S32/F32 复用时数据会被破坏。

**线程上下文（更正）**：audio 回调运行在**主循环线程且持有 BQL**。依据：`qemu_inprocess.c:147-150` 先 `bql_lock()` 再 `qemu_main_loop()`；`util/main-loop.c:298-320` 只在 poll 阻塞期间放锁，**dispatch 阶段持有**。因此：

- 宿主在 `audio_out`/`audio_in` 里阻塞 ⇒ **冻结整个 vCPU 主循环**
- 回调收到的缓冲是**后端内部环形缓冲指针** ⇒ **不可长期保留、不可重入 QEMU**（BQL 下重入 = 死锁）

**潜在 ABI/锁问题**（据子审计实测）：

| # | 问题 |
| --- | --- |
| A1 | ABI **没有字节长度字段**，字节数只能由 `frames*channels*2` 反推；宿主误判即静默错位 |
| A2 | `int16_t` 硬编码**仅在强制 S16 时成立**；macOS 构建定义 `FLOAT_MIXENG`（`audio/audio_int.h:28-31`），clip 路径随 `hw->info.af` 变化 |
| A3 | `audio_in` 的 `read` 给的是**字节数**（`audio_int.h:208`），而 callback 收 **frames** —— 转换必须在驱动内完成；且欠读只 `break` 不报错 |
| A4 | `audio_out` 返回 `void`，**无法回报失败**（无 underrun 通道） |
| A5 | `channels`/`rate` 每次调用回传，与 QEMU "init 时协商一次" 的模型冲突 |

### 2.4 Block

**真实入口**（实测）：

```bash
$ git --no-optional-locks grep -n "bdrv_co_preadv" HEAD -- include/block
HEAD:include/block/block_int-common.h:546:    int coroutine_fn GRAPH_RDLOCK_PTR (*bdrv_co_preadv)(BlockDriverState *bs,
HEAD:include/block/block_int-common.h:550:    int coroutine_fn GRAPH_RDLOCK_PTR (*bdrv_co_preadv_part)(

$ git --no-optional-locks grep -ln "struct BlockDriver {" HEAD
HEAD:include/block/block_int-common.h
```

**关键**：`bdrv_co_preadv` 带 **`coroutine_fn`** 限定 —— **QEMU block 层运行在 coroutine 上下文**。

**要真接线必须挂接的对象**：
- 一个自定义 `BlockDriver`，实现 `bdrv_co_preadv` / `bdrv_co_pwritev`（或 `_part` 变体）
- 通过 `bdrv_open` / `blockdev-add`（QAPI）实例化，并由 `disk_id` 映射到多个 `BlockDriverState` 实例

**生命周期**：`BlockDriverState` 在 `bdrv_open` 时创建、`bdrv_close` 时销毁；guest 的每次磁盘 IO 都会走到 `bdrv_co_preadv`。

**线程上下文**：**coroutine + BQL 持有**（`include/block/block-io.h:335-353` 的 `IO_OR_GS`、`include/block/block-global-state.h:34-49` 的 GS 语义；`bs->aio_context = qemu_get_aio_context()` 于 `block.c:426`）。**直接同步调用宿主 `block_read` = BQL 下阻塞 IO，冻结 vCPU 主循环。**

**正确做法**：`include/block/thread-pool.h:37-39 thread_pool_submit_co()` —— 它 yield coroutine，函数在**池线程、无 BQL** 执行（参照 `block/file-posix.c:2501-2504`、`block/qcow2-threads.c:54`）。**驱动内绝不能回调进 QEMU。**

**`disk_id` 的答案**：QEMU 的 `BlockDriverState` **没有** disk_id 字段 —— 必须由驱动在 `bdrv_open` 时从 options 取 `disk-id` 存进 `bs->opaque`，1 node = 1 disk_id。

**注册与选择**：`block.c:404-409 bdrv_register()` + `include/qemu/module.h:55 block_init()` 宏；源文件须入 `block/meson.build:2-42`（注意 `:141-146 module_block.py` 会**解析源文件文本**生成 `module_block.h`）。选择走 `block.c:4104-4111 bdrv_find_format()` → `:442-454` 线性扫 `bdrv_drivers` 比对 `format_name`。最简参照 `block/null.c`（327 行）。

**潜在 ABI 问题**：

| # | 问题 |
| --- | --- |
| B1 | `uint64_t sector` + `size_t len`：sector 单位（注释只说 512 字节）与 `len` 的关系未定义 —— `len` 是字节还是 sector 数？两者是否可能非整数倍？ |
| B2 | `uint32_t disk_id` 只有 32 位，且**没有任何注册/注销 API** 告诉宿主有哪些 disk_id 存在。宿主无从知道该准备哪些盘。 |
| B3 | callback 返回 `int`，但 QEMU 要求 `0` 或 `-errno`（`include/block/block-io.h:327`）。**ABI 未声明该约定**，而现有 trampoline 在"无回调"时返回 **`-1` = `-EPERM`**（`src/QemuInProcessRuntime.cpp:97/107`）—— **语义错误**。 |
| B6 | **最易踩**：只要实现了 `bdrv_co_preadv`，`request_alignment` 默认降为 **1**（`block/io.c:196-198`），`offset/bytes` **不保证 512 对齐**；而 ABI 只给 `sector`、**没有 sector 内偏移**。必须自设 `bs->bl.request_alignment = BDRV_SECTOR_SIZE` 或做 read-modify-write。 |
| B7 | 若未设 `protocol_name`，`file=` 形式的 filename 会被误当作待探测格式（`block.c:2107-2110`）。 |
| B4 | 无 flush / discard / write-zeroes 回调 —— guest 的 `BLKFLSBUF` 等无法表达。 |
| B5 | `coroutine_fn` 上下文 + 宿主 callback 是**同步签名**，二者在语义上冲突（见上文线程上下文）。 |

---

## 3. 核对 ready marker 的原结论

withdrawn 的 `.qemu-inprocess-ready` 写道：

> The display/input/audio/block callback ABI is stored in the core but is not yet wired to real QEMU frontend/device backends.

**核对结果：结论正确，但低估了缺口。** 准确的表述应是：

1. ABI **被存储** ✅（`qemu_inprocess.c:78`）
2. **没有任何调用点** ✅（§1.1）
3. **QEMU 侧连一行 glue 都没有** ✅（§1.4）—— marker 说"not yet wired"，读者可能以为只需"接上"，实际需要**从零编写 glue**，且
4. **`qemu-inprocess/` 未接入 QEMU 的 meson 构建**（`grep -c qemu-inprocess` = 0）—— 这是一个**前置**于 glue 的缺口：即使 glue 写好了，也不会被编译进 QEMU。

因此"未接线"不是收尾工作，而是一个**未开始的子系统**。

---

## 4. 判定汇总

| 类别 | 判定 | 判定理由 |
| --- | --- | --- |
| Display | **NOT VERIFIED** | 无调用点；无 `DisplayChangeListenerOps` 实现；无注册 |
| Input | **NOT VERIFIED** | 无调用点；无 `qemu_input_event_send` 调用；且 ABI 本身类型擦除 |
| Audio | **NOT VERIFIED** | 无调用点；无 `audio_pcm_ops` 实现；且模型与 QEMU 的 init-协商不匹配 |
| Block | **NOT VERIFIED** | 无调用点；无 `BlockDriver` 实现；且 coroutine 上下文未处理 |

**没有任何一项可以判 VERIFIED。** 也没有判 BLOCKED：不存在"环境不允许验证"的情况 —— 这是**根本未实现**，与 dataless 之类的环境限制无关（audio 一节的部分行号确因 dataless 未核实，已标注）。

---

## 5. 下一轮最小不重叠文件范围建议

> **本节已被硬约束推翻并重写。** 初稿建议把 connector 放在 `ui/`、`audio/`、`block/` 下 ——
> **那是错的，会让 standalone `qemu-system-aarch64` 链接失败。**

### 5.1 为什么不能放 `ui/`（实测）

```bash
$ grep -n "libqemuui" build-qemu-inprocess/qemu-build/build.ninja | head -2
build.ninja:13531  ... qemu-system-aarch64-unsigned: ... ui/libqemuui.a.p/<all .o> ...
```

`meson.build:4471-4475` 用 `objects: lib.extract_all_objects(recursive: true)` 把 UI 对象**整体**并入 standalone `qemu-system-aarch64`。因此任何放在 `ui/` 下的 connector 都会**被拉进 standalone 二进制**，它引用的 `qemu_inprocess_*` 在那里未定义 → **standalone qemu-system 链接失败**。

**同理适用于 `audio/` 与 `block/`**（它们的对象同样进入 standalone 的链接输入）。**把 connector 放进这些目录会破坏普通 qemu-system 主程序** —— 而"保留普通 qemu-system 原有行为"是本项目的硬要求。

### 5.2 唯一安全落点：`qemu-inprocess/`

`meson.build:4413-4421` 的 `qemu-inprocess-<target>` 静态库**只**被 in-process 二进制链接。所以**四类 connector 全部放这里**：

| 工作项 | 建议新文件（全部在 `qemu-inprocess/` 下） |
| --- | --- |
| 内部接口 | `qemu-inprocess/qemu_inprocess_ui.h`（**勿改**已有 `QemuInProcessCallbacks` 结构） |
| Display connector | `qemu-inprocess/qemu_inprocess_ui.c` —— static dcl ops + 每 console 一个 dcl + `QemuConsoleEvent` notifier + 矩形→全帧 + 同步拷贝 + 显式格式映射 |
| Input 注入器 | `qemu-inprocess/qemu_inprocess_input.c` —— blob→`QemuInputEvent`（或 `QDict`→`qmp_marshal_input_send_event`），持 BQL 或 bh 投递，返回可区分错误码 |
| Audio connector | `qemu-inprocess/qemu_inprocess_audio.c` —— 新 QOM 类型 `"audio-inprocess"`，`.parent = TYPE_AUDIO_MIXENG_BACKEND` |
| Block connector | `qemu-inprocess/qemu_inprocess_block.c` —— 新 `BlockDriver` + `block_init()` 注册 |

### 5.3 需要修改的既有文件（共 2 处，均已标注冲突风险）

| 文件 | 改动 | 风险 |
| --- | --- | --- |
| `qemu-source/meson.build:4415` | 把上述新文件加入 `qemu-inprocess-<target>` 的 sources 列表 | ⚠ **与 in-process 的 meson hunk 是同一处**（该文件也是 lifecycle 所有者此前编辑过的），须先协调 |
| `qemu-source/qemu-inprocess/qemu_inprocess.c` | `init()` 成功后调用 install hook（约 2 行） | ⚠ `qemu-inprocess/**` 归 lifecycle 所有者；若被锁定，此行须由该 owner 执行或显式移交。可用 weak 符号把链接风险降到最低 |

**若还需改 `qapi/audio.json`**（audio 的 `AudiodevDriver` 是封闭 enum，不改则新 driver 被 visitor 拒绝）：这是一次**独占的 QAPI 变更窗口**，应单独排期，且要重跑 QAPI 代码生成。

### 5.4 明确不要碰

`qapi/ui.json` 与 QAPI 重生成（display 侧用 `qemu_console_add_notifier()` 即可完全绕开）；`ui/console.c`、`include/ui/console.h`、`ui/input.c`、`include/ui/input.h`（所需符号**全部已是导出符号**，且都在 in-process 归档的链接输入里）；`system/runstate.c`、`util/qemu-config.c`、`monitor/qemu-config-qmp.c`（runstate/qemu-config 范围）；`system/vl.c` 与 `include/system/system.h`（本轮不需要）。

### 5.5 验收断言（防 green-wash）

在接线被声称完成之前，以下必须成立：

1. **非 `qemu-inprocess/` 目录必须出现调用点** —— 否则只是又一次"存储但未调用"
2. 运行时 **`DisplayState.listeners` 非空**，且 `display_update` 在**主循环线程与 vCPU 线程各至少触发一次**（用 trace `displaychangelistener_register` 佐证）
3. input 在运行态必须产生 **trace `input_event_key_qcode`**（trace 点在 `ui/input.c:229-271`）；非运行态必须返回**可区分错误**而非静默丢弃
4. **standalone `qemu-system-aarch64` 仍能链接** —— 这是"保留原有行为"的直接检验

未达成前，marker 不得声称已接线。

## 6. 复现命令速查

```bash
cd <repo>/qemu-source

# callback 是否被调用
grep -cE "session->cb\.(display_update|input_event|audio_out|audio_in|block_read|block_write)\(" \
    qemu-inprocess/qemu_inprocess.c            # -> 0

# getter 的调用者
grep -rn "qemu_inprocess_callbacks" --include=*.c --include=*.cpp --include=*.h --include=*.hpp \
    ../src ../tests qemu-inprocess/            # -> 仅 mock 的定义

# QEMU 是否引用 core / 是否接入构建
grep -rn "qemu_inprocess" --include=*.c --include=*.h --include=*.m \
    system/ ui/ audio/ block/ include/ util/   # -> 只有 checked-init 私有变量
git --no-optional-locks cat-file -p HEAD:meson.build | grep -c "qemu-inprocess"   # -> 0

# 真实挂接点
git --no-optional-locks cat-file -p HEAD:include/ui/console.h   | grep -n "DisplayChangeListenerOps"
git --no-optional-locks cat-file -p HEAD:include/ui/input.h     | grep -n "qemu_input_event_send"
git --no-optional-locks grep -n "bdrv_co_preadv" HEAD -- include/block
```

**dataless 注意事项（已更正）**：本报告初稿称 "qemu-source 约 50% 是 dataless"，**该数字错误**。实际扫描：11281 个 tracked 文件中仅 **230 个（2.0%）** 为 `blocks==0`，且全部落在 `tests/`(213)、`rust/`(7)、`subprojects/`(4)、`scripts/`(2)、`python/`(2)、`tools/`(1)、`target/`(1)。**`audio/`、`block/`、`include/`、`hw/`、`system/` 中为 0 个** —— 本审计范围完全可读。

复核：
```bash
# 抽样 2000 个 tracked 文件
dataless = 0
```

`shasum` 对 dataless 文件会**静默返回空文件哈希 `e3b0c442`**，这一点仍然成立，做完整性校验前务必先确认 `blocks != 0`。

---

## 7. 本报告未宣称的内容

- 未对 `audio_pcm_ops` 的具体字段与行号作断言（源码 dataless，未能读取）。
- 未运行任何 QEMU 进程、未链接任何二进制做 callback 触发实验 —— 因为**没有调用点可供触发**，做动态实验只会复述 §1 的静态结论。
- 未把 mock 的 callback 相关行为计入任何判定。mock 的 `qemu_inprocess_callbacks()` 返回 NULL、且从不触发任何回调，因此它对 callback 接线**零信息量**。
