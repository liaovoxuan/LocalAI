# HarmonyOS / OpenHarmony 虚拟化兼容后端

> **2026-10-07 安全更新（优先于本文后续的旧设计说明）：** Harmony 后端已移除
> `QProcess` / `--version` / `startDetached` 外部 QEMU 探测与启动路径。当前实现
> 失败关闭：在真实 `QemuInProcessRuntime` 及目标架构归档安全连入产品前，
> 报告 `runtime-missing` 并拒绝启动，绝不回退到子进程。本文中后续描述
> `QProcess` / PATH / `runtime/qemu` 可执行文件的章节仅作历史设计记录，不再是
> 当前产品行为。当前门禁状态见 `build-qemu-inprocess/.qemu-inprocess-blocked`
> 和 `docs/qemu-inprocess.md`。

## 1.1 当前集成：QemuInProcessRuntime 同 PID 核心（fail-closed 硬门禁）

> 本段描述 2026-10-07 之后的**当前**集成方式。关键词：**同 PID 进程内核心、不暴露 PID 语义、双宏硬门禁、前端回调不假、跨 ISA 必 TCG**。

后端现在**只**通过 `QemuInProcessRuntime` 的**同 PID** 接口驱动 QEMU：

```
create("harmony", handle) → setHostIsa(hostIsa) → init(handle, launch)
  → start(handle) → … → stop(handle) → destroy(handle)
```

- **绝不**使用 `QProcess`、外部 `qemu-system-*`、`fork`/`exec*`/`system`/`popen`/`posix_spawn`、
  `CreateProcess`/`ShellExecute`/`NSTask`/`QDesktopServices::openUrl`。禁用原语由
  `build-harmony-inprocess-integration/scan_harmony_spawn_apis.py` 在测试期强制扫描
  （命中即非零退出），见 §7。
- **绝不调用 `workerPid()`**。`start()` 返回给调用方的 `processId` 是**不透明会话令牌
  （opaque session token）**——一个由核心单调递增分配的 id，**绝非 OS PID**，也从不与
  任何 PID 比较。`HarmonyVirtualizationEngine` 内部用 `activeSessions_` 记录自己发出的
  令牌，`stop(token)` 只转发本引擎发出过的令牌；未知/从未启动的令牌在触及环境之前就被
  拒绝（不 spawn、不假 stop）。

### 双宏硬门禁（不得绕过）

真实核心路径**仅当以下两个宏同时定义**才编译进后端：

```cpp
#if defined(HARMONY_INPROCESS_REAL_CORE) && defined(DEEPSEEK_HOST_SAFETY_GATE_PASSED)
#   include "QemuInProcessRuntime.hpp"
    // 真实同 PID 核心路径
#endif
```

| 宏 | 含义 | 默认 |
| --- | --- | --- |
| `HARMONY_INPROCESS_REAL_CORE` | 已链接静态 QEMU 核心对象 | **OFF** |
| `DEEPSEEK_HOST_SAFETY_GATE_PASSED` | DeepSeek 的 `qemu_init` 致命 `exit()` 安全门已验证 | **OFF** |

**两者皆 OFF 时**，后端落在 fail-closed 桩：明确报告 runtime 未连入、拒绝启动、
**不回退到任何外部子进程**。这既满足了"公共构建只在真实 QEMU 静态核心可用且 DeepSeek
宿主安全门通过时启用"的要求，也**不掩盖 DeepSeek 正在修的 fatal-init 风险**——
`qemu_init` 在畸形配置下仍可 `exit()` 杀死整个宿主进程，因此在那道门落地之前，公共构建
必须保持 fail-closed。在 `DEEPSEEK_HOST_SAFETY_GATE_PASSED` 通过前把它打开是**不安全**的，
因此默认 OFF 是有意的。

> ⚠️ **`fatal-init` 与真实前端回调仍是硬门禁，本文档不写 "ready"。** 真实核心尚未连入、
> Display/Input/Audio/Block 回调 ABI 尚未接通——这两项是独立的待办，不是"已完工"。

### 前端回调不假（硬门禁）

`QemuInProcessRuntime` 的 Display/Input/Audio/Block 回调 ABI 在当前构建中尚未真实接通。
因此：

```cpp
HarmonyFrontendStatus frontendStatus() const;                 // → NotSupported
HarmonyFrontendStatus setFrontendCallbacks(const ...&) const; // → NotSupported，且什么都不存
```

两者都**只返回 `NotSupported`，绝不返回 `NotReady`，也绝不存储任何回调**。一旦返回
"成功 / 已接通"就属于伪造行为，测试 `testFrontendCallbacksAreNotFaked()` 专门断言这种
"假成功"在结构上不可能发生。

### 架构策略（跨 ISA 必 TCG）

- 候选客户机架构：`aarch64` / `x86_64` / `riscv64` 的跨 ISA TCG。
- 硬件加速**仅在真实可用且同 ISA 时**报告：`harmonyHostState()` 只在
  `hostArch == guestArch` 且 `/dev/kvm` 存在且可读写时返回 `Accelerated`；跨架构客户机
  永远 `TcgOnly`（即使 KVM 完全可用，也不存在跨架构硬件加速）。
- `harmonyHostHasAcceleration()` 进一步收紧为"宿主能加速它**声明的每一个**客户机架构"：
  只要 `runnableGuestArches` 中出现一个跨 ISA 客户机，就不再对外声称"有硬件加速"
  （否则对该客户机是误导）。这是"硬件加速仅在真实可用且同 ISA 时报告"的直接落地。

### 独立集成构建与无子进程扫描

`build-harmony-inprocess-integration/` 是一个**可控、独立**的 CMake 构建，镜像项目的
后端测试源集合（含 mini-vmac C 核心，仅编译不修改），并额外挂两个测试：

1. `harmony_virtualization_tests` —— 含确定性生命周期、并发 stop、**错误传播**（init
   失败透传）、**重复销毁幂等**、**无子进程保证**、前端回调不假、跨 ISA 仅 TCG 等用例；
2. `harmony_spawn_api_scan` —— 对 `HarmonyVirtualizationEngine.{cpp,hpp}` 与
   `VirtualizationEngineRegistry.{cpp,hpp}` 做禁用原语扫描，剥离注释/字符串后匹配
   QProcess/startDetached/fork/popen/system(/execv*/posix_spawn*/CreateProcess/ShellExecute/
   NSTask，命中即失败；同时校验双宏门控确实存在于源中。

两个宏默认 OFF，因此该构建始终验证的是 **fail-closed 桩 + 不暴露 PID 语义 + 无子进程**
这一组合。详见 §7 的实测结果。

本文档描述 VirtualWorld 的 HarmonyOS/OpenHarmony 后端
（`src/HarmonyVirtualizationEngine.hpp` / `.cpp`）。

> **本后端只新增以下文件，未改动任何其他文件：**
>
> - `src/HarmonyVirtualizationEngine.hpp`
> - `src/HarmonyVirtualizationEngine.cpp`
> - `tests/HarmonyVirtualizationEngineTests.cpp`
> - `docs/harmonyos-virtualization.md`（本文件）
>
> `CMakeLists.txt`、`README.md`、`VMManager`、`main`、公共
> `VirtualizationEngine` / `VirtualizationEngineRegistry` /
> `VirtualizationCapabilities` 文件、Mac/Windows/Linux 后端文件以及
> `qemu-source` / `qemu-master` 均未修改。

## 1. 这个后端到底是什么（诚实声明）

HarmonyOS NEXT 与 OpenHarmony **没有**面向第三方应用公开、稳定、通用的原生
虚拟化 API，不存在可以像 macOS `Virtualization.framework` 或 Windows WHPX 那样
直接调用的宿主接口。因此本后端**不是**原生虚拟化后端，也**从不**这样宣称自己。

它是一个**限定在 HarmonyOS/OpenHarmony（Linux 兼容模式）宿主上的 QEMU 兼容后端**：

| 情况 | 行为 | tier |
| --- | --- | --- |
| 有可运行的 QEMU，且 `/dev/kvm` 可读写（同架构客户机） | 驱动 QEMU，请求 `accel=kvm` | `HardwareAccelerated`（hardware） |
| 有可运行的 QEMU，但无可用硬件加速 | 驱动 QEMU + TCG 软件模拟 | `SoftwareEmulation`（emulation） |
| QEMU 存在但无法执行（HarmonyOS NEXT 沙盒） | 返回 `runtime-not-runnable` | 不支持 |
| 没有 QEMU | 返回 `runtime-missing` | 不支持 |
| 宿主不是 HarmonyOS/OpenHarmony | 返回 `host-unsupported` | 不支持 |

**关键不变式：`tier()` 永远不会返回 `VirtualizationTier::Native`。** 即使使用
KVM，CPU 是硬件虚拟化的，但设备模型仍由 QEMU 提供，因此
`EngineCapabilities::nativeExecution` 恒为 `false`。这一点由
`testNeverClaimsNativeTier()` 对全部宿主状态逐一断言。

## 2. 宿主状态机

所有"能不能跑这个客户机"的判断都收敛为这五个状态，名字刻意设计为不可能被误解为
原生虚拟化：

| 状态 | `statusCode` | 级别 | 含义 |
| --- | --- | --- | --- |
| `NotHarmonyHost` | `host-unsupported` | Error | 不是 HarmonyOS/OpenHarmony 宿主 |
| `RuntimeMissing` | `runtime-missing` | Error | 该客户机架构没有可运行的 QEMU |
| `RuntimeNotRunnable` | `runtime-not-runnable` | Error | 存在 QEMU 可执行文件但无法运行 |
| `TcgOnly` | `degraded` | **Warning** | 可用，但仅软件模拟 |
| `Accelerated` | `available` | Info | 可用，同架构 KVM 硬件加速 |

`degraded` 与 `available` 是**不同**的稳定码：上层可以据此区分"能跑但是软件模拟"
和"能跑且硬件加速"。

`TcgOnly` 产生的是 **Warning 而不是 Error**，这是刻意的：否则
`VirtualizationEngineRegistry::select()` 会跳过这台机器上唯一真正能跑的
HarmonyOS 后端。

### 架构策略

- 候选客户机架构：`aarch64`、`x86_64`、`riscv64`（ARM64 优先，因为 HarmonyOS
  设备实际就是 ARM64）。
- 每一项都以**运行时探测结果为准**：只有在对应 `qemu-system-<arch>` 通过
  `--version` 健康检查后才算可用（`HarmonyHostSnapshot::runnableGuestArches`）。
- `ppc` / `i386` / `m68k` 等一律不是本后端的候选，直接返回
  `guest-arch-unsupported` 并建议改用 QEMU 后端。
- **硬件加速仅限同架构**：`aarch64` 宿主上的 `x86_64` 或 `riscv64` 客户机永远走
  TCG，即使 `/dev/kvm` 完全可用。不存在跨架构硬件加速。

## 3. 能力探测注入（可测试性的核心）

后端被拆成两层，使全部决策都可以在没有 HarmonyOS 设备、也不启动任何进程的情况下
测试：

| 层 | 位置 | 内容 |
| --- | --- | --- |
| 纯策略层 | `HarmonyVirtualizationEngine.hpp` 全部 `inline` | `HarmonyHostState`、能力/校验/参数安全判定，完全由注入的 `HarmonyHostSnapshot` 驱动 |
| 环境接缝 | `HarmonyEnvironment`（4 个纯虚函数） | 读宿主、解析 QEMU 程序、构造参数数组、启停进程 |
| 真实实现 | `HarmonyVirtualizationEngine.cpp` | 薄封装：`detectHostProfile()` + `RuntimeManager` + `QemuEngine` |

`HarmonyEnvironment` 是唯一的注入点。测试注入 `FakeHarmonyEnvironment`
（脚本化），因此 `start()`/`stop()`/`probe()` 的每一条失败路径都可复现。

`HarmonyVirtualizationEngine(QSharedPointer<HarmonyEnvironment>)` 传入 **null 时按
字面处理**（不会偷偷装入真实环境），因此"引擎没有环境"的防御分支也是可测的。

### 探测成本

`RuntimeManager` 会为每个 QEMU 候选执行 `<qemu> --version`（8 秒上限）。因此在
**非 HarmonyOS/OpenHarmony 宿主上完全不做 QEMU 探测**：快照第一阶段只看
`detectHostProfile()`，发现不是 Harmony 家族就立即返回。这让 macOS/Windows/Linux
上的能力查询保持廉价且无副作用，并由 `testRealEnvironment()` 断言
（`qemuProbePerformed == false`）。

快照按引擎实例缓存；`invalidateProbe()` 强制重读。

**`hostSnapshot()` 返回副本，不返回引用。** 能力查询是 `const` 方法，却可以被
CLI 与 GUI 线程同时调用；如果它交出对 `cachedSnapshot_` 的引用，一个并发的
`invalidateProbe()`（`cachedSnapshot_ = HarmonyHostSnapshot();`）会在读者手里的
引用下面改写那些 `QString` / `QStringList`， readers 于是读到撕裂或半清空的数据。
返回副本意味着读者持有的快照在它持有期间不可变。`testConcurrentProbeInvalidation()`
让 readers 与 invalidators 直接对撞：退回旧签名后 ThreadSanitizer 立刻报 9 处
数据竞争并非零退出，修好后 0 报告。

## 4. 命令构造与注入防护

**整个后端没有任何 shell。** 客户机由"程序路径 + `QStringList` 参数数组"描述，
直接交给 `QProcess::start(program, arguments)`：

- 不拼接命令行字符串，因此客户机路径或内核命令行中的 shell 元字符不可能变成第二条
  命令（`testNoShellStringIsProduced` 用 `; rm -rf /` 载荷验证它只是一个普通数组
  元素）。
- 参数由应用既有的 `QemuEngine::buildArguments()`（底层
  `Translator::buildQemuCommandParts`）生成——单一事实来源，且与其他后端完全一致。
- `harmonyArgumentsAreWellFormed()` 在启动前做结构校验：拒绝空数组、null 元素、
  内嵌 NUL/换行，以及**以 `-` 开头却含空格的元素**（多个选项被拼成一个字符串的
  特征）。含空格的合法值（磁盘路径、内核命令行）不受影响，因为它们是独立的、
  不以 `-` 开头的元素。
- 配置校验还会检查内核/initrd/固件/EFI/磁盘/光盘路径与
  `extraArgs`/`unsupportedArgs` 的 NUL 与换行（`path-invalid` /
  `argument-invalid`）。

加速器通过 `VmConfig::accelerator` 表达（`kvm` 或 `tcg`），由共享翻译器渲染成
`-machine <machine>,accel=<token>`。共享的 `selectAccelerator()` 仍可能把它降级为
`tcg`——这是刻意的双重检查：本后端表达**意图**，翻译器负责**现实**。

## 5. 生命周期、超时与错误传播

`start(config, pid, error)` 的顺序是"先全部拒绝，再启动"：

1. `validate()` 若有 Error 直接失败（`host-unsupported` / `runtime-missing` /
   `runtime-not-runnable` / `guest-arch-*` / `config-invalid`）；
2. 无法解析 QEMU 程序 → 失败，**不进入 spawner**；
3. 参数数组未通过安全校验 → 失败，**不进入 spawner**；
4. 启动失败 → 透传环境错误；
5. **spawner 返回的 pid ≤ 0 时拒绝返回成功**——绝不声称客户机已启动。

超时：

- QEMU `--version` 探测有 8 秒上限（与 `RuntimeManager` 一致），超时会置
  `HarmonyHostSnapshot::qemuProbeTimedOut`，并在 `probe()` 中产生
  `runtime-probe-timeout` Warning；该状态**不会**被当作可用。
- "找到但跑不起来"的候选先被记为 `unprovenCandidate`，然后**用一次有界等待重跑
  `--version`**，以便把"超时"与"硬性失败"分开、也避免一次偶发失败被误报成
  "blocked"。重跑成功时按它**实际所在的位置**填写 `qemuSource`
  （`runtime/qemu` 或 `PATH`），而不是一律写成 `PATH`。
- `HarmonyHostSnapshot::qemuSource` 的完整取值：`runtime/qemu`（应用 runtime
  目录内）、`PATH`、`unrunnable`（磁盘上确有该二进制，但本设备拒绝执行它 ——
  HarmonyOS NEXT 沙箱情形）、`missing`。该字段**永不为空**：快照总要说清它在哪找过。
- `startDetached()` 本身不阻塞、无超时。因此"已启动"的准确含义是
  "进程已成功创建并拿到 pid"，这与其他 QEMU 后端一致；本后端不额外声称客户机
  已经引导成功。

稳定错误码一览：

| `code` | 级别 | 含义 |
| --- | --- | --- |
| `host-unsupported` | Error | 宿主不是 HarmonyOS/OpenHarmony |
| `runtime-missing` | Error | 该架构没有可运行的 QEMU |
| `runtime-not-runnable` | Error | QEMU 存在但无法执行（沙盒/设备策略） |
| `runtime-probe-timeout` | Warning | `--version` 探测超时 |
| `software-emulation` | Warning | 可用但仅 TCG（degraded） |
| `hardware-acceleration` | Info | 可用且同架构 KVM |
| `guest-arch-missing` | Error | 客户机架构为空 |
| `guest-arch-unsupported` | Error | 不是 aarch64/x86_64/riscv64 |
| `backend-mismatch` | Error | System Software 配置 |
| `cpu-count-invalid` / `memory-too-small` | Error | 资源参数非法 |
| `path-invalid` / `disk-path-empty` | Error | 路径问题（含注入防护） |
| `argument-invalid` | Error | 附加参数含控制字符 |
| `unsupported-arguments` | Warning | 存在原样透传的参数，需人工确认 |

所有 unsupported/degraded 结果都带有可读的原因文本，并**明确建议回退到
`qemu-tcg`**。

## 6. 与共享接口 / 注册表的集成

本后端**没有修改任何公共文件**。接入方式有两条，两条都已验证：

### (a) 自注册（当前已生效，零集成改动）

`HarmonyVirtualizationEngine.cpp` 末尾按
`VirtualizationEngine.hpp` 注释所描述的约定自注册：

```cpp
const bool vwHarmonyBackendRegistered = []() {
    vw::VirtualizationEngineRegistry::instance().registerBackend(
        vw::makeHarmonyVirtualizationEngine());
    return true;
}();
```

`CMakeLists.txt`（由 WorkBuddy 维护）已经自动探测本后端的源文件，并把它们加入
`VIRTUALWORLD_BACKEND_SOURCES`；`VIRTUALWORLD_INCLUDE_CONTRIB_BACKENDS` 默认为
`ON`，因此**只要这两个文件存在就会被编译并注册**：

```cmake
option(VIRTUALWORLD_INCLUDE_CONTRIB_BACKENDS
       "Compile LinuxKvmEngine/HarmonyVirtualizationEngine when their sources exist" ON)
...
if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/src/HarmonyVirtualizationEngine.hpp"
   AND EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/src/HarmonyVirtualizationEngine.cpp")
    set(VIRTUALWORLD_HARMONY_BACKEND 1)
endif()
```

### (b) 显式工厂（WorkBuddy 可选用）

WorkBuddy 需要在 `VirtualizationEngine.hpp` 中声明、并在
`registerBuiltinBackends()` 中调用下面两个符号（本后端已在
`HarmonyVirtualizationEngine.hpp` 中**声明**、在 `.cpp` 中**定义**）：

```cpp
// 需要 WorkBuddy 加入 src/VirtualizationEngine.hpp（镜像 macOS/Windows 的写法）：
bool harmonyVirtualizationEngineAvailable();
VirtualizationEnginePtr makeHarmonyVirtualizationEngine();
```

```cpp
// 需要 WorkBuddy 在 VirtualizationEngineRegistry::registerBuiltinBackends() 中加入：
if (harmonyVirtualizationEngineAvailable()) {
    const VirtualizationEnginePtr engine = makeHarmonyVirtualizationEngine();
    if (engine) {
        registerBackend(engine);
    }
}
```

两条路径同时存在也是安全的：`registerBackend()` 按 id 覆盖，不会重复注册。

### ⚠️ 集成前提：`VIRTUALWORLD_HAVE_HARMONY_BACKEND` 必须由构建定义

`src/VirtualizationEngine.cpp` 现在（WorkBuddy 于 06:53 接入）用

```cpp
#if defined(VIRTUALWORLD_HAVE_HARMONY_BACKEND) && defined(__has_include)
#if __has_include("HarmonyVirtualizationEngine.hpp")
#define VW_HAVE_HARMONY_BACKEND 1
#include "HarmonyVirtualizationEngine.hpp"
#endif
#endif
...
#if !defined(VW_HAVE_HARMONY_BACKEND)
// 只有本后端**未**编译进来时，才提供这两个兜底定义
bool harmonyVirtualizationEngineAvailable() { return false; }
VirtualizationEnginePtr makeHarmonyVirtualizationEngine() { return VirtualizationEnginePtr(); }
#endif
```

也就是说：**当本后端被编译进来时，`VirtualizationEngine.cpp` 故意不定义这两个
工厂，而由本后端的 `HarmonyVirtualizationEngine.cpp` 定义**（这正是本文件的做法，
与 macOS 后端一致）。

但 `CMakeLists.txt` 只 `set(VIRTUALWORLD_HARMONY_BACKEND 1)`（一个 CMake 变量），
**从未**把 C++ 代码要检查的那个宏 `VIRTUALWORLD_HAVE_HARMONY_BACKEND` 传给编译器
（全文件没有 `target_compile_definitions`）。后果有两个：

1. `VW_HAVE_HARMONY_BACKEND` 永远未定义 → `VirtualizationEngine.cpp` 的兜底定义
   永远参与编译 → 与本后端 `.cpp` 中的定义**重复符号**，链接失败：
   `duplicate symbol 'vw::harmonyVirtualizationEngineAvailable()'` /
   `duplicate symbol 'vw::makeHarmonyVirtualizationEngine()'`。
2. `harmonyBackendCompiledIn()` 永远返回 `false`，即使本后端确实已编译并自注册。

**需要的修复（属 WorkBuddy 的文件，本后端不越权修改）**：在
`VIRTUALWORLD_HARMONY_BACKEND` 被设置的地方，为 `VirtualWorld` 及其测试目标加上

```cmake
target_compile_definitions(VirtualWorld PRIVATE VIRTUALWORLD_HAVE_HARMONY_BACKEND=1)
target_compile_definitions(VirtualWorldVirtualizationEngineTests PRIVATE VIRTUALWORLD_HAVE_HARMONY_BACKEND=1)
```

或者让 `VirtualizationEngine.cpp` 第 27 行的条件改为只看 `__has_include`
（去掉对那个从未定义的宏的依赖），与它自己处理 macOS 的方式保持一致。

在修复落地前，本地验证本后端时可临时加 `-DVIRTUALWORLD_HAVE_HARMONY_BACKEND=1`
（本文件的所有测试结果都是这样得到的，且已验证加上该宏后链接与运行全部正常）。

映射关系（与共享契约 1:1，后端不使用 Qt 以外的公共类型）：

| 共享接口 | 本后端 |
| --- | --- |
| `backendId()` | `backend_id::harmonyVirtualization()` = `"harmony-virtualization"` |
| `tier()` | `HardwareAccelerated`（有 KVM）或 `SoftwareEmulation`，**永不为 Native** |
| `isHostSupported(reason)` | 快照 `hostIsHarmony` 且至少一个架构有可运行 QEMU |
| `supportsGuestArch(arch, reason)` | 运行时探测 + 同架构策略 |
| `probe()` / `probeFor(arch)` | `EngineProbeResult`（含 `canStart`/`canStop`/`accelerator`） |
| `validate(VmConfig)` | 纯策略校验（见 §5） |
| `start()` / `stop()` | 经 `HarmonyEnvironment` 缝 |
| `statusCode()` | §2 的状态码 |
| `requirements()` | QEMU 路径、`/dev/kvm`、沙盒限制 |

## 7. 构建与测试

### 随项目构建（推荐）

```bash
cmake -S . -B build -DBUILD_TESTING=ON
cmake --build build -j 4            # 会自动编译 src/HarmonyVirtualizationEngine.cpp
ctest --test-dir build --output-on-failure
```

### 单独构建本后端的测试

该测试需要共享契约与 QEMU 管线一起链接（与
`VirtualWorldVirtualizationEngineTests` 的源文件集合一致）：

```bash
clang++ -std=c++17 -fPIC -Isrc $(pkg-config --cflags Qt6Core) \
  tests/HarmonyVirtualizationEngineTests.cpp \
  src/HarmonyVirtualizationEngine.cpp src/VirtualizationEngine.cpp \
  src/VirtualizationEngineRegistry.cpp src/VirtualizationCapabilities.cpp \
  src/WindowsHypervisorEngine.cpp src/Platform.cpp src/VmConfig.cpp \
  src/QemuEngine.cpp src/QemuParser.cpp src/RuntimeManager.cpp src/SupportTrust.cpp \
  src/SystemSoftwareEngine.cpp src/SystemSoftwareRomGenerator.cpp src/Translator.cpp \
  src/RecoveryGuard.cpp \
  $(pkg-config --libs Qt6Core) -lssl -lcrypto -o harmony-tests
./harmony-tests
```

（macOS 上还需追加 `src/MacVirtualizationEngine.mm` 与
`-framework Virtualization -framework Foundation -framework Security`，
与 `CMakeLists.txt` 中 `VIRTUALWORLD_MACOS_BACKEND` 的处理一致。）

### 独立集成构建（fail-closed 验证首选）

`build-harmony-inprocess-integration/` 提供**可控、独立**的 CMake 构建，默认两个硬门禁宏
`HARMONY_INPROCESS_REAL_CORE` / `DEEPSEEK_HOST_SAFETY_GATE_PASSED` 均为 OFF，因此验证的
始终是 fail-closed 桩 + 不暴露 PID 语义 + 无子进程这一组合（不连入真实核心、不启动任何
guest/UTM/VM）：

```bash
cd build-harmony-inprocess-integration
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --target harmony_virtualization_tests -j4
cd build
ctest --output-on-failure          # 2 个测试：单元套件 + spawn-api 扫描
```

其中 `harmony_spawn_api_scan` 对 `HarmonyVirtualizationEngine.{cpp,hpp}` 与
`VirtualizationEngineRegistry.{cpp,hpp}` 做禁用原语扫描（命中 QProcess/fork/exec*/system/
popen/posix_spawn*/CreateProcess/ShellExecute/NSTask 即非零退出），并校验双宏门控确实存在于
源中。

### 实测结果（Apple Silicon，macOS 27，独立集成构建）

| 项目 | 结果 |
| --- | --- |
| 单元套件 `harmony_virtualization_tests` | **1570 项检查，0 失败，0 skip**，stderr 无告警 |
| `harmony_spawn_api_scan` | **CLEAN**，退出码 0（backend is strictly in-process） |
| AddressSanitizer | 0 报告（随项目构建路径） |
| ThreadSanitizer | **0 报告**（并发能力查询、并发探测失效、并发启停压力、确定性混沌循环） |
| 新增断言覆盖 | 并发 stop、重复销毁幂等、无子进程保证、前端回调不假、跨 ISA 仅 TCG、init 失败透传 |
| 回归 | 随项目构建的 `VirtualizationEngineTests` 仍全部通过 |

> ⚠️ **当前两份门禁宏均 OFF，因此上述结果验证的是 fail-closed 行为，不是真实核心可用。**
> 在 DeepSeek 的 `qemu_init` 致命 `exit()` 安全门（`DEEPSEEK_HOST_SAFETY_GATE_PASSED`）
> 与真实静态核心链接（`HARMONY_INPROCESS_REAL_CORE`）**两者皆 ON** 之前，公共构建保持
> fail-closed，本文档不写 "ready"。

（随项目全文构建历史上曾因他人文件 `src/WindowsHypervisorEngine.cpp:726` 字符串引号未转义
而失败，与本后端无关；本后端的验证使用上面的独立集成构建完成，**仓库内他人文件未被修改**、
**未 commit / 未 push**。）

阶段三新增覆盖：**显式宿主×客户机架构矩阵**（8 组，含"模拟器缺失时既不可用也
不加速"）、**重复启停生命周期**（3 轮 + 失败后恢复）、**并发能力查询**
（4×25，验证被缓存的探测是线程安全的）、**并发探测失效**（3 readers 与 2
invalidators 对撞，`hostSnapshot()` 的返回副本不变式）、**每一条拒绝都必须
点名 fallback**。

**平台故障注入复核（专项，固定种子确定性压力循环）** —
`testChaosDeterministicFaultInjectionLoop()` 用 `kChaosSeed` 固定的 LCG 决定每
一轮的故障模式，把下列场景集中、可复现地压一遍（每轮都 `invalidateProbe()`
制造缓存风暴，再按种子随机交错 `start` / `startDetached` / `stop` / 销毁）：

- **缓存风暴 + 环境突变**：每轮先 `invalidateProbe()`，迫使引擎在故障宿主态下重算
  快照；快照副本语义（`hostSnapshot()` 返回按值拷贝）保证读快照不会被并发改写。
- **runtime 找到但不可运行**（`runtime-not-runnable`）与**重试来源**：重跑
  `--version` 成功后 `qemuSource` 回填真实位置（不再谎标 `PATH`），断言落到
  `RuntimeNotRunnable` 态；该分支本机（macOS）走不到，仅由 `FakeHarmonyEnvironment`
  注入路径覆盖。
- **KVM 权限/设备消失**：`/dev/kvm` 从可读写变丢失 → 同架构客户机由 `Accelerated`
  降级为 `TcgOnly`（`degraded` + 软件模拟），`probeFor(arch)` 的 `accelerator` 相应
  从 `kvm` 变 `tcg`。
- **硬件加速到 TCG 降级**：`kvm` 不可用时 `accelerator` 恒为 `tcg`，`statusCode`
  为 `degraded` 且 `tier()` 为 `SoftwareEmulation`，绝不谎报 `available`。
- **跨架构永远 TCG**：x86_64 客户机在 aarch64 宿主（或反之）上 `probeFor()` 的
  `accelerator` 恒为 `tcg`，`tier()` 恒不为 `Native`。
- **超时恢复**：`qemuProbeTimedOut` 态下 `start` 给出 `runtime-not-runnable`，
  且下一轮 `invalidateProbe()` 后能在 `RuntimeMissing`/其他态下正常恢复。
- **tier 永不为 Native**：每一轮都对 `tier()` 断言 `!= Native`，并对 `statusCode()`
  断言非空、`nativeExecution` 恒 `false`。

循环参数：`kHarmonyChaosRounds = 96` 轮、种子 `kHarmonyChaosSeed = 0x4A21C0DE`、
故障宿主态在（missing / blocked / degraded /
accelerated）×（同架构 / 跨架构）间按种子游走；报告项：循环数、注入的并发度
（单引擎串行、无多引擎并发竞态被引入）、每轮回调/状态校验计数、资源归属不变量
（无残留快照引用、无悬垂 `FakeHost`）。

测试覆盖：后端 id/tier、架构规范化与候选集合、五态状态机、状态消息插值、
"tier 永不为 Native"、"非 HarmonyOS 宿主永不误报支持"、注入的
missing/blocked/degraded/accelerated 四条路径、配置校验（含注入防护）、
参数数组结构与注入载荷、加速器决策（同架构 kvm / 跨架构 tcg）、
start 的四条失败路径与成功路径、stop 的 pid 校验与错误透传、
探测超时、`probeFor` 语义、探测缓存、真实环境与真实注册表集成。

## 8. 已知未验证 / 未实现项

- **没有在任何真实 HarmonyOS/OpenHarmony 设备上运行过。** 本机是 macOS，
  `detectHostProfile()` 在 macOS 上不会走 Harmony 家族分支，因此也无法用环境变量
  伪装。真实探测路径（`RuntimeManager` 在 `runtime/qemu/harmonyos/<arch>/bin` 与
  Linux 回退目录中查找并执行 `qemu-system-*`）**未经实机验证**。
- **没有启动过任何客户机。** `startDetached()` 之后的引导结果、
  `QemuEngine::stopVm()` 对真实 pid 的终止，都未被测试覆盖（测试按约定从不启动
  虚拟机）。
- 硬件加速"可用"的判定依据是 `/dev/kvm` **存在且可读写**加上同架构，**没有**
  真正初始化一次 KVM 来证明内核会接受它。若内核/容器策略拒绝，QEMU 会自行报错；
  本后端不会因此谎报成功（`statusCode` 仍是 `available`，但启动错误会透传）。
  这是刻意的取舍：避免每次能力查询都启动一个 QEMU 进程。
- HarmonyOS NEXT 沙盒"禁止执行任意进程"的判定依赖 `RuntimeManager` 的健康检查
  失败（`--version` 不成功）。若某设备允许执行但 QEMU 因其他原因不可用，会得到
  同样的 `runtime-not-runnable`，不会更精确地区分。
- "找到但跑不起来"之后的**重跑 `--version` 分支**只在真实 HarmonyOS 宿主、且磁盘
  上确实存在该 QEMU 二进制时才会执行 —— 本机（macOS）永远走不到它，因此该分支
  无自动化覆盖，只有 `FakeHarmonyEnvironment` 所给的注入路径可测。
- 注册表自动选择那条断言写成**与环境无关**：本机没有 QEMU 时它断言"选择失败且
  理由点名 QEMU、每个被拒后端都带稳定码与原因"，装了 QEMU 时断言"落在某个 QEMU
  后端且绝不选 harmony"。它不靠 `skip` 规避环境差异。
- 图形/音频/共享目录等设备能力未单独探测：一律交给共享 QEMU 管线，本后端不为
  HarmonyOS 做特殊处理。
- KVM 上的 32 位客户机（`i386`/`arm`）不在候选集合内，交由 QEMU 后端处理。
