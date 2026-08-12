# -*- coding: utf-8 -*-
import base64
import json
import mimetypes
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import traceback
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

import requests

APP_VERSION = "1.0"
LOCALAI_APP_NAME = "LocalAI"
APP_NAME = "CloudAI"
CLOUDAI_APP_NAME = "CloudAI"
MASK = "********"
SECRET_PREFIX = "v1:"


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
    if machine in {"riscv64", "riscv64gc"}:
        return "riscv64"
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


def detect_harmony_family(info=None):
    info = info or {}
    marker = " ".join(
        str(info.get(key, "")).lower()
        for key in ("ID", "ID_LIKE", "NAME", "PRETTY_NAME", "VARIANT", "VERSION_CODENAME")
    )
    env_marker = " ".join(
        str(os.environ.get(key, "")).lower()
        for key in ("OHOS_SDK_HOME", "HARMONYOS_SDK_HOME", "DEVECO_SDK_HOME")
    )
    marker = f"{marker} {env_marker}"
    if "openharmony" in marker or "ohos" in marker:
        return "openharmony"
    if "harmonyos" in marker or "harmony os" in marker:
        return "harmonyos"
    return ""


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
        apple_silicon = arch == "arm64"
        profile.update({
            "name": f"macOS {mac_version}" if mac_version else "macOS",
            "family": "macos-apple-silicon" if apple_silicon else "macos-intel",
            "targeted": targeted,
            "ui_scale_bias": 1.06 if apple_silicon else 1.01,
            "scroll_units": 1,
            "font": "SF Pro Text" if version >= (10, 15) or not version else "Helvetica Neue",
            "apple_silicon": apple_silicon,
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
        harmony_family = detect_harmony_family(info)
        if harmony_family:
            is_openharmony = harmony_family == "openharmony"
            profile.update({
                "name": info.get("PRETTY_NAME") or ("OpenHarmony" if is_openharmony else "HarmonyOS"),
                "family": harmony_family,
                "targeted": arch in ({"arm64", "x64", "riscv64"} if is_openharmony else {"arm64"}),
                "ui_scale_bias": 1.04,
                "scroll_units": 3,
                "font": "Noto Sans CJK SC",
                "package_targets": ["hap", "app"] if is_openharmony else ["hap"],
            })
            return profile
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
    if family.startswith("macos"):
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


def get_app_data_dir_for(app_name):
    if os.environ.get("CLOUDAI_PORTABLE") == "1":
        return get_base_dir()
    system = platform.system()
    if system == "Windows":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if root:
            return os.path.join(root, app_name)
    if system == "Darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", app_name)
    root = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(root, app_name)


def get_app_data_dir():
    return get_app_data_dir_for(CLOUDAI_APP_NAME)


APP_DATA_DIR = get_app_data_dir()
LOCALAI_DATA_DIR = get_app_data_dir_for(LOCALAI_APP_NAME)
LOG_DIR = os.path.join(APP_DATA_DIR, "logs")
LOCAL_CONFIG_FILE = os.path.join(APP_DATA_DIR, "config.json")
SHARED_LANGUAGE_CONFIG_FILE = os.path.join(LOCALAI_DATA_DIR, "config.json")
CLOUD_CONFIG_DIR = os.path.join(APP_DATA_DIR, "config")
CLOUD_CONFIG_FILE = os.path.join(CLOUD_CONFIG_DIR, "cloudai_config.json")
CLOUD_SECRET_FILE = os.path.join(CLOUD_CONFIG_DIR, "cloudai_secrets.json")
CLOUD_CHAT_DIR = os.path.join(APP_DATA_DIR, "cloud_chats")
CLOUD_MIGRATION_MARKER = os.path.join(CLOUD_CONFIG_DIR, ".legacy_migrated")
LEGACY_CLOUD_CONFIG_DIR = os.path.join(LOCALAI_DATA_DIR, "config")
LEGACY_CLOUD_CONFIG_FILE = os.path.join(LEGACY_CLOUD_CONFIG_DIR, "cloudai_config.json")
LEGACY_CLOUD_SECRET_FILE = os.path.join(LEGACY_CLOUD_CONFIG_DIR, "cloudai_secrets.json")
LEGACY_CLOUD_CHAT_DIR = os.path.join(LOCALAI_DATA_DIR, "cloud_chats")


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
        "cloud_welcome_subtitle": "CloudAI 使用云模型提供商，独立运行，并可沿用已有语言偏好。",
        "cloud_policy_title": "隐私政策与使用指南",
        "cloud_policy_subtitle": "请先了解 CloudAI 的隐私政策和使用指南。文档会随应用一起提供，也可以稍后在应用目录中查看。",
        "cloud_policy_location": "文档位置：{path}",
        "cloud_policy_open": "打开文档",
        "cloud_policy_missing": "未找到文档，请确认 CloudAI 隐私政策.docx 已放在应用目录或打包资源中。",
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
        "cloud_auth_error": "当前 Provider 的 API Key 无效、已过期或没有权限。请打开 CloudAI 设置重新填写 API Key，或切换 Provider。",
        "cloud_request_error": "云端模型请求失败。请检查 Provider、Base URL、模型名称和网络连接。",
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
        "cloud_about": "CloudAI {version}\n云端模型助手\n独立运行，可沿用已有语言偏好。",
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
        "cloud_qemu_bridge": "VirtualWorld",
        "cloud_qemu_title": "VirtualWorld QEMU / UTM",
        "cloud_qemu_input": "QEMU 命令、UTM 包路径或 plist 内容",
        "cloud_qemu_import": "导入文件/包",
        "cloud_qemu_target_platform": "目标平台",
        "cloud_qemu_target_format": "目标格式",
        "cloud_qemu_instruction": "转换/修改指令",
        "cloud_qemu_program": "程序转换",
        "cloud_qemu_ai": "AI 编写",
        "cloud_qemu_check": "AI 输出后进行程序检查",
        "cloud_qemu_result": "转换结果",
        "cloud_qemu_warnings": "兼容性警告",
        "cloud_qemu_guide": "QEMU 转 UTM 会保存为 .utm 包，包内配置文件固定为 config.plist。替换旧虚拟机时：在旧 .utm 上右键显示包内容，备份原 config.plist，再用新生成的 config.plist 替换。",
        "cloud_qemu_copy": "复制结果",
        "cloud_qemu_save": "保存结果",
        "cloud_qemu_saved": "已保存到：{path}",
        "cloud_qemu_copied": "已复制转换结果。",
        "cloud_qemu_empty": "请输入 QEMU 命令或导入 .utm/.plist 文件。",
        "cloud_qemu_no_warnings": "没有兼容性警告。",
        "cloud_qemu_error": "转换失败：{error}",
        "cloud_qemu_ai_no_key": "当前云模型 Provider 未配置 API Key，无法使用 AI 编写。",
    },
    "zh_tw": {
        "cloud_title": "CloudAI",
        "cloud_wizard_title": "CloudAI 首次啟動精靈",
        "cloud_welcome": "歡迎使用 CloudAI",
        "cloud_welcome_subtitle": "CloudAI 使用雲端模型提供商，獨立運行，並可沿用既有語言偏好。",
        "cloud_policy_title": "隱私政策與使用指南",
        "cloud_policy_subtitle": "請先了解 CloudAI 的隱私政策和使用指南。文件會隨應用一起提供，也可以稍後在應用目錄中查看。",
        "cloud_policy_location": "文件位置：{path}",
        "cloud_policy_open": "開啟文件",
        "cloud_policy_missing": "找不到文件，請確認 CloudAI 隱私政策.docx 已放在應用目錄或打包資源中。",
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
        "cloud_auth_error": "目前 Provider 的 API Key 無效、已過期或沒有權限。請開啟 CloudAI 設定重新填寫 API Key，或切換 Provider。",
        "cloud_request_error": "雲端模型請求失敗。請檢查 Provider、Base URL、模型名稱和網路連線。",
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
        "cloud_about": "CloudAI {version}\n雲端模型助理\n獨立運行，可沿用既有語言偏好。",
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
        "cloud_qemu_bridge": "VirtualWorld",
        "cloud_qemu_title": "VirtualWorld QEMU / UTM",
        "cloud_qemu_input": "QEMU 指令、UTM 套件路徑或 plist 內容",
        "cloud_qemu_import": "匯入檔案/套件",
        "cloud_qemu_target_platform": "目標平台",
        "cloud_qemu_target_format": "目標格式",
        "cloud_qemu_instruction": "轉換/修改指令",
        "cloud_qemu_program": "程式轉換",
        "cloud_qemu_ai": "AI 編寫",
        "cloud_qemu_check": "AI 輸出後進行程式檢查",
        "cloud_qemu_result": "轉換結果",
        "cloud_qemu_warnings": "相容性警告",
        "cloud_qemu_guide": "QEMU 轉 UTM 會儲存為 .utm 套件，套件內設定檔固定為 config.plist。替換舊虛擬機時：在舊 .utm 上右鍵顯示套件內容，備份原 config.plist，再用新產生的 config.plist 替換。",
        "cloud_qemu_copy": "複製結果",
        "cloud_qemu_save": "儲存結果",
        "cloud_qemu_saved": "已儲存到：{path}",
        "cloud_qemu_copied": "已複製轉換結果。",
        "cloud_qemu_empty": "請輸入 QEMU 指令或匯入 .utm/.plist 檔案。",
        "cloud_qemu_no_warnings": "沒有相容性警告。",
        "cloud_qemu_error": "轉換失敗：{error}",
        "cloud_qemu_ai_no_key": "目前雲端模型 Provider 未設定 API Key，無法使用 AI 編寫。",
    },
    "en_us": {
        "cloud_title": "CloudAI",
        "cloud_wizard_title": "CloudAI First Launch Wizard",
        "cloud_welcome": "Welcome to CloudAI",
        "cloud_welcome_subtitle": "CloudAI uses cloud model providers, runs independently, and can reuse an existing language preference.",
        "cloud_policy_title": "Privacy Policy and User Guide",
        "cloud_policy_subtitle": "Review CloudAI's privacy policy and user guide before continuing. The document is bundled with the app and can be opened later from the app folder.",
        "cloud_policy_location": "Document location: {path}",
        "cloud_policy_open": "Open Document",
        "cloud_policy_missing": "Document not found. Make sure CloudAI 隐私政策.docx is in the app folder or bundled resources.",
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
        "cloud_auth_error": "The current provider rejected the API Key. Open CloudAI Settings to enter a valid key, or switch provider.",
        "cloud_request_error": "Cloud model request failed. Check the provider, Base URL, model name and network connection.",
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
        "cloud_about": "CloudAI {version}\nCloud model assistant\nRuns independently and can reuse an existing language preference.",
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
        "cloud_qemu_bridge": "VirtualWorld",
        "cloud_qemu_title": "VirtualWorld QEMU / UTM",
        "cloud_qemu_input": "QEMU command, UTM package path, or plist content",
        "cloud_qemu_import": "Import File/Package",
        "cloud_qemu_target_platform": "Target platform",
        "cloud_qemu_target_format": "Target format",
        "cloud_qemu_instruction": "Conversion instruction",
        "cloud_qemu_program": "Program Convert",
        "cloud_qemu_ai": "AI Write",
        "cloud_qemu_check": "Run program check after AI output",
        "cloud_qemu_result": "Conversion Result",
        "cloud_qemu_warnings": "Compatibility Warnings",
        "cloud_qemu_guide": "QEMU to UTM is saved as a .utm package with config.plist inside. To replace an existing VM, show package contents on the old .utm, back up its config.plist, then replace it with the generated config.plist.",
        "cloud_qemu_copy": "Copy Result",
        "cloud_qemu_save": "Save Result",
        "cloud_qemu_saved": "Saved to: {path}",
        "cloud_qemu_copied": "Conversion result copied.",
        "cloud_qemu_empty": "Enter a QEMU command or import a .utm/.plist file.",
        "cloud_qemu_no_warnings": "No compatibility warnings.",
        "cloud_qemu_error": "Conversion failed: {error}",
        "cloud_qemu_ai_no_key": "The current cloud provider has no API Key configured, so AI writing is unavailable.",
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

CLOUDAI_EXTRA_TEXT = {
    "ja": {"cloud_settings": "設定", "cloud_history": "履歴", "cloud_export": "チャットを書き出す", "cloud_wallpaper": "壁紙を変更", "cloud_import_file": "ファイルを読み込む", "cloud_theme": "外観", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "プログラムで変換", "cloud_qemu_ai": "AI で作成", "cloud_qemu_save": "結果を保存", "cloud_qemu_copy": "結果をコピー", "cloud_qemu_warnings": "互換性の警告", "cloud_qemu_no_warnings": "互換性の警告はありません。", "cloud_language_saved": "言語を変更しました：{language}"},
    "fr": {"cloud_settings": "Réglages", "cloud_history": "Historique", "cloud_export": "Exporter le chat", "cloud_wallpaper": "Changer le fond", "cloud_import_file": "Importer un fichier", "cloud_theme": "Apparence", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "Conversion par programme", "cloud_qemu_ai": "Écrire avec l'IA", "cloud_qemu_save": "Enregistrer le résultat", "cloud_qemu_copy": "Copier le résultat", "cloud_qemu_warnings": "Avertissements", "cloud_qemu_no_warnings": "Aucun avertissement.", "cloud_language_saved": "Langue changée : {language}"},
    "de": {"cloud_settings": "Einstellungen", "cloud_history": "Verlauf", "cloud_export": "Chat exportieren", "cloud_wallpaper": "Hintergrund ändern", "cloud_import_file": "Datei importieren", "cloud_theme": "Darstellung", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "Per Programm konvertieren", "cloud_qemu_ai": "Mit KI schreiben", "cloud_qemu_save": "Ergebnis speichern", "cloud_qemu_copy": "Ergebnis kopieren", "cloud_qemu_warnings": "Warnungen", "cloud_qemu_no_warnings": "Keine Warnungen.", "cloud_language_saved": "Sprache geändert zu: {language}"},
    "ko": {"cloud_welcome": "CloudAI에 오신 것을 환영합니다", "cloud_next": "다음", "cloud_send": "보내기", "cloud_settings": "설정", "cloud_history": "기록", "cloud_export": "대화 내보내기", "cloud_wallpaper": "배경 변경", "cloud_import_file": "파일 가져오기", "cloud_theme": "화면 모드", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "프로그램 변환", "cloud_qemu_ai": "AI 작성", "cloud_qemu_save": "결과 저장", "cloud_qemu_copy": "결과 복사", "cloud_qemu_warnings": "호환성 경고", "cloud_qemu_no_warnings": "호환성 경고가 없습니다.", "cloud_language_saved": "언어가 변경되었습니다: {language}"},
    "es": {"cloud_welcome": "Bienvenido a CloudAI", "cloud_next": "Siguiente", "cloud_send": "Enviar", "cloud_settings": "Configuración", "cloud_history": "Historial", "cloud_export": "Exportar chat", "cloud_wallpaper": "Cambiar fondo", "cloud_import_file": "Importar archivo", "cloud_theme": "Apariencia", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "Conversión automática", "cloud_qemu_ai": "Escribir con IA", "cloud_qemu_save": "Guardar resultado", "cloud_qemu_copy": "Copiar resultado", "cloud_qemu_warnings": "Advertencias", "cloud_qemu_no_warnings": "Sin advertencias.", "cloud_language_saved": "Idioma cambiado a: {language}"},
    "it": {"cloud_welcome": "Benvenuto in CloudAI", "cloud_next": "Avanti", "cloud_send": "Invia", "cloud_settings": "Impostazioni", "cloud_history": "Cronologia", "cloud_export": "Esporta chat", "cloud_wallpaper": "Cambia sfondo", "cloud_import_file": "Importa file", "cloud_theme": "Aspetto", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "Conversione programma", "cloud_qemu_ai": "Scrivi con IA", "cloud_qemu_save": "Salva risultato", "cloud_qemu_copy": "Copia risultato", "cloud_qemu_warnings": "Avvisi", "cloud_qemu_no_warnings": "Nessun avviso.", "cloud_language_saved": "Lingua cambiata in: {language}"},
    "pt": {"cloud_welcome": "Bem-vindo ao CloudAI", "cloud_next": "Avançar", "cloud_send": "Enviar", "cloud_settings": "Configurações", "cloud_history": "Histórico", "cloud_export": "Exportar conversa", "cloud_wallpaper": "Alterar papel de parede", "cloud_import_file": "Importar arquivo", "cloud_theme": "Aparência", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "Converter pelo programa", "cloud_qemu_ai": "Escrever com IA", "cloud_qemu_save": "Salvar resultado", "cloud_qemu_copy": "Copiar resultado", "cloud_qemu_warnings": "Avisos", "cloud_qemu_no_warnings": "Sem avisos.", "cloud_language_saved": "Idioma alterado para: {language}"},
    "ru": {"cloud_welcome": "Добро пожаловать в CloudAI", "cloud_next": "Далее", "cloud_send": "Отправить", "cloud_settings": "Настройки", "cloud_history": "История", "cloud_export": "Экспорт чата", "cloud_wallpaper": "Сменить фон", "cloud_import_file": "Импорт файла", "cloud_theme": "Оформление", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "Преобразовать программой", "cloud_qemu_ai": "Написать с ИИ", "cloud_qemu_save": "Сохранить результат", "cloud_qemu_copy": "Копировать результат", "cloud_qemu_warnings": "Предупреждения", "cloud_qemu_no_warnings": "Предупреждений нет.", "cloud_language_saved": "Язык изменён на: {language}"},
    "nl": {"cloud_welcome": "Welkom bij CloudAI", "cloud_next": "Volgende", "cloud_send": "Verzenden", "cloud_settings": "Instellingen", "cloud_history": "Geschiedenis", "cloud_export": "Chat exporteren", "cloud_wallpaper": "Achtergrond wijzigen", "cloud_import_file": "Bestand importeren", "cloud_theme": "Weergave", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "Programma converteren", "cloud_qemu_ai": "Schrijven met AI", "cloud_qemu_save": "Resultaat opslaan", "cloud_qemu_copy": "Resultaat kopiëren", "cloud_qemu_warnings": "Waarschuwingen", "cloud_qemu_no_warnings": "Geen waarschuwingen.", "cloud_language_saved": "Taal gewijzigd naar: {language}"},
    "sv": {"cloud_welcome": "Välkommen till CloudAI", "cloud_next": "Nästa", "cloud_send": "Skicka", "cloud_settings": "Inställningar", "cloud_history": "Historik", "cloud_export": "Exportera chatt", "cloud_wallpaper": "Byt bakgrund", "cloud_import_file": "Importera fil", "cloud_theme": "Utseende", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "Programkonvertering", "cloud_qemu_ai": "Skriv med AI", "cloud_qemu_save": "Spara resultat", "cloud_qemu_copy": "Kopiera resultat", "cloud_qemu_warnings": "Varningar", "cloud_qemu_no_warnings": "Inga varningar.", "cloud_language_saved": "Språk ändrat till: {language}"},
    "da": {"cloud_welcome": "Velkommen til CloudAI", "cloud_next": "Næste", "cloud_send": "Send", "cloud_settings": "Indstillinger", "cloud_history": "Historik", "cloud_export": "Eksportér chat", "cloud_wallpaper": "Skift baggrund", "cloud_import_file": "Importér fil", "cloud_theme": "Udseende", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "Programkonvertering", "cloud_qemu_ai": "Skriv med AI", "cloud_qemu_save": "Gem resultat", "cloud_qemu_copy": "Kopiér resultat", "cloud_qemu_warnings": "Advarsler", "cloud_qemu_no_warnings": "Ingen advarsler.", "cloud_language_saved": "Sprog ændret til: {language}"},
    "fi": {"cloud_welcome": "Tervetuloa CloudAI:hin", "cloud_next": "Seuraava", "cloud_send": "Lähetä", "cloud_settings": "Asetukset", "cloud_history": "Historia", "cloud_export": "Vie keskustelu", "cloud_wallpaper": "Vaihda taustakuva", "cloud_import_file": "Tuo tiedosto", "cloud_theme": "Ulkoasu", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "Ohjelmamuunnos", "cloud_qemu_ai": "Kirjoita AI:lla", "cloud_qemu_save": "Tallenna tulos", "cloud_qemu_copy": "Kopioi tulos", "cloud_qemu_warnings": "Varoitukset", "cloud_qemu_no_warnings": "Ei varoituksia.", "cloud_language_saved": "Kieli vaihdettu: {language}"},
    "no": {"cloud_welcome": "Velkommen til CloudAI", "cloud_next": "Neste", "cloud_send": "Send", "cloud_settings": "Innstillinger", "cloud_history": "Historikk", "cloud_export": "Eksporter chat", "cloud_wallpaper": "Bytt bakgrunn", "cloud_import_file": "Importer fil", "cloud_theme": "Utseende", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "Programkonvertering", "cloud_qemu_ai": "Skriv med AI", "cloud_qemu_save": "Lagre resultat", "cloud_qemu_copy": "Kopier resultat", "cloud_qemu_warnings": "Advarsler", "cloud_qemu_no_warnings": "Ingen advarsler.", "cloud_language_saved": "Språk endret til: {language}"},
    "tr": {"cloud_welcome": "CloudAI'ye hoş geldiniz", "cloud_next": "İleri", "cloud_send": "Gönder", "cloud_settings": "Ayarlar", "cloud_history": "Geçmiş", "cloud_export": "Sohbeti dışa aktar", "cloud_wallpaper": "Duvar kâğıdını değiştir", "cloud_import_file": "Dosya içe aktar", "cloud_theme": "Görünüm", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "Programla dönüştür", "cloud_qemu_ai": "AI ile yaz", "cloud_qemu_save": "Sonucu kaydet", "cloud_qemu_copy": "Sonucu kopyala", "cloud_qemu_warnings": "Uyarılar", "cloud_qemu_no_warnings": "Uyarı yok.", "cloud_language_saved": "Dil değiştirildi: {language}"},
    "pl": {"cloud_welcome": "Witamy w CloudAI", "cloud_next": "Dalej", "cloud_send": "Wyślij", "cloud_settings": "Ustawienia", "cloud_history": "Historia", "cloud_export": "Eksportuj czat", "cloud_wallpaper": "Zmień tło", "cloud_import_file": "Importuj plik", "cloud_theme": "Wygląd", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "Konwersja programu", "cloud_qemu_ai": "Napisz z AI", "cloud_qemu_save": "Zapisz wynik", "cloud_qemu_copy": "Kopiuj wynik", "cloud_qemu_warnings": "Ostrzeżenia", "cloud_qemu_no_warnings": "Brak ostrzeżeń.", "cloud_language_saved": "Zmieniono język na: {language}"},
    "cs": {"cloud_welcome": "Vítejte v CloudAI", "cloud_next": "Další", "cloud_send": "Odeslat", "cloud_settings": "Nastavení", "cloud_history": "Historie", "cloud_export": "Exportovat chat", "cloud_wallpaper": "Změnit pozadí", "cloud_import_file": "Importovat soubor", "cloud_theme": "Vzhled", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "Programový převod", "cloud_qemu_ai": "Psát pomocí AI", "cloud_qemu_save": "Uložit výsledek", "cloud_qemu_copy": "Kopírovat výsledek", "cloud_qemu_warnings": "Varování", "cloud_qemu_no_warnings": "Žádná varování.", "cloud_language_saved": "Jazyk změněn na: {language}"},
    "uk": {"cloud_welcome": "Ласкаво просимо до CloudAI", "cloud_next": "Далі", "cloud_send": "Надіслати", "cloud_settings": "Налаштування", "cloud_history": "Історія", "cloud_export": "Експорт чату", "cloud_wallpaper": "Змінити фон", "cloud_import_file": "Імпорт файлу", "cloud_theme": "Вигляд", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "Програмне перетворення", "cloud_qemu_ai": "Написати з ШІ", "cloud_qemu_save": "Зберегти результат", "cloud_qemu_copy": "Копіювати результат", "cloud_qemu_warnings": "Попередження", "cloud_qemu_no_warnings": "Попереджень немає.", "cloud_language_saved": "Мову змінено на: {language}"},
    "el": {"cloud_welcome": "Καλώς ήρθατε στο CloudAI", "cloud_next": "Επόμενο", "cloud_send": "Αποστολή", "cloud_settings": "Ρυθμίσεις", "cloud_history": "Ιστορικό", "cloud_export": "Εξαγωγή συνομιλίας", "cloud_wallpaper": "Αλλαγή φόντου", "cloud_import_file": "Εισαγωγή αρχείου", "cloud_theme": "Εμφάνιση", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "Μετατροπή προγράμματος", "cloud_qemu_ai": "Σύνταξη με AI", "cloud_qemu_save": "Αποθήκευση αποτελέσματος", "cloud_qemu_copy": "Αντιγραφή αποτελέσματος", "cloud_qemu_warnings": "Προειδοποιήσεις", "cloud_qemu_no_warnings": "Δεν υπάρχουν προειδοποιήσεις.", "cloud_language_saved": "Η γλώσσα άλλαξε σε: {language}"},
    "ar": {"cloud_welcome": "مرحبًا بك في CloudAI", "cloud_next": "التالي", "cloud_send": "إرسال", "cloud_settings": "الإعدادات", "cloud_history": "السجل", "cloud_export": "تصدير المحادثة", "cloud_wallpaper": "تغيير الخلفية", "cloud_import_file": "استيراد ملف", "cloud_theme": "المظهر", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "تحويل بالبرنامج", "cloud_qemu_ai": "كتابة بالذكاء الاصطناعي", "cloud_qemu_save": "حفظ النتيجة", "cloud_qemu_copy": "نسخ النتيجة", "cloud_qemu_warnings": "تحذيرات", "cloud_qemu_no_warnings": "لا توجد تحذيرات.", "cloud_language_saved": "تم تغيير اللغة إلى: {language}"},
    "mn": {"cloud_welcome": "CloudAI-д тавтай морил", "cloud_next": "Дараах", "cloud_send": "Илгээх", "cloud_settings": "Тохиргоо", "cloud_history": "Түүх", "cloud_export": "Чат экспортлох", "cloud_wallpaper": "Дэвсгэр солих", "cloud_import_file": "Файл импортлох", "cloud_theme": "Харагдах байдал", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "Программаар хөрвүүлэх", "cloud_qemu_ai": "AI-аар бичих", "cloud_qemu_save": "Үр дүн хадгалах", "cloud_qemu_copy": "Үр дүн хуулах", "cloud_qemu_warnings": "Анхааруулга", "cloud_qemu_no_warnings": "Анхааруулга байхгүй.", "cloud_language_saved": "Хэл солигдлоо: {language}"},
    "th": {"cloud_welcome": "ยินดีต้อนรับสู่ CloudAI", "cloud_next": "ถัดไป", "cloud_send": "ส่ง", "cloud_settings": "การตั้งค่า", "cloud_history": "ประวัติ", "cloud_export": "ส่งออกแชต", "cloud_wallpaper": "เปลี่ยนวอลเปเปอร์", "cloud_import_file": "นำเข้าไฟล์", "cloud_theme": "รูปลักษณ์", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "แปลงด้วยโปรแกรม", "cloud_qemu_ai": "เขียนด้วย AI", "cloud_qemu_save": "บันทึกผลลัพธ์", "cloud_qemu_copy": "คัดลอกผลลัพธ์", "cloud_qemu_warnings": "คำเตือน", "cloud_qemu_no_warnings": "ไม่มีคำเตือน", "cloud_language_saved": "เปลี่ยนภาษาเป็น: {language}"},
    "vi": {"cloud_welcome": "Chào mừng đến với CloudAI", "cloud_next": "Tiếp theo", "cloud_send": "Gửi", "cloud_settings": "Cài đặt", "cloud_history": "Lịch sử", "cloud_export": "Xuất trò chuyện", "cloud_wallpaper": "Đổi hình nền", "cloud_import_file": "Nhập tệp", "cloud_theme": "Giao diện", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "Chuyển đổi bằng chương trình", "cloud_qemu_ai": "Viết bằng AI", "cloud_qemu_save": "Lưu kết quả", "cloud_qemu_copy": "Sao chép kết quả", "cloud_qemu_warnings": "Cảnh báo", "cloud_qemu_no_warnings": "Không có cảnh báo.", "cloud_language_saved": "Đã đổi ngôn ngữ sang: {language}"},
    "id": {"cloud_welcome": "Selamat datang di CloudAI", "cloud_next": "Berikutnya", "cloud_send": "Kirim", "cloud_settings": "Pengaturan", "cloud_history": "Riwayat", "cloud_export": "Ekspor chat", "cloud_wallpaper": "Ganti wallpaper", "cloud_import_file": "Impor file", "cloud_theme": "Tampilan", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "Konversi program", "cloud_qemu_ai": "Tulis dengan AI", "cloud_qemu_save": "Simpan hasil", "cloud_qemu_copy": "Salin hasil", "cloud_qemu_warnings": "Peringatan", "cloud_qemu_no_warnings": "Tidak ada peringatan.", "cloud_language_saved": "Bahasa diubah ke: {language}"},
    "ms": {"cloud_welcome": "Selamat datang ke CloudAI", "cloud_next": "Seterusnya", "cloud_send": "Hantar", "cloud_settings": "Tetapan", "cloud_history": "Sejarah", "cloud_export": "Eksport sembang", "cloud_wallpaper": "Tukar kertas dinding", "cloud_import_file": "Import fail", "cloud_theme": "Penampilan", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "Tukar dengan program", "cloud_qemu_ai": "Tulis dengan AI", "cloud_qemu_save": "Simpan hasil", "cloud_qemu_copy": "Salin hasil", "cloud_qemu_warnings": "Amaran", "cloud_qemu_no_warnings": "Tiada amaran.", "cloud_language_saved": "Bahasa ditukar kepada: {language}"},
    "hi": {"cloud_welcome": "CloudAI में आपका स्वागत है", "cloud_next": "अगला", "cloud_send": "भेजें", "cloud_settings": "सेटिंग्स", "cloud_history": "इतिहास", "cloud_export": "चैट निर्यात करें", "cloud_wallpaper": "वॉलपेपर बदलें", "cloud_import_file": "फ़ाइल आयात करें", "cloud_theme": "दिखावट", "cloud_qemu_bridge": "VirtualWorld", "cloud_qemu_title": "VirtualWorld QEMU / UTM", "cloud_qemu_program": "प्रोग्राम से बदलें", "cloud_qemu_ai": "AI से लिखें", "cloud_qemu_save": "परिणाम सहेजें", "cloud_qemu_copy": "परिणाम कॉपी करें", "cloud_qemu_warnings": "चेतावनियाँ", "cloud_qemu_no_warnings": "कोई चेतावनी नहीं.", "cloud_language_saved": "भाषा बदली गई: {language}"},
}
for _code, _values in CLOUDAI_EXTRA_TEXT.items():
    CLOUD_TEXT.setdefault(_code, CLOUD_TEXT["en_us"].copy()).update(_values)
    CHAT_GUI_TEXT.setdefault(_code, CHAT_GUI_TEXT["en_us"].copy()).update(_values)

for _code in list(CHAT_GUI_TEXT):
    if _code == "zh_tw":
        _defaults = {
            "cloud_open_history": "開啟",
            "filetype_documents_images": "文件與圖片",
            "filetype_documents": "文件",
            "filetype_images": "圖片",
            "filetype_all": "所有檔案",
        }
    elif _code.startswith("en"):
        _defaults = {
            "cloud_open_history": "Open",
            "filetype_documents_images": "Documents and Images",
            "filetype_documents": "Documents",
            "filetype_images": "Images",
            "filetype_all": "All files",
        }
    else:
        _defaults = {
            "cloud_open_history": "打开",
            "filetype_documents_images": "文档和图片",
            "filetype_documents": "文档",
            "filetype_images": "图片",
            "filetype_all": "所有文件",
        }
    CLOUD_TEXT.setdefault(_code, CLOUD_TEXT["en_us"].copy()).update({k: v for k, v in _defaults.items() if k not in CLOUD_TEXT.get(_code, {})})
    CHAT_GUI_TEXT[_code].update({k: v for k, v in _defaults.items() if k not in CHAT_GUI_TEXT[_code]})


def normalize_language(value):
    key = str(value or "zh_cn").strip().lower().replace("_", "-")
    return LANGUAGE_ALIASES.get(key, "zh_cn")


def get_lang(config):
    return normalize_language(config.get("language", "zh_cn"))


def ensure_app_dirs():
    os.makedirs(APP_DATA_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(os.path.join(APP_DATA_DIR, "exports"), exist_ok=True)


def migrate_legacy_cloud_data():
    legacy_seen = any(os.path.exists(path) for path in (LEGACY_CLOUD_CONFIG_FILE, LEGACY_CLOUD_SECRET_FILE, LEGACY_CLOUD_CHAT_DIR))
    for source, target in ((LEGACY_CLOUD_CONFIG_FILE, CLOUD_CONFIG_FILE),):
        try:
            if os.path.exists(source) and not os.path.exists(target):
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(source, target)
        except Exception:
            pass
    if not os.path.exists(CLOUD_MIGRATION_MARKER):
        try:
            current = read_json_dict(CLOUD_SECRET_FILE)
            legacy = read_json_dict(LEGACY_CLOUD_SECRET_FILE)
            legacy_config = read_json_dict(LEGACY_CLOUD_CONFIG_FILE)
            changed = False
            for provider in CLOUD_PROVIDERS:
                if raw_secret_is_usable(current.get(provider, "")):
                    continue
                recovered = decode_secret(legacy.get(provider, "")) or recover_provider_key_from_config(legacy_config, provider)
                if recovered:
                    current[provider] = encode_secret(recovered)
                    changed = True
            if changed:
                os.makedirs(os.path.dirname(CLOUD_SECRET_FILE), exist_ok=True)
                with open(CLOUD_SECRET_FILE, "w", encoding="utf-8") as handle:
                    json.dump(current, handle, ensure_ascii=False, indent=2)
        except Exception:
            pass
    try:
        if os.path.isdir(LEGACY_CLOUD_CHAT_DIR):
            os.makedirs(CLOUD_CHAT_DIR, exist_ok=True)
            has_chats = any(name.endswith(".json") for name in os.listdir(CLOUD_CHAT_DIR))
            if not has_chats:
                for name in os.listdir(LEGACY_CLOUD_CHAT_DIR):
                    if name.endswith(".json"):
                        source = os.path.join(LEGACY_CLOUD_CHAT_DIR, name)
                        target = os.path.join(CLOUD_CHAT_DIR, name)
                        if os.path.isfile(source) and not os.path.exists(target):
                            shutil.copy2(source, target)
    except Exception:
        pass
    if legacy_seen:
        try:
            os.makedirs(os.path.dirname(CLOUD_MIGRATION_MARKER), exist_ok=True)
            with open(CLOUD_MIGRATION_MARKER, "w", encoding="utf-8") as handle:
                handle.write(datetime.now().isoformat())
        except Exception:
            pass


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
    elif os.path.exists(SHARED_LANGUAGE_CONFIG_FILE):
        try:
            with open(SHARED_LANGUAGE_CONFIG_FILE, "r", encoding="utf-8") as handle:
                shared = json.load(handle)
            if isinstance(shared, dict):
                for key in ("language", "theme"):
                    if shared.get(key):
                        config[key] = shared[key]
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
    migrate_legacy_cloud_data()


def cloud_text(config, key, **kwargs):
    return chat_gui_text(config, key, **kwargs)


def mask_key(value):
    return MASK if value else ""


def is_masked_key(value):
    text = str(value or "").strip()
    return text == MASK or (text and set(text) == {"*"})


def looks_like_plain_secret(value):
    text = str(value or "").strip()
    if not text or is_masked_key(text):
        return False
    if len(text) < 12 or any(ch.isspace() for ch in text):
        return False
    if text.startswith(("{", "[")) or text.lower() in {"none", "null", "undefined"}:
        return False
    return True


def decode_secret_payload(value, salt):
    raw = base64.urlsafe_b64decode(value.encode("ascii"))
    plain = bytes(byte ^ salt[index % len(salt)] for index, byte in enumerate(raw))
    text = plain.decode("utf-8")
    if not text or any((ord(ch) < 32 and ch not in "\r\n\t") for ch in text):
        return ""
    return text


def encode_secret(value):
    if not value:
        return ""
    raw = value.encode("utf-8")
    salt = platform.node().encode("utf-8") or b"cloudai"
    mixed = bytes(byte ^ salt[index % len(salt)] for index, byte in enumerate(raw))
    return SECRET_PREFIX + base64.urlsafe_b64encode(mixed).decode("ascii")


def decode_secret(value):
    value = str(value or "").strip()
    if not value or is_masked_key(value):
        return ""
    salt = platform.node().encode("utf-8") or b"cloudai"
    try:
        if value.startswith(SECRET_PREFIX):
            return decode_secret_payload(value[len(SECRET_PREFIX):], salt)
        decoded = decode_secret_payload(value, salt)
        if decoded:
            return decoded
    except Exception:
        pass
    return value if looks_like_plain_secret(value) else ""


def read_json_dict(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def recover_provider_key_from_config(config_data, provider):
    providers = config_data.get("providers", {}) if isinstance(config_data, dict) else {}
    item = providers.get(provider, {}) if isinstance(providers, dict) else {}
    raw = item.get("api_key", "") if isinstance(item, dict) else ""
    return decode_secret(raw)


def raw_secret_is_usable(secret_value):
    return bool(decode_secret(secret_value))


def normalize_secret_store(data):
    changed = False
    normalised = {}
    for provider, raw in (data or {}).items():
        if provider not in CLOUD_PROVIDERS:
            continue
        key = decode_secret(raw)
        if not key:
            changed = True
            continue
        encoded = encode_secret(key)
        normalised[provider] = encoded
        if raw != encoded:
            changed = True
    return normalised, changed


def load_secret_store():
    ensure_cloud_dirs()
    data = read_json_dict(CLOUD_SECRET_FILE)
    normalised, changed = normalize_secret_store(data)
    if changed:
        try:
            with open(CLOUD_SECRET_FILE, "w", encoding="utf-8") as handle:
                json.dump(normalised, handle, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return normalised


def save_secret_store(data):
    ensure_cloud_dirs()
    safe = data if isinstance(data, dict) else {}
    with open(CLOUD_SECRET_FILE, "w", encoding="utf-8") as handle:
        json.dump(safe, handle, ensure_ascii=False, indent=2)


def get_api_key(provider):
    return decode_secret(load_secret_store().get(provider, ""))


def set_api_key(provider, api_key):
    secrets = load_secret_store()
    api_key = str(api_key or "").strip()
    if api_key and not is_masked_key(api_key):
        secrets[provider] = encode_secret(api_key)
    elif not api_key:
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
            "primary": "#2563eb",
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
        "primary": "#2563eb",
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


CLOUDAI_POLICY_DOC = "CloudAI 隐私政策.docx"


def bundled_document_path(filename):
    return resource_path(filename)


def read_docx_preview(path, max_chars=2200):
    if not path or not os.path.exists(path):
        return ""
    try:
        with zipfile.ZipFile(path) as archive:
            xml_data = archive.read("word/document.xml")
        root = ElementTree.fromstring(xml_data)
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


def open_document_file(path):
    if not path or not os.path.exists(path):
        return False
    try:
        if platform.system() == "Darwin":
            subprocess.Popen(["open", path])
        elif platform.system() == "Windows":
            os.startfile(path)
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception:
        return False


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


def cloud_exception_key(exc):
    if isinstance(exc, PermissionError) or str(exc) == "missing_api_key":
        return "cloud_no_key"
    if isinstance(exc, requests.exceptions.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (401, 403):
            return "cloud_auth_error"
        return "cloud_request_error"
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.RequestException)):
        return "cloud_request_error"
    text = str(exc).lower()
    if "401" in text or "403" in text or "authorization" in text or "unauthorized" in text:
        return "cloud_auth_error"
    if "missing_api_key" in text:
        return "cloud_no_key"
    return "cloud_request_error"


def cloud_exception_message(config, exc):
    return cloud_text(config, cloud_exception_key(exc))


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
            apply_window_icon(self, self.local_config.get("theme", "auto"))
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
            self.button(cloud_text(self.local_config, "cloud_next"), self.show_policy)

        def show_policy(self):
            self.clear()
            self.label(cloud_text(self.local_config, "cloud_policy_title"), 20, True)
            self.label(cloud_text(self.local_config, "cloud_policy_subtitle"), 11)
            doc_path = bundled_document_path(CLOUDAI_POLICY_DOC)
            location = cloud_text(
                self.local_config,
                "cloud_policy_location",
                path=doc_path if os.path.exists(doc_path) else cloud_text(self.local_config, "cloud_policy_missing"),
            )
            tk.Label(self.body, text=location, bg="#f7f8fb", fg="#4b5563", font=(get_platform_font(), 10), wraplength=480, justify="left").pack(anchor="w", pady=(0, 8))
            text_frame = tk.Frame(self.body, bg="#f7f8fb")
            text_frame.pack(fill="both", expand=True)
            scrollbar = tk.Scrollbar(text_frame)
            scrollbar.pack(side="right", fill="y")
            preview = tk.Text(text_frame, height=8, wrap="word", bg="#ffffff", fg="#111827", relief="solid", bd=1, yscrollcommand=scrollbar.set)
            preview.pack(side="left", fill="both", expand=True)
            scrollbar.config(command=preview.yview)
            preview.insert("1.0", read_docx_preview(doc_path) or cloud_text(self.local_config, "cloud_policy_missing"))
            preview.configure(state="disabled")
            actions = tk.Frame(self.body, bg="#f7f8fb")
            actions.pack(fill="x", pady=10)
            tk.Button(actions, text=cloud_text(self.local_config, "cloud_policy_open"), command=lambda: open_document_file(doc_path), relief="flat").pack(side="left")
            tk.Button(actions, text=cloud_text(self.local_config, "cloud_next"), command=self.show_provider, bg="#2563eb", fg="white", relief="flat", padx=18, pady=8).pack(side="right")

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
            try:
                self.update_idletasks()
            except Exception:
                pass
            try:
                self.quit()
            except Exception:
                pass
            try:
                self.destroy()
            except Exception:
                pass

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
            self.message_layout_width = 0
            self.last_trackpad_scroll_time = 0
            self.status_window = None
            self.pending_files = []
            self.title(f"CloudAI {APP_VERSION}")
            self.colors = self.palette()
            self.configure(bg=self.colors["window"])
            self.configure_ttk_style()
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

        def configure_ttk_style(self):
            try:
                style = ttk.Style(self)
                if platform.system() != "Darwin":
                    try:
                        style.theme_use("clam")
                    except Exception:
                        pass
                style.configure(
                    "TCombobox",
                    fieldbackground=self.colors["input"],
                    background=self.colors["input"],
                    foreground=self.colors["text"],
                    arrowcolor=self.colors["text"],
                    bordercolor=self.colors["border"],
                    lightcolor=self.colors["border"],
                    darkcolor=self.colors["border"],
                )
                style.map(
                    "TCombobox",
                    fieldbackground=[("readonly", self.colors["input"])],
                    foreground=[("readonly", self.colors["text"])],
                    selectbackground=[("readonly", self.colors["surface_hover"])],
                    selectforeground=[("readonly", self.colors["text"])],
                )
            except Exception:
                pass

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

        def center_child_window(self, win, width, height):
            self.update_idletasks()
            screen_w = max(self.winfo_screenwidth(), 1)
            screen_h = max(self.winfo_screenheight(), 1)
            parent_x = self.winfo_rootx()
            parent_y = self.winfo_rooty()
            parent_w = max(self.winfo_width(), width)
            parent_h = max(self.winfo_height(), height)
            x = min(max(parent_x + (parent_w - width) // 2, 0), max(screen_w - width, 0))
            y = min(max(parent_y + (parent_h - height) // 2, 0), max(screen_h - height, 0))
            win.geometry(f"{width}x{height}+{x}+{y}")
            try:
                win.transient(self)
                win.lift()
            except Exception:
                pass

        def styled_entry(self, parent, textvariable, show=None):
            primary_color = self.colors.get("primary", self.colors.get("user_bubble", "#2563eb"))
            return tk.Entry(
                parent,
                textvariable=textvariable,
                show=show or "",
                bg=self.colors["input"],
                fg=self.colors["text"],
                insertbackground=self.colors["text"],
                disabledbackground=self.colors["surface"],
                disabledforeground=self.colors["muted"],
                relief="solid",
                bd=1,
                highlightthickness=1,
                highlightbackground=self.colors["border"],
                highlightcolor=primary_color,
                font=(get_platform_font(), 11),
            )

        def styled_option_menu(self, parent, variable, values):
            primary_color = self.colors.get("primary", self.colors.get("user_bubble", "#2563eb"))
            if not values:
                values = [""]
            if not variable.get():
                variable.set(values[0])
            widget = tk.Menubutton(parent, textvariable=variable, indicatoron=True)
            widget.configure(
                bg=self.colors["input"],
                fg=self.colors["text"],
                activebackground=self.colors["surface_hover"],
                activeforeground=self.colors["text"],
                relief="solid",
                bd=1,
                highlightthickness=1,
                highlightbackground=self.colors["border"],
                highlightcolor=primary_color,
                anchor="w",
                padx=8,
                pady=4,
                font=(get_platform_font(), 11),
            )
            menu = tk.Menu(widget, tearoff=0)
            menu.configure(
                bg=self.colors["surface"],
                fg=self.colors["text"],
                activebackground=primary_color,
                activeforeground="#ffffff",
                tearoff=0,
                font=(get_platform_font(), 11),
            )
            for value in values:
                menu.add_command(label=value, command=lambda selected=value: variable.set(selected))
            widget.configure(menu=menu)
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
            self.styled_button(toolbar, self.t("cloud_qemu_bridge"), self.show_qemu_bridge).pack(side="left", padx=6, pady=8)
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
            self.after_idle(self.mark_canvas_layout_ready)
            self.update_file_label()

        def mark_canvas_layout_ready(self):
            try:
                self.update_idletasks()
                self.message_layout_width = max(self.canvas.winfo_width(), 1)
            except Exception:
                pass

        def stable_canvas_width(self):
            try:
                self.update_idletasks()
            except Exception:
                pass
            return max(
                int(self.canvas.winfo_width() or 0),
                int(self.message_layout_width or 0),
                int(self.winfo_width() or 0) - 48,
                360,
            )

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
            canvas_width = max(self.canvas.winfo_width(), 1)
            if self.local_config.get("wallpaper_path"):
                self.wallpaper_image = None
                self.apply_wallpaper(self.local_config.get("wallpaper_path", ""), False)
            else:
                self.redraw_wallpaper()
            has_messages = bool(self.message_windows or self.status_window)
            if has_messages and abs(canvas_width - self.message_layout_width) >= 8:
                self.message_layout_width = canvas_width
                self.render_messages(preserve_scroll=True)
            elif not has_messages:
                self.message_layout_width = canvas_width

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
            canvas_width = self.stable_canvas_width()
            if not self.message_layout_width:
                self.message_layout_width = canvas_width
            wrap = max(360, int(canvas_width * 0.88))
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
            x = canvas_width - 22 if is_user else 22
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
                    (self.t("filetype_documents_images"), "*.doc *.docx *.txt *.md *.markdown *.csv *.tsv *.json *.jsonl *.yaml *.yml *.xml *.html *.htm *.rtf *.log *.ini *.cfg *.conf *.toml *.py *.js *.ts *.java *.c *.cpp *.h *.hpp *.cs *.go *.rs *.swift *.kt *.php *.rb *.sh *.bat *.ps1 *.sql *.css *.png *.jpg *.jpeg *.gif *.bmp *.webp *.tiff *.tif *.heic *.heif *.avif *.ico"),
                    (self.t("filetype_documents"), "*.doc *.docx *.txt *.md *.markdown *.csv *.tsv *.json *.jsonl *.yaml *.yml *.xml *.html *.htm *.rtf *.log *.ini *.cfg *.conf *.toml *.py *.js *.ts *.java *.c *.cpp *.h *.hpp *.cs *.go *.rs *.swift *.kt *.php *.rb *.sh *.bat *.ps1 *.sql *.css"),
                    (self.t("filetype_images"), "*.png *.jpg *.jpeg *.gif *.bmp *.webp *.tiff *.tif *.heic *.heif *.avif *.ico"),
                    (self.t("filetype_all"), "*.*"),
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
            win.configure(bg=self.colors["panel"])
            self.center_child_window(win, 480, 520)
            tk.Label(win, text=self.t("cloud_history"), bg=self.colors["panel"], fg=self.colors["text"],
                     font=(get_platform_font(), 18, "bold")).pack(anchor="w", padx=18, pady=(18, 10))
            list_frame = tk.Frame(win, bg=self.colors["panel"])
            list_frame.pack(fill="both", expand=True, padx=18, pady=(0, 18))
            chats = list_cloud_chats()
            if not chats:
                tk.Label(list_frame, text=self.t("cloud_history_empty"), bg=self.colors["panel"],
                         fg=self.colors["muted"], font=(get_platform_font(), 12)).pack(anchor="w", pady=10)
                return

            rows = []
            primary_color = self.colors.get("primary", self.colors.get("user_bubble", "#2563eb"))
            listbox = tk.Listbox(
                list_frame,
                bg=self.colors["input"],
                fg=self.colors["text"],
                selectbackground=primary_color,
                selectforeground="#ffffff",
                activestyle="none",
                relief="solid",
                bd=1,
                highlightthickness=1,
                highlightbackground=self.colors["border"],
                highlightcolor=primary_color,
                font=(get_platform_font(), 12),
            )
            scrollbar = tk.Scrollbar(list_frame, orient="vertical", command=listbox.yview)
            listbox.configure(yscrollcommand=scrollbar.set)
            listbox.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")

            def load(path):
                self.current_chat_path = save_cloud_chat(self.current_chat_path, self.messages, self.local_config)
                self.messages = open_cloud_chat(path)
                self.current_chat_path = path
                self.render_messages()
                win.destroy()

            for chat in chats:
                stamp = chat.get("updated_at", "")[:16].replace("T", " ")
                text = f"{chat['title']}    {stamp}".strip()
                rows.append(chat["path"])
                listbox.insert("end", text)

            def open_selected(_event=None):
                selection = listbox.curselection()
                if selection:
                    load(rows[selection[0]])

            listbox.bind("<Double-Button-1>", open_selected)
            listbox.bind("<Return>", open_selected)
            self.styled_button(win, self.t("cloud_open_history"), open_selected, True).pack(anchor="e", padx=18, pady=(0, 16))

        def find_virtualworld_launcher(self):
            app_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
            bundle_dir = Path(getattr(sys, "_MEIPASS", app_dir))
            if platform.system() == "Darwin":
                names = ("VirtualWorld.app", "VirtualWorld")
            elif platform.system() == "Windows":
                names = ("VirtualWorld.exe", "VirtualWorld")
            else:
                names = ("VirtualWorld", "virtualworld")
            roots = (
                app_dir,
                app_dir.parent,
                bundle_dir,
                bundle_dir.parent,
                Path(__file__).resolve().parent,
                Path(__file__).resolve().parent / "dist",
                Path(__file__).resolve().parent / "build" / "virtualworld",
            )
            for root in roots:
                for name in names:
                    candidate = root / name
                    if candidate.exists():
                        return candidate
            found = shutil.which("VirtualWorld") or shutil.which("virtualworld")
            return Path(found) if found else None

        def show_qemu_bridge(self):
            try:
                launcher = self.find_virtualworld_launcher()
                if not launcher:
                    raise FileNotFoundError("VirtualWorld executable was not found next to CloudAI.")
                if platform.system() == "Darwin" and launcher.suffix == ".app":
                    subprocess.Popen(["open", str(launcher)])
                else:
                    subprocess.Popen([str(launcher)])
            except Exception as exc:
                log_cloud_error(exc)
                messagebox.showerror(APP_NAME, self.t("cloud_qemu_error", error=exc))

        def schedule_send_button_update(self, _event=None):
            if self.send_update_job:
                try:
                    self.after_cancel(self.send_update_job)
                except Exception:
                    pass
            delay = 20 if platform.system() == "Darwin" else 25
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
            canvas_width = self.stable_canvas_width()
            height = max(self.content_height + 20, self.canvas.winfo_height())
            self.canvas.configure(scrollregion=(0, 0, canvas_width, height))
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
                answer = cloud_exception_message(self.local_config, exc)
            if not self.closing:
                try:
                    self.after(0, lambda: self.finish_answer(answer))
                except Exception:
                    pass

        def finish_answer(self, answer):
            if self.closing:
                return
            self.asking = False
            self.messages.append({"role": "assistant", "content": answer})
            self.replace_status("assistant", answer)
            self.current_chat_path = save_cloud_chat(self.current_chat_path, self.messages, self.local_config)
            self.update_send_button_state()

        def show_language(self):
            win = tk.Toplevel(self)
            win.title(self.t("language_title"))
            win.configure(bg=self.colors["panel"])
            self.center_child_window(win, 420, 180)
            options = {item["name"]: code for code, item in LANGUAGE_OPTIONS.items()}
            current = LANGUAGE_OPTIONS[get_lang(self.local_config)]["name"]
            var = tk.StringVar(value=current)
            self.styled_option_menu(win, var, list(options.keys())).pack(fill="x", padx=18, pady=24)

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
            win.configure(bg=self.colors["panel"])
            width = min(680, max(560, int(self.winfo_screenwidth() * 0.42)))
            height = min(620, max(520, int(self.winfo_screenheight() * 0.72)))
            self.center_child_window(win, width, height)
            win.minsize(520, 480)
            provider_var = tk.StringVar(value=self.cloud_config.get("provider", "openai_official"))

            footer = tk.Frame(win, bg=self.colors["toolbar"], highlightthickness=1, highlightbackground=self.colors["border"])
            footer.pack(side="bottom", fill="x", padx=0, pady=0)
            content = tk.Frame(win, bg=self.colors["panel"])
            content.pack(side="top", fill="both", expand=True, padx=16, pady=(8, 0))

            def section(title):
                tk.Label(content, text=title, bg=self.colors["panel"], fg=self.colors["text"],
                         font=(get_platform_font(), 14, "bold")).pack(anchor="w", padx=2, pady=(10, 4))

            def label(text):
                tk.Label(content, text=text, bg=self.colors["panel"], fg=self.colors["muted"],
                         font=(get_platform_font(), 10)).pack(anchor="w", padx=2, pady=(6, 2))

            section(self.t("language_title"))
            options = {item["name"]: code for code, item in LANGUAGE_OPTIONS.items()}
            current = LANGUAGE_OPTIONS[get_lang(self.local_config)]["name"]
            language_var = tk.StringVar(value=current)
            self.styled_option_menu(content, language_var, list(options.keys())).pack(fill="x", padx=2, pady=2)

            section(self.t("cloud_theme"))
            theme_var = tk.StringVar(value=self.local_config.get("theme", "auto"))
            theme_frame = tk.Frame(content, bg=self.colors["panel"])
            theme_frame.pack(fill="x", padx=2, pady=2)
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
            provider_menu = self.styled_option_menu(content, provider_var, [code for code in CLOUD_PROVIDERS])
            provider_menu.pack(fill="x", padx=2, pady=2)
            base_var = tk.StringVar()
            model_var = tk.StringVar()
            key_var = tk.StringVar()

            def load_provider_fields(_event=None):
                provider = provider_var.get()
                if provider not in CLOUD_PROVIDERS:
                    provider = next(iter(CLOUD_PROVIDERS))
                    provider_var.set(provider)
                item = self.cloud_config["providers"].setdefault(provider, {})
                base_var.set(item.get("base_url") or CLOUD_PROVIDERS[provider]["base_url"])
                model_var.set(item.get("model") or CLOUD_PROVIDERS[provider]["models"][0])
                key_var.set(mask_key(get_api_key(provider)))

            provider_var.trace_add("write", lambda *_args: load_provider_fields())
            label(self.t("cloud_api_key"))
            self.styled_entry(content, key_var, show="*").pack(fill="x", padx=2, pady=2, ipady=4)
            label(self.t("cloud_base_url"))
            self.styled_entry(content, base_var).pack(fill="x", padx=2, pady=2, ipady=4)
            label(self.t("cloud_model"))
            self.styled_entry(content, model_var).pack(fill="x", padx=2, pady=2, ipady=4)
            load_provider_fields()

            section(self.t("cloud_usage"))
            usage_var = tk.StringVar(value=self.t("cloud_usage_unavailable"))
            usage_label = tk.Label(content, textvariable=usage_var, bg=self.colors["panel"], fg=self.colors["text"],
                                   justify="left", wraplength=max(420, width - 56))
            usage_label.pack(fill="x", padx=2, pady=3)

            def refresh_usage():
                usage_var.set(self.t("cloud_usage_loading"))

                def worker():
                    try:
                        summary = fetch_cloud_usage(self.cloud_config)
                        text = self.t("cloud_usage_summary", usage=summary) if summary else self.t("cloud_usage_unavailable")
                    except Exception as exc:
                        log_cloud_error(exc)
                        text = cloud_exception_message(self.local_config, exc)
                    win.after(0, lambda: usage_var.set(text))

                threading.Thread(target=worker, daemon=True).start()

            self.styled_button(content, self.t("cloud_refresh_usage"), refresh_usage).pack(anchor="w", padx=2, pady=(0, 4))

            section(self.t("cloud_export"))
            actions = tk.Frame(content, bg=self.colors["panel"])
            actions.pack(fill="x", padx=2, pady=2)
            self.styled_button(actions, self.t("cloud_export"), self.export_current_chat).pack(side="left", padx=(0, 8))
            self.styled_button(actions, self.t("cloud_wallpaper"), self.choose_wallpaper).pack(side="left")

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
            self.configure_ttk_style()
            apply_window_icon(self, self.local_config.get("theme", "auto"))
            saved_messages = list(self.messages)
            self.message_windows = []
            self.status_window = None
            self.content_height = 0
            self.wallpaper_item = None
            self.wallpaper_image = None
            self.last_canvas_width = 0
            self.last_canvas_height = 0
            self.message_layout_width = 0
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
            try:
                self.current_chat_path = save_cloud_chat(self.current_chat_path, self.messages, self.local_config)
            except Exception as exc:
                log_cloud_error(exc)
            for job in (self.send_update_job, self.wallpaper_resize_job):
                if job:
                    try:
                        self.after_cancel(job)
                    except Exception:
                        pass
            for child in list(self.winfo_children()):
                try:
                    child.destroy()
                except Exception:
                    pass
            try:
                self.update_idletasks()
            except Exception:
                pass
            try:
                self.quit()
            except Exception:
                pass
            try:
                self.destroy()
            except Exception:
                pass

            def force_destroy():
                try:
                    if self.winfo_exists():
                        self.destroy()
                except Exception:
                    pass
            try:
                self.after(50, force_destroy)
            except Exception:
                pass

    app = App()
    app.mainloop()
    return True


def main():
    ensure_cloud_dirs()
    run_cloudai_gui()


if __name__ == "__main__":
    main()
