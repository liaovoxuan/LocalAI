import os
import sys
import base64
import zipfile
import tempfile
import shutil
import shlex
import xml.etree.ElementTree as ET

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
import re
from html import unescape
from urllib.parse import quote, unquote, urljoin, urlparse
from datetime import datetime

import requests
import psutil

get_cpu_info = None
_CPUINFO_LOADED = False


APP_VERSION = "0.8 Beta"
APP_VERSION_LABEL = f"{APP_VERSION} SE"

OLLAMA_GENERATE_URL = "http://localhost:11434/api/generate"
OLLAMA_CHAT_URL = "http://localhost:11434/api/chat"
OLLAMA_TAGS_URL = "http://localhost:11434/api/tags"
OLLAMA_DOWNLOAD_URL = "https://ollama.com/download"

APP_NAME = "LocalAI"


def version_tuple(value):
    parts = []
    for item in re.findall(r"\d+", str(value or "")):
        try:
            parts.append(int(item))
        except Exception:
            break
    return tuple(parts)


def read_linux_os_release():
    data = {}
    if platform.system() != "Linux":
        return data
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    if "=" not in line:
                        continue
                    key, value = line.rstrip("\n").split("=", 1)
                    data[key] = value.strip().strip('"')
                if data:
                    return data
        except Exception:
            continue
    return data


def get_os_optimization_profile():
    system = platform.system()
    machine = platform.machine().lower()
    profile = {
        "system": system,
        "targeted": False,
        "name": system or "Unknown",
        "family": system.lower() or "unknown",
        "ui_scale_bias": 1.0,
        "scroll_units": 1,
        "font": "Helvetica",
        "ollama_extra_paths": [],
    }

    if system == "Darwin":
        mac_version = platform.mac_ver()[0] or ""
        major = version_tuple(mac_version)[:1]
        profile.update({
            "name": f"macOS {mac_version}" if mac_version else "macOS",
            "family": "macos",
            "targeted": bool(major and major[0] >= 14),
            "ui_scale_bias": 1.02,
            "scroll_units": 1,
            "font": "SF Pro Text",
            "ollama_extra_paths": [
                "/opt/homebrew/bin/ollama",
                "/usr/local/bin/ollama",
                os.path.expanduser("~/Applications/Ollama.app/Contents/Resources/ollama"),
                "/Applications/Ollama.app/Contents/Resources/ollama",
            ],
        })
        return profile

    if system == "Windows":
        release = platform.release()
        version = platform.version()
        build = version_tuple(version)
        build_number = build[-1] if build else 0
        profile.update({
            "name": f"Windows {release} build {build_number}" if build_number else f"Windows {release}",
            "family": "windows",
            "targeted": release in {"10", "11"} and (build_number >= 19045 or release == "11"),
            "ui_scale_bias": 1.0,
            "scroll_units": 3,
            "font": "Segoe UI",
            "ollama_extra_paths": [
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Ollama", "ollama.exe"),
                os.path.join(os.environ.get("ProgramFiles", ""), "Ollama", "ollama.exe"),
                os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Ollama", "ollama.exe"),
            ],
        })
        return profile

    if system == "Linux":
        info = read_linux_os_release()
        distro_id = (info.get("ID") or "").lower()
        like = (info.get("ID_LIKE") or "").lower()
        version_id = info.get("VERSION_ID") or ""
        version = version_tuple(version_id)
        domestic_ids = {"uos", "deepin", "kylin", "openkylin", "uniontech", "loongnix", "neokylin", "asianux"}
        is_domestic = distro_id in domestic_ids or any(item in like for item in domestic_ids)
        is_ubuntu = distro_id == "ubuntu" and version >= (22, 4)
        is_debian = distro_id == "debian" and version >= (12,)
        is_fedora = distro_id == "fedora" and version >= (40,)
        profile.update({
            "name": info.get("PRETTY_NAME") or "Linux",
            "family": "linux-cn" if is_domestic else "linux",
            "targeted": is_domestic or is_ubuntu or is_debian or is_fedora,
            "ui_scale_bias": 1.04 if is_domestic else 1.0,
            "scroll_units": 3,
            "font": "Noto Sans CJK SC" if is_domestic else "Noto Sans",
            "ollama_extra_paths": [
                "/usr/local/bin/ollama",
                "/usr/bin/ollama",
                "/snap/bin/ollama",
                os.path.expanduser("~/.local/bin/ollama"),
                "/var/lib/flatpak/exports/bin/ollama",
                os.path.expanduser("~/.local/share/flatpak/exports/bin/ollama"),
            ],
        })
        if "ubuntu" in like and version >= (22, 4):
            profile["targeted"] = True
        if "debian" in like and version >= (12,):
            profile["targeted"] = True
        return profile

    return profile


def configure_runtime_environment():
    profile = get_os_optimization_profile()
    if profile["family"] == "macos":
        os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")
    elif profile["family"] == "windows":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                import ctypes
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    elif profile["family"].startswith("linux"):
        os.environ.setdefault("GDK_SCALE", os.environ.get("GDK_SCALE", "1"))
        os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    return profile


OS_OPTIMIZATION_PROFILE = configure_runtime_environment()


def get_ui_scale_bias():
    return OS_OPTIMIZATION_PROFILE.get("ui_scale_bias", 1.0) if OS_OPTIMIZATION_PROFILE.get("targeted") else 1.0


def get_scroll_units(default=1):
    return OS_OPTIMIZATION_PROFILE.get("scroll_units", default) if OS_OPTIMIZATION_PROFILE.get("targeted") else default


def get_platform_font(default="Helvetica"):
    return OS_OPTIMIZATION_PROFILE.get("font", default) if OS_OPTIMIZATION_PROFILE.get("targeted") else default


def get_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)

    return os.path.dirname(os.path.abspath(__file__))


def resource_path(*parts):
    candidates = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(bundle_root)
    candidates.append(get_base_dir())
    candidates.append(os.path.dirname(os.path.abspath(__file__)))
    for root in candidates:
        path = os.path.join(root, *parts)
        if os.path.exists(path):
            return path
    return os.path.join(candidates[0], *parts) if candidates else os.path.join(*parts)


LOCALAI_POLICY_DOC = "Readme.docx"


def read_docx_preview(path, max_chars=1600):
    if not path or not os.path.exists(path):
        return ""
    try:
        with zipfile.ZipFile(path) as archive:
            xml_data = archive.read("word/document.xml")
        root = ET.fromstring(xml_data)
        namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paragraphs = []
        for paragraph in root.findall(".//w:p", namespace):
            parts = [node.text or "" for node in paragraph.findall(".//w:t", namespace)]
            text = "".join(parts).strip()
            if text:
                paragraphs.append(text)
            if len("\n".join(paragraphs)) >= max_chars:
                break
        return "\n\n".join(paragraphs)[:max_chars]
    except Exception:
        return ""


def get_app_data_dir():
    if os.environ.get("LOCALAI_PORTABLE") == "1":
        return get_base_dir()

    system = platform.system()

    if system == "Windows":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if root:
            return os.path.join(root, APP_NAME)

    if system == "Darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", APP_NAME)

    root = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(root, APP_NAME)


APP_DATA_DIR = get_app_data_dir()
CHAT_DIR = os.path.join(APP_DATA_DIR, "chats")
EXPORT_DIR = os.path.join(APP_DATA_DIR, "exports")
LOG_DIR = os.path.join(APP_DATA_DIR, "logs")
CONFIG_FILE = os.path.join(APP_DATA_DIR, "config.json")

MAX_HISTORY_ITEMS = 50
CONTEXT_ITEMS = 10

def ensure_app_dirs():
    os.makedirs(CHAT_DIR, exist_ok=True)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)


DEFAULT_CONFIG = {
    "last_model": "",
    "lmstudio_base_url": "http://localhost:1234/v1",
    "openai_model": "",
    "api_key": "",
    "api_base_url": "",
    "provider": "ollama",
    "first_run_done": False,
    "first_welcome_done": False,
    "se_tutorial_done": False,
    "update_url": "https://raw.githubusercontent.com/liaovoxuan/LocalAI/main/version.json",
    "auto_check_update": True,
    "allow_low_spec_force": True,
    "language": "zh_cn",
    "theme": "auto",
    "first_device_info_done": False,
    "wallpaper_path": ""
}

APP_EDITION = "se"
SUPPORTED_PROVIDERS = ('ollama',)
WEB_FEATURES = {
    "basic_web_search": True,
    "auto_source_limit": 2,
    "full_page_read": "limited",
    "deep_research": False,
    "parallel_agents": False,
    "scheduled_web_tasks": False,
    "custom_search_engine": False,
    "citation_management": "none",
    "enterprise_gateway": False,
}

ACTIVE_CONFIG = None
DEVICE_CACHE = None

LANGUAGE_OPTIONS = {
    "zh_cn": {
        "name": "简体中文",
        "model_language": "Simplified Chinese",
        "user_role": "用户",
        "assistant_role": "助手",
    },
    "zh_tw": {
        "name": "繁體中文",
        "model_language": "Traditional Chinese as used in Taiwan or Hong Kong",
        "user_role": "使用者",
        "assistant_role": "助理",
    },
    "en_us": {
        "name": "English (United States)",
        "model_language": "American English, using US spelling and phrasing",
        "user_role": "User",
        "assistant_role": "Assistant",
    },
    "en_gb": {
        "name": "English (United Kingdom)",
        "model_language": "British English, using UK spelling and phrasing",
        "user_role": "User",
        "assistant_role": "Assistant",
    },
    "ja": {
        "name": "日本語",
        "model_language": "Japanese",
        "user_role": "ユーザー",
        "assistant_role": "アシスタント",
    },
    "fr": {
        "name": "Français",
        "model_language": "French",
        "user_role": "Utilisateur",
        "assistant_role": "Assistant",
    },
    "de": {
        "name": "Deutsch",
        "model_language": "German",
        "user_role": "Benutzer",
        "assistant_role": "Assistent",
    },
}

LANGUAGE_ALIASES = {
    "zh": "zh_cn",
    "cn": "zh_cn",
    "zh-cn": "zh_cn",
    "zh-hans": "zh_cn",
    "zh_cn": "zh_cn",
    "zh-tw": "zh_tw",
    "zh-hant": "zh_tw",
    "tw": "zh_tw",
    "en": "en_us",
    "en-us": "en_us",
    "en_us": "en_us",
    "en-gb": "en_gb",
    "en-uk": "en_gb",
    "en_uk": "en_gb",
    "ja": "ja",
    "jp": "ja",
    "fr": "fr",
    "de": "de",
}

ADDITIONAL_LANGUAGE_OPTIONS = {
    "en_au": {"name": "English (Australia)", "model_language": "Australian English, using Australian spelling and phrasing", "user_role": "User", "assistant_role": "Assistant"},
    "ko": {"name": "한국어", "model_language": "Korean", "user_role": "사용자", "assistant_role": "어시스턴트"},
    "es": {"name": "Español", "model_language": "Spanish", "user_role": "Usuario", "assistant_role": "Asistente"},
    "it": {"name": "Italiano", "model_language": "Italian", "user_role": "Utente", "assistant_role": "Assistente"},
    "pt": {"name": "Português", "model_language": "Portuguese", "user_role": "Usuário", "assistant_role": "Assistente"},
    "ru": {"name": "Русский", "model_language": "Russian", "user_role": "Пользователь", "assistant_role": "Ассистент"},
    "nl": {"name": "Nederlands", "model_language": "Dutch", "user_role": "Gebruiker", "assistant_role": "Assistent"},
    "sv": {"name": "Svenska", "model_language": "Swedish", "user_role": "Användare", "assistant_role": "Assistent"},
    "da": {"name": "Dansk", "model_language": "Danish", "user_role": "Bruger", "assistant_role": "Assistent"},
    "fi": {"name": "Suomi", "model_language": "Finnish", "user_role": "Käyttäjä", "assistant_role": "Avustaja"},
    "no": {"name": "Norsk", "model_language": "Norwegian", "user_role": "Bruker", "assistant_role": "Assistent"},
    "tr": {"name": "Türkçe", "model_language": "Turkish", "user_role": "Kullanıcı", "assistant_role": "Asistan"},
    "pl": {"name": "Polski", "model_language": "Polish", "user_role": "Użytkownik", "assistant_role": "Asystent"},
    "cs": {"name": "Čeština", "model_language": "Czech", "user_role": "Uživatel", "assistant_role": "Asistent"},
    "uk": {"name": "Українська", "model_language": "Ukrainian", "user_role": "Користувач", "assistant_role": "Асистент"},
    "el": {"name": "Ελληνικά", "model_language": "Greek", "user_role": "Χρήστης", "assistant_role": "Βοηθός"},
    "ar": {"name": "العربية", "model_language": "Arabic", "user_role": "المستخدم", "assistant_role": "المساعد"},
    "mn": {"name": "Монгол", "model_language": "Mongolian", "user_role": "Хэрэглэгч", "assistant_role": "Туслах"},
    "th": {"name": "ไทย", "model_language": "Thai", "user_role": "ผู้ใช้", "assistant_role": "ผู้ช่วย"},
    "vi": {"name": "Tiếng Việt", "model_language": "Vietnamese", "user_role": "Người dùng", "assistant_role": "Trợ lý"},
    "id": {"name": "Bahasa Indonesia", "model_language": "Indonesian", "user_role": "Pengguna", "assistant_role": "Asisten"},
    "ms": {"name": "Bahasa Melayu", "model_language": "Malay", "user_role": "Pengguna", "assistant_role": "Pembantu"},
    "hi": {"name": "हिन्दी", "model_language": "Hindi", "user_role": "उपयोगकर्ता", "assistant_role": "सहायक"},
}
LANGUAGE_OPTIONS.update(ADDITIONAL_LANGUAGE_OPTIONS)
LANGUAGE_ALIASES.update({
    "en-au": "en_au", "en_au": "en_au", "en-aus": "en_au", "au": "en_au",
    "ko": "ko", "kr": "ko", "ko-kr": "ko",
    "es": "es", "es-es": "es", "spanish": "es",
    "it": "it", "it-it": "it",
    "pt": "pt", "pt-br": "pt", "pt-pt": "pt",
    "ru": "ru", "ru-ru": "ru",
    "nl": "nl", "nl-nl": "nl", "dutch": "nl",
    "sv": "sv", "sv-se": "sv",
    "da": "da", "da-dk": "da",
    "fi": "fi", "fi-fi": "fi",
    "no": "no", "nb": "no", "nn": "no", "nb-no": "no",
    "tr": "tr", "tr-tr": "tr",
    "pl": "pl", "pl-pl": "pl",
    "cs": "cs", "cz": "cs", "cs-cz": "cs",
    "uk": "uk", "uk-ua": "uk",
    "el": "el", "el-gr": "el", "gr": "el",
    "ar": "ar", "ar-sa": "ar",
    "mn": "mn", "mn-mn": "mn",
    "th": "th", "th-th": "th",
    "vi": "vi", "vi-vn": "vi",
    "id": "id", "id-id": "id",
    "ms": "ms", "ms-my": "ms",
    "hi": "hi", "hi-in": "hi",
})


UI_TEXT = {
    "zh_cn": {
        "help_hint": "输入 /help 查看指令",
        "input_prompt": "👉 你：",
        "thinking": "\n⏳ AI 正在思考中...",
        "empty_answer": "⚠️ 未获取到有效回答。",
        "current_chat": "\n📌 当前会话：{title}",
        "current_model": "\n当前模型：{model}（{size}）",
        "goodbye": "已退出。",
        "device_title": "\n🧠 设备检测结果：",
        "system": "系统",
        "arch": "架构",
        "cpu": "CPU",
        "cpu_vendor": "CPU 厂商",
        "physical_cores": "物理核心",
        "logical_threads": "逻辑线程",
        "memory": "内存",
        "model_recommendation": "\n模型推荐：",
        "recommended_model": "推荐模型",
        "not_recommended": "不建议本地运行",
        "reason": "原因",
        "commands": "可用指令",
        "privacy_title": "隐私说明",
        "status_title": "\n当前状态：",
        "version": "版本",
        "model": "模型",
        "model_level": "模型级别",
        "file": "文件",
        "message_count": "消息数",
        "device_recommendation": "设备推荐",
        "language_changed": "✅ 语言已切换为：{name}",
        "ollama_connection_error": "\n❌ 无法连接 Ollama。",
        "run_ollama": "请先运行：ollama serve",
        "or_run_model": "或运行：ollama run {model}",
        "error_written": "详细错误已写入 logs/error.log\n",
    },
    "zh_tw": {
        "help_hint": "輸入 /help 查看指令",
        "input_prompt": "👉 你：",
        "thinking": "\n⏳ AI 正在思考中...",
        "empty_answer": "⚠️ 未取得有效回答。",
        "current_chat": "\n📌 目前會話：{title}",
        "current_model": "\n目前模型：{model}（{size}）",
        "goodbye": "已結束。",
        "device_title": "\n🧠 裝置偵測結果：",
        "system": "系統",
        "arch": "架構",
        "cpu": "CPU",
        "cpu_vendor": "CPU 廠商",
        "physical_cores": "實體核心",
        "logical_threads": "邏輯執行緒",
        "memory": "記憶體",
        "model_recommendation": "\n模型建議：",
        "recommended_model": "建議模型",
        "not_recommended": "不建議本機執行",
        "reason": "原因",
        "commands": "可用指令",
        "privacy_title": "隱私說明",
        "status_title": "\n目前狀態：",
        "version": "版本",
        "model": "模型",
        "model_level": "模型級別",
        "file": "檔案",
        "message_count": "訊息數",
        "device_recommendation": "裝置建議",
        "language_changed": "✅ 語言已切換為：{name}",
        "ollama_connection_error": "\n❌ 無法連線到 Ollama。",
        "run_ollama": "請先執行：ollama serve",
        "or_run_model": "或執行：ollama run {model}",
        "error_written": "詳細錯誤已寫入 logs/error.log\n",
    },
    "en_us": {
        "help_hint": "Type /help for commands",
        "input_prompt": "👉 You: ",
        "thinking": "\n⏳ AI is thinking...",
        "empty_answer": "⚠️ No valid answer received.",
        "current_chat": "\n📌 Current chat: {title}",
        "current_model": "\nCurrent model: {model} ({size})",
        "goodbye": "Exited.",
        "device_title": "\n🧠 Device report:",
        "system": "System",
        "arch": "Architecture",
        "cpu": "CPU",
        "cpu_vendor": "CPU vendor",
        "physical_cores": "Physical cores",
        "logical_threads": "Logical threads",
        "memory": "Memory",
        "model_recommendation": "\nModel recommendation:",
        "recommended_model": "Recommended model",
        "not_recommended": "Not recommended for local use",
        "reason": "Reason",
        "commands": "Available commands",
        "privacy_title": "Privacy notice",
        "status_title": "\nCurrent status:",
        "version": "Version",
        "model": "Model",
        "model_level": "Model level",
        "file": "File",
        "message_count": "Message count",
        "device_recommendation": "Device recommendation",
        "language_changed": "✅ Language changed to: {name}",
        "ollama_connection_error": "\n❌ Cannot connect to Ollama.",
        "run_ollama": "Run this first: ollama serve",
        "or_run_model": "Or run: ollama run {model}",
        "error_written": "Details were written to logs/error.log\n",
    },
    "en_gb": {
        "help_hint": "Type /help for commands",
        "input_prompt": "👉 You: ",
        "thinking": "\n⏳ AI is thinking...",
        "empty_answer": "⚠️ No valid answer received.",
        "current_chat": "\n📌 Current chat: {title}",
        "current_model": "\nCurrent model: {model} ({size})",
        "goodbye": "Exited.",
        "device_title": "\n🧠 Device report:",
        "system": "System",
        "arch": "Architecture",
        "cpu": "CPU",
        "cpu_vendor": "CPU vendor",
        "physical_cores": "Physical cores",
        "logical_threads": "Logical threads",
        "memory": "Memory",
        "model_recommendation": "\nModel recommendation:",
        "recommended_model": "Recommended model",
        "not_recommended": "Not recommended for local use",
        "reason": "Reason",
        "commands": "Available commands",
        "privacy_title": "Privacy notice",
        "status_title": "\nCurrent status:",
        "version": "Version",
        "model": "Model",
        "model_level": "Model level",
        "file": "File",
        "message_count": "Message count",
        "device_recommendation": "Device recommendation",
        "language_changed": "✅ Language changed to: {name}",
        "ollama_connection_error": "\n❌ Cannot connect to Ollama.",
        "run_ollama": "Run this first: ollama serve",
        "or_run_model": "Or run: ollama run {model}",
        "error_written": "Details were written to logs/error.log\n",
    },
}

UI_TEXT["ja"] = {
    "help_hint": "/help でコマンドを表示",
    "input_prompt": "👉 あなた：",
    "thinking": "\n⏳ AI が考えています...",
    "empty_answer": "⚠️ 有効な回答を取得できませんでした。",
    "current_chat": "\n📌 現在のチャット：{title}",
    "current_model": "\n現在のモデル：{model}（{size}）",
    "goodbye": "終了しました。",
    "device_title": "\n🧠 デバイス検出結果：",
    "system": "システム",
    "arch": "アーキテクチャ",
    "cpu": "CPU",
    "cpu_vendor": "CPU ベンダー",
    "physical_cores": "物理コア",
    "logical_threads": "論理スレッド",
    "memory": "メモリ",
    "model_recommendation": "\nモデル推奨：",
    "recommended_model": "推奨モデル",
    "not_recommended": "ローカル実行は推奨されません",
    "reason": "理由",
    "commands": "利用可能なコマンド",
    "privacy_title": "プライバシーについて",
    "status_title": "\n現在の状態：",
    "version": "バージョン",
    "model": "モデル",
    "model_level": "モデルレベル",
    "file": "ファイル",
    "message_count": "メッセージ数",
    "device_recommendation": "デバイス推奨",
    "language_changed": "✅ 言語を変更しました：{name}",
    "ollama_connection_error": "\n❌ Ollama に接続できません。",
    "run_ollama": "先に実行してください：ollama serve",
    "or_run_model": "または実行：ollama run {model}",
    "error_written": "詳細なエラーは logs/error.log に書き込まれました\n",
}

UI_TEXT["fr"] = {
    "help_hint": "Tapez /help pour afficher les commandes",
    "input_prompt": "👉 Vous : ",
    "thinking": "\n⏳ L'IA réfléchit...",
    "empty_answer": "⚠️ Aucune réponse valide reçue.",
    "current_chat": "\n📌 Conversation actuelle : {title}",
    "current_model": "\nModèle actuel : {model} ({size})",
    "goodbye": "Fermé.",
    "device_title": "\n🧠 Rapport de l'appareil :",
    "system": "Système",
    "arch": "Architecture",
    "cpu": "CPU",
    "cpu_vendor": "Fabricant du CPU",
    "physical_cores": "Coeurs physiques",
    "logical_threads": "Threads logiques",
    "memory": "Mémoire",
    "model_recommendation": "\nRecommandation de modèle :",
    "recommended_model": "Modèle recommandé",
    "not_recommended": "Non recommandé en local",
    "reason": "Raison",
    "commands": "Commandes disponibles",
    "privacy_title": "Confidentialité",
    "status_title": "\nÉtat actuel :",
    "version": "Version",
    "model": "Modèle",
    "model_level": "Niveau du modèle",
    "file": "Fichier",
    "message_count": "Nombre de messages",
    "device_recommendation": "Recommandation de l'appareil",
    "language_changed": "✅ Langue changée : {name}",
    "ollama_connection_error": "\n❌ Impossible de se connecter à Ollama.",
    "run_ollama": "Lancez d'abord : ollama serve",
    "or_run_model": "Ou lancez : ollama run {model}",
    "error_written": "Les détails ont été écrits dans logs/error.log\n",
}

UI_TEXT["de"] = {
    "help_hint": "Geben Sie /help ein, um Befehle anzuzeigen",
    "input_prompt": "👉 Sie: ",
    "thinking": "\n⏳ KI denkt nach...",
    "empty_answer": "⚠️ Keine gültige Antwort erhalten.",
    "current_chat": "\n📌 Aktueller Chat: {title}",
    "current_model": "\nAktuelles Modell: {model} ({size})",
    "goodbye": "Beendet.",
    "device_title": "\n🧠 Gerätebericht:",
    "system": "System",
    "arch": "Architektur",
    "cpu": "CPU",
    "cpu_vendor": "CPU-Hersteller",
    "physical_cores": "Physische Kerne",
    "logical_threads": "Logische Threads",
    "memory": "Arbeitsspeicher",
    "model_recommendation": "\nModell-Empfehlung:",
    "recommended_model": "Empfohlenes Modell",
    "not_recommended": "Für lokale Nutzung nicht empfohlen",
    "reason": "Grund",
    "commands": "Verfügbare Befehle",
    "privacy_title": "Datenschutzhinweis",
    "status_title": "\nAktueller Status:",
    "version": "Version",
    "model": "Modell",
    "model_level": "Modellstufe",
    "file": "Datei",
    "message_count": "Nachrichtenanzahl",
    "device_recommendation": "Geräteempfehlung",
    "language_changed": "✅ Sprache geändert zu: {name}",
    "ollama_connection_error": "\n❌ Verbindung zu Ollama nicht möglich.",
    "run_ollama": "Führen Sie zuerst aus: ollama serve",
    "or_run_model": "Oder führen Sie aus: ollama run {model}",
    "error_written": "Details wurden in logs/error.log geschrieben\n",
}

COMMAND_HELP = {
    "zh_cn": [
        ("/help", "查看帮助"),
        ("/new", "新建会话"),
        ("/list", "查看会话列表"),
        ("/history", "查看历史会话"),
        ("/open", "打开历史会话"),
        ("/load", "加载会话"),
        ("/rename", "重命名当前会话"),
        ("/export", "导出当前会话为 Markdown"),
        ("/clear", "清空当前会话"),
        ("/model", "重新选择模型"),
        ("/search", "联网搜索（标准版不可用）"),
        ("/device", "查看设备检测结果"),
        ("/snaphelp", "在首次教学中保存指令截图到桌面"),
        ("/checkupdate", "检查软件更新"),
        ("/info", "查看当前状态"),
        ("/privacy", "查看隐私说明"),
        ("/language", "切换语言"),
        ("/multi", "多行输入"),
        ("/exit", "退出程序"),
    ],
    "zh_tw": [
        ("/help", "查看說明"),
        ("/new", "建立新會話"),
        ("/list", "查看會話列表"),
        ("/history", "查看歷史會話"),
        ("/open", "開啟歷史會話"),
        ("/load", "載入會話"),
        ("/rename", "重新命名目前會話"),
        ("/export", "將目前會話匯出為 Markdown"),
        ("/clear", "清空目前會話"),
        ("/model", "重新選擇模型"),
        ("/search", "網路搜尋（標準版不可用）"),
        ("/device", "查看裝置偵測結果"),
        ("/snaphelp", "在首次教學中儲存指令截圖到桌面"),
        ("/checkupdate", "檢查軟體更新"),
        ("/info", "查看目前狀態"),
        ("/privacy", "查看隱私說明"),
        ("/language", "切換語言"),
        ("/multi", "多行輸入"),
        ("/exit", "結束程式"),
    ],
    "en_us": [
        ("/help", "Show help"),
        ("/new", "Start a new chat"),
        ("/list", "List chats"),
        ("/history", "List chat history"),
        ("/open", "Open a chat from history"),
        ("/load", "Load a chat"),
        ("/rename", "Rename the current chat"),
        ("/export", "Export the current chat as Markdown"),
        ("/clear", "Clear the current chat"),
        ("/model", "Choose another model"),
        ("/search", "Web search (not available in Standard edition)"),
        ("/device", "Show device report"),
        ("/snaphelp", "Save a command screenshot to Desktop during the first tutorial"),
        ("/checkupdate", "Check for updates"),
        ("/info", "Show current status"),
        ("/privacy", "Show privacy notice"),
        ("/language", "Change language"),
        ("/multi", "Enter multiline input"),
        ("/exit", "Exit"),
    ],
    "en_gb": [
        ("/help", "Show help"),
        ("/new", "Start a new chat"),
        ("/list", "List chats"),
        ("/history", "List chat history"),
        ("/open", "Open a chat from history"),
        ("/load", "Load a chat"),
        ("/rename", "Rename the current chat"),
        ("/export", "Export the current chat as Markdown"),
        ("/clear", "Clear the current chat"),
        ("/model", "Choose another model"),
        ("/search", "Web search (not available in Standard edition)"),
        ("/device", "Show device report"),
        ("/snaphelp", "Save a command screenshot to Desktop during the first tutorial"),
        ("/checkupdate", "Check for updates"),
        ("/info", "Show current status"),
        ("/privacy", "Show privacy notice"),
        ("/language", "Change language"),
        ("/multi", "Enter multiline input"),
        ("/exit", "Exit"),
    ],
    "ja": [
        ("/help", "ヘルプを表示"),
        ("/new", "新しいチャットを開始"),
        ("/list", "チャット一覧を表示"),
        ("/history", "チャット履歴を表示"),
        ("/open", "履歴からチャットを開く"),
        ("/load", "チャットを読み込む"),
        ("/rename", "現在のチャット名を変更"),
        ("/export", "現在のチャットを Markdown として書き出す"),
        ("/clear", "現在のチャットを消去"),
        ("/model", "別のモデルを選択"),
        ("/search", "Web 検索（標準版では利用不可）"),
        ("/device", "デバイスレポートを表示"),
        ("/snaphelp", "初回チュートリアル中にコマンド画像をデスクトップへ保存"),
        ("/checkupdate", "更新を確認"),
        ("/info", "現在の状態を表示"),
        ("/privacy", "プライバシー説明を表示"),
        ("/language", "言語を変更"),
        ("/multi", "複数行入力"),
        ("/exit", "終了"),
    ],
    "fr": [
        ("/help", "Afficher l'aide"),
        ("/new", "Démarrer une nouvelle conversation"),
        ("/list", "Lister les conversations"),
        ("/history", "Lister l'historique"),
        ("/open", "Ouvrir une conversation de l'historique"),
        ("/load", "Charger une conversation"),
        ("/rename", "Renommer la conversation actuelle"),
        ("/export", "Exporter la conversation actuelle en Markdown"),
        ("/clear", "Effacer la conversation actuelle"),
        ("/model", "Choisir un autre modèle"),
        ("/search", "Recherche web (indisponible dans l'édition Standard)"),
        ("/device", "Afficher le rapport de l'appareil"),
        ("/snaphelp", "Enregistrer une capture des commandes sur le bureau pendant le tutoriel"),
        ("/checkupdate", "Vérifier les mises à jour"),
        ("/info", "Afficher l'état actuel"),
        ("/privacy", "Afficher la confidentialité"),
        ("/language", "Changer de langue"),
        ("/multi", "Saisie multiligne"),
        ("/exit", "Quitter"),
    ],
    "de": [
        ("/help", "Hilfe anzeigen"),
        ("/new", "Neuen Chat starten"),
        ("/list", "Chats auflisten"),
        ("/history", "Chatverlauf auflisten"),
        ("/open", "Chat aus dem Verlauf öffnen"),
        ("/load", "Chat laden"),
        ("/rename", "Aktuellen Chat umbenennen"),
        ("/export", "Aktuellen Chat als Markdown exportieren"),
        ("/clear", "Aktuellen Chat leeren"),
        ("/model", "Anderes Modell wählen"),
        ("/search", "Websuche (in Standardedition nicht verfügbar)"),
        ("/device", "Gerätebericht anzeigen"),
        ("/snaphelp", "Befehlsübersicht während der Einführung auf dem Desktop speichern"),
        ("/checkupdate", "Nach Updates suchen"),
        ("/info", "Aktuellen Status anzeigen"),
        ("/privacy", "Datenschutzhinweis anzeigen"),
        ("/language", "Sprache ändern"),
        ("/multi", "Mehrzeilige Eingabe"),
        ("/exit", "Beenden"),
    ],
}

DRAG_FILE_HELP = {
    "zh_cn": "提示：可将图片或文档直接拖入终端输入行；视觉模型可读取图片。",
    "zh_tw": "提示：可將圖片或文件直接拖入終端輸入列；視覺模型可讀取圖片。",
    "en_us": "Tip: drag images or documents into the terminal input line; vision models can read images.",
    "en_gb": "Tip: drag images or documents into the terminal input line; vision models can read images.",
    "ja": "ヒント：画像や文書を端末の入力行にドラッグできます。視覚モデルは画像を読み取れます。",
    "fr": "Astuce : glissez des images ou documents dans la ligne du terminal ; les modèles visuels peuvent lire les images.",
    "de": "Tipp: Ziehen Sie Bilder oder Dokumente in die Terminal-Eingabezeile; Vision-Modelle können Bilder lesen.",
}

SE_TUTORIAL_TEXT = {
    "zh_cn": {
        "se_tutorial_title": "LocalAI SE 首次使用教学",
        "se_tutorial_intro": "下面是 SE 终端版的常用指令。建议输入 /snaphelp 保存一张指令截图到桌面，之后忘记了也能直接查看。",
        "se_tutorial_prompt": "输入 /snaphelp 保存截图，直接回车继续：",
        "se_tutorial_saved": "✅ 教学截图已保存到桌面：{path}",
        "se_tutorial_failed": "❌ 教学截图保存失败，已改为保存文本：{path}",
        "se_tutorial_continue": "教学完成。之后可随时输入 /help 查看指令。",
    },
    "zh_tw": {
        "se_tutorial_title": "LocalAI SE 首次使用教學",
        "se_tutorial_intro": "下面是 SE 終端版的常用指令。建議輸入 /snaphelp 儲存一張指令截圖到桌面，之後忘記了也能直接查看。",
        "se_tutorial_prompt": "輸入 /snaphelp 儲存截圖，直接 Enter 繼續：",
        "se_tutorial_saved": "✅ 教學截圖已儲存到桌面：{path}",
        "se_tutorial_failed": "❌ 教學截圖儲存失敗，已改為儲存文字：{path}",
        "se_tutorial_continue": "教學完成。之後可隨時輸入 /help 查看指令。",
    },
    "en_us": {
        "se_tutorial_title": "LocalAI SE First-Run Tutorial",
        "se_tutorial_intro": "Here are the common SE terminal commands. Type /snaphelp to save a command screenshot to your Desktop so you can check it later.",
        "se_tutorial_prompt": "Type /snaphelp to save a screenshot, or press Enter to continue: ",
        "se_tutorial_saved": "✅ Tutorial screenshot saved to Desktop: {path}",
        "se_tutorial_failed": "❌ Screenshot failed, so a text copy was saved instead: {path}",
        "se_tutorial_continue": "Tutorial complete. You can type /help anytime to view commands.",
    },
}
SE_TUTORIAL_TEXT["en_gb"] = SE_TUTORIAL_TEXT["en_us"]
for _code, _values in SE_TUTORIAL_TEXT.items():
    UI_TEXT.setdefault(_code, UI_TEXT["zh_cn"]).update(_values)

PRIVACY_LINES = {
    "zh_cn": [
        "LocalAI 标准版只调用本机 Ollama。",
        "聊天内容只保存在本机 chats 文件夹。",
        "程序不会上传聊天内容。",
        "程序只在检查更新时访问 GitHub 的 version.json。",
        "无账号系统，无云端推理。",
    ],
    "zh_tw": [
        "LocalAI 標準版只呼叫本機 Ollama。",
        "聊天內容只儲存在本機 chats 資料夾。",
        "程式不會上傳聊天內容。",
        "程式只會在檢查更新時存取 GitHub 的 version.json。",
        "無帳號系統，無雲端推理。",
    ],
    "en_us": [
        "LocalAI Standard only calls the local Ollama service.",
        "Chat content is stored only in the local chats folder.",
        "The program does not upload chat content.",
        "The program accesses GitHub version.json only when checking for updates.",
        "There is no account system and no cloud inference.",
    ],
    "en_gb": [
        "LocalAI Standard only calls the local Ollama service.",
        "Chat content is stored only in the local chats folder.",
        "The program does not upload chat content.",
        "The program accesses GitHub version.json only when checking for updates.",
        "There is no account system and no cloud inference.",
    ],
    "ja": [
        "LocalAI 標準版はローカルの Ollama のみを呼び出します。",
        "チャット内容はローカルの chats フォルダーにのみ保存されます。",
        "このプログラムはチャット内容をアップロードしません。",
        "更新確認時のみ GitHub の version.json にアクセスします。",
        "アカウント機能もクラウド推論もありません。",
    ],
    "fr": [
        "LocalAI Standard appelle uniquement le service Ollama local.",
        "Les conversations sont stockées uniquement dans le dossier local chats.",
        "Le programme ne téléverse pas le contenu des conversations.",
        "Le programme accède au fichier GitHub version.json uniquement lors de la recherche de mises à jour.",
        "Il n'y a ni système de compte ni inférence dans le cloud.",
    ],
    "de": [
        "LocalAI Standard ruft nur den lokalen Ollama-Dienst auf.",
        "Chatinhalte werden nur im lokalen Ordner chats gespeichert.",
        "Das Programm lädt keine Chatinhalte hoch.",
        "Das Programm greift nur bei der Update-Prüfung auf GitHubs version.json zu.",
        "Es gibt kein Kontosystem und keine Cloud-Inferenz.",
    ],
}

UI_TEXT["zh_cn"].update({
    "first_welcome_banner": "\n================================\n欢迎使用 LocalAI\n本软件默认本地运行，聊天内容保存在本机。\n首次启动需要选择语言。\n================================\n",
    "select_language_prompt": "请选择语言 / Choose language（默认 1）：",
    "language_menu_title": "\n语言 / Language：",
    "language_select_prompt": "请选择语言编号 / Select language number：",
    "invalid_selection": "❌ 选择无效。",
    "no_update_url": "⚠️ 未配置更新地址。",
    "update_found": "\n🔔 发现新版本：{latest}",
    "current_version": "当前版本：{version}",
    "release_notes": "更新内容：{notes}",
    "download_url": "下载地址：{url}",
    "open_download_prompt": "是否打开下载页面？(Y/N)：",
    "already_latest": "✅ 当前已是最新版本：{version}",
    "update_failed": "⚠️ 检查更新失败：{error_type}: {error}",
    "pulling_model": "\n⬇️ 正在安装模型：{model}",
    "pulling_model_hint": "这可能需要较长时间，请保持网络连接。",
    "model_install_done": "✅ 模型安装完成。",
    "ollama_not_found": "❌ 未找到 Ollama，请先安装 Ollama。",
    "model_install_failed": "❌ 模型安装失败。",
    "recommended_model_missing": "未检测到推荐模型：{model}",
    "auto_install_model_prompt": "是否自动安装推荐模型？(Y/N)：",
    "manual_model_prompt": "未检测到模型，请手动输入模型名：",
    "invalid_model_name": "❌ 模型名无效",
    "model_name_example": "示例：qwen2.5:7b",
    "model_name_warning": "不要输入：ollama pull xxx 或 ollama run xxx",
    "available_models": "\n可用模型：",
    "choose_model_prompt": "选择模型编号（默认：{default}）：",
    "new_chat_title": "新会话",
    "unnamed_chat": "未命名",
    "chat_number_prompt": "\n输入聊天编号: ",
    "invalid_number": "❌ 编号无效",
    "out_of_range": "❌ 超出范围",
    "chat_opened": "\n✅ 已打开聊天: {title}",
    "no_chat_history": "❌ 没有历史聊天记录",
    "chat_history_title": "\n📚 历史聊天记录：",
    "exported": "📄 已导出：{path}",
    "export_time": "导出时间",
    "export_user": "用户",
    "export_ai": "AI",
    "export_system": "系统",
    "multiline_hint": "进入多行模式，空行结束：",
    "app_subtitle": "本地隐私 AI 助手",
    "startup_warning": "⚠️ 特别说明：请自行确认消息真实性（AI 也可能出错）",
    "device_blocked_warning": "⚠️ 当前设备不推荐本地运行 AI 模型。",
    "force_hint": "如果仍要继续，请输入 force。",
    "force_prompt": "输入 force 继续，其他任意内容退出：",
    "previous_model_missing": "⚠️ 上次使用的模型 {model} 未检测到。",
    "new_chat_created": "🆕 新会话已创建",
    "rename_prompt": "新标题: ",
    "renamed": "✅ 已重命名",
    "load_select_prompt": "选择编号：",
    "loaded_chat": "📂 已加载：{title}",
    "chat_cleared": "🧹 当前会话已清空",
    "model_changed": "✅ 已切换模型：{model}（{size}）",
    "ai_label": "\n🤖 AI（{elapsed:.1f}s）：",
    "generic_error": "\n❌ 出错：{error_type}: {error}",
    "keyboard_exit": "\n已退出",
    "fatal_error": "程序发生错误，请查看 logs/error.log",
    "search_unavailable": "标准版不支持 /search。请直接提问，或使用支持联网搜索的版本。",
    "cloudai_recommend_title": "\n☁️ 建议使用 CloudAI",
    "cloudai_recommend_body": "检测到当前设备的实际可用显存约 {memory}GB，低于 4GB。本地模型可能启动慢、卡顿或失败。建议优先使用 CloudAI；如仍想继续配置本地模型，可输入 skip 跳过。",
    "cloudai_recommend_prompt": "输入 skip 继续本地模型配置，直接回车结束并稍后配置 CloudAI：",
    "cloudai_recommend_saved": "已记录 CloudAI 推荐。你仍可稍后返回 LocalAI 配置本地模型。",
    "reason_low_spec_blocked": "CPU 核心数过少或内存过低，本地模型可能严重卡顿或无法运行。",
    "reason_apple_8gb": "Apple Silicon 8GB 可尝试 3B 模型。",
    "reason_apple_16gb": "Apple Silicon 16GB 适合 7B。",
    "reason_apple_high": "高内存 Apple Silicon，推荐 14B，后续可尝试更大模型。如需更大模型需自行购买云端 API Token。",
    "reason_x86_tiny": "超低内存设备，推荐 0.8B。",
    "reason_x86_low": "低内存设备，推荐 3B。",
    "reason_x86_ok": "16GB Intel/AMD 推荐 7B。",
    "reason_x86_good": "32GB 可尝试 14B。如需更大模型（如 22B）需自行购买云端 API Token。",
    "reason_x86_high": "高性能设备，可运行大型模型。如需更大模型需自行购买云端 API Token。",
    "reason_cn_low": "国产平台建议保守使用轻量模型。",
    "reason_cn_ok": "国产平台兼容性可能不同，推荐先使用 7B。",
    "reason_loongson": "龙芯平台兼容性不确定，建议轻量模型或等待专门适配。",
    "reason_unknown_low": "未知平台，低内存，推荐轻量模型。",
    "reason_unknown_ok": "未知平台，推荐 7B。",
    "reason_unknown_good": "未知平台，建议先从 7B 开始。",
    "reason_hw_excellent": "综合评估优秀，显存、带宽、架构和后端兼容性都适合较大本地模型。",
    "reason_hw_good": "综合评估良好，推荐使用 7B 级别模型，兼顾速度和效果。",
    "reason_hw_medium": "综合评估中等，推荐使用 3B 级别模型以保证响应速度。",
    "reason_hw_pass": "综合评估合格，建议使用 0.8B 轻量模型。",
    "reason_gpu_not_recommended": "综合评估不推荐本地运行，实际可用显存或后端兼容性不足。",
})

UI_TEXT["zh_tw"].update({
    "first_welcome_banner": "\n================================\n歡迎使用 LocalAI\n本軟體預設在本機執行，聊天內容儲存在本機。\n首次啟動需要選擇語言。\n================================\n",
    "select_language_prompt": "請選擇語言 / Choose language（預設 1）：",
    "language_menu_title": "\n語言 / Language：",
    "language_select_prompt": "請選擇語言編號 / Select language number：",
    "invalid_selection": "❌ 選擇無效。",
    "no_update_url": "⚠️ 未設定更新位址。",
    "update_found": "\n🔔 發現新版本：{latest}",
    "current_version": "目前版本：{version}",
    "release_notes": "更新內容：{notes}",
    "download_url": "下載位址：{url}",
    "open_download_prompt": "是否開啟下載頁面？(Y/N)：",
    "already_latest": "✅ 目前已是最新版本：{version}",
    "update_failed": "⚠️ 檢查更新失敗：{error_type}: {error}",
    "pulling_model": "\n⬇️ 正在安裝模型：{model}",
    "pulling_model_hint": "這可能需要較長時間，請保持網路連線。",
    "model_install_done": "✅ 模型安裝完成。",
    "ollama_not_found": "❌ 找不到 Ollama，請先安裝 Ollama。",
    "model_install_failed": "❌ 模型安裝失敗。",
    "recommended_model_missing": "未偵測到建議模型：{model}",
    "auto_install_model_prompt": "是否自動安裝建議模型？(Y/N)：",
    "manual_model_prompt": "未偵測到模型，請手動輸入模型名稱：",
    "invalid_model_name": "❌ 模型名稱無效",
    "model_name_example": "範例：qwen2.5:7b",
    "model_name_warning": "不要輸入：ollama pull xxx 或 ollama run xxx",
    "available_models": "\n可用模型：",
    "choose_model_prompt": "選擇模型編號（預設：{default}）：",
    "new_chat_title": "新會話",
    "unnamed_chat": "未命名",
    "chat_number_prompt": "\n輸入會話編號: ",
    "invalid_number": "❌ 編號無效",
    "out_of_range": "❌ 超出範圍",
    "chat_opened": "\n✅ 已開啟會話: {title}",
    "no_chat_history": "❌ 沒有歷史會話",
    "chat_history_title": "\n📚 歷史會話：",
    "exported": "📄 已匯出：{path}",
    "export_time": "匯出時間",
    "export_user": "使用者",
    "export_ai": "AI",
    "export_system": "系統",
    "multiline_hint": "進入多行模式，空行結束：",
    "app_subtitle": "本機隱私 AI 助理",
    "startup_warning": "⚠️ 特別說明：請自行確認訊息真實性（AI 也可能出錯）",
    "device_blocked_warning": "⚠️ 目前裝置不建議本機執行 AI 模型。",
    "force_hint": "如果仍要繼續，請輸入 force。",
    "force_prompt": "輸入 force 繼續，其他任意內容結束：",
    "previous_model_missing": "⚠️ 上次使用的模型 {model} 未偵測到。",
    "new_chat_created": "🆕 新會話已建立",
    "rename_prompt": "新標題: ",
    "renamed": "✅ 已重新命名",
    "load_select_prompt": "選擇編號：",
    "loaded_chat": "📂 已載入：{title}",
    "chat_cleared": "🧹 目前會話已清空",
    "model_changed": "✅ 已切換模型：{model}（{size}）",
    "ai_label": "\n🤖 AI（{elapsed:.1f}s）：",
    "generic_error": "\n❌ 出錯：{error_type}: {error}",
    "keyboard_exit": "\n已結束",
    "fatal_error": "程式發生錯誤，請查看 logs/error.log",
    "search_unavailable": "標準版不支援 /search。請直接提問，或使用支援網路搜尋的版本。",
    "cloudai_recommend_title": "\n☁️ 建議使用 CloudAI",
    "cloudai_recommend_body": "偵測到目前裝置的實際可用顯存約 {memory}GB，低於 4GB。本機模型可能啟動慢、卡頓或失敗。建議優先使用 CloudAI；如仍想繼續設定本機模型，可輸入 skip 跳過。",
    "cloudai_recommend_prompt": "輸入 skip 繼續本機模型設定，直接 Enter 結束並稍後設定 CloudAI：",
    "cloudai_recommend_saved": "已記錄 CloudAI 建議。你仍可稍後返回 LocalAI 設定本機模型。",
})

UI_TEXT["en_us"].update({
    "first_welcome_banner": "\n================================\nWelcome to LocalAI\nThis app runs locally by default, and chats are saved on this computer.\nChoose a language on first launch.\n================================\n",
    "select_language_prompt": "Choose language (default 1): ",
    "language_menu_title": "\nLanguage:",
    "language_select_prompt": "Select language number: ",
    "invalid_selection": "❌ Invalid selection.",
    "no_update_url": "⚠️ No update URL configured.",
    "update_found": "\n🔔 New version found: {latest}",
    "current_version": "Current version: {version}",
    "release_notes": "Release notes: {notes}",
    "download_url": "Download URL: {url}",
    "open_download_prompt": "Open the download page? (Y/N): ",
    "already_latest": "✅ You are on the latest version: {version}",
    "update_failed": "⚠️ Update check failed: {error_type}: {error}",
    "pulling_model": "\n⬇️ Installing model: {model}",
    "pulling_model_hint": "This may take a while. Keep your network connected.",
    "model_install_done": "✅ Model installation complete.",
    "ollama_not_found": "❌ Ollama was not found. Please install Ollama first.",
    "model_install_failed": "❌ Model installation failed.",
    "recommended_model_missing": "Recommended model not found: {model}",
    "auto_install_model_prompt": "Install the recommended model automatically? (Y/N): ",
    "manual_model_prompt": "No models found. Enter a model name manually: ",
    "invalid_model_name": "❌ Invalid model name",
    "model_name_example": "Example: qwen2.5:7b",
    "model_name_warning": "Do not enter: ollama pull xxx or ollama run xxx",
    "available_models": "\nAvailable models:",
    "choose_model_prompt": "Choose model number (default: {default}): ",
    "new_chat_title": "New chat",
    "unnamed_chat": "Untitled",
    "chat_number_prompt": "\nEnter chat number: ",
    "invalid_number": "❌ Invalid number",
    "out_of_range": "❌ Out of range",
    "chat_opened": "\n✅ Opened chat: {title}",
    "no_chat_history": "❌ No chat history",
    "chat_history_title": "\n📚 Chat history:",
    "exported": "📄 Exported: {path}",
    "export_time": "Export time",
    "export_user": "User",
    "export_ai": "AI",
    "export_system": "System",
    "multiline_hint": "Multiline mode. Submit an empty line to finish:",
    "app_subtitle": "Local private AI assistant",
    "startup_warning": "⚠️ Note: Please verify important information yourself. AI can be wrong.",
    "device_blocked_warning": "⚠️ This device is not recommended for running local AI models.",
    "force_hint": "To continue anyway, type force.",
    "force_prompt": "Type force to continue, or anything else to exit: ",
    "previous_model_missing": "⚠️ Previously used model {model} was not found.",
    "new_chat_created": "🆕 New chat created",
    "rename_prompt": "New title: ",
    "renamed": "✅ Renamed",
    "load_select_prompt": "Choose number: ",
    "loaded_chat": "📂 Loaded: {title}",
    "chat_cleared": "🧹 Current chat cleared",
    "model_changed": "✅ Switched model: {model} ({size})",
    "ai_label": "\n🤖 AI ({elapsed:.1f}s):",
    "generic_error": "\n❌ Error: {error_type}: {error}",
    "keyboard_exit": "\nExited",
    "fatal_error": "Program error. See logs/error.log",
    "search_unavailable": "Standard edition does not support /search. Ask directly, or use a version with web search.",
    "cloudai_recommend_title": "\n☁️ CloudAI recommended",
    "cloudai_recommend_body": "The usable GPU memory detected is about {memory} GB, below 4 GB. Local models may start slowly, stutter, or fail. CloudAI is recommended first. Type skip if you still want to configure a local model.",
    "cloudai_recommend_prompt": "Type skip to continue local model setup, or press Enter to exit and configure CloudAI later: ",
    "cloudai_recommend_saved": "CloudAI recommendation saved. You can return to LocalAI later to configure a local model.",
})

UI_TEXT["en_gb"].update(UI_TEXT["en_us"])
UI_TEXT["en_gb"].update({
    "model_changed": "✅ Switched model: {model} ({size})",
    "startup_warning": "⚠️ Note: Please verify important information yourself. AI can be wrong.",
})

UI_TEXT["ja"].update({
    "first_welcome_banner": "\n================================\nLocalAI へようこそ\nこのアプリは標準でローカル実行され、チャット内容はこのコンピューターに保存されます。\n初回起動時に言語を選択してください。\n================================\n",
    "select_language_prompt": "言語を選択してください（既定 1）：",
    "language_menu_title": "\n言語：",
    "language_select_prompt": "言語番号を選択してください：",
    "invalid_selection": "❌ 選択が無効です。",
    "no_update_url": "⚠️ 更新 URL が設定されていません。",
    "update_found": "\n🔔 新しいバージョンがあります：{latest}",
    "current_version": "現在のバージョン：{version}",
    "release_notes": "更新内容：{notes}",
    "download_url": "ダウンロード先：{url}",
    "open_download_prompt": "ダウンロードページを開きますか？(Y/N)：",
    "already_latest": "✅ 現在のバージョンは最新です：{version}",
    "update_failed": "⚠️ 更新確認に失敗しました：{error_type}: {error}",
    "pulling_model": "\n⬇️ モデルをインストールしています：{model}",
    "pulling_model_hint": "時間がかかる場合があります。ネットワーク接続を維持してください。",
    "model_install_done": "✅ モデルのインストールが完了しました。",
    "ollama_not_found": "❌ Ollama が見つかりません。先に Ollama をインストールしてください。",
    "model_install_failed": "❌ モデルのインストールに失敗しました。",
    "recommended_model_missing": "推奨モデルが見つかりません：{model}",
    "auto_install_model_prompt": "推奨モデルを自動インストールしますか？(Y/N)：",
    "manual_model_prompt": "モデルが見つかりません。モデル名を入力してください：",
    "invalid_model_name": "❌ モデル名が無効です",
    "model_name_example": "例：qwen2.5:7b",
    "model_name_warning": "入力しないでください：ollama pull xxx または ollama run xxx",
    "available_models": "\n利用可能なモデル：",
    "choose_model_prompt": "モデル番号を選択（既定：{default}）：",
    "new_chat_title": "新しいチャット",
    "unnamed_chat": "無題",
    "chat_number_prompt": "\nチャット番号を入力: ",
    "invalid_number": "❌ 番号が無効です",
    "out_of_range": "❌ 範囲外です",
    "chat_opened": "\n✅ チャットを開きました: {title}",
    "no_chat_history": "❌ チャット履歴がありません",
    "chat_history_title": "\n📚 チャット履歴：",
    "exported": "📄 書き出しました：{path}",
    "export_time": "書き出し時刻",
    "export_user": "ユーザー",
    "export_ai": "AI",
    "export_system": "システム",
    "multiline_hint": "複数行モードです。空行で終了します：",
    "app_subtitle": "ローカルプライバシー AI アシスタント",
    "startup_warning": "⚠️ 注意：重要な情報は自分で確認してください。AI は間違えることがあります。",
    "device_blocked_warning": "⚠️ このデバイスでローカル AI モデルを実行することは推奨されません。",
    "force_hint": "続行する場合は force と入力してください。",
    "force_prompt": "続行するには force、終了するにはその他を入力：",
    "previous_model_missing": "⚠️ 前回使用したモデル {model} が見つかりません。",
    "new_chat_created": "🆕 新しいチャットを作成しました",
    "rename_prompt": "新しいタイトル: ",
    "renamed": "✅ 名前を変更しました",
    "load_select_prompt": "番号を選択：",
    "loaded_chat": "📂 読み込みました：{title}",
    "chat_cleared": "🧹 現在のチャットを消去しました",
    "model_changed": "✅ モデルを切り替えました：{model}（{size}）",
    "ai_label": "\n🤖 AI（{elapsed:.1f}s）：",
    "generic_error": "\n❌ エラー：{error_type}: {error}",
    "keyboard_exit": "\n終了しました",
    "fatal_error": "プログラムエラーが発生しました。logs/error.log を確認してください",
    "search_unavailable": "標準版は /search に対応していません。直接質問するか、Web 検索対応版を使用してください。",
})

UI_TEXT["fr"].update({
    "first_welcome_banner": "\n================================\nBienvenue dans LocalAI\nCette application fonctionne localement par défaut, et les conversations sont enregistrées sur cet ordinateur.\nChoisissez une langue au premier lancement.\n================================\n",
    "select_language_prompt": "Choisissez la langue (défaut 1) : ",
    "language_menu_title": "\nLangue :",
    "language_select_prompt": "Sélectionnez le numéro de langue : ",
    "invalid_selection": "❌ Sélection non valide.",
    "no_update_url": "⚠️ Aucune URL de mise à jour configurée.",
    "update_found": "\n🔔 Nouvelle version trouvée : {latest}",
    "current_version": "Version actuelle : {version}",
    "release_notes": "Notes de version : {notes}",
    "download_url": "Adresse de téléchargement : {url}",
    "open_download_prompt": "Ouvrir la page de téléchargement ? (Y/N) : ",
    "already_latest": "✅ Vous utilisez déjà la dernière version : {version}",
    "update_failed": "⚠️ Échec de la recherche de mise à jour : {error_type}: {error}",
    "pulling_model": "\n⬇️ Installation du modèle : {model}",
    "pulling_model_hint": "Cela peut prendre du temps. Gardez la connexion réseau active.",
    "model_install_done": "✅ Installation du modèle terminée.",
    "ollama_not_found": "❌ Ollama est introuvable. Installez Ollama d'abord.",
    "model_install_failed": "❌ Échec de l'installation du modèle.",
    "recommended_model_missing": "Modèle recommandé introuvable : {model}",
    "auto_install_model_prompt": "Installer automatiquement le modèle recommandé ? (Y/N) : ",
    "manual_model_prompt": "Aucun modèle trouvé. Entrez un nom de modèle : ",
    "invalid_model_name": "❌ Nom de modèle non valide",
    "model_name_example": "Exemple : qwen2.5:7b",
    "model_name_warning": "N'entrez pas : ollama pull xxx ou ollama run xxx",
    "available_models": "\nModèles disponibles :",
    "choose_model_prompt": "Choisissez le numéro du modèle (défaut : {default}) : ",
    "new_chat_title": "Nouvelle conversation",
    "unnamed_chat": "Sans titre",
    "chat_number_prompt": "\nEntrez le numéro de conversation : ",
    "invalid_number": "❌ Numéro non valide",
    "out_of_range": "❌ Hors limite",
    "chat_opened": "\n✅ Conversation ouverte : {title}",
    "no_chat_history": "❌ Aucun historique de conversation",
    "chat_history_title": "\n📚 Historique des conversations :",
    "exported": "📄 Exporté : {path}",
    "export_time": "Heure d'export",
    "export_user": "Utilisateur",
    "export_ai": "IA",
    "export_system": "Système",
    "multiline_hint": "Mode multiligne. Une ligne vide termine la saisie :",
    "app_subtitle": "Assistant IA local et privé",
    "startup_warning": "⚠️ Remarque : vérifiez vous-même les informations importantes. L'IA peut se tromper.",
    "device_blocked_warning": "⚠️ Cet appareil n'est pas recommandé pour exécuter des modèles IA locaux.",
    "force_hint": "Pour continuer quand même, tapez force.",
    "force_prompt": "Tapez force pour continuer, ou autre chose pour quitter : ",
    "previous_model_missing": "⚠️ Le modèle utilisé précédemment {model} est introuvable.",
    "new_chat_created": "🆕 Nouvelle conversation créée",
    "rename_prompt": "Nouveau titre : ",
    "renamed": "✅ Renommé",
    "load_select_prompt": "Choisissez un numéro : ",
    "loaded_chat": "📂 Chargé : {title}",
    "chat_cleared": "🧹 Conversation actuelle effacée",
    "model_changed": "✅ Modèle changé : {model} ({size})",
    "ai_label": "\n🤖 IA ({elapsed:.1f}s) :",
    "generic_error": "\n❌ Erreur : {error_type}: {error}",
    "keyboard_exit": "\nFermé",
    "fatal_error": "Erreur du programme. Consultez logs/error.log",
    "search_unavailable": "L'édition Standard ne prend pas en charge /search. Posez votre question directement ou utilisez une version avec recherche web.",
})

UI_TEXT["de"].update({
    "first_welcome_banner": "\n================================\nWillkommen bei LocalAI\nDiese App läuft standardmäßig lokal, und Chats werden auf diesem Computer gespeichert.\nWählen Sie beim ersten Start eine Sprache aus.\n================================\n",
    "select_language_prompt": "Sprache wählen (Standard 1): ",
    "language_menu_title": "\nSprache:",
    "language_select_prompt": "Sprachnummer wählen: ",
    "invalid_selection": "❌ Ungültige Auswahl.",
    "no_update_url": "⚠️ Keine Update-URL konfiguriert.",
    "update_found": "\n🔔 Neue Version gefunden: {latest}",
    "current_version": "Aktuelle Version: {version}",
    "release_notes": "Versionshinweise: {notes}",
    "download_url": "Download-Adresse: {url}",
    "open_download_prompt": "Download-Seite öffnen? (Y/N): ",
    "already_latest": "✅ Sie verwenden bereits die neueste Version: {version}",
    "update_failed": "⚠️ Update-Prüfung fehlgeschlagen: {error_type}: {error}",
    "pulling_model": "\n⬇️ Modell wird installiert: {model}",
    "pulling_model_hint": "Das kann eine Weile dauern. Halten Sie die Netzwerkverbindung aktiv.",
    "model_install_done": "✅ Modellinstallation abgeschlossen.",
    "ollama_not_found": "❌ Ollama wurde nicht gefunden. Installieren Sie Ollama zuerst.",
    "model_install_failed": "❌ Modellinstallation fehlgeschlagen.",
    "recommended_model_missing": "Empfohlenes Modell nicht gefunden: {model}",
    "auto_install_model_prompt": "Empfohlenes Modell automatisch installieren? (Y/N): ",
    "manual_model_prompt": "Keine Modelle gefunden. Modellnamen manuell eingeben: ",
    "invalid_model_name": "❌ Ungültiger Modellname",
    "model_name_example": "Beispiel: qwen2.5:7b",
    "model_name_warning": "Nicht eingeben: ollama pull xxx oder ollama run xxx",
    "available_models": "\nVerfügbare Modelle:",
    "choose_model_prompt": "Modellnummer wählen (Standard: {default}): ",
    "new_chat_title": "Neuer Chat",
    "unnamed_chat": "Unbenannt",
    "chat_number_prompt": "\nChatnummer eingeben: ",
    "invalid_number": "❌ Ungültige Nummer",
    "out_of_range": "❌ Außerhalb des Bereichs",
    "chat_opened": "\n✅ Chat geöffnet: {title}",
    "no_chat_history": "❌ Kein Chatverlauf",
    "chat_history_title": "\n📚 Chatverlauf:",
    "exported": "📄 Exportiert: {path}",
    "export_time": "Exportzeit",
    "export_user": "Benutzer",
    "export_ai": "KI",
    "export_system": "System",
    "multiline_hint": "Mehrzeiliger Modus. Eine leere Zeile beendet die Eingabe:",
    "app_subtitle": "Lokaler privater KI-Assistent",
    "startup_warning": "⚠️ Hinweis: Prüfen Sie wichtige Informationen selbst. KI kann falsch liegen.",
    "device_blocked_warning": "⚠️ Dieses Gerät wird für lokale KI-Modelle nicht empfohlen.",
    "force_hint": "Um trotzdem fortzufahren, geben Sie force ein.",
    "force_prompt": "Geben Sie force zum Fortfahren ein, sonst wird beendet: ",
    "previous_model_missing": "⚠️ Das zuvor verwendete Modell {model} wurde nicht gefunden.",
    "new_chat_created": "🆕 Neuer Chat erstellt",
    "rename_prompt": "Neuer Titel: ",
    "renamed": "✅ Umbenannt",
    "load_select_prompt": "Nummer wählen: ",
    "loaded_chat": "📂 Geladen: {title}",
    "chat_cleared": "🧹 Aktueller Chat geleert",
    "model_changed": "✅ Modell gewechselt: {model} ({size})",
    "ai_label": "\n🤖 KI ({elapsed:.1f}s):",
    "generic_error": "\n❌ Fehler: {error_type}: {error}",
    "keyboard_exit": "\nBeendet",
    "fatal_error": "Programmfehler. Siehe logs/error.log",
    "search_unavailable": "Die Standardedition unterstützt /search nicht. Fragen Sie direkt oder verwenden Sie eine Version mit Websuche.",
})

for _lang in ["zh_tw", "en_us", "en_gb", "ja", "fr", "de"]:
    for _key, _value in UI_TEXT["zh_cn"].items():
        UI_TEXT[_lang].setdefault(_key, _value)

UI_TEXT["zh_cn"].update({
    "app_title": "LocalAI 标准版",
})

UI_TEXT["zh_tw"].update({
    "app_title": "LocalAI 標準版",
    "reason_low_spec_blocked": "CPU 核心數過少或記憶體過低，本機模型可能嚴重卡頓或無法執行。",
    "reason_apple_8gb": "Apple Silicon 8GB 可嘗試 3B 模型。",
    "reason_apple_16gb": "Apple Silicon 16GB 適合 7B。",
    "reason_apple_high": "高記憶體 Apple Silicon，建議 14B，後續可嘗試更大模型。如需更大模型需自行購買雲端 API Token。",
    "reason_x86_tiny": "超低記憶體裝置，建議 0.8B。",
    "reason_x86_low": "低記憶體裝置，建議 3B。",
    "reason_x86_ok": "16GB Intel/AMD 建議 7B。",
    "reason_x86_good": "32GB 可嘗試 14B。如需更大模型（如 22B）需自行購買雲端 API Token。",
    "reason_x86_high": "高效能裝置，可執行大型模型。如需更大模型需自行購買雲端 API Token。",
    "reason_cn_low": "國產平台建議保守使用輕量模型。",
    "reason_cn_ok": "國產平台相容性可能不同，建議先使用 7B。",
    "reason_loongson": "龍芯平台相容性不確定，建議輕量模型或等待專門適配。",
    "reason_unknown_low": "未知平台，低記憶體，建議輕量模型。",
    "reason_unknown_ok": "未知平台，建議 7B。",
    "reason_unknown_good": "未知平台，建議先從 7B 開始。",
    "reason_hw_excellent": "綜合評估優秀，顯存、頻寬、架構和後端相容性都適合較大的本機模型。",
    "reason_hw_good": "綜合評估良好，建議使用 7B 級別模型，兼顧速度和效果。",
    "reason_hw_medium": "綜合評估中等，建議使用 3B 級別模型以確保回應速度。",
    "reason_hw_pass": "綜合評估合格，建議使用 0.8B 輕量模型。",
    "reason_gpu_not_recommended": "綜合評估不建議本機執行，實際可用顯存或後端相容性不足。",
})

UI_TEXT["en_us"].update({
    "app_title": "LocalAI Standard",
    "reason_low_spec_blocked": "The CPU has too few cores or memory is too low. Local models may be very slow or fail to run.",
    "reason_apple_8gb": "Apple Silicon with 8 GB memory can try a 3B model.",
    "reason_apple_16gb": "Apple Silicon with 16 GB memory is suitable for a 7B model.",
    "reason_apple_high": "High-memory Apple Silicon is recommended for 14B. Larger models may require a cloud API token you purchase separately.",
    "reason_x86_tiny": "Very low-memory device. A 0.8B model is recommended.",
    "reason_x86_low": "Low-memory device. A 3B model is recommended.",
    "reason_x86_ok": "16 GB Intel/AMD devices are recommended for 7B.",
    "reason_x86_good": "32 GB devices can try 14B. Larger models such as 22B may require a cloud API token you purchase separately.",
    "reason_x86_high": "High-performance device. It can run larger models. Still larger models may require a cloud API token.",
    "reason_cn_low": "For this platform, start conservatively with a lightweight model.",
    "reason_cn_ok": "Compatibility may vary on this platform. Start with 7B.",
    "reason_loongson": "Loongson compatibility is uncertain. Use a lightweight model or wait for dedicated support.",
    "reason_unknown_low": "Unknown platform with low memory. A lightweight model is recommended.",
    "reason_unknown_ok": "Unknown platform. A 7B model is recommended.",
    "reason_unknown_good": "Unknown platform. Start with 7B first.",
    "reason_hw_excellent": "Overall rating is excellent. VRAM, bandwidth, architecture, and backend compatibility are suitable for larger local models.",
    "reason_hw_good": "Overall rating is good. A 7B model is recommended for a balanced local experience.",
    "reason_hw_medium": "Overall rating is medium. A 3B model is recommended for better responsiveness.",
    "reason_hw_pass": "Overall rating is pass. A lightweight 0.8B model is recommended.",
    "reason_gpu_not_recommended": "Overall rating is not recommended for local inference because usable GPU memory or backend compatibility is insufficient.",
})

UI_TEXT["en_gb"].update(UI_TEXT["en_us"])

UI_TEXT["ja"].update({
    "app_title": "LocalAI 標準版",
    "reason_low_spec_blocked": "CPU コア数が少ないかメモリが不足しています。ローカルモデルは非常に遅いか、実行できない可能性があります。",
    "reason_apple_8gb": "Apple Silicon 8GB では 3B モデルを試せます。",
    "reason_apple_16gb": "Apple Silicon 16GB は 7B モデルに適しています。",
    "reason_apple_high": "高メモリの Apple Silicon では 14B を推奨します。さらに大きなモデルには別途クラウド API Token が必要な場合があります。",
    "reason_x86_tiny": "超低メモリのデバイスです。0.8B モデルを推奨します。",
    "reason_x86_low": "低メモリのデバイスです。3B モデルを推奨します。",
    "reason_x86_ok": "16GB の Intel/AMD では 7B を推奨します。",
    "reason_x86_good": "32GB では 14B を試せます。22B などの大きなモデルには別途クラウド API Token が必要な場合があります。",
    "reason_x86_high": "高性能デバイスのため大型モデルを実行できます。さらに大きなモデルにはクラウド API Token が必要な場合があります。",
    "reason_cn_low": "このプラットフォームでは軽量モデルから慎重に始めることを推奨します。",
    "reason_cn_ok": "このプラットフォームでは互換性が異なる場合があります。まず 7B を推奨します。",
    "reason_loongson": "Loongson プラットフォームの互換性は不確実です。軽量モデルを使うか、専用対応を待つことを推奨します。",
    "reason_unknown_low": "不明なプラットフォームで低メモリです。軽量モデルを推奨します。",
    "reason_unknown_ok": "不明なプラットフォームです。7B を推奨します。",
    "reason_unknown_good": "不明なプラットフォームです。まず 7B から始めることを推奨します。",
})

UI_TEXT["fr"].update({
    "app_title": "LocalAI Standard",
    "reason_low_spec_blocked": "Le CPU a trop peu de coeurs ou la mémoire est trop faible. Les modèles locaux peuvent être très lents ou ne pas fonctionner.",
    "reason_apple_8gb": "Apple Silicon avec 8 Go de mémoire peut essayer un modèle 3B.",
    "reason_apple_16gb": "Apple Silicon avec 16 Go de mémoire convient à un modèle 7B.",
    "reason_apple_high": "Apple Silicon avec beaucoup de mémoire est recommandé pour 14B. Des modèles plus grands peuvent nécessiter un jeton d'API cloud acheté séparément.",
    "reason_x86_tiny": "Appareil avec très peu de mémoire. Un modèle 0.8B est recommandé.",
    "reason_x86_low": "Appareil avec peu de mémoire. Un modèle 3B est recommandé.",
    "reason_x86_ok": "Les appareils Intel/AMD avec 16 Go sont recommandés pour 7B.",
    "reason_x86_good": "Les appareils avec 32 Go peuvent essayer 14B. Des modèles plus grands comme 22B peuvent nécessiter un jeton d'API cloud acheté séparément.",
    "reason_x86_high": "Appareil performant. Il peut exécuter de grands modèles. Des modèles encore plus grands peuvent nécessiter un jeton d'API cloud.",
    "reason_cn_low": "Pour cette plateforme, commencez prudemment avec un modèle léger.",
    "reason_cn_ok": "La compatibilité peut varier sur cette plateforme. Commencez avec 7B.",
    "reason_loongson": "La compatibilité Loongson est incertaine. Utilisez un modèle léger ou attendez une prise en charge dédiée.",
    "reason_unknown_low": "Plateforme inconnue avec peu de mémoire. Un modèle léger est recommandé.",
    "reason_unknown_ok": "Plateforme inconnue. Un modèle 7B est recommandé.",
    "reason_unknown_good": "Plateforme inconnue. Commencez par 7B.",
})

UI_TEXT["de"].update({
    "app_title": "LocalAI Standard",
    "reason_low_spec_blocked": "Die CPU hat zu wenige Kerne oder zu wenig Arbeitsspeicher. Lokale Modelle können sehr langsam sein oder gar nicht starten.",
    "reason_apple_8gb": "Apple Silicon mit 8 GB Arbeitsspeicher kann ein 3B-Modell ausprobieren.",
    "reason_apple_16gb": "Apple Silicon mit 16 GB Arbeitsspeicher eignet sich für ein 7B-Modell.",
    "reason_apple_high": "Apple Silicon mit viel Arbeitsspeicher wird für 14B empfohlen. Größere Modelle können einen separat erworbenen Cloud-API-Token erfordern.",
    "reason_x86_tiny": "Gerät mit sehr wenig Arbeitsspeicher. Ein 0.8B-Modell wird empfohlen.",
    "reason_x86_low": "Gerät mit wenig Arbeitsspeicher. Ein 3B-Modell wird empfohlen.",
    "reason_x86_ok": "Intel/AMD-Geräte mit 16 GB werden für 7B empfohlen.",
    "reason_x86_good": "Geräte mit 32 GB können 14B ausprobieren. Größere Modelle wie 22B können einen separat erworbenen Cloud-API-Token erfordern.",
    "reason_x86_high": "Leistungsfähiges Gerät. Es kann größere Modelle ausführen. Noch größere Modelle können einen Cloud-API-Token erfordern.",
    "reason_cn_low": "Beginnen Sie auf dieser Plattform vorsichtig mit einem leichten Modell.",
    "reason_cn_ok": "Die Kompatibilität kann auf dieser Plattform variieren. Beginnen Sie mit 7B.",
    "reason_loongson": "Die Loongson-Kompatibilität ist unsicher. Verwenden Sie ein leichtes Modell oder warten Sie auf gezielte Unterstützung.",
    "reason_unknown_low": "Unbekannte Plattform mit wenig Arbeitsspeicher. Ein leichtes Modell wird empfohlen.",
    "reason_unknown_ok": "Unbekannte Plattform. Ein 7B-Modell wird empfohlen.",
    "reason_unknown_good": "Unbekannte Plattform. Beginnen Sie zuerst mit 7B.",
})

UI_TEXT["zh_cn"].update({
    "searching": "🌐 正在联网搜索...",
    "search_keywords": "搜索关键词：{keywords}",
    "search_summarizing": "\n⏳ AI 正在总结搜索结果...",
    "search_ai_label": "\n🤖 AI 联网总结（{elapsed:.1f}s）：",
    "search_no_effective_results": "未获取到有效搜索结果，将直接交给 AI 说明当前没有可用联网结果。",
})
UI_TEXT["zh_tw"].update({
    "searching": "🌐 正在網路搜尋...",
    "search_keywords": "搜尋關鍵字：{keywords}",
    "search_summarizing": "\n⏳ AI 正在摘要搜尋結果...",
    "search_ai_label": "\n🤖 AI 網路摘要（{elapsed:.1f}s）：",
    "search_no_effective_results": "未取得有效搜尋結果，將直接交給 AI 說明目前沒有可用網路結果。",
})
UI_TEXT["en_us"].update({
    "searching": "🌐 Searching online...",
    "search_keywords": "Search keywords: {keywords}",
    "search_summarizing": "\n⏳ AI is summarizing search results...",
    "search_ai_label": "\n🤖 AI web summary ({elapsed:.1f}s):",
    "search_no_effective_results": "No effective search results were retrieved. AI will explain that no usable web results are available.",
})
UI_TEXT["en_gb"].update(UI_TEXT["en_us"])
UI_TEXT["ja"].update({
    "searching": "🌐 Web 検索中...",
    "search_keywords": "検索キーワード：{keywords}",
    "search_summarizing": "\n⏳ AI が検索結果を要約しています...",
    "search_ai_label": "\n🤖 AI Web 要約（{elapsed:.1f}s）：",
    "search_no_effective_results": "有効な検索結果を取得できませんでした。利用可能な Web 結果がないことを AI が説明します。",
})
UI_TEXT["fr"].update({
    "searching": "🌐 Recherche en ligne...",
    "search_keywords": "Mots-clés de recherche : {keywords}",
    "search_summarizing": "\n⏳ L'IA résume les résultats de recherche...",
    "search_ai_label": "\n🤖 Résumé web de l'IA ({elapsed:.1f}s) :",
    "search_no_effective_results": "Aucun résultat de recherche utile n'a été récupéré. L'IA expliquera qu'aucun résultat web exploitable n'est disponible.",
})
UI_TEXT["de"].update({
    "searching": "🌐 Online-Suche läuft...",
    "search_keywords": "Suchbegriffe: {keywords}",
    "search_summarizing": "\n⏳ KI fasst Suchergebnisse zusammen...",
    "search_ai_label": "\n🤖 KI-Webzusammenfassung ({elapsed:.1f}s):",
    "search_no_effective_results": "Es wurden keine brauchbaren Suchergebnisse abgerufen. Die KI erklärt, dass keine nutzbaren Webergebnisse verfügbar sind.",
})


ADDITIONAL_LANGUAGE_UI_OVERRIDES = {
    "en_au": {"language_changed": "✅ Language changed to: {name}", "input_prompt": "👉 You: ", "thinking": "\n⏳ AI is thinking...", "goodbye": "Exited."},
    "ko": {"language_changed": "✅ 언어가 변경되었습니다: {name}", "input_prompt": "👉 사용자: ", "thinking": "\n⏳ AI가 생각 중입니다...", "goodbye": "종료되었습니다."},
    "es": {"language_changed": "✅ Idioma cambiado a: {name}", "input_prompt": "👉 Tú: ", "thinking": "\n⏳ La IA está pensando...", "goodbye": "Saliendo."},
    "it": {"language_changed": "✅ Lingua cambiata in: {name}", "input_prompt": "👉 Tu: ", "thinking": "\n⏳ L'IA sta pensando...", "goodbye": "Uscito."},
    "pt": {"language_changed": "✅ Idioma alterado para: {name}", "input_prompt": "👉 Você: ", "thinking": "\n⏳ A IA está pensando...", "goodbye": "Encerrado."},
    "ru": {"language_changed": "✅ Язык изменён на: {name}", "input_prompt": "👉 Вы: ", "thinking": "\n⏳ ИИ думает...", "goodbye": "Выход."},
    "nl": {"language_changed": "✅ Taal gewijzigd naar: {name}", "input_prompt": "👉 Jij: ", "thinking": "\n⏳ AI denkt na...", "goodbye": "Afgesloten."},
    "sv": {"language_changed": "✅ Språk ändrat till: {name}", "input_prompt": "👉 Du: ", "thinking": "\n⏳ AI tänker...", "goodbye": "Avslutat."},
    "da": {"language_changed": "✅ Sprog ændret til: {name}", "input_prompt": "👉 Du: ", "thinking": "\n⏳ AI tænker...", "goodbye": "Afsluttet."},
    "fi": {"language_changed": "✅ Kieli vaihdettu: {name}", "input_prompt": "👉 Sinä: ", "thinking": "\n⏳ Tekoäly miettii...", "goodbye": "Suljettu."},
    "no": {"language_changed": "✅ Språk endret til: {name}", "input_prompt": "👉 Du: ", "thinking": "\n⏳ AI tenker...", "goodbye": "Avsluttet."},
    "tr": {"language_changed": "✅ Dil değiştirildi: {name}", "input_prompt": "👉 Sen: ", "thinking": "\n⏳ Yapay zekâ düşünüyor...", "goodbye": "Çıkıldı."},
    "pl": {"language_changed": "✅ Zmieniono język na: {name}", "input_prompt": "👉 Ty: ", "thinking": "\n⏳ AI myśli...", "goodbye": "Zakończono."},
    "cs": {"language_changed": "✅ Jazyk změněn na: {name}", "input_prompt": "👉 Vy: ", "thinking": "\n⏳ AI přemýšlí...", "goodbye": "Ukončeno."},
    "uk": {"language_changed": "✅ Мову змінено на: {name}", "input_prompt": "👉 Ви: ", "thinking": "\n⏳ ШІ думає...", "goodbye": "Вийшли."},
    "el": {"language_changed": "✅ Η γλώσσα άλλαξε σε: {name}", "input_prompt": "👉 Εσείς: ", "thinking": "\n⏳ Το AI σκέφτεται...", "goodbye": "Έγινε έξοδος."},
    "ar": {"language_changed": "✅ تم تغيير اللغة إلى: {name}", "input_prompt": "👉 أنت: ", "thinking": "\n⏳ يفكر الذكاء الاصطناعي...", "goodbye": "تم الخروج."},
    "mn": {"language_changed": "✅ Хэл солигдлоо: {name}", "input_prompt": "👉 Та: ", "thinking": "\n⏳ AI бодож байна...", "goodbye": "Гарлаа."},
    "th": {"language_changed": "✅ เปลี่ยนภาษาเป็น: {name}", "input_prompt": "👉 คุณ: ", "thinking": "\n⏳ AI กำลังคิด...", "goodbye": "ออกแล้ว."},
    "vi": {"language_changed": "✅ Đã đổi ngôn ngữ sang: {name}", "input_prompt": "👉 Bạn: ", "thinking": "\n⏳ AI đang suy nghĩ...", "goodbye": "Đã thoát."},
    "id": {"language_changed": "✅ Bahasa diubah ke: {name}", "input_prompt": "👉 Anda: ", "thinking": "\n⏳ AI sedang berpikir...", "goodbye": "Keluar."},
    "ms": {"language_changed": "✅ Bahasa ditukar kepada: {name}", "input_prompt": "👉 Anda: ", "thinking": "\n⏳ AI sedang berfikir...", "goodbye": "Keluar."},
    "hi": {"language_changed": "✅ भाषा बदली गई: {name}", "input_prompt": "👉 आप: ", "thinking": "\n⏳ AI सोच रहा है...", "goodbye": "बंद किया गया."},
}
for _code, _values in ADDITIONAL_LANGUAGE_UI_OVERRIDES.items():
    _base = UI_TEXT.get("en_us", UI_TEXT["zh_cn"]).copy()
    _base.update(_values)
    UI_TEXT[_code] = _base
    COMMAND_HELP[_code] = COMMAND_HELP.get("en_us", COMMAND_HELP["zh_cn"])
    PRIVACY_LINES[_code] = PRIVACY_LINES.get("en_us", PRIVACY_LINES["zh_cn"])

SEARCH_HELP_TEXT = {
    "zh_cn": "联网搜索（完整搜索源，默认最多 2 个来源）",
    "zh_tw": "網路搜尋（完整搜尋來源，預設最多 2 個來源）",
    "en_us": "Web search (full search sources, up to 2 default sources)",
    "en_gb": "Web search (full search sources, up to 2 default sources)",
    "ja": "Web 検索（完全な検索元、既定で最大 2 件）",
    "fr": "Recherche web (sources complètes, jusqu'à 2 sources par défaut)",
    "de": "Websuche (vollständige Quellen, standardmäßig bis zu 2 Quellen)",
}
for _code, _items in COMMAND_HELP.items():
    for _idx, (_command, _description) in enumerate(_items):
        if _command == "/search":
            _items[_idx] = (_command, SEARCH_HELP_TEXT.get(_code, SEARCH_HELP_TEXT["zh_cn"]))
            break


def normalize_language(value):
    key = str(value or "zh_cn").strip().lower().replace("_", "-")
    return LANGUAGE_ALIASES.get(key, "zh_cn")


def get_lang(config):
    return normalize_language(config.get("language", "zh_cn"))


def get_language_profile(config_or_code):
    if isinstance(config_or_code, dict):
        code = get_lang(config_or_code)
    else:
        code = normalize_language(config_or_code)
    return LANGUAGE_OPTIONS[code]


def tr(config_or_code, key, **kwargs):
    code = get_lang(config_or_code) if isinstance(config_or_code, dict) else normalize_language(config_or_code)
    text = UI_TEXT.get(code, UI_TEXT["zh_cn"]).get(key, UI_TEXT["zh_cn"].get(key, key))
    return text.format(**kwargs) if kwargs else text


def get_runtime_config():
    if ACTIVE_CONFIG is not None:
        return ACTIVE_CONFIG

    try:
        return load_config()
    except Exception:
        return DEFAULT_CONFIG.copy()

MODEL_SIZES = {
    "qwen2.5:0.8b": "0.4GB",
    "qwen2.5:1.5b": "1.1GB",
    "qwen2.5:3b": "2.0GB",
    "qwen2.5:7b": "4.7GB",
    "qwen3.5:9b": "5.5GB",
    "qwen2.5:14b": "9GB"
}


def log_error(e):
    ensure_app_dirs()
    with open(os.path.join(LOG_DIR, "error.log"), "a", encoding="utf-8") as f:
        f.write(f"\n[{datetime.now().isoformat()}]\n")
        f.write(traceback.format_exc())
        f.write("\n")


def load_config():
    ensure_app_dirs()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                config = DEFAULT_CONFIG.copy()
                config.update(data)
                config["language"] = normalize_language(config.get("language"))
                config = normalize_provider_config(config)
                return config
        except Exception:
            pass

    config = normalize_provider_config(DEFAULT_CONFIG.copy())
    save_config(config)
    return config

def first_welcome(config):
    if config.get("first_welcome_done", False):
        config["language"] = normalize_language(config.get("language"))
        return config

    print(tr(config, "first_welcome_banner"))
    doc_path = resource_path(LOCALAI_POLICY_DOC)
    print("\n隐私政策与使用手册 / Privacy Policy and User Guide")
    print(f"文档位置 / Document location: {doc_path}")
    preview = read_docx_preview(doc_path)
    if preview:
        print("\n" + preview[:1200])
    else:
        print("未找到 Readme.docx，请确认文档位于应用目录或打包资源中。")
    print("-" * 50)

    codes = list(LANGUAGE_OPTIONS.keys())
    for i, code in enumerate(codes, 1):
        print(f"{i}. {LANGUAGE_OPTIONS[code]['name']}")

    choice = input(tr(config, "select_language_prompt")).strip()

    if choice.isdigit() and 1 <= int(choice) <= len(codes):
        language = codes[int(choice) - 1]
    else:
        language = "zh_cn"

    config["language"] = language
    print(tr(config, "language_changed", name=LANGUAGE_OPTIONS[language]["name"]))
    config["first_welcome_done"] = True
    save_config(config)
    return config


def choose_language(config):
    print(tr(config, "language_menu_title"))
    codes = list(LANGUAGE_OPTIONS.keys())
    current = get_lang(config)

    for i, code in enumerate(codes, 1):
        marker = "*" if code == current else " "
        print(f"{i}. [{marker}] {LANGUAGE_OPTIONS[code]['name']}")

    choice = input(tr(config, "language_select_prompt")).strip()

    if choice.isdigit() and 1 <= int(choice) <= len(codes):
        language = codes[int(choice) - 1]
        config["language"] = language
        save_config(config)
        print(tr(config, "language_changed", name=LANGUAGE_OPTIONS[language]["name"]))
    else:
        print(tr(config, "invalid_selection"))


def save_config(config):
    ensure_app_dirs()
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def check_update(config, silent=False):
    url = config.get("update_url", "")

    if not url:
        if not silent:
            print(tr(config, "no_update_url"))
        return

    try:
        res = requests.get(url, timeout=1.2 if silent else 5)
        res.raise_for_status()
        data = res.json()

        latest = data.get("version", "")
        notes = data.get("notes", "")
        windows_url = data.get("windows_url", "")
        macos_url = data.get("macos_url", "")
        linux_url = data.get("linux_url", "")

        if latest and latest != APP_VERSION:
            print(tr(config, "update_found", latest=latest))
            print(tr(config, "current_version", version=APP_VERSION))

            if notes:
                print(tr(config, "release_notes", notes=notes))

            system = platform.system()

            if system == "Windows" and windows_url:
                print(tr(config, "download_url", url=windows_url))
            elif system == "Darwin" and macos_url:
                print(tr(config, "download_url", url=macos_url))
            elif system == "Linux" and linux_url:
                print(tr(config, "download_url", url=linux_url))

            choice = input(tr(config, "open_download_prompt")).strip().lower()
            if choice == "y":
                target = windows_url if system == "Windows" else macos_url if system == "Darwin" else linux_url
                if target:
                    webbrowser.open(target)

        elif not silent:
            print(tr(config, "already_latest", version=APP_VERSION))

    except Exception as e:
        if not silent:
            print(tr(config, "update_failed", error_type=type(e).__name__, error=e))


def lazy_cpu_info():
    global get_cpu_info, _CPUINFO_LOADED
    if not _CPUINFO_LOADED:
        _CPUINFO_LOADED = True
        try:
            from cpuinfo import get_cpu_info as loaded_get_cpu_info
            get_cpu_info = loaded_get_cpu_info
        except Exception:
            get_cpu_info = None
    return get_cpu_info


def get_cpu_name():
    machine = platform.machine().lower()
    system = platform.system()

    if system == "Darwin" and machine in ["arm64", "aarch64"]:
        return "Apple Silicon"

    name = platform.processor() or os.environ.get("PROCESSOR_IDENTIFIER", "")
    if name:
        return name

    cpu_info = lazy_cpu_info()
    if cpu_info:
        try:
            return cpu_info().get("brand_raw", "Unknown CPU")
        except Exception:
            pass

    if os.name == "nt":
        try:
            result = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"],
                encoding="utf-8",
                errors="ignore",
                timeout=2
            ).strip()
            if result:
                return result
        except Exception:
            pass

    return "Unknown CPU"


def detect_cpu_vendor(cpu_name):
    name = cpu_name.lower()

    if "apple" in name or "a18" in name or platform.machine().lower() in ["arm64", "aarch64"] and platform.system() == "Darwin":
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


def detect_gpu_type(name):
    lower = (name or "").lower()
    if any(token in lower for token in ["apple", "m1", "m2", "m3", "m4", "a18", "a18 pro"]):
        return "unified"
    if any(token in lower for token in ["intel", "uhd", "iris", "integrated", "radeon graphics", "vega graphics", "adreno", "qualcomm"]):
        return "integrated"
    if any(token in lower for token in ["nvidia", "geforce", "rtx", "gtx", "quadro", "tesla", "radeon", "rx ", "arc"]):
        return "discrete"
    return "unknown"


def detect_gpu_vendor(name):
    lower = (name or "").lower()
    if any(token in lower for token in ["nvidia", "geforce", "rtx", "gtx", "quadro", "tesla"]):
        return "NVIDIA"
    if any(token in lower for token in ["amd", "radeon", "rx ", "vega"]):
        return "AMD"
    if any(token in lower for token in ["intel", "uhd", "iris", "arc"]):
        return "Intel"
    if any(token in lower for token in ["apple", "m1", "m2", "m3", "m4", "a18"]):
        return "Apple"
    if any(token in lower for token in ["qualcomm", "adreno"]):
        return "Qualcomm"
    return "Unknown"


def estimate_gpu_bandwidth_gbps(name, gpu_type="unknown"):
    lower = (name or "").lower()
    rules = [
        ("rtx 4090", 1008), ("rtx 4080", 716), ("rtx 4070 ti", 504), ("rtx 4070", 504),
        ("rtx 4060 ti", 288), ("rtx 4060", 272), ("rtx 3090", 936), ("rtx 3080", 760),
        ("rtx 3070", 448), ("rtx 3060", 360), ("rtx 3050", 224), ("gtx 1660", 192),
        ("gtx 1650", 128), ("rx 7900", 960), ("rx 7800", 624), ("rx 7700", 432),
        ("rx 7600", 288), ("rx 6950", 576), ("rx 6900", 512), ("rx 6800", 512),
        ("rx 6700", 384), ("rx 6600", 224), ("rx 580", 256), ("arc a770", 512),
        ("arc a750", 512), ("arc a580", 512), ("m4 max", 546), ("m4 pro", 273),
        ("m4", 120), ("m3 max", 400), ("m3 pro", 150), ("m3", 100), ("m2 ultra", 800),
        ("m2 max", 400), ("m2 pro", 200), ("m2", 100), ("m1 ultra", 800),
        ("m1 max", 400), ("m1 pro", 200), ("m1", 68), ("a18 pro", 120), ("a18", 100),
    ]
    for token, bandwidth in rules:
        if token in lower:
            return bandwidth
    if gpu_type == "discrete":
        return 320
    if gpu_type == "unified":
        return 100
    if gpu_type == "integrated":
        return 60
    return 0


def estimate_gpu_vram_gb(name):
    lower = (name or "").lower()
    rules = [
        ("rtx 4090", 24), ("rtx 4080", 16), ("rtx 4070 ti", 12), ("rtx 4070", 12),
        ("rtx 4060 ti", 8), ("rtx 4060", 8), ("rtx 3090", 24), ("rtx 3080 ti", 12),
        ("rtx 3080", 10), ("rtx 3070 ti", 8), ("rtx 3070", 8), ("rtx 3060", 12),
        ("rtx 3050", 8), ("gtx 1660", 6), ("gtx 1650", 4), ("rx 7900 xtx", 24),
        ("rx 7900 xt", 20), ("rx 7800", 16), ("rx 7700", 12), ("rx 7600", 8),
        ("rx 6950", 16), ("rx 6900", 16), ("rx 6800", 16), ("rx 6700", 12),
        ("rx 6600", 8), ("rx 580", 8), ("arc a770", 16), ("arc a750", 8),
    ]
    for token, vram in rules:
        if token in lower:
            return vram
    return 0


def infer_gpu_architecture_support(name, system, gpu_type="unknown"):
    lower = (name or "").lower()
    if gpu_type == "unified" or "apple" in lower or re.search(r"\bm[1-4]\b|a18", lower):
        return "Metal / Unified Memory"
    if any(token in lower for token in ["rtx 50", "rtx 40", "rtx 30", "rtx 20", "rtx", "quadro", "tesla"]):
        return "CUDA"
    if any(token in lower for token in ["gtx 16", "gtx 10"]):
        return "CUDA legacy"
    if any(token in lower for token in ["radeon", "rx ", "amd"]):
        return "ROCm" if system == "Linux" else "DirectML / Vulkan"
    if "arc" in lower:
        return "oneAPI / Vulkan"
    if gpu_type == "integrated":
        return "Shared Memory"
    return "CPU fallback"


def backend_compatibility_score(gpu, system, vendor):
    name = gpu.get("name", "")
    lower = name.lower()
    kind = gpu.get("type", "unknown")
    if vendor == "Apple Silicon" or kind == "unified":
        return 5, "Metal"
    if "nvidia" in lower or any(token in lower for token in ["rtx", "gtx", "quadro", "tesla"]):
        return (5, "CUDA") if system in ["Windows", "Linux"] else (3, "CUDA unavailable on modern macOS")
    if "amd" in lower or "radeon" in lower or "rx " in lower:
        if system == "Linux":
            return 4, "ROCm"
        if system == "Windows":
            return 3, "DirectML / Vulkan"
        return 2, "Metal compatibility varies"
    if "arc" in lower:
        return 3, "oneAPI / Vulkan"
    if kind == "integrated":
        return 2, "Shared memory backend"
    return 1, "CPU fallback"


def gpu_memory_gb(value):
    try:
        number = int(value or 0)
    except Exception:
        return 0
    if number <= 0:
        return 0
    return round(number / (1024 ** 3), 1)


def normalize_gpu(name, memory_gb=0, gpu_type=None, shared_gb=0):
    name = (name or "Unknown GPU").strip()
    gpu_type = gpu_type or detect_gpu_type(name)
    estimated_vram = estimate_gpu_vram_gb(name) if gpu_type == "discrete" else 0
    memory_gb = max(float(memory_gb or 0), float(estimated_vram or 0))
    return {
        "name": name,
        "type": gpu_type,
        "vendor": detect_gpu_vendor(name),
        "bandwidth_gbps": estimate_gpu_bandwidth_gbps(name, gpu_type),
        "vram_gb": round(float(memory_gb or 0), 1),
        "shared_gb": round(float(shared_gb or 0), 1),
    }


def detect_nvidia_smi_gpus():
    try:
        result = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            encoding="utf-8",
            errors="ignore",
            timeout=3,
        )
    except Exception:
        return []
    gpus = []
    for line in result.splitlines():
        if not line.strip() or "," not in line:
            continue
        name, memory = [part.strip() for part in line.split(",", 1)]
        try:
            vram = round(float(memory) / 1024, 1)
        except Exception:
            vram = estimate_gpu_vram_gb(name)
        gpus.append(normalize_gpu(name, vram, "discrete", 0))
    return gpus


def detect_windows_gpus(ram_gb):
    gpus = detect_nvidia_smi_gpus()
    command = [
        "powershell", "-NoProfile", "-Command",
        "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM | ConvertTo-Json -Compress"
    ]
    try:
        result = subprocess.check_output(command, encoding="utf-8", errors="ignore", timeout=3).strip()
        if not result:
            return []
        data = json.loads(result)
        if isinstance(data, dict):
            data = [data]
        gpus = []
        for item in data:
            name = item.get("Name") or "Unknown GPU"
            if any(gpu["name"].lower() in name.lower() or name.lower() in gpu["name"].lower() for gpu in gpus):
                continue
            kind = detect_gpu_type(name)
            vram = gpu_memory_gb(item.get("AdapterRAM")) if kind == "discrete" else 0
            shared = min(max(round(ram_gb / 2, 1), 1), 16) if kind in ["integrated", "unknown"] else 0
            gpus.append(normalize_gpu(name, vram, kind, shared))
        return gpus
    except Exception:
        return []


def detect_macos_gpus(ram_gb, vendor):
    if vendor == "Apple Silicon":
        return [normalize_gpu("Apple integrated GPU", 0, "unified", ram_gb)]
    try:
        result = subprocess.check_output(
            ["system_profiler", "SPDisplaysDataType"],
            encoding="utf-8",
            errors="ignore",
            timeout=4,
        )
    except Exception:
        return []
    gpus = []
    current_name = ""
    current_vram = 0
    for raw_line in result.splitlines() + [""]:
        line = raw_line.strip()
        if line.startswith("Chipset Model:"):
            if current_name:
                kind = detect_gpu_type(current_name)
                shared = min(max(round(ram_gb / 2, 1), 1), 16) if kind == "integrated" else 0
                gpus.append(normalize_gpu(current_name, current_vram, kind, shared))
            current_name = line.split(":", 1)[1].strip()
            current_vram = 0
        elif line.startswith("VRAM") and ":" in line:
            value = line.split(":", 1)[1]
            match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(GB|MB)", value, flags=re.IGNORECASE)
            if match:
                amount = float(match.group(1))
                current_vram = amount if match.group(2).lower() == "gb" else round(amount / 1024, 1)
        elif not line and current_name:
            kind = detect_gpu_type(current_name)
            shared = min(max(round(ram_gb / 2, 1), 1), 16) if kind == "integrated" else 0
            gpus.append(normalize_gpu(current_name, current_vram, kind, shared))
            current_name = ""
            current_vram = 0
    return gpus


def detect_linux_gpus(ram_gb):
    gpus = detect_nvidia_smi_gpus()
    try:
        result = subprocess.check_output(["lspci"], encoding="utf-8", errors="ignore", timeout=3)
        for line in result.splitlines():
            if not re.search(r"VGA|3D|Display", line, flags=re.IGNORECASE):
                continue
            name = line.split(":", 2)[-1].strip()
            if any(gpu["name"].lower() in name.lower() or name.lower() in gpu["name"].lower() for gpu in gpus):
                continue
            kind = detect_gpu_type(name)
            shared = min(max(round(ram_gb / 2, 1), 1), 16) if kind in ["integrated", "unknown"] else 0
            gpus.append(normalize_gpu(name, 0, kind, shared))
    except Exception:
        pass
    return gpus


def detect_gpus(system, ram_gb, vendor):
    if system == "Windows":
        gpus = detect_windows_gpus(ram_gb)
    elif system == "Darwin":
        gpus = detect_macos_gpus(ram_gb, vendor)
    elif system == "Linux":
        gpus = detect_linux_gpus(ram_gb)
    else:
        gpus = []
    return gpus or [normalize_gpu("Unknown GPU", 0, "unknown", min(max(round(ram_gb / 2, 1), 1), 16))]


def primary_gpu(gpus):
    if not gpus:
        return normalize_gpu("Unknown GPU", 0, "unknown", 0)
    return sorted(
        gpus,
        key=lambda gpu: (
            2 if gpu.get("type") == "discrete" else 1 if gpu.get("type") in ["unified", "integrated"] else 0,
            gpu.get("vram_gb", 0),
            gpu.get("shared_gb", 0),
        ),
        reverse=True,
    )[0]


def format_gpu_report(gpus):
    parts = []
    for gpu in gpus or []:
        kind = gpu.get("type", "unknown")
        memory = gpu.get("vram_gb", 0) if kind == "discrete" else gpu.get("shared_gb", 0)
        memory_label = "VRAM" if kind == "discrete" else "shared"
        bandwidth = gpu.get("bandwidth_gbps", 0)
        bandwidth_text = f", {bandwidth}GB/s" if bandwidth else ""
        parts.append(f"{gpu.get('name', 'Unknown GPU')} [{gpu.get('vendor', 'Unknown')}, {kind}, {memory_label} {memory}GB{bandwidth_text}]")
    return "; ".join(parts) if parts else "Unknown GPU"


def detect_disk_status():
    try:
        ensure_app_dirs()
        usage = shutil.disk_usage(APP_DATA_DIR)
    except Exception:
        try:
            usage = shutil.disk_usage(os.path.expanduser("~"))
        except Exception:
            return {"total_gb": 0, "free_gb": 0, "warning": False}
    total = round(usage.total / (1024 ** 3), 1)
    free = round(usage.free / (1024 ** 3), 1)
    return {"total_gb": total, "free_gb": free, "warning": free < 20}


def hardware_grade_label(grade, lang="zh_cn"):
    labels = {
        "excellent": {"zh_cn": "优秀", "zh_tw": "優秀", "en": "Excellent"},
        "good": {"zh_cn": "良好", "zh_tw": "良好", "en": "Good"},
        "medium": {"zh_cn": "中等", "zh_tw": "中等", "en": "Medium"},
        "pass": {"zh_cn": "合格", "zh_tw": "合格", "en": "Pass"},
        "not_recommended": {"zh_cn": "不推荐", "zh_tw": "不推薦", "en": "Not recommended"},
    }
    code = get_lang(lang) if isinstance(lang, dict) else normalize_language(lang)
    key = "zh_tw" if code == "zh_tw" else "zh_cn" if code == "zh_cn" else "en"
    return labels.get(grade, labels["not_recommended"]).get(key, grade)


def model_for_hardware_grade(grade):
    return {
        "excellent": "qwen2.5:14b",
        "good": "qwen2.5:7b",
        "medium": "qwen2.5:3b",
        "pass": "qwen2.5:0.8b",
        "not_recommended": None,
    }.get(grade)


def effective_gpu_memory_gb(device):
    if device.get("vendor") == "Apple Silicon":
        return float(device.get("ram_gb", 0) or 0)
    gpu = primary_gpu(device.get("gpus", []))
    kind = gpu.get("type", "unknown")
    ram = float(device.get("ram_gb", 0) or 0)
    if kind == "discrete":
        return float(gpu.get("vram_gb", 0) or 0)
    shared = float(gpu.get("shared_gb", 0) or 0)
    if shared <= 0:
        shared = min(max(round(ram / 2, 1), 1), 16)
    if kind in ["integrated", "unknown"]:
        vendor = (device.get("vendor") or "").lower()
        ratio = 0.65 if any(name in vendor for name in ["amd", "intel", "hygon"]) else 0.85
        return round(min(shared, ram * ratio), 1)
    return round(shared, 1)


def assess_hardware(device):
    gpu = primary_gpu(device.get("gpus", []))
    system = device.get("system", platform.system())
    vendor = device.get("vendor", "")
    ram = float(device.get("ram_gb", 0) or 0)
    effective = effective_gpu_memory_gb(device)
    bandwidth = float(gpu.get("bandwidth_gbps", 0) or estimate_gpu_bandwidth_gbps(gpu.get("name", ""), gpu.get("type", "unknown")))
    backend_score, backend = backend_compatibility_score(gpu, system, vendor)
    architecture = infer_gpu_architecture_support(gpu.get("name", ""), system, gpu.get("type", "unknown"))

    score = 0
    if effective >= 16:
        score += 4
    elif effective >= 10:
        score += 3
    elif effective >= 6:
        score += 2
    elif effective >= 4:
        score += 1

    if bandwidth >= 700:
        score += 3
    elif bandwidth >= 350:
        score += 2
    elif bandwidth >= 120:
        score += 1

    score += max(0, min(backend_score, 5)) - 1

    if ram >= 32:
        score += 2
    elif ram >= 16:
        score += 1
    elif ram < 8:
        score -= 2

    if vendor == "Apple Silicon":
        if ram >= 32:
            score += 2
        elif ram >= 16:
            score += 1

    if effective < 4 or ram < 6:
        grade = "not_recommended"
    elif score >= 10:
        grade = "excellent"
    elif score >= 8:
        grade = "good"
    elif score >= 5:
        grade = "medium"
    else:
        grade = "pass"

    return {
        "grade": grade,
        "model": model_for_hardware_grade(grade),
        "effective_gpu_memory_gb": round(effective, 1),
        "bandwidth_gbps": round(bandwidth, 1),
        "architecture": architecture,
        "backend": backend,
        "backend_score": backend_score,
        "gpu_name": gpu.get("name", "Unknown GPU"),
        "gpu_vendor": gpu.get("vendor", detect_gpu_vendor(gpu.get("name", ""))),
    }


def format_hardware_assessment(device, lang="zh_cn"):
    assessment = device.get("hardware_assessment") or assess_hardware(device)
    return (
        f"{hardware_grade_label(assessment.get('grade'), lang)} | "
        f"{assessment.get('gpu_name', 'Unknown GPU')} | "
        f"{assessment.get('effective_gpu_memory_gb', 0)}GB | "
        f"{assessment.get('bandwidth_gbps', 0)}GB/s | "
        f"{assessment.get('architecture', '')} | "
        f"{assessment.get('backend', '')}"
    )


def disk_warning_text(device, lang="zh_cn"):
    disk = device.get("disk", {})
    free = disk.get("free_gb", 0)
    code = get_lang(lang) if isinstance(lang, dict) else normalize_language(lang)
    if code == "zh_tw":
        return f"⚠️ 硬碟可用空間約 {free}GB，安裝本機模型可能空間不足。"
    if code == "zh_cn":
        return f"⚠️ 硬盘可用空间约 {free}GB，安装本地模型可能空间不足。"
    return f"⚠️ Free disk space is about {free} GB. Installing local models may fail."


def gpu_limited_recommendation(device, current):
    if device.get("vendor") == "Apple Silicon":
        return current
    gpu = primary_gpu(device.get("gpus", []))
    ram = device.get("ram_gb", 0)
    cores = device.get("physical_cores", 0)
    kind = gpu.get("type", "unknown")
    vram = gpu.get("vram_gb", 0)
    shared = gpu.get("shared_gb", 0)

    def rec(level, model, reason_key):
        return {"level": level, "model": model, "reason_key": reason_key}

    if kind == "discrete":
        if vram < 3:
            return rec("low", "qwen2.5:0.8b", "reason_x86_tiny")
        if vram < 6:
            return rec("low", "qwen2.5:3b", "reason_x86_low")
        if vram < 8:
            return rec("ok", "qwen2.5:3b", "reason_x86_low")
        if vram < 12:
            return rec("ok", "qwen2.5:7b", "reason_x86_ok")
        if ram >= 24:
            return rec("good", "qwen2.5:14b", "reason_x86_good")
        return rec("ok", "qwen2.5:7b", "reason_x86_ok")

    if kind in ["integrated", "unified", "unknown"]:
        if ram <= 6 or cores <= 3:
            return rec("blocked", None, "reason_low_spec_blocked")
        if shared < 6 or ram <= 12:
            return rec("low", "qwen2.5:3b", "reason_x86_low")
        if shared >= 8 and ram >= 16:
            return rec("ok", "qwen2.5:7b", "reason_x86_ok")
        return rec("low", "qwen2.5:3b", "reason_x86_low")

    return current


def detect_device():
    global DEVICE_CACHE
    if DEVICE_CACHE is not None:
        return DEVICE_CACHE.copy()

    system = platform.system()
    machine = platform.machine()
    cpu_name = get_cpu_name()
    vendor = detect_cpu_vendor(cpu_name)

    physical_cores = psutil.cpu_count(logical=False) or 0
    logical_cores = psutil.cpu_count(logical=True) or 0
    ram_gb = round(psutil.virtual_memory().total / (1024 ** 3))
    gpus = detect_gpus(system, ram_gb, vendor)
    disk = detect_disk_status()

    DEVICE_CACHE = {
        "system": system,
        "machine": machine,
        "cpu_name": cpu_name,
        "vendor": vendor,
        "physical_cores": physical_cores,
        "logical_cores": logical_cores,
        "ram_gb": ram_gb,
        "gpus": gpus,
        "gpu": primary_gpu(gpus),
        "disk": disk,
    }
    DEVICE_CACHE["hardware_assessment"] = assess_hardware(DEVICE_CACHE)
    return DEVICE_CACHE.copy()


def evaluate_device(device):
    vendor = device["vendor"]
    ram = device["ram_gb"]
    cores = device["physical_cores"]
    assessment = device.get("hardware_assessment") or assess_hardware(device)

    def rec(level, model, reason_key):
        return {"level": level, "model": model, "reason_key": reason_key}

    def with_assessment(base):
        grade = assessment.get("grade", "not_recommended")
        model = assessment.get("model")
        if grade == "not_recommended":
            return rec("blocked", None, "reason_gpu_not_recommended")
        level = {
            "excellent": "high",
            "good": "good",
            "medium": "ok",
            "pass": "low",
        }.get(grade, base.get("level", "ok"))
        return rec(level, model or base.get("model"), f"reason_hw_{grade}")

    if cores <= 3 or ram <= 6:
        return rec("blocked", None, "reason_low_spec_blocked")

    if vendor == "Apple Silicon":
        if ram <= 8:
            return with_assessment(rec("ok", "qwen2.5:3b", "reason_apple_8gb"))
        elif ram <= 16:
            return with_assessment(rec("good", "qwen2.5:7b", "reason_apple_16gb"))
        else:
            return with_assessment(rec("high", "qwen2.5:14b", "reason_apple_high"))

    if vendor in ["Intel", "AMD", "Hygon"]:
        if ram <= 6:
            base = rec("low", "qwen2.5:0.8b", "reason_x86_tiny")
        elif ram <= 8:
            base = rec("low", "qwen2.5:3b", "reason_x86_low")
        elif ram <= 16:
            base = rec("ok", "qwen2.5:7b", "reason_x86_ok")
        elif ram <= 32:
            base = rec("good", "qwen2.5:14b", "reason_x86_good")
        else:
            base = rec("high", "qwen2.5:14b", "reason_x86_high")
        return with_assessment(gpu_limited_recommendation(device, base))

    if vendor in ["Zhaoxin", "Phytium", "Kunpeng"]:
        base = rec("low", "qwen2.5:3b", "reason_cn_low") if ram <= 8 else rec("ok", "qwen2.5:7b", "reason_cn_ok")
        return with_assessment(gpu_limited_recommendation(device, base))

    if vendor == "Loongson":
        return with_assessment(rec("warning", "qwen2.5:3b", "reason_loongson"))

    if ram <= 8:
        base = rec("low", "qwen2.5:3b", "reason_unknown_low")
    elif ram <= 16:
        base = rec("ok", "qwen2.5:7b", "reason_unknown_ok")
    else:
        base = rec("good", "qwen2.5:7b", "reason_unknown_good")
    return with_assessment(gpu_limited_recommendation(device, base))

def print_device_report(device, recommendation, config=None, force=False):
    if config is not None and not force:
        if config.get("first_device_info_done", False):
            return

    lang = config or "zh_cn"
    print(tr(lang, "device_title"))
    print(f"{tr(lang, 'system')}：{device['system']}")
    print(f"{tr(lang, 'arch')}：{device['machine']}")
    print(f"{tr(lang, 'cpu')}：{device['cpu_name']}")
    print(f"{tr(lang, 'cpu_vendor')}：{device['vendor']}")
    print(f"{tr(lang, 'physical_cores')}：{device['physical_cores']}")
    print(f"{tr(lang, 'logical_threads')}：{device['logical_cores']}")
    print(f"{tr(lang, 'memory')}：{device['ram_gb']}GB")
    print(f"GPU：{format_gpu_report(device.get('gpus', []))}")
    print(f"{'综合等级' if normalize_language(get_lang(lang) if isinstance(lang, dict) else lang) == 'zh_cn' else 'Hardware Rating'}：{format_hardware_assessment(device, lang)}")
    disk = device.get("disk", {})
    if disk:
        print(f"{'硬盘可用空间' if normalize_language(get_lang(lang) if isinstance(lang, dict) else lang) == 'zh_cn' else 'Free Disk Space'}：{disk.get('free_gb', 0)}GB / {disk.get('total_gb', 0)}GB")

    print(tr(lang, "model_recommendation"))
    if device.get("disk", {}).get("warning"):
        print(disk_warning_text(device, lang))
    if recommendation["model"]:
        print(f"{tr(lang, 'recommended_model')}：{recommendation['model']}")
    else:
        print(f"{tr(lang, 'recommended_model')}：{tr(lang, 'not_recommended')}")

    reason_key = recommendation.get("reason_key", "reason_unknown_good")
    print(f"{tr(lang, 'reason')}：{tr(lang, reason_key)}\n")

    if config is not None and not force:
        config["first_device_info_done"] = True
        save_config(config)


def hardware_cloudai_recommendation(device):
    assessment = device.get("hardware_assessment") or assess_hardware(device)
    available = assessment.get("effective_gpu_memory_gb", 0)
    if device.get("vendor") == "Apple Silicon":
        return {"recommend": False, "available_gb": available, "skip_reason": "apple_silicon"}
    return {"recommend": available < 4, "available_gb": available, "skip_reason": ""}

def maybe_prompt_cloudai_for_low_gpu(config, device):
    if config.get("cloudai_low_gpu_prompt_done", False):
        return True

    result = hardware_cloudai_recommendation(device)
    if not result.get("recommend"):
        return True

    print(tr(config, "cloudai_recommend_title"))
    print(tr(config, "cloudai_recommend_body", memory=result.get("available_gb", 0)))
    choice = input(tr(config, "cloudai_recommend_prompt")).strip().lower()

    config["cloudai_low_gpu_prompt_done"] = True
    if choice == "skip":
        save_config(config)
        return True

    config["mode"] = "cloudai_recommended"
    save_config(config)
    print(tr(config, "cloudai_recommend_saved"))
    return False


def get_ollama_binary_path():
    candidates = []
    found = shutil.which("ollama")
    if found:
        candidates.append(found)
    candidates.extend(OS_OPTIMIZATION_PROFILE.get("ollama_extra_paths", []))

    system = platform.system()
    if system == "Darwin":
        candidates.extend([
            "/opt/homebrew/bin/ollama",
            "/usr/local/bin/ollama",
            "/Applications/Ollama.app/Contents/Resources/ollama",
        ])
    elif system == "Windows":
        local_appdata = os.environ.get("LOCALAPPDATA", "")
        program_files = os.environ.get("ProgramFiles", "")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", "")
        candidates.extend([
            os.path.join(local_appdata, "Programs", "Ollama", "ollama.exe"),
            os.path.join(local_appdata, "Ollama", "ollama.exe"),
            os.path.join(program_files, "Ollama", "ollama.exe"),
            os.path.join(program_files_x86, "Ollama", "ollama.exe"),
        ])
    else:
        candidates.extend(["/usr/local/bin/ollama", "/usr/bin/ollama", "/snap/bin/ollama"])

    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def get_ollama_app_path():
    if platform.system() != "Darwin":
        return None
    for path in ("/Applications/Ollama.app", os.path.expanduser("~/Applications/Ollama.app")):
        if os.path.isdir(path):
            return path
    return None


def check_ollama_installed():
    return get_ollama_binary_path() is not None or get_ollama_app_path() is not None


def is_ollama_running():
    for url in (OLLAMA_TAGS_URL, "http://127.0.0.1:11434/api/tags", "http://localhost:11434/api/version"):
        try:
            res = requests.get(url, timeout=1.2)
            if res.status_code == 200:
                return True
        except Exception:
            continue
    return False


def get_models():
    for url in (OLLAMA_TAGS_URL, "http://127.0.0.1:11434/api/tags"):
        try:
            res = requests.get(url, timeout=1.5)
            res.raise_for_status()
            return res.json().get("models", [])
        except Exception:
            continue
    return []


def list_ollama_models():
    return get_models()


def get_ollama_models():
    return get_models()


def recommend_model(device):
    return evaluate_device(device)


def model_exists(model_name):
    models = get_models()
    names = [m.get("name", "") for m in models]
    return model_name in names


def pull_model(model_name, config):
    print(tr(config, "pulling_model", model=model_name))
    print(tr(config, "pulling_model_hint"))

    try:
        subprocess.run([get_ollama_binary_path() or "ollama", "pull", model_name], check=True)
        print(tr(config, "model_install_done"))
        return True
    except FileNotFoundError:
        print(tr(config, "ollama_not_found"))
        return False
    except subprocess.CalledProcessError:
        print(tr(config, "model_install_failed"))
        return False


def is_valid_model_name(name):
    if not name:
        return False

    lower = name.lower()

    bad_keywords = [
        "ollama pull",
        "/search",
        "http://",
        "https://",
        "www."
    ]

    for bad in bad_keywords:
        if bad in lower:
            return False

    return True


def choose_model(config, recommendation):
    models = get_models()

    recommended_model = recommendation.get("model")

    if recommended_model and not model_exists(recommended_model):
        print(tr(config, "recommended_model_missing", model=recommended_model))
        choice = input(tr(config, "auto_install_model_prompt")).strip().lower()
        if choice == "y":
            pull_model(recommended_model, config)

    models = get_models()

    if not models:
        while True:
            model = input(tr(config, "manual_model_prompt")).strip()

            if not model:
                return recommended_model or "qwen2.5:7b"

            if is_valid_model_name(model):
                return model

            print(tr(config, "invalid_model_name"))
            print(tr(config, "model_name_example"))
            print(tr(config, "model_name_warning"))

    print(tr(config, "available_models"))
    for i, m in enumerate(models):
        print(f"{i + 1}. {m['name']}")

    default = config.get("last_model") or recommended_model or models[0]["name"]
    choice = input(tr(config, "choose_model_prompt", default=default)).strip()

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

    if any(x in name for x in ["0.8b", "0.6b", "1.5b", "1.7b", "3b"]):
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
    return title or "chat"


def new_chat(config, first_title=None):
    if first_title is None:
        first_title = tr(config, "new_chat_title")

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = os.path.join(CHAT_DIR, timestamp + ".json")

    data = {
        "title": first_title,
        "created_at": datetime.now().isoformat(),
        "messages": []
    }

    save_chat(path, data)
    return path, data


def open_chat(config):
    files = list_chats(config)

    if not files:
        return None, None

    choice = input(tr(config, "chat_number_prompt")).strip()

    if not choice.isdigit():
        print(tr(config, "invalid_number"))
        return None, None

    idx = int(choice) - 1

    if idx < 0 or idx >= len(files):
        print(tr(config, "out_of_range"))
        return None, None

    path = os.path.join(CHAT_DIR, files[idx])

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    print(tr(config, "chat_opened", title=data.get("title", tr(config, "unnamed_chat"))))

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


def list_chats(config):
    files = sorted(
        [f for f in os.listdir(CHAT_DIR) if f.endswith(".json")],
        reverse=True
    )

    if not files:
        print(tr(config, "no_chat_history"))
        return []

    print(tr(config, "chat_history_title"))

    for i, filename in enumerate(files, 1):
        path = os.path.join(CHAT_DIR, filename)
        title = filename
        created = ""

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                title = data.get("title", filename)
                created = data.get("created_at", "")[:16].replace("T", " ")
        except Exception:
            pass

        suffix = f" | {created}" if created else ""
        print(f"{i}. {title}{suffix}  ({filename})")

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


def export_md(path, data, config):
    base = os.path.basename(path).replace(".json", ".md")
    export_path = os.path.join(EXPORT_DIR, base)

    with open(export_path, "w", encoding="utf-8") as f:
        f.write(f"# {data.get('title', tr(config, 'unnamed_chat'))}\n\n")
        f.write(f"- {tr(config, 'export_time')}：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

        for item in data.get("messages", []):
            if item["role"] == "user":
                f.write(f"## 👤 {tr(config, 'export_user')}\n{item['content']}\n\n")
            elif item["role"] == "ai":
                f.write(f"## 🤖 {tr(config, 'export_ai')}\n{item['content']}\n\n")
            else:
                f.write(f"## ⚠️ {tr(config, 'export_system')}\n{item['content']}\n\n")

    print(tr(config, "exported", path=export_path))


def multiline_input(config):
    line = input(tr(config, "input_prompt")).rstrip()

    if line != "/multi":
        return line

    print(tr(config, "multiline_hint"))
    lines = []

    while True:
        current = input()
        if current == "":
            break
        lines.append(current)

    return "\n".join(lines).strip()


def build_prompt(question, messages, lang="zh_cn"):
    profile = get_language_profile(lang)
    recent = messages[-CONTEXT_ITEMS:]

    history_text = ""
    for item in recent:
        role = profile["user_role"] if item["role"] == "user" else profile["assistant_role"]
        history_text += f"{role}: {item['content']}\n"

    return f"""
You are a local AI assistant running on the user's computer.

Privacy rules:
1. AI inference runs locally whenever possible.
2. Chat content is not uploaded by default.
3. No system identity spoofing.
4. Standard edition only uses restricted web search when the user enables it.

Reply rules:
1. Reply in {profile["model_language"]} by default.
2. Do not pretend to know uncertain facts.
3. Do not make up facts.
4. Do not output long reasoning process.
5. Refer to conversation history when useful, but do not repeat mechanically.
6. If the user explicitly asks for another language, follow the user's request for that answer.

[Conversation history]
{history_text}

[Current question]
{question}

Please answer:
"""


def encode_images_for_ollama(image_paths):
    images = []
    for path in image_paths or []:
        try:
            images.append(encode_image_for_ollama(path))
        except Exception as exc:
            log_error(exc)
    return images


def ollama_error_text(response):
    try:
        payload = response.json()
        if isinstance(payload, dict):
            return str(payload.get("error") or payload.get("message") or response.text)
    except Exception:
        pass
    return response.text or f"HTTP {response.status_code}"


def ask_ollama_chat(prompt, model, size, images=None):
    message = {"role": "user", "content": prompt}
    if images:
        message["images"] = images
    data = {
        "model": model,
        "messages": [message],
        "stream": False,
        "options": get_options(size),
    }
    response = requests.post(OLLAMA_CHAT_URL, json=data, timeout=180)
    response.raise_for_status()
    payload = response.json()
    message = payload.get("message") if isinstance(payload, dict) else None
    if isinstance(message, dict):
        return (message.get("content") or "").strip()
    return ((payload.get("response") if isinstance(payload, dict) else "") or "").strip()


def ask_ollama(prompt, model, size, image_paths=None):
    images = encode_images_for_ollama(image_paths) if image_paths else []
    data = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": get_options(size)
    }
    if images:
        data["images"] = images

    response = requests.post(OLLAMA_GENERATE_URL, json=data, timeout=180)
    if response.status_code >= 400 and images:
        log_error(Exception(f"Ollama generate vision failed: {ollama_error_text(response)}"))
        return ask_ollama_chat(prompt, model, size, images)

    response.raise_for_status()
    return response.json().get("response", "").strip()


def normalize_provider(value):
    provider = str(value or "ollama").strip().lower().replace("-", "_")
    aliases = {
        "lmstudio": "lm_studio",
        "lm_studio": "lm_studio",
        "openai_compatible_api": "openai_compatible",
        "openai_compatible": "openai_compatible",
        "compatible": "openai_compatible",
        "openai": "openai_official",
        "openai_official": "openai_official",
        "official_openai": "openai_official",
        "ollama": "ollama",
    }
    provider = aliases.get(provider, provider)
    return provider if provider in SUPPORTED_PROVIDERS else "ollama"


def provider_display_name(provider):
    return {
        "ollama": "Ollama",
        "lm_studio": "LM Studio",
        "openai_compatible": "OpenAI Compatible API",
        "openai_official": "OpenAI Official API",
    }.get(normalize_provider(provider), "Ollama")


def normalize_provider_config(config):
    config = config or {}
    if "provider" not in config:
        legacy = config.get("model_provider")
        config["provider"] = legacy or "ollama"
    if not config.get("api_base_url") and config.get("openai_compatible_url"):
        config["api_base_url"] = config.get("openai_compatible_url", "")
    if not config.get("api_key") and config.get("openai_api_key"):
        config["api_key"] = config.get("openai_api_key", "")
    if not config.get("openai_model") and config.get("openai_compatible_model"):
        config["openai_model"] = config.get("openai_compatible_model", "")
    config["provider"] = normalize_provider(config.get("provider", "ollama"))
    config.setdefault("api_base_url", "")
    config.setdefault("api_key", "")
    config.setdefault("openai_model", "")
    config.setdefault("lmstudio_base_url", "http://localhost:1234/v1")
    return config


def messages_to_prompt(messages):
    if isinstance(messages, str):
        return messages
    parts = []
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role", "user")
        content = item.get("content", "")
        if isinstance(content, list):
            content = "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
        parts.append(f"{role}: {content}")
    return "\n".join(parts).strip()


MODERATION_BLOCK_MESSAGE = "这个话题不合适，换一个聊聊吧！"


MODERATION_PATTERNS = [
    r"(制作|制造|合成|配方|教程|步骤).{0,12}(炸弹|爆炸物|燃烧瓶|枪支|弹药|毒品|冰毒|海洛因|芬太尼)",
    r"(购买|售卖|贩卖|交易|运输).{0,12}(毒品|枪支|弹药|爆炸物|假币|身份证|银行卡)",
    r"(儿童色情|未成年人色情|幼女色情|萝莉色情|child porn|csam)",
    r"(如何|怎么).{0,8}(杀人|绑架|勒索|投毒|纵火|恐袭|恐怖袭击)",
    r"(自杀方法|怎么自杀|协助自杀|自残教程|割腕教程)",
    r"(盗号|撞库|脱库|木马|勒索病毒|钓鱼网站|绕过支付|破解支付|窃取密码|窃取隐私|开盒教程)",
    r"(洗钱|逃税|诈骗话术|电信诈骗|伪造公章|伪造证件|伪造发票)",
    r"(仇恨|屠杀|灭绝).{0,12}(民族|种族|宗教|性别|残疾|同性恋|跨性别)",
]


def content_allowed(text):
    if not text:
        return True
    normalized = re.sub(r"\s+", "", str(text).lower())
    return not any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in MODERATION_PATTERNS)


def moderated_text(text):
    return text if content_allowed(text) else MODERATION_BLOCK_MESSAGE


def openai_chat_url(base_url):
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def openai_models_url(base_url):
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    return base + "/models"


def response_text_from_json(data):
    if not isinstance(data, dict):
        return ""
    for key in ("response", "text", "output", "content", "answer"):
        value = data.get(key)
        if isinstance(value, str):
            return value.strip()
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] or {}
        if isinstance(first.get("text"), str):
            return first["text"].strip()
        message = first.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [item.get("text", "") for item in content if isinstance(item, dict)]
            return "\n".join(part for part in parts if part).strip()
    return ""


def image_data_url(path):
    ext = os.path.splitext(path)[1].lower().strip(".") or "jpeg"
    if ext == "jpg":
        ext = "jpeg"
    return f"data:image/{ext};base64,{encode_image_for_ollama(path)}"


def openai_messages_with_images(messages, image_paths=None):
    result = []
    source = messages if isinstance(messages, list) else [{"role": "user", "content": str(messages or "")}]
    for item in source:
        if not isinstance(item, dict):
            continue
        result.append({"role": item.get("role", "user"), "content": item.get("content", "")})
    if image_paths:
        if not result:
            result.append({"role": "user", "content": ""})
        content = result[-1].get("content", "")
        blocks = [{"type": "text", "text": content if isinstance(content, str) else str(content)}]
        blocks.extend({"type": "image_url", "image_url": {"url": image_data_url(path)}} for path in image_paths)
        result[-1]["content"] = blocks
    return result


def ask_openai_chat_completions(messages, model, config, base_url, api_key="", image_paths=None):
    url = openai_chat_url(base_url)
    if not url:
        raise ValueError("API Base URL is empty.")
    target_model = (config.get("openai_model") or model or "").strip()
    if not target_model:
        raise ValueError("Model is empty.")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    data = {
        "model": target_model,
        "messages": openai_messages_with_images(messages, image_paths),
        "temperature": 0.7,
        "stream": False,
    }
    response = requests.post(url, headers=headers, json=data, timeout=180)
    response.raise_for_status()
    return response_text_from_json(response.json())


def ask_lm_studio(messages, model, config, image_paths=None):
    cfg = normalize_provider_config(config)
    base_url = cfg.get("lmstudio_base_url") or "http://localhost:1234/v1"
    return ask_openai_chat_completions(messages, model, cfg, base_url, cfg.get("api_key", ""), image_paths)


def ask_openai_compatible(messages, model, config, image_paths=None):
    cfg = normalize_provider_config(config)
    base_url = cfg.get("api_base_url")
    if not base_url:
        raise ValueError("API Base URL is empty.")
    return ask_openai_chat_completions(messages, model, cfg, base_url, cfg.get("api_key", ""), image_paths)


def ask_openai_official(messages, model, config, image_paths=None):
    cfg = normalize_provider_config(config)
    api_key = cfg.get("api_key", "")
    if not api_key:
        raise ValueError("OpenAI API Key is empty.")
    cfg = cfg.copy()
    cfg["openai_model"] = cfg.get("openai_model") or model or "gpt-4.1-mini"
    return ask_openai_chat_completions(messages, cfg["openai_model"], cfg, "https://api.openai.com/v1", api_key, image_paths)


def ask_model(messages, model, config):
    cfg = normalize_provider_config(config.copy() if isinstance(config, dict) else {})
    provider = normalize_provider(cfg.get("provider"))
    size = cfg.get("_model_size") or get_model_size(model)
    image_paths = cfg.get("_image_paths") or []
    prompt = messages_to_prompt(messages)
    if not content_allowed(prompt):
        return MODERATION_BLOCK_MESSAGE
    if provider == "lm_studio":
        try:
            return moderated_text(ask_lm_studio(messages, model, cfg, image_paths))
        except Exception as exc:
            log_error(exc)
            provider = "ollama"
    elif provider == "openai_compatible":
        try:
            return moderated_text(ask_openai_compatible(messages, model, cfg, image_paths))
        except Exception as exc:
            log_error(exc)
            provider = "ollama"
    elif provider == "openai_official":
        try:
            return moderated_text(ask_openai_official(messages, model, cfg, image_paths))
        except Exception as exc:
            log_error(exc)
            provider = "ollama"
    return moderated_text(ask_ollama(prompt, model, size, image_paths))


def ask_local(prompt, model, size, image_paths=None):
    config = (ACTIVE_CONFIG or load_config()).copy()
    config["_model_size"] = size
    config["_image_paths"] = image_paths or []
    return ask_model([{"role": "user", "content": prompt}], model, config)


def test_provider_connection(provider, config):
    cfg = normalize_provider_config(config.copy() if isinstance(config, dict) else {})
    provider = normalize_provider(provider)
    try:
        if provider == "ollama":
            response = requests.get(OLLAMA_TAGS_URL, timeout=3)
            return response.status_code == 200, "Ollama OK" if response.status_code == 200 else response.text
        if provider == "lm_studio":
            url = openai_models_url(cfg.get("lmstudio_base_url") or "http://localhost:1234/v1")
            response = requests.get(url, timeout=5)
            return 200 <= response.status_code < 300, "LM Studio OK" if 200 <= response.status_code < 300 else response.text
        if provider == "openai_compatible":
            if not cfg.get("api_base_url"):
                return False, "API Base URL is empty."
            headers = {}
            if cfg.get("api_key"):
                headers["Authorization"] = f"Bearer {cfg['api_key']}"
            response = requests.get(openai_models_url(cfg.get("api_base_url")), headers=headers, timeout=8)
            return 200 <= response.status_code < 300, "OpenAI Compatible OK" if 200 <= response.status_code < 300 else response.text
        if provider == "openai_official":
            if not cfg.get("api_key"):
                return False, "OpenAI API Key is empty."
            response = requests.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {cfg['api_key']}"}, timeout=8)
            return 200 <= response.status_code < 300, "OpenAI OK" if 200 <= response.status_code < 300 else response.text
    except Exception as exc:
        return False, str(exc)
    return False, "Provider is not supported."


IMAGE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif",
    ".heic", ".heif", ".avif", ".ico"
}
DOCUMENT_EXTENSIONS = {
    ".doc", ".docx", ".txt", ".md", ".markdown", ".csv", ".tsv", ".json",
    ".jsonl", ".yaml", ".yml", ".xml", ".html", ".htm", ".rtf", ".log",
    ".ini", ".cfg", ".conf", ".toml", ".py", ".js", ".ts", ".java", ".c",
    ".cpp", ".h", ".hpp", ".cs", ".go", ".rs", ".swift", ".kt", ".php",
    ".rb", ".sh", ".bat", ".ps1", ".sql", ".css"
}
VISION_MODEL_HINTS = (
    "llava", "bakllava", "moondream", "minicpm-v", "minicpm-vl",
    "qwen2-vl", "qwen2.5-vl", "qwen2vl", "qwen2.5vl", "qwen-vl",
    "gemma3", "llama3.2-vision", "pixtral", "vision"
)


def is_image_file(path):
    return os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS


def is_document_file(path):
    return os.path.splitext(path)[1].lower() in DOCUMENT_EXTENSIONS


def model_supports_images(model):
    name = (model or "").lower()
    return any(hint in name for hint in VISION_MODEL_HINTS)


def encode_image_for_ollama(path):
    try:
        from PIL import Image
        with Image.open(path) as image:
            image = image.convert("RGB")
            image.thumbnail((1600, 1600))
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as temp:
                temp_path = temp.name
            try:
                image.save(temp_path, format="JPEG", quality=88)
                with open(temp_path, "rb") as f:
                    return base64.b64encode(f.read()).decode("ascii")
            finally:
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
    except Exception:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")


def read_docx_text(path):
    parts = []
    with zipfile.ZipFile(path) as docx:
        names = ["word/document.xml"]
        names.extend(
            name for name in docx.namelist()
            if name.startswith("word/header") or name.startswith("word/footer")
        )
        for name in names:
            if name not in docx.namelist():
                continue
            root = ET.fromstring(docx.read(name))
            for node in root.iter():
                if node.tag.endswith("}t") and node.text:
                    parts.append(node.text)
                elif node.tag.endswith("}p"):
                    parts.append("\n")
    text = "".join(parts)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def read_doc_text(path):
    commands = []
    if platform.system() == "Darwin" and shutil.which("textutil"):
        commands.append(["textutil", "-convert", "txt", "-stdout", path])
    for tool in ("antiword", "catdoc"):
        if shutil.which(tool):
            commands.append([tool, path])
    for command in commands:
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            text = (result.stdout or "").strip()
            if result.returncode == 0 and text:
                return text
        except Exception as exc:
            log_error(exc)
    return ""


def read_plain_text(path, max_chars=12000):
    encodings = ["utf-8", "utf-8-sig", sys.getdefaultencoding(), "gbk", "big5", "latin-1"]
    seen = set()
    for encoding in encodings:
        if not encoding or encoding in seen:
            continue
        seen.add(encoding)
        try:
            with open(path, "r", encoding=encoding, errors="strict") as f:
                text = f.read(max_chars + 1)
            return text[:max_chars].strip()
        except Exception:
            continue
    try:
        with open(path, "rb") as f:
            data = f.read(max_chars * 2)
        return data.decode("utf-8", errors="ignore")[:max_chars].strip()
    except Exception:
        return ""


def read_document_text(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        return read_docx_text(path)
    if ext == ".doc":
        return read_doc_text(path)
    return read_plain_text(path, max_chars=120000)


RAG_CHUNK_SIZE = 1100
RAG_CHUNK_OVERLAP = 180
RAG_MAX_CONTEXT_CHARS = 12000
RAG_TOP_K = 6


def normalize_rag_text(text):
    return re.sub(r"\s+", " ", text or "").strip()


def chunk_document_text(text, chunk_size=RAG_CHUNK_SIZE, overlap=RAG_CHUNK_OVERLAP):
    text = normalize_rag_text(text)
    if not text:
        return []
    chunks = []
    start = 0
    text_len = len(text)
    while start < text_len:
        end = min(start + chunk_size, text_len)
        if end < text_len:
            boundary = max(text.rfind("。", start, end), text.rfind(".", start, end), text.rfind("\n", start, end))
            if boundary > start + chunk_size // 2:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= text_len:
            break
        start = max(end - overlap, 0)
    return chunks


def rag_tokens(text):
    lowered = (text or "").lower()
    latin = re.findall(r"[a-z0-9_]{2,}", lowered)
    cjk = re.findall(r"[\u4e00-\u9fff]", lowered)
    cjk_pairs = ["".join(cjk[i:i + 2]) for i in range(max(len(cjk) - 1, 0))]
    return set(latin + cjk + cjk_pairs)


def score_rag_chunk(question_tokens, chunk):
    chunk_tokens = rag_tokens(chunk)
    if not question_tokens or not chunk_tokens:
        return 0
    overlap = question_tokens & chunk_tokens
    if not overlap:
        return 0
    return sum(3 if len(token) > 1 else 1 for token in overlap)


def retrieve_relevant_chunks(name, text, question, top_k=RAG_TOP_K):
    chunks = split_text(text)
    if not chunks:
        return []
    selected = search_chunks(chunks, question, top_k=top_k)
    return [(name, index, chunk, score) for score, index, chunk in selected]


def read_file(path):
    if is_document_file(path):
        return read_document_text(path)
    return ""


def split_text(text, chunk_size=RAG_CHUNK_SIZE, overlap=RAG_CHUNK_OVERLAP):
    return chunk_document_text(text, chunk_size=chunk_size, overlap=overlap)


def search_chunks(chunks, question, top_k=RAG_TOP_K):
    question_tokens = rag_tokens(question)
    scored = []
    for index, chunk in enumerate(chunks, 1):
        score = score_rag_chunk(question_tokens, chunk)
        scored.append((score, index, chunk))
    scored.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    if scored and scored[0][0] > 0:
        return scored[:top_k]
    return scored[:min(3, top_k)]


def ask_document(question, file_paths, model, size, messages=None, lang="zh_cn"):
    imported = summarize_imported_files(file_paths or [], model, question)
    model_question = append_imported_files_to_question(question, imported) if file_paths else question
    prompt = build_prompt(model_question, messages or [], lang)
    return ask_local(prompt, model, size, imported.get("images", [])) or tr(lang, "empty_answer")


def summarize_imported_files(paths, model, question=""):
    docs = []
    image_paths = []
    blind_images = []
    unreadable_files = []
    can_see_images = model_supports_images(model)
    context_chars = 0

    for path in paths:
        name = os.path.basename(path)
        if is_document_file(path):
            try:
                text = read_document_text(path)
                if text:
                    for doc_name, index, chunk, score in retrieve_relevant_chunks(name, text, question):
                        if context_chars + len(chunk) > RAG_MAX_CONTEXT_CHARS and docs:
                            break
                        docs.append((doc_name, f"[chunk {index}, score {score}]\n{chunk}"))
                        context_chars += len(chunk)
                else:
                    unreadable_files.append(name)
            except Exception as exc:
                log_error(exc)
                unreadable_files.append(name)
        elif is_image_file(path):
            if can_see_images:
                image_paths.append(path)
            else:
                blind_images.append(name)
        else:
            unreadable_files.append(name)

    return {
        "documents": docs,
        "images": image_paths,
        "blind_images": blind_images,
        "unreadable_files": unreadable_files,
        "can_see_images": can_see_images,
        "rag_enabled": True,
    }


def append_imported_files_to_question(question, imported):
    sections = []
    if question:
        sections.append(question)
    if imported["documents"]:
        doc_text = []
        for name, text in imported["documents"]:
            doc_text.append(f"[Document: {name}]\n{text}")
        sections.append("[Relevant document chunks retrieved by LocalAI RAG]\n" + "\n\n".join(doc_text))
    if imported["images"]:
        sections.append(
            "[Imported images attached directly to the model]\n"
            + "\n".join(os.path.basename(path) for path in imported["images"])
            + "\nPlease inspect the attached image content and answer the user's request."
        )
    if imported.get("blind_images"):
        sections.append(
            "[Imported images not visible to this model]\n"
            + "\n".join(imported["blind_images"])
            + "\nYou must say exactly: 我看不到图片"
        )
    if imported.get("unreadable_files"):
        sections.append("[Imported files LocalAI could not convert]\n" + "\n".join(imported["unreadable_files"]))
    return "\n\n".join(sections)


def parse_dropped_file_paths(text):
    paths = []
    try:
        tokens = shlex.split(text, posix=(os.name != "nt"))
    except Exception:
        tokens = text.split()

    tokens.extend(re.findall(r"file://\S+", text))
    for token in tokens:
        cleaned = token.strip().strip("\"'")
        if not cleaned:
            continue
        if cleaned.startswith("file://"):
            cleaned = unquote(urlparse(cleaned).path)
        cleaned = os.path.expanduser(cleaned)
        cleaned = os.path.abspath(cleaned)
        if cleaned in paths:
            continue
        if os.path.exists(cleaned) and (is_document_file(cleaned) or is_image_file(cleaned)):
            paths.append(cleaned)
    return paths


def remove_file_paths_from_text(text, paths):
    result = text
    for path in sorted(paths, key=len, reverse=True):
        variants = {
            path,
            os.path.abspath(path),
            shlex.quote(path),
            path.replace(" ", "\\ "),
            "file://" + quote(path),
        }
        for item in variants:
            result = result.replace(item, " ")
    return re.sub(r"\s+", " ", result).strip()


SEARCH_SOURCES = {
    "news": {
        "name": "Bing 新闻",
        "domain": "",
        "search_url": "https://www.bing.com/news/search?q={q}&format=rss"
    },
    "wiki": {
        "name": "维基百科",
        "domain": "zh.wikipedia.org",
        "search_url": "https://zh.wikipedia.org/wiki/Special:Search?search={q}"
    },
    "wiki_en": {
        "name": "Wikipedia",
        "domain": "en.wikipedia.org",
        "search_url": "https://en.wikipedia.org/wiki/Special:Search?search={q}"
    },
    "baidu": {
        "name": "百度",
        "domain": "baidu.com",
        "search_url": "https://www.baidu.com/s?wd={q}"
    },
    "baike": {
        "name": "百度百科",
        "domain": "baike.baidu.com",
        "search_url": "https://baike.baidu.com/search/word?word={q}"
    },
    "apple_cn": {
        "name": "Apple 中国",
        "domain": "apple.com.cn",
        "search_url": "https://www.apple.com.cn/search/{q}"
    },
    "apple": {
        "name": "Apple",
        "domain": "apple.com",
        "search_url": "https://www.apple.com/search/{q}"
    },
    "apple_support": {
        "name": "Apple Support",
        "domain": "support.apple.com",
        "search_url": "https://support.apple.com/search?query={q}"
    },
    "microsoft": {
        "name": "Microsoft",
        "domain": "microsoft.com",
        "search_url": "https://www.microsoft.com/search?q={q}"
    },
    "learn_ms": {
        "name": "Microsoft Learn",
        "domain": "learn.microsoft.com",
        "search_url": "https://learn.microsoft.com/search/?terms={q}"
    },
    "openai": {
        "name": "OpenAI",
        "domain": "openai.com",
        "search_url": "https://openai.com/search/?q={q}"
    },
    "python": {
        "name": "Python Docs",
        "domain": "docs.python.org",
        "search_url": "https://docs.python.org/3/search.html?q={q}"
    },
    "pypi": {
        "name": "PyPI",
        "domain": "pypi.org",
        "search_url": "https://pypi.org/search/?q={q}"
    },
    "csdn": {
        "name": "CSDN",
        "domain": "csdn.net",
        "search_url": "https://so.csdn.net/so/search?q={q}"
    },
    "cnblogs": {
        "name": "博客园",
        "domain": "cnblogs.com",
        "search_url": "https://zzk.cnblogs.com/s?w={q}"
    },
    "gitee": {
        "name": "Gitee",
        "domain": "gitee.com",
        "search_url": "https://search.gitee.com/?q={q}"
    },
    "stackoverflow": {
        "name": "Stack Overflow",
        "domain": "stackoverflow.com",
        "search_url": "https://stackoverflow.com/search?q={q}"
    },
    "reddit": {
        "name": "Reddit",
        "domain": "reddit.com",
        "search_url": "https://www.reddit.com/search/?q={q}"
    },
    "arxiv": {
        "name": "arXiv",
        "domain": "arxiv.org",
        "search_url": "https://arxiv.org/search/?query={q}&searchtype=all"
    },
    "pubmed": {
        "name": "PubMed",
        "domain": "pubmed.ncbi.nlm.nih.gov",
        "search_url": "https://pubmed.ncbi.nlm.nih.gov/?term={q}"
    },
    "jd": {
        "name": "京东",
        "domain": "jd.com",
        "search_url": "https://search.jd.com/Search?keyword={q}"
    },
    "xianyu": {
        "name": "闲鱼",
        "domain": "goofish.com",
        "search_url": "https://www.goofish.com/search?q={q}"
    },
    "bili": {
        "name": "bilibili",
        "domain": "bilibili.com",
        "search_url": "https://search.bilibili.com/all?keyword={q}"
    },
    "douyin": {
        "name": "抖音",
        "domain": "douyin.com",
        "search_url": "https://www.douyin.com/search/{q}"
    },
    "zhihu": {
        "name": "知乎",
        "domain": "zhihu.com",
        "search_url": "https://www.zhihu.com/search?type=content&q={q}"
    },
    "weibo": {
        "name": "微博",
        "domain": "weibo.com",
        "search_url": "https://s.weibo.com/weibo?q={q}"
    },
    "xiaohongshu": {
        "name": "小红书",
        "domain": "xiaohongshu.com",
        "search_url": "https://www.xiaohongshu.com/search_result?keyword={q}"
    },
    "toutiao": {
        "name": "今日头条",
        "domain": "toutiao.com",
        "search_url": "https://so.toutiao.com/search?keyword={q}"
    },
    "taobao": {
        "name": "淘宝",
        "domain": "taobao.com",
        "search_url": "https://s.taobao.com/search?q={q}"
    },
    "github": {
        "name": "GitHub",
        "domain": "github.com",
        "search_url": "https://github.com/search?q={q}"
    },
    "youtube": {
        "name": "YouTube",
        "domain": "youtube.com",
        "search_url": "https://www.youtube.com/results?search_query={q}"
    },
    "amazon": {
        "name": "Amazon",
        "domain": "amazon.com",
        "search_url": "https://www.amazon.com/s?k={q}"
    },
    "theverge": {
        "name": "The Verge",
        "domain": "theverge.com",
        "search_url": "https://www.theverge.com/search?q={q}"
    }
}

ALLOWED_SEARCH_DOMAINS = tuple(source["domain"].lower() for source in SEARCH_SOURCES.values())


def is_permitted_url(url):
    try:
        host = urlparse(url).netloc.lower().split(":", 1)[0]
    except Exception:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in ALLOWED_SEARCH_DOMAINS)


def extract_urls(text):
    return re.findall(r"https?://[^\s<>\"]+", text or "")

SOURCE_TYPES = {
    "apple_cn": "官方",
    "apple": "官方",
    "apple_support": "官方",
    "github": "官方",
    "microsoft": "官方",
    "learn_ms": "官方",
    "openai": "官方",
    "python": "官方",
    "pypi": "官方",
    "news": "第三方",
    "wiki": "第三方",
    "wiki_en": "第三方",
    "baike": "第三方",
    "baidu": "第三方",
    "zhihu": "第三方",
    "bili": "第三方",
    "douyin": "第三方",
    "csdn": "第三方",
    "cnblogs": "第三方",
    "gitee": "第三方",
    "stackoverflow": "第三方",
    "reddit": "第三方",
    "arxiv": "第三方",
    "pubmed": "第三方",
    "jd": "第三方",
    "taobao": "第三方",
    "xianyu": "第三方",
    "weibo": "第三方",
    "xiaohongshu": "第三方",
    "toutiao": "第三方",
    "youtube": "第三方",
    "amazon": "第三方",
    "theverge": "第三方",
}

SOURCE_TYPE_ORDER = {
    "官方": 0,
    "第三方": 1,
    "不确定": 2,
}

DEFAULT_SEARCH_SOURCES = [
    "news",
    "baidu",
    "baike",
    "zhihu",
    "bili",
    "douyin",
    "csdn",
    "gitee",
    "apple_cn",
    "wiki",
]

SOURCE_KEYWORDS = {
    "news": ["最新", "新闻", "消息", "资讯", "热搜", "热点", "今天", "今日", "实时", "latest", "news", "today", "recent", "current"],
    "apple_cn": ["apple", "iphone", "ipad", "mac", "macbook", "ios", "ipados", "macos", "watchos", "visionos", "苹果", "保修", "维修", "国行", "中国"],
    "apple": ["apple", "iphone", "ipad", "mac", "macbook", "ios", "ipados", "macos", "watchos", "visionos", "苹果"],
    "apple_support": ["support", "repair", "warranty", "保修", "维修", "支持", "故障", "apple"],
    "microsoft": ["windows", "office", "surface", "xbox", "microsoft", "微软"],
    "learn_ms": ["azure", ".net", "powershell", "visual studio", "windows api", "microsoft learn"],
    "openai": ["openai", "chatgpt", "gpt", "api", "codex"],
    "python": ["python", "pip", "venv", "asyncio", "tkinter", "pyinstaller"],
    "pypi": ["pypi", "package", "library", "pip"],
    "csdn": ["报错", "错误", "代码", "python", "java", "javascript", "教程", "开发", "编程"],
    "cnblogs": ["报错", "错误", "代码", "教程", "开发", "博客园"],
    "gitee": ["gitee", "码云", "开源", "国产", "仓库"],
    "stackoverflow": ["error", "exception", "traceback", "bug", "报错", "错误", "代码", "python", "javascript"],
    "github": ["github", "repo", "repository", "release", "issue", "开源"],
    "reddit": ["reddit", "review", "experience", "评价", "体验"],
    "arxiv": ["paper", "research", "论文", "研究", "模型"],
    "pubmed": ["medical", "medicine", "clinical", "health", "医学", "健康", "疾病"],
    "jd": ["京东", "价格", "购买", "商品"],
    "taobao": ["淘宝", "价格", "购买", "商品"],
    "xianyu": ["闲鱼", "二手", "二手交易"],
    "bili": ["bilibili", "b站", "视频", "教程"],
    "douyin": ["抖音", "短视频", "视频", "热点", "探店"],
    "zhihu": ["知乎", "评价", "体验", "推荐", "怎么样", "为什么"],
    "weibo": ["微博", "热搜", "热点", "新闻"],
    "xiaohongshu": ["小红书", "攻略", "种草", "体验", "推荐"],
    "toutiao": ["头条", "今日头条", "新闻", "热点"],
    "youtube": ["youtube", "video", "视频", "教程"],
    "amazon": ["amazon", "亚马逊", "price", "buy"],
    "theverge": ["news", "tech news", "科技新闻"],
}

SOURCE_PRIORITY_RULES = [
    (["最新", "新闻", "消息", "资讯", "热搜", "热点", "今天", "今日", "实时", "latest", "news", "today", "recent", "current"], ["news", "baidu", "toutiao", "weibo", "bili"]),
    (["ios", "ipados", "macos", "watchos", "visionos", "iphone", "ipad", "macbook", "apple", "苹果", "保修", "维修", "支持"], ["apple_cn", "apple_support", "apple"]),
    (["github", "repo", "repository", "release", "issue", "pull request", "开源项目", "项目"], ["github"]),
    (["百科", "是什么", "wiki", "wikipedia", "词条"], ["wiki", "wiki_en", "baike"]),
    (["抖音", "bilibili", "b站", "短视频", "视频", "探店"], ["bili", "douyin", "xiaohongshu"]),
    (["报错", "错误", "代码", "编程", "开发", "python", "java", "javascript", "pyinstaller"], ["csdn", "gitee", "cnblogs", "python", "stackoverflow"]),
    (["购买", "价格", "商品", "二手"], ["jd", "taobao", "xianyu", "zhihu"]),
    (["新闻", "热点", "热搜"], ["baidu", "toutiao", "weibo", "bili"]),
]

OFFICIAL_SOURCE_DOMAINS = {
    "apple_cn": ["apple.com.cn", "support.apple.com", "developer.apple.com", "apple.com"],
    "apple": ["apple.com", "support.apple.com", "developer.apple.com", "apple.com.cn"],
    "apple_support": ["support.apple.com", "apple.com", "developer.apple.com", "apple.com.cn"],
    "microsoft": ["microsoft.com", "support.microsoft.com", "learn.microsoft.com", "windows.com"],
    "learn_ms": ["learn.microsoft.com", "support.microsoft.com", "microsoft.com"],
    "openai": ["openai.com", "platform.openai.com", "help.openai.com", "community.openai.com"],
    "python": ["docs.python.org", "python.org", "peps.python.org", "pypi.org"],
    "pypi": ["pypi.org", "docs.pypi.org", "packaging.python.org"],
    "github": ["github.com", "docs.github.com", "github.blog"],
}

SEARCH_STOPWORDS = {
    "search", "please", "about", "what", "when", "where", "which", "how", "the", "and", "for", "with",
    "搜索", "查询", "查找", "联网", "一下", "请问", "请", "关于", "資料", "资料", "搜尋",
    "検索", "について", "調べて", "ください",
    "recherche", "chercher", "sur", "pour", "bitte", "suche", "suchen", "über",
}

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

SEARCH_ENGINES = [
    {"name": "Safari", "kind": "google", "url": "https://www.google.com/search?q={q}&hl=zh-CN"},
    {"name": "Bing", "kind": "bing", "url": "https://www.bing.com/search?q={q}"},
    {"name": "Edge", "kind": "bing", "url": "https://cn.bing.com/search?q={q}"},
]

FRESH_SEARCH_PATTERNS = [
    r"最新", r"新闻", r"消息", r"资讯", r"热搜", r"热点", r"今天", r"今日", r"刚刚", r"实时", r"現在",
    r"\blatest\b", r"\bnews\b", r"\brecent\b", r"\btoday\b", r"\bcurrent\b", r"\bbreaking\b", r"\bnow\b",
    r"actualité", r"noticias", r"nachrichten", r"новости", r"ニュース",
]

NO_AUTO_SEARCH_PATTERNS = [
    r"翻译", r"譯", r"翻譯", r"translate", r"translation", r"怎么说", r"怎麼說",
    r"用.*(英语|英文|日语|日文|法语|德语|繁体|簡體|中文).*说",
    r"用.*(english|japanese|french|german|chinese).*",
    r"こんにちは|你好|您好|hello|hi\b|thanks|thank you|谢谢|謝謝",
    r"解释一下|summarize|总结一下|改写|润色|proofread|rewrite",
]


def clean_html(text):
    text = re.sub(r"<.*?>", "", text)
    text = unescape(text)
    text = text.replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def strip_urls(text):
    text = re.sub(r"https?://\S+", "", text or "")
    text = re.sub(r"www\.\S+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_result_text(text):
    return strip_urls(clean_html(text))


def safe_get(url, *, params=None, timeout=8):
    try:
        response = requests.get(
            url,
            params=params,
            headers=HTTP_HEADERS,
            timeout=(4, timeout),
            allow_redirects=True,
        )
        response.raise_for_status()
        return response
    except requests.exceptions.RequestException:
        return None


def text_keywords_for_scoring(text):
    keywords = []
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.+-]{1,}|[\u4e00-\u9fff]{2,}|[\u3040-\u30ff]{2,}|[\uac00-\ud7af]{2,}", text or ""):
        token = token.strip("._-").lower()
        if token and token not in SEARCH_STOPWORDS and token not in keywords:
            keywords.append(token)
    return keywords[:18]


def split_readable_chunks(html):
    html = re.sub(r"(?is)<(script|style|noscript|svg|header|footer|nav|form|aside).*?</\1>", " ", html or "")
    html = re.sub(r"(?is)<!--.*?-->", " ", html)
    title_match = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    meta_match = re.search(r'(?is)<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', html)
    candidates = []
    if title_match:
        candidates.append(clean_html(title_match.group(1)))
    if meta_match:
        candidates.append(clean_html(meta_match.group(1)))
    for chunk in re.findall(r"(?is)<(?:h1|h2|h3|p|li|td|th|article|section)[^>]*>(.*?)</(?:h1|h2|h3|p|li|td|th|article|section)>", html):
        text = clean_html(chunk)
        if 25 <= len(text) <= 1800:
            candidates.append(text)
    if not candidates:
        candidates.append(clean_html(html))
    cleaned = []
    seen = set()
    noise = re.compile(
        r"(cookie|cookies|privacy policy|terms of use|sign in|log in|subscribe|advertisement|验证码|登录|注册|隐私|广告|跳转|加载中)",
        re.IGNORECASE,
    )
    for item in candidates:
        item = strip_urls(re.sub(r"\s+", " ", item).strip())
        if len(item) < 25 or noise.search(item):
            continue
        key = item[:120].lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(item)
    return cleaned


def extract_relevant_page_text(html, query="", max_chars=1400):
    chunks = split_readable_chunks(html)
    keywords = text_keywords_for_scoring(query)
    if not keywords:
        return " ".join(chunks)[:max_chars].strip()

    scored = []
    for index, chunk in enumerate(chunks):
        lower = chunk.lower()
        score = 0
        for keyword in keywords:
            if keyword in lower:
                score += 4 if len(keyword) >= 4 else 2
        if re.search(r"\b(20\d{2}|19\d{2})[-/.年]\d{1,2}", chunk):
            score += 1
        if index < 3:
            score += 1
        scored.append((score, index, chunk))

    selected = [chunk for score, _index, chunk in sorted(scored, key=lambda item: (-item[0], item[1])) if score > 0]
    if len(selected) < 3:
        selected.extend(chunk for _score, _index, chunk in scored[:6] if chunk not in selected)

    text = " ".join(selected)
    return re.sub(r"\s+", " ", text).strip()[:max_chars]


def extract_ddg_url(url):
    # DuckDuckGo 有时会返回 /l/?uddg=真实网址
    if "uddg=" in url:
        try:
            return unquote(url.split("uddg=", 1)[1].split("&", 1)[0])
        except Exception:
            return url
    return url


def extract_search_keywords(text):
    text = re.sub(r"^/search\b", "", text, flags=re.IGNORECASE).strip()
    parts = text.split(maxsplit=1)
    if parts and parts[0].lower() in SEARCH_SOURCES:
        text = parts[1].strip() if len(parts) > 1 else ""

    intent_patterns = [
        r"请帮我搜索", r"帮我搜索", r"帮我查一下", r"请查一下", r"查一下", r"搜索一下",
        r"请搜索", r"搜索", r"查询", r"查找", r"联网搜索", r"关于",
        r"please search", r"search for", r"look up", r"find information about",
        r"調べてください", r"検索してください", r"検索", r"について",
        r"recherche(?:r)?", r"chercher", r"suche", r"suchen",
    ]
    for pattern in intent_patterns:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)

    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^\w\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af.-]+", " ", text, flags=re.UNICODE)
    tokens = [item.strip("._-").lower() for item in text.split()]
    keywords = []

    for token in tokens:
        if not token or token in SEARCH_STOPWORDS:
            continue
        if len(token) <= 1 and not re.search(r"[\u4e00-\u9fff\u3040-\u30ff]", token):
            continue
        if token not in keywords:
            keywords.append(token)

    if not keywords:
        return text.strip()

    return " ".join(keywords[:10]).strip()


def should_auto_search(question):
    text = question.strip()
    compact = re.sub(r"\s+", "", text)

    if len(compact) < 4:
        return False

    if text.startswith("/"):
        return False

    for pattern in NO_AUTO_SEARCH_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return False

    if extract_urls(text):
        return True

    if is_fresh_query(text):
        return True

    keyword = extract_search_keywords(text)
    if len(re.sub(r"\s+", "", keyword)) < 4:
        return False

    return True


def is_fresh_query(query):
    text = query or ""
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in FRESH_SEARCH_PATTERNS)


def is_useful_url(url):
    if not url:
        return False

    lower_url = url.lower()
    if "baidu.com/link" in lower_url:
        return True
    if "google.com/url?" in lower_url and "q=" in lower_url:
        return True

    bad_parts = [
        "duckduckgo.com/y.js",
        "javascript:",
        "mailto:",
        "/settings",
        "/feedback",
        "baidu.com/s?",
        "www.baidu.com",
        "google.com/search",
        "google.com/preferences",
        "google.com/advanced_search",
        "google.com.hk/search",
        "so.com/link",
        "so.com/clk",
        "so.com/s?",
        "so.com/suggest",
        "cache.so.com",
        "www.so.com",
        "hao.360.com",
        "360.cn",
        "baoku.360.cn",
        "wenda.so.com",
    ]
    if any(part in lower_url for part in bad_parts):
        return False

    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def fetch_page_summary(url, max_chars=1400, query=""):
    if not is_useful_url(url):
        return ""

    try:
        res = safe_get(url, timeout=8)
        if res is None:
            return ""
        content_type = res.headers.get("Content-Type", "").lower()
        if "text/html" not in content_type and "text/plain" not in content_type:
            return ""

        text = extract_relevant_page_text(res.text, query=query, max_chars=max_chars)

        return text[:max_chars] if len(text) >= 60 else ""
    except Exception:
        return ""


def readable_html_text(html):
    html = re.sub(r"(?is)<(script|style|noscript|svg|header|footer|nav|form).*?</\1>", " ", html or "")
    html = re.sub(r"(?is)<!--.*?-->", " ", html)
    html = re.sub(r"(?is)</(?:tr|p|li|h1|h2|h3|td|th)>", ". ", html)
    text = clean_html(html)
    text = re.sub(
        r"(cookie|cookies|privacy policy|terms of use|sign in|log in|subscribe|advertisement|验证码|登录|注册|隐私|广告)",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    return strip_urls(re.sub(r"\s+", " ", text).strip())


def apple_software_version_query(query):
    text = (query or "").lower()
    has_apple_os = any(
        token in text
        for token in ["macos", "ios", "ipados", "watchos", "visionos", "tvos", "苹果系统", "苹果版本", "mac 系统", "mac系统"]
    )
    has_version_intent = any(
        token in text
        for token in ["最新", "当前", "目前", "现在", "版本", "latest", "current", "newest", "recent"]
    )
    return has_apple_os and has_version_intent


def apple_requested_products(query):
    text = (query or "").lower()
    product_map = [
        ("macOS", ["macos", "mac os", "mac 系统", "mac系统"]),
        ("iOS", ["ios", "iphone"]),
        ("iPadOS", ["ipados", "ipad"]),
        ("watchOS", ["watchos", "apple watch"]),
        ("visionOS", ["visionos", "vision pro"]),
        ("tvOS", ["tvos", "apple tv"]),
    ]
    products = [name for name, tokens in product_map if any(token in text for token in tokens)]
    return products or ["macOS", "iOS", "iPadOS", "watchOS", "visionOS", "tvOS"]


def extract_product_version_context(text, product):
    if product == "macOS":
        patterns = [
            r"(macOS\s+[A-Za-z][A-Za-z ]{1,30}\s+\d+\W{0,12}\d+(?:\.\d+){1,3})",
            r"(macOS\s+[A-Za-z][A-Za-z ]{1,30}\s+\d+(?:\.\d+){1,3})",
        ]
    else:
        patterns = [
            rf"({re.escape(product)}\s+\d+(?:\.\d+){{0,3}}(?:\s+[A-Za-z][A-Za-z ]{{1,30}})?(?:\s+\d+(?:\.\d+){{1,3}})?)",
        ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            start = max(match.start() - 180, 0)
            end = min(match.end() + 520, len(text))
            return text[start:end].strip()
    if product == "macOS":
        named = re.search(r"macOS\s+[A-Za-z][A-Za-z ]{1,30}", text, flags=re.IGNORECASE)
        if named:
            start = max(named.start() - 80, 0)
            end = min(named.end() + 700, len(text))
            return text[start:end].strip()
    idx = text.lower().find(product.lower())
    if idx >= 0:
        return text[max(idx - 180, 0): min(idx + 700, len(text))].strip()
    return ""


def apple_software_version_results(query):
    if not apple_software_version_query(query):
        return []

    pages = [
        {
            "title": "Find out which macOS your Mac is using",
            "url": "https://support.apple.com/en-us/109033",
            "products": ["macOS"],
        },
        {
            "title": "Apple security releases",
            "url": "https://support.apple.com/en-us/100100",
            "products": ["macOS", "iOS", "iPadOS", "watchOS", "visionOS", "tvOS"],
        },
    ]
    requested = set(apple_requested_products(query))
    items = []
    seen = set()

    for page in pages:
        products = requested.intersection(page["products"])
        if not products:
            continue
        res = safe_get(page["url"], timeout=10)
        if res is None:
            continue
        text = readable_html_text(res.text)
        if len(text) < 80:
            continue
        for product in products:
            context = extract_product_version_context(text, product)
            if not context:
                continue
            key = (page["url"], product)
            if key in seen:
                continue
            seen.add(key)
            items.append({
                "title": f"{product} 官方版本信息 - {page['title']}",
                "url": page["url"],
                "snippet": context[:260],
                "content": context[:1000],
            })

    if not items:
        return []
    return [{
        "key": "apple_software_versions",
        "name": "Apple Support 官方版本信息",
        "type": "官方",
        "fallback_url": "https://support.apple.com/",
        "results": items,
    }]


def resolve_result_url(url):
    if not url:
        return ""

    lower = url.lower()
    if "google.com/url?" in lower and "q=" in lower:
        try:
            return unquote(url.split("q=", 1)[1].split("&", 1)[0])
        except Exception:
            return url

    if "baidu.com/link" in lower:
        res = safe_get(url, timeout=5)
        if res is not None and res.url:
            return res.url

    return url


def duckduckgo_search(query, limit=5, engine_url="https://duckduckgo.com/html/?q={q}"):
    url = engine_url.format(q=quote(query))

    try:
        res = safe_get(url, timeout=8)
        if res is None:
            return []
        html = res.text

        results = []
        blocks = re.findall(r'(?is)<div class="result.*?</div>\s*</div>', html)

        if not blocks:
            blocks = re.findall(r'(?is)<a rel="nofollow" class="result__a" href="(.*?)">(.*?)</a>', html)

        for block in blocks:
            if isinstance(block, tuple):
                link, title = block
                snippet = ""
            else:
                link_match = re.search(r'(?is)<a rel="nofollow" class="result__a" href="(.*?)">(.*?)</a>', block)
                if not link_match:
                    continue
                link, title = link_match.groups()
                snippet_match = re.search(r'(?is)<a class="result__snippet"[^>]*>(.*?)</a>', block)
                snippet = clean_result_text(snippet_match.group(1)) if snippet_match else ""

            title = clean_result_text(title)
            link = resolve_result_url(extract_ddg_url(unescape(link)))

            if not title or not is_useful_url(link):
                continue

            results.append({
                "title": title,
                "url": link,
                "snippet": snippet,
            })

            if len(results) >= limit:
                break

        return results

    except Exception:
        return []


def google_search(query, limit=5, engine_url="https://www.google.com/search?q={q}&hl=zh-CN"):
    url = engine_url.format(q=quote(query))

    try:
        res = safe_get(url, timeout=8)
        if res is None:
            return []
        html = res.text

        results = []
        blocks = re.findall(r'(?is)<div class="g".*?</div>\s*</div>', html)
        if not blocks:
            blocks = re.findall(r'(?is)<a href="(/url\?q=|https?://)(.*?)".*?</a>', html)

        for block in blocks:
            if isinstance(block, tuple):
                prefix, link = block
                if prefix == "/url?q=":
                    link = link.split("&", 1)[0]
                title = ""
                snippet = ""
            else:
                link_match = re.search(r'(?is)<a href="(?:/url\?q=)?(https?://[^"&]+)[^"]*"[^>]*>.*?<h3[^>]*>(.*?)</h3>', block)
                if not link_match:
                    continue
                link, title = link_match.groups()
                snippet_match = re.search(r'(?is)<div[^>]+(?:VwiC3b|IsZvec|aCOpRe)[^>]*>(.*?)</div>', block)
                snippet = clean_result_text(snippet_match.group(1)) if snippet_match else ""

            link = resolve_result_url(unquote(unescape(link)))
            title = clean_result_text(title)
            snippet = clean_result_text(snippet)

            if not title or not is_useful_url(link):
                continue

            results.append({
                "title": title,
                "url": link,
                "snippet": snippet,
            })

            if len(results) >= limit:
                break

        return results
    except Exception:
        return []


def baidu_search(query, limit=5, engine_url="https://www.baidu.com/s?wd={q}"):
    url = engine_url.format(q=quote(query))

    try:
        res = safe_get(url, timeout=8)
        if res is None:
            return []
        html = res.text

        results = []
        blocks = re.findall(r'(?is)<div[^>]+class="[^"]*(?:result|c-container)[^"]*".*?</div>\s*</div>', html)
        if not blocks:
            blocks = re.findall(r'(?is)<h3[^>]*>.*?</h3>.*?(?:<div[^>]*class="[^"]*c-abstract[^"]*"[^>]*>.*?</div>)?', html)

        for block in blocks:
            if re.search(r"(?i)(广告|推广|商业推广|百度快照|相关搜索)", clean_html(block)):
                continue

            link_match = re.search(r'(?is)<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', block)
            if not link_match:
                continue

            link, title = link_match.groups()
            title = clean_result_text(title)
            snippet_match = re.search(r'(?is)<div[^>]+class="[^"]*(?:c-abstract|content-right|result-desc)[^"]*"[^>]*>(.*?)</div>', block)
            snippet = clean_result_text(snippet_match.group(1)) if snippet_match else ""
            link = resolve_result_url(unescape(link))

            if not title or not is_useful_url(link):
                continue

            results.append({
                "title": title,
                "url": link,
                "snippet": snippet,
            })

            if len(results) >= limit:
                break

        return results
    except Exception:
        return []


def bing_search(query, limit=5, engine_url="https://cn.bing.com/search?q={q}"):
    url = engine_url.format(q=quote(query))

    try:
        res = safe_get(url, timeout=8)
        if res is None:
            return []
        html = res.text

        results = []
        blocks = re.findall(r'(?is)<li class="b_algo".*?</li>', html)

        for block in blocks:
            link_match = re.search(r'(?is)<h2[^>]*>\s*<a[^>]+href="(.*?)"[^>]*>(.*?)</a>', block)
            if not link_match:
                continue

            link, title = link_match.groups()
            title = clean_result_text(title)
            snippet_match = re.search(r'(?is)<p[^>]*>(.*?)</p>', block)
            snippet = clean_result_text(snippet_match.group(1)) if snippet_match else ""
            link = resolve_result_url(unescape(link))

            if not title or not is_useful_url(link):
                continue

            results.append({
                "title": title,
                "url": link,
                "snippet": snippet,
            })

            if len(results) >= limit:
                break

        return results
    except Exception:
        return []


def bing_news_search(query, limit=5, engine_url="https://www.bing.com/news/search?q={q}&format=rss"):
    url = engine_url.format(q=quote(query))
    try:
        res = safe_get(url, timeout=8)
        if res is None:
            return []

        root = ET.fromstring(res.content)
        results = []
        for item in root.findall(".//item"):
            title = clean_result_text(item.findtext("title", ""))
            description = clean_result_text(item.findtext("description", ""))
            link = resolve_result_url(item.findtext("link", ""))
            published = clean_result_text(item.findtext("pubDate", ""))

            if not title or not is_useful_url(link):
                continue

            snippet = description
            if published:
                snippet = f"{published}｜{snippet}" if snippet else published

            results.append({"title": title, "url": link, "snippet": snippet})
            if len(results) >= limit:
                break

        return results
    except Exception:
        return []


def so_search(query, limit=5, engine_url="https://www.so.com/s?q={q}"):
    url = engine_url.format(q=quote(query))

    try:
        res = safe_get(url, timeout=8)
        if res is None:
            return []
        html = res.text

        results = []
        blocks = re.findall(r'(?is)<li[^>]+class="[^"]*(?:res-list|result)[^"]*".*?</li>', html)
        if not blocks:
            blocks = re.findall(r'(?is)<h3[^>]*>.*?</h3>.*?(?:<p[^>]*>.*?</p>)?', html)

        for block in blocks:
            if re.search(r"(?i)(广告|推广|赞助|商业推广|快照|360问答|相关搜索)", clean_html(block)):
                continue

            link_match = re.search(r'(?is)<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', block)
            if not link_match:
                continue

            link, title = link_match.groups()
            link = resolve_result_url(unescape(link))
            title = clean_result_text(title)
            snippet_match = re.search(r'(?is)<p[^>]*>(.*?)</p>', block)
            snippet = clean_result_text(snippet_match.group(1)) if snippet_match else ""

            if not title or not is_useful_url(link):
                continue
            if not snippet and len(title) < 6:
                continue

            results.append({
                "title": title,
                "url": link,
                "snippet": snippet,
            })

            if len(results) >= limit:
                break

        return results
    except Exception:
        return []


def generic_anchor_search_results(query, limit=5, engine_url=None, html=None):
    if html is None:
        if not engine_url:
            return []
        try:
            search_url = engine_url.format(q=quote(query))
            res = safe_get(search_url, timeout=8)
            if res is None:
                return []
            html = res.text
        except Exception:
            return []
    else:
        search_url = engine_url.format(q=quote(query)) if engine_url else ""

    base_url = search_url or ""
    results = []
    seen = set()
    skip_hosts = (
        "google.com", "www.google.com", "bing.com", "www.bing.com", "cn.bing.com",
        "baidu.com", "www.baidu.com", "so.com", "www.so.com",
    )
    for href, title in re.findall(r'(?is)<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html or ""):
        title = clean_result_text(title)
        if not title or len(title) < 2:
            continue
        link = resolve_result_url(unescape(href))
        if base_url:
            link = urljoin(base_url, link)
        parsed = urlparse(link)
        host = parsed.netloc.lower().split(":", 1)[0]
        if host in skip_hosts:
            continue
        if link in seen or not is_useful_url(link):
            continue
        if re.search(r"(?i)(登录|登入|注册|反馈|设置|广告|推广|隐私|条款|privacy|terms|settings|feedback|cached|translate)", title):
            continue
        if len(title.strip()) <= 3 and not re.search(r"[A-Za-z0-9]{3,}|[\u4e00-\u9fff]{4,}", title):
            continue
        seen.add(link)
        results.append({"title": title, "url": link, "snippet": ""})
        if len(results) >= limit:
            break
    return results


def search_page_text_result(source, query, max_chars=900):
    search_url = source.get("search_url", "").format(q=quote(query))
    if not search_url:
        return None
    try:
        res = safe_get(search_url, timeout=8)
        if res is None:
            return None
        text = clean_html(res.text)
        text = re.sub(
            r"(登录|注册|广告|推广|cookie|privacy policy|terms of use|subscribe|sign in|log in)",
            " ",
            text,
            flags=re.IGNORECASE,
        )
        text = strip_urls(re.sub(r"\s+", " ", text).strip())
        if len(text) < 80:
            return None
        head = text[:600]
        if re.search(r"(?i)(window\.|function\s*\(|const\s+|var\s+|metadata\s*:|webpack|__next_data__|schema\.org)", head):
            return None
        if head.count("{") + head.count("}") + head.count(";") > 18:
            return None
        return {
            "title": f"{source.get('name', 'Search')} 搜索页摘要",
            "url": search_url,
            "snippet": text[:240],
            "content": text[:max_chars],
        }
    except Exception:
        return None


def result_matches_source(item, source, source_key=None):
    domains = OFFICIAL_SOURCE_DOMAINS.get(source_key, []) if source_key else []
    if not domains:
        domain = (source.get("domain") or "").lower()
        domains = [domain] if domain else []
    domains = [domain.lower() for domain in domains if domain]
    if not domains:
        return True
    try:
        host = urlparse(item.get("url", "")).netloc.lower().split(":", 1)[0]
    except Exception:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def merge_search_results(groups, limit=8):
    merged = []
    seen = set()
    for results in groups:
        for item in results or []:
            url = item.get("url", "")
            title = clean_result_text(item.get("title", ""))
            key = (url or title).lower()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
            if len(merged) >= limit:
                return merged
    return merged


def general_search(query, limit=5, collect_all=False):
    collected = []
    for engine in SEARCH_ENGINES:
        kind = engine["kind"]
        engine_url = engine["url"]

        if kind == "baidu":
            results = baidu_search(query, limit=limit, engine_url=engine_url)
        elif kind == "bing":
            results = bing_search(query, limit=limit, engine_url=engine_url)
        elif kind == "google":
            results = google_search(query, limit=limit, engine_url=engine_url)
        elif kind == "so":
            results = so_search(query, limit=limit, engine_url=engine_url)
        else:
            results = duckduckgo_search(query, limit=limit, engine_url=engine_url)

        if not results:
            results = generic_anchor_search_results(query, limit=limit, engine_url=engine_url)

        if results:
            if not collect_all:
                return results
            collected.append(results)

    return merge_search_results(collected, limit=limit) if collect_all else []


def official_domain_search(source_key, query, limit=5):
    domains = OFFICIAL_SOURCE_DOMAINS.get(source_key) or [SEARCH_SOURCES[source_key].get("domain", "")]
    result_groups = []
    for domain in domains:
        if not domain:
            continue
        result_groups.append(general_search(f"site:{domain} {query}", limit=limit, collect_all=True))
    return merge_search_results(result_groups, limit=max(limit, 8))


def wikipedia_search(query, limit=3):
    # 维基用官方 API，稳定性比网页抓取高
    api = "https://zh.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "format": "json",
        "utf8": 1,
        "srlimit": limit
    }

    try:
        res = safe_get(api, params=params, timeout=8)
        if res is None:
            return []
        data = res.json()

        results = []
        for item in data.get("query", {}).get("search", []):
            title = item.get("title", "")
            snippet = clean_result_text(item.get("snippet", ""))
            url = "https://zh.wikipedia.org/wiki/" + quote(title.replace(" ", "_"))

            results.append({
                "title": title,
                "url": url,
                "snippet": snippet
            })

        return results

    except Exception:
        return []


def enrich_search_results(results, max_pages=3, query=""):
    enriched = []
    seen_urls = set()

    for item in results:
        url = item.get("url", "")
        if not is_useful_url(url) or url in seen_urls:
            continue

        seen_urls.add(url)
        item = item.copy()
        if len(enriched) < max_pages:
            page_text = fetch_page_summary(url, max_chars=1800, query=query)
            if page_text:
                item["content"] = page_text

        if item.get("snippet") or item.get("content"):
            enriched.append(item)

    return enriched


def search_one_source(source_key, query, limit=5):
    source = SEARCH_SOURCES[source_key]

    if source_key == "wiki":
        results = wikipedia_search(query, limit=limit)
    elif source_key == "news":
        results = bing_news_search(query, limit=limit, engine_url=source["search_url"])
    elif SOURCE_TYPES.get(source_key) == "官方":
        results = official_domain_search(source_key, query, limit=limit)
    else:
        # 用搜索引擎的 site: 检索，避免直接抓取百度/淘宝/京东/闲鱼等页面时被反爬拦截。
        # 大陆网络下优先尝试百度和 Bing，再尝试 Google/DuckDuckGo，最后用 360 兜底。
        results = general_search(f"site:{source['domain']} {query}", limit=limit, collect_all=True)

    fallback_url = source["search_url"].format(q=quote(query))
    if not results:
        results = generic_anchor_search_results(query, limit=limit, engine_url=source["search_url"])
    results = [item for item in results if result_matches_source(item, source, source_key)]

    enriched = enrich_search_results(results, max_pages=3, query=query)
    if not has_effective_search_results([{"results": enriched}]):
        page_item = search_page_text_result(source, query)
        if page_item:
            enriched = [page_item]

    return {
        "key": source_key,
        "name": source["name"],
        "type": SOURCE_TYPES.get(source_key, "不确定"),
        "fallback_url": fallback_url,
        "results": enriched
    }


def search_source_group(keys, query, limit_per_source):
    results = []
    for key in keys:
        item = search_one_source(key, query, limit=limit_per_source)
        results.append(item)
    return results


def direct_url_results(query, max_pages=3):
    items = []
    seen = set()
    for url in extract_urls(query):
        clean_url = url.rstrip(").,，。；;]")
        if clean_url in seen or not is_useful_url(clean_url):
            continue
        seen.add(clean_url)
        content = fetch_page_summary(clean_url, max_chars=1600, query=query)
        if content:
            host = urlparse(clean_url).netloc.lower()
            items.append({
                "title": host,
                "url": clean_url,
                "snippet": content[:240],
                "content": content,
            })
        if len(items) >= max_pages:
            break
    if not items:
        return []
    return [{
        "key": "direct_url",
        "name": "用户提供的网址",
        "type": "不确定",
        "fallback_url": "",
        "results": items,
    }]


def select_search_sources(query, source_key="auto"):
    if source_key == "all":
        return list(SEARCH_SOURCES.keys())

    if source_key != "auto":
        return [source_key] if source_key in SEARCH_SOURCES else []

    lower = query.lower()
    selected = []

    for keywords, sources in SOURCE_PRIORITY_RULES:
        if any(keyword.lower() in lower for keyword in keywords):
            for key in sources:
                if key in SEARCH_SOURCES and key not in selected:
                    selected.append(key)

    for key, keywords in SOURCE_KEYWORDS.items():
        if key not in SEARCH_SOURCES:
            continue
        if key not in selected and any(keyword.lower() in lower for keyword in keywords):
            selected.append(key)

    for key in DEFAULT_SEARCH_SOURCES:
        if key not in selected and key in SEARCH_SOURCES:
            selected.append(key)
        if len(selected) >= 8:
            break

    selected = sorted(
        selected,
        key=lambda key: SOURCE_TYPE_ORDER.get(SOURCE_TYPES.get(key, "不确定"), 2)
    )

    return selected[: int(WEB_FEATURES.get("auto_source_limit", 8))]


def web_search(query, source_key="all", limit_per_source=3):
    direct_results = direct_url_results(query)
    if source_key in ("auto", "all") and has_effective_search_results(direct_results):
        return direct_results, None

    apple_version_results = apple_software_version_results(query)
    if source_key in ("auto", "all") and has_effective_search_results(apple_version_results):
        return apple_version_results, None

    keys = select_search_sources(query, source_key=source_key)
    if not keys:
        return None, f"Unknown search source: {source_key}"

    if source_key == "all":
        all_results = search_source_group(keys, query, limit_per_source)
        all_results.sort(
            key=lambda item: SOURCE_TYPE_ORDER.get(item.get("type", "不确定"), 2)
        )
        return all_results, None

    official_keys = [key for key in keys if SOURCE_TYPES.get(key) == "官方"]
    other_keys = [key for key in keys if key not in official_keys]

    official_results = search_source_group(official_keys, query, limit_per_source)
    if has_effective_search_results(official_results):
        return official_results, None

    other_results = search_source_group(other_keys, query, limit_per_source)
    return official_results + other_results, None


def search_query_for_web(user_question, raw_keyword=None):
    source_text = raw_keyword or user_question
    if extract_urls(source_text):
        return source_text.strip()
    return extract_search_keywords(source_text)


def has_effective_search_results(results):
    if not results:
        return False

    for group in results:
        for item in group.get("results", []):
            text = " ".join([
                item.get("title", ""),
                item.get("snippet", ""),
                item.get("content", ""),
            ]).strip()
            if len(text) >= 35:
                return True

    return False


def format_search_results(results):
    lines = []

    for group in results:
        source_type = group.get("type", "不确定")
        lines.append(f"\n【{group['name']}｜来源类型：{source_type}】")

        if not group["results"]:
            lines.append("- 未抓取到有效结果")
            continue

        for i, item in enumerate(group["results"], 1):
            title = clean_result_text(item.get("title", "无标题"))
            snippet = clean_result_text(item.get("snippet", ""))

            lines.append(f"{i}. {title}")
            if snippet:
                lines.append(f"   摘要：{snippet}")
            content = clean_result_text(item.get("content", ""))
            if content:
                lines.append(f"   正文摘录：{content}")

    return "\n".join(lines).strip()


def build_search_prompt(query, results_text, lang="zh_cn", has_results=True):
    profile = get_language_profile(lang)
    current_date = datetime.now().strftime("%Y-%m-%d")
    result_rule = (
        "Usable web results are provided below. Summarize only from them."
        if has_results
        else "No usable web results were retrieved. Tell the user clearly that no effective results were obtained, then answer cautiously from general knowledge if possible."
    )
    return f"""
You are LocalAI SE's web search summarization assistant.

Important rules:
1. Reply in {profile["model_language"]} by default.
2. Remind the user that web search results may be inaccurate and should be verified.
3. Summarize only from the search results. Do not present uncertain content as fact.
4. If the search results are insufficient, say so clearly.
5. For products, second-hand listings, prices, or hardware specifications, remind the user to verify the item and page details.
6. {result_rule}
7. In your answer, explicitly state the source type used: 官方 / 第三方 / 不确定. If sources are mixed, say that clearly.

User question:
{query}

Current date:
{current_date}

Web search results:
{results_text}

Please answer based on the results above:
"""


def answer_with_web_search(user_question, raw_keyword, source_key, model, size, config):
    keyword = search_query_for_web(user_question, raw_keyword)

    if keyword:
        print(tr(config, "search_keywords", keywords=keyword))
        print(tr(config, "searching"))
        results, error = web_search(keyword, source_key=source_key)
        if error:
            print(f"❌ {error}")
            results = []
    else:
        results = []

    has_results = has_effective_search_results(results)
    results_text = format_search_results(results) if has_results else ""

    if not has_results:
        print(tr(config, "search_no_effective_results"))
        results_text = "No effective web search results were retrieved."

    prompt = build_search_prompt(
        user_question,
        results_text,
        config.get("language", "zh_cn"),
        has_results=has_results,
    )

    print(tr(config, "search_summarizing"))
    start_time = time.time()
    answer = ask_local(prompt, model, size)
    elapsed = time.time() - start_time

    if not answer:
        answer = tr(config, "empty_answer")

    print(tr(config, "search_ai_label", elapsed=elapsed))
    print(answer)
    print("-" * 50)

    return answer


def answer_with_web_search_for_gui(user_question, model, size, config):
    keyword = search_query_for_web(user_question)
    results = []

    if keyword:
        results, error = web_search(keyword, source_key="auto")
        if error:
            results = []

    has_results = has_effective_search_results(results)
    results_text = format_search_results(results) if has_results else "No effective web search results were retrieved."

    prompt = build_search_prompt(
        user_question,
        results_text,
        config.get("language", "zh_cn"),
        has_results=has_results,
    )

    answer = ask_local(prompt, model, size)
    return answer or tr(config, "empty_answer")


def parse_search_command(q):
    # 支持：
    # /search 关键词
    # /search wiki 关键词
    # /search github 关键词
    parts = q.split(maxsplit=2)

    if len(parts) == 1:
        return None, ""

    if len(parts) == 2:
        return "all", parts[1].strip()

    possible_source = parts[1].strip().lower()
    if possible_source in SEARCH_SOURCES or possible_source == "all":
        return possible_source, parts[2].strip()

    return "all", q.replace("/search", "", 1).strip()


def show_help(config):
    code = get_lang(config)
    print(f"\n{tr(config, 'commands')}：\n")
    for command, description in COMMAND_HELP.get(code, COMMAND_HELP["zh_cn"]):
        print(f"{command:<13}{description}")
    print(f"\n{DRAG_FILE_HELP.get(code, DRAG_FILE_HELP['zh_cn'])}")


def build_se_tutorial_lines(config):
    code = get_lang(config)
    lines = [
        tr(config, "se_tutorial_title"),
        "",
        tr(config, "se_tutorial_intro"),
        "",
        f"{tr(config, 'commands')}：",
        "",
    ]
    for command, description in COMMAND_HELP.get(code, COMMAND_HELP["zh_cn"]):
        lines.append(f"{command:<13} {description}")
    lines.extend(["", DRAG_FILE_HELP.get(code, DRAG_FILE_HELP["zh_cn"])])
    return lines


def get_desktop_dir():
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    return desktop if os.path.isdir(desktop) else os.path.expanduser("~")


def tutorial_font(size=22):
    candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/seguiemj.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    try:
        from PIL import ImageFont
        for path in candidates:
            if os.path.exists(path):
                return ImageFont.truetype(path, size)
        return ImageFont.load_default()
    except Exception:
        return None


def save_se_tutorial_screenshot(config):
    lines = build_se_tutorial_lines(config)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    desktop = get_desktop_dir()
    image_path = os.path.join(desktop, f"LocalAI_SE_Commands_{timestamp}.png")
    text_path = os.path.join(desktop, f"LocalAI_SE_Commands_{timestamp}.txt")
    text = "\n".join(lines)

    try:
        from PIL import Image, ImageDraw
        font = tutorial_font(22)
        title_font = tutorial_font(30)
        padding = 36
        line_height = 34
        width = 1180
        height = padding * 2 + 46 + line_height * max(1, len(lines) - 1)
        image = Image.new("RGB", (width, height), "#f8fafc")
        draw = ImageDraw.Draw(image)
        y = padding
        for index, line in enumerate(lines):
            current_font = title_font if index == 0 else font
            fill = "#111827" if index != 0 else "#2563eb"
            draw.text((padding, y), line, font=current_font, fill=fill)
            y += 46 if index == 0 else line_height
        image.save(image_path)
        return image_path, True
    except Exception as exc:
        try:
            log_error(exc)
        except Exception:
            pass
        with open(text_path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return text_path, False


def run_se_first_tutorial(config):
    if config.get("se_tutorial_done", False):
        return

    lines = build_se_tutorial_lines(config)
    print("\n" + "=" * 42)
    for line in lines:
        print(line)
    print("=" * 42)

    choice = input(tr(config, "se_tutorial_prompt")).strip().lower()
    if choice == "/snaphelp":
        path, ok = save_se_tutorial_screenshot(config)
        key = "se_tutorial_saved" if ok else "se_tutorial_failed"
        print(tr(config, key, path=path))

    print(tr(config, "se_tutorial_continue"))
    config["se_tutorial_done"] = True
    save_config(config)


def show_privacy(config):
    code = get_lang(config)
    print(f"\n{tr(config, 'privacy_title')}：\n")
    for i, line in enumerate(PRIVACY_LINES.get(code, PRIVACY_LINES["zh_cn"]), 1):
        print(f"{i}. {line}")


def show_info(model, size, current_path, data, device, recommendation, config):
    print(tr(config, "status_title"))
    print(f"{tr(config, 'version')}：{APP_VERSION_LABEL}")
    print(f"{tr(config, 'model')}：{model}")
    print(f"{tr(config, 'model_level')}：{size}")
    print(f"{tr(config, 'current_chat', title=data.get('title', tr(config, 'unnamed_chat'))).strip()}")
    print(f"{tr(config, 'file')}：{current_path}")
    print(f"{tr(config, 'message_count')}：{len(data.get('messages', []))}")
    print(f"{tr(config, 'device_recommendation')}：{recommendation.get('model')}")
    print()


def main():
    global ACTIVE_CONFIG

    config = load_config()
    ACTIVE_CONFIG = config
    config = first_welcome(config)
    ACTIVE_CONFIG = config

    print(f"""
==============================
🟢 {tr(config, "app_title")} v{APP_VERSION_LABEL}
{tr(config, "app_subtitle")}
==============================
{tr(config, "help_hint")}

{tr(config, "startup_warning")}
""")

    device = detect_device()
    recommendation = evaluate_device(device)

    print_device_report(device, recommendation, config)

    if not maybe_prompt_cloudai_for_low_gpu(config, device):
        return

    if recommendation["level"] == "blocked":
        print(tr(config, "device_blocked_warning"))
        print(tr(config, "force_hint"))
        choice = input(tr(config, "force_prompt")).strip().lower()

        if choice != "force":
            print(tr(config, "goodbye"))
            return

    if config.get("auto_check_update", True):
        check_update(config, silent=True)

    if not config.get("first_run_done", False):
        model = choose_model(config, recommendation)
        config["first_run_done"] = True
        config["last_model"] = model
        save_config(config)
    else:
        model = config.get("last_model") or recommendation.get("model")

    if not model_exists(model):
        print(tr(config, "previous_model_missing", model=model))
        model = choose_model(config, recommendation)
        config["last_model"] = model
        save_config(config)

    run_se_first_tutorial(config)

    size = get_model_size(model)

    print(tr(config, "current_model", model=model, size=size))

    current_path, chat_data = new_chat(config)

    while True:
        print(tr(config, "current_chat", title=chat_data.get('title', tr(config, 'unnamed_chat'))))
        q = multiline_input(config)

        if not q:
            continue

        if q == "/exit":
            print(tr(config, "goodbye"))
            break

        if q == "/help":
            show_help(config)
            continue

        if q == "/snaphelp":
            path, ok = save_se_tutorial_screenshot(config)
            key = "se_tutorial_saved" if ok else "se_tutorial_failed"
            print(tr(config, key, path=path))
            continue

        if q == "/privacy":
            show_privacy(config)
            continue

        if q == "/language":
            choose_language(config)
            continue

        if q == "/checkupdate":
            check_update(config)
            continue

        if q == "/device":
            print_device_report(device, recommendation, config=config, force=True)
            continue

        if q == "/history":
            list_chats(config)
            continue

        if q == "/open":
            result = open_chat(config)

            if result != (None, None):
                current_path, chat_data = result

            continue

        if q == "/new":
            current_path, chat_data = new_chat(config)
            print(tr(config, "new_chat_created"))
            continue

        if q == "/rename":
            title = input(tr(config, "rename_prompt")).strip()

            if title:
                current_path = rename_chat_file(current_path, title)
                chat_data["title"] = safe_title(title)
                save_chat(current_path, chat_data)
                print(tr(config, "renamed"))

            continue

        if q == "/list":
            list_chats(config)
            continue

        if q == "/load":
            files = list_chats(config)
            if not files:
                continue

            idx = input(tr(config, "load_select_prompt")).strip()
            if idx.isdigit():
                i = int(idx) - 1
                if 0 <= i < len(files):
                    current_path, chat_data = load_chat(files[i])
                    print(tr(config, "loaded_chat", title=chat_data.get("title", files[i])))
                else:
                    print(tr(config, "invalid_number"))
            continue

        if q == "/export":
            export_md(current_path, chat_data, config)
            continue

        if q == "/clear":
            chat_data["messages"] = []
            save_chat(current_path, chat_data)
            print(tr(config, "chat_cleared"))
            continue

        if q == "/model":
            model = choose_model(config, recommendation)
            size = get_model_size(model)
            print(tr(config, "model_changed", model=model, size=size))
            continue

        if q == "/info":
            show_info(model, size, current_path, chat_data, device, recommendation, config)
            continue

        if q == "/search" or q.startswith("/search "):
            source_key, raw_keyword = parse_search_command(q)
            if not search_query_for_web(raw_keyword, raw_keyword):
                print(tr(config, "search_no_effective_results"))
                continue
            try:
                answer = answer_with_web_search(raw_keyword, raw_keyword, source_key, model, size, config)
            except requests.exceptions.ConnectionError:
                print(tr(config, "ollama_connection_error"))
                print(tr(config, "run_ollama"))
                print(tr(config, "or_run_model", model=model) + "\n")
                continue
            except Exception as e:
                log_error(e)
                print(tr(config, "generic_error", error_type=type(e).__name__, error=e))
                print(tr(config, "error_written"))
                continue

            messages = chat_data.get("messages", [])
            messages.append({"role": "user", "content": q})
            messages.append({"role": "ai", "content": answer})
            chat_data["messages"] = messages[-MAX_HISTORY_ITEMS:]
            save_chat(current_path, chat_data)
            continue

        try:
            messages = chat_data.get("messages", [])
            imported_paths = parse_dropped_file_paths(q)
            question_text = remove_file_paths_from_text(q, imported_paths) if imported_paths else q
            imported_names = ", ".join(os.path.basename(path) for path in imported_paths)
            display_question = question_text or "请阅读导入的文件"
            if imported_names:
                display_question = f"{display_question}\n[导入文件] {imported_names}"

            if len(messages) == 0:
                title = safe_title(question_text or imported_names or q)
                chat_data["title"] = title
                current_path = rename_chat_file(current_path, title)

            messages.append({"role": "user", "content": display_question})
            chat_data["messages"] = messages[-MAX_HISTORY_ITEMS:]
            save_chat(current_path, chat_data)

            print(tr(config, "thinking"))
            start_time = time.time()

            if imported_paths:
                answer = ask_document(question_text, imported_paths, model, size, messages, config.get("language", "zh_cn"))
            else:
                prompt = build_prompt(q, messages, config.get("language", "zh_cn"))
                answer = ask_local(prompt, model, size)

            elapsed = time.time() - start_time

            if not answer:
                answer = tr(config, "empty_answer")

            print(tr(config, "ai_label", elapsed=elapsed))
            print(answer)
            print("-" * 50)

            messages.append({"role": "ai", "content": answer})
            chat_data["messages"] = messages[-MAX_HISTORY_ITEMS:]
            save_chat(current_path, chat_data)

        except requests.exceptions.ConnectionError:
            print(tr(config, "ollama_connection_error"))
            print(tr(config, "run_ollama"))
            print(tr(config, "or_run_model", model=model) + "\n")

        except Exception as e:
            log_error(e)
            print(tr(config, "generic_error", error_type=type(e).__name__, error=e))
            print(tr(config, "error_written"))


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    
    try:
        main()
    except KeyboardInterrupt:
        print(tr(get_runtime_config(), "keyboard_exit"))
    except Exception as e:
        log_error(e)
        print(tr(get_runtime_config(), "fatal_error"))
