# CloudAI 第三方插件开发与安装

CloudAI 桌面版支持通过独立的 Python 插件扩展「设置 > 工具与插件」和 Agent 模式。插件不需要修改 CloudAI 源码，安装后重启应用即可生效。

## 1. 插件目录

在 CloudAI 设置中点击“打开插件目录”，将每个插件放在一个独立文件夹中。默认目录为：

| 系统 | 默认目录 |
| --- | --- |
| macOS | `~/Library/Application Support/CloudAI/plugins` |
| Windows | `%LOCALAPPDATA%/CloudAI/plugins`（没有时使用 `%APPDATA%/CloudAI/plugins`） |
| Linux / HarmonyOS Linux 环境 | `$XDG_DATA_HOME/CloudAI/plugins`，或 `~/.local/share/CloudAI/plugins` |

开发阶段也可以使用环境变量 `CLOUDAI_PLUGIN_PATH` 指定一个或多个插件根目录，多个目录使用系统路径分隔符（macOS/Linux 为 `:`, Windows 为 `;`）。应用会同时扫描默认目录和程序目录下的 `plugins` 文件夹。

## 2. 最小插件结构

```text
hello_plugin/
├── plugin.json
└── plugin.py
```

`plugin.json`：

```json
{
  "id": "hello_plugin",
  "name": "Hello Plugin",
  "version": "1.0.0",
  "api_version": 1,
  "entry": "plugin.py",
  "description": "CloudAI plugin example",
  "author": "Your Name"
}
```

`id`、`api_version` 和 `entry` 是运行时必需字段；`name` 和 `version` 用于设置页展示，缺省时会回退到插件 ID 或空版本。`id` 只能包含英文字母、数字、`.`、`_`、`-`，且必须以字母或数字开头；`api_version` 当前固定为 `1`。设置 `"enabled": false` 可以暂时禁用插件。

`plugin.py` 必须提供 `register(api)` 函数：

```python
def register(api):
    api.register_action("显示问候", show_greeting)
    api.register_tool(
        "reverse_text",
        reverse_text,
        description="Reverse a string",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"]
        }
    )


def show_greeting(arguments, context):
    context.notify("Hello from CloudAI")


def reverse_text(arguments, context):
    return {"text": str(arguments.get("text", ""))[::-1]}
```

也兼容 `create_plugin(api)` 作为入口。回调推荐使用 `(arguments, context)`；为了兼容简单插件，也接受一个参数或无参数的函数。

## 3. 两种扩展点

### 设置操作

`api.register_action(label, callback, description="")` 会在 CloudAI 设置的“第三方插件”区域生成按钮。按钮回调运行在界面线程，适合打开文件、显示结果或调用系统 API。`context.notify(message, error=False)` 会使用 CloudAI 的提示框显示信息。

### Agent 工具

`api.register_tool(name, callback, description="", parameters=schema)` 会把工具加入 Agent 的 JSON 动作协议。工具动作名自动变为 `plugin.<plugin_id>.<name>`，模型可根据插件描述调用它。每次调用都会经过 CloudAI Agent 的现有确认对话框，用户拒绝后不会执行。工具回调应返回字典、列表、字符串、数字、布尔值或 `None`，并避免直接操作 Tk 控件。

## 4. 安装、更新和卸载

1. 复制插件文件夹到默认插件目录，或复制到 `CLOUDAI_PLUGIN_PATH` 指向的目录。
2. 确认 `plugin.json` 和入口 `.py` 文件位于该插件文件夹内。
3. 完全退出并重新启动 CloudAI。
4. 打开设置，在“第三方插件”区域确认状态为“已加载”，再使用插件按钮或 Agent 模式。

更新时用新版本文件夹替换旧文件夹并重启。卸载时退出 CloudAI 后删除对应插件文件夹即可。若加载失败，设置页会显示错误摘要，详细 traceback 保存在插件文件夹中的 `cloudai-error.log`。

## 5. 安全与兼容性

插件是本地 Python 代码，会以当前用户权限运行；CloudAI 不会对第三方 Python 代码提供进程级沙箱。只安装你信任的来源，并在安装前检查源码。Agent 工具虽然会经过确认，但设置按钮是用户主动点击后执行的。

插件只能使用当前 `api_version` 提供的接口。CloudAI 会拒绝目录穿越入口、非法插件 ID、重复工具名和不兼容的 API 版本；一个插件加载失败不会阻止其他插件或主程序启动。

示例插件位于 `projects/CloudAI/plugin_examples/hello_plugin`，可直接复制到插件目录测试。
