# -*- coding: utf-8 -*-
import base64
import json
import mimetypes
import os
import platform
import re
import sys
import traceback
import zipfile
from datetime import datetime
from xml.etree import ElementTree

import requests

APP_VERSION = "0.8 Beta"
LOCALAI_APP_NAME = "LocalAI"
APP_NAME = "CloudAI"
MASK = "********"


def version_tuple(value):
    parts = []
    for item in re.findall(r"\d+", str(value or "")):
        try:
            parts.append(int(item))
        except Exception:
            break
    return tuple(parts)


def normalized_arch():
    machine = (platform.machine() or "").lower()
    if machine in {"x86_64", "amd64"}:
        return "x64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    if machine in {"i386", "i686", "x86"}:
        return "x86"
    return machine or "unknown"


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
    arch = normalized_arch()
    profile = {
        "system": system,
        "arch": arch,
        "targeted": False,
        "name": system or "Unknown",
        "family": (system or "unknown").lower(),
        "ui_scale_bias": 1.0,
        "scroll_units": 2,
        "font": "Helvetica",
    }

    if system == "Darwin":
        mac_version = platform.mac_ver()[0] or ""
        version = version_tuple(mac_version)
        targeted = version >= (10, 14) if version else True
        profile.update({
            "name": f"macOS {mac_version}" if mac_version else "macOS",
            "family": "macos",
            "targeted": targeted,
            "ui_scale_bias": 1.04 if arch == "arm64" else 1.01,
            "scroll_units": 1,
            "font": "SF Pro Text" if version >= (10, 15) or not version else "Helvetica Neue",
        })
        return profile

    if system == "Windows":
        release = platform.release()
        build = version_tuple(platform.version())
        build_number = build[-1] if build else 0
        major = build[0] if build else 0
        is_x64 = arch == "x64"
        is_arm64 = arch == "arm64"
        win7_sp1_or_newer = release == "7" and build_number >= 7601
        win8_or_newer = release in {"8", "8.1", "10", "11"} or major >= 10
        windows_arm_supported = is_arm64 and (release in {"10", "11"} or major >= 10)
        profile.update({
            "name": f"Windows {release} build {build_number}" if build_number else f"Windows {release}",
            "family": "windows",
            "targeted": (is_x64 and (win7_sp1_or_newer or win8_or_newer)) or windows_arm_supported,
            "ui_scale_bias": 1.03 if is_arm64 else 1.0,
            "scroll_units": 3,
            "font": "Segoe UI",
        })
        return profile

    if system == "Linux":
        info = read_linux_os_release()
        distro_id = (info.get("ID") or "").lower()
        like = (info.get("ID_LIKE") or "").lower()
        version = version_tuple(info.get("VERSION_ID") or "")
        domestic_ids = {"uos", "deepin", "kylin", "openkylin", "uniontech", "loongnix", "neokylin", "asianux"}
        is_domestic = distro_id in domestic_ids or any(item in like for item in domestic_ids)
        is_ubuntu = (distro_id == "ubuntu" or "ubuntu" in like) and version >= (20, 4)
        is_debian = (distro_id == "debian" or "debian" in like) and version >= (11,)
        is_fedora = (distro_id == "fedora" or "fedora" in like) and version >= (40,)
        is_generic_new = bool(version and version >= (20, 4))
        profile.update({
            "name": info.get("PRETTY_NAME") or "Linux",
            "family": "linux-cn" if is_domestic else "linux",
            "targeted": is_domestic or is_ubuntu or is_debian or is_fedora or is_generic_new,
            "ui_scale_bias": 1.05 if is_domestic else 1.02,
            "scroll_units": 3,
            "font": "Noto Sans CJK SC" if is_domestic else "Noto Sans",
        })
        return profile

    return profile


def configure_runtime_environment():
    profile = get_os_optimization_profile()
    family = profile.get("family", "")
    if family == "macos":
        os.environ.setdefault("TK_SILENCE_DEPRECATION", "1")
        os.environ.setdefault("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", "YES")
    elif family == "windows":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                import ctypes
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    elif family.startswith("linux"):
        os.environ.setdefault("GDK_SCALE", os.environ.get("GDK_SCALE", "1"))
        os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
        os.environ.setdefault("NO_AT_BRIDGE", "1")
    return profile


OS_OPTIMIZATION_PROFILE = configure_runtime_environment()


def get_ui_scale_bias():
    return OS_OPTIMIZATION_PROFILE.get("ui_scale_bias", 1.0) if OS_OPTIMIZATION_PROFILE.get("targeted") else 1.0


def get_scroll_units(default=2):
    return OS_OPTIMIZATION_PROFILE.get("scroll_units", default) if OS_OPTIMIZATION_PROFILE.get("targeted") else default


def get_base_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def get_app_data_dir():
    if os.environ.get("LOCALAI_PORTABLE") == "1":
        return get_base_dir()
    system = platform.system()
    if system == "Windows":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if root:
            return os.path.join(root, LOCALAI_APP_NAME)
    if system == "Darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", LOCALAI_APP_NAME)
    root = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(root, LOCALAI_APP_NAME)


APP_DATA_DIR = get_app_data_dir()
LOG_DIR = os.path.join(APP_DATA_DIR, "logs")
LOCAL_CONFIG_FILE = os.path.join(APP_DATA_DIR, "config.json")
CLOUD_CONFIG_DIR = os.path.join(APP_DATA_DIR, "config")
CLOUD_CONFIG_FILE = os.path.join(CLOUD_CONFIG_DIR, "cloudai_config.json")
CLOUD_SECRET_FILE = os.path.join(CLOUD_CONFIG_DIR, "cloudai_secrets.json")
CLOUD_CHAT_DIR = os.path.join(APP_DATA_DIR, "cloud_chats")


DEFAULT_CONFIG = {
    "last_model": "",
    "lmstudio_base_url": "http://localhost:1234/v1",
    "openai_model": "",
    "api_key": "",
    "api_base_url": "",
    "provider": "ollama",
    "first_run_done": False,
    "first_welcome_done": False,
    "update_url": "https://raw.githubusercontent.com/liaovoxuan/LocalAI/main/version.json",
    "auto_check_update": True,
    "allow_low_spec_force": True,
    "language": "zh_cn",
    "theme": "auto",
    "first_device_info_done": False,
    "wallpaper_path": "",
}


LANGUAGE_OPTIONS = {
    "zh_cn": {"name": "简体中文", "model_language": "Simplified Chinese", "user_role": "用户", "assistant_role": "助手"},
    "zh_tw": {"name": "繁體中文", "model_language": "Traditional Chinese", "user_role": "使用者", "assistant_role": "助理"},
    "en_us": {"name": "English (United States)", "model_language": "American English", "user_role": "User", "assistant_role": "Assistant"},
    "en_gb": {"name": "English (United Kingdom)", "model_language": "British English", "user_role": "User", "assistant_role": "Assistant"},
    "en_au": {"name": "English (Australia)", "model_language": "Australian English", "user_role": "User", "assistant_role": "Assistant"},
    "ja": {"name": "日本語", "model_language": "Japanese", "user_role": "ユーザー", "assistant_role": "アシスタント"},
    "ko": {"name": "한국어", "model_language": "Korean", "user_role": "사용자", "assistant_role": "어시스턴트"},
    "fr": {"name": "Français", "model_language": "French", "user_role": "Utilisateur", "assistant_role": "Assistant"},
    "de": {"name": "Deutsch", "model_language": "German", "user_role": "Benutzer", "assistant_role": "Assistent"},
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


LANGUAGE_ALIASES = {
    "zh": "zh_cn", "cn": "zh_cn", "zh-cn": "zh_cn", "zh-hans": "zh_cn", "zh_cn": "zh_cn",
    "zh-tw": "zh_tw", "zh-hant": "zh_tw", "tw": "zh_tw",
    "en": "en_us", "en-us": "en_us", "en_us": "en_us",
    "en-gb": "en_gb", "en-uk": "en_gb", "en_uk": "en_gb",
    "en-au": "en_au", "en_au": "en_au", "au": "en_au",
    "ja": "ja", "jp": "ja", "ko": "ko", "kr": "ko", "fr": "fr", "de": "de",
    "es": "es", "it": "it", "pt": "pt", "ru": "ru", "nl": "nl", "sv": "sv", "da": "da",
    "fi": "fi", "no": "no", "nb": "no", "tr": "tr", "pl": "pl", "cs": "cs", "uk": "uk",
    "el": "el", "ar": "ar", "mn": "mn", "th": "th", "vi": "vi", "id": "id", "ms": "ms", "hi": "hi",
}


CLOUD_PROVIDERS = {
    "openai_official": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4.1-mini", "gpt-4.1", "gpt-4o-mini", "gpt-4o"],
    },
    "openai_compatible": {
        "name": "OpenAI Compatible",
        "base_url": "",
        "models": ["gpt-4.1-mini", "qwen-plus", "deepseek-chat"],
    },
    "deepseek": {
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models": ["deepseek-chat", "deepseek-reasoner"],
    },
    "openrouter": {
        "name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "models": ["openai/gpt-4o-mini", "deepseek/deepseek-chat", "anthropic/claude-3.5-sonnet"],
    },
    "siliconflow": {
        "name": "SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": ["Qwen/Qwen2.5-7B-Instruct", "deepseek-ai/DeepSeek-V3"],
    },
}


CLOUD_TEXT = {
    "zh_cn": {
        "cloud_title": "CloudAI",
        "cloud_wizard_title": "CloudAI 首次启动向导",
        "cloud_welcome": "欢迎使用 CloudAI",
        "cloud_welcome_subtitle": "CloudAI 使用云模型提供商。语言设置与 LocalAI 共用。",
        "cloud_provider": "云模型提供商",
        "cloud_api_key": "API Key",
        "cloud_base_url": "Base URL",
        "cloud_model": "默认模型",
        "cloud_finish": "完成",
        "cloud_later": "稍后配置",
        "cloud_next": "下一步",
        "cloud_start": "开始使用",
        "cloud_settings": "CloudAI 设置",
        "cloud_no_key": "当前 Provider 尚未配置 API Key，请前往设置配置，或切换 Provider。",
        "cloud_key_saved": "API Key 已保存，配置文件中不会显示完整密钥。",
        "cloud_send": "发送",
        "cloud_thinking": "CloudAI 正在思考...",
        "cloud_input_hint": "输入消息，Enter 发送，Shift+Enter 换行",
        "cloud_language_saved": "语言已切换为：{language}",
        "cloud_new_chat": "新对话",
        "cloud_history": "历史聊天",
        "cloud_history_empty": "暂无历史聊天。",
        "cloud_usage": "云端用量",
        "cloud_refresh_usage": "刷新用量",
        "cloud_usage_unavailable": "无法从当前 Provider 获取用量。部分云服务不开放用量接口，请前往控制台查看。",
        "cloud_usage_loading": "正在获取用量...",
        "cloud_usage_summary": "用量信息：{usage}",
        "cloud_provider_config": "云模型配置",
        "cloud_about": "CloudAI {version}\n云端模型助手\n语言设置与 LocalAI 共用。",
        "cloud_key_status_ready": "已配置",
        "cloud_key_status_missing": "未配置 API Key",
        "cloud_export": "导出聊天记录",
        "cloud_export_done": "聊天记录已导出：\n{path}",
        "cloud_export_empty": "当前聊天没有可导出的内容。",
        "cloud_wallpaper": "更换壁纸",
        "cloud_wallpaper_error": "无法加载这张图片。请尝试 PNG/GIF，或安装 Pillow 后使用 JPG/JPEG。",
        "cloud_import_file": "导入文件",
        "cloud_file_imported": "已导入：{names}",
        "cloud_file_error": "无法读取所选文件。",
        "cloud_file_notice": "已附加文件：{names}",
        "cloud_theme": "深浅色模式",
        "cloud_theme_light": "浅色模式",
        "cloud_theme_dark": "深色模式",
        "cloud_theme_auto": "自动",
        "cloud_theme_saved": "外观设置已保存。",
        "cloud_untitled_chat": "未命名对话",
    },
    "zh_tw": {
        "cloud_title": "CloudAI",
        "cloud_wizard_title": "CloudAI 首次啟動精靈",
        "cloud_welcome": "歡迎使用 CloudAI",
        "cloud_welcome_subtitle": "CloudAI 使用雲端模型提供商。語言設定與 LocalAI 共用。",
        "cloud_provider": "雲端模型提供商",
        "cloud_api_key": "API Key",
        "cloud_base_url": "Base URL",
        "cloud_model": "預設模型",
        "cloud_finish": "完成",
        "cloud_later": "稍後設定",
        "cloud_next": "下一步",
        "cloud_start": "開始使用",
        "cloud_settings": "CloudAI 設定",
        "cloud_no_key": "目前 Provider 尚未設定 API Key，請前往設定，或切換 Provider。",
        "cloud_key_saved": "API Key 已儲存，設定檔不會顯示完整金鑰。",
        "cloud_send": "傳送",
        "cloud_thinking": "CloudAI 正在思考...",
        "cloud_input_hint": "輸入訊息，Enter 傳送，Shift+Enter 換行",
        "cloud_language_saved": "語言已切換為：{language}",
        "cloud_new_chat": "新對話",
        "cloud_history": "歷史聊天",
        "cloud_history_empty": "暫無歷史聊天。",
        "cloud_usage": "雲端用量",
        "cloud_refresh_usage": "重新整理用量",
        "cloud_usage_unavailable": "無法從目前 Provider 取得用量。部分雲端服務不開放用量介面，請前往控制台查看。",
        "cloud_usage_loading": "正在取得用量...",
        "cloud_usage_summary": "用量資訊：{usage}",
        "cloud_provider_config": "雲端模型設定",
        "cloud_about": "CloudAI {version}\n雲端模型助理\n語言設定與 LocalAI 共用。",
        "cloud_key_status_ready": "已設定",
        "cloud_key_status_missing": "未設定 API Key",
        "cloud_export": "匯出聊天記錄",
        "cloud_export_done": "聊天記錄已匯出：\n{path}",
        "cloud_export_empty": "目前聊天沒有可匯出的內容。",
        "cloud_wallpaper": "更換壁紙",
        "cloud_wallpaper_error": "無法載入這張圖片。請嘗試 PNG/GIF，或安裝 Pillow 後使用 JPG/JPEG。",
        "cloud_import_file": "匯入檔案",
        "cloud_file_imported": "已匯入：{names}",
        "cloud_file_error": "無法讀取所選檔案。",
        "cloud_file_notice": "已附加檔案：{names}",
        "cloud_theme": "深淺色模式",
        "cloud_theme_light": "淺色模式",
        "cloud_theme_dark": "深色模式",
        "cloud_theme_auto": "自動",
        "cloud_theme_saved": "外觀設定已儲存。",
        "cloud_untitled_chat": "未命名對話",
    },
    "en_us": {
        "cloud_title": "CloudAI",
        "cloud_wizard_title": "CloudAI First Launch Wizard",
        "cloud_welcome": "Welcome to CloudAI",
        "cloud_welcome_subtitle": "CloudAI uses cloud model providers. Language settings are shared with LocalAI.",
        "cloud_provider": "Cloud Model Provider",
        "cloud_api_key": "API Key",
        "cloud_base_url": "Base URL",
        "cloud_model": "Default Model",
        "cloud_finish": "Finish",
        "cloud_later": "Configure Later",
        "cloud_next": "Next",
        "cloud_start": "Start",
        "cloud_settings": "CloudAI Settings",
        "cloud_no_key": "The current provider has no API Key configured. Open Settings to configure one, or switch provider.",
        "cloud_key_saved": "API Key saved. The configuration view will not show the full key.",
        "cloud_send": "Send",
        "cloud_thinking": "CloudAI is thinking...",
        "cloud_input_hint": "Type a message. Enter sends, Shift+Enter inserts a newline.",
        "cloud_language_saved": "Language changed to: {language}",
        "cloud_new_chat": "New Chat",
        "cloud_history": "History",
        "cloud_history_empty": "No chat history yet.",
        "cloud_usage": "Cloud Usage",
        "cloud_refresh_usage": "Refresh Usage",
        "cloud_usage_unavailable": "Usage is unavailable from the current provider. Some cloud services expose usage only in their console.",
        "cloud_usage_loading": "Loading usage...",
        "cloud_usage_summary": "Usage: {usage}",
        "cloud_provider_config": "Cloud Model Configuration",
        "cloud_about": "CloudAI {version}\nCloud model assistant\nLanguage settings are shared with LocalAI.",
        "cloud_key_status_ready": "Configured",
        "cloud_key_status_missing": "No API Key",
        "cloud_export": "Export Chat",
        "cloud_export_done": "Chat exported:\n{path}",
        "cloud_export_empty": "This chat has nothing to export.",
        "cloud_wallpaper": "Change Wallpaper",
        "cloud_wallpaper_error": "Cannot load this image. Try PNG/GIF, or install Pillow for JPG/JPEG support.",
        "cloud_import_file": "Import File",
        "cloud_file_imported": "Imported: {names}",
        "cloud_file_error": "Cannot read the selected file.",
        "cloud_file_notice": "Attached file(s): {names}",
        "cloud_theme": "Appearance",
        "cloud_theme_light": "Light",
        "cloud_theme_dark": "Dark",
        "cloud_theme_auto": "Auto",
        "cloud_theme_saved": "Appearance settings saved.",
        "cloud_untitled_chat": "Untitled Chat",
    },
}
CLOUD_TEXT["en_gb"] = CLOUD_TEXT["en_us"]
CLOUD_TEXT["en_au"] = CLOUD_TEXT["en_us"]
CLOUD_TEXT["ja"] = {
    **CLOUD_TEXT["en_us"],
    "cloud_welcome": "CloudAI へようこそ",
    "cloud_next": "次へ",
    "cloud_send": "送信",
}
CLOUD_TEXT["fr"] = {
    **CLOUD_TEXT["en_us"],
    "cloud_welcome": "Bienvenue dans CloudAI",
    "cloud_next": "Suivant",
    "cloud_send": "Envoyer",
}
CLOUD_TEXT["de"] = {
    **CLOUD_TEXT["en_us"],
    "cloud_welcome": "Willkommen bei CloudAI",
    "cloud_next": "Weiter",
    "cloud_send": "Senden",
}
CHAT_GUI_TEXT = {}
for _code in LANGUAGE_OPTIONS:
    base = {
        "language_title": "选择语言",
        "language_saved": "语言已切换为：{language}",
        "save": "保存",
        "you_name": "你",
    }
    if _code.startswith("en"):
        base.update({"language_title": "Choose Language", "language_saved": "Language changed to: {language}", "save": "Save", "you_name": "You"})
    CHAT_GUI_TEXT[_code] = base
    CHAT_GUI_TEXT[_code].update(CLOUD_TEXT.get(_code, CLOUD_TEXT["en_us"]))


ADDITIONAL_CHAT_TEXT = {
    "ko": {"language_title": "언어 선택", "send": "보내기", "save": "저장"},
    "es": {"language_title": "Elegir idioma", "send": "Enviar", "save": "Guardar"},
    "it": {"language_title": "Scegli lingua", "send": "Invia", "save": "Salva"},
    "pt": {"language_title": "Escolher idioma", "send": "Enviar", "save": "Salvar"},
    "ru": {"language_title": "Выберите язык", "send": "Отправить", "save": "Сохранить"},
    "nl": {"language_title": "Taal kiezen", "send": "Verzenden", "save": "Opslaan"},
    "sv": {"language_title": "Välj språk", "send": "Skicka", "save": "Spara"},
    "da": {"language_title": "Vælg sprog", "send": "Send", "save": "Gem"},
    "fi": {"language_title": "Valitse kieli", "send": "Lähetä", "save": "Tallenna"},
    "no": {"language_title": "Velg språk", "send": "Send", "save": "Lagre"},
    "tr": {"language_title": "Dil seç", "send": "Gönder", "save": "Kaydet"},
    "pl": {"language_title": "Wybierz język", "send": "Wyślij", "save": "Zapisz"},
    "cs": {"language_title": "Vyberte jazyk", "send": "Odeslat", "save": "Uložit"},
    "uk": {"language_title": "Виберіть мову", "send": "Надіслати", "save": "Зберегти"},
    "el": {"language_title": "Επιλογή γλώσσας", "send": "Αποστολή", "save": "Αποθήκευση"},
    "ar": {"language_title": "اختر اللغة", "send": "إرسال", "save": "حفظ"},
    "mn": {"language_title": "Хэл сонгох", "send": "Илгээх", "save": "Хадгалах"},
    "th": {"language_title": "เลือกภาษา", "send": "ส่ง", "save": "บันทึก"},
    "vi": {"language_title": "Chọn ngôn ngữ", "send": "Gửi", "save": "Lưu"},
    "id": {"language_title": "Pilih Bahasa", "send": "Kirim", "save": "Simpan"},
    "ms": {"language_title": "Pilih Bahasa", "send": "Hantar", "save": "Simpan"},
    "hi": {"language_title": "भाषा चुनें", "send": "भेजें", "save": "सहेजें"},
}
for _code, _values in ADDITIONAL_CHAT_TEXT.items():
    CHAT_GUI_TEXT.setdefault(_code, CHAT_GUI_TEXT["en_us"].copy()).update(_values)


def normalize_language(value):
    key = str(value or "zh_cn").strip().lower().replace("_", "-")
    return LANGUAGE_ALIASES.get(key, "zh_cn")


def get_lang(config):
    return normalize_language(config.get("language", "zh_cn"))


def ensure_app_dirs():
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(os.path.join(APP_DATA_DIR, "exports"), exist_ok=True)


def load_config():
    ensure_app_dirs()
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(LOCAL_CONFIG_FILE):
        try:
            with open(LOCAL_CONFIG_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                config.update(data)
        except Exception:
            pass
    config["language"] = normalize_language(config.get("language"))
    return config


def save_config(config):
    ensure_app_dirs()
    clean = DEFAULT_CONFIG.copy()
    clean.update(config or {})
    clean["language"] = normalize_language(clean.get("language"))
    with open(LOCAL_CONFIG_FILE, "w", encoding="utf-8") as handle:
        json.dump(clean, handle, ensure_ascii=False, indent=2)


def chat_gui_text(config, key, **kwargs):
    code = get_lang(config)
    text = CHAT_GUI_TEXT.get(code, CHAT_GUI_TEXT["zh_cn"]).get(key, CHAT_GUI_TEXT["zh_cn"].get(key, key))
    return text.format(**kwargs) if kwargs else text


def ensure_cloud_dirs():
    ensure_app_dirs()
    os.makedirs(CLOUD_CONFIG_DIR, exist_ok=True)
    os.makedirs(CLOUD_CHAT_DIR, exist_ok=True)


def cloud_text(config, key, **kwargs):
    return chat_gui_text(config, key, **kwargs)


def mask_key(value):
    return MASK if value else ""


def encode_secret(value):
    if not value:
        return ""
    raw = value.encode("utf-8")
    salt = platform.node().encode("utf-8") or b"cloudai"
    mixed = bytes(byte ^ salt[index % len(salt)] for index, byte in enumerate(raw))
    return base64.urlsafe_b64encode(mixed).decode("ascii")


def decode_secret(value):
    if not value:
        return ""
    try:
        raw = base64.urlsafe_b64decode(value.encode("ascii"))
        salt = platform.node().encode("utf-8") or b"cloudai"
        plain = bytes(byte ^ salt[index % len(salt)] for index, byte in enumerate(raw))
        return plain.decode("utf-8")
    except Exception:
        return ""


def load_secret_store():
    ensure_cloud_dirs()
    if not os.path.exists(CLOUD_SECRET_FILE):
        return {}
    try:
        with open(CLOUD_SECRET_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_secret_store(data):
    ensure_cloud_dirs()
    safe = data if isinstance(data, dict) else {}
    with open(CLOUD_SECRET_FILE, "w", encoding="utf-8") as handle:
        json.dump(safe, handle, ensure_ascii=False, indent=2)


def get_api_key(provider):
    return decode_secret(load_secret_store().get(provider, ""))


def set_api_key(provider, api_key):
    secrets = load_secret_store()
    if api_key:
        secrets[provider] = encode_secret(api_key)
    else:
        secrets.pop(provider, None)
    save_secret_store(secrets)


def default_cloud_config():
    providers = {}
    for code, info in CLOUD_PROVIDERS.items():
        providers[code] = {
            "enabled": code == "openai_official",
            "base_url": info["base_url"],
            "model": info["models"][0],
            "api_key": "",
        }
    return {
        "first_run_completed": False,
        "provider": "openai_official",
        "default_model": CLOUD_PROVIDERS["openai_official"]["models"][0],
        "providers": providers,
    }


def load_cloud_config():
    ensure_cloud_dirs()
    config = default_cloud_config()
    if os.path.exists(CLOUD_CONFIG_FILE):
        try:
            with open(CLOUD_CONFIG_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                config.update(data)
        except Exception:
            pass
    config.setdefault("providers", {})
    for code, info in CLOUD_PROVIDERS.items():
        current = config["providers"].setdefault(code, {})
        current.setdefault("enabled", code == config.get("provider"))
        current.setdefault("base_url", info["base_url"])
        current.setdefault("model", info["models"][0])
        current["api_key"] = mask_key(get_api_key(code))
    if config.get("provider") not in CLOUD_PROVIDERS:
        config["provider"] = "openai_official"
    config.setdefault("default_model", config["providers"][config["provider"]].get("model", ""))
    return config


def save_cloud_config(config):
    ensure_cloud_dirs()
    public_config = json.loads(json.dumps(config))
    for provider, item in public_config.get("providers", {}).items():
        item["api_key"] = mask_key(get_api_key(provider))
    with open(CLOUD_CONFIG_FILE, "w", encoding="utf-8") as handle:
        json.dump(public_config, handle, ensure_ascii=False, indent=2)


def active_provider_config(config):
    provider = config.get("provider", "openai_official")
    item = config.get("providers", {}).get(provider, {})
    base_url = item.get("base_url") or CLOUD_PROVIDERS[provider]["base_url"]
    model = item.get("model") or config.get("default_model") or CLOUD_PROVIDERS[provider]["models"][0]
    return provider, base_url, model, get_api_key(provider)


def log_cloud_error(exc):
    ensure_cloud_dirs()
    path = os.path.join(LOG_DIR, "cloudai_error.log")
    text = traceback.format_exc()
    for secret in load_secret_store().values():
        decoded = decode_secret(secret)
        if decoded:
            text = text.replace(decoded, MASK)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"\n[{datetime.now().isoformat()}]\n{text}\n")


def get_platform_font(default="Helvetica"):
    return OS_OPTIMIZATION_PROFILE.get("font", default) if OS_OPTIMIZATION_PROFILE.get("targeted") else default


def resolve_theme(value):
    value = (value or "auto").strip().lower()
    if value in {"dark", "light"}:
        return value
    return "dark" if datetime.now().hour >= 18 or datetime.now().hour < 7 else "light"


def is_macos_tahoe_or_newer():
    if os.environ.get("LOCALAI_DISABLE_LIQUID_GLASS") == "1":
        return False
    if platform.system() != "Darwin":
        return False
    try:
        version = platform.mac_ver()[0] or ""
        major = int(version.split(".")[0]) if version else 0
        if major >= 26:
            return True
    except Exception:
        pass
    try:
        return int((platform.release() or "0").split(".")[0]) >= 25
    except Exception:
        return False


def with_liquid_glass_palette(colors, theme):
    if not is_macos_tahoe_or_newer():
        colors["glass"] = False
        return colors
    dark = resolve_theme(theme) == "dark"
    colors.update({
        "glass": True,
        "window": "#eef3fb" if not dark else "#0b1220",
        "toolbar": "#e8f0fa" if not dark else "#101827",
        "panel": "#f7faff" if not dark else "#0f172a",
        "surface": "#f9fbff" if not dark else "#172033",
        "surface_hover": "#edf5ff" if not dark else "#22304a",
        "input": "#fbfdff" if not dark else "#111c2e",
        "border": "#c8d7eb" if not dark else "#31415c",
        "ai_bubble": "#ffffff" if not dark else "#162238",
        "ai_text": "#202123" if not dark else "#f9fafb",
        "muted": "#5f6f86" if not dark else "#c9d5e8",
        "glass_border": "#bdd0e8" if not dark else "#3a4d6b",
    })
    return colors


def theme_palette(value):
    if resolve_theme(value) == "dark":
        return with_liquid_glass_palette({
            "window": "#111827",
            "toolbar": "#111827",
            "panel": "#111827",
            "surface": "#1f2937",
            "surface_hover": "#263244",
            "input": "#111827",
            "text": "#f9fafb",
            "muted": "#d1d5db",
            "border": "#374151",
            "ai_bubble": "#1f2937",
            "ai_text": "#f9fafb",
            "user_bubble": "#2563eb",
            "user_text": "#ffffff",
            "disabled": "#374151",
        }, value)
    return with_liquid_glass_palette({
        "window": "#ffffff",
        "toolbar": "#f7f7f8",
        "panel": "#ffffff",
        "surface": "#f7f7f8",
        "surface_hover": "#ececf1",
        "input": "#ffffff",
        "text": "#202123",
        "muted": "#667085",
        "border": "#d0d5dd",
        "ai_bubble": "#ffffff",
        "ai_text": "#202123",
        "user_bubble": "#2563eb",
        "user_text": "#ffffff",
        "disabled": "#d0d5dd",
    }, value)


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


def app_icon_path(theme):
    variant = "dark" if resolve_theme(theme) == "dark" else "light"
    return resource_path("assets", "icons", f"localai_{variant}.png")


def apply_window_icon(window, theme):
    try:
        import tkinter as tk
        icon_path = app_icon_path(theme)
        if not os.path.exists(icon_path):
            return
        image = tk.PhotoImage(file=icon_path)
        window.iconphoto(True, image)
        window._cloudai_icon_image = image
    except Exception:
        pass


def export_chat_markdown(messages):
    ensure_app_dirs()
    export_dir = os.path.join(APP_DATA_DIR, "exports")
    os.makedirs(export_dir, exist_ok=True)
    name = "cloudai_chat_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".md"
    path = os.path.join(export_dir, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f"# CloudAI Chat\n\n")
        for item in messages:
            role = item.get("role", "assistant")
            title = "User" if role == "user" else "CloudAI"
            handle.write(f"## {title}\n\n{item.get('content', '')}\n\n")
    return path


def cloud_chat_title(messages, fallback):
    for item in messages:
        if item.get("role") == "user" and item.get("content", "").strip():
            title = item["content"].strip().replace("\n", " ")
            return title[:36] + ("..." if len(title) > 36 else "")
    return fallback


def new_cloud_chat_path():
    ensure_cloud_dirs()
    return os.path.join(CLOUD_CHAT_DIR, "cloud_chat_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".json")


def save_cloud_chat(path, messages, config):
    if not messages:
        return path
    ensure_cloud_dirs()
    path = path or new_cloud_chat_path()
    data = {
        "title": cloud_chat_title(messages, cloud_text(config, "cloud_untitled_chat")),
        "updated_at": datetime.now().isoformat(),
        "messages": messages,
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    return path


def list_cloud_chats():
    ensure_cloud_dirs()
    chats = []
    for name in os.listdir(CLOUD_CHAT_DIR):
        if not name.endswith(".json"):
            continue
        path = os.path.join(CLOUD_CHAT_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            chats.append({
                "path": path,
                "title": data.get("title") or os.path.splitext(name)[0],
                "updated_at": data.get("updated_at", ""),
            })
        except Exception:
            continue
    return sorted(chats, key=lambda item: item.get("updated_at", ""), reverse=True)


def open_cloud_chat(path):
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data.get("messages", []) if isinstance(data, dict) else []


def openai_chat_url(base_url):
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


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
    return ""


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


def messages_to_text(messages):
    parts = []
    for item in messages or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content", "")
        if isinstance(content, list):
            content = "\n".join(str(block.get("text", "")) for block in content if isinstance(block, dict))
        parts.append(str(content))
    return "\n".join(parts)


def content_allowed(text):
    if not text:
        return True
    normalized = re.sub(r"\s+", "", str(text).lower())
    return not any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in MODERATION_PATTERNS)


def moderated_text(text):
    return text if content_allowed(text) else MODERATION_BLOCK_MESSAGE


TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml",
    ".html", ".htm", ".rtf", ".log", ".ini", ".cfg", ".conf", ".toml", ".py", ".js", ".ts",
    ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".go", ".rs", ".swift", ".kt", ".php",
    ".rb", ".sh", ".bat", ".ps1", ".sql", ".css",
}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".tif", ".heic", ".heif", ".avif", ".ico"}
NATIVE_FILE_MODEL_HINTS = (
    "gpt-4o", "gpt-4.1", "vision", "vl", "qwen-vl", "qwen2-vl", "qwen2.5-vl",
    "gemini", "claude", "llava", "minicpm-v", "pixtral", "omni",
)


def model_supports_native_files(model):
    name = (model or "").lower()
    return any(hint in name for hint in NATIVE_FILE_MODEL_HINTS)


def is_image_file(path):
    return os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS


def encode_file_data_url(path):
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    with open(path, "rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def read_docx_text(path):
    with zipfile.ZipFile(path) as archive:
        xml_data = archive.read("word/document.xml")
    root = ElementTree.fromstring(xml_data)
    texts = []
    for node in root.iter():
        if node.tag.endswith("}t") and node.text:
            texts.append(node.text)
    return "\n".join(texts).strip()


def read_text_file(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".docx":
        return read_docx_text(path)
    if ext == ".doc":
        return ""
    if ext in TEXT_EXTENSIONS:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.read()
    return ""


def build_file_context(paths):
    parts = []
    for path in paths or []:
        text = read_text_file(path)
        if text:
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            if len(text) > 12000:
                text = text[:12000] + "\n...[truncated]"
            parts.append(f"[File: {os.path.basename(path)}]\n{text}")
    return "\n\n".join(parts).strip()


def messages_with_files(messages, file_paths, model):
    if not file_paths:
        return messages
    result = [dict(item) for item in messages]
    if not result:
        result.append({"role": "user", "content": ""})
    last = result[-1]
    content = last.get("content", "")
    native = model_supports_native_files(model)
    if native:
        blocks = [{"type": "text", "text": content if isinstance(content, str) else str(content)}]
        for path in file_paths:
            if not os.path.exists(path):
                continue
            if is_image_file(path):
                blocks.append({"type": "image_url", "image_url": {"url": encode_file_data_url(path)}})
            else:
                blocks.append({
                    "type": "file",
                    "file": {
                        "filename": os.path.basename(path),
                        "file_data": encode_file_data_url(path),
                    },
                })
        last["content"] = blocks
        return result
    context = build_file_context(file_paths)
    if context:
        last["content"] = f"{content}\n\nUse the following file content to answer:\n\n{context}".strip()
    else:
        names = ", ".join(os.path.basename(path) for path in file_paths if os.path.exists(path))
        notice = f"The attached file(s) could not be read as text by this app: {names}. If you cannot access them directly, say that clearly."
        last["content"] = f"{content}\n\n{notice}".strip()
    return result


def summarize_usage_data(data):
    if not isinstance(data, dict):
        return ""
    nested = data.get("data")
    if isinstance(nested, dict):
        nested_summary = summarize_usage_data(nested)
        if nested_summary:
            return nested_summary
    if "total_usage" in data:
        return f"total_usage={data.get('total_usage')}"
    if "total_credits" in data:
        return f"credits={data.get('total_credits')}, used={data.get('total_usage', '')}".strip(", ")
    if "balance" in data and isinstance(data.get("balance"), (str, int, float)):
        return f"balance={data.get('balance')}"
    if "total_granted" in data or "total_used" in data:
        granted = data.get("total_granted", "")
        used = data.get("total_used", "")
        return f"granted={granted}, used={used}".strip(", ")
    if "data" in data and isinstance(data["data"], list):
        return f"{len(data['data'])} records"
    if "usage" in data:
        return json.dumps(data["usage"], ensure_ascii=False)
    keys = [key for key in ("limit", "used", "remaining", "balance", "total") if key in data]
    if keys:
        return ", ".join(f"{key}={data.get(key)}" for key in keys)
    return ""


def cloud_usage_urls(provider, base_url):
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return []
    if base.endswith("/chat/completions"):
        base = base[: -len("/chat/completions")]
    urls = []
    if provider == "openai_official":
        urls.extend([
            "https://api.openai.com/v1/dashboard/billing/usage",
            "https://api.openai.com/v1/usage",
        ])
    elif provider == "openrouter":
        urls.append("https://openrouter.ai/api/v1/credits")
    elif provider == "deepseek":
        urls.append("https://api.deepseek.com/user/balance")
    elif provider == "siliconflow":
        urls.extend(["https://api.siliconflow.cn/v1/user/info", "https://api.siliconflow.cn/v1/user/balance"])
    urls.extend([base + "/usage", base + "/dashboard/billing/usage", base + "/user/usage"])
    return list(dict.fromkeys(urls))


def fetch_cloud_usage(cloud_config):
    provider, base_url, _, api_key = active_provider_config(cloud_config)
    if not api_key:
        raise PermissionError("missing_api_key")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    last_error = None
    for url in cloud_usage_urls(provider, base_url):
        try:
            response = requests.get(url, headers=headers, timeout=20)
            if response.status_code in (404, 405):
                continue
            response.raise_for_status()
            summary = summarize_usage_data(response.json())
            if summary:
                return summary
        except Exception as exc:
            last_error = exc
    if last_error:
        raise last_error
    return ""


def ask_openai_chat_completions(messages, model, base_url, api_key="", file_paths=None):
    url = openai_chat_url(base_url)
    if not url:
        raise ValueError("API Base URL is empty.")
    target_model = (model or "").strip()
    if not target_model:
        raise ValueError("Model is empty.")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": target_model,
        "messages": messages_with_files(messages, file_paths, target_model),
        "temperature": 0.7,
        "stream": False,
    }
    response = requests.post(url, headers=headers, json=payload, timeout=180)
    response.raise_for_status()
    return response_text_from_json(response.json()) or ""


def ask_cloudai(messages, cloud_config, file_paths=None):
    if not content_allowed(messages_to_text(messages)):
        return MODERATION_BLOCK_MESSAGE
    provider, base_url, model, api_key = active_provider_config(cloud_config)
    if not api_key:
        raise PermissionError("missing_api_key")
    return moderated_text(ask_openai_chat_completions(messages, model, base_url, api_key, file_paths))


def run_cloudai_wizard(local_config, cloud_config):
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception:
        return cloud_config

    if cloud_config.get("first_run_completed"):
        return cloud_config

    class Wizard(tk.Tk):
        def __init__(self):
            super().__init__()
            self.local_config = local_config
            self.cloud_config = cloud_config
            self.step = 0
            self.title(cloud_text(self.local_config, "cloud_wizard_title"))
            self.geometry("560x420")
            self.minsize(520, 380)
            self.configure(bg="#f7f8fb")
            self.protocol("WM_DELETE_WINDOW", self.finish_later)
            self.body = tk.Frame(self, bg="#f7f8fb")
            self.body.pack(fill="both", expand=True, padx=28, pady=24)
            self.show_language()

        def clear(self):
            for child in self.body.winfo_children():
                child.destroy()

        def label(self, text, size=14, bold=False):
            font = (get_platform_font(), size, "bold" if bold else "normal")
            tk.Label(self.body, text=text, bg="#f7f8fb", fg="#111827", font=font, wraplength=480, justify="left").pack(anchor="w", pady=8)

        def button(self, text, command):
            tk.Button(self.body, text=text, command=command, bg="#2563eb", fg="white", relief="flat", padx=18, pady=8).pack(anchor="e", pady=12)

        def show_language(self):
            self.clear()
            self.label(cloud_text(self.local_config, "language_title"), 20, True)
            options = {item["name"]: code for code, item in LANGUAGE_OPTIONS.items()}
            current = LANGUAGE_OPTIONS[get_lang(self.local_config)]["name"]
            self.language_var = tk.StringVar(value=current)
            ttk.Combobox(self.body, textvariable=self.language_var, values=list(options.keys()), state="readonly").pack(fill="x", pady=12)
            self.button(cloud_text(self.local_config, "cloud_next"), lambda: self.save_language(options))

        def save_language(self, options):
            self.local_config["language"] = normalize_language(options.get(self.language_var.get(), "zh_cn"))
            save_config(self.local_config)
            self.show_welcome()

        def show_welcome(self):
            self.clear()
            self.label(cloud_text(self.local_config, "cloud_welcome"), 22, True)
            self.label(cloud_text(self.local_config, "cloud_welcome_subtitle"), 12)
            self.button(cloud_text(self.local_config, "cloud_next"), self.show_provider)

        def show_provider(self):
            self.clear()
            self.label(cloud_text(self.local_config, "cloud_provider"), 20, True)
            self.provider_var = tk.StringVar(value=self.cloud_config.get("provider", "openai_official"))
            for code, info in CLOUD_PROVIDERS.items():
                tk.Radiobutton(self.body, text=info["name"], value=code, variable=self.provider_var, bg="#f7f8fb").pack(anchor="w", pady=4)
            self.button(cloud_text(self.local_config, "cloud_next"), self.show_key)

        def show_key(self):
            self.clear()
            provider = self.provider_var.get()
            self.cloud_config["provider"] = provider
            self.label(f"{CLOUD_PROVIDERS[provider]['name']} - {cloud_text(self.local_config, 'cloud_api_key')}", 18, True)
            self.key_var = tk.StringVar()
            tk.Entry(self.body, textvariable=self.key_var, show="*", relief="solid").pack(fill="x", pady=10, ipady=6)
            self.button(cloud_text(self.local_config, "cloud_next"), self.show_base_url)
            tk.Button(self.body, text=cloud_text(self.local_config, "cloud_later"), command=self.show_base_url, relief="flat").pack(anchor="e")

        def show_base_url(self):
            provider = self.cloud_config["provider"]
            key = self.key_var.get().strip()
            if key:
                set_api_key(provider, key)
            self.clear()
            self.label(cloud_text(self.local_config, "cloud_base_url"), 18, True)
            current = self.cloud_config["providers"][provider].get("base_url") or CLOUD_PROVIDERS[provider]["base_url"]
            self.base_var = tk.StringVar(value=current)
            tk.Entry(self.body, textvariable=self.base_var, relief="solid").pack(fill="x", pady=10, ipady=6)
            self.button(cloud_text(self.local_config, "cloud_next"), self.show_model)

        def show_model(self):
            provider = self.cloud_config["provider"]
            self.cloud_config["providers"][provider]["base_url"] = self.base_var.get().strip()
            self.clear()
            self.label(cloud_text(self.local_config, "cloud_model"), 18, True)
            models = CLOUD_PROVIDERS[provider]["models"]
            self.model_var = tk.StringVar(value=self.cloud_config["providers"][provider].get("model") or models[0])
            combo = ttk.Combobox(self.body, textvariable=self.model_var, values=models, state="normal")
            combo.pack(fill="x", pady=10, ipady=6)
            self.button(cloud_text(self.local_config, "cloud_finish"), self.finish)

        def finish_later(self):
            self.cloud_config["first_run_completed"] = True
            save_cloud_config(self.cloud_config)
            self.safe_close()

        def finish(self):
            provider = self.cloud_config["provider"]
            model = self.model_var.get().strip()
            self.cloud_config["providers"][provider]["model"] = model
            self.cloud_config["providers"][provider]["enabled"] = True
            self.cloud_config["default_model"] = model
            self.cloud_config["first_run_completed"] = True
            save_cloud_config(self.cloud_config)
            self.safe_close()

        def safe_close(self):
            if getattr(self, "_closing", False):
                return
            self._closing = True
            def force_destroy():
                try:
                    self.quit()
                except Exception:
                    pass
                try:
                    self.destroy()
                except Exception:
                    pass
            try:
                self.after_idle(force_destroy)
            except Exception:
                force_destroy()

    app = Wizard()
    app.mainloop()
    return load_cloud_config()


def run_cloudai_gui():
    try:
        import threading
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except Exception:
        return False

    local_config = load_config()
    cloud_config = run_cloudai_wizard(local_config, load_cloud_config())

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.local_config = load_config()
            self.cloud_config = cloud_config
            self.messages = []
            self.current_chat_path = ""
            self.asking = False
            self.closing = False
            self.send_update_job = None
            self.wallpaper_image = None
            self.wallpaper_item = None
            self.wallpaper_resize_job = None
            self.message_windows = []
            self.content_height = 0
            self.last_canvas_width = 0
            self.last_canvas_height = 0
            self.last_trackpad_scroll_time = 0
            self.status_window = None
            self.pending_files = []
            self.title(f"CloudAI {APP_VERSION}")
            self.colors = self.palette()
            self.configure(bg=self.colors["window"])
            apply_window_icon(self, self.local_config.get("theme", "auto"))
            self.protocol("WM_DELETE_WINDOW", self.close)
            self.register_macos_quit()
            self.apply_responsive_window()
            self.build()
            self.after(200, self.focus_input)

        def t(self, key, **kwargs):
            self.local_config = load_config()
            return cloud_text(self.local_config, key, **kwargs)

        def palette(self):
            return theme_palette(self.local_config.get("theme", "auto"))

        def apply_responsive_window(self):
            screen_w = max(self.winfo_screenwidth(), 1)
            screen_h = max(self.winfo_screenheight(), 1)
            scale = max(0.88, min(1.24, min(screen_w / 1440, screen_h / 900) * get_ui_scale_bias()))
            width = min(max(int(1040 * scale), 780), int(screen_w * 0.94))
            height = min(max(int(720 * scale), 540), int(screen_h * 0.9))
            x = max((screen_w - width) // 2, 0)
            y = max((screen_h - height) // 2, 0)
            try:
                current = float(self.tk.call("tk", "scaling"))
                self.tk.call("tk", "scaling", max(0.9, min(2.0, current * min(max(scale, 0.95), 1.08))))
            except Exception:
                pass
            self.geometry(f"{width}x{height}+{x}+{y}")
            self.minsize(min(780, width), min(540, height))
            self.resizable(True, True)

        def register_macos_quit(self):
            if platform.system() != "Darwin":
                return
            try:
                self.createcommand("tk::mac::Quit", self.close)
            except Exception:
                pass

        def styled_button(self, parent, text, command, primary=False):
            bg = "#2563eb" if primary else self.colors["surface"]
            fg = "#ffffff" if primary else self.colors["text"]
            hover = "#1d4ed8" if primary else self.colors["surface_hover"]
            widget = tk.Label(parent, text=text, bg=bg, fg=fg, padx=14, pady=8, cursor="hand2",
                              font=(get_platform_font(), 11, "bold" if primary else "normal"))
            widget._cloudai_command = command
            widget._cloudai_enabled = True
            widget.bind("<Button-1>", lambda _event: command() if getattr(widget, "_cloudai_enabled", True) else None)
            widget.bind("<Enter>", lambda _event: widget.configure(bg=hover) if getattr(widget, "_cloudai_enabled", True) else None)
            widget.bind("<Leave>", lambda _event: widget.configure(bg=getattr(widget, "_cloudai_bg", bg)))
            widget._cloudai_bg = bg
            return widget

        def build(self):
            self.configure(bg=self.colors["window"])
            toolbar = tk.Frame(self, bg=self.colors["toolbar"], height=52,
                               highlightthickness=1 if self.colors.get("glass") else 0,
                               highlightbackground=self.colors.get("glass_border", self.colors["toolbar"]))
            toolbar.pack(fill="x")
            logo_text = "☁ CloudAI" if platform.system() != "Darwin" else " CloudAI"
            self.styled_button(toolbar, f"{logo_text} {APP_VERSION}", self.show_about).pack(side="left", padx=(10, 6), pady=8)
            self.styled_button(toolbar, self.t("cloud_new_chat"), self.new_chat).pack(side="left", padx=6, pady=8)
            self.styled_button(toolbar, self.t("cloud_history"), self.show_history).pack(side="left", padx=6, pady=8)
            self.styled_button(toolbar, self.t("cloud_import_file"), self.import_files).pack(side="left", padx=6, pady=8)
            self.styled_button(toolbar, self.t("cloud_export"), self.export_current_chat).pack(side="left", padx=6, pady=8)
            self.styled_button(toolbar, self.t("cloud_wallpaper"), self.choose_wallpaper).pack(side="left", padx=6, pady=8)
            self.styled_button(toolbar, self.t("cloud_settings"), self.show_settings).pack(side="left", padx=6, pady=8)
            self.provider_label = tk.Label(toolbar, bg=self.colors["toolbar"], fg=self.colors["muted"], text="")
            self.provider_label.pack(side="right", padx=12)

            chat_wrap = tk.Frame(self, bg=self.colors["panel"],
                                 highlightthickness=1 if self.colors.get("glass") else 0,
                                 highlightbackground=self.colors.get("glass_border", self.colors["panel"]))
            chat_wrap.pack(fill="both", expand=True)
            self.canvas = tk.Canvas(chat_wrap, bg=self.colors["panel"], highlightthickness=0, bd=0)
            scrollbar = tk.Scrollbar(chat_wrap, orient="vertical", command=self.canvas.yview)
            self.canvas.configure(yscrollcommand=scrollbar.set)
            scrollbar.pack(side="right", fill="y")
            self.canvas.pack(side="left", fill="both", expand=True)
            self.canvas.bind("<Configure>", self.on_canvas_configure)
            self.bind_mousewheel()

            bottom = tk.Frame(self, bg=self.colors["window"], padx=18, pady=14,
                              highlightthickness=1 if self.colors.get("glass") else 0,
                              highlightbackground=self.colors.get("glass_border", self.colors["window"]))
            bottom.pack(fill="x", padx=14, pady=14)
            self.file_label = tk.Label(bottom, text="", bg=self.colors["window"], fg=self.colors["muted"],
                                       font=(get_platform_font(), 10), anchor="w", justify="left")
            self.file_label.pack(fill="x", pady=(0, 6))
            input_row = tk.Frame(bottom, bg=self.colors["window"])
            input_row.pack(fill="x")
            self.input = tk.Text(input_row, height=3, wrap="word", relief="solid", bd=1, bg=self.colors["input"],
                                 fg=self.colors["text"], insertbackground=self.colors["text"],
                                 highlightthickness=1, highlightbackground=self.colors["border"],
                                 font=(get_platform_font(), 12))
            self.input.pack(side="left", fill="x", expand=True, padx=(0, 10))
            self.input.bind("<Return>", self.handle_enter)
            self.input.bind("<Shift-Return>", self.handle_shift_enter)
            self.input.bind("<KeyRelease>", self.schedule_send_button_update)
            self.input.bind("<<Modified>>", self.handle_text_modified)
            self.input.bind("<<Paste>>", lambda _event: self.after(1, self.schedule_send_button_update), add="+")
            self.input.bind("<<Cut>>", lambda _event: self.after(1, self.schedule_send_button_update), add="+")
            self.send_button = self.styled_button(input_row, self.t("cloud_send"), self.send, True)
            self.send_button.pack(side="right", fill="y")
            self.update_provider_label()
            self.update_send_button_state()
            self.after(120, lambda: self.apply_wallpaper(self.local_config.get("wallpaper_path", ""), False))
            self.update_file_label()

        def bind_mousewheel(self):
            def scroll(event):
                if self.closing:
                    return "break"
                now = time.monotonic()
                min_interval = 0.008 if platform.system() == "Darwin" else 0.003
                if now - self.last_trackpad_scroll_time < min_interval:
                    return "break"
                self.last_trackpad_scroll_time = now
                units = get_scroll_units(3)
                if event.num == 4:
                    self.canvas.yview_scroll(-units, "units")
                elif event.num == 5:
                    self.canvas.yview_scroll(units, "units")
                else:
                    if platform.system() == "Windows":
                        delta = int(-event.delta / 120) if event.delta else 0
                    else:
                        delta = -1 if event.delta > 0 else 1
                    self.canvas.yview_scroll(delta * units, "units")
                return "break"
            self.canvas.bind("<MouseWheel>", scroll)
            self.canvas.bind("<Button-4>", scroll)
            self.canvas.bind("<Button-5>", scroll)

        def on_canvas_configure(self, event):
            width = int(getattr(event, "width", 0) or self.canvas.winfo_width())
            height = int(getattr(event, "height", 0) or self.canvas.winfo_height())
            if abs(width - self.last_canvas_width) < 8 and abs(height - self.last_canvas_height) < 8:
                return
            self.last_canvas_width = width
            self.last_canvas_height = height
            if self.wallpaper_resize_job:
                self.after_cancel(self.wallpaper_resize_job)
            self.wallpaper_resize_job = self.after(140, self.perform_canvas_resize)

        def perform_canvas_resize(self):
            self.wallpaper_resize_job = None
            self.wallpaper_image = None
            self.redraw_wallpaper()
            self.render_messages(preserve_scroll=True)

        def focus_input(self):
            try:
                self.input.focus_set()
            except Exception:
                pass

        def update_provider_label(self):
            provider, _, model, key = active_provider_config(self.cloud_config)
            suffix = self.t("cloud_key_status_ready") if key else self.t("cloud_key_status_missing")
            self.provider_label.config(text=f"{CLOUD_PROVIDERS[provider]['name']} / {model} / {suffix}")

        def append(self, role, content):
            self.add_message_bubble(role, content)

        def add_message_bubble(self, role, content, redraw=True):
            name = self.t("you_name") if role == "user" else "CloudAI"
            is_user = role == "user"
            bubble_bg = self.colors["user_bubble"] if is_user else self.colors["ai_bubble"]
            bubble_fg = self.colors["user_text"] if is_user else self.colors["ai_text"]
            wrap = max(360, int(max(self.canvas.winfo_width(), self.winfo_width()) * 0.88))
            box = tk.Frame(
                self.canvas,
                bg=bubble_bg,
                padx=13,
                pady=9,
                highlightthickness=1,
                highlightbackground=self.colors["user_bubble"] if is_user else self.colors.get("glass_border", self.colors["border"]),
            )
            box.bind("<MouseWheel>", lambda event: self.canvas.event_generate("<MouseWheel>", delta=getattr(event, "delta", 0)), add="+")
            box.bind("<Button-4>", lambda event: self.canvas.event_generate("<Button-4>"), add="+")
            box.bind("<Button-5>", lambda event: self.canvas.event_generate("<Button-5>"), add="+")
            tk.Label(box, text=name, bg=bubble_bg, fg=bubble_fg,
                     font=(get_platform_font(), 9, "bold"), anchor="w").pack(anchor="w")
            tk.Label(box, text=content, bg=bubble_bg, fg=bubble_fg,
                     font=(get_platform_font(), 12), wraplength=wrap, justify="left").pack(anchor="w")
            for child in box.winfo_children():
                child.bind("<MouseWheel>", lambda event: self.canvas.event_generate("<MouseWheel>", delta=getattr(event, "delta", 0)), add="+")
                child.bind("<Button-4>", lambda event: self.canvas.event_generate("<Button-4>"), add="+")
                child.bind("<Button-5>", lambda event: self.canvas.event_generate("<Button-5>"), add="+")
            box.update_idletasks()
            bubble_width = min(max(box.winfo_reqwidth(), 120), wrap + 34)
            bubble_height = max(box.winfo_reqheight(), 44)
            x = self.canvas.winfo_width() - 22 if is_user else 22
            y = self.content_height + 16
            window_id = self.canvas.create_window(x, y, window=box, anchor="ne" if is_user else "nw", width=bubble_width)
            self.message_windows.append((window_id, box))
            self.content_height += bubble_height + 14
            self.raise_message_windows()
            if redraw:
                self.after(30, self.update_scroll_region)

        def append_status(self, content):
            box = tk.Frame(self.canvas, bg=self.colors["panel"], padx=13, pady=9)
            tk.Label(box, text=content, bg=self.colors["panel"], fg=self.colors["muted"],
                     font=(get_platform_font(), 12)).pack(anchor="w")
            window_id = self.canvas.create_window(22, self.content_height + 16, window=box, anchor="nw")
            self.status_window = (window_id, box)
            self.content_height += max(box.winfo_reqheight(), 44) + 14
            self.raise_message_windows()
            self.after(30, self.update_scroll_region)

        def replace_status(self, role, content):
            try:
                window_id, box = self.status_window
                box.destroy()
                self.canvas.delete(window_id)
                self.status_window = None
            except Exception:
                pass
            self.append(role, content)

        def new_chat(self):
            self.current_chat_path = save_cloud_chat(self.current_chat_path, self.messages, self.local_config)
            self.messages = []
            self.current_chat_path = ""
            self.render_messages()

        def show_about(self):
            messagebox.showinfo(APP_NAME, self.t("cloud_about", version=APP_VERSION))

        def import_files(self):
            paths = filedialog.askopenfilenames(
                title=self.t("cloud_import_file"),
                filetypes=[
                    ("Documents and Images", "*.doc *.docx *.txt *.md *.markdown *.csv *.tsv *.json *.jsonl *.yaml *.yml *.xml *.html *.htm *.rtf *.log *.ini *.cfg *.conf *.toml *.py *.js *.ts *.java *.c *.cpp *.h *.hpp *.cs *.go *.rs *.swift *.kt *.php *.rb *.sh *.bat *.ps1 *.sql *.css *.png *.jpg *.jpeg *.gif *.bmp *.webp *.tiff *.tif *.heic *.heif *.avif *.ico"),
                    ("Documents", "*.doc *.docx *.txt *.md *.markdown *.csv *.tsv *.json *.jsonl *.yaml *.yml *.xml *.html *.htm *.rtf *.log *.ini *.cfg *.conf *.toml *.py *.js *.ts *.java *.c *.cpp *.h *.hpp *.cs *.go *.rs *.swift *.kt *.php *.rb *.sh *.bat *.ps1 *.sql *.css"),
                    ("Images", "*.png *.jpg *.jpeg *.gif *.bmp *.webp *.tiff *.tif *.heic *.heif *.avif *.ico"),
                    ("All files", "*.*"),
                ],
            )
            self.pending_files = [path for path in paths if os.path.exists(path)]
            if paths and not self.pending_files:
                messagebox.showerror(APP_NAME, self.t("cloud_file_error"))
            self.update_file_label()
            self.update_send_button_state()

        def update_file_label(self):
            if not hasattr(self, "file_label"):
                return
            if not self.pending_files:
                self.file_label.config(text="")
                return
            names = ", ".join(os.path.basename(path) for path in self.pending_files)
            self.file_label.config(text=self.t("cloud_file_imported", names=names))

        def show_history(self):
            self.current_chat_path = save_cloud_chat(self.current_chat_path, self.messages, self.local_config)
            win = tk.Toplevel(self)
            win.title(self.t("cloud_history"))
            win.geometry("440x520")
            win.configure(bg=self.colors["panel"])
            tk.Label(win, text=self.t("cloud_history"), bg=self.colors["panel"], fg=self.colors["text"],
                     font=(get_platform_font(), 18, "bold")).pack(anchor="w", padx=18, pady=(18, 10))
            list_frame = tk.Frame(win, bg=self.colors["panel"])
            list_frame.pack(fill="both", expand=True, padx=18, pady=(0, 18))
            chats = list_cloud_chats()
            if not chats:
                tk.Label(list_frame, text=self.t("cloud_history_empty"), bg=self.colors["panel"],
                         fg=self.colors["muted"], font=(get_platform_font(), 12)).pack(anchor="w", pady=10)
                return

            def load(path):
                self.current_chat_path = save_cloud_chat(self.current_chat_path, self.messages, self.local_config)
                self.messages = open_cloud_chat(path)
                self.current_chat_path = path
                self.render_messages()
                win.destroy()

            for chat in chats:
                text = f"{chat['title']}\n{chat.get('updated_at', '')[:19].replace('T', ' ')}"
                btn = tk.Button(list_frame, text=text, command=lambda p=chat["path"]: load(p),
                                bg=self.colors["surface"], fg=self.colors["text"],
                                activebackground=self.colors["surface"], activeforeground=self.colors["text"],
                                relief="flat", bd=0, padx=12, pady=10, anchor="w",
                                justify="left", font=(get_platform_font(), 11))
                btn.pack(fill="x", pady=5)

        def schedule_send_button_update(self, _event=None):
            if self.send_update_job:
                try:
                    self.after_cancel(self.send_update_job)
                except Exception:
                    pass
            delay = 80 if platform.system() == "Darwin" else 35
            self.send_update_job = self.after(delay, self.update_send_button_state)

        def handle_text_modified(self, _event=None):
            try:
                self.input.edit_modified(False)
            except Exception:
                pass
            self.schedule_send_button_update()

        def update_send_button_state(self, _event=None):
            content = self.input.get("1.0", "end-1c").strip() if hasattr(self, "input") else ""
            enabled = (bool(content) or bool(self.pending_files)) and not self.asking
            bg = "#2563eb" if enabled else self.colors["disabled"]
            fg = "#ffffff" if enabled else self.colors["muted"]
            self.send_button._cloudai_enabled = enabled
            self.send_button._cloudai_bg = bg
            self.send_button.config(bg=bg, fg=fg, cursor="hand2" if enabled else "arrow")

        def update_scroll_region(self):
            height = max(self.content_height + 20, self.canvas.winfo_height())
            self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(), height))
            self.raise_message_windows()
            self.canvas.yview_moveto(1.0)

        def raise_message_windows(self):
            if self.wallpaper_item is not None:
                self.canvas.tag_lower(self.wallpaper_item)
            for window_id, _box in self.message_windows:
                self.canvas.tag_raise(window_id)
            if self.status_window:
                self.canvas.tag_raise(self.status_window[0])

        def render_messages(self, preserve_scroll=False):
            for window_id, box in self.message_windows:
                try:
                    box.destroy()
                except Exception:
                    pass
                self.canvas.delete(window_id)
            self.message_windows = []
            if self.status_window:
                try:
                    self.status_window[1].destroy()
                    self.canvas.delete(self.status_window[0])
                except Exception:
                    pass
                self.status_window = None
            self.content_height = 0
            for item in self.messages:
                self.add_message_bubble(item.get("role", "assistant"), item.get("content", ""), redraw=False)
            self.raise_message_windows()
            if preserve_scroll:
                height = max(self.content_height + 20, self.canvas.winfo_height())
                self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(), height))
            else:
                self.after(30, self.update_scroll_region)

        def load_wallpaper_image(self, path, width, height):
            if not path or not os.path.exists(path) or width <= 2 or height <= 2:
                return None
            try:
                from PIL import Image, ImageOps, ImageTk
                image = Image.open(path)
                image = ImageOps.exif_transpose(image).convert("RGB")
                image = ImageOps.fit(image, (width, height), method=Image.LANCZOS)
                return ImageTk.PhotoImage(image)
            except Exception:
                try:
                    image = tk.PhotoImage(file=path)
                    target_w = max(int(width), 1)
                    target_h = max(int(height), 1)
                    img_w = max(image.width(), 1)
                    img_h = max(image.height(), 1)
                    cover_scale = max(target_w / img_w, target_h / img_h)
                    if cover_scale > 1:
                        factor = max(int(cover_scale + 0.999), 1)
                        image = image.zoom(factor, factor)
                        img_w *= factor
                        img_h *= factor
                    else:
                        factor = 1
                        while img_w // (factor + 1) >= target_w and img_h // (factor + 1) >= target_h:
                            factor += 1
                        if factor > 1:
                            image = image.subsample(factor, factor)
                    return image
                except Exception:
                    return None

        def reload_wallpaper(self):
            self.wallpaper_resize_job = None
            self.apply_wallpaper(self.local_config.get("wallpaper_path", ""), False)

        def apply_wallpaper(self, path, show_error=True):
            if not path:
                if self.wallpaper_item:
                    self.canvas.delete(self.wallpaper_item)
                    self.wallpaper_item = None
                return True
            image = self.load_wallpaper_image(path, max(self.canvas.winfo_width(), 1), max(self.canvas.winfo_height(), 1))
            if image is None:
                if show_error:
                    messagebox.showerror(APP_NAME, self.t("cloud_wallpaper_error"))
                return False
            self.wallpaper_image = image
            self.redraw_wallpaper()
            return True

        def redraw_wallpaper(self):
            if self.wallpaper_image is None:
                return
            x = max(self.canvas.winfo_width(), 1) // 2
            y = max(self.canvas.winfo_height(), 1) // 2
            if self.wallpaper_item is None:
                self.wallpaper_item = self.canvas.create_image(x, y, image=self.wallpaper_image, anchor="center")
            else:
                self.canvas.coords(self.wallpaper_item, x, y)
                self.canvas.itemconfigure(self.wallpaper_item, image=self.wallpaper_image)
            self.raise_message_windows()

        def choose_wallpaper(self):
            path = filedialog.askopenfilename(
                title=self.t("cloud_wallpaper"),
                filetypes=[("Images", "*.png *.jpg *.jpeg *.gif *.bmp *.webp"), ("All files", "*.*")],
            )
            if not path:
                return
            if self.apply_wallpaper(path, True):
                self.local_config["wallpaper_path"] = path
                save_config(self.local_config)
                self.rebuild_ui()

        def export_current_chat(self):
            if not self.messages:
                messagebox.showinfo(APP_NAME, self.t("cloud_export_empty"))
                return
            path = export_chat_markdown(self.messages)
            messagebox.showinfo(APP_NAME, self.t("cloud_export_done", path=path))

        def handle_enter(self, event):
            if event.state & 0x0001:
                return None
            self.send()
            return "break"

        def handle_shift_enter(self, _event):
            self.input.insert("insert", "\n")
            self.after(1, self.schedule_send_button_update)
            return "break"

        def send(self):
            if self.asking:
                return
            content = self.input.get("1.0", "end-1c").strip()
            request_files = list(self.pending_files)
            if not content and not request_files:
                return
            provider, _, _, key = active_provider_config(self.cloud_config)
            if not key:
                messagebox.showinfo(APP_NAME, self.t("cloud_no_key"))
                return
            self.input.delete("1.0", "end")
            self.pending_files = []
            self.update_file_label()
            self.update_send_button_state()
            display_content = content
            if request_files:
                names = ", ".join(os.path.basename(path) for path in request_files)
                display_content = (display_content + "\n\n" if display_content else "") + self.t("cloud_file_notice", names=names)
            self.messages.append({"role": "user", "content": content})
            self.append("user", display_content)
            self.append_status(self.t("cloud_thinking"))
            self.asking = True
            self.update_send_button_state()
            threading.Thread(target=self.ask_background, args=(request_files,), daemon=True).start()

        def ask_background(self, request_files):
            try:
                answer = ask_cloudai(self.messages, self.cloud_config, request_files)
            except Exception as exc:
                log_cloud_error(exc)
                answer = str(exc) if str(exc) != "missing_api_key" else self.t("cloud_no_key")
            self.after(0, lambda: self.finish_answer(answer))

        def finish_answer(self, answer):
            self.asking = False
            self.messages.append({"role": "assistant", "content": answer})
            self.replace_status("assistant", answer)
            self.current_chat_path = save_cloud_chat(self.current_chat_path, self.messages, self.local_config)
            self.update_send_button_state()

        def show_language(self):
            win = tk.Toplevel(self)
            win.title(self.t("language_title"))
            win.geometry("420x180")
            win.configure(bg=self.colors["panel"])
            options = {item["name"]: code for code, item in LANGUAGE_OPTIONS.items()}
            current = LANGUAGE_OPTIONS[get_lang(self.local_config)]["name"]
            var = tk.StringVar(value=current)
            ttk.Combobox(win, textvariable=var, values=list(options.keys()), state="readonly").pack(fill="x", padx=18, pady=24)

            def save():
                self.local_config["language"] = normalize_language(options.get(var.get(), "zh_cn"))
                save_config(self.local_config)
                messagebox.showinfo(APP_NAME, self.t("cloud_language_saved", language=var.get()))
                win.destroy()

            self.styled_button(win, self.t("save"), save, True).pack(anchor="e", padx=18)

        def show_settings(self):
            import threading

            win = tk.Toplevel(self)
            win.title(self.t("cloud_settings"))
            win.geometry("600x680")
            win.configure(bg=self.colors["panel"])
            provider_var = tk.StringVar(value=self.cloud_config.get("provider", "openai_official"))

            footer = tk.Frame(win, bg=self.colors["panel"], highlightthickness=1, highlightbackground=self.colors["border"])
            footer.pack(side="bottom", fill="x", padx=0, pady=0)
            canvas = tk.Canvas(win, bg=self.colors["panel"], highlightthickness=0, bd=0)
            scrollbar = tk.Scrollbar(win, orient="vertical", command=canvas.yview)
            canvas.configure(yscrollcommand=scrollbar.set)
            scrollbar.pack(side="right", fill="y")
            canvas.pack(side="left", fill="both", expand=True)
            content = tk.Frame(canvas, bg=self.colors["panel"])
            content_window = canvas.create_window((0, 0), window=content, anchor="nw")

            def resize_content(event):
                canvas.itemconfigure(content_window, width=event.width)

            def update_scroll(_event=None):
                canvas.configure(scrollregion=canvas.bbox("all"))

            canvas.bind("<Configure>", resize_content)
            content.bind("<Configure>", update_scroll)

            settings_last_scroll_time = [0]

            def scroll_settings(event):
                now = time.monotonic()
                min_interval = 0.008 if platform.system() == "Darwin" else 0.003
                if now - settings_last_scroll_time[0] < min_interval:
                    return "break"
                settings_last_scroll_time[0] = now
                units = get_scroll_units(3)
                if getattr(event, "num", None) == 4:
                    canvas.yview_scroll(-units, "units")
                elif getattr(event, "num", None) == 5:
                    canvas.yview_scroll(units, "units")
                else:
                    raw_delta = getattr(event, "delta", 0)
                    if platform.system() == "Windows":
                        delta = int(-raw_delta / 120) if raw_delta else 0
                    else:
                        delta = -1 if raw_delta > 0 else 1
                    canvas.yview_scroll(delta * units, "units")
                return "break"

            canvas.bind("<MouseWheel>", scroll_settings)
            canvas.bind("<Button-4>", scroll_settings)
            canvas.bind("<Button-5>", scroll_settings)
            content.bind("<MouseWheel>", scroll_settings)

            def section(title):
                tk.Label(content, text=title, bg=self.colors["panel"], fg=self.colors["text"],
                         font=(get_platform_font(), 15, "bold")).pack(anchor="w", padx=18, pady=(18, 8))

            def label(text):
                tk.Label(content, text=text, bg=self.colors["panel"], fg=self.colors["muted"],
                         font=(get_platform_font(), 11)).pack(anchor="w", padx=18, pady=(10, 4))

            section(self.t("language_title"))
            options = {item["name"]: code for code, item in LANGUAGE_OPTIONS.items()}
            current = LANGUAGE_OPTIONS[get_lang(self.local_config)]["name"]
            language_var = tk.StringVar(value=current)
            ttk.Combobox(content, textvariable=language_var, values=list(options.keys()), state="readonly").pack(fill="x", padx=18, pady=4)

            section(self.t("cloud_theme"))
            theme_var = tk.StringVar(value=self.local_config.get("theme", "auto"))
            theme_frame = tk.Frame(content, bg=self.colors["panel"])
            theme_frame.pack(fill="x", padx=18, pady=4)
            for value, label_text in (
                ("light", self.t("cloud_theme_light")),
                ("dark", self.t("cloud_theme_dark")),
                ("auto", self.t("cloud_theme_auto")),
            ):
                tk.Radiobutton(
                    theme_frame,
                    text=label_text,
                    value=value,
                    variable=theme_var,
                    bg=self.colors["panel"],
                    fg=self.colors["text"],
                    selectcolor=self.colors["surface"],
                    activebackground=self.colors["panel"],
                    activeforeground=self.colors["text"],
                ).pack(side="left", padx=(0, 14))

            section(self.t("cloud_provider_config"))
            label(self.t("cloud_provider"))
            provider_combo = ttk.Combobox(
                content,
                textvariable=provider_var,
                values=[code for code in CLOUD_PROVIDERS],
                state="readonly",
            )
            provider_combo.pack(fill="x", padx=18, pady=4)
            base_var = tk.StringVar()
            model_var = tk.StringVar()
            key_var = tk.StringVar()

            def load_provider_fields(_event=None):
                provider = provider_var.get()
                item = self.cloud_config["providers"].setdefault(provider, {})
                base_var.set(item.get("base_url") or CLOUD_PROVIDERS[provider]["base_url"])
                model_var.set(item.get("model") or CLOUD_PROVIDERS[provider]["models"][0])
                key_var.set(mask_key(get_api_key(provider)))

            provider_combo.bind("<<ComboboxSelected>>", load_provider_fields)
            label(self.t("cloud_api_key"))
            tk.Entry(content, textvariable=key_var, show="*", bg=self.colors["input"], fg=self.colors["text"],
                     insertbackground=self.colors["text"]).pack(fill="x", padx=18, pady=4, ipady=5)
            label(self.t("cloud_base_url"))
            tk.Entry(content, textvariable=base_var, bg=self.colors["input"], fg=self.colors["text"],
                     insertbackground=self.colors["text"]).pack(fill="x", padx=18, pady=4, ipady=5)
            label(self.t("cloud_model"))
            tk.Entry(content, textvariable=model_var, bg=self.colors["input"], fg=self.colors["text"],
                     insertbackground=self.colors["text"]).pack(fill="x", padx=18, pady=4, ipady=5)
            load_provider_fields()

            section(self.t("cloud_usage"))
            usage_var = tk.StringVar(value=self.t("cloud_usage_unavailable"))
            usage_label = tk.Label(content, textvariable=usage_var, bg=self.colors["panel"], fg=self.colors["text"],
                                   justify="left", wraplength=500)
            usage_label.pack(fill="x", padx=18, pady=6)

            def refresh_usage():
                usage_var.set(self.t("cloud_usage_loading"))

                def worker():
                    try:
                        summary = fetch_cloud_usage(self.cloud_config)
                        text = self.t("cloud_usage_summary", usage=summary) if summary else self.t("cloud_usage_unavailable")
                    except PermissionError:
                        text = self.t("cloud_no_key")
                    except Exception as exc:
                        log_cloud_error(exc)
                        text = self.t("cloud_usage_unavailable")
                    win.after(0, lambda: usage_var.set(text))

                threading.Thread(target=worker, daemon=True).start()

            self.styled_button(content, self.t("cloud_refresh_usage"), refresh_usage).pack(anchor="w", padx=18, pady=(2, 8))

            section(self.t("cloud_export"))
            actions = tk.Frame(content, bg=self.colors["panel"])
            actions.pack(fill="x", padx=18, pady=4)
            self.styled_button(actions, self.t("cloud_export"), self.export_current_chat).pack(side="left", padx=(0, 8))
            self.styled_button(actions, self.t("cloud_wallpaper"), self.choose_wallpaper).pack(side="left")
            tk.Frame(content, bg=self.colors["panel"], height=16).pack(fill="x")

            def save():
                self.local_config["language"] = normalize_language(options.get(language_var.get(), "zh_cn"))
                self.local_config["theme"] = theme_var.get()
                save_config(self.local_config)
                provider = provider_var.get()
                item = self.cloud_config["providers"].setdefault(provider, {})
                item["base_url"] = base_var.get().strip()
                item["model"] = model_var.get().strip()
                item["enabled"] = True
                self.cloud_config["provider"] = provider
                self.cloud_config["default_model"] = item["model"]
                key = key_var.get().strip()
                if key and key != MASK:
                    set_api_key(provider, key)
                    messagebox.showinfo(APP_NAME, self.t("cloud_key_saved"))
                save_cloud_config(self.cloud_config)
                self.cloud_config = load_cloud_config()
                self.update_provider_label()
                self.local_config = load_config()
                try:
                    win.destroy()
                except Exception:
                    pass
                self.rebuild_ui()

            self.styled_button(footer, self.t("save"), save, True).pack(side="right", padx=18, pady=12)

        def rebuild_ui(self):
            self.colors = self.palette()
            apply_window_icon(self, self.local_config.get("theme", "auto"))
            saved_messages = list(self.messages)
            for child in self.winfo_children():
                child.destroy()
            self.build()
            for item in saved_messages:
                self.append(item.get("role", "assistant"), item.get("content", ""))

        def close(self):
            if self.closing:
                return
            self.closing = True
            try:
                self.protocol("WM_DELETE_WINDOW", lambda: None)
            except Exception:
                pass
            self.current_chat_path = save_cloud_chat(self.current_chat_path, self.messages, self.local_config)
            for job in (self.send_update_job, self.wallpaper_resize_job):
                if job:
                    try:
                        self.after_cancel(job)
                    except Exception:
                        pass
            def force_destroy():
                try:
                    self.quit()
                except Exception:
                    pass
                try:
                    self.destroy()
                except Exception:
                    pass
            try:
                self.after_idle(force_destroy)
            except Exception:
                force_destroy()

    app = App()
    app.mainloop()
    return True


def main():
    ensure_cloud_dirs()
    run_cloudai_gui()


if __name__ == "__main__":
    main()
