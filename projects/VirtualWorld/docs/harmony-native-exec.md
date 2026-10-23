# 同架构近原生执行引擎设计（HarmonyOS / OpenHarmony ARM64）

> **文档性质与诚实声明（请先读）**
>
> 本文档是一份**设计规格 + 离线探针证据**，不是已实现的虚拟机，也不是已实现的运行时。
> 它描述了一种在**无硬件虚拟化能力**的 HarmonyOS / OpenHarmony ARM64 设备上，运行
> **ARM64 Linux 用户态 / 容器型客体**的“同架构近原生执行引擎”。本任务**未启动任何
> VM / UTM / guest，未构建 QEMU**。关于运行时限制的验证状态（见 §11）：
> - **已完成**：在运行的 HarmonyPC 2in1 **模拟器（API 24 / OpenHarmony 6.1.1.125 /
>   aarch64 / 8GB）的 shell 域**实测了能力探针，**确认 `PROT_EXEC` 封禁成立**（mmap RX/RWX、
>   RW→RX mprotect、文件映射 RX、执行固定 blob 全部 DENIED，errno=13）。
> - **未完成（明确阻塞）**：第三方**应用沙箱（HAP）**实测因本机 DevEco 安装**缺失
>   `hapsigner` 签名工具**、且 DevEco 自动签名需要 AGC / 华为账号后端（无头环境不可得），
>   无法对 HAP 签名并 `hdc install`，故**未能取得应用沙箱运行时数据**。
> - **重要**：shell 域实测**不得替代** HAP 应用沙箱实测；且 **API 24 模拟器可能与真实
>   HarmonyPC 设备存在差异**。所有未经验证的运行时结论仍标注“未验证运行时”。
>
> 本文档**不恢复**任何既有门禁标记（如 `.qemu-inprocess-ready` / `.qemu-inprocess-blocked`），
> 也**不修改** `qemu-source/**`、`src/**`、`tests/**`、CMake、`ci/ohos/**`、
> `third_party/ohos-ports/**`、`.deepseek/**`。

---

## 0. 产品措辞（必须精确的“容器型客体”定义）

| 术语 | 正确用法 | 错误用法（禁止） |
|------|----------|------------------|
| 本引擎 | “同架构近原生用户态执行引擎” / “ARM64 Linux 容器型客体运行环境” | “虚拟机”、“虚拟化”、“硬件加速”、“Native 执行层” |
| 客体 | “ARM64 Linux 用户态 / 容器型客体” | “客户机操作系统”、“guest OS”、“内核” |
| 内核 | “由用户态微内核 / 系统调用兼容层替代”——没有真正的 guest 内核 | “我们模拟了 Linux 内核”、“guest kernel” |
| 加速 | “普通 AArch64 基本块在宿主 ARM64 原生执行”（近原生，非 100% 原生） | “原生虚拟化”、“bare-metal 性能” |

**一句话定位**：在不具备硬件虚拟化的 OpenHarmony 设备上，把 **ARM64 Linux 用户态
程序（容器型客体）** 直接放进宿主进程，**绝大部分指令在宿主 ARM64 上原生执行**，仅把
少量“不能安全直通”的指令（系统调用 `SVC`、系统寄存器访问、特权指令、时钟/随机数、
自修改代码）重定向到受控 trampoline 与 Linux→OHOS 系统调用兼容层。**它不冒充完整硬件
虚拟机，guest 内核由用户态兼容层替代。**

---

## 1. 为什么需要它，以及它要解决的根本矛盾

现有 `HarmonyVirtualizationEngine`（`docs/harmonyos-virtualization.md`）是一个
**fail-closed、同 PID 进程内** 的 QEMU 后端，其跨 ISA / 同 ISA 回退都依赖 **QEMU TCG**。
TCG 有两种执行模式：

- **JIT 模式**：把翻译后的机器码写入 **`PROT_EXEC` 可执行内存**（代码缓存）。
- **TCI 模式**（TCG Interpreter）：把翻译结果序列化为**普通 RW 内存**中的字节码，由 C
  解释器循环执行，**完全不需要 `PROT_EXEC`**。

关键约束来自对 HarmonyOS 7.0 的调研（见 §2）：**第三方应用无法获取任何含 `PROT_EXEC`
的内存**。这意味着：

1. TCG **JIT 模式在 HarmonyOS 7.0 上无法初始化**（代码缓存 mmap 失败 → 已有 Unicorn/
   QEMU 项目在 Mate 70 / OHOS 7.0 上实测崩溃）。
2. 任何“重写 guest 代码并生成新可执行页”的二进制翻译 / 静态重写方案，同样被封锁
   （除非应用持有官方受限 ACL 权限 `ohos.permission.kernel.ALLOW_EXECUTABLE_FORT_MEMORY`）。
3. **近原生执行**（让普通 AArch64 指令直接跑在宿主 ARM64 上）本身**不要求运行时分配
   `PROT_EXEC`** —— 因为 guest 代码来自其**被加载器映射为可执行**的原始映像，而不是由
   引擎在运行时新分配的。矛盾的真正焦点是：**如何在“不分配新可执行页”的前提下，拦截
   guest 的敏感指令（尤其是 `SVC` 系统调用）**。

这正是 §5 选择 **方案 B（轻量 DBT）并分两档落地** 的根本原因。

---

## 2. HarmonyOS / OpenHarmony 运行时限制核验（SDK 头文件 + 链接级 + 社区实证）

> 核验级别说明：**SDK 头文件与交叉链接为本地可证明**；**设备运行时行为依据社区实证
> （vmrp-ohos 在 Mate 70 / OHOS 7.0.100 的实测）与华为官方变更说明**，本机无设备，**未
> 在真实设备上复现**。

### 2.1 可执行内存（决定性约束）

- **头文件可证明**：SDK sysroot `usr/include/sys/mman.h` 中 `PROT_EXEC = 4` 常量存在
  （链接级 API 面正常），但这只是声明。
- **运行时封禁（社区实证 + 官方变更）**：HarmonyOS **API 18（5.1）起** 实施“匿名内存执行
  权限管控”策略变更（changelog `b031`）。第三方应用：
  - `mmap(PROT_READ|PROT_WRITE|PROT_EXEC)` → **EINVAL(22)**；
  - `mmap(RW)` + `mprotect(..., PROT_EXEC)`（含 W^X 两步走）→ **EINVAL(22)**；
  - 纯 `mmap(PROT_EXEC)` → **EINVAL(22)**；
  - `memfd_create()` + `mprotect(PROT_EXEC)` → **EACCES(13)**；
  - 直接 `syscall(SYS_mmap=222, PROT_RWX)` / `syscall(SYS_mprotect=226, EXEC)` 绕过
    libc 仍失败；Debug / Release 签名均被拒。
  - **系统进程 / 框架（如 ArkCompiler JIT）不受影响**——限制仅针对第三方应用原生代码。
- **官方授权通道**：受限 ACL 权限 `ohos.permission.kernel.ALLOW_EXECUTABLE_FORT_MEMORY`
  （`kernel.` 前缀 = 内核级可执行内存门控），需经 AGC 提交场景说明、审核、重签 Profile。
  未授权却仅声明该权限会导致**安装失败**；“安全盾模式”下全局禁用 JIT（连授权应用也失效）。
- **已验证的保底**：vmrp-ohos 在等待 ACL 期间移植 **TCI（TCG Interpreter）**，使应用在
  **不触碰 `PROT_EXEC`** 的情况下恢复可用（比 JIT 慢 3–5×，对低速客体可接受）。

### 2.2 其它由 `docs/ohos-qemu-dependencies.md` 与 NDK libc 权限指引交叉确认的限制

| 类别 | 机制 | 受影响 musl 接口（节选） | 对本引擎的含义 |
|------|------|--------------------------|----------------|
| 可执行内存 | 内核策略 `b031` | `mmap`/`mprotect` 的 `PROT_EXEC` | **不能用 JIT / 生成新 exec 页**（见 §1） |
| Seccomp | `SIGSYS`（栈顶在 `ld-musl`） | `mount`/`umount`/`unshare`/`setns`/`chroot`/`init_module`/`pivot_root`/`reboot` | guest 容器**不能用真实命名空间/挂载**；容器模型必须在应用沙箱内用路径重定向模拟 |
| SELinux | `EACCES` | `ioctl`/`shm_open`/`shm_unlink`/`popen`/`forkpty`/`openpty`/`ptsname` | guest 用到的 `ioctl`/`shm`/`popen` 需 shim 或返回明确 errno |
| 沙箱 | `ENOENT` | `open`/`openat`/`fopen`/`rename`/`tmpfile` 越出应用沙箱目录 | guest 文件路径必须重定向进**应用沙箱子树**；天然契合“容器根 = 沙箱子树” |
| Capabilities | `EPERM` | `mount` 需 `CAP_SYS_ADMIN` 等 | 第三方应用无这些 capability，相关能力不可得 |
| 桩/默认失败 | — | `brk`（空实现/默认失败）、`getutent` 等 | guest 若用 `brk`/`sbrk` 做堆 → 必须映射到 `mmap` 堆 |
| 动态链接签名 | hmmac 安全模块 | 任意 `.so` 的 `dlopen` 受路径安全标签约束 | 把任意 guest ELF 当 `.so` 加载可能受签名/标签限制（见 §5 方案 B 的落地两档） |

> 注意：`signal.h` 在 SDK 中存在（标准 POSIX 信号接口受 musl 支持），但 Seccomp 对
> 被禁系统调用会发 `SIGSYS`——本引擎通过**拦截 `SVC` 而非让 guest 直接发系统调用**来规避
> 这一风险（§6）。

---

## 3. 架构图

```
                         ┌──────────────────────────────────────────────┐
                         │            OpenHarmony 宿主进程 (aarch64)       │
                         │                                                │
   ARM64 Linux 容器型客体  │   ┌────────────────────────────────────────┐   │
   (用户态 ELF，无 guest   │   │        近原生执行引擎 (本设计)            │   │
   内核，内核=兼容层)      │   │                                          │   │
        │                 │   │  ┌──────────────┐    ┌───────────────┐  │   │
        │ 加载             │   │  │ 加载/映射     │    │ 静态/增量扫描 │  │   │
        ▼                 │   │  │ (原始映像,    │───>│ SVC / sysreg /│  │   │
   ┌─────────────┐        │   │  │  已是 exec)   │    │ 特权/分支目标 │  │   │
   │ guest .text │        │   │  └──────┬───────┘    └──────┬────────┘  │   │
   │ (普通算术=  │        │   │         │ 普通基本块          │ 敏感指令  │   │
   │  73% 原生)  │────────┼──>│         ▼                    ▼          │   │
   └─────────────┘        │   │  ┌──────────────┐   ┌────────────────┐  │   │
                         │   │  │ 原生执行      │   │ 受控 trampoline│  │   │
                         │   │  │ (宿主 ARM64) │   │ → Linux→OHOS   │  │   │
                         │   │  └──────────────┘   │   系统调用兼容层│  │   │
                         │   │                      └───────┬───────┘  │   │
                         │   │  SVC 拦截点 ───────────────────┘          │   │
                         │   │      │                                   │   │
                         │   │      ▼                                   │   │
                         │   │  ┌──────────────────────────────────┐    │   │
                         │   │  │ Linux→OHOS 系统调用映射层         │    │   │
                         │   │  │ file/net/thread/signal/mmap/futex │    │   │
                         │   │  └───────────────┬──────────────────┘    │   │
                         │   └──────────────────┼───────────────────────┘   │
                         │                      ▼                         │
                         │   ┌────────────────────────────────────────┐    │
                         │   │ 回退：进程内 TCG（TCI 模式，无 PROT_EXEC）│    │
                         │   │ 跨 ISA / 内核态 / 实模式 / 自修改代码    │    │
                         │   │  —— 复用 QemuInProcessRuntime ——         │    │
                         │   └────────────────────────────────────────┘    │
                         └──────────────────────────────────────────────┘

   隔离边界：
     • 地址空间隔离：guest 映射在宿主进程内的独立区域（无独立页表）
     • 信号隔离：引擎注册的信号 handler 不泄漏给 guest；guest 信号经兼容层转译
     • 页权限：guest 代码从“加载器已映射为可执行”的原始映像执行（不新分配 PROT_EXEC）
     • 代码缓存切换：原生块与 TCG(TCI) 块之间通过受控入口/出口切换（见 §7）
```

**核心不变量**：普通 AArch64 基本块在宿主 ARM64 **原生执行**；敏感指令（尤其是 `SVC`）
**必须**在落到内核之前被引擎截获并转译，绝不“无地址空间/信号隔离设计地直接执行未知客体代码”。

---

## 4. 三种实现方案比较

| 维度 | A. AArch64 静态二进制重写 | B. 轻量 DBT（仅翻译敏感指令，大多 1:1 发射） | C. 精简 QEMU linux-user |
|------|--------------------------|----------------------------------------------|--------------------------|
| 原理 | 离线把整份二进制重写，替换所有敏感指令为 trampoline，再整体加载执行 | 加载时/增量地把敏感指令替换为跳板，普通指令原样执行 | 同 ISA 下仍由 TCG 全量翻译（linux-user 不提供“原生直通”） |
| 原生速度 | 最高（整份原生） | **高**（普通块原生，仅敏感指令转发） | 低（仍 TCG；同 ISA 也翻译） |
| OHOS 7.0 可行性 | **阻塞**（需生成新 exec 页 → 需 ACL 权限） | **分档**：有 ACL 权限=完全可行；无权限=退化为“原生映像+故障驱动拦截” | TCG 必须 **TCI 模式**（否则 JIT 初始化失败） |
| 自修改代码 | 难（需重扫） | 中（按页增量重写） | 易（TCG 天然重译） |
| 间接/未知目标 | 难（需全静态解析） | 易（运行时解析+页级插桩） | 易 |
| 安全隔离 | 弱（整份客体重写为原生，隔离靠设计） | 中（敏感点集中可审计） | 强（TCG 全隔离） |
| 工程复杂度 | 高 | 中 | 低（复用 QEMU） |

### MVP 选择：**方案 B（轻量 DBT）**，并定义两档落地

- **档 1（无 ACL 权限，今天即可做，受限）——“原生映像 + 故障驱动敏感指令拦截 + TCI 兜底”**：
  - guest 代码**从加载器已映射为可执行的原始映像**直接原生执行（不新分配 PROT_EXEC，合规）；
  - 敏感指令拦截**不依赖代码页重写**，改用：(a) 加载时静态扫描得到敏感指令位置表；
    (b) 对“必须 trap 的点”使用**页权限故障**或**受控执行模型**触发 C 处理函数，
    在函数中模拟 `SVC`/系统寄存器/特权指令后恢复；普通块保持原生；
  - 任何无法安全原生运行的部分（跨 ISA、内核态、自修改）交给 **进程内 TCG 的 TCI 模式**。
  - 代价：敏感点密集时退化为接近解释器；但“普通计算型客体”仍可获近原生速度。
- **档 2（持有 `ALLOW_EXECUTABLE_FORT_MEMORY` ACL 权限，推荐目标）——“经典 DBT”**：
  - 复制并增量重写 guest 代码到**新 exec 页**（trampoline 内联），普通块 1:1 原生，敏感指令
    内联跳板 → 近原生速度 + 透明隔离；
  - 这是 §3 架构图的完整形态。

> 选择 B 而非 A/C 的理由：A 在无 ACL 权限时被硬性封锁、且对自修改/间接目标脆弱；C 在
> 同 ISA 下并不提供“原生直通”（仍全量 TCG），违背“避免把全部 ARM64 指令交给 TCG”的初衷。
> B 在“有 ACL 权限”时给出最佳性价比，在“无权限”时仍有可运行的降级形态，且天然衔接
> 现有进程内 TCG(TCI) 兜底。

---

## 5. MVP 分阶段计划（档 2 为目标，档 1 为可立即开工的受限版）

| 阶段 | 目标 | 关键产出 | 门禁/依赖 |
|------|------|----------|-----------|
| P0 探针与事实澄清 | 离线分析 + 约束固化 | 本文档 + `experiments/ohos-native-exec/probe` + `build-ohos-native-exec` | 已完成（见 §8） |
| P1 加载与地址空间 | 把 guest ELF 映射进宿主进程、建立区域表与 FD/信号隔离 | 加载器 + 区域表（**不新分配 PROT_EXEC**） | 档1 即可 |
| P2 静态敏感指令扫描 | 解析 guest 指令流，建立 `SVC`/sysreg/特权/分支目标位置表 | 复用 §8 探针的解码能力 | 档1 即可 |
| P3 Linux→OHOS 系统调用兼容层 | file/net/thread/signal/mmap/futex 映射表 | 见 §6 表格 | 档1 即可 |
| P4 trampoline / 故障驱动拦截 | 敏感点 C 处理（无 PROT_EXEC 路径） | 档1 拦截器 | 档1 即可 |
| P5 进程内 TCG(TCI) 兜底 | 接 `QemuInProcessRuntime`，TCI 模式跑跨 ISA/内核态/自修改 | 引擎↔TCG 切换边界（§7） | 复用既有 in-process 成果 |
| P6 ACL 权限路径 | 申请 `ALLOW_EXECUTABLE_FORT_MEMORY`，切到档2 经典 DBT | 新 exec 页 + 内联跳板 | 需华为审核（外部依赖） |
| P7 设备运行时验证 | 在真实 OHOS 设备上验证 RSS / 信号 / 信号隔离 | 运行时报告（当前**未做**） | 需设备 |

---

## 6. Linux→OHOS 系统调用兼容策略（含无法安全映射的 errno）

| 子系统 | 策略 | 无法安全映射时 |
|--------|------|----------------|
| `open/openat/fopen` | 路径重定向进**应用沙箱子树**（容器根）；沙箱外路径返回 `ENOENT` | `EROFS` / `ENOENT` |
| `read/write/close/dup` | FD 重映射表直通 | — |
| 网络 `socket/connect/...` | OHOS 支持 BSD socket，按 `AF_*` 直通；非常用 domain 返回 `EAFNOSUPPORT` | `EPROTONOSUPPORT` |
| 线程 `clone/futex` | `pthread`/`futex` 映射到 OHOS；`CLONE_NEW*`/`unshare`/`setns` 因 Seccomp 不可用 → 返回 `EPERM` | `EPERM`（容器不支持真实命名空间） |
| 信号 `rt_sigaction/...` | POSIX 信号 musl 支持；signum 转译；`SA_RESTORER` 由宿主处理 | `EINVAL`（非法 signum） |
| `mmap/mprotect` | `PROT_READ|WRITE` 直通；**`PROT_EXEC` 在无 ACL 权限时返回 `EINVAL`**（guest 不应依赖运行时生成 exec 页）；`brk`/`sbrk` 映射到 `mmap` 堆 | `EINVAL` / `ENOMEM` |
| `clock_gettime` / 计时 | `SVC` 层转译到 OHOS 时钟；`CNTFRQ_EL0`/`CNTPCT_EL0`（EL0 系统寄存器）由 trampoline 模拟 | `ENOSYS` |
| `getrandom` / 随机数 | 映射到 OHOS `getrandom` 或 `/dev/urandom` 读取 | `ENOSYS` |
| `ioctl` | SELinux 可能 `EACCES`；白名单 + 转译，未识别返回 `ENOTTY` | `EACCES` / `ENOTTY` |
| `execve` | 容器模型首版**不支持**进程替换 → 返回 `ENOSYS` | `ENOSYS` |
| `mount/umount/chroot` | Seccomp 封锁 → 返回 `EPERM` | `EPERM` |

> 关键不变量：**guest 永远不直接发原始系统调用**。所有 `SVC` 在落到内核前被引擎截获，
> 经上表转译——因此 Seccomp 的 `SIGSYS` 风险被前置消除。

---

## 7. 与 VirtualWorld / QemuInProcessRuntime 的接口边界（未来集成）

- **职责切分**：近原生执行引擎负责**同 ISA 用户态客体**的近原生执行；`QemuInProcessRuntime`
  负责**跨 ISA / 内核态 / 实模式 / 自修改代码**的 **进程内 TCG(TCI)** 兜底。
- **切换边界**（状态 / 页权限 / 代码缓存）：
  - **状态**：guest 通用寄存器在原生块与 TCG 块间通过**显式上下文封送**传递；进入 TCG 前
    保存原生执行上下文，回到原生前恢复。
  - **页权限**：原生路径下 guest 代码来自已映射映像；TCG(TCI) 路径下代码缓存为**普通 RW
    内存**（无 PROT_EXEC），两套页权限模型互不干扰。
  - **代码缓存**：原生块使用宿主进程的原生代码段；TCG 块使用 TCI 字节码缓冲；两者通过
    **受控入口/出口**（trampoline 与 TCI dispatch）切换，绝不直接跳入对方代码区。
- **门禁继承**：沿用 `HarmonyVirtualizationEngine` 的 fail-closed 与“**tier 永不为 Native**”
  约定——本引擎即便以档2 运行，对外仍**不宣称 Native / 硬件虚拟化**，仅报告
  `SoftwareEmulation`（近原生降级）或 `degraded`，与现有状态机一致。
- **双宏门禁**：若未来把真实核心接入，沿用 `HARMONY_INPROCESS_REAL_CORE` /
  `DEEPSEEK_HOST_SAFETY_GATE_PASSED` 双宏硬门禁，默认 OFF（fail-closed）。
- **无子进程**：继续遵守禁用原语扫描（fork/exec/system/popen/... 永不用于启动客体）。

---

## 8. 离线重写探针结果与复现

### 8.1 探针做了什么（及没做什么）

- **做**：解析 AArch64 ELF，解码指令流，**识别并报告** `SVC`、系统寄存器访问（`MRS`/`MSR`）、
  特权指令（`HVC`/`SMC`、DC/IC/AT/TLBI）、以及分支/分支目标。**不执行、不翻译、不分配
  可执行内存**。内存占用仅与文件大小相关（mmap 只读私有映射）。
- **没做**：不生成任何 guest 代码、不启动 guest、不做运行时验证。
- **合规**：探针与样例客体都落在 `experiments/ohos-native-exec/**` 与
  `build-ohos-native-exec/**`，**未触碰** `qemu-source/**`、`src/**`、`tests/**`、CMake、
  `ci/ohos/**`、`third_party/ohos-ports/**`、`.deepseek/**`。

### 8.2 样例客体与实测输出

样例客体 `experiments/ohos-native-exec/sample-guest/guest.c` 是一个 freestanding 的
AArch64 片段，特意包含：`mrs tpidr_el0`、`mrs cntfrq_el0`、一个带分支的循环、`svc #0`。
它用既有 OHOS 交叉工具链（`ci/ohos/env-ohos.sh`）编译为 **`aarch64-linux-ohos` ELF 对象**
（链接级证明 + 真实解码输入）。

探针（宿主 Apple Silicon 原生 clang 构建）对 `guest.ohos.o` 的输出：

```
# aarch64_rewrite_probe
file            : build-ohos-native-exec/guest.ohos.o
EI_CLASS        : 2 (1=32,2=64)
e_machine       : 183 (183=AArch64)  is_aarch64=YES
e_type          : 1 (1=REL,2=EXEC,3=DYN)
code_ranges     : 1
  range[0] off=0x40 vaddr=0x40 size=0xc0

# instruction summary (total decoded: 48)
SVC (syscall)   : 1
HVC / SMC       : 0 / 0  (privileged; must never pass through)
MRS (sysreg rd) : 2
MSR (sysreg wr) : 0
sys-inst(DC/IC..): 0  (privileged)
B / BL          : 7 / 0
B.cond          : 0
CBZ/CBNZ        : 0
TBZ/TBNZ        : 2
indirect BR/BLR/RET: 1
other/arith     : 35

# sensitive-instruction detail (first 64 each)
SVC offsets     : 0xf0
MRS/MSR (off: reg): 0x4c:TPIDR_EL0 0x54:CNTFRQ_EL0
direct-branch (off -> target): 0x70->0x7c 0x80->0xe0 0x84->0x90 0x94->0xb8 0x98->0xa4 0xac->0xcc 0xc0->0xcc 0xc4->0xd0 0xd4->0x7c

# resource: max RSS observed = 1680 KiB (cap 1536 MiB = 1572864 KiB)
```

**读数**：
- **73%（35/48）是指令流中的普通算术/访存**，可在宿主 ARM64 上**原生执行**——印证“避免
  把全部指令交给 TCG”的性价比。
- 必须被引擎处理的敏感/控制类指令：**1×SVC**（`svc #0`，Linux 系统调用入口）、**2×MRS**
  （`TPIDR_EL0` 线程局部基址、`CNTFRQ_EL0` 计时频率——二者都是 EL0 可见、需 shim 的系统
  寄存器）、**10× 分支/分支目标**（trampoline 与代码缓存边界的落点）。
- 探针自身最大 RSS ≈ **1.6 MiB**，远低于 1.5 GiB 上限（满足“测试必须低内存”）。

### 8.3 链接级证明（aarch64-linux-ohos）

- `guest.ohos.o`：`aarch64-unknown-linux-ohos-clang` 交叉编译产物，`llvm-readelf -h` 确认
  `Machine: AArch64`、`Class: ELF64`。
- `build-ohos-native-exec/aarch64_rewrite_probe.ohos`：探针源码经同一 OHOS 工具链
  **交叉链接成功**（链接级证明本引擎工具链可产出 aarch64-OHOS 二进制；未在该设备运行）。

### 8.4 复现命令

```bash
cd <repo-root>
# 1) 构建宿主侧探针并运行（产出真实解码结果）
cc -O2 experiments/ohos-native-exec/probe/aarch64_rewrite_probe.c \
   -o build-ohos-native-exec/aarch64_rewrite_probe

# 2) 交叉编译样例客体 + 交叉链接探针（aarch64-linux-ohos，链接级证明）
. ci/ohos/env-ohos.sh
"$CC" -O0 -nostdlib -c experiments/ohos-native-exec/sample-guest/guest.c \
   -o build-ohos-native-exec/guest.ohos.o
"$CC" -O2 experiments/ohos-native-exec/probe/aarch64_rewrite_probe.c \
   -o build-ohos-native-exec/aarch64_rewrite_probe.ohos

# 3) 运行探针
./build-ohos-native-exec/aarch64_rewrite_probe build-ohos-native-exec/guest.ohos.o
```
（也可一键执行 `sh build-ohos-native-exec/build.sh`，并发编译受控在 2 以内。）

---

## 9. 风险清单

| # | 风险 | 严重度 | 缓解 |
|---|------|--------|------|
| R1 | HarmonyOS 7.0 `PROT_EXEC` 封禁 → 档2 经典 DBT 在无 ACL 权限时不可行 | 高 | 档1 降级形态（原生映像 + 故障驱动拦截 + TCI）；申请 `ALLOW_EXECUTABLE_FORT_MEMORY` |
| R2 | 无 ACL 权限时“拦截敏感指令而不重写代码页”实现复杂、敏感点密集时退化为解释器 | 中 | 仅对已知敏感点做页级 trap；统计敏感密度决定是否整体交 TCI |
| R3 | guest 任意 ELF 当宿主模块加载受 hmmac/签名标签限制（`dlopen`） | 中 | 用 loader 直接映射 + 静态扫描，而非依赖 `dlopen`；路径签名合规 |
| R4 | EL0 系统寄存器（如 `TPIDR_EL0`）语义差异（TLS 基址在 OHOS 由 musl 管理） | 中 | trampoline 模拟，返回引擎侧 TLS 视图 |
| R5 | 自修改代码 / JIT 型 guest（自身需要 `PROT_EXEC`） | 高 | 交 TCI；档1 下直接拒绝并返回 `EINVAL`/`ENOSYS` |
| R6 | 信号隔离：guest 信号与宿主/引擎 handler 冲突 | 中 | 兼容层转译 signum，handler 不泄漏 |
| R7 | Seccomp/SELinux 对 `mount`/`ioctl`/`shm` 的封锁使部分 Linux 能力不可得 | 中 | 上表明确返回 errno，不伪装成功 |
| R8 | 跨 ISA 客体（x86_64/riscv64）必须走 TCG(TCI)，非本引擎近原生范围 | 低 | 明确边界，交 `QemuInProcessRuntime` |
| R9 | 设备运行时未验证（本机无 OHOS 设备） | 高（证据层面） | 所有运行时结论标注“未验证”；仅 SDK 头文件/链接级为本地证明。**更新**：已在 API 24 模拟器 shell 域完成能力探针实测（§11），但应用沙箱（HAP）实测因 `hapsigner` 缺失而阻塞 |

---

## 10. 结论

- 本设计在**无硬件虚拟化**的 OpenHarmony ARM64 上，以“**近原生 + 敏感指令集中拦截 + TCG(TCI)
  兜底**”的方式运行 ARM64 Linux 用户态 / 容器型客体；**不冒充虚拟机、不模拟内核硬件**。
- HarmonyOS 7.0 的 `PROT_EXEC` 封禁是决定性的设计约束：它**封锁了 JIT 与“生成新 exec 页”
  类方案**，使 MVP 必须分两档——无 ACL 权限时降级为“原生映像 + 故障驱动拦截 + TCI”，持
  有 `ALLOW_EXECUTABLE_FORT_MEMORY` 后升级为经典 DBT。
- 离线探针已证明：普通 AArch64 指令流中 **~73% 可原生执行**，仅需集中处理少量 `SVC`/
  系统寄存器/分支；探针与样例均在 `aarch64-linux-ohos` 上完成链接级证明，且 RSS 仅 ~1.6 MiB。
- **未做且未声明**：未启动任何 guest/VM、未构建 QEMU；未恢复任何既有门禁标记；未修改受
  保护目录。运行时验证状态见 §11：**shell 域能力探针已在 API 24 模拟器实测完成**；
  **HAP 应用沙箱实测因签名工具 `hapsigner` 缺失而未能完成**（shell 结果不得替代 HAP）。
  本文档是设计规格 + 离线证据 + 部分运行时实测，不是“已实现完整虚拟机”。

---

## 11. 真机/模拟器实测（HarmonyPC 2in1 模拟器 · API 24）

> **本节是设计任务之后追加的“未验证运行时升级为可复现实测”工作。**
> 实测环境为**真实运行中的** HarmonyPC 2in1 模拟器，通过 HDC（`127.0.0.1:5555`）连接。
> 严格遵循：仅写入 `experiments/ohos-native-exec/**`、`build-ohos-native-exec/**`、
> 本文档；未触碰 `qemu-source/**`、`src/**`、`tests/**`、CMake、`ci/ohos/**`、受保护目录；
> 未启动第二台模拟器、未构建 QEMU；仅执行**固定可审计** blob（`MOV W0,#42; RET`），
> 不执行任何未知 guest 代码；单进程 RSS 远低于 1.5 GiB 上限。

### 11.1 设备事实（实测）

| 项 | 值 | 来源 |
|----|----|------|
| 产品型号 | `emulator`（HarmonyPC 2in1 模拟器） | `param get const.product.model` |
| 系统版本 | OpenHarmony **6.1.1.125** | `param get const.ohos.fullname` |
| API 版本 | **24** | `param get const.ohos.apiversion` |
| 架构 | `aarch64` | `uname -m` |
| 内存 | MemTotal ≈ **8.1 GiB**（8121296 kB） | `/proc/meminfo` |
| 探针运行身份 | shell，uid=2000，context `u:r:sh:s0` | `id` |
| musl 加载器 | `/system/lib/ld-musl-aarch64.so.1` | SDK/链接级 + 实测 |
| HDC 版本 | 3.2.0d | `hdc version` |

> ⚠️ **差异声明**：本实测在 **API 24 / OpenHarmony 6.1.1.125** 模拟器上进行。真实
> HarmonyPC 设备（尤其更高 API / HarmonyOS 7.0 的 `PROT_EXEC` 内核级封禁变更 b031）可能与
> 本模拟器表现不同。本节结论**仅代表该模拟器**，不自动外推到真实设备。

### 11.2 实测一：Shell 域能力探针（已完成 ✅）

**目标**：在 `/data/local/tmp` 通过 `hdc shell` 运行能力探针，逐项记录 errno。
**方法**：`cap_shell_probe`（静态链接 aarch64-linux-ohos 可执行）调用与 HAP **完全相同**的
`run_cap_tests()`；其唯一执行的机器码为固定 blob `MOV W0,#42; RET`（见 `cap_tests.c`
`EXEC_BLOB`）。

**构建**（并发编译 ≤2，受 `ci/ohos/env-ohos.sh` 约束）：
```sh
sh build-ohos-native-exec/build-cap-tests.sh
# 产物: build-ohos-native-exec/cap_shell_probe (静态 aarch64-linux-ohos)
#       build-ohos-native-exec/libcaptest.so   (dlopen 测试用兄弟 .so)
```

**部署与运行**：
```sh
HDC=/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc
$OCD/bin/hdc file send build-ohos-native-exec/cap_shell_probe /data/local/tmp/cap_shell_probe
$OCD/bin/hdc file send build-ohos-native-exec/libcaptest.so   /data/local/tmp/libcaptest.so
$OCD/bin/hdc shell "chmod 755 /data/local/tmp/cap_shell_probe"
$OCD/bin/hdc shell "CAP_SO_PATH=/data/local/tmp/libcaptest.so /data/local/tmp/cap_shell_probe"
```

**实测结果（逐项正确/errno）**：

| # | 检查项 | 结果 | errno | 含义 |
|---|--------|------|-------|------|
| 1 | `mmap_rw`（PROT_READ\|WRITE） | **PASS** | — | 普通 RW 匿名映射可用 |
| 2 | `mmap_rx`（PROT_READ\|EXEC） | **DENIED** | 13 `EACCES` | 直接分配可执行页被拒 |
| 3 | `mmap_rwx`（RWX） | **DENIED** | 13 `EACCES` | RWX 直接分配被拒 |
| 4 | `rw_to_rx_mprotect`（RW→mprotect RX） | **DENIED** | 13 `EACCES` | 即使是先 RW 再改 RX 也被拒 |
| 5 | `file_map_rx`（文件映射 RX） | **DENIED** | 13 `EACCES` | 文件背景可执行映射也被拒 |
| 6 | `exec_fixed_blob`（执行 `MOV W0,#42;RET`） | **DENIED** | 13 `EACCES` | 因无 RX 内存，固定 blob 无法执行 |
| 7 | `sigill_handler`（SIGILL 捕获） | **PASS** | — | 信号 handler 可安装/触发 |
| 8 | `sigsegv_handler`（SIGSEGV 捕获） | **PASS** | — | 信号 handler 可安装/触发 |
| 9 | `pthread_tls`（pthread + `__thread`） | **PASS** | — | 线程局部存储可用 |
| 10 | `futex`（FUTEX_WAIT，期望 EAGAIN） | **DENIED** | 22 `EINVAL` | 该调用返回 EINVAL（参数/内核语义差异） |
| 11 | `dlopen_so`（`CAP_SO_PATH` 指向 `libcaptest.so`） | **DENIED** | 2 `ENOENT` | shell 域 linker 命名空间限制，无法从 `/data/local/tmp` dlopen |
| — | 峰值 RSS | **1928 KiB** | — | 远低于 1.5 GiB 上限（与 §10 估计一致） |

> **核心结论（shell 域）**：`PROT_EXEC` 封禁在 shell 域**实测成立**——无论直接 mmap RX、
> 直接 mmap RWX、还是 RW→RX 的 mprotect，均被 `EACCES` 拒绝；因此连“执行一段固定可审计的
> AArch64 指令”都无法做到。这与设计文档 §2 的论断一致，并给出了**真实 errno 证据**。
> 注意：shell 域（uid 2000, `u:r:sh:s0`）**不等于**第三方应用沙箱；`dlopen` 在 shell 域被
> ENOENT 主要源于 shell 的 linker 命名空间限制，并不等于应用沙箱内也会失败。

> **连接状态说明（诚实记录）**：上述结果来自本次会话中**设备在线时**的成功运行（HDC 目标
> `127.0.0.1:5555`，当时 `hdc list targets` 可见、探针成功执行并落盘）。在后续一次复跑尝试中，
> 模拟器连接已断开（`hdc list targets` 为空、`127.0.0.1:5555` 连接被拒、主机侧无 emulator 进程），
> 故**复跑/可重复性二次确认未能完成**。结果文件 `build-ohos-native-exec/shell-cap-results.txt`
> 已用当次真实输出回填。若需二次确认，请将模拟器重新连回 `127.0.0.1:5555` 后重跑 §11.2 命令。
> 注：本约束禁止启动第二台模拟器，故未尝试重启/新建模拟器。

### 11.3 实测二：HarmonyPC HAP（第三方应用沙箱）—— 实现完成，运行时实测被签名阻塞 ⛔

**目标**：创建最小 HarmonyPC HAP（Native C/C++），在**正常第三方应用沙箱**中运行与 shell
探针**完全相同**的 `run_cap_tests()`，结果写入**应用日志（hilog）**与**私有文件**，再用
`hdc` 取回。明确要求：**shell 结果不得替代 HAP 结果**。

**实现内容（真实代码，非设计稿）**，位于 `experiments/ohos-native-exec/hap/`：
- `AppScope/app.json5`、`build-profile.json5`、`oh-package.json5`、`hvigorfile.ts`、`hvigor/hvigor-config.json5`
- `entry/`：stage 模型模块（module.json5、build-profile.json5、hvigorfile.ts）
- `entry/src/main/ets/entryability/EntryAbility.ts`：在 `onCreate` 调用 `test.RunCapTests(filesDir)`
- `entry/src/main/ets/pages/Index.ets`：最小 UI 页
- `entry/src/main/cpp/CMakeLists.txt`、`cap_hap_test.cpp`、`cap_tests.c/.h`（与 shell 探针同源）、
  `types/libcaptest/index.d.ts`
- 资源：`resources/base/{element,color,profile,media}`
- **包名（bundleName）**：`com.example.ohosnativecaptest`
- **结果落点**：应用私有文件 `<context.filesDir>/cap_results.txt` + hilog（tag `NativeCapTest`）

**Native 实现已验证可编译**（绕开 hvigor/签名，直接用 `aarch64-unknown-linux-ohos-clang`
交叉编译并链接）：
```sh
. ci/ohos/env-ohos.sh
$CC -O2 -fPIC -c hap/.../cpp/cap_tests.c -o cap_tests_hap.o
$CC -O2 -fPIC -std=c++17 -c hap/.../cpp/cap_hap_test.cpp -o cap_hap_test.o
$CC -shared cap_tests_hap.o cap_hap_test.o -o libcaptest_hap_native.so -lhilog_ndk.z
# => 成功；导出符号 RunCapTests / captest_add / run_cap_tests（aarch64 DYN）
```

**⛔ 阻塞点（如实报告，未伪造 HAP 结果）**：
1. 本机 DevEco Studio 6.1.1 安装中 **`hapsigner` 签名工具缺失**（`sdk/.../toolchains/hapsigner`
   不存在；`hvigor-ohos-plugin` 也未内联签名器）。HAP 必须经 `hapsigner` 签名后才能 `hdc install`。
2. DevEco “自动签名”依赖 **AppGallery Connect / 华为账号后端**，在无头环境（无账号、无交互）下
   不可得。
3. 因此：**无法生成可安装的已签名 HAP → 无法在第三方应用沙箱中运行 → 未取得应用沙箱运行时数据**。
4. 已尝试 `ohpm install`（空依赖，0s 完成）与 `hvigorw assembleHap`（因缺 `hvigor-config.json5`
   报错，已补；但即便补齐也无法越过签名阻塞）。

**结论**：HAP 的**实现**已完成且 native 代码可编译；**应用沙箱运行时实测**因签名工具/后端阻塞
而**未能完成**。**shell 域结果仅是 shell 域证据，不构成对第三方应用沙箱的结论**，二者不可互替。
若后续具备 `hapsigner` 或已配置 AGC 自动签名，只需在 `experiments/ohos-native-exec/hap/` 下执行
`hvigorw assembleHap` 并 `hdc install`，即可补齐 §11.3 的应用沙箱实测表（届时把 hilog +
`cap_results.txt` 取回填入）。

### 11.4 复现清单（命令 / 路径汇总）

| 用途 | 值 |
|------|----|
| 构建 shell 探针脚本 | `build-ohos-native-exec/build-cap-tests.sh` |
| shell 探针可执行 | `build-ohos-native-exec/cap_shell_probe`（静态 aarch64-linux-ohos） |
| shell 探针 dlopen 兄弟库 | `build-ohos-native-exec/libcaptest.so` |
| shell 实测原始输出 | `build-ohos-native-exec/shell-cap-results.txt` |
| HAP 工程根 | `experiments/ohos-native-exec/hap/` |
| HAP 包名 | `com.example.ohosnativecaptest` |
| HAP 结果文件落点 | `<应用 filesDir>/cap_results.txt`（安装运行后） |
| HAP 结果日志 | hilog，tag `NativeCapTest` |
| HAP native 可编译验证产物 | `build-ohos-native-exec/libcaptest_hap_native.so` |
| HAP 跨编译工具链 | `ci/ohos/env-ohos.sh`（aarch64-unknown-linux-ohos-clang） |
| HDC | `/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc` |

> **诚实收尾**：本节把“未验证运行时”的**一部分**（shell 域能力）升级为可复现实测，并给出了
> 真实 errno；但**第三方应用沙箱（HAP）这一更关键的一部分因签名工具缺失而未能实测**。两者
> 严格区分，未做任何替代或夸大。模拟器为 API 24，可能与真实 HarmonyPC 设备存在行为差异。
