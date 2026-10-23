# Windows WHPX 虚拟化后端

本文档描述 VirtualWorld 的 Windows WHPX（Windows Hypervisor Platform）虚拟化后端。
该后端仿照 LinuxKvmEngine 的分层设计模式重构，通过 `WindowsEnvironment` 注入缝
实现可测试的探测与执行逻辑，拒绝跨架构客户机（guest-arch-unsupported），
由注册表回退到 qemu-tcg 后端承担。

> 本后端**只**涉及以下文件，未改动 `CMakeLists.txt`、`README.md`、
> `VMManager`、`main`、公共 `VirtualizationEngine`/`Registry`/`Capabilities` 文件、
> Mac/Linux/Harmony 文件以及 `qemu-source`/`qemu-master`：
>
> - `src/WindowsHypervisorEngine.hpp`
> - `src/WindowsHypervisorEngine.cpp`
> - `tests/WindowsHypervisorEngineTests.cpp`
> - `docs/windows-virtualization.md`（本文件）

## 1. 分层架构

本后端采用与 LinuxKvmEngine 相同的分层设计：

```
┌─────────────────────────────────────────────────────┐
│              WindowsHypervisorEngine                │
│  (VirtualizationEngine 子类，持有 WindowsEnvironment) │
├─────────────────────────────────────────────────────┤
│  纯策略函数（无状态，可独立测试）                       │
│  - windowsHostState()                               │
│  - windowsAcceleratorFor()                          │
│  - windowsCanRunGuest()                             │
│  - windowsExecutionTierFor()                         │
│  - windowsValidateConfig()                          │
│  - windowsArgumentsAreWellFormed()                  │
│  - windowsCanonicalGuestArch()                      │
│  - windowsCandidateGuestArches()                    │
├─────────────────────────────────────────────────────┤
│  WindowsHostSnapshot（纯数据）                        │
│  - 宿主平台快照，由 WindowsEnvironment::snapshot() 填充 │
├─────────────────────────────────────────────────────┤
│  WindowsHostState（5 态状态机）                       │
│  NotWindowsHost → WhpxApiMissing →                  │
│  WhpxHypervisorNotPresent → WhpxDegraded →          │
│  WhpxAvailable                                      │
├─────────────────────────────────────────────────────┤
│  WindowsEnvironment（虚基类，注入缝）                  │
│  - snapshot() / program() / arguments()             │
│  - startProcess() / stopProcess()                   │
│  实现：QemuWindowsEnvironment（生产）                 │
│  测试：FakeWindowsEnvironment（脚本化）               │
└─────────────────────────────────────────────────────┘
```

### 1.1 WindowsHostSnapshot

纯数据结构，描述宿主平台在探测时刻的完整状态：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `hostIsWindows` | `bool` | 宿主是否为 Windows |
| `hostFamily` | `QString` | 宿主平台族（`"windows"` / `"darwin"` / `"linux"`） |
| `hostName` | `QString` | 宿主平台显示名 |
| `hostArch` | `QString` | 宿主 ISA（`"x86_64"` / `"aarch64"`） |
| `apiLoadable` | `bool` | `winhvplatform.dll` 是否可加载 |
| `hypervisorPresent` | `bool` | `WHvCapabilityCodeHypervisorPresent` 是否为 TRUE |
| `whpxUsable` | `bool` | WHPX 是否可用（apiLoadable && hypervisorPresent） |
| `qemuFound` | `bool` | QEMU 运行时是否找到 |
| `qemuExecutable` | `bool` | QEMU 可执行文件是否可执行 |
| `qemuPath` | `QString` | QEMU 可执行文件路径 |
| `runnableGuestArches` | `QStringList` | QEMU 可运行的客户机架构列表 |
| `reason` | `QString` | 不可用原因（人类可读） |
| `detail` | `QString` | 附加描述 |

### 1.2 WindowsHostState 状态机

5 个状态，按优先级排列：

| 状态 | 条件 | 同架构可用 | 同架构原生 | 同架构 TCG 回退 |
| --- | --- | --- | --- | --- |
| `NotWindowsHost` | `!hostIsWindows` | ✗ | ✗ | ✗ |
| `WhpxApiMissing` | `hostIsWindows && !apiLoadable` | ✓ | ✗ | ✓ |
| `WhpxHypervisorNotPresent` | `hostIsWindows && apiLoadable && !hypervisorPresent` | ✓ | ✗ | ✓ |
| `WhpxDegraded` | `hostIsWindows && apiLoadable && hypervisorPresent && hostArch == "aarch64"` | ✓ | ✓ | ✗ |
| `WhpxAvailable` | `hostIsWindows && apiLoadable && hypervisorPresent && hostArch != "aarch64"` | ✓ | ✓ | ✗ |

> **跨架构客户机**：无论状态机处于哪一态，跨 ISA 客户机均被本后端拒绝
>（`supportsGuestArch()` 返回 false），由注册表回退到 qemu-tcg 后端。
> 状态机仅描述同架构客户机在 WHPX 不可用时的 TCG 回退路径。

关键语义：

- **WhpxDegraded**：ARM64 Windows 上 WHPX 可用但功能受限（如不支持嵌套虚拟化、
  性能可能低于 x86_64 WHPX）。此状态下同架构客户机仍使用 WHPX（tier = Native），
  但 `validate()` 和 `probeFor()` 会报告 `whpx-degraded` 警告。
- **WhpxApiMissing / WhpxHypervisorNotPresent**：WHPX 不可用，但同架构客户机
  仍可通过 QEMU TCG 运行。此时 `isHostSupported()` 返回 true（QEMU 可用），
  `validate()` 报告 `tcg-fallback` 警告，accelerator 为 `"tcg"`。
- **NotWindowsHost**：非 Windows 宿主，任何操作均不可用。

### 1.3 WindowsEnvironment 注入缝

`WindowsEnvironment` 是虚基类，定义了引擎与平台交互的全部接口：

```cpp
class WindowsEnvironment {
public:
    virtual ~WindowsEnvironment() = default;
    virtual WindowsHostSnapshot snapshot() const = 0;
    virtual QString program(const VmConfig& config) const = 0;
    virtual QStringList arguments(const VmConfig& config) const = 0;
    virtual bool startProcess(const VmConfig& config, qint64* processId, QString* error) = 0;
    virtual bool stopProcess(qint64 processId, QString* error) = 0;
};
```

生产实现 `QemuWindowsEnvironment` 在 `WindowsHypervisorEngine.cpp` 中定义，
负责真实的 DLL 加载、QEMU 路径解析和子进程管理。

测试实现 `FakeWindowsEnvironment` 在测试文件中定义，通过脚本化的快照和
可控的启动/停止行为实现完全离线测试。

## 2. 宿主 / 客户机支持矩阵

| 宿主 | 同架构客户机 | 跨架构客户机 |
| --- | --- | --- |
| Windows x86_64 + WHPX | x86_64（WHPX 硬件加速，Native） | **拒绝**（guest-arch-unsupported） |
| Windows aarch64 + WHPX | aarch64（WHPX 硬件加速，Native，degraded 警告） | **拒绝**（guest-arch-unsupported） |
| Windows x86_64 无 WHPX | x86_64（QEMU TCG，SoftwareEmulation） | **拒绝**（guest-arch-unsupported） |
| Windows aarch64 无 WHPX | aarch64（QEMU TCG，SoftwareEmulation） | **拒绝**（guest-arch-unsupported） |
| 非 Windows 宿主 | 不支持 | 不支持 |

> 跨架构客户机被本后端拒绝后，由 `VirtualizationEngineRegistry` 回退到
> `qemu-tcg` 后端承担。本后端不运行任何跨 ISA 客户机。

关键不变式：

- **后端 tier 恒为 Native**：`tier()` 始终返回 `VirtualizationTier::Native`，
  因为它命名的是后端所封装的机制（WHPX），而非某个客户机的实际执行方式。
  每客户机的真实 tier 由 `executionTierFor()` 承载。
- **跨架构拒绝**：跨 ISA 客户机被本后端拒绝（`supportsGuestArch()` 返回 false），
  而非静默降级到 TCG。拒绝使注册表能选择 qemu-tcg 后端。
- **同架构 TCG 回退**：WHPX 不可用时，同架构客户机仍可通过 QEMU TCG 运行，
  `validate()` 报告 `tcg-fallback` 警告。
- **ARM64 WHPX degraded**：同架构仍为 Native，但报告 `whpx-degraded` 警告。
- **nativeExecution 语义**：仅当 WHPX 可用且同架构时为 true。

## 3. 架构支持

本后端支持的客户机架构：

| 架构 | 规范化 | 说明 |
| --- | --- | --- |
| `x86_64` | `x86_64` | WHPX 在 x86_64 Windows 上原生支持 |
| `amd64` | `x86_64` | `amd64` 是 `x86_64` 的别名 |
| `aarch64` | `aarch64` | WHPX 在 ARM64 Windows 上原生支持（degraded） |
| `arm64` | `aarch64` | `arm64` 是 `aarch64` 的别名 |

其它架构（`riscv64`、`ppc64`、`i386`、`m68k` 等）不在本后端支持范围内。

架构规范化函数 `windowsCanonicalGuestArch()` 将别名映射到规范形式，
不支持架构返回空字符串。候选架构列表由 `windowsCandidateGuestArches()` 返回，
固定为 `["x86_64", "aarch64"]`。

## 4. 诊断码

| 代码 | 级别 | 含义 |
| --- | --- | --- |
| `host-unsupported` | Error | 非 Windows 宿主 |
| `whpx-api-missing` | Warning | `winhvplatform.dll` 不存在或加载失败（同架构 TCG 回退仍可用） |
| `whpx-hypervisor-not-present` | Warning | Hyper-V 未启用（同架构 TCG 回退仍可用） |
| `whpx-degraded` | Warning | ARM64 WHPX 可用但功能受限 |
| `tcg-fallback` | Warning | WHPX 不可用，同架构客户机将通过 QEMU TCG 软件模拟执行 |
| `runtime-missing` | Error | QEMU 运行时未找到或不可执行 |
| `guest-arch-missing` | Error | 未指定客户机架构 |
| `guest-arch-unsupported` | Error | 客户机架构不受本后端支持（空/未知架构或跨 ISA 架构） |
| `backend-mismatch` | Error | 配置指定了其它后端 |
| `cpu-count-invalid` | Error | CPU 核心数 ≤ 0 |
| `memory-too-small` | Error | 内存 ≤ 0 |
| `disk-path-empty` | Error | 磁盘路径为空 |
| `path-invalid` | Error | 路径包含 NUL 或换行符 |
| `argument-invalid` | Error | 附加参数包含 NUL 或换行符 |
| `unsupported-arguments` | Warning | 存在无法等价转换的参数 |
| `available` | Info | 后端完全可用 |
| `whpx-available` | Info | WHPX 硬件加速可用 |

与旧版的关键差异：

- `whpx-not-available` 和 `whpx-not-supported` 已拆分为 `whpx-api-missing` 和
  `whpx-hypervisor-not-present`，且级别从 Error 降为 Warning（同架构 TCG 回退仍可用）。
- `cross-arch-rejected` 和 `cross-arch-tcg-fallback` 均已移除：跨架构客户机现在
  报告 `guest-arch-unsupported`（Error 级别），使注册表回退到 qemu-tcg。
- 新增 `whpx-degraded`（Warning）用于 ARM64 WHPX。
- 新增 `tcg-fallback`（Warning）用于 WHPX 不可用时同架构 TCG 回退提示。

所有 `level == Error` 的问题都会阻止 `start()` 执行。Warning 级问题不会阻止
启动，但会在 `validate()` 和 `probeFor()` 中报告。

## 5. 配置校验

`validate(config)` 执行以下检查（按优先级排列，early-return 语义）：

1. **后端匹配**：`config.backend` 必须为空或 `windows-hypervisor`，否则报
   `backend-mismatch`（Error，early return）。
2. **客户机架构非空**：`config.architecture` 经 trim 后非空，否则报
   `guest-arch-missing`（Error，early return）。
3. **客户机架构在候选列表**：经规范化后在本后端的候选列表中（x86_64/aarch64），
   否则报 `guest-arch-unsupported`（Error，early return）。
4. **宿主平台**：必须是 Windows，否则报 `host-unsupported`（Error，early return）。
   宿主不适用优先于客户机架构问题，避免误导用户。
5. **跨 ISA 拒绝**：客户机架构与宿主架构不同时，报 `guest-arch-unsupported`
   （Error，early return）。WHPX 只支持同架构虚拟化，跨 ISA 是 qemu-tcg 的职责。
6. **字段验证**：CPU 核心数 > 0、内存 > 0、磁盘路径非空、路径无注入字符、
   参数无注入字符等。
7. **宿主状态诊断**：根据状态机报告 `whpx-api-missing` / `whpx-hypervisor-not-present`
   （Warning）或 `whpx-degraded`（Warning）或 `whpx-available`（Info）。
8. **TCG 回退提示**：WHPX 不可用时，报告 `tcg-fallback`（Warning），
   说明同架构客户机将通过 QEMU TCG 执行。
9. **QEMU 运行时**：QEMU 不可执行或无对应架构模拟器时，报 `runtime-missing`
   （Error）。

## 6. 生命周期与错误传播

### start(config, processId, error)

1. 规范化客户机架构，检查是否在候选列表中。不在则返回 false。
2. 检查宿主平台是否为 Windows。非 Windows 则返回 false。
3. **拒绝跨 ISA 客户机**：客户机与宿主架构不同时返回 false，不启动进程。
4. 检查 QEMU 运行时是否可用。不可用则返回 false。
5. 执行配置校验。有 Error 级问题则返回 false。
6. 根据架构匹配和 WHPX 可用性决定加速器：同架构 + WHPX 可用 → `whpx`；
   同架构 + WHPX 不可用 → `tcg`。
7. 构造 QEMU 命令行，包含 `-accel whpx` 或 `-accel tcg` 参数。
8. 通过 `WindowsEnvironment::startProcess()` 启动 QEMU 子进程。
9. 如果启动成功但 `processId == 0`，返回 false。

### stop(processId, error)

1. 检查 `processId > 0`。否则返回 false。
2. 通过 `WindowsEnvironment::stopProcess()` 终止 QEMU 子进程。

### 错误码（statusCode）

| 状态码 | 含义 |
| --- | --- |
| `host-unsupported` | 非 Windows 宿主 |
| `guest-arch-unsupported` | 客户机架构不受支持（空/未知或跨 ISA） |
| `runtime-missing` | QEMU 未找到或不可执行 |
| `whpx-api-missing` | WHPX API 不可加载 |
| `whpx-hypervisor-not-present` | Hyper-V 未启用 |
| `whpx-degraded` | ARM64 WHPX（功能受限） |
| `backend-mismatch` | 配置指定了其它后端 |
| `available` | 后端完全可用 |

> `statusCode()` 的优先级：`host-unsupported` > `guest-arch-unsupported`（空/未知） >
> `guest-arch-unsupported`（跨 ISA） > `runtime-missing` > `whpx-api-missing` >
> `whpx-hypervisor-not-present` > `whpx-degraded` > `available`。

## 7. 动态链接

WHPX 探测使用 Windows API `LoadLibraryA` / `GetProcAddress` 动态加载
`winhvplatform.dll`，而非编译时链接。这确保：

- 在没有 WHPX 的 Windows 系统上，应用不会因缺少 DLL 而无法启动。
- 在非 Windows 平台上，探测直接返回不可用状态，无任何副作用。
- 探测结果在引擎生命周期内缓存，通过 `invalidateProbe()` 可强制刷新。

## 8. 与共享接口的适配

| 共享 `vw::VirtualizationEngine` | 本后端 |
| --- | --- |
| `backendId()` | `"windows-hypervisor"` |
| `displayName()` | `"Windows WHPX"` |
| `tier()` | **恒为 Native**（命名后端机制 WHPX，非执行结果） |
| `executionTierFor(arch)` | 同架构 + WHPX 可用 → Native；其它 → SoftwareEmulation |
| `isHostSupported(reason)` | 基于状态机：NotWindowsHost → false；其它 → QEMU 可用即 true |
| `supportsGuestArch(arch, reason)` | 架构规范化 + 候选列表 + 同 ISA 检查；跨架构**拒绝**（false） |
| `probe()` | 返回 `EngineProbeResult`，含诊断码 |
| `probeFor(arch)` | 针对特定架构的探测；跨 ISA 报告 `guest-arch-unsupported` |
| `validate(config)` | 配置校验 + 状态机诊断 |
| `start(config, pid, error)` | 加速器决策 + QEMU 子进程启动；跨 ISA 拒绝 |
| `stop(pid, error)` | QEMU 子进程终止 |
| `diagnose(config)` | 人类可读诊断文本 |
| `statusCode(config)` | 机器可读状态码 |
| `requirements()` | 需求列表（Windows、WHPX、QEMU、仅同架构客户机） |
| `runsInProcess()` | `false`（QEMU 子进程） |

## 9. 自注册

`WindowsHypervisorEngine.cpp` 末尾的 static lambda 将本后端注册到
`VirtualizationEngineRegistry::instance()`。该 lambda 在静态初始化阶段执行，
确保 Windows 平台上 WHPX 后端可用。

## 10. 跨平台诚实性

本后端严格遵守跨平台诚实性原则：

- **非 Windows 宿主**：`isHostSupported()` 返回 false，`probe()` 报告
  `host-unsupported`，`start()` 永不成功，且永不生成子进程。
- **Windows 无 WHPX（API 缺失）**：`isHostSupported()` 返回 true（QEMU 可用），
  同架构客户机通过 TCG 执行，`validate()` 报告 `whpx-api-missing` +
  `tcg-fallback` 警告。
- **Windows 无 WHPX（Hyper-V 未启用）**：同上，报告 `whpx-hypervisor-not-present`
  + `tcg-fallback` 警告。
- **Windows 有 WHPX（x86_64）**：`isHostSupported()` 返回 true，同架构 tier = Native，
  跨架构被拒绝（`supportsGuestArch()` 返回 false）。
- **Windows 有 WHPX（ARM64）**：同架构 tier = Native（degraded 警告），
  跨架构被拒绝。
- **跨架构请求**：`supportsGuestArch()` 返回 false，`probeFor()` 报告
  `guest-arch-unsupported`，`validate()` 报告 `guest-arch-unsupported` Error，
  `start()` 拒绝启动。注册表将回退到 qemu-tcg 后端。
- **后端 tier 恒为 Native**：`tier()` 命名后端机制（WHPX），不随宿主或客户机
  状态变化。每客户机真实执行 tier 由 `executionTierFor()` 承载。

## 11. 构建与测试

由于不得修改 `CMakeLists.txt`，本后端暂未接入 CMake 目标；测试可用如下命令构建
（Windows / MSVC）：

```cmd
cl /std:c++17 /EHsc /Isrc ^
  tests/WindowsHypervisorEngineTests.cpp ^
  src/WindowsHypervisorEngine.cpp ^
  src/Platform.cpp ^
  src/VirtualizationEngine.cpp ^
  src/VirtualizationEngineRegistry.cpp ^
  src/VirtualizationCapabilities.cpp ^
  src/VmConfig.cpp ^
  src/QemuEngine.cpp ^
  src/QemuParser.cpp ^
  src/RuntimeManager.cpp ^
  /link QtCore4.lib ^
  /out:vw_whpx_tests.exe
vw_whpx_tests.exe
```

测试覆盖（全部通过 `FakeWindowsEnvironment` 注入，无需 Windows 宿主）：

- **身份与架构策略**：backend id、display name、候选架构、规范化映射
- **状态机 5 态转换**：NotWindowsHost / WhpxApiMissing / WhpxHypervisorNotPresent /
  WhpxDegraded / WhpxAvailable
- **加速器策略**：同架构 + WHPX 可用 → whpx；同架构 + WHPX 不可用 → tcg
- **Tier 策略**：后端 tier 恒为 Native；`executionTierFor()` 按客户机返回真实 tier
- **跨架构拒绝**：跨 ISA `supportsGuestArch()` 返回 false，`probeFor()` 报告
  `guest-arch-unsupported`，`validate()` 报告 `guest-arch-unsupported` Error，
  `start()` 拒绝启动且不产生进程
- **ARM64 WHPX degraded**：同架构 Native + whpx-degraded 警告
- **非 Windows 宿主永不支持**：isHostSupported() = false，start() 永不成功且
  永不生成子进程
- **WHPX API 缺失 / Hyper-V 未启用**：同架构 TCG 回退，tcg-fallback 警告
- **QEMU 运行时缺失**：isHostSupported() = false，start() 失败
- **start/stop 环境注入**：FakeWindowsEnvironment 验证加速器决策和进程管理
- **start 失败路径**：环境返回 false 时错误正确传播
- **null 环境守卫**：空环境时 start/stop 安全失败
- **probe/probeFor/validate**：诊断码和 capabilities 一致性
- **statusCode**：与状态机一致，跨 ISA 返回 guest-arch-unsupported
- **diagnose/requirements**：输出包含关键信息
- **注册表集成**：后端已注册，id 一致
- **遗留 probeWhpx()**：在真实宿主上返回合理结果
- **参数安全**：空参数、null 元素、换行符、选项注入等
- **快照缓存与失效**：首次访问触发 snapshot()，后续使用缓存，invalidateProbe() 后重新读取
- **运行时消失与恢复**：invalidateProbe() 使缓存失效后重新探测
- **故障注入**：probe 缓存失效后状态机结果一致；validate 错误在各宿主状态下正确传播；
  start 立即失败时错误正确传播且不修改调用者 pid 指针；start 成功但 PID=0 或负值时
  正确处理；stop 失败时错误正确传播
- **并发压力**：20 轮连续 invalidateProbe() 不崩溃；start/stop 与 probe 失效交叉执行；
  20 轮 start/stop 循环不泄漏
- **Windows 特定覆盖**：WHPX DLL 缺失（apiLoadable=false）；WHPX 无 Hyper-V
  （hypervisorPresent=false）；ARM64 同架构 degraded；QEMU 未找到；QEMU 不可执行；
  跨架构 start 拒绝；所有 reason 非空；statusCode 在多次调用间稳定；
  probe 诊断非空；畸形配置 validate
- **start 失败语义**：start() 失败时不修改调用者传入的 pid 指针（内部用局部变量，
  仅成功时写入 *processId）——测试验证此行为

## 12. 集成测试

以下测试需要在真实 Windows 宿主上执行，不属于本测试套件：

- `winhvplatform.dll` 动态加载成功；
- `WHvGetCapability` 返回 `WHvCapabilityCodeHypervisorPresent = TRUE`；
- QEMU `-accel whpx` 启动成功；
- WHPX 虚拟机创建、启动、停止完整生命周期；
- 无 WHPX 的 Windows 系统（如未启用 Hyper-V）正确回退到同架构 TCG；
- Windows aarch64 + WHPX 正确支持 aarch64 客户机并报告 degraded 警告；
- 跨架构客户机被本后端拒绝，注册表回退到 qemu-tcg 后端。

这些集成测试应作为 CI 流水线的一部分在 Windows runner 上执行。

> **macOS 交叉编译限制**：本测试套件通过 `FakeWindowsEnvironment` 在 macOS 上
> 编译和运行，无需真实 Windows 宿主或 WHPX API。但 WHPX 真机行为
> （DLL 加载、Hyper-V 能力查询、`-accel whpx` 进程启动）只能在 Windows runner 上验证。

## 13. 已知限制

- **Windows aarch64 WHPX**：WHPX 在 ARM64 Windows 上的支持较新，部分旧版本
  Windows 可能不包含 `winhvplatform.dll`。探测机制会正确处理此情况，
  并将状态分类为 WhpxDegraded。
- **Hyper-V 版本依赖**：WHPX 需要 Hyper-V 功能已启用。在 Windows Home 版本上
  可能不可用，需要手动启用 Hyper-V 组件。此时状态为 WhpxHypervisorNotPresent，
  TCG 回退仍可用。
- **嵌套虚拟化**：在虚拟化的 Windows 环境中运行 WHPX（嵌套虚拟化）需要宿主
  hypervisor 支持嵌套虚拟化扩展。未检测此条件。
- **QEMU WHPX 参数**：当前只传递 `-accel whpx`。QEMU 7.0+ 支持额外的 WHPX
  子参数（如 `-accel whpx,kernel-irqchip=off`），本后端暂不支持。
- **vGPU / GPU 直通**：未配置 GPU 直通或 vGPU 支持。
- **内存限制**：WHPX 对客户机内存有上限（取决于 Windows 版本），未检查此限制。
- **TCG 性能**：WHPX 不可用时，同架构客户机通过 QEMU TCG 运行，性能显著低于
  WHPX 硬件加速。`validate()` 和 `probeFor()` 会报告 `tcg-fallback` 警告。
- **跨架构客户机**：本后端拒绝跨 ISA 客户机，由注册表回退到 qemu-tcg 后端。
  用户无需手动选择后端，自动选择机制确保跨架构客户机由正确的后端承担。

## 14. 与旧版的差异

| 方面 | 旧版 | 新版 |
| --- | --- | --- |
| 架构 | 单体探测 + 硬编码决策 | 分层：Snapshot → State → Environment → 策略函数 |
| 可测试性 | 依赖真实 Windows 宿主 | FakeWindowsEnvironment 注入，完全离线测试 |
| 跨架构 | 拒绝（`cross-arch-rejected` Error） | 拒绝（`guest-arch-unsupported` Error），由注册表回退到 qemu-tcg |
| tier | 永为 Native | 恒为 Native（命名机制）；`executionTierFor()` 承载每客户机真实 tier |
| ARM64 WHPX | 与 x86_64 相同处理 | WhpxDegraded 状态 + 警告 |
| WHPX 不可用 | isHostSupported() = false | isHostSupported() = true（QEMU 可用），同架构 TCG 回退 + tcg-fallback 警告 |
| 诊断码 | `whpx-not-available` / `whpx-not-supported` Error | `whpx-api-missing` / `whpx-hypervisor-not-present` Warning + `tcg-fallback` Warning |
| 状态机 | 无 | 5 态：NotWindowsHost / WhpxApiMissing / WhpxHypervisorNotPresent / WhpxDegraded / WhpxAvailable |
| 环境注入 | 无 | WindowsEnvironment 虚基类 + QemuWindowsEnvironment 生产实现 |
| 自注册 | registerBuiltinBackends() 函数 | static lambda 自注册 |
| 遗留兼容 | N/A | probeWhpx() / whpxAvailable() 保留向后兼容 |