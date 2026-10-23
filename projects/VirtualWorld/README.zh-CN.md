# VirtualWorld

[English](README.md) | [简体中文](README.zh-CN.md)

**作者 / 维护者：** 廖博轩（Liao Boxuan）

**开源协议：** [GNU General Public License v2.0 only](LICENSE)

VirtualWorld 是一个使用 C++17 与 Qt 编写的跨平台虚拟机管理器。项目将
QEMU、宿主系统虚拟化接口以及 Mini vMac 兼容运行时统一在同一套配置、能力探测、
后端选择、导入导出和图形界面之下。VirtualWorld 不重新实现 QEMU，而是把 QEMU
作为独立运行时管理和启动。

> **项目状态：积极开发中。** 支持矩阵表示代码已经实现的选择与回退逻辑；
> `Verified = No` 表示尚未在对应物理宿主上完成实机验证，不能理解为已经通过真机验收。

需要随项目构建 QEMU 时，先初始化官方 QEMU 源码子模块：

```bash
git submodule update --init projects/VirtualWorld/qemu-source
```

## 主要功能

- Qt Widgets 图形界面与命令行工具。
- 自动探测宿主能力，并在原生虚拟化、硬件加速与 QEMU TCG 之间可靠回退。
- 导入 QEMU 启动脚本、UTM `.utm` 包以及旧版 VirtualWorld JSON 配置。
- 导出 QEMU 脚本与 UTM 配置。
- 创建和管理 qcow2 磁盘、安装介质与虚拟机配置。
- 支持结构化 JSON 能力报告，方便 CI 和外部工具判断真实执行层级。
- System Software 0-6 使用独立的 Mini vMac 兼容运行时，不经过 QEMU。
- Support Disk 清单、签名、有效期与 SHA-256 校验。

## 支持平台与架构

| 宿主平台 | 宿主架构 | 同架构优先路径 | 跨架构路径 | 实机状态 |
| --- | --- | --- | --- | --- |
| Windows | x86-64 | WHPX / QEMU WHPX | QEMU TCG | 待验证 |
| Windows | ARM64 | WHPX / QEMU WHPX | QEMU TCG | 待验证 |
| macOS | x86-64 | Virtualization.framework / HVF | QEMU TCG | 待验证 |
| macOS | ARM64 | Virtualization.framework / HVF | QEMU TCG | 部分开发验证 |
| Linux | x86-64 | QEMU + KVM | QEMU TCG | 待验证 |
| Linux | ARM64 | QEMU + KVM | QEMU TCG | 待验证 |
| Linux | RISC-V 64 | QEMU + KVM（宿主支持时） | QEMU TCG | 待验证 |
| HarmonyOS / OpenHarmony | 视设备而定 | QEMU + KVM（系统暴露时） | QEMU TCG | 待验证 |

这里的“原生”仅指 VirtualWorld 直接使用宿主虚拟化 API；QEMU 通过 KVM、HVF 或
WHPX 加速时属于硬件加速。不同指令集之间不会误报为原生或硬件直通，跨 ISA 客户机
始终使用 TCG 软件模拟。

后端优先级固定为：

1. 同架构宿主原生虚拟化接口；
2. 真实可用的 QEMU 硬件加速；
3. QEMU TCG 软件模拟。

`qemu-tcg` 被明确固定在候选列表最后。后端只有在能力探测与配置校验都通过后才会被
选中，否则选择器会继续回退，并在 CLI/JSON 报告中给出原因。

更完整的逐宿主/客户机矩阵及状态码说明见[英文 README](README.md)和
[`docs/virtualization-architecture-matrix.md`](docs/virtualization-architecture-matrix.md)。

## 依赖

- CMake 3.16 或更高版本；
- 支持 C++17 的编译器；
- Qt 5 或 Qt 6 的 Core、Widgets、Network 组件；
- OpenSSL Crypto；
- 可选：QEMU 运行时或仓库中的 `qemu-source`；
- macOS 原生后端需要 Virtualization.framework；
- Linux KVM 需要可访问的 `/dev/kvm`；
- Windows WHPX 需要启用“虚拟机平台”并提供 `winhvplatform.dll`。

## 构建

在 LocalAI 总仓库根目录执行：

```bash
cmake -S projects/VirtualWorld -B build/virtualworld \
  -DCMAKE_BUILD_TYPE=Release
cmake --build build/virtualworld --config Release
```

如需关闭 Linux KVM 与 HarmonyOS 可选后端：

```bash
cmake -S projects/VirtualWorld -B build/virtualworld \
  -DVIRTUALWORLD_INCLUDE_CONTRIB_BACKENDS=OFF
```

### 构建随附 QEMU

QEMU 以独立程序形式运行，不静态链接到 VirtualWorld。若 `qemu-source` 子模块已正确
检出，可执行：

```bash
cmake -S projects/VirtualWorld -B build/virtualworld \
  -DVIRTUALWORLD_BUILD_BUNDLED_QEMU=ON
cmake --build build/virtualworld --target qemu-runtime --config Release
cmake --build build/virtualworld --config Release
```

可用 `VIRTUALWORLD_QEMU_TARGET_LIST` 减少 QEMU 目标数量。默认包括 x86_64、i386、
aarch64、arm、PowerPC、m68k、riscv64 与 loongarch64 等系统模拟器。

## 测试

```bash
ctest --test-dir build/virtualworld --output-on-failure
```

常规单元测试不会启动真实虚拟机。平台测试会验证注册、选择、降级、跨架构诚实性、
JSON 字段稳定性以及后端缺失时的回退。真实虚拟机启动必须单独、明确地进行，并记录
宿主、镜像来源、执行后端和测试结果。

## QEMU 运行时目录

VirtualWorld 按以下顺序查找 QEMU：

1. 环境变量 `VIRTUALWORLD_QEMU_RUNTIME`；
2. `runtime/qemu/<platform>/<host-arch>/bin`；
3. 同级平台/架构目录；
4. 应用程序或 `.app` 包内资源；
5. 系统 `PATH`。

推荐目录：

```text
runtime/qemu/
  windows/x86_64/bin/
  windows/aarch64/bin/
  macos/x86_64/bin/
  macos/apple-silicon/bin/
  linux/x86_64/bin/
  linux/aarch64/bin/
  linux/riscv64/bin/
  harmonyos/aarch64/bin/
```

## Mini vMac 与 System Software 0-6

System Software 0-6 使用 `runtime/system-software` 下的 Mini vMac 兼容运行时，
或使用环境变量 `VIRTUALWORLD_SYSTEM_SOFTWARE_RUNTIME` 指定的程序。该路径与 QEMU
翻译、磁盘创建和 Virtualization.framework 导出彼此独立。

Mini vMac 程序及其对应源码按 GPLv2 提供，许可证与源码归档位于
`runtime/system-software/licenses/`。**本项目不提供任何 ROM、System Software 或
客户机磁盘镜像**；源码归档也已移除示例 ROM 与磁盘目录。用户必须自行提供合法取得的
ROM，可直接选择 ROM 路径，或用 `VIRTUALWORLD_SYSTEM_SOFTWARE_ROMS` 指向私有的
四分片 Twiggy ROM 目录。Mini vMac 或 VirtualWorld 的 GPLv2 协议不会授权这些内容。

## 常用命令

```bash
VirtualWorld --gui
VirtualWorld --check x86_64
VirtualWorld --check aarch64 --json
VirtualWorld --list-backends aarch64 --json
VirtualWorld --create-qcow2 ./disk.qcow2 32768
VirtualWorld --import-qemu ./run.sh --save-config ./vm.json
VirtualWorld --import-utm ./Example.utm --save-config ./vm.json
VirtualWorld --print-command ./vm.json
VirtualWorld --export-qemu ./vm.json --output ./run.sh
VirtualWorld --export-utm ./vm.json --output ./Example.utm
VirtualWorld --start ./vm.json --backend auto
VirtualWorld --start ./vm.json --backend qemu-tcg --wait
VirtualWorld --stop <pid>
```

## 源码结构

```text
src/                         VirtualWorld 应用与虚拟化核心
tests/                       单元测试与集成测试
docs/                        平台、CI 与发布说明
cmake/                       运行时复制、清单与发布门禁
ci/virtualization/           CI 矩阵工具
tools/virtualization-conformance/  独立一致性验证器
runtime/qemu/                QEMU 运行时布局与说明
runtime/system-software/     Mini vMac 兼容运行时与资料
qemu-source/                 QEMU 源码子模块
```

`build*`、`dist`、本地日志、下载的运行时、虚拟磁盘和安装镜像都是生成物或本地数据，
不应作为 VirtualWorld 自有源码提交。

## 贡献

欢迎修复错误、补充平台验证、改进文档或增加测试。请先阅读
[CONTRIBUTING.md](CONTRIBUTING.md)：

- 一次提交聚焦一个清晰问题；
- 新功能应带有测试或可重复验证步骤；
- 平台探测必须失败关闭，不能把未知状态写成 `available`；
- 跨架构执行不得报告为 `Native`；
- 不得提交密钥、个人虚拟机数据或无权分发的系统镜像；
- 提交的贡献将按 `GPL-2.0-only` 分发。

安全问题请按 [SECURITY.md](SECURITY.md) 私下报告，不要先公开可利用细节。

## 开源协议与第三方组件

Copyright (C) 2026 廖博轩。

VirtualWorld 自有应用代码使用 **GNU GPL v2.0 only**（SPDX：
`GPL-2.0-only`），完整文本见 [LICENSE](LICENSE)。

QEMU、Mini vMac、Qt、OpenSSL、固件、ROM 与其他第三方文件继续使用各自原始协议。
GPLv2 不是对 ROM、客户机操作系统、安装介质或商标的授权。打包或发布前请阅读：

- [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
- [LICENSE-AUDIT.md](LICENSE-AUDIT.md)
- [OPEN_SOURCE_RELEASE_CHECKLIST.md](OPEN_SOURCE_RELEASE_CHECKLIST.md)

上述说明用于帮助维护者完成合规检查，不构成法律意见。
