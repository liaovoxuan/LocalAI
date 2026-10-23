# Linux KVM/QEMU 兼容后端

本文档描述 VirtualWorld 的 Linux KVM/QEMU 兼容虚拟化后端。该后端为 Linux 宿主提供
基于 QEMU 的硬件加速（KVM）和软件模拟（TCG）虚拟化能力，拒绝跨架构客户机
（guest-arch-unsupported），由注册表回退到 qemu-tcg 后端承担。

> 本后端**只**新增/修改以下文件，未改动 `CMakeLists.txt`、`README.md`、
> `VMManager`、`main`、公共 `VirtualizationEngine`/`Registry`/`Capabilities` 文件、
> Mac/Windows/Harmony 文件以及 `qemu-source`/`qemu-master`：
>
> - `src/LinuxKvmEngine.hpp`
> - `src/LinuxKvmEngine.cpp`
> - `tests/LinuxKvmEngineTests.cpp`
> - `docs/linux-virtualization.md`（本文件）

## 1. 分层设计

后端分为三层，便于测试与上层适配：

| 层 | 位置 | 说明 |
| --- | --- | --- |
| 数据层（纯 C++17） | `LinuxKvmEngine.hpp` 内联实现 | `LinuxHostSnapshot` 结构体：宿主探测结果的纯数据快照，无副作用。 |
| 策略层（纯 C++17） | `LinuxKvmEngine.hpp` 内联实现 | `linuxHostState()` / `linuxAcceleratorFor()` / `linuxValidateConfig()` / `linuxArgumentsAreWellFormed()` 等纯函数，接收快照+配置，返回决策。完全不依赖 Qt / QEMU / 文件系统，可在任意平台编译与单测。 |
| 平台层 | `LinuxKvmEngine.cpp` | `QemuLinuxKvmEnvironment`：真实宿主探测（Linux 宿主检测、RuntimeManager QEMU 探测、`/dev/kvm` QFileInfo 探测）、程序/参数解析、进程启动/停止。 |

`LinuxEnvironment` 虚基类是策略层与平台层之间的缝（seam）：测试注入
`FakeLinuxEnvironment`，生产代码使用 `QemuLinuxKvmEnvironment`。引擎通过
`LinuxEnvironment::snapshot()` 获取宿主快照，之后所有决策走纯函数，不再触碰平台层。

## 2. 宿主状态机

`LinuxHostState` 枚举定义了五种状态，由 `linuxHostState(snapshot, guestArch)` 决定：

| 状态 | 含义 | 同架构可用 | 硬件加速 | 状态码 |
| --- | --- | --- | --- | --- |
| `NotLinuxHost` | 非 Linux 宿主 | 否 | 否 | `host-unsupported` |
| `RuntimeMissing` | Linux 宿主但无 QEMU 运行时 | 否 | 否 | `runtime-missing` |
| `RuntimeNotRunnable` | QEMU 存在但不可执行 | 否 | 否 | `runtime-not-runnable` |
| `TcgOnly` | QEMU 可用但无 KVM | 是 | 否 | `degraded` |
| `Accelerated` | 同架构 KVM 可用且 API 版本已确认 | 是 | 是 | `available` |

> **跨架构客户机**：无论状态机处于哪一态，跨 ISA 客户机均被本后端拒绝
>（`supportsGuestArch()` 返回 false），由注册表回退到 qemu-tcg 后端。
> 状态机仅描述同架构客户机在 KVM 不可用时的 TCG 回退路径。

关键不变式：

- **tier 永不返回 Native**：本后端是 QEMU 兼容层，不是原生 hypervisor。
  `Accelerated` 状态对应 `HardwareAccelerated`，`TcgOnly` 对应 `SoftwareEmulation`。
- **跨架构拒绝**：跨 ISA 客户机被本后端拒绝（`supportsGuestArch()` 返回 false），
  而非静默降级到 TCG。拒绝使注册表能选择 qemu-tcg 后端。
- **Accelerated 需 KVM API 确认**：仅 `/dev/kvm` 存在且可访问不足以声称硬件加速；
  还需 `ioctl(KVM_GET_API_VERSION)` 成功返回正版本号。这防止 KVM 内核模块损坏
  或版本不兼容时误报硬件加速。
- **nativeExecution 永为 false**：客户机通过 QEMU 进程执行，不是宿主内核直接运行。

## 3. KVM API 探测

`LinuxHostSnapshot` 新增以下 KVM API 字段，由环境层在 `/dev/kvm` 可访问时填充：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `kvmApiProbed` | `bool` | `ioctl(KVM_GET_API_VERSION)` 是否已尝试 |
| `kvmApiVersion` | `int` | KVM API 版本号（预期值 12）；0 = 未探测或失败 |
| `kvmMaxVcpus` | `int` | `KVM_CAP_MAX_VCPUS` 结果；0 = 未知 |
| `kvmMaxMemorySlots` | `int` | `KVM_CAP_NR_MEMSLOTS` 结果；0 = 未知 |
| `kvmOpenErrno` | `int` | `open(/dev/kvm)` 的 errno；0 = 无错误 |

### Accelerated 条件增强

`Accelerated` 状态现在要求 **三个** 条件同时满足（此前仅两个）：

1. `/dev/kvm` 存在且可访问（`kvmDevicePresent && kvmDeviceAccessible`）
2. KVM API 已探测（`kvmApiProbed`）
3. KVM API 版本 > 0（`kvmApiVersion > 0`）

这防止 KVM 内核模块损坏或版本不兼容时误报硬件加速。

### KVM errno 诊断

`linuxKvmErrnoMessage(errno)` 将常见 errno 值转为中文诊断：

| errno | 常量 | 诊断 |
| --- | --- | --- |
| 2 | ENOENT | 内核未加载 KVM 模块 |
| 13 | EACCES | 权限不足，需加入 kvm 组 |
| 16 | EBUSY | 设备忙，可能被其他进程独占 |
| 22 | EINVAL | KVM 内核模块版本可能不兼容 |
| 25 | ENOTTY | KVM 内核模块可能未正确加载 |
| 28 | ENOSPC | 可能达到内存槽或 vCPU 上限 |
| 其它 | — | 通用 errno 报告 |

`linuxKvmHasErrno(snapshot)` 判断快照是否携带应展示的 KVM errno（需 `kvmDevicePresent && kvmOpenErrno != 0`）。

### vCPU 限制校验

`linuxVcpuExceedsKvmMax(config, snapshot)` 检查请求的 CPU 核数是否超过 KVM 报告的最大 vCPU 数。
仅在 `Accelerated` 状态且 `kvmMaxVcpus > 0` 时生效，超出时产生 `kvm-vcpu-exceeds-max` Warning。

### riscv64 KVM 成熟度

`linuxKvmIsMatureForArch(arch)` 判断架构的 KVM 支持是否成熟：

- **成熟**：`x86_64`、`aarch64` — 无附加警告
- **实验性**：`riscv64` — 允许 KVM 加速但附加 `kvm-maturity-experimental` Warning

## 4. 宿主 / 客户机支持矩阵

| 宿主 | 同架构客户机 | 跨架构客户机 |
| --- | --- | --- |
| Linux aarch64 + KVM | aarch64（KVM 硬件加速） | **拒绝**（guest-arch-unsupported） |
| Linux aarch64 无 KVM | aarch64（TCG 模拟） | **拒绝**（guest-arch-unsupported） |
| Linux x86_64 + KVM | x86_64（KVM 硬件加速） | **拒绝**（guest-arch-unsupported） |
| Linux x86_64 无 KVM | x86_64（TCG 模拟） | **拒绝**（guest-arch-unsupported） |
| Linux riscv64 + KVM | riscv64（KVM 硬件加速，实验性） | **拒绝**（guest-arch-unsupported） |
| Linux riscv64 无 KVM | riscv64（TCG 模拟） | **拒绝**（guest-arch-unsupported） |
| 非 Linux 宿主 | 不支持（`host-unsupported`） | 不支持 |

> 跨架构客户机被本后端拒绝后，由 `VirtualizationEngineRegistry` 回退到
> `qemu-tcg` 后端承担。本后端不运行任何跨 ISA 客户机。

候选客户机架构（`linuxCandidateGuestArches()`）：`aarch64`、`x86_64`、`riscv64`。

架构规范化（`linuxCanonicalGuestArch()`）：
- `arm64` / `aarch64` → `aarch64`
- `amd64` / `x86_64` → `x86_64`
- `riscv64` / `riscv64gc` → `riscv64`
- 其它架构（`ppc64`、`i386`、`m68k` 等）返回空，表示本后端不覆盖。

## 5. 诊断码

| 代码 | 级别 | 含义 |
| --- | --- | --- |
| `host-unsupported` | Error | 非 Linux 宿主 |
| `runtime-missing` | Error | QEMU 运行时未找到 |
| `runtime-not-runnable` | Error | QEMU 可执行文件存在但无法运行 |
| `runtime-probe-timeout` | Warning | QEMU `--version` 探测超时 |
| `degraded` | Warning | TCG 模拟（无 KVM 硬件加速） |
| `software-emulation` | Warning | 同 `degraded`，用于 validate 输出 |
| `hardware-acceleration` | Info | KVM 硬件加速可用 |
| `available` | Info | 后端完全可用 |
| `guest-arch-missing` | Error | 未指定客户机架构 |
| `guest-arch-unsupported` | Error | 客户机架构不受本后端支持（空/未知架构或跨 ISA） |
| `backend-mismatch` | Error | 配置指定了其它后端（如 `system-software`） |
| `cpu-count-invalid` | Error | CPU 核心数 ≤ 0 |
| `memory-too-small` | Error | 内存 < 128 MB |
| `disk-path-empty` | Error | 磁盘路径为空 |
| `path-invalid` | Error | 路径包含不安全字符（NUL / 换行 / 回车） |
| `argument-invalid` | Error | 参数包含不安全字符或拼接标志 |
| `unsupported-arguments` | Warning | 配置包含不支持的 QEMU 参数 |
| `kvm-api-version` | Info | KVM API 版本、最大 vCPU 数、最大内存槽 |
| `kvm-api-error` | Warning | KVM API 探测失败（errno 诊断） |
| `kvm-vcpu-exceeds-max` | Warning | 请求的 CPU 核数超过 KVM 报告的最大 vCPU 数 |
| `kvm-maturity-experimental` | Warning | 架构的 KVM 支持尚在成熟中（如 riscv64） |

所有 `level == Error` 的问题都会阻止 `start()` 执行。

## 6. 配置校验

`linuxValidateConfig(config, snapshot)` 执行以下检查（纯策略，不触碰文件系统，
按优先级排列，early-return 语义）：

1. **后端匹配**：`config.backend` 必须为空或 `linux-kvm`，否则报 `backend-mismatch`
   （Error，early return）。
2. **客户机架构非空**：`config.architecture` 经 trim 后非空，否则报
   `guest-arch-missing`（Error，early return）。
3. **客户机架构在候选列表**：经规范化后在本后端的候选列表中
   （aarch64/x86_64/riscv64），否则报 `guest-arch-unsupported`（Error，early return）。
4. **宿主状态**：快照状态必须可用（`TcgOnly` 或 `Accelerated`），否则报
   `host-unsupported` / `runtime-missing` / `runtime-not-runnable`（Error，early return）。
5. **跨 ISA 拒绝**：客户机架构与宿主架构不同时，报 `guest-arch-unsupported`
   （Error，early return）。KVM 只支持同架构虚拟化，跨 ISA 是 qemu-tcg 的职责。
6. **CPU 核心数**：必须 > 0，否则报 `cpu-count-invalid`。
7. **内存**：必须 > 0，否则报 `memory-too-small`。
8. **磁盘路径**：非空且不含不安全字符，否则报 `disk-path-empty` / `path-invalid`。
9. **内核/Initrd 路径**：如果指定，不含不安全字符，否则报 `path-invalid`。
10. **额外参数**：不含 NUL / 换行 / 回车，否则报 `argument-invalid`。
11. **不支持的参数**：`config.unsupportedArgs` 报 `unsupported-arguments`（Warning）。
12. **宿主状态诊断**：根据状态机报告 `runtime-missing` / `runtime-not-runnable`
    / `software-emulation` / `hardware-acceleration`。
13. **vCPU 超限**：当 KVM 报告了最大 vCPU 数且请求的 CPU 核数超过此限制时，
    报 `kvm-vcpu-exceeds-max`（Warning）。这是软限制，不阻止启动。
14. **KVM errno**：当 `/dev/kvm` 存在但 KVM API 探测失败（`kvmOpenErrno != 0`）时，
    报 `kvm-api-error`（Warning），附带 errno 对应的中文诊断信息。
15. **KVM 成熟度**：当后端处于 `Accelerated` 状态且客户机架构不是成熟架构
    （当前成熟架构为 x86_64 和 aarch64）时，报 `kvm-maturity-experimental`（Warning）。

## 7. 参数安全

`linuxArgumentsAreWellFormed(arguments)` 检查 QEMU 参数向量的结构性安全：

- **空向量**：拒绝（至少需要可执行文件名）。
- **拼接标志**：以 `-` 开头且包含空白的元素被拒绝（如 `"-machine virt,accel=tcg"`），
  因为这是多个标志被错误地合并为一个字符串的标志。
- **路径/值中的空白**：允许（如 `"-kernel" "/tmp/My VMs/vmlinuz"`），因为它们是独立的元素。
- **NUL / 换行 / 回车**：任何参数中包含这些字符都被拒绝。
- **空字符串参数**：拒绝。

`linuxContainsUnsafeCharacter(string)` 检查单个字符串是否包含 NUL、换行或回车。

这些检查在 `start()` 调用链中执行，确保恶意或损坏的配置不会到达 QEMU 进程。

## 8. 生命周期与错误传播

### start(config, processId, error)

1. 获取宿主快照，检查状态是否可用。不可用则返回 false 并填充 error。
2. 规范化客户机架构，检查是否支持。不支持则返回 false。
3. **拒绝跨 ISA 客户机**：客户机与宿主架构不同时返回 false，不启动进程。
4. 执行配置校验（`linuxValidateConfig`）。有 Error 级问题则返回 false。
5. 通过 `environment->program(config)` 获取 QEMU 可执行文件路径。为空则返回 false。
6. 通过 `environment->arguments(config)` 获取参数向量。
7. 检查参数向量是否良好（`linuxArgumentsAreWellFormed`）。不良则返回 false。
8. 通过 `environment->startProcess(config, processId, error)` 启动进程。
9. 如果启动成功但 `processId == 0`，返回 false（永不声称成功却没有真实 PID）。

### stop(processId, error)

1. 检查 `processId > 0`。否则返回 false。
2. 委托 `environment->stopProcess(processId, error)`。

### 错误码（statusCode）

| 状态码 | 含义 |
| --- | --- |
| `host-unsupported` | 非 Linux 宿主 |
| `guest-arch-unsupported` | 客户机架构不受支持（空/未知或跨 ISA） |
| `runtime-missing` | QEMU 未找到 |
| `runtime-not-runnable` | QEMU 不可执行 |
| `degraded` | TCG 模拟（可用但无硬件加速） |
| `backend-mismatch` | 配置指定了其它后端 |
| `available` | 后端完全可用 |

> `statusCode()` 的优先级：`host-unsupported` > `guest-arch-unsupported`（空/未知） >
> `guest-arch-unsupported`（跨 ISA） > `runtime-missing` > `runtime-not-runnable` >
> `degraded` > `available`。

## 9. 探测缓存

`LinuxKvmEngine` 内部缓存宿主快照：首次调用 `probe()` / `isHostSupported()` /
`supportsGuestArch()` 时读取，后续调用复用。`invalidateProbe()` 强制重新读取。

这避免了重复的 QEMU 探测和 `/dev/kvm` 文件系统检查。

## 10. 自注册

`LinuxKvmEngine.cpp` 底部的静态 lambda 在静态初始化阶段将本后端注册到
`VirtualizationEngineRegistry::instance()`：

```cpp
namespace {
const bool vwLinuxKvmBackendRegistered = []{
    VirtualizationEngineRegistry::instance().registerBackend(
        backend_id::linuxKvm(),
        []() -> VirtualizationEnginePtr { return makeLinuxKvmEngine(); });
    return true;
}();
}
```

这确保后端在 `main()` 之前就已注册，无需手动调用注册函数。

## 11. 工厂函数

| 函数 | 说明 |
| --- | --- |
| `makeLinuxEnvironment()` | 创建 `QemuLinuxKvmEnvironment`（真实平台层） |
| `linuxKvmEngineAvailable()` | 返回本后端在当前宿主是否可用 |
| `makeLinuxKvmEngine()` | 创建 `LinuxKvmEngine` 实例（使用真实环境） |

## 12. 与共享接口的适配

| 共享 `vw::VirtualizationEngine` | 本后端 |
| --- | --- |
| `backendId()` | `"linux-kvm"` |
| `displayName()` | `"Linux KVM/QEMU"` |
| `tier()` | 动态：`HardwareAccelerated`（KVM 可用且同架构）或 `SoftwareEmulation`（TCG only） |
| `isHostSupported(reason)` | 基于 `linuxHostState()` 决策 |
| `supportsGuestArch(arch, reason)` | 架构规范化 + 候选列表 + 同 ISA 检查；跨架构**拒绝**（false） |
| `probe()` | 返回 `EngineProbeResult`，含诊断码 |
| `probeFor(arch)` | 针对特定架构的探测；跨 ISA 报告 `guest-arch-unsupported` |
| `validate(config)` | `linuxValidateConfig()` + 引擎级检查 |
| `start(config, pid, error)` | 环境注入 + 安全校验 + 进程启动 |
| `stop(pid, error)` | 进程停止 |
| `diagnose(config)` | 人类可读诊断文本 |
| `statusCode(config)` | 机器可读状态码 |
| `requirements()` | 需求列表（Linux、QEMU、/dev/kvm） |
| `runsInProcess()` | `false`（QEMU 子进程） |
| `buildArguments(config)` | 返回参数向量（委托环境层） |
| `programFor(config)` | 返回 QEMU 可执行文件路径（委托环境层） |

## 13. 构建与测试

由于不得修改 `CMakeLists.txt`，本后端暂未接入 CMake 目标；测试可用如下命令构建
（macOS / Linux）：

```bash
clang++ -std=c++17 -Wall -Wextra -Wno-unused-parameter \
  -Isrc \
  tests/LinuxKvmEngineTests.cpp \
  src/LinuxKvmEngine.cpp \
  src/Platform.cpp \
  src/VirtualizationEngine.cpp \
  src/VirtualizationEngineRegistry.cpp \
  src/VirtualizationCapabilities.cpp \
  src/VmConfig.cpp \
  src/QemuEngine.cpp \
  src/QemuParser.cpp \
  src/RuntimeManager.cpp \
  -framework QtCore -framework DiskArbitration \
  -o /tmp/vw_linux_kvm_tests
/tmp/vw_linux_kvm_tests
```

测试覆盖：

- 后端 id / name / tier 一致性；
- 架构规范化与候选列表；
- 宿主状态机五种状态的转换与状态码；
- `RuntimeMissing` 消息的 `%1` 插值正确性；
- tier 永不返回 Native、nativeExecution 永为 false；
- 非 Linux 宿主永不支持、永不启动进程；
- RuntimeMissing / RuntimeNotRunnable / RuntimeProbeTimeout 路径；
- TCG 降级可用但诚实（Warning 而非 Error）；
- KVM 加速可用且诚实（Info 而非 Error）；
- KVM API 探测决定 Accelerated 状态（无 API 版本 → TcgOnly）；
- KVM errno 诊断传播（validate / probe）；
- vCPU 超限校验（kvm-vcpu-exceeds-max Warning）；
- riscv64 KVM 成熟度警告（kvm-maturity-experimental Warning）；
- KVM API 版本信息在 probe / diagnose 中输出；
- 配置校验（后端匹配、架构、跨 ISA 拒绝、CPU、内存、磁盘路径、NUL/换行注入、不支持的参数）；
- 参数向量安全（空向量、拼接标志、NUL/换行、空字符串）；
- `buildArguments` 正确设置 accelerator 字段；
- 参数向量永不产生 shell 字符串（保持 QStringList 形式）；
- start 成功 / 失败 / 零 PID / 畸形参数 / 无程序路径；
- stop 有效 PID / 无效 PID / 环境失败 / 无环境引擎；
- 探测缓存与 `invalidateProbe()`；
- 运行时消失与恢复（invalidateProbe 使缓存失效后重新探测）；
- requirements 包含 /dev/kvm、QEMU、Linux；
- `probeFor` 架构特定探测；跨 ISA 报告 guest-arch-unsupported；
- 跨 ISA 拒绝：即使有模拟器也拒绝，validate 报 guest-arch-unsupported Error，start 不产生进程；
- 真实宿主不被误报；
- 真实环境工厂与引擎；
- 注册表自注册与自动选择审计。
- **故障注入**：probe 缓存失效后状态机结果一致；validate 错误在各宿主状态下正确传播；
  start 立即失败时错误正确传播且不修改调用者 pid 指针；start 成功但 PID=0 或负值时
  正确处理；stop 失败时错误正确传播；stop 对 PID=0 和负值 PID 返回 false
- **并发压力**：20 轮连续 invalidateProbe() 不崩溃；start/stop 与 probe 失效交叉执行；
  20 轮 start/stop 循环不泄漏
- **Linux 特定覆盖**：KVM 设备缺失（kvmDevicePresent=false）；KVM 设备权限拒绝
  （kvmDeviceAccessible=false）；KVM API 版本为 0（TcgOnly 降级）；KVM API 版本为负值；
  x86_64 同架构 KVM 加速；跨架构 start 拒绝；riscv64 不被误判为成熟架构；
  所有 reason 非空；statusCode 在多次调用间稳定；probe 诊断非空；
  畸形配置 validate；KVM 设备消失后恢复（invalidateProbe 重新探测）；
  KVM errno 传播（kvmOpenErrno 在 validate/probe 中正确报告为 kvm-api-error）
- **start 失败语义**：start() 失败时不修改调用者传入的 pid 指针（内部用局部变量，
  仅成功时写入 *processId）——测试验证此行为

不满足条件时（非 Linux 宿主、无 QEMU 运行时）相关用例输出 `SKIP` 而非失败。

## 14. 集成测试

以下测试需要在真实 Linux 宿主上执行，不属于本测试套件：

- QEMU `--version` 探测返回正确的版本和架构列表；
- `/dev/kvm` 存在且可访问时 KVM 加速路径正确；
- `/dev/kvm` 存在但不可访问时回退同架构 TCG；
- QEMU 子进程真实启动与停止；
- 跨架构客户机被本后端拒绝，注册表回退到 qemu-tcg 后端；
- RuntimeManager QEMU 路径解析在各种安装位置下的正确性。

这些集成测试应作为 CI 流水线的一部分在 Linux runner 上执行。

> **macOS 交叉编译限制**：本测试套件通过 `FakeLinuxEnvironment` 在 macOS 上
> 编译和运行，无需真实 Linux 宿主或 `/dev/kvm`。但 KVM 真机行为
> （`/dev/kvm` 访问、`ioctl(KVM_GET_API_VERSION)`、QEMU `-accel kvm` 进程启动）
> 只能在 Linux runner 上验证。

## 15. 已知限制

- **riscv64 KVM**：riscv64 宿主上的 KVM 支持现在允许硬件加速（`Accelerated` 状态），
  但附加 `kvm-maturity-experimental` 成熟度警告，因为 RISC-V KVM 在主线内核中
  仍在积极开发。x86_64 和 aarch64 的 KVM 支持被视为成熟，不附加此警告。
- **多 QEMU 二进制**：当前只探测一个 QEMU 路径（通过 RuntimeManager）。
  如果系统安装了多个版本的 QEMU，只使用第一个找到的。
- **KVM 嵌套虚拟化**：未检测宿主是否支持嵌套虚拟化（如 `kvm_intel.nested=1`）。
- **cgroup / seccomp**：未检查 QEMU 进程是否受 cgroup 或 seccomp 限制。
- **Huge pages**：未检查 `/proc/meminfo` 中的 HugePages 可用性。