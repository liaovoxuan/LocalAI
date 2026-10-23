#include "AppLanguage.hpp"

#include <QComboBox>
#include <QDialog>
#include <QDialogButtonBox>
#include <QFormLayout>
#include <QLabel>
#include <QPushButton>
#include <QSettings>
#include <QVBoxLayout>

namespace vw {

namespace {

QString normalizedLanguage(const QString& code) {
    const QString value = code.trimmed().toLower();
    return supportedLanguages().contains(value) ? value : QStringLiteral("zh_cn");
}

bool languageStartsWith(const QString& prefix) {
    return currentLanguage().startsWith(prefix);
}

} // namespace

QString currentLanguage() {
    QSettings settings;
    return normalizedLanguage(settings.value("ui/language", "zh_cn").toString());
}

bool hasSelectedLanguage() {
    QSettings settings;
    return settings.contains("ui/language");
}

QStringList supportedLanguages() {
    return {"zh_cn", "zh_tw", "en_us", "en_gb", "ja", "fr", "de"};
}

QString languageDisplayName(const QString& code) {
    const QString value = normalizedLanguage(code);
    if (value == "zh_tw") return "繁體中文";
    if (value.startsWith("en")) return value == "en_gb" ? "English (UK)" : "English (US)";
    if (value == "ja") return "日本語";
    if (value == "fr") return "Français";
    if (value == "de") return "Deutsch";
    return "简体中文";
}

void setCurrentLanguage(const QString& code) {
    QSettings settings;
    settings.setValue("ui/language", normalizedLanguage(code));
}

QString trText(const QString& key) {
    const bool traditional = currentLanguage() == "zh_tw";
    const bool english = languageStartsWith("en");
    const bool japanese = languageStartsWith("ja");
    const bool french = languageStartsWith("fr");
    const bool german = languageStartsWith("de");

    if (key == "language") return english ? "Language" : japanese ? "言語" : french ? "Langue" : german ? "Sprache" : traditional ? "語言" : "语言";
    if (key == "language_title") return english ? "VirtualWorld Language" : japanese ? "VirtualWorld の言語" : french ? "Langue de VirtualWorld" : german ? "VirtualWorld-Sprache" : traditional ? "VirtualWorld 語言" : "VirtualWorld 语言";
    if (key == "language_prompt") return english ? "Choose the language used by recovery diagnostics and guidance." : japanese ? "復旧診断とガイドに使用する言語を選択してください。" : french ? "Choisissez la langue des diagnostics et de la récupération." : german ? "Wählen Sie die Sprache für Diagnose und Wiederherstellung." : traditional ? "選擇復原診斷與指引使用的語言。" : "选择恢复诊断与指引使用的语言。";
    if (key == "save") return english ? "Save" : japanese ? "保存" : french ? "Enregistrer" : german ? "Speichern" : traditional ? "儲存" : "保存";
    if (key == "cancel") return english ? "Cancel" : japanese ? "キャンセル" : french ? "Annuler" : german ? "Abbrechen" : traditional ? "取消" : "取消";
    if (key == "restart_hint") return english ? "The setting is saved. Reopen the current window to refresh existing controls." : japanese ? "設定を保存しました。既存の操作項目を更新するには現在のウィンドウを開き直してください。" : french ? "Le réglage est enregistré. Rouvrez la fenêtre pour actualiser les contrôles existants." : german ? "Die Einstellung wurde gespeichert. Öffnen Sie das aktuelle Fenster erneut, um vorhandene Bedienelemente zu aktualisieren." : traditional ? "設定已儲存。請重新開啟目前視窗以更新既有控制項。" : "设置已保存。请重新打开当前窗口以更新已有控件。";
    if (key == "self_check_title") return english ? "Virtual Machine Recovery Self-Check" : japanese ? "仮想マシン復旧セルフチェック" : french ? "Auto-diagnostic de récupération de la machine virtuelle" : german ? "Wiederherstellungsprüfung der virtuellen Maschine" : traditional ? "虛擬機復原自我檢查" : "虚拟机恢复自检";
    if (key == "self_check_config") return english ? "A QEMU or virtual-machine configuration issue was found." : japanese ? "QEMU または仮想マシン設定の問題が見つかりました。" : french ? "Un problème de configuration QEMU ou de machine virtuelle a été détecté." : german ? "Ein QEMU- oder VM-Konfigurationsproblem wurde gefunden." : traditional ? "偵測到 QEMU 或虛擬機設定問題。" : "检测到 QEMU 或虚拟机配置问题。";
    if (key == "self_check_disk") return english ? "A virtual-disk integrity or access issue was found." : japanese ? "仮想ディスクの整合性またはアクセスの問題が見つかりました。" : french ? "Un problème d'accès ou d'intégrité du disque virtuel a été détecté." : german ? "Ein Integritäts- oder Zugriffsproblem des virtuellen Datenträgers wurde gefunden." : traditional ? "偵測到虛擬磁碟完整性或存取問題。" : "检测到虚拟硬盘完整性或访问问题。";
    if (key == "self_check_guest") return english ? "QEMU configuration and disk checks passed, but the guest still did not reach a displayable boot state." : japanese ? "QEMU 設定とディスク検査は通過しましたが、ゲスト OS は表示可能な起動状態に達しませんでした。" : french ? "La configuration QEMU et les disques ont passé les vérifications, mais le système invité n'a pas atteint un état de démarrage affichable." : german ? "QEMU-Konfiguration und Datenträgerprüfung waren erfolgreich, aber das Gastsystem erreichte keinen darstellbaren Startzustand." : traditional ? "QEMU 設定與磁碟檢查皆通過，但客體系統仍未進入可顯示的啟動狀態。" : "QEMU 配置和磁盘检查均已通过，但客户机系统仍未进入可显示的启动状态。";
    if (key == "self_check_guest_hint") return english ? "This may indicate guest-system damage. VirtualWorld cannot confirm malware from a host-side check." : japanese ? "これはゲスト OS の損傷を示す可能性があります。VirtualWorld はホスト側検査だけでマルウェアを断定できません。" : french ? "Cela peut indiquer un système invité endommagé. VirtualWorld ne peut pas confirmer un malware uniquement depuis l'hôte." : german ? "Dies kann auf einen beschädigten Gast hindeuten. VirtualWorld kann Malware nicht allein durch eine Host-Prüfung bestätigen." : traditional ? "這可能表示客體系統損壞。VirtualWorld 無法僅透過宿主端檢查確認惡意程式。" : "这可能表示客户机系统损坏。VirtualWorld 无法仅通过宿主端检查确认恶意软件。";
    if (key == "self_check_config_hint") return english ? "Correct the listed QEMU, architecture, firmware, or boot-media settings, then retry." : japanese ? "表示された QEMU、アーキテクチャ、ファームウェア、起動メディア設定を修正して再試行してください。" : french ? "Corrigez les réglages QEMU, d'architecture, de firmware ou de média de démarrage, puis réessayez." : german ? "Korrigieren Sie die aufgeführten QEMU-, Architektur-, Firmware- oder Startmedien-Einstellungen und versuchen Sie es erneut." : traditional ? "請修正列出的 QEMU、架構、韌體或啟動媒體設定後再試一次。" : "请修正列出的 QEMU、架构、固件或启动介质设置后再试一次。";
    if (key == "self_check_disk_hint") return english ? "Keep the VM offline. Restore the disk from a known-good backup or replace the damaged image before retrying." : japanese ? "VM はオフラインのままにしてください。既知の正常なバックアップからディスクを復元するか、破損したイメージを交換してください。" : french ? "Gardez la VM hors ligne. Restaurez le disque depuis une sauvegarde fiable ou remplacez l'image endommagée avant de réessayer." : german ? "Lassen Sie die VM offline. Stellen Sie den Datenträger aus einem bekannten Backup wieder her oder ersetzen Sie das beschädigte Image." : traditional ? "請讓虛擬機保持離線。從已知良好的備份還原磁碟，或更換受損映像檔後再試。" : "请让虚拟机保持离线。从已知良好的备份还原磁盘，或更换受损镜像后再试。";
    if (key == "offline_retry") return english ? "Retry Offline" : japanese ? "オフラインで再試行" : french ? "Réessayer hors ligne" : german ? "Offline erneut versuchen" : traditional ? "離線重試" : "离线重试";
    if (key == "choose_install") return english ? "Choose Recovery Installer" : japanese ? "復旧インストーラを選択" : french ? "Choisir l'installateur de récupération" : german ? "Wiederherstellungs-Installer auswählen" : traditional ? "選擇復原安裝映像" : "选择恢复安装镜像";
    if (key == "close") return english ? "Close" : japanese ? "閉じる" : french ? "Fermer" : german ? "Schließen" : traditional ? "關閉" : "关闭";
    if (key == "quarantine") return english ? "The next start is isolated: networking and shared folders are disabled." : japanese ? "次回の起動は隔離されます。ネットワークと共有フォルダーは無効です。" : french ? "Le prochain démarrage est isolé : réseau et dossiers partagés sont désactivés." : german ? "Der nächste Start erfolgt isoliert: Netzwerk und freigegebene Ordner sind deaktiviert." : traditional ? "下次啟動將進入隔離模式：網路與共享資料夾已停用。" : "下次启动将进入隔离模式：网络和共享文件夹已停用。";
    if (key == "restore_network_question") return english ? "The isolated boot reached a displayable state. Restore networking and shared folders for the next start?" : japanese ? "隔離起動で表示可能な状態に達しました。次回の起動でネットワークと共有フォルダーを復元しますか？" : french ? "Le démarrage isolé a atteint un état affichable. Restaurer le réseau et les dossiers partagés au prochain démarrage ?" : german ? "Der isolierte Start erreichte einen darstellbaren Zustand. Netzwerk und freigegebene Ordner beim nächsten Start wiederherstellen?" : traditional ? "隔離啟動已進入可顯示狀態。是否在下次啟動時還原網路與共享資料夾？" : "离线隔离启动已进入可显示状态。是否在下次启动时恢复网络和共享文件夹？";
    if (key == "recovery_disk") return english ? "Recovery guard disk" : japanese ? "復旧保護ディスク" : french ? "Disque de protection de récupération" : german ? "Wiederherstellungs-Schutzdatenträger" : traditional ? "復原保護磁碟" : "恢复保护盘";
    return key;
}

bool chooseApplicationLanguage(QWidget* parent, bool firstRun) {
    QDialog dialog(parent);
    dialog.setWindowTitle(trText("language_title"));
    auto* layout = new QVBoxLayout(&dialog);
    auto* form = new QFormLayout();
    auto* language = new QComboBox(&dialog);
    for (const QString& code : supportedLanguages()) {
        language->addItem(languageDisplayName(code), code);
    }
    const int currentIndex = language->findData(currentLanguage());
    language->setCurrentIndex(currentIndex >= 0 ? currentIndex : 0);
    form->addRow(trText("language"), language);
    layout->addWidget(new QLabel(firstRun ? trText("language_prompt") : trText("restart_hint"), &dialog));
    layout->addLayout(form);
    auto* buttons = new QDialogButtonBox(QDialogButtonBox::Save | QDialogButtonBox::Cancel, &dialog);
    buttons->button(QDialogButtonBox::Save)->setText(trText("save"));
    buttons->button(QDialogButtonBox::Cancel)->setText(trText("cancel"));
    QObject::connect(buttons, &QDialogButtonBox::accepted, &dialog, &QDialog::accept);
    QObject::connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    layout->addWidget(buttons);
    if (dialog.exec() != QDialog::Accepted) {
        return false;
    }
    setCurrentLanguage(language->currentData().toString());
    return true;
}

} // namespace vw
