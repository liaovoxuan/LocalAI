import os
import sys

if os.name == "nt":
    os.system("chcp 65001 > nul")
              
    try:
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import json
import time
import platform
import subprocess
import traceback
import webbrowser
from datetime import datetime

import requests
import psutil

try:
    from cpuinfo import get_cpu_info
except Exception:
    get_cpu_info = None


APP_VERSION = "0.4.4.1"

OLLAMA_GENERATE_URL = "http://localhost:11434/api/generate"
OLLAMA_TAGS_URL = "http://localhost:11434/api/tags"
OLLAMA_DOWNLOAD_URL = "https://ollama.com/download"

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
    "last_model": "",
    "update_url": "https://raw.githubusercontent.com/liaovoxuan/LocalAI/main/version.json",
    "auto_check_update": True,
    "allow_low_spec_force": True
}

MODEL_SIZES = {
    "qwen2.5:0.5b": "0.4GB",
    "qwen2.5:1.5b": "1.1GB",
    "qwen2.5:3b": "2.0GB",
    "qwen2.5:7b": "4.7GB",
    "qwen3.5:9b": "5.5GB",
    "qwen2.5:14b": "9GB"
}


def log_error(e):
    with open(os.path.join(LOG_DIR, "error.log"), "a", encoding="utf-8") as f:
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

    if not url:
        if not silent:
            print("⚠️ 未配置更新地址。")
        return

    try:
        res = requests.get(url, timeout=8)
        res.raise_for_status()
        data = res.json()

        latest = data.get("version", "")
        notes = data.get("notes", "")
        windows_url = data.get("windows_url", "")
        macos_url = data.get("macos_url", "")
        linux_url = data.get("linux_url", "")

        if latest and latest != APP_VERSION:
            print(f"\n🔔 发现新版本：{latest}")
            print(f"当前版本：{APP_VERSION}")

            if notes:
                print(f"更新内容：{notes}")

            system = platform.system()

            if system == "Windows" and windows_url:
                print(f"下载地址：{windows_url}")
            elif system == "Darwin" and macos_url:
                print(f"下载地址：{macos_url}")
            elif system == "Linux" and linux_url:
                print(f"下载地址：{linux_url}")

            choice = input("是否打开下载页面？(Y/N)：").strip().lower()
            if choice == "y":
                target = windows_url if system == "Windows" else macos_url if system == "Darwin" else linux_url
                if target:
                    webbrowser.open(target)

        elif not silent:
            print(f"✅ 当前已是最新版本：{APP_VERSION}")

    except Exception as e:
        if not silent:
            print(f"⚠️ 检查更新失败：{type(e).__name__}: {e}")


def get_cpu_name():
    if get_cpu_info:
        try:
            return get_cpu_info().get("brand_raw", "Unknown CPU")
        except Exception:
            pass

    name = platform.processor()
    return name if name else "Unknown CPU"


def detect_cpu_vendor(cpu_name):
    name = cpu_name.lower()

    if "apple" in name or platform.machine().lower() in ["arm64", "aarch64"] and platform.system() == "Darwin":
        return "Apple Silicon"

    if "intel" in name:
        return "Intel"

    if "amd" in name or "ryzen" in name or "athlon" in name:
        return "AMD"

    if "hygon" in name or "海光" in name:
        return "Hygon"

    if "zhaoxin" in name or "兆芯" in name:
        return "Zhaoxin"

    if "loongson" in name or "龙芯" in name:
        return "Loongson"

    if "phytium" in name or "飞腾" in name:
        return "Phytium"

    if "kunpeng" in name or "鲲鹏" in name:
        return "Kunpeng"

    return "Unknown"


def detect_device():
    system = platform.system()
    machine = platform.machine()
    cpu_name = get_cpu_name()
    vendor = detect_cpu_vendor(cpu_name)

    physical_cores = psutil.cpu_count(logical=False) or 0
    logical_cores = psutil.cpu_count(logical=True) or 0
    ram_gb = round(psutil.virtual_memory().total / (1024 ** 3))

    return {
        "system": system,
        "machine": machine,
        "cpu_name": cpu_name,
        "vendor": vendor,
        "physical_cores": physical_cores,
        "logical_cores": logical_cores,
        "ram_gb": ram_gb
    }


def evaluate_device(device):
    vendor = device["vendor"]
    ram = device["ram_gb"]
    cores = device["physical_cores"]
    system = device["system"]

    if cores <= 3 or ram <= 6:
        return {
            "level": "blocked",
            "model": None,
            "reason": "CPU 核心数过少或内存过低，本地模型可能严重卡顿或无法运行。"
        }

    if vendor == "Apple Silicon":
        if ram <= 8:
            return {"level": "ok", "model": "qwen2.5:3b", "reason": "Apple Silicon 8GB 可尝试 3B 模型。"}
        elif ram <= 16:
            return {"level": "good", "model": "qwen2.5:7b", "reason": "Apple Silicon 16GB 适合 7B。"}
        else:
            return {"level": "high", "model": "qwen2.5:14b", "reason": "高内存 Apple Silicon，推荐 14B，后续可尝试更大模型。如需更大模型需自行购买云端API Token。"}

    if vendor in ["Intel", "AMD", "Hygon"]:
        if ram<= 6:
            return {"level": "low", "model": "qwen2.5:0.5B", "reason": "超低内存设备，推荐0.5B。"}
        elif ram <= 8:
            return {"level": "low", "model": "qwen2.5:3b", "reason": "低内存设备，推荐3B。"}
        elif ram <= 16:
            return {"level": "ok", "model": "qwen2.5:7b", "reason": "16GB Intel/AMD 推荐 7B。"}
        elif ram <= 32:
            return {"level": "good", "model": "qwen2.5:14b", "reason": "32GB 可尝试 14B。如需更大模型（如22B）需自行购买云端API Token。"}
        else:
            return {"level": "high", "model": "qwen2.5:14b", "reason": "高性能设备，可运行大型模型。如需更大模型需自行购买云端 API Token。"}

    if vendor in ["Zhaoxin", "Phytium", "Kunpeng"]:
        if ram <= 8:
            return {"level": "low", "model": "qwen2.5:3b", "reason": "国产平台建议保守使用轻量模型。"}
        else:
            return {"level": "ok", "model": "qwen2.5:7b", "reason": "国产平台兼容性可能不同，推荐先使用 7B。"}

    if vendor == "Loongson":
        return {
            "level": "warning",
            "model": "qwen2.5:3b",
            "reason": "龙芯平台兼容性不确定，建议轻量模型或等待专门适配。"
        }

    if ram <= 8:
        return {"level": "low", "model": "qwen2.5:3b", "reason": "未知平台，低内存，推荐轻量模型。"}
    elif ram <= 16:
        return {"level": "ok", "model": "qwen2.5:7b", "reason": "未知平台，推荐 7B。"}
    else:
        return {"level": "good", "model": "qwen2.5:7b", "reason": "未知平台，建议先从 7B 开始。"}


def print_device_report(device, recommendation):
    print("\n🧠 设备检测结果：")
    print(f"系统：{device['system']}")
    print(f"架构：{device['machine']}")
    print(f"CPU：{device['cpu_name']}")
    print(f"CPU厂商：{device['vendor']}")
    print(f"物理核心：{device['physical_cores']}")
    print(f"逻辑线程：{device['logical_cores']}")
    print(f"内存：{device['ram_gb']}GB")

    print("\n模型推荐：")
    if recommendation["model"]:
        print(f"推荐模型：{recommendation['model']}")
    else:
        print("推荐模型：不建议本地运行")

    print(f"原因：{recommendation['reason']}\n")

    
def check_ollama_installed():
    try:
        res = requests.get(OLLAMA_TAGS_URL, timeout=3)
        return res.status_code == 200
    except Exception:
        return False


def get_models():
    try:
        res = requests.get(OLLAMA_TAGS_URL, timeout=5)
        res.raise_for_status()
        return res.json().get("models", [])
    except Exception:
        return []


def model_exists(model_name):
    models = get_models()
    names = [m.get("name", "") for m in models]
    return model_name in names


def pull_model(model_name):
    print(f"\n⬇️ 正在安装模型：{model_name}")
    print("这可能需要较长时间，请保持网络连接。")

    try:
        subprocess.run(["ollama", "pull", model_name], check=True)
        print("✅ 模型安装完成。")
        return True
    except FileNotFoundError:
        print("❌ 未找到 Ollama，请先安装 Ollama。")
        return False
    except subprocess.CalledProcessError:
        print("❌ 模型安装失败。")
        return False


def choose_model(config, recommendation):
    models = get_models()

    recommended_model = recommendation.get("model")

    if recommended_model and not model_exists(recommended_model):
        print(f"未检测到推荐模型：{recommended_model}")
        choice = input("是否自动安装推荐模型？(Y/N)：").strip().lower()
        if choice == "y":
            pull_model(recommended_model)

    models = get_models()

    if not models:
        model = input("未检测到模型，请手动输入模型名：").strip()
        return model or recommended_model or "qwen2.5:7b"

    print("\n可用模型：")
    for i, m in enumerate(models):
        print(f"{i + 1}. {m['name']}")

    default = config.get("last_model") or recommended_model or models[0]["name"]
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

    if any(x in name for x in ["0.5b", "0.6b", "1.5b", "1.7b", "3b"]):
        return "tiny"

    if any(x in name for x in ["7b", "8b", "9b"]):
        return "small"

    if any(x in name for x in ["10b", "11b", "12b", "13b", "14b"]):
        return "medium"

    if any(x in name for x in ["30b", "32b", "70b"]):
        return "large"

    return "small"


def get_options(size):
    if size == "tiny":
        return {"temperature": 0.25, "num_predict": 384}
    if size == "small":
        return {"temperature": 0.25, "num_predict": 512}
    if size == "medium":
        return {"temperature": 0.25, "num_predict": 768}
    if size == "large":
        return {"temperature": 0.2, "num_predict": 1024}

    return {"temperature": 0.3, "num_predict": 512}


def safe_title(text):
    title = text[:24].replace("\n", " ").replace("/", "-").replace("\\", "-").strip()
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
你是一个运行在用户本地电脑上的 AI 助手。

隐私原则：
1. AI 推理在本地运行
2. 聊天内容不上传云端
3. 无账号系统
4. 标准版不进行联网搜索

回答规则：
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


def show_help():
    print("""
可用指令：

/help         查看帮助
/new          新建会话
/list         查看会话列表
/load         加载会话
/export       导出当前会话为 Markdown
/clear        清空当前会话
/model        重新选择模型
/device       查看设备检测结果
/checkupdate  检查软件更新
/info         查看当前状态
/privacy      查看隐私说明
/multi        多行输入
/exit         退出程序
""")


def show_privacy():
    print("""
隐私说明：

1. LocalAI 标准版只调用本机 Ollama。
2. 聊天内容只保存在本机 chats 文件夹。
3. 程序不会上传聊天内容。
4. 程序只在检查更新时访问 GitHub 的 version.json。
5. 无账号系统，无云端推理。
""")


def show_info(model, size, current_path, data, device, recommendation):
    print("\n当前状态：")
    print(f"版本：{APP_VERSION}")
    print(f"模型：{model}")
    print(f"模型级别：{size}")
    print(f"当前会话：{data.get('title', '未命名')}")
    print(f"文件：{current_path}")
    print(f"消息数：{len(data.get('messages', []))}")
    print(f"设备推荐：{recommendation.get('model')}")
    print()


def main():
    print(f"""
==============================
🟢 LocalAI 标准版 v{APP_VERSION}
本地隐私 AI 助手
==============================
输入 /help 查看指令

⚠️ 特别说明：请自行确认消息真实性（AI不是人，人都会犯错，更何况AI呢）
""")

    config = load_config()

    device = detect_device()
    recommendation = evaluate_device(device)

    print_device_report(device, recommendation)

    if recommendation["level"] == "blocked":
        print("⚠️ 当前设备不推荐本地运行 AI 模型。")
        print("如果仍要继续，请输入 force。")
        choice = input("输入 force 继续，其他任意内容退出：").strip().lower()

        if choice != "force":
            print("已退出。")
            return

    if config.get("auto_check_update", True):
        check_update(config, silent=True)

    model = choose_model(config, recommendation)
    size = get_model_size(model)

    print(f"\n当前模型：{model}（{size}）")

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

        if q == "/device":
            print_device_report(device, recommendation)
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
            model = choose_model(config, recommendation)
            size = get_model_size(model)
            print(f"✅ 已切换模型：{model}（{size}）")
            continue

        if q == "/info":
            show_info(model, size, current_path, chat_data, device, recommendation)
            continue

        try:
            messages = chat_data.get("messages", [])

            if len(messages) == 0:
                title = safe_title(q)
                chat_data["title"] = title
                current_path = rename_chat_file(current_path, title)

            messages.append({"role": "user", "content": q})
            chat_data["messages"] = messages[-MAX_HISTORY_ITEMS:]
            save_chat(current_path, chat_data)

            prompt = build_prompt(q, messages)

            print("\n⏳ AI 正在思考中...")
            start_time = time.time()

            answer = ask_local(prompt, model, size)

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
            print("\n❌ 无法连接 Ollama。")
            print("请先运行：ollama serve")
            print(f"或运行：ollama run {model}\n")

        except Exception as e:
            log_error(e)
            print(f"\n❌ 出错：{type(e).__name__}: {e}")
            print("详细错误已写入 logs/error.log\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已退出")
    except Exception as e:
        log_error(e)
        print("程序发生错误，请查看 logs/error.log")
