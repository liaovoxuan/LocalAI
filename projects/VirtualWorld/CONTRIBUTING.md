# Contributing to VirtualWorld / 参与 VirtualWorld

[中文](#中文) | [English](#english)

## 中文

感谢你参与 VirtualWorld。提交代码前请先确认改动边界，并避免把本地构建产物、虚拟机
数据或第三方受限内容带入仓库。

### 基本流程

1. 从最新主分支创建短生命周期分支。
2. 一次提交只解决一个清晰问题；不要顺带格式化无关文件。
3. 遵循现有 C++17、Qt 与 CMake 风格。
4. 功能改动应补充测试；平台功能应记录可重复的宿主/架构验证证据。
5. 执行：

   ```bash
   cmake -S projects/VirtualWorld -B build/virtualworld \
     -DCMAKE_BUILD_TYPE=Release
   cmake --build build/virtualworld --config Release
   ctest --test-dir build/virtualworld --output-on-failure
   ```

6. 在提交说明中写清动机、行为变化、测试结果与尚未验证的平台。

### 虚拟化规则

- 能力探测应失败关闭；未知、空或异常状态不得变成 `available`。
- 跨 ISA 客户机必须走软件模拟，不得报告为原生执行。
- `qemu-tcg` 必须保持最终兜底后端。
- 平台专用代码应保留缺失时的 QEMU 回退路径。
- 不要在普通测试中启动真实虚拟机；实机测试必须由操作者明确授权。

### 许可与提交内容

提交贡献即表示你有权提交该内容，并同意它按项目的 `GPL-2.0-only` 协议分发。
第三方代码必须保留来源、版权和许可证，不得提交：

- 私钥、密码、令牌或个人信息；
- 用户虚拟磁盘、快照、日志或配置；
- 无明确再分发权的 ROM、固件、系统软件或安装镜像；
- 无对应源码与许可证义务说明的 GPL 二进制文件。

行为准则很简单：尊重他人，讨论技术问题而非人身，不骚扰、不歧视，并善意对待审查意见。

## English

Thank you for contributing. Keep each change focused, preserve unrelated work,
and do not commit build output, personal VM data, or restricted third-party
material.

### Workflow

1. Create a short-lived branch from the current main branch.
2. Solve one clear problem per change; avoid unrelated formatting.
3. Follow the existing C++17, Qt, and CMake style.
4. Add tests for behavior changes and reproducible host/architecture evidence
   for platform claims.
5. Configure, build, and test with the commands above.
6. Describe the motivation, behavior change, test results, and any platforms
   that remain unverified.

### Virtualization invariants

- Capability detection must fail closed; unknown or empty status never means
  `available`.
- A cross-ISA guest must use emulation and must never be reported as native.
- `qemu-tcg` remains the final fallback.
- Platform code must preserve the QEMU fallback when unavailable.
- Ordinary tests do not start a real VM. Real-VM tests require explicit
  operator authorization.

### Licensing

By submitting a contribution, you confirm that you have the right to submit it
and agree that it may be distributed under `GPL-2.0-only`. Preserve all
third-party attribution and license notices. Do not contribute credentials,
private VM data, unlicensed ROMs/guest media, or GPL binaries without the
required corresponding source and notices.

Be respectful, discuss the work rather than the person, and treat review
comments in good faith.
