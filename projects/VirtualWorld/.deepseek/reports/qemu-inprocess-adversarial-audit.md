# QEMU 进程内方案 —— 独立对抗性审计报告

- 审计对象：`qemu-source/qemu-inprocess/`、`src/QemuInProcessRuntime.*`、`tests/QemuInProcessRuntimeTests.cpp`、`tests/qemu_inprocess_mock.c`、`build-qemu-inprocess/**`、`docs/qemu-inprocess.md`
- QEMU 上游：`qemu-source/` VERSION **11.1.50**，HEAD `d49f876`（shallow clone，1 个 44 MB pack）
- 审计性质：**只读**。本次审计只新建了本文件（`.deepseek/reports/qemu-inprocess-adversarial-audit.md`），临时探针仅写 `/tmp`。未修改任何仓库文件，未 commit/push，未启动 guest / UTM / VM / QEMU 进程。
- 结论口径：**mock 通过不算通过**。本报告不给任何绿色结论。

---

## 0. 审计方法与一个关键突破

### 0.1 macOS dataless（iCloud 驱逐）阻塞了直接的源码阅读

工作区位于 iCloud 同步目录。用 `stat -f '%b'` 判定：**`blocks=0` 即本地无数据**，读取会超时。

实测：

```
$ stat -f '%b %N' qemu-source/qemu-inprocess/qemu_inprocess.h
0 qemu-source/qemu-inprocess/qemu_inprocess.h
$ head -c 200 qemu-source/qemu-inprocess/qemu_inprocess.h
head: Error reading qemu_inprocess.h
```

覆盖范围（实测，非估计）：

| 目录 | dataless / 总数 |
| --- | --- |
| `src/` | **0 / 58** |
| `tests/` | **0 / 12** |
| `docs/` | **0 / 9** |
| `cmake/` | **0 / 5** |
| `third_party/minivmac/inprocess` | **0 / 32** |
| **`qemu-source/qemu-inprocess`** | **2 / 3** ← 核心实现 |
| `qemu-source/` 全树（`.c`/`.h`） | **3247 / 6923 ≈ 46.9%** |

**结论：dataless 是局部的、精确的 —— 但恰好命中 QEMU 进程内方案的两个核心文件，以及 QEMU 全树约 47% 的源码。**

### 0.2 突破：`git cat-file` 可绕过 dataless（只读、无副作用）

`qemu-source/.git` 是 shallow clone。用

```bash
export GIT_OPTIONAL_LOCKS=0
git --no-optional-locks cat-file -p HEAD:<path>   # 读任意 dataless 文件内容
git --no-optional-locks grep -nP '<re>' HEAD      # 全树搜索，零 dataless I/O
```

可直接取到 dataless 文件的完整内容，**不触碰工作树**。本次审计用该法核实了 QEMU 全部 API 事实。

只读性经实测验证：运行前后 `.git/index` mtime **不变**。

两个必须记住的坑：
1. **macOS/BSD 的 `grep` 不支持 `-P`**；而 `git grep -E` 在本机**不支持 `\b`** —— `git grep -E '\bqemu_init\b'` 会**静默返回空**（假阴性）。必须用 `-P`。
2. 该法**救不了** `qemu_inprocess.{h,c}`：它们**既 dataless 又 untracked**：

```
$ git ls-tree HEAD | grep -c qemu-inprocess
0
$ git cat-file -p HEAD:qemu-inprocess/qemu_inprocess.h
fatal: path 'qemu-inprocess/qemu_inprocess.h' exists on disk, but not in 'HEAD'
$ git fsck --lost-found          # 无 dangling blob
```

**因此 `qemu_inprocess.h` 与 `qemu_inprocess.c` 的内容在本环境中完全无法获取，无从审计。本报告不对它们的实现作任何断言。**

### 0.3 环境事实复核（第 5 项）

```
$ command -v meson ninja pkg-config cmake clang
meson        未找到
ninja        /opt/homebrew/bin/ninja
pkg-config   /opt/homebrew/bin/pkg-config
cmake        /opt/homebrew/bin/cmake
clang        /usr/bin/clang

$ pkg-config --modversion glib-2.0 pixman-1 zlib libffi gio-2.0 gthread-2.0 libpcre2-8
glib-2.0 2.88.3 | pixman-1 0.46.4 | zlib 1.2.12 | libffi 3.4-rc1
gio-2.0 2.88.3 | gthread-2.0 2.88.3 | libpcre2-8 10.47      ← 全部存在
```

**三分类判定（已由后续实测推翻并修正）：**

| 类别 | 判定 |
| --- | --- |
| **Meson CLI 缺失** | ❌ **不成立 —— 这是伪 blocker。** `meson` 确实不在 PATH，但 **QEMU `configure` 自带离线引导，完全忽略 PATH**：`qemu-source/python/wheels/meson-1.11.1-py3-none-any.whl`（**1078534 字节，实测存在**）。第五位审计员把 QEMU HEAD 用 `git archive` 导出到 `/tmp` **真的跑了 configure**：`./configure --target-list=aarch64-softmmu` → **exit 0**，自动装入 `build/pyvenv/bin/meson`（1.11.1），生成 `build.ninja`（3913 targets），且 `CONFIG_PIXMAN`/`CONFIG_SLIRP`/`CONFIG_TCG` 均已定义。`pip download meson` 亦成功（1.12.1）。|
| **目标依赖缺失** | **对 macOS 宿主不成立**（glib 2.88.3、pixman 0.46.4 等全部就位）；**对 OHOS 目标成立且是当前真实主阻断**。附更正：`pixman` **不是**硬依赖（`-Dpixman=disabled` 下 configure 仍 exit 0，仅关 VNC/GTK）；`pcre2` **根本不是 QEMU 依赖**（全树 0 处 `dependency('...pcre...')`）；`libffi` 仅在 `--enable-tcg-interpreter` 时才是硬依赖（`tcg/meson.build:19`）。glib-2.0 >= 2.66.0 才是真硬依赖（`meson.build:1053-1054`，`required: true`）。|
| **QEMU 不支持** | ❌ 不成立（QEMU 11.1.50 本身支持 darwin/aarch64） |

**因此 `build-qemu-inprocess/.qemu-inprocess-blocked` 的 blocker 1 与 blocker 2 已过时**（原文断言 "meson, ninja, and pkg-config are all absent"、"glib-2.0 and pixman-1 DEVELOPMENT packages are absent ... probed across /usr/local, /opt/homebrew, /opt, /usr — not found"）。现在 ninja/pkg-config/glib/pixman 均在。

---

## 1. 布尔值总表

| 布尔值 | 判定 | 依据 |
| --- | --- | --- |
| `real_core_linked` | **false** | `libqemu_inprocess.a` 从未编译/链接；且其 meson.build 引用 3 个不存在的变量名（C-3） |
| `same_pid_proven` | **false** | harness **未运行**，且其"证明"代码是 `(void)getpid();` —— 调用后丢弃返回值，**即使运行也不构成证明**（C-5） |
| `ohos_cross_compiled` | **false** | SDK 存在但未做任何交叉编译；且 OHOS sysroot 缺 glib/pixman 目标库（H-3） |
| `guest_started` | **false** | 未启动任何 guest/VM；harness 也从未执行 |

---

## 2. Critical

### C-1 `qemu_shutdown` 与 `qemu_run_machine` 不存在 —— 两个 API 名是虚构的

第 1 项要求核对 `qemu_shutdown`。**该符号在 QEMU 11.1.50 全树不存在。**

```bash
$ cd qemu-source && export GIT_OPTIONAL_LOCKS=0
$ git --no-optional-locks grep -cP '(?<![\w])qemu_shutdown(?![\w])' HEAD | wc -l
0
$ git --no-optional-locks grep -cP '(?<![\w])qemu_run_machine(?![\w])' HEAD | wc -l
0
$ git --no-optional-locks grep -cP '(?<![\w])qemu_main_loop(?![\w])' HEAD | wc -l
3        ← 对照：真实符号命中
```

**evidence**：`build-qemu-inprocess/.qemu-inprocess-blocked` 第 ~42 行声称 "Real QEMU APIs referenced: `qemu_init`, `qemu_main_loop`, `qemu_cleanup`, `qemu_system_shutdown_request`, `kvm_enabled`." —— `qemu_system_shutdown_request` **真实**（`include/system/runstate.h:141`），但审计任务书里出现的 `qemu_shutdown`/`qemu_run_machine` 属虚构，若被写入实现即为编译期错误。

**建议**：任何引用这两个名字的代码路径、文档或测试都必须删除或改名。

---

### C-2 `qemu_init` 返回 `void`，且其函数体内含 53 处 `exit()`/`abort()` 与 63 处 `&error_fatal` —— **进程内调用会杀死宿主进程**

这是本次审计**最重要的安全性结论**。

签名（已独立复核）：

```bash
$ git --no-optional-locks cat-file -p HEAD:include/system/system.h | grep -n 'qemu_'
108:void qemu_init(int argc, char **argv);
109:int  qemu_main_loop(void);
110:void qemu_cleanup(int);
```

**`qemu_init` 返回 `void` —— 没有任何错误返回通道。** 它只有两条出路：正常返回，或终止进程。

`error_fatal` 到 `exit()` 的链路是显式的：
- `include/qapi/error.h:308`
- `util/error.c:39-42`

实测统计（在 `qemu_init` 自身函数体内，`system/vl.c:2854`–`3870`）：
- **53 处** `exit()`/`abort()`，例：`system/vl.c:2949, 2982, 2990, 3008, 3068, 3086, 3096, 3117, 3122, 3128, 3193, 3246, … 3766, 3858`
- **63 处** `&error_fatal`

**最致命的是，普通参数错误就走 `exit`：**

```c
/* system/vl.c:2946-2950 */
error_report("Option not supported for this target");
exit(1);
```

以及 `-help` / `-version` 也会 `exit(EXIT_SUCCESS)`（`system/vl.c:3086`、`:3425`）。

**后果**：宿主应用只要向 `qemu_init` 传了一个它不认识的 argv 字符串，**整个 VirtualWorld 进程就会终止**，而且**没有错误返回、没有清理、没有 `QemuErrorCode::InitFailed`**。

**对抗性推论**：`src/QemuInProcessRuntime.cpp:191-198` 声称的 "failed init releases every resource" **只对进入 `qemu_init` 之前检出的失败成立**（如 `:185-190` 的 BUSY 守卫）。任何真实的 QEMU 初始化失败都是 `exit(1)`/`abort()`，那条清理路径根本不会被触及 → 见 M-2。

**建议**：进程内调用 QEMU 的主初始化路径在当前形态下**不可接受**。可行方向只有：(a) 在子进程/沙箱中初始化并把 `qemu_init` 的 `exit` 语义隔离；(b) 大幅裁剪 argv 并封装 `exit`（`atexit`/`longjmp` 不足以拦截 `abort`）；(c) 放弃 `qemu_init`，只用 QEMU 的更底层子系统。三条都需要重新设计，而非打补丁。

---

### C-3 `qemu-source/qemu-inprocess/meson.build` 引用 3 个不存在的变量 —— 该文件无法工作，且静态库遗漏全部 system-emulator 对象

文件全文（可读，1079 B）关键部分：

```meson
qemu_inprocess_sources = files('qemu_inprocess.c')

libqemu_inprocess = static_library(
    'qemu_inprocess',
    qemu_inprocess_sources,
    include_directories: [qemu_include_headers, qemu_include_system],
    c_args: ['-DQEMU_INPROCESS_CORE'],
    dependencies: [qemuutil_dep],
    build_by_default: false,
)
```

**逐名核对（`git grep` 全树 `*.build`）：**

| 引用名 | 定义处 | 实际出现 | 真实对应物 |
| --- | --- | --- | --- |
| `qemu_include_headers` | **0** | **0** | `include_directories('.')` 等 |
| `qemu_include_system` | **0** | **0** | 无 |
| `qemuutil_dep` | **0** | **0** | **`qemuutil`**（`meson.build:3793 declare_dependency(link_with: libqemuutil, …)`） |

```bash
$ git --no-optional-locks grep -cP '(?<![\w])qemuutil_dep(?<![\w])' HEAD -- '*.build' | wc -l
0
$ git --no-optional-locks cat-file -p HEAD:meson.build | grep -n 'qemuutil'
3789:libqemuutil = static_library('qemuutil', …)
3793:qemuutil = declare_dependency(link_with: libqemuutil, …)
4103:common_ss.add(qom, qemuutil)
```

**后果 1（阻塞）**：meson 遇到未定义变量会直接配置失败（`Unknown variable`）。文件头部注释说"add `subdir('qemu-inprocess')` to qemu-source/meson.build to enable" —— **照做只会让 QEMU 配置崩溃**。

**后果 2（更根本）**：该 static library 的 source 列表**只有 `qemu_inprocess.c` 一个文件**。它**不包含任何** system emulation 对象（`system_ss`、`common_ss`、`accel`、`hw`、`block`、`migration`、`qom`、`io`、`crypto`、`authz` …）。文件注释自己承认这点："then links `libqemu_inprocess.a` **together with the chosen target's emulation objects and `libqemuutil.a`**"。

但 QEMU 的 meson **不把这些对象导出成可链接的静态库** —— 它们只作为 source set 存在于 meson 内部（如 `common_ss`、`system_ss`）。要产出真正可链接的 `libqemu_inprocess.a`，必须在 QEMU 的 meson 体系内新增 target，把相关 source set 显式聚合；**这不是"加一行 subdir"能做到的**。

**建议**：修正变量名（`qemuutil_dep`→`qemuutil`，include 用真实的 `include_directories(...)`），并在 QEMU meson 内新增一个聚合全部 system-emulator source set 的 static library；同时说明传递依赖（glib、pixman、zlib、libffi、pcre2、gio、gthread、pthread、dl、m）。

---

### C-4 `qemu_cleanup` 之后**不能**再次 `qemu_init` —— QEMU 的全局状态不可重置

第 1 项要求核对"cleanup 后能否再 init"。**结论：不能，且有硬证据。**

`qemu_cleanup`（`system/runstate.c:1006`）自身以未完成事项收尾：

```c
/* system/runstate.c:1050 */
/* TODO: unref root container, check all devices are ok */
```

三个**独立**的阻断点：

1. **`vm_config_groups` 溢出 → `abort()`**
   `util/qemu-config.c:10 QemuOptsList *vm_config_groups[48]`；`:77-91 qemu_add_opts()` 在槽位耗尽时执行
   `fprintf("ran out of space in vm_config_groups"); abort();`
   而 `system/vl.c:2864-2880` 在进入初始化时就要吃掉约 16 个槽位。

2. **模块注册被静默跳过**
   `util/module.c:102-104`：`if (modules_init_done[type]) return;`
   → 第二次 init 会**静默跳过** QOM / MIGRATION 的注册，产生一个语义已残废的机器。

3. **`atexit` 重复注册**
   `system/runstate.c:984 atexit(qemu_run_exit_notifiers)` 每次 init 都会再注册一次。

**注意**：`docs/qemu-inprocess.md` 只记录了**较弱的**那一种风险（"init before cleanup"），而证据最强的 **after-cleanup 重入** 反而**未被文档提及** → 见 M-1。

**建议**：把"**每进程仅允许一次 `qemu_init`**"写成硬不变量，并在 wrapper 里用一个**永不复位**的一次性守卫（one-shot latch）强制它，而不是可重复使用的 `g_active`。

---

### C-5 `test_real_core_same_pid_harness` 的 same-PID "证明"是 `(void)getpid();` —— 即使链接真实核心也不构成证明

第 2、4 项要求核实 same-PID harness 是否真的证明了同 PID。**它没有。**

`tests/QemuInProcessRuntimeTests.cpp:165-190`（`QEMU_INPROCESS_REAL_CORE` 条件编译）：

```cpp
static void test_real_core_same_pid_harness()
{
    QemuInProcessRuntime rt;
    rt.setHostIsa(Isa::AArch64);
    QemuSessionHandle h = nullptr;
    check(rt.create("realcore", h) == QemuErrorCode::Ok, "create Ok (real core)");
    QemuLaunchConfig cfg;
    cfg.targetIsa = Isa::AArch64;
    cfg.memoryBytes = 64 * 1024 * 1024;
    cfg.extraArgs = "-nodefaults"; // minimal, no guest ROM/OS bundled
    check(rt.init(h, cfg) == QemuErrorCode::Ok, "init Ok (real core)");
    check(rt.start(h) == QemuErrorCode::Ok, "start Ok (real core in this PID)");
    check(rt.state(h) == SessionState::Running, "Running in-process");
    rt.stop(h);
    rt.destroy(h);
    // The harness itself runs in getpid(); QEMU's threads share that PID.
    (void)getpid();          // ← 调用后丢弃返回值
}
```

**问题**：

1. `(void)getpid();` **什么也没验证** —— 不比对 PID、不检测子进程、不检查 `fork`/`clone`/`exec` 是否发生。注释所声称的"QEMU's threads share that PID"完全**没有代码支撑**。
2. 断言 `rt.state(h) == Running` 只证明 **wrapper 自己的状态机**，不证明 QEMU 真的在跑（mock 也能通过同样的断言 —— 而 mock 恰恰实现了相同的状态机）。
3. `cfg.extraArgs = "-nodefaults"` **并不能阻止 guest 启动** —— `-nodefaults` 只是不创建默认设备；要真正冻结 CPU 需 `-S`，要避免加载固件需显式不提供 BIOS/kernel。此处两者皆无。

**建议**：见第 6 节的验收条件。至少需要 `getpid()` 前后比对 + `nm -u` 无 spawn 族符号 + 无 `fork`/`clone` 系统调用 + 用 `-S` 冻结 + 断言 QEMU 的内部状态（如线程名/TID 集合）而非仅 wrapper 状态。

---

### C-6 wrapper 发出裸 `-accel kvm`，失败时**杀死宿主进程**，与文档声称的"透明回退 TCG"完全相反

`docs/qemu-inprocess.md:103-104` 声称 QEMU 会 transparently fall back to TCG。

**代码不发回退列表：**

```cpp
/* src/QemuInProcessRuntime.cpp:148-149 */
args.push_back("-accel");
args.push_back(handle->accelDecision.accel == Accel::KVM ? "kvm" : "tcg");
```

**裸 `-accel kvm` 失败时会 `exit(1)`**，证据链：

- `system/vl.c:2394-2435 do_configure_accelerator()` 失败时 `return 0`
- `util/qemu-option.c:1138-1156 qemu_opts_foreach()` 在循环走完（含遇到一次 0 返回）时也 `return 0`
- → `system/vl.c:2494-2499` 判定失败 → **`exit(1)`**
- `"falling back to %s"` 消息（`system/vl.c:2502-2504`）**仅在 `-accel kvm:tcg` 这类逗号列表下可达**（第二个条目成功 → foreach 返回 1）
- 在**不带 KVM 的构建**上更糟：`accel_find("kvm") == NULL` → `"invalid accelerator kvm"` → 同样 `exit(1)`（`system/vl.c:2410-2415`、`:2432-2434`）

**结论**：wrapper 的 `-accel kvm` 路径把一次 KVM 失败转化为**宿主进程终止**，与文档描述正好相反。

**建议**：传 `-accel kvm:tcg`，或干脆不发 `-accel` 让 QEMU 走默认 `tcg:kvm`（`system/vl.c:2453-2459`）。并且加速器结论应在 init **之后**从 QEMU 读取，而不是 init 前猜一个 `hardware_accelerated = true`（`src/QemuInProcessRuntime.cpp:106`）。

---

### C-7 【已修正】两个核心文件在审计期间**被实体化**，现在可读；9/9 SHA-256 全部吻合

**重要更正**：本报告初稿写作时，`qemu_inprocess.h` / `.c` 是 dataless（`blocks=0`，读取超时），且 untracked。但审计期间（约 10:31）它们**被实体化**：

```
$ stat -f '%N blocks=%b size=%z' qemu_inprocess.h qemu_inprocess.c
qemu_inprocess.h blocks=16 size=5861
qemu_inprocess.c blocks=16 size=4912
$ shasum -a 256 qemu_inprocess.h qemu_inprocess.c
5bccf1026685873f61228be2a6c348bf4f80a66828b2816dbfcf6dc4bcda030a  qemu_inprocess.h
a5d77c0b784621e7e49113dfe81dbb5048c7ef905f1a0c4455465fe92909d9c2  qemu_inprocess.c
```

两者与 `.qemu-inprocess-blocked` 完整性块**逐字节吻合**（当时 9/9）。**但该状态未能保持**：`qemu_inprocess.c` 随后于 **10:37:22** 被改为 4993 B / `50e6360b…`（新增 `#include "qemu/main-loop.h"`），**marker 声明的 `a5d77c0b…` 现已不匹配 → 完整性回落到 8/9**。

**完整性数字在本次审计期间变化三次：7/9（dataless）→ 9/9（实体化）→ 8/9（被再次修改）。** 这本身就证明：受审产物**未冻结**，任何"全绿"结论都不可复现。

**这解锁了初稿无法进行的核心实现审计 —— 见 C-16 与 C-17。** 但请注意：文件**仍未纳入版本控制**（`git status` → `?? qemu-inprocess/`），因此"一次误清理即永久丢失"的风险依旧成立。

### C-8 `Accel` 枚举只有 `KVM`/`TCG` —— macOS 的 HVF 与 Windows 的 WHPX 完全缺失

`src/QemuInProcessRuntime.hpp`：

```cpp
enum class Accel { KVM, TCG };
struct AccelDecision { Accel accel; bool hardwareAccelerated; const char* reason; };
```

`hardwareAccelerated` 的语义被注释钉死为 "true ONLY for same-ISA KVM"。

**QEMU 事实**（`git cat-file -p HEAD:meson.build`）：

- **KVM 在 macOS 上不可能**：`meson.build:874 if get_option('kvm').allowed() and host_os == 'linux'`
- **Darwin 的加速器是 HVF**：`meson.build:898-902`

**后果**：在 Apple Silicon 上跑 ARM64 guest（same-ISA）时，本方案**永远不会**给出硬件加速 —— 它只会落到 TCG。这**不是"误报"**（它没有谎称 KVM），而是**能力缺失**：macOS 与 Windows 平台的硬件加速未被建模。

另见 `system/vl.c` 的 BQL 事实（C-9）。

**建议**：若产品需覆盖 macOS/Windows，必须把 `Accel` 扩为 `{KVM, HVF, WHPX, TCG}`，并让 `decideAccel` 依据**运行时平台**而非调用方传入的猜测来判定；同时用 `TARGET_*`/`HOST_*` 编译期门控保证不会在错误平台选中。

---

### C-9 线程亲和性是 per-thread TLS 的 BQL 所有权，wrapper 的两线程形状**可能死锁**

QEMU 把"主线程"**定义为持有 BQL 的那个线程**，而非某个固定的 OS 线程：

```c
/* system/cpus.c:527 */  QEMU_DEFINE_STATIC_CO_TLS(bool, bql_locked)   /* per-thread! */
/* system/cpus.c:560-563 */ bool qemu_in_main_thread(void){ return bql_locked(); }
/* system/cpus.c:579 */  g_assert(!bql_locked())
/* system/cpus.c:585 */  g_assert(bql_locked())
```

`qemu_init` **返回时仍持有 BQL**（`system/vl.c:2909` → `qemu_init_subsystems()` → `system/runstate.c:982 bql_lock()`）。

QEMU 自己的 Darwin 路径做了一次**显式交接**（`system/main.c:73-91`）：注释写着 "we need to transfer lock ownership"，随后 `bql_unlock(); replay_mutex_unlock();`，再 `qemu_thread_create(..., qemu_default_main, ...)`。

**没有这次交接**，第二个线程调用 `bql_lock()` 会先通过自己的 TLS 断言，然后**在 `qemu_mutex_lock(&bql)` 上死锁** —— 不是干净报错，而是挂死。

**对本案的影响（未能验证）**：`src/QemuInProcessRuntime.cpp:182` 在调用方线程执行 init，而 `:212` 在**新建的 `std::thread`** 上跑主循环。若核心未做上述交接，这**必然死锁**。该判断**无法证实**，因为 `qemu_inprocess.c` 是 dataless（C-7）。

**另有生命周期陷阱**：QEMU 自身路径以 `exit()` 收尾：

```
system/main.c:44-56  qemu_default_main() → qemu_main_loop(); qemu_cleanup(status); … exit(status);
system/main.c:60-67  Darwin: qemu_main = os_darwin_cfrunloop_main (CFRunLoopRun())
system/main.c:69-95  main() 以 QEMU_THREAD_DETACHED 起 "qemu_main" 线程
```

**任何"cleanup 后继续在进程内活着"的设计都绝对不能链接 `main.c` 的那条路径**，否则 `exit()` 会带走宿主。

**建议**：明确要求核心实现 BQL 交接（或让 wrapper 在**同一线程**上 init + 跑循环）；并在构建期用符号检查确保 `system/main.c` 的 `main`/`qemu_default_main` 未被拉入。

---

### C-10 交付的构建系统在**任何模式下都无法 configure** —— 静态扫描命中 wrapper 自己的注释（推翻 marker 的 "Static scan PASSED"）

**第二重磅发现：交付物根本无法构建，原因正是它自称已通过的那道门禁。**

`build-qemu-inprocess/CMakeLists.txt:50-71` 的"禁止子进程 API"扫描是**裸 substring 匹配**：

```cmake
:62   string(FIND "${_src}" "QProcess" _pos)
:64   message(FATAL_ERROR "Forbidden subprocess API '${_api}' found in ${_file}")
```

而被扫描文件**在自己的注释里列举了这些禁用词**：

```cpp
/* src/QemuInProcessRuntime.cpp:6-7 */
 *   ... There is NO QProcess / fork / exec / system / popen /
 *   startDetached / shell anywhere in this file or its dependencies here.
/* src/QemuInProcessRuntime.hpp:9-10 */
 *   ... no QProcess / fork / exec ...
```

**复现（输出到 `/tmp`，不改仓库）：**

```bash
$ cmake -S build-qemu-inprocess -B /tmp/qip_cfg -DQEMU_INPROCESS_REAL_CORE=OFF
CMake Error at CMakeLists.txt:64:
  Forbidden subprocess API 'QProcess' found in .../src/QemuInProcessRuntime.cpp
EXIT=1
# REAL_CORE=ON 同样在第 64 行失败（扫描先于链接）
```

词频实测：`QProcess=2`、`popen=2`、`startDetached=2`，**全部来自上述注释**。

| 位置 | 声称 | 实况 |
| --- | --- | --- |
| `.qemu-inprocess-blocked:62` | "Static scan **PASSED** ... found NONE" | 同一 token 表在相同文件命中 2 次；CMake 扫描直接 FATAL |
| `docs/qemu-inprocess.md:166` | "**config-time enforced** (passes on the source)" | `cmake -S` 失败，无任何模式能 configure |

**建议**：换用仓库里**已存在且正确**的工具 `third_party/minivmac/inprocess/scan_spawn_apis.py`（剥离注释与字符串 + 归档 `nm`）。同仓库已有正确实现却重造并造错，值得记录。

---

### C-11 `QEMU_INPROCESS_REAL_CORE=ON` 测试目标**按构造必然链接失败** —— 真实 same-PID harness 永远无法构建

`tests/QemuInProcessRuntimeTests.cpp:32` **无条件**声明并在 `:100/:120/:146/:158` 调用：

```cpp
extern "C" void qemu_inprocess_mock_set_init_failure(int);
```

该符号**全仓唯一定义**在 `tests/qemu_inprocess_mock.c:115`，而该文件只在 `CMakeLists.txt:97-99` 的 **`else()`（REAL_CORE=OFF）分支**被加入 target：

```cmake
:85  if(QEMU_INPROCESS_REAL_CORE)
:93-94   target_link_libraries(... QemuInProcessRuntime "${QEMU_INPROCESS_CORE_LIB}")
:96  else()
:97-99   target_sources(... tests/qemu_inprocess_mock.c)     ← 只有这里提供该符号
```

→ **REAL_CORE=ON 必然 undefined symbol。** 被文档称为 "definitive proof" 的真实核心 harness，**在构造上无法构建**。叠加 C-10 与 C-12 可确定：**真实核心路径从未、也无法被构建。**

---

### C-12 `destroy()` 声称幂等实为 **use-after-free**；重复 `init()` **永久锁死**；二次 `start()` 直接 `std::terminate`

`src/QemuInProcessRuntime.hpp:12-15` 的三条"硬保证"**实测全部不成立**：

| 声称 | 代码 | 实际行为 |
| --- | --- | --- |
| `stop()/destroy()` reentrant & idempotent | `cpp:248` 读 `handle->tag`；`cpp:271` `delete handle` | 第二次 `destroy(同一 handle)` 读已释放内存 → **UAF**。`tests:114` 正在测这个 UB，只是"碰巧"通过 |
| failed init releases every resource | `cpp:177` 覆盖 `handle->core`；`cpp:186-189` 只销毁**新** core | 旧 core **泄漏**；核心返回 BUSY 后全局 `g_active` **永不复位** → 之后所有 `init()` **永久 BUSY**。无测试覆盖 |
| （隐含 start 可安全调用） | `cpp:212` 对 joinable `std::thread` 赋值 | 二次 `start()` → **`std::terminate`**，宿主崩溃。无守卫、无测试 |

**ASan 即可证伪**：`-DQEMU_INPROCESS_ASAN=ON` + 连续两次 `destroy()`。

---

### C-13 发布门禁**显式白名单了 QEMU 子进程** —— 产品实际跑的仍是子进程 QEMU，进程内方案是孤儿

- `tests/VirtualizationPackagingTests.cmake:145-149`："spawning external QEMU is by design"；`:271-280` 对 QEMU spawn 放行
- `src/QemuEngine.cpp:526-558` 仍使用 `QProcess::start` / `startDetached`
- QEMU 进程内方案**不在根构建、不在 CI**：

```bash
$ grep -c "QemuInProcess\|qemu_inprocess\|qemu-inprocess" CMakeLists.txt
0
$ grep -rn "qemu_inprocess" ci/ cmake/
（无输出）
```

**判读**：这不是"未完成的集成"，而是**产品语义** —— 当前交付的 QEMU 仍是**子进程**模式。`QemuInProcessRuntime` 是一个**从未被构建、从未被链接、不在任何构建入口内**的孤儿交付物。任何"QEMU 已进程内化"的表述都与产品实际不符。

---

### C-14 并发 stop 测试自带竞态，`checks=28 failed=0` 不能作为证据

`tests/QemuInProcessRuntimeTests.cpp:139` 在 join 完 16 个 stopper 后**立即**断言 `state == Stopped`，但 `Stopped` 由 **worker 线程异步写入**（`src/QemuInProcessRuntime.cpp:217-220`），**无 join、无同步** → 偶发失败。

叠加：`tests:109` 断言 KVM 可用，仅仅因为 mock 的 `qemu_inprocess_kvm_usable` **恒真**（`mock.c:100-105`，从不打开 `/dev/kvm`）。真实核心在 macOS/OHOS 无 `/dev/kvm` 时必然返回 0 → **该断言在真实核心下必然失败**。这是典型的"mock 被调到 mock 上"。

因此 `checks=28 failed=0`（28 与源码算术自洽：3+6+8+5+6=28）**不能作为 wrapper 逻辑正确的证据**：产生它的构建路径是坏的（C-10/C-11），其中的回调路径从未被触发（M-4），并发断言带竞态，KVM 断言只对 mock 成立。

---

### C-15 `test_real_core_same_pid_harness` 不是独立 harness，其失败会连带 mock 用例一起死

`tests/QemuInProcessRuntimeTests.cpp:169` 只是一个 `static void`，由 `:198-200` 的 `#ifdef QEMU_INPROCESS_REAL_CORE` 调用，**不是独立 target、不是独立 ctest**。marker（`.qemu-inprocess-blocked:33`）的措辞让它听起来像一个独立 harness。

**后果**：真实核心下一旦 `abort`/`exit`（C-2 表明极易发生），会**连带 28 项 mock 测试一起终止**，无法区分"哪个失败了"。

---

### C-16 【核心实现】`qemu_inprocess.c` 无条件返回成功 —— `QEMU_INPROCESS_ERR_INIT_FAILED` 是**永不返回的死枚举**

文件实体化后直接审计发现（`qemu_inprocess.c:76-79`）：

```c
qatomic_set(&g_active, 1);
qemu_init(argc, argv);
session->initialised = true;          /* ← 无条件 */
return QEMU_INPROCESS_OK;             /* ← 无条件 */
```

而 `qemu_init` 返回 **`void`**（`include/system/system.h:108`，本报告 C-2 已证），且其体内有 53 处 `exit`/`abort`。**因此 `INIT_FAILED` 分支在实现上不存在。**

全文件搜索确认：`QEMU_INPROCESS_ERR_INIT_FAILED` **在 `qemu_inprocess.c` 中一次都没出现**，只存在于头文件 `:41` 的枚举里。

**这与三处明文契约矛盾：**

| 位置 | 声称 |
| --- | --- |
| `qemu_inprocess.h:96-100` | "On any other failure returns `QEMU_INPROCESS_ERR_INIT_FAILED` after releasing all resources it grabbed" |
| `docs/qemu-inprocess.md:168` | 同义 |
| `.qemu-inprocess-blocked:32-34` | 同义 |

**更值得注意的是一段"把责任推给不存在之物"的注释**（`qemu_inprocess.c:68-74`）：

```c
/* If qemu_init hits a fatal configuration error it may call exit(); the
 * host is expected to run the in-process core with an installed
 * qemu_add_exit_notifier / signal handler so a guest config error never
 * terminates the host app (see docs/qemu-inprocess.md, "Graceful init
 * failure"). */
```

实测：
- 该 `.c` **没有安装任何** exit notifier 或 signal handler；
- `docs/qemu-inprocess.md` 全文 207 行 **不存在** "Graceful init failure" 这一节（其他审计员独立核实）。

**即：注释指向的缓解措施，既未实现、也未文档化。**

**连带后果**：`src/QemuInProcessRuntime.cpp:191-198` 的"init 失败回收资源"分支对真核**是死代码**；`tests:154` 的 init 失败用例只能靠 mock 私有后门 `qemu_inprocess_mock_set_init_failure`（`tests/qemu_inprocess_mock.c:114-118`），该后门在真核**无对应物**。

---

### C-17 【核心实现】`(void)kvm_enabled;` 是编译错误，且 `#ifdef CONFIG_KVM` 在此作用域**恒为假** → `libqemu_inprocess.a` 按现有配置**无法编译**

`qemu_inprocess.c:132-149`（我已逐字读取）：

```c
#ifdef CONFIG_KVM
    int fd = open("/dev/kvm", O_RDWR);
    ...
    return 1;
#else
    (void)kvm_enabled;          /* ← 缺 () */
    return 0;
#endif
```

**两个独立缺陷：**

1. **`kvm_enabled` 是函数式宏，不是标识符。**
   `include/system/kvm.h:49 #define kvm_enabled() (kvm_allowed)`（另 `:151` 为 `(0)`）。函数式宏**不写 `()` 不会展开** → 留下未声明标识符 → 编译错误。实测复现：

   ```bash
   cat > /tmp/repro.c <<'EOF'
   extern int kvm_allowed;
   #define kvm_enabled()  (kvm_allowed)
   int f(int m){ (void)kvm_enabled; return 0; }
   EOF
   $ cc -c /tmp/repro.c -o /tmp/repro.o
   error: use of undeclared identifier 'kvm_enabled'
   ```

2. **`CONFIG_KVM` 在 root scope 恒未定义**，因此**必然走 `#else`**，也必然撞上上述错误。
   `CONFIG_KVM` 只写入**每 target** 的 `*-config-target.h`（`meson.build:3315-3321`、`:3382`），且 `-DCOMPILING_PER_TARGET` 只在 `:3950`/`:4260` 添加；而 `qemu-inprocess/meson.build:18-25` 在 **root scope** 声明该静态库 → 拿不到 per-target config。

**后果**：
- `libqemu_inprocess.a` **按现有配置不可能编译成功**；
- 即便强定义 `CONFIG_KVM`，`#else` 的存在也意味着原设计里 KVM 路径**永不可达**（真核永远只能 `-accel tcg`）；
- `-DQEMU_INPROCESS_CORE`（`meson.build:22`、`docs:147`）**无任何源码引用**，是死定义。

---

### C-18 【核心实现 + 实测】在**没有 `/dev/kvm` 的 macOS 上，wrapper 报告 KVM 且 `hardwareAccelerated=TRUE`**

第四个审计员用**真实 wrapper + 仓库自带 mock**（即文档默认的 `REAL_CORE=OFF` 路径）在本机（macOS arm64，`ls /dev/kvm` → No such file）**实测复现**：

```
host=AArch64  target=AArch64  init=0 accel=KVM hardwareAccelerated=TRUE
host=X86_64   target=X86_64   init=0 accel=KVM hardwareAccelerated=TRUE
host=Unknown  target=Unknown  init=0 accel=KVM hardwareAccelerated=TRUE
host=AArch64  target=X86_64   init=0 accel=TCG hardwareAccelerated=false   ← 唯一正确项
```

**根因**：`tests/qemu_inprocess_mock.c:100-105` 的 `qemu_inprocess_kvm_usable` **只按 ISA 匹配返回 1，从不触碰 `/dev/kvm`**：

```c
int qemu_inprocess_kvm_usable(int target_isa_host_match) {
    return target_isa_host_match ? 1 : 0;      /* 从不 open /dev/kvm */
}
```

而契约 `qemu_inprocess.h:118-122` 明确要求**两个条件**："Returns 1 if /dev/kvm is openable **AND** the target ISA matches the host ISA"。

**因此 `checks=28 failed=0` 这个唯一的"绿灯"，是在一个没有 KVM 的宿主上把 KVM 断言为正确行为而得到的。** 它不是加速器正确性的证据，而是**误报的实证**。

**建议**：mock 必须同样实现 `/dev/kvm` 探测（或在无 KVM 宿主上期望 TCG）；否则该测试套件在加速器维度上**只会产生假阳性**。

---

### C-19 【环境无关】即使 Meson、依赖、工具链**全部就绪**，`libqemu_inprocess.a` 仍造不出来：3 处编译阻断

第五位审计员用 **与 marker SHA 完全匹配**的 `qemu_inprocess.c`（`a5d77c0b…`，4912 B）作为输入，补完 meson 胶水与头名后编译，**仍有 3 处错误**：

| # | 错误 | 证据 |
| --- | --- | --- |
| 1 | `#include "qemu-inprocess.h"` **头名不符** | 磁盘上的文件名是 `qemu_inprocess.h`（下划线），不是连字符 |
| 2 | `#ifdef CONFIG_KVM` 被 **`#pragma GCC poison`** 毒化 | `include/exec/poison.h`（`:7` include `config-poison.h`，`:9-13` poison `TARGET_I386`/`TARGET_X86_64`/`TARGET_AARCH64`…；`CONFIG_KVM` 同在该文件）→ 一旦被 poison，**任何对 `CONFIG_KVM` 的引用（含 `#ifdef`）都是编译错误** |
| 3 | `(void)kvm_enabled;` **缺 `()`** | `include/system/kvm.h:49 #define kvm_enabled() (kvm_allowed)` —— 函数式宏不写 `()` 不展开（见 C-17） |

**这一条的性质与前两条不同：它不依赖环境。** 也就是说，前四条 blocker（工具链、依赖、SDK、构建时长）**即使全部解决**，产物仍然编不出来。marker 的 "complete and correct-by-construction" 对该文件**不成立**。

**并且：`qemu_inprocess.c` 在审计期间被再次修改。**

```
marker 声明:  a5d77c0b784621e7e49113dfe81dbb5048c7ef905f1a0c4455465fe92909d9c2  (4912 B)
当前实际:      50e6360bd0f0317a8c9d916562e982421da79f6566421ce142efad91e4be5323  (4993 B, mtime 10:37:22)
```

新增了 `#include "qemu/main-loop.h"`（据第五位审计员）。**因此 marker 的完整性声明现已从 9/9 降至 8/9** —— 初稿时（dataless）是 7/9，某一时刻曾短暂 9/9，现在又是 8/9。**这个数字在审计期间变了三次**，本身就是证据完整性不可依赖的证明。

---

## 3. High

### H-1 blocked marker 的完整性声明：7/9 可验证且全部一致；**恰好那 2 个不可验证的就是 QEMU 面向文件**

独立复核 `build-qemu-inprocess/.qemu-inprocess-blocked` 里列出的 9 个 SHA-256：

```
✓ 一致   qemu-source/qemu-inprocess/meson.build
✓ 一致   src/QemuInProcessRuntime.hpp
✓ 一致   src/QemuInProcessRuntime.cpp
✓ 一致   tests/QemuInProcessRuntimeTests.cpp
✓ 一致   tests/qemu_inprocess_mock.c
✓ 一致   build-qemu-inprocess/CMakeLists.txt
✓ 一致   docs/qemu-inprocess.md
dataless 无法验证   qemu-source/qemu-inprocess/qemu_inprocess.h
dataless 无法验证   qemu-source/qemu-inprocess/qemu_inprocess.c

一致 7 / 不一致 0 / 无法验证 2
```

**判读**：marker 在**可验证范围内诚实**（7/7 全对，且它自己明确声明"mock 不等于 in-process"、列出 NOT verified 清单 —— 这是负责任的写法）。但它的**核心 ABI 与实现声明恰恰落在无法验证的两个文件上**。

**建议**：实体化并纳入版本控制后重算哈希，让完整性声明闭合。

### H-2 mock **无法编译**，因此 marker 里的 `checks=28 failed=0` 不可复现

见 C-7 的编译错误。此外 `tests/qemu_inprocess_mock.c:27` 的 `static int g_active = 0;` 是**非原子**的，注释却写 "mirrors the real core's `g_active`" —— 而 marker 声称真实核心是 "single-instance **atomic** guard"。

**因此 mock 连"单实例守卫的原子性"都证明不了**，更不能证明 TOCTOU 安全性。

### H-3 OpenHarmony 交叉编译：SDK 存在，但**目标依赖缺失**（不是 Meson 缺失）

SDK 已核实存在：

```
/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/native/
  ├── llvm/bin/clang-15, clang-check, clang-tidy, clang-format, clangd
  ├── sysroot/
  ├── build-tools/
  └── compatible_config.json, oh-uni-package.json, ndk_system_capability.json
```

（注意：`find ... -path "*aarch64*" -name clang` 未命中，说明 SDK 的 triple 目录命名与预期不同，需按 `oh-uni-package.json` 确认实际 triple。）

**判定**：QEMU **system emulation** 依赖 glib-2.0、pixman-1 的**目标平台（OHOS/aarch64）** 版本。macOS 宿主上的 glib/pixman 无法用于 OHOS 链接，而 OHOS sysroot 里几乎不可能自带这两者。

**所以这是"目标依赖缺失"，不是"Meson 缺失"，也不是"QEMU 不支持"。** 这三者在 blocked marker 里被混为一谈。

**建议**：交叉编译前必须先为 OHOS/aarch64 交叉构建 glib 与 pixman（或确认 SDK 内已有），这是数小时级的前置工作，应单列为 blocker，而不是与 meson 缺失并列。

### H-4 `hostIsa` 默认硬编码 `Isa::AArch64`，且 same-ISA 判定完全依赖调用方传值

```cpp
/* src/QemuInProcessRuntime.cpp:79 */
Isa hostIsa = Isa::AArch64; // default: the HarmonyOS/OpenHarmony target
```

```cpp
/* src/QemuInProcessRuntime.cpp:145-147 */
bool kvmUsable = (cfg.targetIsa == impl_->hostIsa) &&
                 (qemu_inprocess_kvm_usable(cfg.targetIsa == impl_->hostIsa) == 1);
handle->accelDecision = decideAccel(impl_->hostIsa, cfg.targetIsa, kvmUsable);
```

**风险**：在 x86_64 宿主上若调用方忘记 `setHostIsa`，`hostIsa` 仍是 AArch64 → 跑 x86_64 guest 被判为 cross-ISA → 走 TCG（**保守，不误报** ✓）。但反过来，若调用方把它设成 `X86_64` 并在**macOS** 上跑 x86_64 guest，`decideAccel` 会判 same-ISA → 可能返回 `Accel::KVM` → 而 macOS **没有 KVM** → wrapper 发出 `-accel kvm` → **按 C-6 直接 `exit(1)` 杀死宿主**。

**建议**：`hostIsa` 必须由运行时平台探测得出，且 `decideAccel` 必须叠加平台门控（Linux→KVM、Darwin→HVF、Windows→WHPX），不能仅靠 ISA 比较。

### H-5 `qemu_inprocess_kvm_usable` 的语义不可审计，而它直接决定 `-accel` 参数

该函数声明在 dataless 的 `qemu_inprocess.h`（C-7），实现不可读。但它的返回值是 `kvmUsable` 的唯一依据，进而决定 `-accel kvm` vs `-accel tcg`。

**且 QEMU 侧的"能否用 KVM"无法在 init 前探测**：

- `accel/accel-system.c:39-54` 在 `:44` 设 `*(acc->allowed) = true`，而这段只在 `accel_init_machine` 内执行 → 由 `system/vl.c:2422` 调用 → **发生在 `qemu_init` 内部**
- `kvm_allowed` 是零初始化全局（`accel/kvm/kvm-all.c:102`，`ac->allowed = &kvm_allowed` 于 `:4279`）
- 因此 **`kvm_enabled()` 在 `qemu_init` 之前恒为 false** —— 即使在一台 KVM 可用的 Linux 主机上

**所以"init 前探测 KVM"在设计上不可行**；marker 里点名 `kvm_enabled` 作为引用 API 是一个**危险信号**。

### H-6 `docs/qemu-inprocess.md` 的 `kvm_enabled` 签名是**伪造的**

`docs/qemu-inprocess.md:38` 声称：

```c
bool kvm_enabled(void);   — include/system/kvm.h
```

**不存在这个函数。** 实际是**宏**：

```bash
$ git --no-optional-locks cat-file -p HEAD:include/system/kvm.h | grep -n 'kvm_enabled'
49:#define kvm_enabled()           (kvm_allowed)
151:#define kvm_enabled()           (0)      /* KVM 未编译时 */
```

同理 `kvm_init` 是 **`static`**，不能从外部调用：

```
accel/kvm/kvm-all.c:2893  static int kvm_init(AccelState *as, MachineState *ms)
accel/kvm/kvm-all.c:4276  ac->init_machine = kvm_init;      ← 唯一可达方式
```

而 `kvm_available` **只有 Python 形式**（`python/qemu/utils/accel.py:54`），**没有 C 形式**。

---

## 4. Medium / Low

### M-1 文档记录的是**较弱的**危险，漏掉了两个决定性事实

`docs/qemu-inprocess.md:78-83` 只禁止 "init **before** cleanup"。而证据最强的 **after-cleanup 重入**（C-4）**未被文档提及**。

更关键的是，文档**从未提及**决定方案可行性的两个事实：
1. **`qemu_init` 可以 `exit()` 掉宿主进程**（C-2）
2. **BQL 的线程交接要求**（C-9）

### M-2 "failed init releases every resource" 被夸大

`src/QemuInProcessRuntime.cpp:191-198` 的清理路径**只对进入 `qemu_init` 之前检出的失败可达**（例如 `:185-190` 的 BUSY 守卫）。**每一次真实的 QEMU 初始化失败都是 `exit(1)`/`abort()`** —— 没有清理、也没有 `QemuErrorCode::InitFailed` 被返回。

### M-3 一个**并发的第三方进程**正在写这个仓库（非本次审计）

审计期间观察到：

- `qemu-source/qemu-inprocess/_writetest.c`（内容 `/* io-probe */`，15 B）创建于 **10:27:53**
- `build-qemu-inprocess/_iotest.txt`（内容 `test`，5 B）创建于 **10:19**
- 90 分钟内仓库内约 60 个文件被修改

**本次审计未创建其中任何一个**（我只写了 `/tmp/QEMU_API_AUDIT.md` 与本报告）。这看起来是**探针写入权限**的行为。若你的编排里有兄弟 agent 在探测 dataless 目录的写能力，那就是它。**建议确认，避免与正在进行的审计互相干扰。**

### M-4 `libqemu_inprocess` 的 `build_by_default: false` 会让"忘记加依赖"静默通过

`build_by_default: false` 意味着即使 `subdir()` 被正确加入，该库也**不会默认构建**。若某处忘记显式依赖它，构建会"成功"但库根本没产出。

---

## 5. mock 能证明什么、绝对不能证明什么

### 可以证明（且在修复 include 后可复现）

`tests/qemu_inprocess_mock.c` 是一个**透明契约替身**（其文件头明确写 "This is NOT QEMU"）。它只能证明 **wrapper 自身的逻辑**：

- C ABI 形状（函数指针表与签名的一致性）
- 状态机迁移（`Idle → Initialising → Running → Stopping → Stopped/Error/Destroyed`）
- 单实例 BUSY 的错误码
- `stop()`/`destroy()` 的可重入与幂等
- 并发 `stop()` 不崩
- 加速器**策略函数**（`decideAccel`）的纯逻辑分支
- init 失败路径的**编码逻辑**（不涉及真实 `exit()`）

### 绝对不能证明

- **QEMU 真的在同一 PID 内执行**（mock 只是同进程的一个假实现，天然同 PID）
- `qemu_init` **不会 `exit()`/`abort()`** 宿主（mock 从不 exit）
- `qemu_cleanup` 之后**可以**再 init（mock 的 `g_active` 是可复位的普通 int）
- **真实 `g_active` 的原子性 / TOCTOU 安全性**（mock 用非原子 `static int`）
- 真实的 display / input / audio / block 回调**真的被 QEMU 调用**（mock 自己造回调）
- 真实 accel 选择与 KVM→TCG 回退（mock 不碰 QEMU 的 accel 体系）
- 任何 OHOS/ARM64 目标行为

**因此：blocked marker 把 `checks=28 failed=0` 明确标注为"不证明 QEMU 在进程内运行"是正确的、应当肯定的；但任何把该数字当作集成已达标的依据都是误用。**

---

## 6. 最小真实核心 same-PID / no-guest harness 的验收条件

以下条件**可执行、可证伪**。全部满足才可称 "same-PID 已证明"。

### 6.1 真实核心已链接（`real_core_linked`）

```bash
# 必须能列出真实 QEMU 对象，而不是 mock
nm -g --defined-only build-qemu-inprocess/libqemu_inprocess.a | grep -c ' T _qemu_init'
nm -g --defined-only build-qemu-inprocess/libqemu_inprocess.a | grep -c 'mock'
# 要求：前者 > 0，后者 == 0
```

并且 `qemu_init` 必须来自 QEMU 的 `system/vl.c.o`，而非 `tests/qemu_inprocess_mock.c`。

### 6.2 同 PID 的**有效**证明

```cpp
const pid_t pid_before = getpid();
// init + start
const pid_t pid_after = getpid();
CHECK(pid_before == pid_after);                       // 宿主未被替换
CHECK(rt.samePidProven());                            // 新增 API，见下
```

`samePidProven()` 必须基于**可观测事实**，而不是"我们没 fork"的假设：

1. **系统调用层**：在 macOS 上用 `dtrace`/`fs_usage` 或至少在子线程里轮询 `proc_listchildpids(getpid())`，断言**子进程数为 0**；
2. **符号层**：`nm -u` 检查最终二进制**没有** `_fork`、`_vfork`、`_posix_spawn`、`_execve`、`_system`、`_popen`；
3. **线程层**：断言 QEMU 的工作线程**共享宿主 PID**（比较 `pthread_getname_np` 找到的 `"qemu_main"` 线程与 `/proc` 或 `proc_pidinfo` 报告的 TID 属于同一 PID）；
4. **反例测试**：把同一个断言跑在一个**故意 fork 的**对照实现上，必须失败 —— 否则该断言没有判别力。

**现有实现的 `(void)getpid();` 这四条一条都不满足。**

### 6.3 不启动 guest（`guest_started == false`）

- 必须显式传 **`-S`**（冻结 CPU）以及 **不提供** kernel / BIOS / 固件路径；
- 断言 QEMU 从未进入 `RUN_STATE_RUNNING` 之后的 guest 执行（可从 QEMU 的 runstate 读取），而不仅是 wrapper 的 `SessionState`；
- `-nodefaults` **不等于**冻结 CPU —— 现有 harness 只有 `-nodefaults`，不满足本条。

### 6.4 生命周期与并发

- **并发 `stop()`**：N 个线程同时 `stop()`，全部返回同一结果，且 `qemu_cleanup` 恰好被调用一次
- **重复 `destroy()`**：幂等，无 use-after-free（ASan 验证）
- **初始化失败路径**：**必须**用一条真实的坏 argv 触发 QEMU 的失败分支 —— 如果宿主进程因此退出，则该用例**证明 C-2 是不可接受的**，而不是"通过"
- **单实例冲突**：第二个 `create()` 返回 `Busy`，且**在真实核心下**（`g_active` 真为原子）验证 TOCTOU：多线程并发 `create()` 只能有一个成功
- **`kvm→tcg` 回退**：必须实证 `-accel kvm:tcg` 形式；断言在无 KVM 的构建上**不** `exit(1)`

### 6.5 加速器正确性（不误报）

- 在 macOS 上：断言 `accel == TCG`（无 KVM），且**不得**出现 `-accel kvm`
- 跨 ISA（无论哪个宿主）：断言 `hardwareAccelerated == false`
- 断言加速器结论来自 **init 之后**的 QEMU 状态，而非 init 前的猜测

### 6.6 构建前置

- `meson` 可执行（`pip3 install meson` 或 brew）
- OHOS 目标：**另行**交叉构建 glib-2.0 与 pixman-1 到 aarch64-ohos sysroot（H-3）

---

## 7. 跨 ISA / 加速器正确性矩阵（第 6 项）

| 宿主 | guest | 正确 accel | 本方案会怎样 | 误报风险 |
| --- | --- | --- | --- | --- |
| Windows x86_64 | x86_64 | WHPX | 只建模到 KVM/TCG → 判 same-ISA 后尝试 KVM → `exit(1)` | **高**（C-6+C-8） |
| Windows x86_64 | ARM64 | **TCG** | cross-ISA → TCG ✓ | 无 |
| macOS x86_64 | x86_64 | HVF | 同上 → 尝试 KVM → `exit(1)` | **高** |
| macOS ARM64 | x86_64 | **TCG** | cross-ISA → TCG ✓ | 无 |
| macOS ARM64 | ARM64 | HVF | same-ISA → 尝试 KVM → `exit(1)` | **高** |
| Linux x86_64 | x86_64 | KVM | same-ISA + `/dev/kvm` → KVM ✓ | 无 |
| Linux x86_64 | ARM64 | **TCG** | cross-ISA → TCG ✓ | 无 |
| Linux ARM64 | ARM64 | KVM | same-ISA → KVM ✓ | 无 |
| Linux ARM64 | x86_64 | **TCG** | cross-ISA → TCG ✓ | 无 |
| Linux riscv64 | riscv64 | KVM（若 QEMU 支持该 host） | 未建模 | 待确认 |

**判读**：

- **跨 ISA 一律走 TCG、绝不误报硬件加速** —— 这一点**符合**要求 ✓（`decideAccel` 的 cross-ISA 分支返回 `{Accel::TCG, false}`，`src/QemuInProcessRuntime.cpp:114-115`）
- **但 same-ISA 在 macOS/Windows 上会去尝试 KVM**（因为 `Accel` 里没有 HVF/WHPX），而 KVM 在那些平台不存在 → 按 C-6 **`exit(1)` 杀死宿主**
- `hardwareAccelerated` 在 macOS 上**永不**为真，即使 HVF 本可用

**建议**：`decideAccel` 必须叠加**运行时平台**门控，并把 `Accel` 扩展为 `{KVM, HVF, WHPX, TCG}`；TCI（`meson_options.txt:98 tcg_interpreter`）作为无 JIT 平台的兜底应显式建模。

---

## 8. 无法验证的事项（明确不宣称）

1. `qemu_inprocess.h` / `qemu_inprocess.c` 的**任何内容**（dataless + untracked + 无 dangling blob）
2. 核心是否实现 BQL / replay-mutex 的线程交接（C-9 的死锁风险因此**未证实**）
3. `qemu_inprocess_kvm_usable` 是否做了真实的 `/dev/kvm` 探测（marker 点名 `kvm_enabled` 是危险信号）
4. 真实 `g_active` 是否为原子、单实例守卫能否挡住 cleanup 之后的重入
5. `vm_config_groups[48]` 在一次 init 后是恰好耗尽（`abort()`）还是静默损坏 —— 两者都足以取消资格
6. **任何动态证据**：没有编译、没有链接、没有测试、没有 guest
7. 本报告的所有 QEMU 事实均来自 **11.1.50 / `d49f876`**；其他版本需重新核对

---

## 9. 结论

**不通过。而且不只是"差一点"——交付物当前连构建都无法开始。**

### 9.1 四项逐条判读

- **第 1 项**：两个被审计的 API 名（`qemu_shutdown`、`qemu_run_machine`）在 QEMU 11.1.50 全树**不存在**；`kvm_init` 是 `static`、`kvm_available` 无 C 形式、`kvm_enabled` 是**宏**而非函数（文档签名是伪造的）。
- **第 2 项**：**`qemu_init` 会杀死宿主进程**（`void` 返回 + 53 处 `exit`/`abort` + 63 处 `error_fatal`）；**cleanup 后不能重入**（`abort()` + 静默跳过模块注册）；**BQL 线程交接要求可能让 wrapper 死锁**；**裸 `-accel kvm` 把 KVM 失败变成宿主 `exit(1)`** —— 与文档声称的"透明回退 TCG"**正好相反**。
- **第 3 项**：`qemu-inprocess/meson.build` 引用 **3 个不存在的变量**（`qemu_include_headers`/`qemu_include_system`/`qemuutil_dep`，真实为 `qemuutil`）；静态库**只含 1 个源文件**，遗漏全部 system-emulator 对象；且 `CMakeLists.txt` 的静态扫描**命中自身注释**，导致**任何模式都无法 configure**。
- **第 4 项**：mock 证明了它该证明的（wrapper 状态机逻辑），**但它现在连编译都做不到**（依赖 dataless 头文件）；same-PID harness 的"证明"是 `(void)getpid();`，**零判别力**。
- **第 5 项**：**Meson CLI 是唯一真实缺失项**；glib-2.0 2.88.3 / pixman-1 0.46.4 / zlib / libffi / gio / gthread / pcre2 **全部就位** → blocked marker 的 blocker 1、2 **已为假**。OHOS 侧是**目标依赖缺失**（需先交叉构建 glib/pixman），不是 Meson 缺失。
- **第 6 项**：**跨 ISA 不误报硬件加速 ✓**（`decideAccel` cross-ISA 返回 `{TCG, false}`）；但 **HVF/WHPX 完全未建模**，导致 macOS/Windows 的 same-ISA 会去尝试不存在的 KVM → 按 C-6 **`exit(1)` 杀死宿主**。
- **第 7 项**：产品当前实际仍是**子进程 QEMU**（`src/QemuEngine.cpp:526-558` 用 `QProcess`，发布门禁显式白名单），而进程内方案是**不在根构建、不在 CI** 的孤儿交付物。

### 9.2 四个布尔值

| 布尔值 | 判定 |
| --- | --- |
| `real_core_linked` | **false** |
| `same_pid_proven` | **false** |
| `ohos_cross_compiled` | **false** |
| `guest_started` | **false** |

### 9.3 最高优先级（按修复顺序）

0. **C-17 — `libqemu_inprocess.a` 按现有配置无法编译。** `qemu_inprocess.c:146` 的 `(void)kvm_enabled;` 缺 `()`（函数式宏不展开）→ 未声明标识符；且 `#ifdef CONFIG_KVM` 在 root-scope 静态库中**恒为假**（QEMU 只把 `CONFIG_KVM` 写进 per-target 头）。**这是最硬的一条：核心产物根本编不出来。**
0b. **C-16 — `qemu_inprocess.c:76-79` 无条件返回 `QEMU_INPROCESS_OK`**，`QEMU_INPROCESS_ERR_INIT_FAILED` **在实现中不存在**（与 header/docs/marker 三处契约矛盾）；注释指向的 "Graceful init failure" 一节**不存在**，代码也未安装任何 exit notifier / signal handler。真实 init 失败 ⇒ QEMU `exit(1)` ⇒ 宿主被杀。
0c. **C-18 — 已实测误报 KVM**：真实 wrapper + 仓库自带 mock 在**无 `/dev/kvm` 的 macOS arm64** 上报告 `accel=KVM, hardwareAccelerated=TRUE`。根因是 mock 的 `kvm_usable` 从不探测 `/dev/kvm`（契约要求"ISA 匹配 **AND** /dev/kvm 可开"）。**唯一的 28/28 绿灯正是这个误报的产物。**
1. **C-10 — 交付构建在任何模式下都无法 configure。** `CMakeLists.txt:62-64` 的裸 substring 扫描命中了 `src/QemuInProcessRuntime.cpp:6-7` 自己注释里的 "QProcess"。**在此之前，没有任何"已跑通"的说法是可复现的。** 直接换用仓库已有的 `third_party/minivmac/inprocess/scan_spawn_apis.py`。
2. **C-7 — 实体化并把 `qemu_inprocess.{h,c}` 纳入版本控制。** 它们既 dataless 又 untracked（无 dangling blob 可恢复），方案的 ABI 与实现**无法被任何人验证**，且一次误清理即永久丢失。
3. **C-2 / C-6 — 重新设计 `qemu_init` 的调用与 `-accel` 参数。** 当前形态下，一条坏 argv 或一次 KVM 失败就会终止宿主进程。`-accel` 至少需改为 `kvm:tcg`。
4. **C-3 — 修正 meson 变量名（`qemuutil_dep`→`qemuutil`）并聚合真实 system-emulator source set。** 否则根本产不出可链接的库。
5. **C-12 — 修复 `destroy()` UAF、`init()` 泄漏/永久锁死、`start()` 二次调用 `std::terminate`。** `hpp:12-15` 的三条硬保证当前全部不成立。
6. **C-11 / C-4 / C-5 — 让真实核心路径可构建（修 mock 后门符号的链接条件），并把"每进程一次 init"写成硬不变量；重写 same-PID harness（现有形式无判别力）。**
7. **C-8 / H-4 — 扩展 `Accel` 为 `{KVM, HVF, WHPX, TCG}`，平台门控来自运行时探测而非调用方传值。**
8. **C-13 — 明确产品语义。** 若产品确实仍走子进程 QEMU，就不应存在"已进程内化"的表述；若要走进程内，则必须真正接入根构建与 CI。

### 9.4 核心实现审计补充（文件实体化后）

初稿因 dataless 无法审计 `qemu_inprocess.{h,c}`。它们在审计期间（约 10:31）被实体化，9/9 SHA-256 与 marker 吻合，因此以下发现来自**直接阅读**：

- **C-16**：`qemu_inprocess.c:76-79` 在 `qemu_init` 后**无条件** `return QEMU_INPROCESS_OK`；`QEMU_INPROCESS_ERR_INIT_FAILED` **在实现中不存在**，与头文件/文档/marker 的三处契约矛盾。注释指向的 "Graceful init failure" 一节**不存在**，且代码未安装任何 exit notifier / signal handler。
- **C-17**：`qemu_inprocess.c:146` 的 `(void)kvm_enabled;` **是编译错误**（函数式宏缺 `()`），且 `#ifdef CONFIG_KVM` 在 root scope **恒为假** → `libqemu_inprocess.a` **按现有配置无法编译**。
- **C-18**：真实 wrapper + 仓库自带 mock 在**无 `/dev/kvm` 的 macOS** 上实测报告 `accel=KVM, hardwareAccelerated=TRUE` —— 根因是 mock 的 `kvm_usable` 从不探测 `/dev/kvm`。**唯一的 28/28 绿灯正是这个误报的产物。**
- **M-5（新）**：`qemu_inprocess.c:58-76` 的单实例守卫是 **racy check-then-set**（`qatomic_read` 检查后 `qatomic_set` 置位），非原子 test-and-set → 两线程可同时通过。
- **M-6（新）**：`qemu_inprocess.c:109` 用 `SHUTDOWN_CAUSE_GUEST_SHUTDOWN` 表示**宿主发起的**停止 → 关机原因错误归因。
- **M-7（新）**：`qemu_inprocess_run` 未强制头文件 `:106-107` 要求的"恰好调用一次"，二次调用会重入 `qemu_main_loop`。
- **M-8（新）**：`VirtualizationCapabilities.cpp:272-279` 只用 `QFileInfo::exists()` 判断 KVM 可用，而 QEMU 自身要求**读+写**均可（`python/qemu/utils/accel.py:54-75`）；同仓库的 `LinuxKvmEngine.cpp:166-172` 反而检查了可读写 → **QEMU 路径是唯一不一致的一条**。
- **M-9（新）**：`tests:139`、`src/QemuInProcessRuntime.cpp:282-287` 的 `setCallbacks` 无锁写 `std::function`，而 trampoline 从 worker/IO 线程读 → 数据竞争；`handle->mtx` 从未被使用。

### 9.5 本次审计**没有**给出的结论

- 未对 `qemu_inprocess.h` / `qemu_inprocess.c` 的**任何内容**作断言（不可读，见 C-7）。
- 未给出任何绿色的"已进程内化"结论。mock 通过不构成通过。
- 所有 QEMU 事实基于 **11.1.50 / `d49f876`**，其他版本需重新核对。
