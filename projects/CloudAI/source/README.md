# CloudAI 双实现源码

本目录提供 CloudAI 的两套可独立运行实现：

- `cpp/`：C++17 核心、可选 libcurl 后端、命令行入口和 CTest smoke test。
- `python/`：只依赖 Python 标准库的核心和命令行入口。

两套实现遵循同一份行为契约：

1. provider：OpenAI、OpenAI Compatible、OpenRouter、DeepSeek、SiliconFlow，以及可填写自定义 URL/API Key/模型名的 Custom OpenAI-compatible。
2. 配置：`config.json`、`config/cloudai_config.json`、`config/cloudai_secrets.json`。
3. 请求：OpenAI-compatible `POST /chat/completions`，固定 `temperature=0.7`、`stream=false`。
4. 附件：扩展名在白名单内的文本文件提取为最后一条消息的上下文，单文件最多 256 KiB，文本最多 12,000 字符；二进制、`.docx` 或无法读取的文件给出同样的提示。
5. 用量：按 provider 的已知地址及自定义 `/usage`、`/dashboard/billing/usage`、`/user/usage` 依次探测。
6. 安全行为：请求前后执行相同的基础内容审查，API Key 不写入公共配置，密钥文件使用带 `v1:` 前缀的可迁移混淆格式，并兼容旧明文值。

Agent 模式使用统一 JSON 动作协议：模型可请求 `read_file`、`write_file`、`run_command`、`open_url`、`search_web` 或 `final`，最多执行 12 步。文件路径限制在工作区及用户常用目录，二进制/敏感目录会被拒绝；写入采用备份和原子替换；命令、网址和搜索由桌面端逐次确认。HarmonyOS 端复用同一协议，但只允许应用沙箱内的文本读取，命令、外部网址和写入动作会明确返回不支持。

这是轻量混淆，不等同于操作系统密钥链或硬件加密。正式发布时应把 `ConfigStore` 的密钥读写替换为 HarmonyOS `@ohos.security`/系统密钥库实现。

## C++ 构建

```bash
cmake -S projects/CloudAI/source/cpp -B /tmp/cloudai-build
cmake --build /tmp/cloudai-build
ctest --test-dir /tmp/cloudai-build --output-on-failure
```

需要 libcurl 时：

```bash
cmake -S projects/CloudAI/source/cpp -B /tmp/cloudai-build-curl -DCLOUDAI_USE_LIBCURL=ON
cmake --build /tmp/cloudai-build-curl
```

## Python 运行

```bash
python3 python/cloudai.py /path/to/cloudai-data
```

也可以通过 `CLOUDAI_DATA_DIR` 指定数据目录，通过 `CLOUDAI_API_KEY` 注入当前 provider 的 API Key。
