# HarmonyOS/OpenHarmony 平台侧进程内 Mini vMac 接入审计报告

**日期**: 2026-10-07（修订）
**结论**: Mini vMac 路径无进程派生；进程内 API 已接入（键/鼠标/帧/生命周期）；QProcess::execute 符号存在于最终二进制中，但来自 QEMU/平台后端，不在本模块的禁派生约束内

**重要区分**: 本报告是 **Mini vMac 模块审计**，不是 VirtualWorld/HarmonyOS 总发布合规判定。QEMU/平台后端的 QProcess 命中仅作为模块审计分类结果记录，不等于 Harmony 最终发布合规。

**实机验证状态**:
- `real_hardware_verified = false` — 未在 HarmonyOS 实机执行
- `guest_started = false` — 未启动真实 guest OS
- 模块审计通过 != 总发布通过

---

## 1. 进程派生扫描

### 1.1 引擎源码 + 归档（scan_spawn_apis.py，无 --product）
| 范围 | 源文件数 | spawn 命中 | 归档目标数 | spawn 命中 |
|------|---------|-----------|-----------|-----------|
| `third_party/minivmac/inprocess/` | 4 | 0 | 17 objects | 0 |

**结论**: CLEAN — 无 fork/exec/posix_spawn/popen/system

### 1.2 nm 符号检查（引擎归档）
```
nm libvwminivmac.a | grep -E '(fork|exec|posix_spawn|popen|system)'
```
**结论**: NO FORBIDDEN SPAWN SYMBOLS

### 1.3 nm 符号检查（最终二进制）
最终 app 二进制中**确实存在** spawn 相关符号：
- `_ZN8QProcess7executeE...` — `QProcess::execute`，真 spawn 能力
- `_ZN14QStandardPaths14findExecutableE...` — `QStandardPaths::findExecutable`，**路径查询**（非 spawn）

**事实纠正**: `QStandardPaths::findExecutable` 是路径查询函数，仅检查可执行文件是否存在于 PATH 中，**不派生子进程**。它不应与 `QProcess::execute` 并列为"真 spawn 能力"。之前的报告将二者并列是事实错误。

这些符号来自 QEMU/平台后端代码（QemuEngine、RuntimeManager、HarmonyVirtualizationEngine 等），QEMU 本身必须能派生子进程，不在 Mini vMac 模块的禁派生约束内。

**注意**: 以下符号虽含关键词但非 spawn，属误报：
- `QApplication::exec` / `QMenu::exec` / `QDialog::exec` — Qt 事件循环
- `chrono::system_clock` / `__throw_system_error` — C++ 标准库
- `VZVirtioFileSystemDeviceConfiguration` — Apple Virtualization 框架（含 "FileSystem"）

### 1.4 产品路径源码扫描（scan_spawn_apis.py --product）
**结论**: SPAWN-APIS-FOUND — 但全部命中属于 QEMU/平台后端，不在 Mini vMac 禁派生约束内：

| 文件 | 用途 | 约束内？ |
|------|------|---------|
| `QemuEngine.cpp` | QEMU 子进程启动 | 否 — QEMU 兼容层 |
| `RuntimeManager.cpp` | QEMU 运行时管理 | 否 — QEMU 兼容层 |
| `Platform.cpp` | 平台抽象 | 否 — 平台后端 |
| `LinuxKvmEngine.cpp` | Linux KVM 后端 | 否 — 平台后端 |
| `WindowsHypervisorEngine.cpp` | Windows HV 后端 | 否 — 平台后端 |
| `HarmonyVirtualizationEngine.cpp` | QEMU 探测/启动 | 否 — QEMU 兼容层 |
| `VirtualMachineWindow.cpp` (QMP/QProcess 分支) | QEMU Monitor 通信 | 否 — QEMU 兼容层 |

**Mini vMac 路径（SystemSoftwareEngine.cpp、main.cpp system-software 分支、VirtualMachineWindow.cpp 所有 isSystemSoftwareConfig 分支）**: 无 QProcess/spawn。

**发布门禁判定**: 不应按"全仓无 spawn"判定——最终二进制包含 `QProcess::execute` 等真 spawn 符号，这些属于 QEMU/平台后端的合法需求。门禁应按模块判定：Mini vMac 路径无 spawn 即通过。

---

## 2. 进程内 API 接入状态

| 文件 | 改动 | 约束内 API 覆盖 | 状态 |
|------|------|----------------|------|
| `SystemSoftwareEngine.cpp/.hpp` | 删除 prepareProcess，改用 MiniVmacInProcessCore 的 create/loadRom/attachDisk/start/stop/setKey/setMouse/setMouseButton/setMouseWheel/setFrameCallback/latestFrame | 全套 | OK |
| `main.cpp` (行363-374) | shared_ptr 持有 SystemSoftwareEngine，--wait 用 isRunning() | 无 QProcess/spawn | OK |
| `VirtualMachineWindow.cpp` (isSystemSoftwareConfig 分支) | systemSoftwareEngine_ 替代 prepareProcess+process_.start；setKey/setMouse/setMouseButton/setMouseWheel/setFrameCallback 均已转发 | 键/鼠标/帧/生命周期 | OK |
| `HarmonyVirtualizationEngine.cpp/.hpp` | 无 Mini vMac 相关改动（QProcess 属 QEMU 兼容层） | N/A | OK |

**接入状态如实声明**:
- 键盘事件: 已通过 `setKey()` 转发（行591-594）
- 鼠标位置: 已通过 `setMouse()` 转发（行693-698）
- 鼠标按钮: 已通过 `setMouseButton()` 转发（行675-680）
- 鼠标滚轮: 已通过 `setMouseWheel()` 转发（行756-760）
- 帧 callback: 已通过 `setFrameCallback()` 接入（行1118-1120）
- 生命周期: start/stop/isRunning 已接入
- **未验证项**: real_hardware_verified=false, guest_started=false

---

## 3. 帧像素接通

- `latestFrame()` 返回 512x342 / 21888 字节 / 1-bit 像素
- 依赖 `cfg/CNFUDIPIC.h` 的 `SCRNvPage2`
- 测试验证: revision 3 统一单一投递路径，off-by-one 计数器竞态已修

---

## 4. 四变体支持

| 变体 | ROM 大小 | RAM 大小 | 测试结果 |
|------|---------|---------|---------|
| plus | 128 KiB | 1 MiB | 139/0 |
| 512ke | 128 KiB | 512 KiB | 139/0 |
| 128k | 64 KiB | 128 KiB | 139/0 |
| twiggy | 64 KiB | 512 KiB | 139/0 |

**约束**:
- 一个构建目标只能选一个变体，不支持运行期切换
- 多变体链接会静默取"先满足者"
- 用 `MiniVmacInProcessCore::romSize()` 查询 ROM 大小，不要硬编码

---

## 5. 引擎构建验证

- 编译单元: 17 objects（15 上游核心 + vw_minivmac_core.c + vw_screen_probe.c）
- C 标准: `-std=c11`（`<stdatomic.h>`）
- 依赖: 仅 libc + pthread，无外部依赖
- 单实例约束: 第二个实例返回 `MvStatus::Busy`

---

## 6. 生命周期测试

- **139 checks / 0 failures**（revision 7）
- 24 test names，四变体各 139/0
- 5 次连跑稳定
- revision 4 修 test_destroy_races_stop 的 UB
- revision 5-7 增加测试覆盖

---

## 7. Qt6 兼容性修复

- `Qt::Key_LeftBracket` -> `Qt::Key_BracketLeft`（VirtualMachineWindow.cpp:559）
- `Qt::Key_Backtick` -> `Qt::Key_QuoteLeft`（VirtualMachineWindow.cpp:576）
- 编译 + 链接通过

---

## 8. 禁止项确认

| 禁止项 | 状态 |
|--------|------|
| 随包分发 ROM | 未违反 |
| 放宽 ROM 尺寸校验 | 未违反 |
| 使用真实 ROM | 未违反 |
| 同时链接多个变体归档 | 未违反 |
| 修改 third_party/minivmac/upstream/** | 未违反 |
| 修改 third_party/minivmac/inprocess/ | 未违反 |
| 修改 src/MiniVmacInProcessCore.* | 未违反 |

---

## 9. 门禁扫描盲区（已加固）

**问题**: VirtualMachineWindow.cpp 整文件 allow-list 导致 Mini vMac 分支中的 QProcess 引用不被检查。同一文件同时有 QEMU（QProcess/process_）和 Mini vMac（systemSoftwareEngine_）分支。

**已加固**: 采用双层扫描策略：
1. **产品扫描**（scan_spawn_apis.py --product）保留 VirtualMachineWindow 在 allow-list 中，因为该工具无法区分同一文件内的 Mini vMac 和 QEMU 分支
2. **新增分支级扫描**（tools/verify_minivmac_inprocess_path.py）作为硬约束门禁步骤，解析 isSystemSoftwareConfig 分支范围，精确检测 Mini vMac 路径中的禁止 API

分支级扫描工具的关键特性：
- 仅匹配 `if (isSystemSoftwareConfig(...))` 模式（排除 return/赋值等非 if 上下文中的引用）
- 识别 QEMU 信号处理器（connect(&process_, ...) lambda）中的 ISSC 子分支，QEMU 上下文优先
- 区分硬违规（QProcess/process_/fork/exec 等真 spawn API）和建议性命中（closeQmp 等防御性清理）
- 识别 `} else if` 和 early-return 模式，避免将 else 路径误归入 ISSC 范围

当前扫描结果：15 个 ISSC 块、0 硬违规、1 建议性命中（closeQmp 防御性清理）、139 个 QEMU 路径命中（允许）

门禁集成：VirtualizationPackagingTests.cmake 步骤 (2b)，MINIVMAC_BRANCH_SPAWN 失败码，manifest 中 branch_spawn_scan.minivmac_path_clean 字段