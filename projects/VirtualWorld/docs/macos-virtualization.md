# macOS 原生虚拟化后端（Apple Virtualization.framework）

本文档描述 VirtualWorld 的 macOS 原生虚拟化后端。该后端通过 Apple 的
`Virtualization.framework` 为 macOS 宿主提供 1:1 原生虚拟化能力，并与既有的
QEMU 后端共存：跨架构客户机一律回退 QEMU TCG。

> 本后端**只**新增/修改以下文件，未改动 `CMakeLists.txt`、`README.md`、
> `VMManager`、`main`、公共 `VirtualizationEngine`/`Registry` 文件、
> Windows/Linux/Harmony 文件以及 `qemu-source`/`qemu-master`：
>
> - `src/MacVirtualizationEngine.hpp`
> - `src/MacVirtualizationEngine.mm`
> - `tests/MacVirtualizationEngineTests.cpp`
> - `docs/macos-virtualization.md`（本文件）

## 1. 分层设计

后端分为两层，便于测试与上层适配：

| 层 | 位置 | 说明 |
| --- | --- | --- |
| 策略层（纯 C++17） | `MacVirtualizationEngine.hpp` 内联实现 | 架构规范化、能力矩阵、诊断、配置校验、结果类型。完全不依赖 Objective-C / Qt，可在任意平台编译与单测。 |
| 平台层（Objective-C++） | `MacVirtualizationEngine.mm` | 真实宿主探测、沙盒权限诊断、`VZVirtualMachineConfiguration` 构建与校验、`start`/`stop` 生命周期、异步回调。 |

头文件通过 `VW_MACVIRT_HAS_FRAMEWORK` 决定平台方法的定义位置：

- `VW_MACVIRT_HAS_FRAMEWORK == 1`：macOS + `Virtualization.framework` 头可用，
  平台方法声明在头文件、实现位于 `.mm`。
- `VW_MACVIRT_HAS_FRAMEWORK == 0`：其它平台，头文件提供内联的不可用回退实现，
  保证 Linux/Windows/Harmony 构建不会出现链接错误。

## 2. 宿主 / 客户机支持矩阵

| 宿主 | 原生客户机（Virtualization.framework） | 跨架构 |
| --- | --- | --- |
| Apple Silicon（arm64），macOS 11+ | arm64 Linux（`VZLinuxBootLoader`）；macOS 11 无 macOS 客户机 | 交 QEMU TCG |
| Apple Silicon，macOS 12+ | arm64 Linux + arm64 macOS（IPSW 恢复，见 §7） | 交 QEMU TCG |
| Intel（x86_64），macOS 11+ | x86_64 Linux | 交 QEMU TCG |
| Intel | **不支持** macOS 客户机（仅 Apple Silicon） | 交 QEMU TCG |

关键不变式：

- `supportsGuestArch(arch)` 仅在 `normalizeArch(arch)` 等于宿主原生架构时为真。
- `i386`/`arm`/`ppc`/`riscv64` 等即使能被 QEMU 运行，也**不会**被本后端宣称支持。
- 未实现的能力一律返回明确的 `unsupported` + 稳定 `code` + 可读 `reason`，
  绝不虚假宣称成功。

## 3. 能力码（`Capability::code`）

| 能力 | 成功码 | 常见失败码 |
| --- | --- | --- |
| Linux 原生引导 | `linux-native-boot` | `host-not-macos`、`macos-11-required`、`framework-unavailable`、`host-arch-unsupported` |
| macOS IPSW 恢复探测 | `macos-ipsw-probe-only` | `macos-12-required`、`macos-guest-apple-silicon-only`、`framework-unavailable` |
| 网络（NAT virtio-net） | `networking-supported` | `framework-unavailable` |
| 目录共享（virtio-fs） | `directory-sharing-supported` | `macos-12-required`、`framework-unavailable` |
| 图形（VM view / virtio） | `graphics-supported` | `macos-12-required`、`framework-unavailable` |
| 音频（virtio-sound） | `audio-supported` | `macos-12-required`、`framework-unavailable` |

所有 `supported == false` 的能力都保证 `reason` 与 `code` 非空。

## 4. 诊断码（`diagnoseHost`）

| 代码 | 级别 | 含义 |
| --- | --- | --- |
| `host-not-macos` | Error | 非 macOS 宿主 |
| `macos-11-required` | Error | 低于 macOS 11 |
| `framework-unavailable` | Error | 系统满足版本但框架不可用 |
| `framework-available` | Info | 框架可用（含系统/框架版本） |
| `sandbox-entitlement-missing` | Error | 已沙盒化但缺少 `com.apple.security.virtualization` |
| `sandbox-entitlement-present` | Info | 沙盒 + 权限齐备 |
| `not-sandboxed` | Info | 未沙盒化，无需该权限 |
| `host-arch-unsupported` | Error | CPU 架构不受支持 |
| `native-guest-arch` | Info | 原生客户机架构 |
| `cross-arch-qemu-tcg` | Info | 跨架构回退 QEMU TCG |
| `nested-virtualization` | Warning | `kern.hv_vmm_present` 表明处于嵌套虚拟化 |

## 5. 配置校验码

`MacVirtualizationEngine::validate(config)` = 纯策略校验 + 文件系统存在性校验。

| 代码 | 级别 | 条件 |
| --- | --- | --- |
| `guest-os-missing` | Error | 未指定 `guestOs` 且非 IPSW 恢复 |
| `guest-os-unsupported` | Error | Virtualization.framework 不支持的 guest OS |
| `guest-arch-missing` | Error | Linux 客户机缺少 `architecture` |
| `guest-arch-unsupported` | Error | 宿主无法原生虚拟化该架构（回退 QEMU） |
| `kernel-required` | Error | 未提供内核镜像 |
| `kernel-missing` / `initrd-missing` / `ipsw-missing` / `disk-missing` | Error | 路径存在性校验失败（仅平台层） |
| `linux-guest-unsupported` | Error | 宿主能力不支持 Linux |
| `macos-guest-unsupported` | Error | 宿主能力不支持 macOS 客户机 |
| `ipsw-required` | Error | 需要 IPSW 路径 |
| `cpu-count-invalid` | Error | `cpuCount` 不在 1..64 |
| `memory-too-small` | Error | `< 512 MB` |
| `disk-path-empty` | Error | 空磁盘路径 |
| `directory-sharing-unsupported` / `directory-sharing-path-missing` | Warning | 共享目录相关 |
| `graphics-unsupported` / `audio-unsupported` / `network-unsupported` | Warning | 可选设备能力不足 |

## 6. 生命周期与错误传播

`MacVirtualizationEngine` 暴露同步与异步两套接口：

- `start(config)` / `stop()`：阻塞式适配器，内部以 `std::mutex` +
  `std::condition_variable` 等待 `Virtualization.framework` 回调，超时返回
  `timeout`。调用线程不会触碰 Objective-C 对象；框架回调统一在一条串行
  `dispatch_queue` 上执行。
- `startAsync(config, cb)` / `stopAsync(cb)`：真正的启动放到工作线程，回调在
  工作线程触发，且**恰好一次**。校验/能力类失败走同步快路径，不启动线程。

错误通过 `RunResult` 传播：

| `code` | 含义 | 上层动作 |
| --- | --- | --- |
| `config-invalid` | 校验失败，`issues` 携带明细 | 修正配置或换后端 |
| `guest-arch-unsupported` | 跨架构 | 回退 QEMU TCG |
| `linux-guest-unsupported` | 宿主能力不足 | 回退 QEMU |
| `macos-guest-unsupported` | 非 Apple Silicon / 旧系统 | 回退 QEMU |
| `macos-ipsw-restore-unsupported` | 校验通过但安装流程未接线 | 回退 QEMU（见 §7） |
| `host-not-macos` / `macos-11-required` / `framework-unavailable` | 环境不满足 | 回退 QEMU |
| `sandbox-entitlement-missing` | 权限缺失 | 修复签名/权限 |
| `framework-validation-failed` | 框架拒绝配置 | 展示 `detail` |
| `configuration-build-failed` | 设备构建失败（如磁盘打不开） | 展示 `detail` |
| `start-failed` / `stop-failed` | 框架运行期错误 | 展示 `detail` |
| `already-running` / `not-running` | 状态冲突 | 调整调用时序 |
| `engine-destroyed` | 异步启动在途时引擎被销毁，已回滚该 VM | 重新创建引擎 |
| `timeout` | 回调超时（病态挂起） | 视为不确定，建议重启后端 |
| `guest-stopped-with-error` | 客户机异常停止（委托回调） | 上报错误 |

### 异步 / 内存 / 线程安全

- `.mm` 同时支持 **ARC 与 MRC**，且两条路径都无泄漏：`VW_AUTORELEASE`、
  `vwRetainObjc` / `vwReleaseObjc`、`VW_UNBRIDGE` 屏蔽差异；仅在非 ARC 下编译
  `-dealloc`。`vwReleaseObjc` 在 MRC 下用 `-release` 配对 `-retain`（不能用
  `CFRelease`，因为 runner 不是 CF 类型或 toll-free bridged 对象）。
  ARC 与非 ARC 两种方式均已编译、链接并运行测试。
- **`VZVirtualMachine` 的每一次操作都在它自己的串行 `dispatch_queue` 上发起**
  （委托赋值、`startWithCompletionHandler:`、`requestStopWithError:` /
  `stopWithCompletionHandler:`、解除委托），符合框架“every operation on the
  virtual machine must be done on that queue”的要求。`vwRunOnVirtualizationQueue`
  通过 queue-specific key 检测重入，已在队列上时直接内联执行，避免自死锁。
- `VZVirtualMachine.delegate` 是 **weak** 属性，因此不存在经由委托的引用环。
  唯一存在的环是 `session → runner → machine → 在途 start block → session`，
  在 start 回调返回后即消失；`stop()` 仍会 detach 并释放 runner，避免已停止的
  VM 被残留 session 保活。
- 所有回调都在 `Session::mutex` 内写入结果再 `notify_all`，避免悬垂读取。
- **异步生命周期**：`startAsync` / `stopAsync` 的工作线程持有各自的
  `std::shared_ptr<AsyncControl>`（引擎也持有一份），因此即使引擎先被销毁，
  工作线程仍能安全加锁。工作线程**只有在持有该锁且观察到
  `abandoned == false` 时**才解引用引擎；析构函数在同一把锁内置
  `abandoned = true`，此后工作线程再也无法触达引擎。因此在异步操作在途时销毁
  引擎是安全的：在途启动会被回滚并回报 `engine-destroyed`，而不是静默丢弃一个
  正在运行的 VM。
- `start()` / `startAsync()` 通过同一把锁上的 `pending` 标志抢占唯一的 VM 槽位，
  因此并发启动（含同步与异步混用）会被明确拒绝为 `already-running`，不会出现
  两个 VM 同时构建。
- `stop()` 超时路径不释放 machine，避免与仍在途的回调竞争（已知取舍）。

### `Session::runnerRaw` 的所有权不变量

runner 以类型擦除的 `void*`（`Session::runnerRaw`）持有，它是**唯一一处跨越
C++ 与 Objective-C 所有权边界的字段**，因此有一条明确规则：

- 该字段**只在持有 `Session::mutex` 时**被读、写或清空；
- 任何必须把 runner 交给框架或 `dispatch_queue` 的函数（`vwStopSession()`、
  `vwStartLinuxVm()` 的失败分支），在锁内把 +1 引用**转移**到本地 `void* owned`
  并立即把字段置空 —— 本地因此成为唯一持有者，`~Session()` 不会再对同一引用
  二次释放，在途的框架回调也永远看不到撕裂指针；
- 转移后仍在锁外调用 `vwRequestRunnerStop()` / `vwDetachRunnerHandle()`：
  持有 `Session::mutex` 去发起框架调用会与 `requestStopWithCompletion:` 的
  回调（它同样要取这把锁）形成死锁风险；
- `owned` 在超时路径**故意不释放**，代价是一个有界的泄漏 —— 停止回调可能仍在
  机器上排队，此刻释放会与它竞争。这与 `start()` 的 `timeout` 路径同一取舍。

（修复前 `vwStopSession()` 在锁外读 `runnerRaw`，而 `vwStartLinuxVm()` 在锁外写、
`~Session()` 释放同一个 +1 引用：stop 与失败的 start 竞争会造成引用计数下溢，
stop 与 session 析构竞争则会把悬垂指针交给 `vwDetachRunnerHandle()`。）

### 自动化覆盖：`setRunnerStubForTesting()` 计数替身

上述不变量现在由 `tests/MacVirtualizationEngineTests.cpp` 的
`testChaosRunnerOwnershipInvariant()` 确定性覆盖，**不启动任何真实 VM**：

- `setRunnerStubForTesting(true)` 武装一个**计数替身** `VWMacVirtualizationRunnerStub`，
  它走与真实 runner **完全相同**的所有权路径（`vwRetainObjc` / `vwReleaseObjc`），
  但 `dealloc` 会被计数，且永远不会被交给 `Virtualization.framework`、不会真正引导
  客户机。替身的 +1 精确等于 `Session::runnerRaw` 持有的显式所有权（不使用
  autorelease），因此 `installed == destroyed` 是**精确**不变量，而非延迟平衡。
- 固定种子驱动 `start → stop`、`start → 销毁（不 stop）`、`startAsync → 销毁（在途）`
  三种交错各 12 轮，外加 3 轮 N 线程并发 stop 风暴；每轮结束后断言
  `runnerStubInstalled() == runnerStubDestroyed()`，最终断言全局计数严格相等、零泄漏、
  零过度释放。
- 闸门 `setStartGateForTesting()` 保证工作线程在进入框架握手**之前**被拦下，断言的是
  “引擎销毁时工作线程不再解引用它”这一协议，而**不是**“真实启动一个 VM 再销毁”。
- 该测试在 Release / ASan / TSan 三套构建下运行；TSan 负责验证替身计数与
  `Session::mutex` 上的所有权转移无数据竞争。

> 这是**协议级**覆盖：它证明“每次安装恰好释放一次”这一所有权契约成立，但仍**不**
> 覆盖真实框架握手（`VZVirtualMachine` 构建与 `startWithCompletionHandler:`）。真实
> 引导路径的首次执行仍只在实机上发生。

## 7. macOS 客户机（IPSW restore）现状

- **Linux 引导**：完整构建配置（`VZLinuxBootLoader`、内核/initrd/命令行、
  CPU/内存、virtio 块设备、NAT 网络、熵设备、macOS 12+ 目录共享）并真实启动。
- **macOS 客户机**：仅完成能力探测与配置校验。`VZMacPlatformConfiguration` /
  `VZMacAuxiliaryStorage` / `VZMacOSRestoreImage` / `VZMacOSInstaller` 的安装
  流程**尚未接线**。因此即使能力探测通过（Apple Silicon + macOS 12+），
  `start()` 也返回 `macos-ipsw-restore-unsupported`，明确要求上层回退 QEMU。
  这样避免“看似成功”的假象。

## 8. 与共享接口的适配

WorkBuddy 的公共头文件**已经落地**（`src/VirtualizationEngine.hpp`、
`src/VirtualizationCapabilities.hpp`，均只读取、未修改）：

- backend id：本后端的 `backendId()` = `"macos-virtualization"`，与
  `vw::backend_id::macOSVirtualization()` 完全一致。
- tier：`backendTierName()` = `"native"`，对应
  `VirtualizationTier::Native`。
- 共享头把 macOS 的 x86_64 与 aarch64 宿主**都**指向本后端，与本后端的
  1:1 宿主/客户机策略一致。
- 架构规范化语义一致：双方都**不**把窄 ISA（`i386`/`arm`）折叠进宽 ISA
  （`x86_64`/`aarch64`），因为原生 hypervisor 只能整宽度暴露宿主 ISA。

由于严格的文件所有权，本后端**不**新建/修改任何公共文件，也**不**定义
`makeMacOSVirtualizationEngine()` / `macOSVirtualizationEngineAvailable()`
（Qt 适配层与注册表集成由 WorkBuddy 负责，两边同时定义会导致重复符号）。
本后端发布一个 **Qt-free** 的适配面，映射关系为 1:1：

| 共享 `vw::VirtualizationEngine` | 本后端 |
| --- | --- |
| `backendId()` | `backendId()` → `"macos-virtualization"` |
| `displayName()` | `backendName()` |
| `tier()` | `backendTierName()` → `"native"` |
| `isHostSupported(QString* reason)` | `isHostSupported(std::string* reason)` |
| `supportsGuestArch(arch, QString* reason)` | `supportsGuestArch(arch, std::string* reason)` |
| `probe()` → `EngineProbeResult` | `probe()` → `ProbeResult` |
| `validate(VmConfig)` → `QVector<EngineIssue>` | `validate(Config)` → `std::vector<Issue>` |
| `start(VmConfig, pid, err)` | `start(Config)` → `RunResult` |
| `stop(pid, err)` | `stop()` → `RunResult` |
| `diagnose()` / `statusCode()` | `RunResult.code` + `Issue.code`（稳定字符串） |
| `EngineCapabilities::canStart/canStop` | `Capabilities::canStart/canStop` |
| `EngineIssue.code` | `Issue.code`（建议原样转发） |

`reason` 出参约定：**先清空，仅在答案为 false 时写入**，因此适配层可以直接把它
转成 `QString* reason` 而不会在成功时产生虚假告警。

`canStart` / `canStop` 表达的是“`start()` 是否真的能工作”，而**不是**“宿主理论上
能不能做”：macOS 客户机恢复路径只做了探测，所以它永远不会把 `canStart` 置真
（见 §7）。这条不变式由 `testCanStartHonesty` 守住。

配置字段映射建议：

| 共享 `VmConfig` | `macvirt::Config` |
| --- | --- |
| `guestOs` | `guestOs` |
| `architecture` | `architecture` |
| `boot.kernelPath` | `kernelPath` |
| `boot.initrdPath` | `initrdPath` |
| `boot.kernelAppend` | `kernelCommandLine` |
| `macRecovery.ipswPath` | `ipswPath` |
| `cpuCores` | `cpuCount` |
| `memoryMb` | `memoryMb` |
| `disks[].path` | `diskPaths` |
| `networks` 非空 | `networkEnabled` |
| `sharedDirectories` | `directorySharingEnabled` + `sharedDirectoryPath` |
| `graphics` | `graphicsEnabled` |
| `audio.enabled` | `audioEnabled` |
| `macRecovery.ipswPath` 非空 | `restoreFromIPSW = true` |

## 9. 权限 / 沙盒

- 未沙盒化的 App 直接使用 `Virtualization.framework`，无需额外权限。
- 沙盒化的 App 必须声明 `com.apple.security.virtualization`。缺少该权限时后端
  **全面**报告不可用，而不只是在 `start()` 时才失败：
  - `hostMayCreateVirtualMachine(host)` 返回 false；
  - `probeHost().hostSupported` 返回 false（此前只看 `frameworkAvailable`，会
    让一个"每次创建 VM 都失败"的进程宣称受支持——这违反共享契约
    "不能真正运行配置的后端必须在 validate() 说明"）；
  - `Capabilities::linuxGuest` / `macOSGuestIPSW` / `macOSGuestBoot` 的 code 为
    `sandbox-entitlement-missing`，`canStart` / `canStop` 均为 false；
  - `validate()` 因此报错，注册表会改选 QEMU，而不是选到本后端再在启动时炸掉；
  - `diagnoseHost()` 报 `sandbox-entitlement-missing`（Error）；
  - `start()` 仍然直接失败（`vwPreflight` 中的同一条判定）。
- `Capabilities::frameworkAvailable` 仍然只表示"框架本身可加载"，与权限是两条
  独立的轴：权限不足时该字段依旧为 true，但 `hostSupported` 为 false。
- 诊断通过 `SecTaskCopyValueForEntitlement` 读取 `com.apple.security.app-sandbox`
  与 `com.apple.security.virtualization`。

## 10. 构建与测试

由于不得修改 `CMakeLists.txt`，本后端暂未接入 CMake 测试目标；测试可用如下命令
构建（macOS，ARC）：

```bash
clang++ -std=c++17 -fobjc-arc -Wall -Wextra -Wno-unused-parameter \
  -Isrc \
  tests/MacVirtualizationEngineTests.cpp src/MacVirtualizationEngine.mm \
  -framework Foundation -framework Virtualization -framework Security \
  -o /tmp/vw_macvirt_tests
/tmp/vw_macvirt_tests
```

同时验证过：

- 非 ARC（MRC）编译/链接/运行（注意：当前 `CMakeLists.txt` 未开启 `-fobjc-arc`，
  所以 **MRC 是实际会走的路径**，两条路径都已实测通过）；
- AddressSanitizer 与 ThreadSanitizer 下 0 报告；
- `-mmacosx-version-min=11.0 / 12.0 / 13.0` 搭配
  `-Werror=unguarded-availability-new`，ARC 与 MRC 下均 0 警告，说明所有
  macOS 11/12+ API 都被 `@available` 正确守护；
- 强制 `VW_MACVIRT_HAS_FRAMEWORK=0` 的便携回退路径可独立编译运行；用它产生的
  预处理结果中不包含任何 `Foundation` / `Virtualization` / `Security` /
  `dispatch` 头，证明非 macOS 构建不会引用 Apple 框架文件。
- **与共享契约的端到端联调**：把本后端的 `.mm` 与 WorkBuddy 的
  `VirtualizationEngine.cpp`（`MacVirtualizationEngineAdapter`）一起编译链接，
  并真实调用 `macOSVirtualizationEngineAvailable()` →
  `makeMacOSVirtualizationEngine()` → `backendId()` / `tier()` /
  `isHostSupported()` / `probe()` / `supportsGuestArch()` / `stop()` /
  `requirements()`。实测结果：`backendId=macos-virtualization`、
  `tier=native`、`hostSupported=1`、`canStart=1`、`canStop=1`、
  `hostArch=aarch64`、`accelerator=vz`；跨架构被拒绝且 reason 提示 QEMU；
  空载 `stop()` 诚实返回失败。该联调**不启动任何 VM**。

当前本机（Apple Silicon，macOS 27）实测：Release 构建 **746** 项检查全过、0 失败、0 skip；
地址/线程消毒器（ASan / TSan，见 `build-mac-harmony-chaos/asan` 与
`build-mac-harmony-chaos/tsan`）各 0 报告；共享契约联调全过。

故障注入 / 生命周期压力测试（新增于 `build-mac-harmony-chaos` 轮）通过
`setStartGateForTesting()` 闸门与 `setRunnerStubForTesting()` 计数替身实现，**全程不启动任何
真实 VM**：

```bash
# 在 build-mac-harmony-chaos 下
make VirtualWorldMacVirtualizationEngineTests && ./VirtualWorldMacVirtualizationEngineTests
# ASan / TSan 等价物在 build-mac-harmony-chaos/asan、build-mac-harmony-chaos/tsan 同目标
```

测试覆盖：

- 后端 id/name/tier、`backendDescriptor` 函数指针；
- 架构规范化与 Apple Silicon / Intel 支持矩阵、框架无法引导的宿主架构；
- **显式同架构/跨架构矩阵**（含 `arm64`/`amd64` 别名，`i386`/`arm`/`riscv64`/`ppc64` 必须被拒）；
- **沙盒权限诚实性**：无 `com.apple.security.virtualization` 时
  `hostSupported` / `canStart` / `canStop` / `linuxGuest` / `macOSGuestIPSW`
  全部为 false，`validate()` 拒绝，授予权限后恢复；
- **不可用一律附带 fallback 提示**：每条被拒能力 reason+code 非空，Linux 客户机
  被拒时必须点名 QEMU；
- macOS 版本门控（10 / 11 / 12）、非 macOS 宿主；
- 诊断码（框架、沙盒、嵌套虚拟化、跨架构回退、宿主架构不支持）；
- 配置校验（Linux、macOS IPSW、CPU/内存/磁盘/空路径）；
- `describeHostSupport` / `describeGuestArchSupport` 的 reason 出参约定
  （成功清空、失败必填、接受 `nullptr`）；
- `canStart` / `canStop` 的诚实性（macOS 恢复路径不得置真）；
- guest OS 分类（`linux` / `macos` / `darwin` / `restoreFromIPSW` / 未知）；
- 真实宿主探测一致性、`validate` 文件存在性；
- `start`/`startAsync` 的同步/异步错误传播，回调“恰好一次”，且失败不会把引擎
  卡在 busy 状态；
- `stop`/`stopAsync` 的 `not-running` 语义；
- **异步销毁**：用 `setStartGateForTesting()` 把工作线程卡在框架握手之前，在引擎
  销毁后再放行，断言工作线程不再触碰引擎并回报稳定的 `engine-destroyed`
  （ASan 会捕获 use-after-free）；
- **并发生命周期压力**：4 线程 × 15 轮并发 `start`/`startAsync`/`stop`/`stopAsync`/
  `state`/`probe`/`validate`，反复创建销毁引擎，TSan 0 报告且不启动任何 VM；
- **重复 start/stop 幂等性**：同一引擎跑 5 轮"启动→停止→再停止"，断言每轮
  `start` 返回**完全相同**的稳定错误码、`stop` 稳定返回 `not-running`、
  状态不被卡在 busy、连续两次 `stop` 结论一致；每轮使用不同的缺失内核路径，
  避免"结论恰好同源"蒙混过关；
- 引擎反复构造/销毁、启动失败后的析构，以及不可拷贝的静态断言。
- **确定性混沌循环**（`testChaosDeterministicLifecycleLoop`）：固定种子驱动
  `start`/`startAsync`/`stop`/`stopAsync`/`destroy` 共 60 轮交错，断言引擎永不卡在
  busy 状态、永不伪造成功、每个异步完成回调**恰好一次**；闸门保证工作线程在进入
  框架握手前被拦下（`state` 在闸门中止后为 `error` 而非 `not-started`，因为 preflight 之外的
  失败都归为 `Error`，只有 preflight 拒绝才保持 `not-started`）。
- **并发 stop 重入**（`testChaosConcurrentStopReentrancy`）：8 线程 × 5 轮并发 `stop` 同一
  空闲引擎，每个线程都稳定回报 `not-running`，绝不报告一个伪造的成功，也不与 session 析构竞争
  （ASan/TSan 干净）。
- **迟到回调与重入 start**（`testChaosLateCallbackAndReentrantStart`）：4 轮迟到/中止 start，
  断言完成回调恰好一次、回报稳定的 `chaos-gate` 码、且随后的 start 仍能再次到达闸门而非从陈旧的
  `already-running` 应答。
- **失败构建后的引用归属**（`testChaosFailBuildReferenceOwnership`）：10 轮缺失内核路径触发失败构建，
  断言 `stop`/`stop`/`stopAsync` 任意顺序都保持干净、绝不报告未赢得的成功。
- **runnerRaw 所有权不变量**（`testChaosRunnerOwnershipInvariant`，见 §6 计数替身）：确定性交错
  `start→stop` / `start→销毁（不 stop）` / `startAsync→在途销毁` 各 12 轮 + 3 轮 N 线程并发 stop
  风暴，断言 `installed == destroyed` 精确成立（零泄漏、零过度释放）；固定种子，报告轮数、并发度、
  回调计数与资源不变量。

不满足条件时（非 macOS、旧系统、无框架）相关用例输出 `SKIP` 而非失败。

## 11. 已知未验证 / 未实现项

- **未**在任何真实客户机上完成端到端启动（无真实内核/磁盘镜像）；
  `start()` 的框架握手（`VZVirtualMachine` 构建 → `validateWithError:` →
  `startWithCompletionHandler:`）尚未在成功路径上执行过，`stop()` 的
  `stopWithCompletionHandler:` 路径同理。
- `engine-destroyed` 回滚路径现在**已覆盖**，但覆盖手段是
  `setStartGateForTesting()` 测试闸门：闸门保证工作线程在触碰
  `Virtualization.framework` **之前**被拦下，所以断言的是"引擎被销毁时工作线程
  不再解引用它"这一协议，而**不是**"真实启动一个 VM 再销毁"。`timeout` 路径、
  `didStopWithError:` / `guestDidStopVirtualMachine:` 委托回调仍**未覆盖**。
- `setStartGateForTesting()` 是**测试专用接缝**：生产代码从不安装它，未安装时对
  行为零影响，它只决定是否进入框架握手。
- macOS 客户机 IPSW 恢复/安装流程未实现（见 §7）。
- 图形 / 音频设备仅做能力探测，未构建具体设备对象；仅目录共享（macOS 12+）
  构建了 `VZVirtioFileSystemDeviceConfiguration`。
- Qt 适配类与 `makeMacOSVirtualizationEngine()` 工厂由 WorkBuddy 提供
  （`src/VirtualizationEngine.cpp`）；本后端的 `.mm` 已由 `CMakeLists.txt` 通过
  `VIRTUALWORLD_MACOS_BACKEND` 自动纳入构建。
- Intel Mac（x86_64 宿主）路径为纯逻辑验证（合成宿主），**无实机验证**；
  共享支持矩阵中 macOS 行同样标记 `verified = false`。按 Apple 官方文档，
  `VZLinuxBootLoader` 在 Apple Silicon **与 Intel Mac 上都可用于同架构
  （x86_64）Linux 客户机**，所以 `nativeX86_64Guest = true` 不是一个虚假声明；
  但社区报告 Intel + macOS 13.5 以前对 Linux 内核 6.2（Ubuntu 23.04 / Fedora 38
  等）不可引导（lima 已知问题）。本后端只做 macOS ≥ 11 的门槛判定，**不区分
  这一子情况**，该组合未经验证。
- 超时路径按“病态挂起”处理，不做激进清理（避免与在途回调竞争）。
- `Session::runnerRaw` 的所有权不变量（§6）**已**由 `setRunnerStubForTesting()`
  计数替身的确定性混沌测试覆盖（Release / ASan / TSan 三套构建均 0 failure）。该覆盖是
  **协议级**的：证明“每次安装恰好释放一次”这一契约成立，但**不**覆盖真实框架握手
  （`VZVirtualMachine` 构建与 `startWithCompletionHandler:`）—— 真实引导路径的首次执行仍
  只在实机上发生。ASan/TSan 对全套件的干净通过 + 静态审查，是该修复的其余依据。
