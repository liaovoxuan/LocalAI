# VirtualWorld 打包 / 安装发布门禁

本文件描述 VirtualWorld 的**可重复打包 / 安装发布门禁（packaging & install
release gate）**。它只覆盖构建与分发的"包装层"，不修改任何引擎实现
（`src/` 下的代码都不在本门禁的改动范围内）。

门禁的目标：在一个构建产物（macOS `.app` 或任意平台的可移植安装目录）上，
把"一个能发给用户的发布包必须包含什么"这件事变成**可自动验证、可机器读取、
且永远诚实**的检查。

---

## 1. 开关（Switch）

门禁是 **opt-in（默认关闭）**，因此下列场景完全不受影响：

- 普通开发构建（`VIRTUALWORLD_ENABLE_PACKAGING_GATE` 未开）；
- `VIRTUALWORLD_INCLUDE_CONTRIB_BACKENDS=OFF`（contrib 后端关闭）；
- 某个平台后端缺失（`src/LinuxKvmEngine.*`、`src/HarmonyVirtualizationEngine.*`
  未编入）时的配置。

开启方式（在 `cmake` 配置时）：

```bash
cmake -DVIRTUALWORLD_ENABLE_PACKAGING_GATE=ON -B build .
cmake --build build
ctest --test-dir build -R VirtualWorldPackagingGate --output-on-failure
```

开启后会新增一个 CTest 测试 `VirtualWorldPackagingGate`。它：

1. 调用 `tests/VirtualizationPackagingTests.cmake`；
2. 用 `cmake -P` 脚本模式运行 `cmake/VerifyVirtualizationBundle.cmake` 与
   `cmake/GenerateVirtualizationManifest.cmake`；
3. 检查构建产物（macOS 上是 `build/VirtualWorld.app`，其它平台是
   `build/` 安装树）；
4. 把结果写入 `build/virtualization-release-manifest.json`。

若门禁判定失败（有缺件），该测试以非零状态退出并输出
`VIRTUALWORLD_PACKAGING_GATE_FAILED`，可接入 CI 阻断发布。

---

## 2. 门禁验证的内容

门禁按下列清单逐项检查。每一项都给出**稳定错误码**与**精确路径**；缺件不会
"假装通过"，也不会崩溃退出（除非确实判定失败）。

| 检查项 | 稳定错误码（缺失时） | 说明 |
| --- | --- | --- |
| 主程序存在 | `MAIN_EXECUTABLE_MISSING` | `VirtualWorld(.app/Contents/MacOS/VirtualWorld)` 或 `VirtualWorld[.exe]` |
| 主程序可执行权限 | `MAIN_NOT_EXECUTABLE` | POSIX 主机用 `test -x`；Windows 跳过 |
| runtime 目录存在 | `RUNTIME_DIR_MISSING` | `runtime/` 目录 |
| 平台 runtime 目录存在 | `RUNTIME_PLATFORM_DIR_MISSING` | `runtime/qemu/<host-platform>/` |
| QEMU 二进制存在 | `QEMU_BINARY_MISSING` | `*/bin/qemu-system-*` 与 `qemu-img` |
| QEMU 二进制可执行 | `QEMU_BINARY_NOT_EXECUTABLE` | POSIX 主机；Windows 跳过 |
| support 资源目录 | `SUPPORT_DIR_MISSING` | `support/` 目录非空 |
| 固件 / 数据 | `FIRMWARE_MISSING` | `*.bin *.rom *.edk2 *.fd *.nvram` |
| App bundle 布局 | `APP_BUNDLE_LAYOUT_INVALID` | 仅 macOS：`Contents/MacOS` + `Contents/Resources` |
| App 签名目录 | `APP_CODESIGN_MISSING` | 仅 macOS：`Contents/_CodeSignature` |
| App 签名可验证 | `APP_CODESIGN_INVALID` | 仅 macOS：`codesign --verify` |

平台路径布局（`vw_detect_layout`）按宿主自动选择：

- **macOS**：`Contents/MacOS/VirtualWorld` + `Contents/Resources/{runtime,support}`
- **Windows**：`VirtualWorld.exe`（或 `bin/`） + `runtime/` + `support/`
- **Linux / 其它 POSIX**：`VirtualWorld`（或 `bin/`） + `runtime/` + `support/`

---

## 3. Skip 语义（绝不伪造）

当前主机**无法真正验证**的检查，一律标记为 `skipped`，且永远不报告为通过：

- **非 macOS 主机**：`app-bundle-layout`、`app-codesign-present`、
  `app-codesign-verify` 全部 `skipped`（这些是 macOS `.app` 专属规则）。
- **Windows 主机**：主程序与 QEMU 二进制的 POSIX 可执行位检查 `skipped`
  （Windows 不使用 Unix 权限位）。
- **`codesign` 不可用**：即使是在 macOS，若找不到 `codesign` 工具，
  `app-codesign-verify` 也只 `skipped`，不会报成功也不会报失败。

这意味着：在 macOS 构建主机上，Windows/Linux 专属的"安装程序规则"不会被
伪造为通过；在 Windows/Linux 构建主机上，macOS 的 bundle/codesign 规则也不会
被伪造。每条检查的最终状态是 `ok` / `skipped` / 失败，三者互斥且透明。

**本门禁不下载、不运行任何新软件，也不启动任何虚拟机（VM）。**

---

## 4. 发布清单 `virtualization-release-manifest.json`

门禁总是（无论 verify 还是 scan 模式）生成一份机器可读清单。Schema 标识为
`virtualworld.release/1`。最低必须包含：

```jsonc
{
  "schema": "virtualworld.release/1",
  "product_version": "1.0",
  "generated_at": "2026-10-05T...Z",          // UTC 时间戳
  "host": { "os": "Darwin", "arch": "arm64" },
  "verified": false,                           // 永远 false（见 §5）
  "real_hardware_verified": false,             // 永远 false（见 §5）
  "contrib_backends": true,                    // VIRTUALWORLD_INCLUDE_CONTRIB_BACKENDS
  "backends": {
    "macos": true, "windows": true,
    "linux_kvm": true, "harmony": true         // 编入的布尔值
  },
  "main_program": { "path": "...", "sha256": "...", "size": 12345 },
  "qemu_binaries": [ { "path": "...", "sha256": "...", "size": 0 } ],
  "firmware_count": 7,
  "support_count": 6,
  "gate": { "status": "passed", "checks": [ ... ], "failure_count": 0,
            "failures": [ ... ] },
  "minivmac_inprocess": {
    "engine_selfprove_checks": 139,
    "engine_selfprove_failures": 0,     // 仅当 failures == 0 才 true
    "engine_selfprove_pass": true,
    "engine_selfprove_runs": 3,         // 门禁重跑次数
    "spawn_scan_clean": true,           // 必须 true，否则门禁失败
    "force_rebuild_fresh": true,        // 门禁运行前是否强制重建了引擎与自证二进制
    "product_spawn_scan": {
      "minivmac_path_clean": true,      // Mini vMac 路径无派生 API 违规
      "allowed_hits": 69,               // QEMU/平台后端命中（allow-list，非违规）
      "violation_hits": 0,
      "violations": []
    }
  }
}
```

字段要点：

- `schema`：固定 `virtualworld.release/1`，便于下游解析器版本协商。
- `host`：生成清单时的**宿主** OS / arch（由 `-DHOST_OS`/`-DHOST_ARCH` 传入，
  缺省回退到 `CMAKE_HOST_SYSTEM_NAME` / `CMAKE_HOST_SYSTEM_PROCESSOR`）。
- `contrib_backends` 与 `backends.{macos,windows,linux_kvm,harmony}`：如实记录
  本构建编入了哪些后端。Windows（WHPX）是纯 C++、每个构建都编入，因此恒为
  `true`；其余三者跟随其真实开关。
- `main_program.sha256`：主程序的 SHA-256。
- `qemu_binaries[]`：每个随包 QEMU 二进制的**相对路径 + SHA-256 + 大小**。
- `gate`：嵌入本次门禁结果（状态、逐项检查、失败项）。若仅 `scan` 模式则为
  `{"status":"not_run"}`。

---

## 5. 诚实原则：真机验证一律为 false

清单里 `verified` 与 `real_hardware_verified` **永远为 `false`**。

本门禁只验证"包装完整性"（文件在不在、权限对不对、布局对不对、签名能不能
验）。它**不**启动 VM、不运行 guest、不接触任何真实硬件，因此**绝不**把
"合成通过"或"编译通过"写成"真机验证通过"。任何真机 / 真实设备上的验证结论，
必须由专门的硬件测试流水线（`tools/virtualization-conformance`，由其它工作流
负责）另行提供，且同样应在其产物里显式声明，而不是在本清单里伪造。

支持矩阵里所有行在发布清单层面统一以 `real_hardware_verified: false` 标注，
直到有独立的真机证据为止。

---

## 6. Mini vMac 进程内引擎验证（发布链路）

Mini vMac 现已改造为真正的**进程内引擎**（不再派生子进程）。它在发布链路里
纳入三项独立验证，作为 CTest 运行，并且也被 `VirtualWorldPackagingGate`
门禁在 verify 模式下**真实重跑**，把结果写进发布清单（绝不伪造）：

| CTest / 步骤 | 验证内容 | 期望 / 判定 |
| --- | --- | --- |
| `VirtualWorldMiniVmacInProcessTests` | 引擎自证：生命周期 / 并发 / containment 测试套件 | 套件真实输出多少就是多少；门禁**重跑 3 次**，要求每次 `0 failures` / `PASS`（单次通过不足以证明稳定） |
| `VirtualWorldMiniVmacInProcessScan` | `scan_spawn_apis.py`（引擎扫描） | 必须输出 `RESULT: CLEAN`（**仅限引擎范围**：4 个引擎源文件 + 生产归档中不得出现派生 API；非全仓零 QProcess） |
| 产品树扫描（新增） | `scan_spawn_apis.py --product` | 区分 Mini vMac 路径与 QEMU/平台后端；Mini vMac 路径不得出现 QProcess/startDetached 等派生 API，否则判为**硬约束违规** |

实现要点：

- 引擎（C 静态库 `vw_minivmac_inprocess` 与测试归档 `vw_minivmac_inprocess_test`）
  由项目 CMake 直接编译：15 个上游核心 `.c` + `vw_minivmac_core.c` +
  `vw_screen_probe.c`（共 17 个目标文件），`std=c11`，仅依赖 libc 与 pthread，
  无 Cocoa/SDL/GTK/Win32/Qt。这与 `third_party/minivmac/inprocess/build_engine.sh`
  的翻译单元集合**完全一致**。
- C++ API（`src/MiniVmacInProcessCore.{cpp,hpp}`）编入 `VirtualWorld` 可执行文件
  并链接生产归档。该文件只读，本门禁不修改。
- 上游核心 `third_party/minivmac/upstream/**` 与引擎层
  `third_party/minivmac/inprocess/**` 均只读，必须与发布 tarball
  `runtime/system-software/licenses/Mini-vMac-VirtualWorld-Source.tar.gz`
  逐字节一致。
- **产品树扫描的 allow-list**（这些文件的派生 API 属 QEMU / 平台后端，
  不在 Mini vMac 约束内，不得误伤）：`VirtualizationCapabilities`、`QemuEngine`、
  `VirtualMachineWindow`、`RuntimeManager`、`Platform`、`LinuxKvmEngine`、
  `HarmonyVirtualizationEngine`、`WindowsHypervisorEngine`。任何**不在**该列表里的
  `src/` 派生 API 命中（历史上唯一一处是 `src/main.cpp`，revision 7 已由 JoyCode
  迁移到进程内 API，现命中数为 0）即被判定为 Mini vMac 路径违规。

诚实注意事项（以 `build-minivmac-inprocess/.minivmac-inprocess-ready` 的
**revision 7** 为准）：

- revision 7 的权威数字：**17 个目标文件 / 139 checks / 0 failures**，
  `consecutive_runs: 5/5 PASS`；`callback_exactly_once: true`、
  `frame_pixels_available: true`（512×342 / 21888 字节，窗口已可直接出画面）。
  revision 6 新增多机型变体（plus / 512ke / 128k / twiggy，各自 124 checks），
  revision 7 补齐鼠标输入（setMouseButton / setMouseWheel）并修复一处输入竞态
  （TSan 实测），升到 139 checks。早先"113 / 116 / 117 checks、callback 与
  frame 均为 false"是 v1/v2 的陈旧内容。
- 历史偶发失败**已被引擎所有者修复**：revision 3 统一了帧回调投递路径
  （原先 `emitLatestFrame()` 在循环末尾补投一帧却不计入引擎计数，造成 off-by-one）；
  revision 4 修复了 `test_destroy_races_stop` 自身 delete+stop 并发 UB。
  因此现在 139/0 稳定，但门禁仍**重跑 3 次**以防回归——117→124→139 演化期间
  确实出现过间歇性失败，单次通过不足为证。
- **门禁在运行前强制重建引擎与自证二进制**（`cmake --build --target
  minivmac_engine_standalone` 与 `--target VirtualWorldMiniVmacInProcessTests`）。
  原因：曾有一次门禁跑的是**旧构建产物**（自证报告 checks=124，与实际 139 不符），
  拿旧 checks 数字比对产生了**假失败**。强制重建后门禁永远比对 freshly-built 的
  引擎，不再假失败；重建若失败，门禁直接判 `failed`（`MINIVMAC_REBUILD`），绝不拿
  可能陈旧的产物蒙混。该结论与 codesign **无关**——codesign 检查只是 macOS 专属的
  `skipped` 项，从不是那次失败的原因（原先"codesign 导致门禁失败"的判断已撤回）。
- **单变体约束**：引擎每个机型变体定义约 192 个全局符号，plus 与 128k 即冲突 176 个；
  静态库链接对重复符号不报错，而是保留先满足者，机型由**链接顺序静默决定**。因此
  **同一可执行文件只能链接一个变体**。当前 `VirtualWorld` 只编入默认（plus）变体
  （`VW_MINIVMAC_INCLUDE` → `cfg/` = Mac Plus）。变体归档
  （`build-minivmac-inprocess/libvwminivmac-<variant>.a`）仅由
  `minivmac_engine_standalone` 自定义 target 产出、供验证使用，**绝不**加入
  `target_link_libraries(VirtualWorld)`。要发布其它机型，须另建专属引擎归档与专属
  CMake target，不可将多个变体共链接。详见 `CMakeLists.txt` 中的 SINGLE-VARIANT
  CONSTRAINT 注释。
- 三项验证的结果均由**测试退出码与解析输出**推导，不由声明决定；门禁在
  scan 非 CLEAN、自证任一跑 failures>0、产品树 Mini vMac 路径有违规、或强制重建
  失败时，把门禁判为 `failed`（绝不假绿）。
- 发布清单 `minivmac_inprocess` 块含 `engine_selfprove_runs`、
  `force_rebuild_fresh` 与 `product_spawn_scan`（含 `minivmac_path_clean` /
  `allowed_hits` / `violation_hits` / `violations`），如实记录产品树扫描结论。

### 6.1 main.cpp 的 Mini vMac 派生路径（**已解决**）

`src/main.cpp:356-364`（CLI `--start` 的 `isSystemSoftwareConfig` 分支）曾
构建 `QProcess` 并调用 `process.startDetached(&pid)` 启动 Mini vMac（现已修复，见下）：

```cpp
vw::SystemSoftwareEngine engine;
if (vw::ensureSystemSoftwareRom(...) && engine.prepareProcess(process, launchConfig, &error)) {
    started = process.startDetached(&pid);   // src/main.cpp:363
}
```

这同时违反两条硬约束：**禁止进程派生** 与 **禁止 PID 语义**。它是一条
**Mini vMac 启动路径**（先 `ensureSystemSoftwareRom` + `engine.prepareProcess`，
再 `startDetached`），与系统软件引擎内部已被清理的派生无关——`SystemSoftwareEngine`
内部的派生已迁移到 `MiniVmacInProcessCore` 进程内 API，唯独 `main.cpp` 这一条被遗漏。

该问题**不在本任务范围**，且牵涉 JoyCode 已删除的 `SystemSoftwareEngine::prepareProcess`
（同处 `main.cpp:362` 曾导致编译失败），属跨 agent 一致性修复。本门禁**不修改**
`main.cpp`；现由 JoyCode 完成迁移，blocker 已解除。

**现状（revision 7 标记 `main_cpp_fixed: true`）**：main.cpp 的 system-software 分支已
迁移到进程内 API——移除 `QProcess` / `prepareProcess` / `startDetached(&pid)`，改用
`SystemSoftwareEngine::start()` 并以 `shared_ptr` 持有到 `--wait` 结束（进程内运行，
局部对象析构即停机），`pid` 恒为 0。`#include <QProcess>` 已移除；main.cpp 在
`build-minivmac-integrate` 真实编译通过。

**验证**：门禁的产品树扫描对 `src/main.cpp` 命中数现为 **0**；
`product_spawn_scan.minivmac_path_clean = true`，`violation_hits = 0`，
`allowed_hits = 69`（均为 QEMU / 平台后端，属 allow-list，非违规）。完整
`VirtualWorld.app`（约 2.98 MB）可正常链接，门禁 `verify` 模式跑通。

后果：引擎本身已真实编入公共构建（所有 backend test 与 Translator test 均编译并链接
`MiniVmacInProcessCore`），两项引擎验证 + 产品树扫描 + 强制重建均已就绪，门禁 `verify`
可正常执行。唯一仍失败的 CTest 是 `VirtualWorldTranslatorTests`（JoyCode 的
`buildArguments()` 契约变更导致 `tests/TranslatorTests.cpp:54` 断言失败），不在本门禁
任务范围。本任务**不声称集成已完整通过**——但 main.cpp 这一历史 blocker 已解除。

---

## 7. 独立运行（命令行）

不借助 CTest 也能直接跑脚本：

```bash
# 仅生成清单（不做通过/失败判定）
cmake -DPACKAGING_GATE_MODE=scan \
      -DBUNDLE_ROOT=build/VirtualWorld.app \
      -DPRODUCT_VERSION=1.0 -DHOST_OS=Darwin -DHOST_ARCH=arm64 \
      -DCONTRIB_BACKENDS=ON \
      -DBACKEND_MAC=ON -DBACKEND_WINDOWS=ON \
      -DBACKEND_LINUX_KVM=ON -DBACKEND_HARMONY=ON \
      -DMANIFEST_FILE=release-manifest.json \
      -P tests/VirtualizationPackagingTests.cmake

# 运行门禁 + 生成清单（失败会 FATAL_ERROR）
cmake -DPACKAGING_GATE_MODE=verify \
      -DBUNDLE_ROOT=build/VirtualWorld.app ... -P tests/VirtualizationPackagingTests.cmake
```

也可以只跑验证或只跑清单生成：

```bash
cmake -DVW_VERIFY_ENTRY=1 -DBUNDLE_ROOT=build/VirtualWorld.app \
      -P cmake/VerifyVirtualizationBundle.cmake
cmake -DVW_MANIFEST_ENTRY=1 -DBUNDLE_ROOT=build/VirtualWorld.app \
      -P cmake/GenerateVirtualizationManifest.cmake
```

---

## 8. 允许改动范围与文件清单

本门禁属于"包装层"工作，受严格文件范围约束。允许改动 / 新建的文件：

- `CMakeLists.txt`：新增 `VIRTUALWORLD_ENABLE_PACKAGING_GATE` 开关与
  `VirtualWorldPackagingGate` 测试（默认关闭）；并新增 Mini vMac 进程内引擎的两个
  C 静态库 target（`vw_minivmac_inprocess` 生产、`vw_minivmac_inprocess_test` 测试）、
  把 `src/MiniVmacInProcessCore.{cpp,hpp}` 编入 `VirtualWorld`、以及两个引擎验证
  CTest（`VirtualWorldMiniVmacInProcessTests`、`VirtualWorldMiniVmacInProcessScan`）。
- `cmake/VerifyVirtualizationBundle.cmake`：门禁核心逻辑与稳定错误码。
- `cmake/GenerateVirtualizationManifest.cmake`：清单生成。
- `tests/VirtualizationPackagingTests.cmake`：CTest / 命令行驱动脚本。
- `docs/virtualization-release.md`：本文件。

**不在**本工作范围内（不得修改）：所有 `src/` 实现、既有 C++ 测试、四个平台
文件及其文档、`README.md`、`tools/virtualization-conformance`、`qemu-source`、
`qemu-master`、`build-final` 等既有构建目录。
