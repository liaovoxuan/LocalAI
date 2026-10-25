# LocalAI Workspace

仓库按产品、共享组件、工具和构建产物分层，避免不同项目的源码和打包文件互相覆盖。

## 项目目录

```text
projects/
  UTM/              原 UTM macOS/Xcode 工程与辅助工具
  LocalAI/          本地 AI 桌面端、HarmonyOS 工程和打包规格（HarmonyOS 通过 hllama 接入 GGUF 推理）
  LocalAI-SE/       LocalAI-SE 桌面端、HarmonyOS 工程和打包规格（HarmonyOS 通过 hllama 接入 GGUF 推理）
  CloudAI/          CloudAI 桌面端、HarmonyOS 工程、C++/Python 源码和测试（HarmonyOS 通过 NAPI 接入共享 C++ 核心）
  VirtualWorld/     VirtualWorld C++ 工程、QEMU 源码和运行时
shared/
  assets/           跨项目图标等资源
  resources/        共享配置、版本和数据集
  runtime/          可分发的 llama.cpp 运行时
  third_party/      第三方源码（当前包含 llama.cpp）
  native/            LocalAI/CloudAI 的共享 C++ 组件
tooling/
  scripts/          构建和模型下载脚本
  tests/            跨项目 Python 测试
  python/           维护工具模块
artifacts/           本地或 CI 生成的安装包、日志和临时文件
```

LocalAI 桌面端的本地/云端模式、云服务商与密钥配置、附件上限、打包要求和 CloudAI 迁移说明见
[`projects/LocalAI/README.md`](projects/LocalAI/README.md)。

## 模型分发

所有平台的安装包仅包含程序和推理运行时，不包含模型权重；CI 不下载默认模型。
本地缓存的模型也不会被打进安装包。

因备案安排，暂不向中国大陆地区提供内置模型下载。自备模型的导入和本地推理继续可用，
其他应用功能不受这项下载限制影响。LocalAI、LocalAI SE 的 GGUF 下载和 Ollama 模型拉取
在开始前通过 [Country.is](https://country.is/) 检查下载网络的公网 IP 所属地区；
地区为中国大陆或无法确认地区时暂停下载，并提示导入自备模型。地区检查仅在下载时联网，
不会在启动或本地推理时执行，也不在本地保存 IP 地址。第三方自行下载不属于本项目的下载入口。

## 克隆后的数据库准备

知识库以无损压缩归档 `localai_knowledge.db.gz` 保存，以符合 GitHub 单文件大小限制。
首次克隆后，在仓库根目录运行 `gzip -dk localai_knowledge.db.gz`，再启动本地聊天程序。
本机已有的 `localai_knowledge.db` 保持原样；更新知识库后，用
`gzip -c localai_knowledge.db > localai_knowledge.db.gz` 更新归档再提交。
子模块本地改动的还原步骤及 GitHub 同步说明见
[`tooling/GITHUB_SYNC.md`](tooling/GITHUB_SYNC.md)。

## 常用构建命令

### GitHub 下载 CloudAI Linux ARM64 / RISC-V 64

无需登录 GitHub 的公开下载入口是
[CloudAI 1.0.1 Linux Release](https://github.com/liaovoxuan/LocalAI/releases/tag/cloudai-v1.0.1-linux)：

- [ARM64 / AArch64 完整 ZIP](https://github.com/liaovoxuan/LocalAI/releases/download/cloudai-v1.0.1-linux/CloudAI-Linux-ARM64-aarch64.zip)
- [RISC-V 64 完整 ZIP](https://github.com/liaovoxuan/LocalAI/releases/download/cloudai-v1.0.1-linux/CloudAI-Linux-RISCV64.zip)

ZIP 内含对应架构的 DEB、RPM、便携 tar.gz、SHA-256 和架构核验报告。
Release 的 Assets 也提供单独的安装包。

开发构建位于 [Build App](https://github.com/liaovoxuan/LocalAI/actions/workflows/main.yml)。
选择成功运行，在页面底部 **Artifacts** 中按设备架构下载；
注意：[GitHub Actions 的 artifact 下载要求登录 GitHub](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts)，
即使仓库公开，未登录访问 artifact 下载地址也可能显示 404。公开下载请使用上面的 Release。

| Linux 架构 | 下载的 artifact | 包内可执行文件的 ELF 架构 |
| --- | --- | --- |
| ARM64 / AArch64 | `CloudAI-linux-arm64` | `ARM aarch64` |
| RISC-V 64 | `CloudAI-linux-riscv64` | `RISC-V`，LP64D ABI |
| x86_64 / AMD64 | `LocalAI-linux-x64-1.0`（包含 CloudAI 等产品） | `x86-64` |

ARM64 和 RISC-V 包包含 DEB、RPM、便携 tar.gz、SHA-256 校验值及验证报告，
要求 glibc 2.39 或更高版本及 X11/XWayland 桌面，不需要另装 Python。
旧 `LocalAI-linux-universal-1.0` 实际只适用于 x86_64。
安装和架构核验方法见 [CloudAI Linux 打包说明](projects/CloudAI/packaging/README.md)。

### 本地多平台桌面打包

仓库提供统一的 `localai-pack` 命令封装现有 PyInstaller spec。它会按项目和目标平台
选择 spec，清理隔离的工作目录，并在成功后生成 `artifact-manifest.json`（产物大小、
文件数和 SHA-256）。默认 `--backend auto` 会在当前系统使用 native；在 macOS/Linux
上发现 Wine 时，Windows 目标会走 Wine 中的 Windows Python；发现 Docker/Podman 时，
Linux 目标会在 Linux 容器中运行 Linux Python。PyInstaller 官方仍要求每个目标使用
对应平台的 Python/运行时，Linux 还应在你要支持的最老 glibc 环境构建；这些后端是把
对应环境自动化，并不是把 macOS Python 伪装成交叉编译器。

```bash
# 查看项目和本机工具链
./localai-pack list
./localai-pack doctor

# 构建当前 macOS Apple Silicon 的 LocalAI.app
./localai-pack build --project localai --target current --arch native

# macOS Intel/Apple 双架构（需要 macOS，且依赖支持 universal2 的 Python 环境）
./localai-pack build --project localai --target macos --arch universal2

# 构建 LocalAI SE 或 CloudAI
./localai-pack build --project localai-se
./localai-pack build --project cloudai

# macOS 上用 Wine 构建 Windows x64（先在 Wine 前缀安装 Windows Python、依赖和 PyInstaller）
LOCALAI_WINE_PYTHON="$HOME/.wine/drive_c/Python312/python.exe" \
  ./localai-pack build --project localai --target windows --arch x86_64 --backend wine

# Linux 容器构建（默认 debian:bookworm-slim；可用 --linux-image 指定基础镜像）
./localai-pack build --project localai --target linux --arch x86_64 --backend docker

# 对照已有清单验证产物；path 可以是 dist 目录或具体产物
./localai-pack verify --project localai --target macos --arch arm64 \
  artifacts/packages/localai/macos/arm64
```

首次使用需要有带 Tkinter 的 Python：用该 Python 执行
`python3 -m venv .venv-localai-pack-tk`，然后运行
`.venv-localai-pack-tk/bin/python -m pip install -r requirements.txt`。
本机已配置的 `.venv-localai-pack-tk` 会被 `./localai-pack` 优先使用。
默认产物位于 `artifacts/packages/<project>/<target>/<arch>/`，中间文件位于
`artifacts/work/`。可用 `--dist /absolute/path` 改变产物根目录，或用
`--no-clean` 保留上一次输出。签名、 notarization、DMG/安装包制作不会被隐式执行，
需要在目标平台完成验收后再通过平台工具显式处理。

```bash
# CloudAI C++ 核心和 smoke test
cmake -S projects/CloudAI/source/cpp -B /tmp/cloudai-build
cmake --build /tmp/cloudai-build
ctest --test-dir /tmp/cloudai-build --output-on-failure

# VirtualWorld
cmake -S projects/VirtualWorld -B /tmp/virtualworld-build
cmake --build /tmp/virtualworld-build --config Release

# llama.cpp 运行时
python3 tooling/scripts/build_llamacpp_runtime.py

# 桌面端打包（按目标选择对应 spec）
pyinstaller --clean --noconfirm projects/LocalAI/packaging/LocalAI.spec
pyinstaller --clean --noconfirm projects/LocalAI-SE/packaging/LocalAI_SE.spec
pyinstaller --clean --noconfirm projects/CloudAI/packaging/CloudAI.spec
```

HarmonyOS 工程分别从 `projects/LocalAI/harmonyos`、`projects/LocalAI-SE/harmonyos` 和
`projects/CloudAI/harmonyos` 用 DevEco Studio 打开；签名文件只在本机配置，不提交到版本库。

## VS Code 启动脚本（macOS）

运行 `./vscode-launch-verified.command` 会启动稳定版 VS Code、`Visual Studio Code 2.app` 和 Insiders：稳定版沿用原用户数据和登录状态，但 Codex 扩展通过 `~/.vscode-isolated/vscode1-portdan/codex-home` 与 `codex-vscode2-api-key.conf` 使用原有 `new.sharedchat.cc/codex` 中转站；Code 2 使用 `~/.vscode-isolated/vscode2-openai` 独立用户目录、简体中文界面并复用稳定版已注册的扩展（包括 `openai.chatgpt` Codex 插件），配置官方 OpenAI 提供方；Insiders 使用 `~/.vscode-isolated/insiders-portdan` 独立用户目录并复用 Insiders 扩展目录，固定访问 `https://portdan.com`，读取单独的 `codex-insiders-api-key.conf` API Key。Insiders 的模型下拉列表只显示 `DeepSeek V4 Flash` 和 `DeepSeek V4 Pro`，默认选择 Flash；其他模型不会出现在该列表中。

该脚本不会启动 ChatGPT 应用。ChatGPT 使用全局 `~/.codex/config.toml` 和官方 OpenAI 提供方；VS Code 1 的 `chatgpt.cliExecutable` 指向 `vscode1-codex-wrapper.command`，由包装器只为 VS Code 1 的 Codex 进程设置独立 `CODEX_HOME`。因此从 Dock/Finder 打开 VS Code 1 也不会让 ChatGPT 继承中转站配置。修改后需要重新加载一次 VS Code 1 窗口，已有的 Codex 进程不会自动更换环境。

Code 2 不会复制当前的 API Key 登录（当前凭据不是 OpenAI 官方 OAuth）。首次使用 Code 2 时，请执行脚本提示的 `CODEX_HOME=... codex login --device-auth`，按浏览器提示登录 OpenAI 官方账户；`401 Unauthorized` 通常表示这一步尚未完成。

也可以直接运行 `./vscode-code2-login.command` 完成 Code 2 的官方设备登录。登录成功后重新运行启动脚本即可；该凭据只写入 Code 2 的隔离目录，不会改变稳定版 VS Code 或 Insiders 的登录状态。

## 迷你离线聊天模型

```bash
# 启动聊天（默认使用 LLM 风格流式 delta；输入 /quit 退出）
python3 LocalAI-modle.py chat

# 流式输出并在每轮显示真实 byte-token、TTFT、总耗时和吞吐
python3 LocalAI-modle.py chat --show-stats

# 兼容需要整段输出的终端或脚本
python3 LocalAI-modle.py chat --no-stream

# 非交互式 JSONL 事件流，便于程序采集 delta 和 completed.usage
python3 LocalAI-modle.py stream --question "你好" --offline

# 重新训练；每次训练都会先后归档旧模型、新模型和 SHA-256 备案清单
python3 LocalAI-modle.py train

# 查看本机训练备案（位于 model_backups/，不提交到 Git）
python3 LocalAI-modle.py model-backups

# 运行聊天、数学、百科、历史、法律、汉字和英语词典测试
python3 LocalAI-modle.py test

# 生成并运行 1000+ 项离线系统评测
python3 LocalAI-modle.py build-eval
python3 LocalAI-modle.py eval

# 执行代表性离线工作负载并报告进程峰值 RSS
python3 LocalAI-modle.py benchmark-memory

# 综合测试内存、吞吐、上下文、并发及两套准确率
python3 benchmark_comprehensive.py

# 完全禁用数据库 MISS 后的联网检索
python3 LocalAI-modle.py chat --offline

# 非交互式询问本地文本/代码/CSV 文件
python3 LocalAI-modle.py ask-file --file README.md --question "如何运行系统评测？"

# 更新中国大陆全国性法律目录
python3 LocalAI-modle.py import-china-laws

# 从 Wikidata 扩充历史人物；重复执行会更新已有记录，不会生成重复项
python3 LocalAI-modle.py import-history-people --history-limit 10000 --min-sitelinks 5

# 从第 10000 条之后继续导入 20000 位，避免重复扫描前一批
python3 LocalAI-modle.py import-history-people --history-offset 10000 --history-limit 20000 --min-sitelinks 5

# 扩充生物、地理、科技及普通人常用的三语通用知识（可重复、可断点执行）
python3 LocalAI-modle.py import-knowledge --knowledge-limit 1500

# 网络中断后也可只续跑某些领域（名称可从 knowledge-stats 查看）
python3 LocalAI-modle.py import-knowledge --knowledge-limit 1500 --knowledge-topics biology,geography,technology

# 查看百科分类、来源索引和当前数据库容量
python3 LocalAI-modle.py knowledge-stats
```

聊天模型使用 `localai_tiny_model.npz`。当前 v2 模型有 180,256 个稀疏 n-gram 特征、21 个
意图类别和 3,785,397 个参数，文件为 30,285,796 bytes（30.29 MB / 28.88 MiB）。推理只读取
输入实际命中的特征，训练时也只构造活跃特征矩阵，因此扩大参数容量不会为每条短句创建巨大的
稠密向量。历史、汉字及英语资料按需从
`localai_knowledge.db` 查询，不会在启动时把整个数据库载入内存。历史人物条目包含
简体中文、繁体中文和英文名称、别名、生卒年、简介及来源。

基础百科覆盖地理、物理、化学、生物、计算机、常见物品、单位和常识。查询先经过
Unicode NFKC、`casefold()`、高频简繁映射和常见错拼归一，再按“精确词条/标题 →
FTS5 正文 BM25”检索。记录包含 `source`、`source_url`、`revision`、`confidence`；
存在争议的知识以多条 `knowledge_claims` 保存，不强行覆盖成唯一说法。
批量扩充器还覆盖天文、地球与环境、工程、健康常识、数学、交通能源和生活知识，
每条同时保存简体、繁体、英文摘要及 Wikipedia 页面修订号。导入内容按 CC BY-SA 4.0
记录逐语言署名链接。每次导入时 SQLite `max_page_count` 被锁在 592 MiB，并额外检查 WAL/SHM，
因此整个知识库不会越过 600 MiB 的产品上限；达到安全线时会保留已提交数据并停止。
当前随项目提供的快照含 835 条通用百科：生物 168、地理 154、科技 122、计算机 78、
物理 72、化学 69、天文 46、地球环境 46、健康 44、生活常识 32，另有原有物品和单位种子；
数据库约 139 MiB。训练文档有 110,372 行，系统评测含 1,751 项，其中 450 项从新增百科
确定性抽样生成。

意图训练另外加入 178 条国内表达样本，覆盖“在吗”“今儿”“咋用”“帮我瞅瞅”等口语、
常见错别字、国内生活办事和法律求助问法；这些样本与原有简体、繁体和英文训练数据合并，
不会以国内优化为由移除其他语言能力。独立国内表达题集为 21/21。

本地数据库没有答案时，在线模式会查询 Wikipedia API，并把结果单独缓存到
`search_cache.db`；失败或证据不足时明确拒绝猜测。网络缓存不会写入正式知识库。
`--offline` 会关闭新联网请求（已有缓存仍可读取）。

数学计算由受限 AST、`decimal.Decimal`、`fractions.Fraction` 和确定性规则完成，支持
基础算术、分数、高精度小数、三角函数、一次/二次方程、二元一次方程组、基础几何、
统计概率、单位换算、日期差和时区。超长整数会保存到桌面 UTF-8 文档。

每次执行 `train` 都会在 `model_backups/<时间戳>/` 建立不可混淆的训练备案：训练开始前
先复制当前 `model_before.npz`，候选模型回读验证成功并原子替换后再复制
`model_after.npz`，同时复制当次 `training_source.py` 和 `requirements.txt`。`manifest.json` 记录
训练参数、Python/NumPy 版本，以及代码、训练文档、知识库和模型的 SHA-256；若训练异常则保留 `failed` 状态和错误信息。该目录默认不进入 Git，
应按项目的备份策略另行复制到可靠存储。

聊天中可输入 `/open 文件路径` 读取不超过 10 MiB 的 TXT、Markdown、代码、JSON、
YAML 或 CSV；后续提问会返回带 `[文件名:L行号]` 的原文依据。`/forget-file` 关闭文件，
`/memory` 查看内存中的最近对话轮数，`/stream` 切换流式/整段输出，`/stats` 切换 Token
统计。会话保留最近 64 轮且退出后不落盘；历史实体会保存简体、繁体和英文规范标题，
因此可支持“Who was Napoleon?”后接“他后来怎样？”这类跨语言代词追问。

流式接口按 `response.start → response.output_text.delta → response.completed` 输出事件；完成事件
包含输入、输出、总 Token、TTFT、总耗时、吞吐和分块数。这里的真实 tokenizer 是 UTF-8 字节，
所以一个中文汉字通常是 3 Token、ASCII 字符通常是 1 Token；分块绝不会从中间拆坏 Unicode
字符。回答仍由模板、SQLite 或确定性 Python 工具先计算，再增量传输，不应误称为自回归生成。

法律查询会先识别司法辖区，目前区分中国大陆、美国联邦/美国州、中国台湾地区，
并对香港、澳门及中国大陆地方规则给出单独提示。例如可以询问“中国民法典管什么”、
“美国联邦最低工资适用什么法”或“臺灣民法的成年年齡”。未说明地区时，模型会先要求
补充适用地区；法律回答带状态、日期、官方来源和非法律意见声明。美国州法及地方条例
尚未完整收录，不能用联邦法答案代替。

法律库还包含48个可执行的办事指南。例如询问“公司不发工资怎么办”，系统会先确认工作地，
同时提示保存合同、考勤、工资条、银行流水和沟通记录，并避免伪造证据或非法取得公司
资料。补充“中国大陆深圳”“美国加州”或“台湾地区”后，会分别给出当地可用的书面催告、
投诉/劳检、调解、仲裁或诉讼路径、重要期限和官方来源。连续对话中可以先问问题，再单独
回复地区。指南只提供一般法律信息，不替代律师根据个案提供的意见。

生活法律场景不是只有欠薪，还覆盖辞退裁员、加班、工伤、消费退款、网络诈骗、租房押金
和维修、借款与催收、交通事故、家庭暴力、个人信息泄露、就业歧视与性骚扰、医疗纠纷、
版权、合同违约、离婚及子女事项。每类均区分中国大陆、美国联邦与州/城市衔接、中国台湾
地区，按照“紧急安全 → 保存证据 → 书面沟通 → 主管机关/调解/诉讼 → 期限提醒”输出。
在没有足够事实或地区时，系统只给跨地区安全步骤并追问，不猜测胜诉结果。

非法律生活场景已单独建立 `daily_life_guides` 数据表，不再把“生活能力”等同于法律咨询。
当前包含44个主题、17个分组，覆盖燃气/火灾/停电/漏水、食品与做饭、常见健康分流、睡眠与
压力、网络和设备故障、账号与手机安全、出行和证件、预算账单、学习拖延、职场沟通、儿童与
老人照护、宠物、地震洪水高温空气污染、搬家整理和快递等。回答按“现在先做 → 随后检查 →
不要这样做 → 何时升级处理”组织，并保存来源、修订时间和置信度。高风险症状只做安全分流，
不会冒充医生诊断；法律争议仍进入独立的地域化法律流程。

当前 macOS 环境的典型离线聊天 RSS 约 105.91 MiB，普通工作负载峰值约 105.95 MiB；打开接近
10 MiB 的支持上限文件并检索时，压力峰值约 158.56 MiB。最新固定回归为 1,751/1,751，独立
综合改写题为 46/46，非法律生活场景为 52/52（44个主题），独立生活法律场景为 40/40，独立
国内表达题为 21/21。实际数值会随操作
系统、Python/NumPy 版本、查询和 SQLite 页面缓存变化；题集通过率不等于开放世界绝对准确率。
标准模式的文件问答压力峰值也只占 2 GiB 的约 7.74%，无需额外的低内存运行分支。具体旧电脑
的 CPU、操作系统、交换空间和磁盘速度仍会影响实际响应速度。
