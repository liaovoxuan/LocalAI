# LocalAI 桌面端

LocalAI 桌面端（`projects/LocalAI/desktop/local_ai.py`，应用名 `LocalAI`）现在同时支持**本地 provider** 和**云端 provider**。
原 `projects/CloudAI/` 桌面应用没有被删除，它的云端能力已经作为 LocalAI 的「云模式」集成进来；两套云实现的源码仍在
`projects/CloudAI/source/`，其 C++/Python 核心保持独立可运行，详见 [CloudAI 双实现源码](../CloudAI/source/README.md)。

本文档同时面向使用者（第 1–6 节）和维护者（第 7–9 节）。

## 1. 启动方式

```bash
# 桌面 GUI（默认；需要带 Tkinter 的 Python）
python3 projects/LocalAI/desktop/local_ai.py

# 终端交互模式（不进入 GUI）
LOCALAI_CLI=1 python3 projects/LocalAI/desktop/local_ai.py

# 等价写法；Tkinter 不可用时也会自动回退到终端模式
LOCALAI_NO_GUI=1 python3 projects/LocalAI/desktop/local_ai.py
```

`local_ai.py` 会把自身所在目录加入 `sys.path`，因此同一目录下的五个 `cloud*.py` 模块（见第 7 节）必须与它一起分发。

其他环境变量：`LOCALAI_PORTABLE=1` 把数据目录改为程序目录；`LOCALAI_FORCE_LLAMA_CPP=1` 把可用
provider 收窄为 `llama_cpp` 一种（用于只带 llama.cpp 的构建）。

### 数据目录

| 系统 | 默认目录 |
| --- | --- |
| macOS | `~/Library/Application Support/LocalAI` |
| Windows | `%LOCALAPPDATA%\LocalAI`（缺失时回退 `%APPDATA%\LocalAI`） |
| Linux | `$XDG_DATA_HOME/LocalAI`，或 `~/.local/share/LocalAI` |
| 便携模式 | 设置 `LOCALAI_PORTABLE=1` 时使用程序所在目录 |

```
<数据目录>/
  config.json                     公开配置（不含密钥）
  chats/  exports/  logs/
  config/cloudai_secrets.json     密钥保险库（见第 4 节）
  config/.legacy_migrated         旧 CloudAI 数据迁移标记
```

## 2. 本地 / 云端 GUI 开关

工具栏的**模式按钮**在 `local`（本地）与 `cloud`（云端）之间切换，切换结果写入 `config.json` 的 `mode` 字段。

| 能力 | 本地模式 | 云端模式 |
| --- | --- | --- |
| 请求出口 | `ollama` / `llama_cpp` / `lm_studio` / `openai_compatible` / `openai_official` | 第 3 节的云 provider |
| 模型选择 | Ollama 已安装模型列表（`last_model`） | 云端设置里的模型名（自由文本） |
| API Key | 本地 API 型 provider 用保险库槽位 | 云 provider 用保险库槽位 |
| Web 搜索开关 | 可用 | 不参与（云端请求直接走 provider） |
| Agent 开关 | 可用 | 按钮禁用，并提示切回本地模式 |

「设置」菜单项：外观（`light` / `dark` / `auto`）、**云端设置**（云模式）或**模型 + 服务商**（本地模式）、用量、语言
（30 种界面语言）、导出对话、壁纸。语言和主题会立即生效并保存。

### 请求进行中的界面守卫

`asking` 为真（本地或云端回复仍在生成）时，以下操作会被拒绝并提示「请等当前回复完成后再操作」：
新建对话、载入历史对话、切换模式、切换 Agent、打开服务商设置、打开模型选择、打开云端设置。
这样迟到的 `finish_ai_answer` 只会写回原对话，不会落到另一个会话里。

## 3. 云服务商配置

云 provider 预设（`cloudproviders.CLOUD_PROVIDER_PRESETS`，默认 `openai_official`）：

| provider id | 显示名 | 预置 base_url | 预置模型候选 |
| --- | --- | --- | --- |
| `openai_official` | OpenAI | `https://api.openai.com/v1` | `gpt-4.1-mini` `gpt-4.1` `gpt-4o-mini` `gpt-4o` |
| `openai_compatible` | OpenAI Compatible | 空，必须自填 | `gpt-4.1-mini` `qwen-plus` `deepseek-chat` |
| `deepseek` | DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` `deepseek-reasoner` |
| `openrouter` | OpenRouter | `https://openrouter.ai/api/v1` | `openai/gpt-4o-mini` `deepseek/deepseek-chat` `anthropic/claude-3.5-sonnet` |
| `siliconflow` | SiliconFlow | `https://api.siliconflow.cn/v1` | `Qwen/Qwen2.5-7B-Instruct` `deepseek-ai/DeepSeek-V3` |

上表是代码里的**预设值**，只是下拉框候选；模型名可以自由输入，可用模型、配额和计费一律以服务商控制台为准，
本项目不对任何 provider 作可用性承诺。

**云端设置对话框**（云模式下唯一的模型/服务商入口）包含四个字段：

| 字段 | 说明 |
| --- | --- |
| 服务商 | 下拉选择上表的显示名，内部保存规范化 id |
| 模型 | 文本输入，保存到 `cloud.providers[<id>].model` 与 `cloud.default_model` |
| Base URL | 文本输入；必须是以 `http://` 或 `https://` 开头的完整地址 |
| API Key | 密码输入框，显示为掩码；保存后写入保险库，不进入 `config.json` |

「删除密钥」只做标记，提示「密钥将在点击保存后删除」，真正删除发生在**保存**时。掩码占位符（`********`）不会被当成
真实密钥写入，也不会覆盖保险库里的旧值。

**自定义端点**用 `openai_compatible`：预置 base_url 为空，必须自己填写（例如本地或自建的 OpenAI 兼容服务），
模型名同样自由填写。

provider id 也接受别名，`custom`、`local`、`localhost`、`self-hosted`、`兼容`、`自定义` 都会规范化为
`openai_compatible`；`gpt`、`chatgpt` 规范化为 `openai_official`。

### 本地 provider 的密钥槽位

| 本地 provider | 保险库槽位 |
| --- | --- |
| `openai_official` | `openai_official` |
| `openai_compatible` | `openai_compatible` |
| `lm_studio` | `openai_compatible`（与前者共用） |

`ollama` 和 `llama_cpp` 没有密钥槽位，因此服务商设置里选择它们时不会接受 API Key。

## 4. 配置结构与密钥保险库

### 规范化配置（canonical）

```json
{
  "mode": "local",
  "provider": "ollama",
  "cloud": {
    "provider": "deepseek",
    "default_model": "deepseek-chat",
    "providers": {
      "deepseek": { "enabled": true, "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat", "api_key": "" }
    }
  }
}
```

保存时 `config.json` 会递归清除凭据字段（`api_key`、`openai_api_key`、`access_token`、`secret`、`api_secret`）、
所有 `v1:` 形式的密文和以下划线开头的内部状态，因此**磁盘上的 `config.json` 永远不含明文密钥**。
运行时需要的密钥在请求时从保险库按槽位读取。

### 旧 `cloud.public` 迁移

历史版本把云端配置放在 `cloud.public` 下。加载时会自动迁移到上面的规范化形状：

- `cloud.public.provider` → `cloud.provider`
- `cloud.public.model` → `cloud.default_model`
- `cloud.public.providers.<id>` 与规范化条目合并，**规范化字段优先**，旧结构只补空缺
- `cloud.base_url` / `cloud.api_key` 归入当前 provider 条目

迁移在内存中完成并回写；`cloud.public` 不会继续出现在 `config.json` 中。

### 保险库

| 项目 | 值 |
| --- | --- |
| 路径 | `<数据目录>/config/cloudai_secrets.json` |
| 形状 | `{"<槽位>": "v1:<base64>"}` |
| 编码 | `v1:` + Base64(明文 XOR salt)，salt 为固定字节串 `CloudAI` |
| 权限 | 尽力设为 `0600`（原子替换后 chmod，失败不报错） |

保险库是密钥的唯一持久化位置。写入后会回读校验，校验失败则报错并保留调用方输入；读取失败时界面提示
「无法读取密钥保险库」，不会把密文当明文使用。格式相同但无法解码的条目会**原样保留**并记为
`unrecoverable`；损坏的保险库文件会先复制为 `.corrupt` 再重写。

## 5. 附件

云端请求可以附带文件，支持范围：

- **文本（38 种扩展名）**：`.txt .md .markdown .csv .tsv .json .jsonl .yaml .yml .xml .html .htm .rtf .log .ini .cfg .conf .toml .py .js .ts .java .c .cpp .h .hpp .cs .go .rs .swift .kt .php .rb .sh .bat .ps1 .sql .css`
- **图片（12 种）**：`.png .jpg .jpeg .gif .bmp .webp .tiff .tif .heic .heif .avif .ico`
- **`.docx`**：只提取 `word/document.xml` 的正文文字
- **`.doc`**：不支持，会得到统一的「无法读取」提示

上限（`cloudattachments` + 主机侧）：

| 限制 | 值 | 常量 |
| --- | --- | --- |
| 单文件读取字节 | 262144（256 KiB） | `MAX_FILE_BYTES` |
| 单文件正文 | 12000 字符 | `MAX_FILE_CHARS` |
| **整个请求的附件上下文** | **24000 字符** | `CLOUD_ATTACHMENT_TOTAL_CHARS` |
| 截断标记 | `...[truncated]` | `TRUNCATION_MARKER` |

`ask_cloud` 会把主机上限作为 `total_limit` 传给 `cloudattachments.messages_with_files`，所以**无论附件数量多少，
聚合上下文都不会超过 24000 字符**，超出部分被截断并附带截断标记；单文件上限同时保留。

模型名命中原生文件模型特征（`gpt-4o`、`gpt-4.1`、`vision`、`vl`、`gemini`、`claude`、`llava` 等）时，附件改为
`image_url` / `file` 的 data URL 内容块；这条路径同样受 24000 字符聚合上限约束。读取失败、二进制或未支持的
扩展名不会抛异常，只会让模型看到一条「无法读取」的说明。

## 6. 流式、用量与错误

**流式**：云端请求使用 SSE，逐行解析 `data:` 帧，遇到 `data: [DONE]` 结束；连接直接关闭也会正常结束，
非法帧被跳过而不中断。界面事件一律写入回复队列，由 Tk 主线程每 50 毫秒轮询消费，**工作线程不调用任何 Tk 方法**，
本地模式与云模式走同一条通道。首个分片前显示「思考中」，增量文本按 70 毫秒节流刷新。

**用量**：云模式下「用量」打开异步对话框（超时 8 秒，在工作线程执行），依次探测该 provider 的已知地址，再探测
自定义 base_url 加 `/usage`、`/dashboard/billing/usage`、`/user/usage`；`404`/`405` 视为「该端点不存在」跳过，
其余失败记录后继续，只有全部端点都没有结果时才报错。摘要取响应中第一个命中的
`total_usage`/`total_credits`/`balance`/`total_granted`/`total_used`/`limit`/`used`/`remaining`/`total` 字段。
本地模式显示本地 token 统计并提供「重置用量」。

**错误**：云端失败统一映射为可翻译的分类，界面分别显示「尚未设置 API 密钥」「密钥无效或权限不足（401/403）」
「云请求失败，请检查服务地址、模型和网络」和「服务地址无效」四种提示。日志只记录脱敏后的诊断信息。
**云模式不会回退到 Ollama**：provider 报错就直接呈现云端错误，不会静默改用本地模型。

## 7. 打包（维护者）

`local_ai.py` 依赖同目录的五个模块，它们必须作为**顶层模块**随包分发：

```
projects/LocalAI/desktop/
  local_ai.py
  cloudattachments.py   附件读取与上限
  clouderrors.py        错误分类
  cloudproviders.py     provider 预设与 URL 校验
  cloudsecrets.py       保险库与旧数据迁移
  cloudusage.py         用量探测
```

`packaging/LocalAI.spec`（及 `local_ai.spec`）以 `SOURCE = desktop/local_ai.py` 为入口并把
`pathex=[desktop]` 交给 PyInstaller，因此这五个顶层 import 会被自动分析并打入产物。维护时注意：

- 不要把这些模块改名、移到子包或改成相对导入；`local_ai.py` 在非冻结运行时按裸名导入并把自身目录加入 `sys.path`。
- 如果某个构建配置裁掉了它们，应显式加入 `hiddenimports`，而不是依赖运行期 `sys.path` 兜底。
- 仓库已有的统一打包入口是 `./localai-pack build --project localai`；命令与产物清单规则见
  [仓库 README](../../README.md)。

## 8. 从 CloudAI 迁移

CloudAI 现在**集成在 LocalAI 里**：云 provider、密钥保险库、附件、用量探测和错误分类都在 LocalAI 桌面端可用。
`projects/CloudAI/` 旧工程及其 C++/Python 实现**保留在仓库中、不删除**，仍可单独构建和测试。

首次以云模式运行 LocalAI 时，会自动收割下列来源并写入保险库（优先级从高到低）：

| 顺序 | 来源 |
| --- | --- |
| 1 | 现有保险库 `<数据目录>/config/cloudai_secrets.json`（同槽位优先，不会被旧副本覆盖） |
| 2 | 旧公开配置 `<数据目录>/config/cloudai_config.json` 中 `providers.<id>.api_key` |
| 3 | 同级 CloudAI 数据目录 `<数据目录>/../CloudAI/config/cloudai_secrets.json` 与 `cloudai_config.json` |
| 4 | LocalAI 自己的 `config.json` 中的明文 `api_key` / `openai_api_key`（按 provider 映射到槽位） |
| 5 | CloudAI 1.0.1 桌面版用主机名作 salt 写出的密文（兜底尝试） |

迁移保证：

- **不丢数据**：无法解码的值原样保留并记为 `unrecoverable`，损坏文件先复制为 `.corrupt`。
- **幂等**：只填空槽位，重复运行不会覆盖已有密钥，第二次运行不再改动文件。
- **有标记**：写完后创建 `config/.legacy_migrated`；只有真正读取过来源才创建。
- **失败不阻断启动**：数据目录只读、保险库不可写或密钥无法归属时，加载仍会成功，密钥留在内存和原文件里。

旧 provider id `custom` 会自动规范化为 `openai_compatible`，无需手工改名。

## 9. 已知限制

- **未做真实 provider 认证**：所有验证都在隔离目录里用本地 HTTP 桩完成，没有对任何线上 provider 做过端到端联调；
  上线前请自行用真实账号、真实端点和真实模型验证。
- **跨文件崩溃原子性窗口**：`config.json` 与保险库是两个文件，各自原子写入，但两者之间没有事务。若在两次写入
  之间进程崩溃，可能出现「配置已更新、密钥仍是旧值」或相反的组合；此时重试保存即可收敛。
- **保险库是轻量混淆，不是加密**：`v1:` 格式为固定 salt 的 XOR + Base64，没有任何机密性保证；任何能读取该文件的人
  都能还原密钥。因此依赖文件系统权限，不要把密钥文件放进同步盘或版本库。高安全场景应改用系统密钥库。
- **平台构建验证边界**：PyInstaller 仍要求每个目标平台使用对应的 Python/运行时；Linux 应在你要支持的最老 glibc
  环境构建。仓库脚本只是把这些环境自动化，不是交叉编译器。签名、notarization、DMG/安装包制作不会被隐式执行。
- **界面细节**：主题和语言选择器不受第 2 节的请求守卫保护（它们只改外观和文案，不改变路由或会话归属）；
  本地模式的流式文本经过 50 毫秒轮询刷新，因此比云端事件入队略晚。
- **附件聚合上限按字符计**：24000 字符是「附件上下文」的预算，图片或原生文件模型走 data URL 时按编码后的字符数
  计算，因此单个大文件可能占据大部分预算，其余附件会被丢弃并标记截断。

## 相关文档

- [仓库 README](../../README.md)：项目分层、`./localai-pack` 打包命令、桌面端打包与运行环境
- [CloudAI 双实现源码](../CloudAI/source/README.md)：C++/Python 核心契约、Agent 动作协议、HarmonyOS 注意事项
- [LocalAI HarmonyOS 工程](harmonyos/README.md)
