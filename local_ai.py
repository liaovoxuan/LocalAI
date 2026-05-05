import requests
import json
import os
import time
import traceback
from datetime import datetime

APP_VERSION = "0.4.2"

OLLAMA_GENERATE_URL = "http://localhost:11434/api/generate"
OLLAMA_TAGS_URL = "http://localhost:11434/api/tags"

CHAT_DIR = "chats"
EXPORT_DIR = "exports"
LOG_DIR = "logs"
CONFIG_FILE = "config.json"

MAX_HISTORY_ITEMS = 50
CONTEXT_ITEMS = 10

os.makedirs(CHAT_DIR, exist_ok=True)
os.makedirs(EXPORT_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)


DEFAULT_CONFIG = {
    "mode": "local",
    "last_model": "",
    "cloud_api_url": "",
    "cloud_api_key": "",
    "update_url": "https://你的阿里云OSS或CDN地址/version.json",
    "auto_check_update": True
}


def log_error(e):
    path = os.path.join(LOG_DIR, "error.log")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n[{datetime.now().isoformat()}]\n")
        f.write(traceback.format_exc())
        f.write("\n")


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                config = DEFAULT_CONFIG.copy()
                config.update(data)
                return config
        except Exception:
            pass

    save_config(DEFAULT_CONFIG)
    return DEFAULT_CONFIG.copy()


def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def check_update(config, silent=False):
    url = config.get("update_url", "")

    if not url or "你的阿里云" in url:
        if not silent:
            print("⚠️ 未配置有效更新地址。")
        return

    try:
        res = requests.get(url, timeout=8)
        res.raise_for_status()
        data = res.json()

        latest = data.get("version", "")
        download = data.get("download_url", "")
        notes = data.get("notes", "")

        if latest and latest != APP_VERSION:
            print(f"\n🔔 发现新版本：{latest}")
            print(f"当前版本：{APP_VERSION}")
            if notes:
                print(f"更新内容：{notes}")
            if download:
                print(f"下载地址：{download}")
            print()
        elif not silent:
            print(f"✅ 当前已是最新版本：{APP_VERSION}")

    except Exception as e:
        if not silent:
            print(f"⚠️ 检查更新失败：{type(e).__name__}: {e}")


def get_models():
    try:
        res = requests.get(OLLAMA_TAGS_URL, timeout=5)
        res.raise_for_status()
        return res.json().get("models", [])
    except Exception:
        return []


def first_run_check():
    print("🔍 正在检测本地环境...")

    models = get_models()

    if not models:
        print("⚠️ 未检测到 Ollama 或本地模型。")
        print("请先确认 Ollama 已安装并运行。")
        print("推荐命令：")
        print("ollama run qwen2.5:7b")
        print()

    else:
        print("✅ 已检测到 Ollama 模型。")


def choose_mode(config):
    print("""
选择运行模式：
1. 本地模式（调用 Ollama）
2. 云端模式（调用服务器 API）
""")

    choice = input(f"选择模式（当前：{config.get('mode', 'local')}，默认回车保持）：").strip()

    if choice == "2":
        config["mode"] = "cloud"

        api_url = input("云端 API 地址（回车保留原值）：").strip()
        api_key = input("API Key（回车保留原值）：").strip()

        if api_url:
            config["cloud_api_url"] = api_url
        if api_key:
            config["cloud_api_key"] = api_key

    elif choice == "1":
        config["mode"] = "local"

    save_config(config)
    return config["mode"]


def choose_model(config):
    models = get_models()

    if not models:
        model = input("手动输入模型名（例如 qwen2.5:7b）：").strip()
        return model or "qwen2.5:7b"

    print("可用模型：")
    for i, m in enumerate(models):
        print(f"{i + 1}. {m['name']}")

    default = config.get("last_model") or models[0]["name"]
    choice = input(f"选择模型编号（默认：{default}）：").strip()

    if choice.isdigit():
        idx = int(choice) - 1
        if 0 <= idx < len(models):
            model = models[idx]["name"]
            config["last_model"] = model
            save_config(config)
            return model

    config["last_model"] = default
    save_config(config)
    return default


def get_model_size(model_name):
    name = model_name.lower()

    if any(x in name for x in ["7b", "8b", "9b"]):
        return "small"
    if any(x in name for x in ["10b", "11b", "12b", "13b", "14b"]):
        return "medium"
    if any(x in name for x in ["30b", "32b", "70b"]):
        return "large"

    return "small"


def get_options(size):
    if size == "small":
        return {"temperature": 0.25, "num_predict": 512}
    if size == "medium":
        return {"temperature": 0.25, "num_predict": 768}
    if size == "large":
        return {"temperature": 0.2, "num_predict": 1024}

    return {"temperature": 0.3, "num_predict": 512}


def safe_title(text):
    title = text[:24].replace("\n", " ").replace("/", "-").strip()
    return title or "新会话"


def new_chat(first_title="新会话"):
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = os.path.join(CHAT_DIR, timestamp + ".json")

    data = {
        "title": first_title,
        "created_at": datetime.now().isoformat(),
        "messages": []
    }

    save_chat(path, data)
    return path, data


def rename_chat_file(path, title):
    try:
        safe = safe_title(title)
        timestamp = os.path.basename(path).split(".")[0]
        new_name = f"{timestamp}_{safe}.json"
        new_path = os.path.join(CHAT_DIR, new_name)

        if path != new_path and not os.path.exists(new_path):
            os.rename(path, new_path)
            return new_path

    except Exception:
        pass

    return path


def list_chats():
    files = [f for f in os.listdir(CHAT_DIR) if f.endswith(".json")]
    files.sort()

    if not files:
        print("暂无会话。")
        return []

    for i, filename in enumerate(files):
        path = os.path.join(CHAT_DIR, filename)
        title = filename

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                title = data.get("title", filename)
        except Exception:
            pass

        print(f"{i + 1}. {title}  ({filename})")

    return files


def load_chat(filename):
    path = os.path.join(CHAT_DIR, filename)

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        data = {
            "title": filename,
            "created_at": datetime.now().isoformat(),
            "messages": data
        }

    return path, data


def save_chat(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def export_md(path, data):
    base = os.path.basename(path).replace(".json", ".md")
    export_path = os.path.join(EXPORT_DIR, base)

    with open(export_path, "w", encoding="utf-8") as f:
        f.write(f"# {data.get('title', '未命名会话')}\n\n")
        f.write(f"- 导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

        for item in data.get("messages", []):
            if item["role"] == "user":
                f.write(f"## 👤 用户\n{item['content']}\n\n")
            elif item["role"] == "ai":
                f.write(f"## 🤖 AI\n{item['content']}\n\n")
            else:
                f.write(f"## ⚠️ 系统\n{item['content']}\n\n")

    print(f"📄 已导出：{export_path}")


def multiline_input():
    line = input("👉 你：").rstrip()

    if line != "/multi":
        return line

    print("进入多行模式，空行结束：")
    lines = []

    while True:
        current = input()
        if current == "":
            break
        lines.append(current)

    return "\n".join(lines).strip()


def build_prompt(question, messages):
    recent = messages[-CONTEXT_ITEMS:]

    history_text = ""
    for item in recent:
        role = "用户" if item["role"] == "user" else "助手"
        history_text += f"{role}：{item['content']}\n"

    return f"""
你是一个运行在本地/云端的 AI 助手。

规则：
1. 用简洁中文回答
2. 不确定就说不确定
3. 不要编造事实
4. 不要输出思考过程
5. 必须参考历史对话，但不要机械重复

【历史对话】
{history_text}

【当前问题】
{question}

请回答：
"""


def ask_local(prompt, model, size):
    data = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": get_options(size)
    }

    response = requests.post(
        OLLAMA_GENERATE_URL,
        json=data,
        timeout=180
    )

    response.raise_for_status()
    return response.json().get("response", "").strip()


def ask_cloud(prompt, config):
    api_url = config.get("cloud_api_url", "")
    api_key = config.get("cloud_api_key", "")

    if not api_url:
        return "❌ 未配置云端 API 地址。"

    headers = {"Content-Type": "application/json"}

    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {"prompt": prompt}

    response = requests.post(
        api_url,
        headers=headers,
        json=payload,
        timeout=180
    )

    response.raise_for_status()
    data = response.json()

    return (
        data.get("answer")
        or data.get("response")
        or data.get("content")
        or "⚠️ 云端返回为空。"
    )


def ask_ai(prompt, mode, model, size, config):
    if mode == "cloud":
        return ask_cloud(prompt, config)
    return ask_local(prompt, model, size)


def show_help():
    print("""
可用指令：

/help         查看帮助
/new          新建会话
/list         查看会话列表
/load         加载会话
/export       导出当前会话为 Markdown
/clear        清空当前会话
/model        重新选择本地模型
/mode         切换本地/云端模式
/checkupdate  检查软件更新
/info         查看当前状态
/privacy      查看隐私说明
/multi        多行输入
/exit         退出程序
""")


def show_privacy():
    print("""
隐私说明：

1. 本地模式下：
   - 问题只发送到本机 Ollama 服务
   - 聊天记录保存在本机 chats 文件夹
   - 不会上传到云端

2. 云端模式下：
   - 问题会发送到你配置的 API 地址
   - 请确认该 API 的隐私政策

3. 标准版不会自动联网搜索。
""")


def show_info(mode, model, size, current_path, data, config):
    print("\n当前状态：")
    print(f"版本：{APP_VERSION}")
    print(f"模式：{mode}")

    if mode == "local":
        print(f"本地模型：{model}")
        print(f"模型级别：{size}")
    else:
        print(f"云端 API：{config.get('cloud_api_url') or '未配置'}")

    print(f"当前会话：{data.get('title', '未命名')}")
    print(f"文件：{current_path}")
    print(f"消息数：{len(data.get('messages', []))}")
    print()


def main():
    print(f"""
==============================
🟢 Local AI 标准版 v{APP_VERSION}
本地 / 云端双模式
==============================
输入 /help 查看指令
""")

    config = load_config()

    first_run_check()

    if config.get("auto_check_update", True):
        check_update(config, silent=True)

    mode = choose_mode(config)

    model = ""
    size = "small"

    if mode == "local":
        model = choose_model(config)
        size = get_model_size(model)
        print(f"\n当前模式：本地")
        print(f"当前模型：{model}（{size}）")
    else:
        print("\n当前模式：云端")

    current_path, chat_data = new_chat()

    while True:
        print(f"\n📌 当前会话：{chat_data.get('title', '未命名')}")
        q = multiline_input()

        if not q:
            continue

        if q == "/exit":
            print("已退出。")
            break

        if q == "/help":
            show_help()
            continue

        if q == "/privacy":
            show_privacy()
            continue

        if q == "/checkupdate":
            check_update(config)
            continue

        if q == "/new":
            current_path, chat_data = new_chat()
            print("🆕 新会话已创建")
            continue

        if q == "/list":
            list_chats()
            continue

        if q == "/load":
            files = list_chats()
            if not files:
                continue

            idx = input("选择编号：").strip()
            if idx.isdigit():
                i = int(idx) - 1
                if 0 <= i < len(files):
                    current_path, chat_data = load_chat(files[i])
                    print(f"📂 已加载：{chat_data.get('title', files[i])}")
                else:
                    print("编号无效。")
            continue

        if q == "/export":
            export_md(current_path, chat_data)
            continue

        if q == "/clear":
            chat_data["messages"] = []
            save_chat(current_path, chat_data)
            print("🧹 当前会话已清空")
            continue

        if q == "/model":
            if mode != "local":
                print("当前是云端模式，无法选择本地模型。")
            else:
                model = choose_model(config)
                size = get_model_size(model)
                print(f"✅ 已切换模型：{model}（{size}）")
            continue

        if q == "/mode":
            mode = choose_mode(config)
            if mode == "local":
                model = choose_model(config)
                size = get_model_size(model)
                print(f"✅ 已切换到本地模式：{model}（{size}）")
            else:
                print("✅ 已切换到云端模式")
            continue

        if q == "/info":
            show_info(mode, model, size, current_path, chat_data, config)
            continue

        try:
            messages = chat_data.get("messages", [])

            if len(messages) == 0:
                title = safe_title(q)
                chat_data["title"] = title
                current_path = rename_chat_file(current_path, title)

            # 崩溃保护：先保存用户输入
            messages.append({"role": "user", "content": q})
            chat_data["messages"] = messages[-MAX_HISTORY_ITEMS:]
            save_chat(current_path, chat_data)

            prompt = build_prompt(q, messages)

            print("\n⏳ AI 正在思考中...")
            start_time = time.time()

            answer = ask_ai(prompt, mode, model, size, config)

            elapsed = time.time() - start_time

            if not answer:
                answer = "⚠️ 未获取到有效回答。"

            print(f"\n🤖 AI（{elapsed:.1f}s）：")
            print(answer)
            print("-" * 50)

            messages.append({"role": "ai", "content": answer})
            chat_data["messages"] = messages[-MAX_HISTORY_ITEMS:]
            save_chat(current_path, chat_data)

        except requests.exceptions.ConnectionError:
            msg = "❌ 无法连接 Ollama。" if mode == "local" else "❌ 无法连接云端 API。"
            print(f"\n{msg}\n")

        except Exception as e:
            log_error(e)
            print(f"\n❌ 出错：{type(e).__name__}: {e}")
            print("详细错误已写入 logs/error.log\n")


if __name__ == "__main__":
    main()
