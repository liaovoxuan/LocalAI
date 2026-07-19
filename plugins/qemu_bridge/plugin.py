from __future__ import annotations

import json
import plistlib
from pathlib import Path

from .ai_modify import build_ai_prompt, modify_with_ai_or_rules
from .models import TargetPlatform
from .parser import parse_input
from .standalone import build_utm_plist
from .translator import convert_config


PLUGIN_TEXT = {
    "zh_cn": {
        "qemu_bridge": "QEMU 转换",
        "title": "QEMU / UTM 转换",
        "input": "QEMU 命令或 UTM 包路径",
        "target": "目标平台",
        "convert": "转换",
        "open_utm": "导入文件/包",
        "result": "转换结果",
        "warnings": "兼容性警告",
        "copy": "复制结果",
        "ai_modify": "AI 修改",
        "target_format": "目标格式",
        "instruction": "转换/修改指令",
        "save_as": "保存转换结果",
        "saved": "已保存到：{path}",
        "model_unavailable": "本地模型不可用，已使用程序规则完成修改。",
        "copied": "已复制转换结果。",
        "empty": "请输入 QEMU 命令或选择 .utm 包。",
        "error": "转换失败：{error}",
        "no_warnings": "没有兼容性警告。",
    },
    "zh_tw": {
        "qemu_bridge": "QEMU 轉換",
        "title": "QEMU / UTM 轉換",
        "input": "QEMU 指令、UTM 套件路徑或 plist 內容",
        "target": "目標平台",
        "convert": "轉換",
        "open_utm": "匯入檔案/套件",
        "result": "轉換結果",
        "warnings": "相容性警告",
        "copy": "複製結果",
        "ai_modify": "AI 修改",
        "target_format": "目標格式",
        "instruction": "轉換/修改指令",
        "save_as": "儲存轉換結果",
        "saved": "已儲存到：{path}",
        "model_unavailable": "本地模型不可用，已使用程式規則完成修改。",
        "copied": "已複製轉換結果。",
        "empty": "請輸入 QEMU 指令或選擇 .utm 套件。",
        "error": "轉換失敗：{error}",
        "no_warnings": "沒有相容性警告。",
    },
    "en_us": {
        "qemu_bridge": "QEMU Bridge",
        "title": "QEMU / UTM Bridge",
        "input": "QEMU command, UTM package path, or plist content",
        "target": "Target platform",
        "convert": "Convert",
        "open_utm": "Import File/Package",
        "result": "Conversion Result",
        "warnings": "Compatibility Warnings",
        "copy": "Copy Result",
        "ai_modify": "AI Modify",
        "target_format": "Target format",
        "instruction": "Conversion instruction",
        "save_as": "Save Conversion Result",
        "saved": "Saved to: {path}",
        "model_unavailable": "The local model is unavailable. Rule-based repair was used.",
        "copied": "Conversion result copied.",
        "empty": "Enter a QEMU command or choose a .utm package.",
        "error": "Conversion failed: {error}",
        "no_warnings": "No compatibility warnings.",
    },
}


class QEMUBridgePlugin:
    plugin_id = "localai.qemu_bridge"
    name = "QEMU Bridge"
    version = "0.2.0"

    def register(self, host):
        host.register_tool(
            name="qemu_bridge.convert_command",
            callback=self.convert_command,
            description="Convert QEMU or UTM configuration to a target-platform QEMU command.",
        )
        if hasattr(host, "register_gui_action"):
            host.register_gui_action(
                plugin_id=self.plugin_id,
                label_key="qemu_bridge",
                callback=lambda app: self.show_gui(app),
            )

    def convert_command(self, command, target_platform):
        config = parse_input(command)
        result = convert_config(config, TargetPlatform(target_platform.lower()))
        return {
            "command": result.command,
            "has_errors": result.has_errors,
            "issues": [
                {"level": issue.level, "code": issue.code, "message": issue.message}
                for issue in result.issues
            ],
        }

    def show_gui(self, app):
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk

        colors = getattr(app, "colors", {})
        bg = colors.get("window", "#f7f7f8")
        panel = colors.get("surface", "#ffffff")
        text = colors.get("text", "#111827")
        muted = colors.get("muted", "#6b7280")
        border = colors.get("border", "#d1d5db")
        blue = "#2563eb"
        font_name = getattr(app, "font_name", None) or "TkDefaultFont"

        def tr(key, **kwargs):
            lang = "zh_cn"
            try:
                lang = app.config_data.get("language", "zh_cn")
            except Exception:
                pass
            data = PLUGIN_TEXT.get(lang, PLUGIN_TEXT.get("en_us", PLUGIN_TEXT["zh_cn"]))
            value = data.get(key, PLUGIN_TEXT["zh_cn"].get(key, key))
            return value.format(**kwargs) if kwargs else value

        win = tk.Toplevel(app)
        win.title(tr("title"))
        win.geometry(getattr(app, "child_geometry", lambda w, h: f"{w}x{h}")(800, 720))
        win.configure(bg=bg)

        body = tk.Frame(win, bg=bg, padx=18, pady=16)
        body.pack(fill="both", expand=True)
        tk.Label(body, text=tr("title"), bg=bg, fg=text, font=(font_name, 17, "bold")).pack(anchor="w")
        tk.Label(body, text=tr("input"), bg=bg, fg=muted, font=(font_name, 10)).pack(anchor="w", pady=(14, 4))
        input_box = tk.Text(
            body,
            height=7,
            bg=panel,
            fg=text,
            insertbackground=text,
            relief="flat",
            highlightthickness=1,
            highlightbackground=border,
            highlightcolor=border,
            font=(font_name, 11),
        )
        input_box.pack(fill="x")

        controls = tk.Frame(body, bg=bg)
        controls.pack(fill="x", pady=12)
        tk.Label(controls, text=tr("target"), bg=bg, fg=text, font=(font_name, 11)).pack(side="left")
        target_var = tk.StringVar(value=TargetPlatform.MACOS.value)
        ttk.Combobox(
            controls,
            textvariable=target_var,
            values=[item.value for item in TargetPlatform],
            state="readonly",
            width=14,
        ).pack(side="left", padx=10)
        tk.Label(controls, text=tr("target_format"), bg=bg, fg=text, font=(font_name, 11)).pack(side="left", padx=(18, 0))
        target_format_var = tk.StringVar(value="QEMU")
        ttk.Combobox(
            controls,
            textvariable=target_format_var,
            values=["QEMU", "UTM"],
            state="readonly",
            width=10,
        ).pack(side="left", padx=10)

        tk.Label(body, text=tr("instruction"), bg=bg, fg=muted, font=(font_name, 10)).pack(anchor="w", pady=(0, 4))
        instruction_box = tk.Text(
            body,
            height=3,
            bg=panel,
            fg=text,
            insertbackground=text,
            relief="flat",
            highlightthickness=1,
            highlightbackground=border,
            highlightcolor=border,
            font=(font_name, 11),
            wrap="word",
        )
        instruction_box.pack(fill="x")

        result_label = tk.Label(body, text=tr("result"), bg=bg, fg=text, font=(font_name, 12, "bold"))
        result_label.pack(anchor="w", pady=(6, 4))
        result_box = tk.Text(
            body,
            height=9,
            bg=panel,
            fg=text,
            insertbackground=text,
            relief="flat",
            highlightthickness=1,
            highlightbackground=border,
            highlightcolor=border,
            font=(font_name, 11),
            wrap="word",
        )
        result_box.pack(fill="both", expand=True)

        warning_label = tk.Label(body, text=tr("warnings"), bg=bg, fg=text, font=(font_name, 12, "bold"))
        warning_label.pack(anchor="w", pady=(10, 4))
        warning_box = tk.Text(
            body,
            height=5,
            bg=panel,
            fg=text,
            relief="flat",
            highlightthickness=1,
            highlightbackground=border,
            highlightcolor=border,
            font=(font_name, 10),
            wrap="word",
        )
        warning_box.pack(fill="x")

        def write_text(widget, value):
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", value)
            widget.configure(state="disabled")

        def convert():
            source = input_box.get("1.0", "end-1c").strip()
            if not source:
                messagebox.showinfo(tr("title"), tr("empty"))
                return
            try:
                payload = program_convert_result(source, target_format_var.get(), target_var.get())
                saved_path = save_result_dialog(payload, target_format_var.get(), tr, filedialog)
            except Exception as exc:
                write_text(result_box, "")
                write_text(warning_box, tr("error", error=exc))
                return
            write_text(result_box, payload.command)
            issues = payload.issues
            if issues:
                write_text(warning_box, "\n".join(f"[{item.level}] {item.code}: {item.message}" for item in issues))
            else:
                write_text(warning_box, tr("no_warnings"))
            if saved_path:
                messagebox.showinfo(tr("title"), tr("saved", path=saved_path))

        def choose_utm():
            path = filedialog.askdirectory(title=tr("open_utm"), mustexist=True)
            if not path:
                path = filedialog.askopenfilename(
                    title=tr("open_utm"),
                    filetypes=[
                        ("Supported", "*.utm *.plist *.sh *.cmd *.txt"),
                        ("UTM Package", "*.utm"),
                        ("Property List", "*.plist"),
                        ("QEMU Script", "*.sh *.cmd"),
                        ("All files", "*.*"),
                    ],
                )
            if path:
                input_box.delete("1.0", "end")
                input_box.insert("1.0", load_imported_source(path))

        def copy_result():
            value = result_box.get("1.0", "end-1c").strip()
            if value:
                win.clipboard_clear()
                win.clipboard_append(value)
                messagebox.showinfo(tr("title"), tr("copied"))

        def ai_modify():
            source = input_box.get("1.0", "end-1c").strip()
            if not source:
                messagebox.showinfo(tr("title"), tr("empty"))
                return
            instruction = instruction_box.get("1.0", "end-1c").strip()
            target_format = target_format_var.get().lower()
            ai_text = ""
            prompt = build_ai_prompt(source, target_format, instruction)
            try:
                ai_text = call_local_model(app, prompt)
            except Exception:
                messagebox.showinfo(tr("title"), tr("model_unavailable"))
            try:
                payload = modify_with_ai_or_rules(source, target_format, instruction, ai_text)
                saved_path = save_result_dialog(payload, target_format, tr, filedialog)
            except Exception as exc:
                write_text(result_box, "")
                write_text(warning_box, tr("error", error=exc))
                return
            write_text(result_box, payload.command)
            if payload.issues:
                write_text(warning_box, "\n".join(f"[{item.level}] {item.code}: {item.message}" for item in payload.issues))
            else:
                write_text(warning_box, tr("no_warnings"))
            if saved_path:
                messagebox.showinfo(tr("title"), tr("saved", path=saved_path))

        actions = tk.Frame(body, bg=bg)
        actions.pack(fill="x", pady=(12, 0))
        button_style = {"bg": blue, "fg": "#ffffff", "padx": 16, "pady": 8, "cursor": "hand2", "font": (font_name, 11, "bold")}
        tk.Label(actions, text=tr("open_utm"), **button_style).pack(side="left")
        actions.winfo_children()[-1].bind("<Button-1>", lambda _event: choose_utm())
        tk.Label(actions, text=tr("convert"), **button_style).pack(side="left", padx=8)
        actions.winfo_children()[-1].bind("<Button-1>", lambda _event: convert())
        tk.Label(actions, text=tr("ai_modify"), **button_style).pack(side="left", padx=8)
        actions.winfo_children()[-1].bind("<Button-1>", lambda _event: ai_modify())
        tk.Label(actions, text=tr("copy"), bg=panel, fg=text, padx=16, pady=8, cursor="hand2", font=(font_name, 11)).pack(side="right")
        actions.winfo_children()[-1].bind("<Button-1>", lambda _event: copy_result())


def load_plugin():
    return QEMUBridgePlugin()


def call_local_model(app, prompt: str) -> str:
    import __main__

    ask_model = getattr(__main__, "ask_model", None)
    if not callable(ask_model):
        raise RuntimeError("LocalAI ask_model is unavailable.")
    model = getattr(app, "current_model", "") or getattr(app, "selected_model", "")
    config = getattr(app, "config_data", {}) or {}
    return ask_model([{"role": "user", "content": prompt}], model, config)


def program_convert_result(source: str, target_format: str, target_platform: str):
    target_format = str(target_format or "qemu").strip().lower()
    if target_format == "utm":
        return modify_with_ai_or_rules(source, "utm", "按当前配置进行程序转换。", "")
    config = parse_input(source)
    return convert_config(config, TargetPlatform(str(target_platform or TargetPlatform.MACOS.value).lower()))


def load_imported_source(path: str) -> str:
    source = Path(path)
    if source.is_dir() or source.suffix.lower() == ".utm":
        return str(source)
    if source.suffix.lower() in {".plist", ".sh", ".cmd", ".txt"}:
        return source.read_text(encoding="utf-8", errors="ignore")
    return str(source)


def save_result_dialog(payload, target_format: str, tr, filedialog):
    target_format = str(target_format or "qemu").lower()
    if target_format == "utm":
        path = filedialog.asksaveasfilename(
            title=tr("save_as"),
            defaultextension=".utm",
            filetypes=[("UTM Package", "*.utm"), ("All files", "*.*")],
        )
        if not path:
            return ""
        save_utm_payload(payload, Path(path))
        return path
    path = filedialog.asksaveasfilename(
        title=tr("save_as"),
        defaultextension=".sh",
        filetypes=[("Shell Script", "*.sh"), ("Windows Command", "*.cmd"), ("All files", "*.*")],
    )
    if not path:
        return ""
    Path(path).write_text(payload.command.strip() + "\n", encoding="utf-8")
    return path


def save_utm_payload(payload, path: Path):
    if path.suffix.lower() != ".utm":
        path = path.with_suffix(".utm")
    if path.exists() and path.is_file():
        raise ValueError(f"{path} is a file, not a UTM package directory.")
    path.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(payload.command)
    except Exception:
        data = build_utm_plist(payload.config, "other", [])
    with (path / "config.plist").open("wb") as handle:
        plistlib.dump(data, handle, sort_keys=False)
