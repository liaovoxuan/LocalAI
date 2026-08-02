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
        "guide": "QEMU 转 UTM 会保存为 .utm 包，包内配置文件固定为 config.plist。替换旧虚拟机时：在旧 .utm 上右键显示包内容，备份原 config.plist，再用新生成的 config.plist 替换。",
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
        "guide": "QEMU 轉 UTM 會儲存為 .utm 套件，套件內設定檔固定為 config.plist。替換舊虛擬機時：在舊 .utm 上右鍵顯示套件內容，備份原 config.plist，再用新產生的 config.plist 替換。",
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
        "guide": "QEMU to UTM is saved as a .utm package with config.plist inside. To replace an existing VM, show package contents on the old .utm, back up its config.plist, then replace it with the generated config.plist.",
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
PLUGIN_TEXT["en_gb"] = PLUGIN_TEXT["en_us"]
PLUGIN_TEXT["en_au"] = PLUGIN_TEXT["en_us"]
PLUGIN_TEXT.update({
    "ja": {
        **PLUGIN_TEXT["en_us"],
        "qemu_bridge": "QEMU 変換",
        "title": "QEMU / UTM 変換",
        "target": "対象プラットフォーム",
        "convert": "変換",
        "open_utm": "ファイル/パッケージを読み込む",
        "result": "変換結果",
        "warnings": "互換性の警告",
        "copy": "結果をコピー",
        "ai_modify": "AI で修正",
        "target_format": "対象形式",
        "instruction": "変換/修正指示",
        "save_as": "変換結果を保存",
        "saved": "保存しました：{path}",
        "copied": "変換結果をコピーしました。",
        "empty": "QEMU コマンドを入力するか .utm パッケージを選択してください。",
        "error": "変換に失敗しました：{error}",
        "no_warnings": "互換性の警告はありません。",
    },
    "ko": {"qemu_bridge": "QEMU 변환", "title": "QEMU / UTM 변환", "target": "대상 플랫폼", "convert": "변환", "open_utm": "파일/패키지 가져오기", "result": "변환 결과", "warnings": "호환성 경고", "copy": "결과 복사", "ai_modify": "AI 수정", "target_format": "대상 형식", "instruction": "변환/수정 지시", "save_as": "변환 결과 저장", "saved": "저장됨: {path}", "copied": "변환 결과가 복사되었습니다.", "empty": "QEMU 명령을 입력하거나 .utm 패키지를 선택하세요.", "error": "변환 실패: {error}", "no_warnings": "호환성 경고가 없습니다."},
    "fr": {"qemu_bridge": "Pont QEMU", "title": "Pont QEMU / UTM", "target": "Plateforme cible", "convert": "Convertir", "open_utm": "Importer fichier/paquet", "result": "Résultat", "warnings": "Avertissements", "copy": "Copier", "ai_modify": "Modifier avec l'IA", "target_format": "Format cible", "instruction": "Instruction", "save_as": "Enregistrer le résultat", "saved": "Enregistré : {path}", "copied": "Résultat copié.", "empty": "Saisissez une commande QEMU ou choisissez un paquet .utm.", "error": "Échec de la conversion : {error}", "no_warnings": "Aucun avertissement."},
    "de": {"qemu_bridge": "QEMU-Brücke", "title": "QEMU / UTM-Brücke", "target": "Zielplattform", "convert": "Konvertieren", "open_utm": "Datei/Paket importieren", "result": "Ergebnis", "warnings": "Kompatibilitätswarnungen", "copy": "Kopieren", "ai_modify": "Mit KI ändern", "target_format": "Zielformat", "instruction": "Anweisung", "save_as": "Ergebnis speichern", "saved": "Gespeichert: {path}", "copied": "Ergebnis kopiert.", "empty": "QEMU-Befehl eingeben oder .utm-Paket wählen.", "error": "Konvertierung fehlgeschlagen: {error}", "no_warnings": "Keine Warnungen."},
    "es": {"qemu_bridge": "Puente QEMU", "title": "Puente QEMU / UTM", "target": "Plataforma destino", "convert": "Convertir", "open_utm": "Importar archivo/paquete", "result": "Resultado", "warnings": "Advertencias", "copy": "Copiar", "ai_modify": "Modificar con IA", "target_format": "Formato destino", "instruction": "Instrucción", "save_as": "Guardar resultado", "saved": "Guardado en: {path}", "copied": "Resultado copiado.", "empty": "Introduce un comando QEMU o elige un paquete .utm.", "error": "Error de conversión: {error}", "no_warnings": "Sin advertencias."},
    "it": {"qemu_bridge": "Bridge QEMU", "title": "Bridge QEMU / UTM", "target": "Piattaforma destinazione", "convert": "Converti", "open_utm": "Importa file/pacchetto", "result": "Risultato", "warnings": "Avvisi", "copy": "Copia", "ai_modify": "Modifica con IA", "target_format": "Formato destinazione", "instruction": "Istruzione", "save_as": "Salva risultato", "saved": "Salvato in: {path}", "copied": "Risultato copiato.", "empty": "Inserisci un comando QEMU o scegli un pacchetto .utm.", "error": "Conversione non riuscita: {error}", "no_warnings": "Nessun avviso."},
    "pt": {"qemu_bridge": "Ponte QEMU", "title": "Ponte QEMU / UTM", "target": "Plataforma alvo", "convert": "Converter", "open_utm": "Importar arquivo/pacote", "result": "Resultado", "warnings": "Avisos", "copy": "Copiar", "ai_modify": "Modificar com IA", "target_format": "Formato alvo", "instruction": "Instrução", "save_as": "Salvar resultado", "saved": "Salvo em: {path}", "copied": "Resultado copiado.", "empty": "Digite um comando QEMU ou escolha um pacote .utm.", "error": "Falha na conversão: {error}", "no_warnings": "Sem avisos."},
    "ru": {"qemu_bridge": "Мост QEMU", "title": "Мост QEMU / UTM", "target": "Целевая платформа", "convert": "Преобразовать", "open_utm": "Импорт файла/пакета", "result": "Результат", "warnings": "Предупреждения", "copy": "Копировать", "ai_modify": "Изменить с ИИ", "target_format": "Целевой формат", "instruction": "Инструкция", "save_as": "Сохранить результат", "saved": "Сохранено: {path}", "copied": "Результат скопирован.", "empty": "Введите команду QEMU или выберите пакет .utm.", "error": "Ошибка преобразования: {error}", "no_warnings": "Предупреждений нет."},
    "nl": {"qemu_bridge": "QEMU-brug", "title": "QEMU / UTM-brug", "target": "Doelplatform", "convert": "Converteren", "open_utm": "Bestand/pakket importeren", "result": "Resultaat", "warnings": "Waarschuwingen", "copy": "Kopiëren", "ai_modify": "Wijzigen met AI", "target_format": "Doelformaat", "instruction": "Instructie", "save_as": "Resultaat opslaan", "saved": "Opgeslagen: {path}", "copied": "Resultaat gekopieerd.", "empty": "Voer een QEMU-opdracht in of kies een .utm-pakket.", "error": "Conversie mislukt: {error}", "no_warnings": "Geen waarschuwingen."},
    "sv": {"qemu_bridge": "QEMU-brygga", "title": "QEMU / UTM-brygga", "target": "Målplattform", "convert": "Konvertera", "open_utm": "Importera fil/paket", "result": "Resultat", "warnings": "Varningar", "copy": "Kopiera", "ai_modify": "Ändra med AI", "target_format": "Målformat", "instruction": "Instruktion", "save_as": "Spara resultat", "saved": "Sparat: {path}", "copied": "Resultat kopierat.", "empty": "Ange ett QEMU-kommando eller välj ett .utm-paket.", "error": "Konvertering misslyckades: {error}", "no_warnings": "Inga varningar."},
    "da": {"qemu_bridge": "QEMU-bro", "title": "QEMU / UTM-bro", "target": "Målplatform", "convert": "Konverter", "open_utm": "Importer fil/pakke", "result": "Resultat", "warnings": "Advarsler", "copy": "Kopiér", "ai_modify": "Rediger med AI", "target_format": "Målformat", "instruction": "Instruktion", "save_as": "Gem resultat", "saved": "Gemt: {path}", "copied": "Resultat kopieret.", "empty": "Indtast en QEMU-kommando eller vælg en .utm-pakke.", "error": "Konvertering mislykkedes: {error}", "no_warnings": "Ingen advarsler."},
    "fi": {"qemu_bridge": "QEMU-silta", "title": "QEMU / UTM-silta", "target": "Kohdealusta", "convert": "Muunna", "open_utm": "Tuo tiedosto/paketti", "result": "Tulos", "warnings": "Varoitukset", "copy": "Kopioi", "ai_modify": "Muokkaa AI:lla", "target_format": "Kohdemuoto", "instruction": "Ohje", "save_as": "Tallenna tulos", "saved": "Tallennettu: {path}", "copied": "Tulos kopioitu.", "empty": "Anna QEMU-komento tai valitse .utm-paketti.", "error": "Muunnos epäonnistui: {error}", "no_warnings": "Ei varoituksia."},
    "no": {"qemu_bridge": "QEMU-bro", "title": "QEMU / UTM-bro", "target": "Målplattform", "convert": "Konverter", "open_utm": "Importer fil/pakke", "result": "Resultat", "warnings": "Advarsler", "copy": "Kopier", "ai_modify": "Endre med AI", "target_format": "Målformat", "instruction": "Instruksjon", "save_as": "Lagre resultat", "saved": "Lagret: {path}", "copied": "Resultat kopiert.", "empty": "Skriv inn en QEMU-kommando eller velg en .utm-pakke.", "error": "Konvertering mislyktes: {error}", "no_warnings": "Ingen advarsler."},
    "tr": {"qemu_bridge": "QEMU Köprüsü", "title": "QEMU / UTM Köprüsü", "target": "Hedef platform", "convert": "Dönüştür", "open_utm": "Dosya/paket içe aktar", "result": "Sonuç", "warnings": "Uyarılar", "copy": "Kopyala", "ai_modify": "AI ile değiştir", "target_format": "Hedef biçim", "instruction": "Talimat", "save_as": "Sonucu kaydet", "saved": "Kaydedildi: {path}", "copied": "Sonuç kopyalandı.", "empty": "Bir QEMU komutu girin veya .utm paketi seçin.", "error": "Dönüştürme başarısız: {error}", "no_warnings": "Uyarı yok."},
    "pl": {"qemu_bridge": "Most QEMU", "title": "Most QEMU / UTM", "target": "Platforma docelowa", "convert": "Konwertuj", "open_utm": "Importuj plik/pakiet", "result": "Wynik", "warnings": "Ostrzeżenia", "copy": "Kopiuj", "ai_modify": "Zmień z AI", "target_format": "Format docelowy", "instruction": "Instrukcja", "save_as": "Zapisz wynik", "saved": "Zapisano: {path}", "copied": "Wynik skopiowany.", "empty": "Wpisz polecenie QEMU lub wybierz pakiet .utm.", "error": "Konwersja nie powiodła się: {error}", "no_warnings": "Brak ostrzeżeń."},
    "cs": {"qemu_bridge": "Most QEMU", "title": "Most QEMU / UTM", "target": "Cílová platforma", "convert": "Převést", "open_utm": "Importovat soubor/balíček", "result": "Výsledek", "warnings": "Varování", "copy": "Kopírovat", "ai_modify": "Upravit pomocí AI", "target_format": "Cílový formát", "instruction": "Pokyn", "save_as": "Uložit výsledek", "saved": "Uloženo: {path}", "copied": "Výsledek zkopírován.", "empty": "Zadejte příkaz QEMU nebo vyberte balíček .utm.", "error": "Převod selhal: {error}", "no_warnings": "Žádná varování."},
    "uk": {"qemu_bridge": "Міст QEMU", "title": "Міст QEMU / UTM", "target": "Цільова платформа", "convert": "Перетворити", "open_utm": "Імпорт файлу/пакета", "result": "Результат", "warnings": "Попередження", "copy": "Копіювати", "ai_modify": "Змінити з ШІ", "target_format": "Цільовий формат", "instruction": "Інструкція", "save_as": "Зберегти результат", "saved": "Збережено: {path}", "copied": "Результат скопійовано.", "empty": "Введіть команду QEMU або виберіть пакет .utm.", "error": "Помилка перетворення: {error}", "no_warnings": "Попереджень немає."},
    "el": {"qemu_bridge": "Γέφυρα QEMU", "title": "Γέφυρα QEMU / UTM", "target": "Πλατφόρμα στόχος", "convert": "Μετατροπή", "open_utm": "Εισαγωγή αρχείου/πακέτου", "result": "Αποτέλεσμα", "warnings": "Προειδοποιήσεις", "copy": "Αντιγραφή", "ai_modify": "Τροποποίηση με AI", "target_format": "Μορφή στόχος", "instruction": "Οδηγία", "save_as": "Αποθήκευση αποτελέσματος", "saved": "Αποθηκεύτηκε: {path}", "copied": "Το αποτέλεσμα αντιγράφηκε.", "empty": "Πληκτρολογήστε εντολή QEMU ή επιλέξτε πακέτο .utm.", "error": "Η μετατροπή απέτυχε: {error}", "no_warnings": "Δεν υπάρχουν προειδοποιήσεις."},
    "ar": {"qemu_bridge": "جسر QEMU", "title": "جسر QEMU / UTM", "target": "المنصة الهدف", "convert": "تحويل", "open_utm": "استيراد ملف/حزمة", "result": "النتيجة", "warnings": "تحذيرات التوافق", "copy": "نسخ", "ai_modify": "تعديل بالذكاء الاصطناعي", "target_format": "الصيغة الهدف", "instruction": "التعليمات", "save_as": "حفظ النتيجة", "saved": "تم الحفظ: {path}", "copied": "تم نسخ النتيجة.", "empty": "أدخل أمر QEMU أو اختر حزمة .utm.", "error": "فشل التحويل: {error}", "no_warnings": "لا توجد تحذيرات."},
    "mn": {"qemu_bridge": "QEMU гүүр", "title": "QEMU / UTM гүүр", "target": "Зорилтот платформ", "convert": "Хөрвүүлэх", "open_utm": "Файл/багц импортлох", "result": "Үр дүн", "warnings": "Анхааруулга", "copy": "Хуулах", "ai_modify": "AI-аар засах", "target_format": "Зорилтот формат", "instruction": "Заавар", "save_as": "Үр дүн хадгалах", "saved": "Хадгалсан: {path}", "copied": "Үр дүн хуулагдлаа.", "empty": "QEMU команд оруулах эсвэл .utm багц сонгоно уу.", "error": "Хөрвүүлэлт амжилтгүй: {error}", "no_warnings": "Анхааруулга байхгүй."},
    "th": {"qemu_bridge": "สะพาน QEMU", "title": "สะพาน QEMU / UTM", "target": "แพลตฟอร์มปลายทาง", "convert": "แปลง", "open_utm": "นำเข้าไฟล์/แพ็กเกจ", "result": "ผลลัพธ์", "warnings": "คำเตือน", "copy": "คัดลอก", "ai_modify": "แก้ไขด้วย AI", "target_format": "รูปแบบปลายทาง", "instruction": "คำสั่ง", "save_as": "บันทึกผลลัพธ์", "saved": "บันทึกแล้ว: {path}", "copied": "คัดลอกผลลัพธ์แล้ว", "empty": "ป้อนคำสั่ง QEMU หรือเลือกแพ็กเกจ .utm", "error": "การแปลงล้มเหลว: {error}", "no_warnings": "ไม่มีคำเตือน"},
    "vi": {"qemu_bridge": "Cầu QEMU", "title": "Cầu QEMU / UTM", "target": "Nền tảng đích", "convert": "Chuyển đổi", "open_utm": "Nhập tệp/gói", "result": "Kết quả", "warnings": "Cảnh báo", "copy": "Sao chép", "ai_modify": "Sửa bằng AI", "target_format": "Định dạng đích", "instruction": "Chỉ dẫn", "save_as": "Lưu kết quả", "saved": "Đã lưu: {path}", "copied": "Đã sao chép kết quả.", "empty": "Nhập lệnh QEMU hoặc chọn gói .utm.", "error": "Chuyển đổi thất bại: {error}", "no_warnings": "Không có cảnh báo."},
    "id": {"qemu_bridge": "Jembatan QEMU", "title": "Jembatan QEMU / UTM", "target": "Platform target", "convert": "Konversi", "open_utm": "Impor file/paket", "result": "Hasil", "warnings": "Peringatan", "copy": "Salin", "ai_modify": "Ubah dengan AI", "target_format": "Format target", "instruction": "Instruksi", "save_as": "Simpan hasil", "saved": "Disimpan: {path}", "copied": "Hasil disalin.", "empty": "Masukkan perintah QEMU atau pilih paket .utm.", "error": "Konversi gagal: {error}", "no_warnings": "Tidak ada peringatan."},
    "ms": {"qemu_bridge": "Jambatan QEMU", "title": "Jambatan QEMU / UTM", "target": "Platform sasaran", "convert": "Tukar", "open_utm": "Import fail/pakej", "result": "Hasil", "warnings": "Amaran", "copy": "Salin", "ai_modify": "Ubah dengan AI", "target_format": "Format sasaran", "instruction": "Arahan", "save_as": "Simpan hasil", "saved": "Disimpan: {path}", "copied": "Hasil disalin.", "empty": "Masukkan arahan QEMU atau pilih pakej .utm.", "error": "Penukaran gagal: {error}", "no_warnings": "Tiada amaran."},
    "hi": {"qemu_bridge": "QEMU ब्रिज", "title": "QEMU / UTM ब्रिज", "target": "लक्ष्य प्लेटफ़ॉर्म", "convert": "बदलें", "open_utm": "फ़ाइल/पैकेज आयात करें", "result": "परिणाम", "warnings": "चेतावनियाँ", "copy": "कॉपी करें", "ai_modify": "AI से बदलें", "target_format": "लक्ष्य फ़ॉर्मैट", "instruction": "निर्देश", "save_as": "परिणाम सहेजें", "saved": "सहेजा गया: {path}", "copied": "परिणाम कॉपी हुआ.", "empty": "QEMU कमांड दर्ज करें या .utm पैकेज चुनें.", "error": "रूपांतरण विफल: {error}", "no_warnings": "कोई चेतावनी नहीं."},
})
for _code, _text in list(PLUGIN_TEXT.items()):
    _base = PLUGIN_TEXT["en_us"].copy()
    _base.update(_text)
    PLUGIN_TEXT[_code] = _base


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
        tk.Label(body, text=tr("guide"), bg=bg, fg=muted, font=(font_name, 10), wraplength=740, justify="left").pack(anchor="w", pady=(8, 4))
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
    try:
        data = json.loads(payload.command)
    except Exception:
        if getattr(payload, "config", None) is None:
            raise ValueError("UTM result must be valid JSON or a checked conversion payload.")
        data = build_utm_plist(payload.config, "other", [])
    path.mkdir(parents=True, exist_ok=True)
    with (path / "config.plist").open("wb") as handle:
        plistlib.dump(data, handle, sort_keys=False)
