# LocalAI for HarmonyOS

HarmonyOS 原生终端版本。界面使用 ArkTS，模型运行时通过 `hllama` OHPM 包接入设备侧 native `llama.cpp`，不嵌入 Python，也不依赖设备上的 Python 环境。终端页面直接使用 `HLlama.initLlamaContext` / `HLlama.startCompletionSync`。

## 功能

- 仅使用应用内 `llama.cpp` 加载 GGUF 模型。
- 模型选择后复制到应用沙箱，重启后自动恢复。
- Agent 模式入口固定显示；Agent 只能分析用户输入和已附加文本，不会伪造文件操作结果。
- 支持文本附件，单次读取上限为 32 KiB。
- 英文基础资源和简体中文本地化资源。
- `entry/oh-package.json5` 声明 `hllama`，`entry/src/main/ets/pages/Index.ets` 提供 `/model`、`/agent`、`/new` 和流式回答入口。

## 在 DevEco Studio 中打包

1. 使用 DevEco Studio 打开本目录。
2. 确认 HarmonyOS SDK API 24 和 Native SDK 已安装。
3. 在 `File > Project Structure > Signing Configs` 中配置自己的签名。仓库不包含证书或密码。
4. 选择 `entry` / `default`，执行 `Build > Build Hap(s)/APP(s) > Build Hap(s)`。

命令行构建：

```sh
/Applications/DevEco-Studio.app/Contents/tools/hvigor/bin/hvigorw \
  --mode project -p product=default assembleHap --no-daemon
```

构建结果通常位于 `entry/build/default/outputs/default/`。真机发布包必须使用开发者自己的签名。

## C++ 宿主测试

```sh
cmake -S . -B build-host
cmake --build build-host
ctest --test-dir build-host --output-on-failure
```

native 推理由 `hllama` 负责，应用代码只依赖稳定的 ArkTS API；因此不会在本工程重复编译 `llama.cpp`，也不会把桌面 ABI 带入 HarmonyOS。
