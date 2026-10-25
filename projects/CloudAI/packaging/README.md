# Linux ARM64 / RISC-V 64 桌面包

GitHub Actions 的 **Build App** 工作流会调用 **Build CloudAI Linux**，
在同一个运行页面列出 `CloudAI-linux-arm64` 和 `CloudAI-linux-riscv64` 任务及产物。
两个 CloudAI 任务独立于 LocalAI 的 llama.cpp 和 VirtualWorld。ARM64 使用 `ubuntu-24.04-arm` 原生 runner；
RISC-V 64 使用 QEMU 和 `ubuntu:24.04` 的 `linux/riscv64` 用户空间。
PyInstaller 会在目标架构上打包 Python 和 Tk/Pillow；缺少预编译 bootloader 时从源码构建。

在 Actions 页面运行 **Build App**，或推送修改到 main，即可从该次 **Build App**
运行底部的 Artifacts 下载 `CloudAI-linux-arm64` / `CloudAI-linux-riscv64`。
也可以手动运行 **Build CloudAI Linux** 单独打包。每个 artifact 包含：

- `cloudai-1.0.1-linux-<arch>.deb`
- `cloudai-1.0.1-linux-<arch>.rpm`
- `cloudai-1.0.1-linux-<arch>.tar.gz`（解压后运行 `CloudAI/CloudAI`）
- `ARCHITECTURE.txt`、`SHA256SUMS`、`build-manifest.json` 和桌面运行库验证报告

版本号读取 `shared/resources/version.json` 的 `components.CloudAI`。
安装前可运行 `sha256sum -c SHA256SUMS`。Debian/Ubuntu 使用
`sudo apt install ./cloudai-1.0.1-linux-arm64.deb`；RPM 系统使用
`sudo dnf install ./cloudai-1.0.1-linux-arm64.rpm`（RISC-V 替换架构名）。

要求 glibc 2.39 或更高版本，以及提供 X11 / XWayland 的 Linux 桌面；
不需要另装 Python。配置默认写入 `~/.local/share/CloudAI`。
这两个包是 Linux 桌面版，不能作为 Android 或 HarmonyOS 安装包使用。

本地复现（需要 Docker；RISC-V 需先配置 binfmt/QEMU）：

```sh
docker run --rm --platform linux/arm64 \
  -e DEBIAN_FRONTEND=noninteractive -v "$PWD:/workspace" -w /workspace \
  ubuntu:24.04 bash tooling/scripts/build_cloudai_linux.sh arm64
```

将两处 `arm64` 替换为 `riscv64` 即可构建 RISC-V。构建会验证所有 ELF
的真实架构，在 Xvfb 中运行冻结后的 `CloudAI --smoke-test`，然后检查 deb/rpm
架构字段，分别解包 DEB、RPM 和 tar.gz，检查每个 ELF 的 CPU 架构和 SHA-256
与原始构建一致，并再次执行桌面运行库检查。随后在没有系统 Python 的全新容器内
安装 deb，测试桌面启动器，再卸载并测试位于含空格路径下的便携包；HTTPS CA 证书
也必须来自包内。产物位于 `release/cloudai/<arch>/`。

原来的 `Build App` 工作流仅构建 Linux x86_64，其 artifact 曾误称为
`LocalAI-linux-universal-1.0`；现已更名为 `LocalAI-linux-x64-1.0`。
这个 x64 artifact 中的 CloudAI 是 x86_64 版本，不能用于 ARM64。
ARM64 请选 **CloudAI-linux-arm64**；Debian 名称是 `arm64`，ELF/RPM 名称是 `aarch64`，
二者表示相同的 64 位 ARM 架构。32 位 `armhf` / `armv7l` Linux 不能运行此包。
