#include "VirtualWorldWindow.hpp"

#include "AppLanguage.hpp"
#include "ConfigManager.hpp"
#include "QemuParser.hpp"
#include "RecoveryDiagnostics.hpp"
#include "RecoveryGuard.hpp"
#include "SystemSoftwareEngine.hpp"
#include "SystemSoftwareRomGenerator.hpp"
#include "Translator.hpp"
#include "UtmParser.hpp"
#include "VirtualMachineWindow.hpp"

#include <QApplication>
#include <QCheckBox>
#include <QComboBox>
#include <QDesktopServices>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDir>
#include <QDirIterator>
#include <QFile>
#include <QFileDialog>
#include <QFileInfo>
#include <QFormLayout>
#include <QFrame>
#include <QHBoxLayout>
#include <QInputDialog>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QMenu>
#include <QMessageBox>
#include <QPlainTextEdit>
#include <QProgressDialog>
#include <QPushButton>
#include <QRegularExpression>
#include <QScreen>
#include <QScrollArea>
#include <QStandardPaths>
#include <QStorageInfo>
#include <QSplitter>
#include <QTextEdit>
#include <QTimer>
#include <QUuid>
#include <QtGlobal>
#include <QUrl>
#include <QVBoxLayout>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>

#include <algorithm>

namespace vw {

enum MediaItemRole {
    MediaTypeRole = Qt::UserRole + 1,
    MediaPathRole,
    MediaInterfaceRole,
    MediaFormatRole,
    MediaReadonlyRole,
    MediaSizeRole,
    MediaRoleRole
};

static QString buttonStyle() {
    return "QPushButton{padding:9px 14px;border-radius:8px;background:#eef3fb;color:#162033;border:1px solid #d8e1ee;}"
           "QPushButton:hover{background:#e4edf9;}"
           "QPushButton#primary{background:#2f6df6;color:white;border:1px solid #2f6df6;}";
}

static qint64 toMb(double value, const QString& unit) {
    const QString normalized = unit.trimmed().toUpper();
    if (normalized == "KB") {
        return qMax<qint64>(1, qint64(value / 1024.0));
    }
    if (normalized == "GB") {
        return qMax<qint64>(1, qint64(value * 1024.0));
    }
    return qMax<qint64>(1, qint64(value));
}

static qint64 toKb(double value, const QString& unit) {
    const QString normalized = unit.trimmed().toUpper();
    if (normalized == "GB") {
        return qMax<qint64>(1, qRound64(value * 1024.0 * 1024.0));
    }
    if (normalized == "MB") {
        return qMax<qint64>(1, qRound64(value * 1024.0));
    }
    return qMax<qint64>(1, qRound64(value));
}

static QString suggestedDownloadDir() {
    QString dir = QStandardPaths::writableLocation(QStandardPaths::DownloadLocation);
    if (dir.isEmpty()) {
        dir = QDir::homePath();
    }
    return dir;
}

static QString bootImageFilter() {
    return "Boot images (*.iso *.cdr *.dmg *.ipsw *.img *.ima *.flp *.dsk *.hfv *.toast *.raw *.qcow2);;All files (*)";
}

static bool guestNeedsInstallImageAtCreate(const VmConfig& config) {
    const QString guestOs = config.guestOs.toLower();
    return guestOs.contains("windows")
        || guestOs.contains("macos")
        || guestOs.contains("linux")
        || guestOs.contains("bsd")
        || guestOs.contains("dos")
        || guestOs.contains("other");
}

static bool isLegacyPcUi(const QString& os);
static bool isClassicMacOsUi(const QString& os);

static qint64 defaultDiskSizeMbForConfig(const VmConfig& config) {
    if (isLegacyPcUi(config.guestOs)) {
        return 64;
    }
    if (isClassicMacOsUi(config.guestOs)) {
        return 2048;
    }
    return 32768;
}

static bool isFloppyImagePath(const QString& path, const QString& guestOs, const QString& interfaceHint) {
    const QString suffix = QFileInfo(path).suffix().toLower();
    if (interfaceHint.trimmed().compare("floppy", Qt::CaseInsensitive) == 0) {
        return true;
    }
    if (suffix == "dsk" || suffix == "ima" || suffix == "flp") {
        return true;
    }
    // Old Windows distributions commonly ship boot floppies as .img.
    return suffix == "img" && isLegacyPcUi(guestOs);
}

static void prependInstallImage(VmConfig& config, const QString& path, const QString& interfaceHint) {
    const QString trimmed = path.trimmed();
    if (trimmed.isEmpty()) {
        return;
    }
    const QString normalizedPath = QFileInfo(trimmed).absoluteFilePath();
    config.cdroms.clear();
    for (int i = config.disks.size() - 1; i >= 0; --i) {
        if (QFileInfo(config.disks.at(i).path).absoluteFilePath() == normalizedPath) {
            config.disks.removeAt(i);
        }
    }
    const QString suffix = QFileInfo(trimmed).suffix().toLower();
    if (isFloppyImagePath(trimmed, config.guestOs, interfaceHint)) {
        config.disks.prepend(DiskConfig{trimmed, "floppy", suffix, false, "floppy"});
        return;
    }
    if (suffix == "qcow2" || suffix == "img" || suffix == "raw" || suffix == "hfv") {
        config.disks.prepend(DiskConfig{trimmed, interfaceHint.isEmpty() ? "ide" : interfaceHint, suffix, false, "disk"});
        return;
    }
    config.disks.prepend(DiskConfig{trimmed, interfaceHint == "cdrom" ? "ide" : interfaceHint, suffix, true, "cdrom"});
}

static void normalizeBootMediaOrder(VmConfig& config) {
    QVector<DiskConfig> legacyCdroms;
    for (const QString& cdrom : config.cdroms) {
        const QString path = cdrom.trimmed();
        if (path.isEmpty()) {
            continue;
        }
        const QString normalizedPath = QFileInfo(path).absoluteFilePath();
        bool alreadyPresent = false;
        for (const DiskConfig& disk : config.disks) {
            if (disk.media == "cdrom"
                && QFileInfo(disk.path).absoluteFilePath() == normalizedPath) {
                alreadyPresent = true;
                break;
            }
        }
        if (alreadyPresent) {
            continue;
        }
        DiskConfig disk;
        disk.path = path;
        disk.media = "cdrom";
        disk.interfaceName = "ide";
        disk.format = QFileInfo(path).suffix().toLower();
        disk.readonly = true;
        legacyCdroms.append(disk);
    }
    config.cdroms.clear();
    if (!legacyCdroms.isEmpty()) {
        legacyCdroms += config.disks;
        config.disks = legacyCdroms;
    }
}

static bool hasBootMedia(const VmConfig& config) {
    for (const DiskConfig& disk : config.disks) {
        if ((disk.media == "cdrom" || disk.media == "floppy")
            && !disk.path.trimmed().isEmpty()) {
            return true;
        }
    }
    for (const QString& path : config.cdroms) {
        if (!path.trimmed().isEmpty()) {
            return true;
        }
    }
    return false;
}

static bool hasWritableSystemDisk(const VmConfig& config) {
    for (const DiskConfig& disk : config.disks) {
        if (disk.media == "disk"
            && !disk.path.trimmed().isEmpty()
            && !disk.readonly) {
            return true;
        }
    }
    return false;
}

static QString cleanVmName(QString name) {
    name = name.trimmed();
    if (name.isEmpty()) {
        name = "VirtualWorld VM";
    }
    static const QRegularExpression unsafe("[\\\\/:*?\"<>|]");
    name.replace(unsafe, "_");
    return name;
}

static QStringList availableArchitectures() {
    return {
        "x86_64",
        "i386",
        "aarch64",
        "arm",
        "ppc",
        "ppc64",
        "m68k",
        "riscv64",
        "loongarch64",
    };
}

static QString supportedArchitectureOrFallback(const QString& preferred) {
    const QString requested = normalizeArchitecture(preferred);
    return requested.isEmpty() ? QStringLiteral("x86_64") : requested;
}

static QString displayNameForGuest(const QString& os) {
    const QString key = os.trimmed().toLower();
    if (key.contains("classic") && key.contains("mac")) return "Classic MacOS";
    if (key.contains("macos")) return "macOS";
    if (key.contains("old windows")) return "Old Windows";
    if (key.contains("windows")) return "Windows";
    if (key.contains("linux")) return "Linux";
    if (key.contains("bsd")) return "BSD";
    if (key.contains("dos")) return "DOS";
    if (key.contains("system software")) return "System Software";
    if (key.contains("other")) return "Other";
    return "VirtualWorld VM";
}

static bool isOldWindowsUi(const QString& os) {
    const QString key = os.trimmed().toLower();
    return key.contains("old windows")
        || key.contains("windows 3")
        || key.contains("windows 95")
        || key.contains("windows 98")
        || key.contains("windows me");
}

static bool isClassicMacOsUi(const QString& os) {
    const QString key = os.trimmed().toLower().replace(" ", "");
    return key.contains("classicmac") || key.contains("macosclassic") || key.contains("classicmacos");
}

static bool isSystemSoftwareUi(const QString& os) {
    const QString key = os.trimmed().toLower();
    return key.contains("system software") || key == "macintosh 128k" || key == "macintosh 512k"
        || key == "mac plus" || key == "twiggy";
}

static QString systemSoftwareVersionFromUi(const QString& os) {
    const QRegularExpression match("system\\s*software\\s*([0-6])", QRegularExpression::CaseInsensitiveOption);
    const auto result = match.match(os);
    if (result.hasMatch()) return result.captured(1);
    const QString key = os.trimmed().toLower();
    if (key == "twiggy") return QStringLiteral("0");
    if (key == "macintosh 128k") return QStringLiteral("1");
    if (key == "macintosh 512k") return QStringLiteral("2");
    if (key == "mac plus") return QStringLiteral("3");
    return QStringLiteral("6");
}

static QString systemSoftwareMachineFromUi(const QString& os) {
    const QString key = os.trimmed().toLower();
    if (key == "macintosh 128k") return "Macintosh 128K";
    if (key == "macintosh 512k") return "Macintosh 512K";
    if (key == "mac plus") return "Mac Plus";
    if (key == "twiggy") return "Twiggy";
    return {};
}

static QString systemSoftwareRomTypeFromUi(const QString& os) {
    const QString key = os.trimmed().toLower();
    if (key == "macintosh 128k" || key == "macintosh 512k") return "mac128k";
    if (key == "mac plus") return "macplus";
    if (key == "twiggy") return "twiggy";
    return {};
}

static int systemSoftwarePresetMemoryKb(const QString& os) {
    const QString key = os.trimmed().toLower();
    if (key == "macintosh 128k") return 128;
    if (key == "macintosh 512k") return 512;
    if (key == "mac plus") return 1024;
    if (key == "twiggy") return 512;
    const QString version = systemSoftwareVersionFromUi(os);
    if (version == "0" || version == "1") return 128;
    if (version == "2" || version == "3") return 512;
    return 1024;
}

static bool isModernMacOsUi(const QString& os) {
    const QString key = os.trimmed().toLower();
    return key.contains("macos") && !isClassicMacOsUi(os);
}

static bool canUseNativeMacIpswFlow(const QString& guestArch) {
    const HostProfile host = detectHostProfile();
    return host.platform == HostPlatform::MacOS
        && normalizeArchitecture(host.arch) == "aarch64"
        && normalizeArchitecture(guestArch) == "aarch64";
}

static QString macOsInstallMethodText(const QString& guestArch) {
    const HostProfile host = detectHostProfile();
    const QString arch = normalizeArchitecture(guestArch);
    if (canUseNativeMacIpswFlow(arch)) {
        return "当前宿主机支持 Apple Silicon macOS IPSW 启动介质。VirtualWorld 会获取候选项，并只接受 Apple 官方 HTTPS 直链；当前 QEMU 后端将其作为恢复启动介质使用，不等同于 Apple 实体机的原生恢复流程。";
    }
    if (host.platform == HostPlatform::MacOS) {
        return "已保留现代 macOS 虚拟机创建入口。当前客户机架构与本机架构不属于 Apple Silicon macOS 原生 IPSW 虚拟化路径，"
               "例如 Intel Mac 模拟 Apple Silicon、Apple Silicon 模拟 Intel Mac 通常只能作为 QEMU/TCG 实验配置，启动和性能不保证。"
               "请选择目标架构并手动导入合法镜像或恢复介质；手动导入的 IPSW 会标记为未验证，不能用于 macOS 恢复自检。"
               "PPC 及更早版本请改选 Classic MacOS。";
    }
    return "macOS Support Disk 和 IPSW 获取仅开放给 macOS 宿主机。"
           "现代 macOS 通常依赖 Apple 硬件与 macOS 的原生虚拟化/IPSW 路径；当前系统不会自动下载 IPSW。"
           "请手动选择目标架构并导入合法镜像或恢复介质，兼容性与性能不保证；PPC 及更早版本请改选 Classic MacOS。";
}

static bool isLegacyPcUi(const QString& os) {
    return os.trimmed().toLower().contains("dos") || isOldWindowsUi(os);
}

static QString inputPresetFromConfig(const VmConfig& config) {
    const QStringList devices = config.usb.devices;
    for (const QString& device : devices) {
        if (device.trimmed().section(',', 0, 0) == "usb-tablet") {
            return "平板指针 + 键盘";
        }
    }
    if (devices.contains("usb-kbd") || devices.contains("usb-mouse")) {
        return "USB 键盘鼠标";
    }
    if (isClassicMacOsUi(config.guestOs)) {
        return "ADB 键盘鼠标";
    }
    if (isLegacyPcUi(config.guestOs)) {
        return "PS/2 键盘鼠标";
    }
    return "平板指针 + 键盘";
}

static void applyInputPreset(VmConfig& config, const QString& preset) {
    config.usb.controller.clear();
    config.usb.devices.clear();
    if (preset.contains("USB", Qt::CaseInsensitive)) {
        config.usb.controller = "usb";
        config.usb.devices << "usb-kbd" << "usb-mouse";
    } else if (preset.contains("平板")) {
        config.usb.controller = "usb";
        config.usb.devices << "usb-kbd" << "usb-tablet";
    }
}

static QString gpuDisplayText(const HostGpuInfo& gpu) {
    QString text = gpu.name;
    if (!gpu.pciAddress.isEmpty()) {
        text += " · " + gpu.pciAddress;
    }
    if (gpu.linkSpeedGbps > 0) {
        text += QString(" · %1Gbps").arg(gpu.linkSpeedGbps);
    }
    text += gpu.passthroughCandidate ? " · 可直通" : " · 不可直通";
    if (!gpu.reason.isEmpty()) {
        text += " · " + gpu.reason;
    }
    return text;
}

static HostGpuInfo gpuById(const QString& id) {
    for (const HostGpuInfo& gpu : detectHostGpus()) {
        if (gpu.id == id) {
            return gpu;
        }
    }
    return {};
}

static void applyGpuSelection(VmConfig& config, const HostGpuInfo& gpu, bool enabled, bool keepVirtualDisplay) {
    config.gpuPassthrough.enabled = enabled && gpu.passthroughCandidate;
    config.gpuPassthrough.askOnStart = !config.gpuPassthrough.enabled;
    config.gpuPassthrough.keepVirtualDisplay = keepVirtualDisplay;
    config.gpuPassthrough.deviceId = gpu.id;
    config.gpuPassthrough.name = gpu.name;
    config.gpuPassthrough.pciAddress = gpu.pciAddress;
    config.gpuPassthrough.backend = "vfio";
}

VirtualWorldWindow::VirtualWorldWindow(QWidget* parent) : QWidget(parent) {
    buildUi();
    loadSavedLibrary();
}

void VirtualWorldWindow::buildUi() {
    setWindowTitle("VirtualWorld");
    resize(1180, 760);
    if (QScreen* screen = QApplication::primaryScreen()) {
        const QRect available = screen->availableGeometry();
        resize(qMin(width(), int(available.width() * 0.82)), qMin(height(), int(available.height() * 0.82)));
    }
    setStyleSheet(QString("QWidget{background:#f7f9fc;color:#1f2937;font-size:14px;}"
                          "QListWidget,QLineEdit,QComboBox,QDoubleSpinBox,QPlainTextEdit,QTextEdit{background:white;border:1px solid #d7deea;border-radius:8px;padding:9px;min-height:28px;}"
                          "QLabel{background:transparent;}")
                  + buttonStyle());

    auto* root = new QVBoxLayout(this);
    root->setContentsMargins(14, 14, 14, 14);
    root->setSpacing(12);

    auto* toolbar = new QHBoxLayout();
    auto addToolButton = [&](const QString& text, auto slot, bool primary = false) {
        auto* button = new QPushButton(text);
        if (primary) button->setObjectName("primary");
        connect(button, &QPushButton::clicked, this, slot);
        toolbar->addWidget(button);
        return button;
    };
    addToolButton("新建", [this] { newVm(); });
    addToolButton("导入 QEMU", [this] { importQemu(); });
    addToolButton("导入 UTM", [this] { importUtm(); });
    addToolButton("导入 VirtualWorld", [this] { importVirtualWorld(); });
    addToolButton("保存配置", [this] { saveVirtualWorld(); });
    addToolButton("删除", [this] { deleteVm(); });
    addToolButton("导出 QEMU", [this] { exportQemu(); });
    addToolButton("导出 UTM", [this] { exportUtm(); });
    addToolButton(trText("language"), [this] {
        if (chooseApplicationLanguage(this, false)) {
            showMessage(trText("language_title"), trText("restart_hint"));
        }
    });
    toolbar->addStretch(1);
    addToolButton("停止", [this] { stopVm(); });
    addToolButton("启动", [this] { startVm(); }, true);
    root->addLayout(toolbar);

    auto* splitter = new QSplitter();
    splitter->setChildrenCollapsible(false);
    root->addWidget(splitter, 1);

    auto* listPane = new QWidget();
    auto* listLayout = new QVBoxLayout(listPane);
    listLayout->setContentsMargins(0, 0, 0, 0);
    listLayout->setSpacing(8);
    vmList_ = new QListWidget();
    vmList_->setMinimumWidth(220);
    auto* deleteSelectedButton = new QPushButton("删除所选虚拟机");
    deleteSelectedButton->setToolTip("删除当前选中的虚拟机，可选择仅移出列表或同时删除虚拟机文件夹。");
    connect(deleteSelectedButton, &QPushButton::clicked, this, [this] { deleteVm(); });
    listLayout->addWidget(vmList_, 1);
    listLayout->addWidget(deleteSelectedButton);
    connect(vmList_, &QListWidget::currentRowChanged, this, [this](int row) {
        if (row < 0 || row >= vms_.size()) return;
        currentIndex_ = row;
        loadVmToEditor(vms_[row]);
    });
    vmList_->setContextMenuPolicy(Qt::CustomContextMenu);
    connect(vmList_, &QListWidget::customContextMenuRequested, this, [this](const QPoint& pos) {
        const QModelIndex index = vmList_->indexAt(pos);
        if (index.isValid()) {
            vmList_->setCurrentRow(index.row());
        }
        QMenu menu(this);
        QAction* deleteAction = menu.addAction("删除虚拟机...");
        connect(deleteAction, &QAction::triggered, this, [this] { deleteVm(); });
        menu.exec(vmList_->viewport()->mapToGlobal(pos));
    });
    splitter->addWidget(listPane);

    auto* detail = new QWidget();
    auto* detailLayout = new QVBoxLayout(detail);
    detailLayout->setContentsMargins(12, 0, 0, 0);
    splitter->addWidget(detail);
    splitter->setStretchFactor(1, 1);

    emptyStateLabel_ = new QLabel("还没有虚拟机\n\n点击“新建”创建虚拟机，或导入 QEMU / UTM / VirtualWorld 配置。");
    emptyStateLabel_->setAlignment(Qt::AlignCenter);
    emptyStateLabel_->setMinimumHeight(300);
    emptyStateLabel_->setStyleSheet("QLabel{font-size:18px;color:#667085;background:white;border:1px solid #d7deea;border-radius:12px;padding:28px;}");
    detailLayout->addWidget(emptyStateLabel_, 1);

    editorPane_ = new QWidget();
    auto* editorOuterLayout = new QVBoxLayout(editorPane_);
    editorOuterLayout->setContentsMargins(0, 0, 0, 0);
    editorOuterLayout->setSpacing(0);
    detailLayout->addWidget(editorPane_, 1);

    auto* editorScroll = new QScrollArea();
    editorScroll->setWidgetResizable(true);
    editorScroll->setFrameShape(QFrame::NoFrame);
    editorScroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    editorScroll->setVerticalScrollBarPolicy(Qt::ScrollBarAsNeeded);
    editorScroll->setStyleSheet("QScrollArea{background:transparent;border:0;}");
    auto* editorContent = new QWidget();
    auto* editorLayout = new QVBoxLayout(editorContent);
    editorLayout->setContentsMargins(0, 0, 10, 18);
    editorLayout->setSpacing(12);
    editorScroll->setWidget(editorContent);
    editorOuterLayout->addWidget(editorScroll, 1);

    auto* form = new QFormLayout();
    form->setHorizontalSpacing(18);
    form->setVerticalSpacing(12);
    form->setFieldGrowthPolicy(QFormLayout::ExpandingFieldsGrow);
    form->setLabelAlignment(Qt::AlignRight | Qt::AlignVCenter);
    nameEdit_ = new QLineEdit();
    storageEdit_ = new QLineEdit();
    storageEdit_->setPlaceholderText("未设置时使用系统默认虚拟机文件夹");
    auto* chooseStorageButton = new QPushButton("选择...");
    auto* storageRow = new QWidget();
    auto* storageLayout = new QHBoxLayout(storageRow);
    storageLayout->setContentsMargins(0, 0, 0, 0);
    storageLayout->setSpacing(8);
    storageLayout->addWidget(storageEdit_, 1);
    storageLayout->addWidget(chooseStorageButton);
    guestOsCombo_ = new QComboBox();
    guestOsCombo_->addItems({"generic", "macOS", "Classic MacOS", "Macintosh 128K", "Macintosh 512K", "Mac Plus", "Twiggy", "System Software 0", "System Software 1", "System Software 2", "System Software 3", "System Software 4", "System Software 5", "System Software 6", "Windows", "Old Windows", "Linux", "BSD", "DOS", "Other"});
    guestOsCombo_->setEditable(true);
    archCombo_ = new QComboBox();
    archCombo_->addItems(availableArchitectures());
    archCombo_->setEditable(true);
    machineCombo_ = new QComboBox();
    machineCombo_->addItems({"q35", "pc", "virt", "mac99", "pseries"});
    machineCombo_->setEditable(true);
    cpuEdit_ = new QLineEdit("max");
    coresSpin_ = new QDoubleSpinBox();
    coresSpin_->setDecimals(0);
    coresSpin_->setRange(1, 128);
    memorySpin_ = new QDoubleSpinBox();
    memorySpin_->setDecimals(2);
    memorySpin_->setRange(1, 1048576);
    memorySpin_->setValue(4);
    memoryUnitCombo_ = new QComboBox();
    memoryUnitCombo_->addItems({"GB", "MB", "KB"});
    auto* memoryRow = new QHBoxLayout();
    memoryRow->addWidget(memorySpin_, 1);
    memoryRow->addWidget(memoryUnitCombo_);
    auto* memoryWidget = new QWidget();
    memoryWidget->setLayout(memoryRow);
    acceleratorCombo_ = new QComboBox();
    acceleratorCombo_->addItems({"auto", "hvf", "whpx", "kvm", "tcg"});
    acceleratorCombo_->setEditable(true);
    networkEnabledCheck_ = new QCheckBox("启用虚拟网络");
    networkEnabledCheck_->setChecked(true);
    networkModeCombo_ = new QComboBox();
    networkModeCombo_->addItems({"user", "bridge", "tap", "socket", "none"});
    networkModeCombo_->setEditable(true);
    networkModelCombo_ = new QComboBox();
    networkModelCombo_->addItems({"virtio-net-pci", "e1000", "e1000e", "rtl8139", "vmxnet3", "ne2k_pci", "sungem", "dp83932"});
    networkModelCombo_->setEditable(true);
    networkBridgeEdit_ = new QLineEdit();
    networkBridgeEdit_->setPlaceholderText("bridge/tap 名称（可选）");
    networkMacEdit_ = new QLineEdit();
    networkMacEdit_->setPlaceholderText("例如 52:54:00:12:34:56（可选）");
    networkForwardsEdit_ = new QLineEdit();
    networkForwardsEdit_->setPlaceholderText("hostfwd=tcp::2222-:22，多项用逗号分隔");
    audioEnabledCheck_ = new QCheckBox("启用虚拟声卡");
    audioDriverCombo_ = new QComboBox();
    audioDriverCombo_->addItems({"auto", "coreaudio", "pa", "pipewire", "sdl", "none"});
    audioDriverCombo_->setEditable(true);
    audioModelCombo_ = new QComboBox();
    audioModelCombo_->addItems({"ich9-intel-hda", "AC97", "ES1370", "sb16", "gus"});
    audioModelCombo_->setEditable(true);
    graphicsCombo_ = new QComboBox();
    graphicsCombo_->addItems({"virtio-vga", "virtio-vga-gl", "virtio-gpu-pci", "virtio-gpu-gl-pci", "qxl-vga", "VGA", "cirrus", "std", "ramfb"});
    graphicsCombo_->setEditable(true);
    inputCombo_ = new QComboBox();
    inputCombo_->addItems({"平板指针 + 键盘", "USB 键盘鼠标", "PS/2 键盘鼠标", "ADB 键盘鼠标"});
    gpuPassthroughCheck_ = new QCheckBox("启动时使用 GPU 直通");
    gpuPassthroughCombo_ = new QComboBox();
    gpuPassthroughCombo_->setMinimumContentsLength(18);
    gpuPassthroughCombo_->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
    gpuPassthroughCombo_->setMaximumWidth(520);
    keepVirtualDisplayCheck_ = new QCheckBox("保留虚拟显卡用于安装和救援显示");
    keepVirtualDisplayCheck_->setChecked(true);
    refreshGpuPassthroughChoices();
    openGlCheck_ = new QCheckBox("OpenGL 图形加速");
    autoResizeCheck_ = new QCheckBox("自适应窗口与分辨率");
    retinaCheck_ = new QCheckBox("Retina / HiDPI");
    tpmCheck_ = new QCheckBox("TPM 2.0");
    tpmSocketEdit_ = new QLineEdit();
    tpmSocketEdit_->setPlaceholderText("swtpm socket，例如 /tmp/vw-tpm.sock");
    kernelEdit_ = new QLineEdit();
    kernelEdit_->setPlaceholderText("Linux kernel / vmlinuz");
    initrdEdit_ = new QLineEdit();
    initrdEdit_->setPlaceholderText("Linux initrd");
    appendEdit_ = new QLineEdit();
    appendEdit_->setPlaceholderText("root=/dev/vda1 console=ttyS0");
    systemSoftwareRomEdit_ = new QLineEdit();
    systemSoftwareRomEdit_->setPlaceholderText("System Software ROM（Mini vMac 兼容 ROM）");
    auto* systemSoftwareRomBrowse = new QPushButton("选择...");
    auto* systemSoftwareRomRow = new QWidget();
    auto* systemSoftwareRomLayout = new QHBoxLayout(systemSoftwareRomRow);
    systemSoftwareRomLayout->setContentsMargins(0, 0, 0, 0);
    systemSoftwareRomLayout->setSpacing(8);
    systemSoftwareRomLayout->addWidget(systemSoftwareRomEdit_, 1);
    systemSoftwareRomLayout->addWidget(systemSoftwareRomBrowse);

    form->addRow("名称", nameEdit_);
    form->addRow("保存位置", storageRow);
    form->addRow("系统", guestOsCombo_);
    form->addRow("架构", archCombo_);
    form->addRow("机型", machineCombo_);
    form->addRow("CPU", cpuEdit_);
    form->addRow("核心", coresSpin_);
    form->addRow("内存", memoryWidget);
    form->addRow("硬件加速", acceleratorCombo_);
    form->addRow("网络", networkEnabledCheck_);
    form->addRow("网络模式", networkModeCombo_);
    form->addRow("网卡型号", networkModelCombo_);
    form->addRow("网桥 / TAP", networkBridgeEdit_);
    form->addRow("MAC 地址", networkMacEdit_);
    form->addRow("端口转发", networkForwardsEdit_);
    form->addRow("声卡", audioEnabledCheck_);
    form->addRow("音频后端", audioDriverCombo_);
    form->addRow("声卡型号", audioModelCombo_);
    form->addRow("显卡", graphicsCombo_);
    form->addRow("输入方式", inputCombo_);
    form->addRow("", gpuPassthroughCheck_);
    form->addRow("直通 GPU", gpuPassthroughCombo_);
    form->addRow("", keepVirtualDisplayCheck_);
    form->addRow("", openGlCheck_);
    form->addRow("", autoResizeCheck_);
    form->addRow("", retinaCheck_);
    form->addRow("", tpmCheck_);
    form->addRow("TPM Socket", tpmSocketEdit_);
    form->addRow("Linux Kernel", kernelEdit_);
    form->addRow("Linux Initrd", initrdEdit_);
    form->addRow("Kernel 参数", appendEdit_);
    form->addRow("System Software ROM", systemSoftwareRomRow);
    editorLayout->addLayout(form);

    auto bindRefresh = [this] { refreshPreview(); };
    connect(nameEdit_, &QLineEdit::textChanged, this, bindRefresh);
    connect(storageEdit_, &QLineEdit::textChanged, this, bindRefresh);
    connect(chooseStorageButton, &QPushButton::clicked, this, [this] {
        const QString initial = storageEdit_->text().trimmed().isEmpty() ? defaultStorageRoot() : storageEdit_->text().trimmed();
        const QString path = QFileDialog::getExistingDirectory(this, "选择虚拟机保存文件夹", initial);
        if (!path.isEmpty()) {
            storageEdit_->setText(path);
        }
    });
    connect(systemSoftwareRomBrowse, &QPushButton::clicked, this, [this] {
        const QString initial = systemSoftwareRomEdit_->text().trimmed().isEmpty()
                                    ? QStandardPaths::writableLocation(QStandardPaths::HomeLocation)
                                    : QFileInfo(systemSoftwareRomEdit_->text()).absolutePath();
        const QString path = QFileDialog::getOpenFileName(this, "选择 System Software ROM", initial, "ROM / 镜像 (*)");
        if (!path.isEmpty()) systemSoftwareRomEdit_->setText(path);
    });
    connect(guestOsCombo_, &QComboBox::currentTextChanged, this, [this, bindRefresh](const QString& os) {
        const QString key = os.toLower();
        const bool systemSoftware = isSystemSoftwareUi(os);
        const auto setModernDeviceControlsEnabled = [this](bool enabled) {
            networkEnabledCheck_->setEnabled(enabled);
            networkModeCombo_->setEnabled(enabled);
            networkModelCombo_->setEnabled(enabled);
            networkBridgeEdit_->setEnabled(enabled);
            networkMacEdit_->setEnabled(enabled);
            networkForwardsEdit_->setEnabled(enabled);
            audioEnabledCheck_->setEnabled(enabled);
            audioDriverCombo_->setEnabled(enabled);
            audioModelCombo_->setEnabled(enabled);
        };
        setModernDeviceControlsEnabled(!systemSoftware);
        if (qemuMediaControls_) qemuMediaControls_->setVisible(!systemSoftware);
        if (systemSoftwareFloppyWidget_) systemSoftwareFloppyWidget_->setVisible(systemSoftware);
        if (mediaList_) mediaList_->setVisible(!systemSoftware);
        if (loadingEditor_) {
            bindRefresh();
            return;
        }
        nameEdit_->setText(displayNameForGuest(os));
        if (systemSoftware) {
            tpmCheck_->setChecked(false);
            archCombo_->setCurrentText("m68k");
            machineCombo_->setCurrentText("q800");
            cpuEdit_->setText("m68000");
            coresSpin_->setValue(1);
            acceleratorCombo_->setCurrentText("tcg");
            graphicsCombo_->setCurrentText("std");
            inputCombo_->setCurrentText("ADB 键盘鼠标");
            networkEnabledCheck_->setChecked(false);
            audioEnabledCheck_->setChecked(false);
            openGlCheck_->setChecked(false);
            autoResizeCheck_->setChecked(false);
            retinaCheck_->setChecked(false);
            memoryUnitCombo_->setCurrentText("KB");
            memorySpin_->setValue(systemSoftwarePresetMemoryKb(os));
        } else if (isClassicMacOsUi(os)) {
            tpmCheck_->setChecked(false);
            const QString arch = supportedArchitectureOrFallback("ppc");
            archCombo_->setCurrentText(arch);
            machineCombo_->setCurrentText(arch == "ppc" ? "mac99" : defaultMachineForArch(arch));
            cpuEdit_->setText(arch == "ppc" || arch == "ppc64" ? "G3" : "max");
            acceleratorCombo_->setCurrentText("tcg");
            graphicsCombo_->setCurrentText("std");
            inputCombo_->setCurrentText("ADB 键盘鼠标");
            openGlCheck_->setChecked(false);
            memoryUnitCombo_->setCurrentText("MB");
            memorySpin_->setValue(128);
            diskInterfaceCombo_->setCurrentText("ide");
        } else if (isLegacyPcUi(os)) {
            tpmCheck_->setChecked(false);
            archCombo_->setCurrentText(supportedArchitectureOrFallback("i386"));
            machineCombo_->setCurrentText("pc");
            cpuEdit_->setText(key.contains("dos") ? "486" : "pentium");
            acceleratorCombo_->setCurrentText("tcg");
            graphicsCombo_->setCurrentText("VGA");
            openGlCheck_->setChecked(false);
            if (key.contains("dos")) {
                memoryUnitCombo_->setCurrentText("MB");
                memorySpin_->setValue(16);
            } else {
                memoryUnitCombo_->setCurrentText("MB");
                memorySpin_->setValue(32);
            }
            diskInterfaceCombo_->setCurrentText("ide");
        } else if (key.contains("windows")) {
            const qint64 currentMemoryMb = memoryValueMb();
            tpmCheck_->setChecked(true);
            archCombo_->setCurrentText(supportedArchitectureOrFallback("x86_64"));
            machineCombo_->setCurrentText("pc");
            cpuEdit_->setText("max");
            acceleratorCombo_->setCurrentText("auto");
            graphicsCombo_->setCurrentText("std");
            inputCombo_->setCurrentText("平板指针 + 键盘");
            memoryUnitCombo_->setCurrentText("GB");
            if (currentMemoryMb < 4096) {
                memorySpin_->setValue(4);
            }
            diskInterfaceCombo_->setCurrentText("sata");
        } else if (key.contains("macos")) {
            const QString arch = supportedArchitectureOrFallback(detectHostProfile().arch == "aarch64" ? "aarch64" : "x86_64");
            archCombo_->setCurrentText(arch);
            machineCombo_->setCurrentText(defaultMachineForArch(arch));
            diskInterfaceCombo_->setCurrentText("nvme");
        } else if (key.contains("linux")) {
            machineCombo_->setCurrentText(defaultMachineForArch(archCombo_->currentText()));
        }
        bindRefresh();
    });
    connect(archCombo_, &QComboBox::currentTextChanged, this, bindRefresh);
    connect(machineCombo_, &QComboBox::currentTextChanged, this, bindRefresh);
    connect(cpuEdit_, &QLineEdit::textChanged, this, bindRefresh);
    connect(coresSpin_, qOverload<double>(&QDoubleSpinBox::valueChanged), this, bindRefresh);
    connect(memorySpin_, qOverload<double>(&QDoubleSpinBox::valueChanged), this, bindRefresh);
    connect(memoryUnitCombo_, &QComboBox::currentTextChanged, this, bindRefresh);
    connect(acceleratorCombo_, &QComboBox::currentTextChanged, this, bindRefresh);
    connect(networkEnabledCheck_, &QCheckBox::toggled, this, bindRefresh);
    connect(networkModeCombo_, &QComboBox::currentTextChanged, this, bindRefresh);
    connect(networkModelCombo_, &QComboBox::currentTextChanged, this, bindRefresh);
    connect(networkBridgeEdit_, &QLineEdit::textChanged, this, bindRefresh);
    connect(networkMacEdit_, &QLineEdit::textChanged, this, bindRefresh);
    connect(networkForwardsEdit_, &QLineEdit::textChanged, this, bindRefresh);
    connect(audioEnabledCheck_, &QCheckBox::toggled, this, bindRefresh);
    connect(audioDriverCombo_, &QComboBox::currentTextChanged, this, bindRefresh);
    connect(audioModelCombo_, &QComboBox::currentTextChanged, this, bindRefresh);
    connect(graphicsCombo_, &QComboBox::currentTextChanged, this, bindRefresh);
    connect(inputCombo_, &QComboBox::currentTextChanged, this, bindRefresh);
    connect(gpuPassthroughCheck_, &QCheckBox::toggled, this, bindRefresh);
    connect(gpuPassthroughCombo_, &QComboBox::currentTextChanged, this, bindRefresh);
    connect(keepVirtualDisplayCheck_, &QCheckBox::toggled, this, bindRefresh);
    connect(openGlCheck_, &QCheckBox::toggled, this, bindRefresh);
    connect(autoResizeCheck_, &QCheckBox::toggled, this, bindRefresh);
    connect(retinaCheck_, &QCheckBox::toggled, this, bindRefresh);
    connect(tpmCheck_, &QCheckBox::toggled, this, bindRefresh);
    connect(tpmSocketEdit_, &QLineEdit::textChanged, this, bindRefresh);
    connect(kernelEdit_, &QLineEdit::textChanged, this, bindRefresh);
    connect(systemSoftwareRomEdit_, &QLineEdit::textChanged, this, bindRefresh);
    connect(initrdEdit_, &QLineEdit::textChanged, this, bindRefresh);
    connect(appendEdit_, &QLineEdit::textChanged, this, bindRefresh);

    auto* mediaRow = new QHBoxLayout();
    mediaList_ = new QListWidget();
    mediaList_->setDragDropMode(QAbstractItemView::InternalMove);
    mediaList_->setDefaultDropAction(Qt::MoveAction);
    mediaList_->setSelectionMode(QAbstractItemView::SingleSelection);
    mediaList_->setDropIndicatorShown(true);
    mediaList_->setContextMenuPolicy(Qt::CustomContextMenu);
    mediaRow->addWidget(mediaList_, 1);
    auto* mediaButtons = new QVBoxLayout();
    diskInterfaceCombo_ = new QComboBox();
    diskInterfaceCombo_->addItems({"virtio", "ide", "sata", "scsi", "nvme", "usb", "floppy", "cdrom"});
    diskSizeSpin_ = new QDoubleSpinBox();
    diskSizeSpin_->setDecimals(2);
    diskSizeSpin_->setRange(1, 1048576);
    diskSizeSpin_->setValue(32);
    diskSizeUnitCombo_ = new QComboBox();
    diskSizeUnitCombo_->addItems({"GB", "MB", "KB"});
    auto* addDiskButton = new QPushButton("添加磁盘");
    auto* addCdButton = new QPushButton("添加镜像");
    auto* createDiskButton = new QPushButton("创建硬盘");
    auto* downloadButton = new QPushButton("下载镜像");
    auto* switchButton = new QPushButton("一键切换启动镜像");
    connect(addDiskButton, &QPushButton::clicked, this, [this] { addDisk(); });
    connect(addCdButton, &QPushButton::clicked, this, [this] { addCdrom(); });
    connect(createDiskButton, &QPushButton::clicked, this, [this] { createDiskImage(); });
    connect(downloadButton, &QPushButton::clicked, this, [this] { downloadImage(); });
    connect(switchButton, &QPushButton::clicked, this, [this] { switchBootImage(); });
    connect(mediaList_->model(), &QAbstractItemModel::rowsMoved, this, [this] {
        applyMediaListOrder();
    });
    connect(mediaList_, &QWidget::customContextMenuRequested, this, [this](const QPoint& position) {
        QListWidgetItem* item = mediaList_->itemAt(position);
        if (!item) {
            return;
        }

        mediaList_->setCurrentItem(item);
        QMenu menu(this);
        QAction* removeAction = menu.addAction("移除驱动器（保留文件）");
        QAction* selectedAction = menu.exec(mediaList_->viewport()->mapToGlobal(position));
        if (selectedAction == removeAction) {
            removeSelectedMedia();
        }
    });
    qemuMediaControls_ = new QWidget();
    auto* qemuMediaLayout = new QVBoxLayout(qemuMediaControls_);
    qemuMediaLayout->setContentsMargins(0, 0, 0, 0);
    qemuMediaLayout->addWidget(new QLabel("默认接口"));
    qemuMediaLayout->addWidget(diskInterfaceCombo_);
    qemuMediaLayout->addWidget(new QLabel("新硬盘大小"));
    qemuMediaLayout->addWidget(diskSizeSpin_);
    qemuMediaLayout->addWidget(diskSizeUnitCombo_);
    qemuMediaLayout->addWidget(addDiskButton);
    qemuMediaLayout->addWidget(addCdButton);
    qemuMediaLayout->addWidget(createDiskButton);
    qemuMediaLayout->addWidget(downloadButton);
    qemuMediaLayout->addWidget(switchButton);
    mediaButtons->addWidget(qemuMediaControls_);
    systemSoftwareFloppyWidget_ = new QWidget();
    auto* floppyForm = new QFormLayout(systemSoftwareFloppyWidget_);
    floppyForm->setContentsMargins(0, 0, 0, 0);
    auto addFloppyRow = [this, bindRefresh](QLineEdit*& edit, const QString& label) {
        edit = new QLineEdit();
        edit->setPlaceholderText(label + "（可选）");
        auto* browse = new QPushButton("选择...");
        auto* row = new QWidget();
        auto* layout = new QHBoxLayout(row);
        layout->setContentsMargins(0, 0, 0, 0);
        layout->addWidget(edit, 1);
        layout->addWidget(browse);
        connect(browse, &QPushButton::clicked, this, [this, edit, label] {
            const QString path = QFileDialog::getOpenFileName(this, "选择 " + label,
                edit->text().isEmpty() ? QDir::homePath() : QFileInfo(edit->text()).absolutePath(),
                "软盘镜像 (*.dsk *.ima *.img *.flp);;所有文件 (*)");
            if (!path.isEmpty()) edit->setText(path);
        });
        connect(edit, &QLineEdit::textChanged, this, bindRefresh);
        return row;
    };
    floppyForm->addRow("Floppy A", addFloppyRow(systemSoftwareFloppyAEdit_, "Floppy A"));
    floppyForm->addRow("Floppy B", addFloppyRow(systemSoftwareFloppyBEdit_, "Floppy B"));
    mediaButtons->addWidget(systemSoftwareFloppyWidget_);
    mediaButtons->addStretch(1);
    mediaRow->addLayout(mediaButtons);
    editorLayout->addWidget(new QLabel("镜像与磁盘"));
    editorLayout->addLayout(mediaRow);
    mediaList_->setMinimumHeight(140);
    systemSoftwareFloppyWidget_->hide();

    issueText_ = new QTextEdit();
    issueText_->setReadOnly(true);
    issueText_->setMaximumHeight(100);
    commandPreview_ = new QPlainTextEdit();
    commandPreview_->setReadOnly(true);
    commandPreview_->setMinimumHeight(160);
    editorLayout->addWidget(new QLabel("兼容性提示"));
    editorLayout->addWidget(issueText_);
    editorLayout->addWidget(new QLabel("启动配置预览"));
    editorLayout->addWidget(commandPreview_);
    editorLayout->addStretch(1);
}

void VirtualWorldWindow::showEmptyLibrary() {
    currentIndex_ = -1;
    refreshVmList();
    setEditorAvailable(false);
    if (emptyStateLabel_) emptyStateLabel_->show();
    if (editorPane_) editorPane_->hide();
}

void VirtualWorldWindow::setEditorAvailable(bool available) {
    if (editorPane_) editorPane_->setVisible(available);
    if (emptyStateLabel_) emptyStateLabel_->setVisible(!available);
}

QString VirtualWorldWindow::sanitizeFileName(const QString& name) const {
    return cleanVmName(name);
}

QString VirtualWorldWindow::defaultStorageRoot() const {
#if defined(Q_OS_WIN)
    const QList<QStorageInfo> volumes = QStorageInfo::mountedVolumes();
    for (const QStorageInfo& volume : volumes) {
        const QString root = QDir::toNativeSeparators(volume.rootPath()).toUpper();
        if (root.startsWith("D:") && volume.isReady() && !volume.isReadOnly()) {
            return QDir(volume.rootPath()).filePath("VirtualWorld VMs");
        }
    }
    return "C:/VirtualWorld VMs";
#else
    QString base = QStandardPaths::writableLocation(QStandardPaths::AppDataLocation);
    if (base.isEmpty()) {
        base = QStandardPaths::writableLocation(QStandardPaths::DocumentsLocation);
    }
    if (base.isEmpty()) {
        base = QDir::homePath();
    }
    return QDir(base).filePath("VirtualWorld VMs");
#endif
}

static QStringList candidateStorageRoots() {
    QStringList roots;
    auto appendUnique = [&roots](const QString& root) {
        const QString trimmed = QDir::cleanPath(root.trimmed());
        if (trimmed.isEmpty() || roots.contains(trimmed)) {
            return;
        }
        roots.append(trimmed);
    };

    QString appData = QStandardPaths::writableLocation(QStandardPaths::AppDataLocation);
    if (appData.isEmpty()) {
        appData = QDir::homePath();
    }

    appendUnique(QDir(appData).filePath("VirtualWorld VMs"));

    const QFileInfo appDataInfo(appData);
    const QString legacyBase = appDataInfo.dir().absolutePath();
    if (!legacyBase.isEmpty() && legacyBase != appData) {
        appendUnique(QDir(legacyBase).filePath("VirtualWorld VMs"));
    }

    const QString documents = QStandardPaths::writableLocation(QStandardPaths::DocumentsLocation);
    if (!documents.isEmpty()) {
        // Older builds stored newly created VMs in Documents by default.
        appendUnique(QDir(documents).filePath("VirtualWorld VMs"));
    }
    appendUnique(QDir::home().filePath("VirtualWorld VMs"));

#if defined(Q_OS_MACOS)
    // Earlier macOS builds alternated between AppDataLocation paths with one
    // and two "VirtualWorld" components. Keep both layouts discoverable.
    QString applicationSupport = QStandardPaths::writableLocation(QStandardPaths::GenericDataLocation);
    if (applicationSupport.isEmpty()) {
        applicationSupport = QDir::home().filePath("Library/Application Support");
    }
    const QString productRoot = QDir(applicationSupport).filePath("VirtualWorld");
    appendUnique(QDir(productRoot).filePath("VirtualWorld VMs"));
    appendUnique(QDir(productRoot).filePath("VirtualWorld/VirtualWorld VMs"));
#endif

    return roots;
}

QString VirtualWorldWindow::defaultStorageDirForVm(const QString& name) const {
    return QDir(defaultStorageRoot()).filePath(sanitizeFileName(name));
}

QString VirtualWorldWindow::libraryIndexPath() const {
    QDir dir(QStandardPaths::writableLocation(QStandardPaths::AppConfigLocation));
    if (!dir.exists()) {
        dir.mkpath(".");
    }
    return dir.filePath("library.json");
}

void VirtualWorldWindow::refreshGpuPassthroughChoices() {
    if (!gpuPassthroughCombo_) {
        return;
    }
    const QString previous = gpuPassthroughCombo_->currentData().toString();
    gpuPassthroughCombo_->clear();
    const QVector<HostGpuInfo> gpus = detectHostGpus();
    for (const HostGpuInfo& gpu : gpus) {
        gpuPassthroughCombo_->addItem(gpuDisplayText(gpu), gpu.id);
        const int row = gpuPassthroughCombo_->count() - 1;
        gpuPassthroughCombo_->setItemData(row, gpuDisplayText(gpu), Qt::ToolTipRole);
        if (!gpu.passthroughCandidate) {
            gpuPassthroughCombo_->setItemData(row, false, Qt::UserRole + 1);
        }
    }
    const int restored = gpuPassthroughCombo_->findData(previous);
    if (restored >= 0) {
        gpuPassthroughCombo_->setCurrentIndex(restored);
    }
    gpuPassthroughCombo_->setEnabled(!gpus.isEmpty());
}

bool VirtualWorldWindow::maybeAskGpuPassthrough(VmConfig& config) {
    if (config.gpuPassthrough.enabled || !config.gpuPassthrough.askOnStart) {
        return true;
    }
    const QVector<HostGpuInfo> gpus = detectHostGpus();
    if (gpus.size() < 2) {
        return true;
    }
    QVector<HostGpuInfo> candidates;
    for (const HostGpuInfo& gpu : gpus) {
        if (gpu.passthroughCandidate) {
            candidates.append(gpu);
        }
    }
    if (candidates.isEmpty()) {
        QMessageBox::information(this, "GPU 直通不可用", "检测到多块 GPU，但没有安全可用的直通候选。Apple Silicon 内建 GPU、核显/集显或链路不满足要求的设备不会用于直通。");
        return true;
    }

    QDialog dialog(this);
    dialog.setWindowTitle("GPU 直通");
    dialog.setStyleSheet(QString("QDialog{background:#f7f9fc;color:#1f2937;font-size:14px;}"
                                 "QComboBox{background:white;border:1px solid #d7deea;border-radius:8px;padding:9px;min-height:30px;}")
                         + buttonStyle());
    auto* layout = new QVBoxLayout(&dialog);
    auto* label = new QLabel("检测到多块 GPU。是否将其中一块作为 GPU 直通设备？\nVirtualWorld 会保留虚拟显卡，方便系统安装和驱动未就绪时显示。");
    label->setWordWrap(true);
    layout->addWidget(label);
    auto* combo = new QComboBox();
    for (const HostGpuInfo& gpu : candidates) {
        combo->addItem(gpuDisplayText(gpu), gpu.id);
    }
    layout->addWidget(combo);
    auto* keepVirtualDisplay = new QCheckBox("保留虚拟显卡用于安装和救援显示");
    keepVirtualDisplay->setChecked(true);
    layout->addWidget(keepVirtualDisplay);
    auto* buttons = new QDialogButtonBox(QDialogButtonBox::Yes | QDialogButtonBox::No | QDialogButtonBox::Cancel);
    buttons->button(QDialogButtonBox::Yes)->setText("使用直通");
    buttons->button(QDialogButtonBox::No)->setText("不使用");
    buttons->button(QDialogButtonBox::Cancel)->setText("取消启动");
    layout->addWidget(buttons);
    connect(buttons, &QDialogButtonBox::accepted, &dialog, &QDialog::accept);
    connect(buttons->button(QDialogButtonBox::No), &QPushButton::clicked, &dialog, &QDialog::reject);
    connect(buttons->button(QDialogButtonBox::Cancel), &QPushButton::clicked, &dialog, [this, &dialog] {
        dialog.done(2);
    });
    const int result = dialog.exec();
    if (result == 2) {
        return false;
    }
    if (result == QDialog::Accepted) {
        applyGpuSelection(config, candidates.value(combo->currentIndex()), true, keepVirtualDisplay->isChecked());
    } else {
        config.gpuPassthrough.askOnStart = false;
    }
    return true;
}

void VirtualWorldWindow::applyGuestPreset(VmConfig& config) const {
    const QString guest = config.guestOs.toLower();
    if (isSystemSoftwareUi(config.guestOs)) {
        config.backend = "system-software";
        config.systemSoftwareVersion = systemSoftwareVersionFromUi(config.guestOs);
        config.systemSoftwareMachine = systemSoftwareMachineFromUi(config.guestOs);
        config.systemSoftwareRomType = systemSoftwareRomTypeFromUi(config.guestOs);
        config.architecture = "m68k";
        config.machine = "q800";
        config.cpuModel = "m68000";
        config.cpuCores = 1;
        config.accelerator = "tcg";
        setMemoryKb(config, systemSoftwarePresetMemoryKb(config.guestOs));
        config.tpm.enabled = false;
        config.networks.clear();
        config.audio = AudioConfig{};
        config.cdroms.clear();
        config.graphics.adapter.clear();
        config.graphics.vga.clear();
        config.graphics.ramMb = 1;
        config.graphics.openGl = false;
        config.graphics.autoResize = false;
        config.graphics.dynamicResolution = false;
        config.graphics.retina = false;
    } else if (isClassicMacOsUi(config.guestOs)) {
        config.architecture = supportedArchitectureOrFallback(config.architecture.trimmed().isEmpty() ? "ppc" : config.architecture);
        config.machine = defaultMachineForArch(config.architecture);
        if (normalizeArchitecture(config.architecture) == "ppc") {
            config.machine = "mac99";
        } else if (normalizeArchitecture(config.architecture) == "m68k") {
            config.machine = "q800";
        }
        config.accelerator = "tcg";
        if (config.cpuModel.trimmed().isEmpty() || config.cpuModel == "max" || config.cpuModel == "host") {
            config.cpuModel = normalizeArchitecture(config.architecture) == "m68k" ? "m68040" : "G3";
        }
        config.cpuCores = 1;
        setMemoryKb(config,
                    qBound<qint64>(qint64(16) * 1024,
                                    effectiveMemoryKb(config),
                                    qint64(512) * 1024));
        config.tpm.enabled = false;
        config.networks = config.networks.isEmpty()
            ? QVector<NetworkConfig>{NetworkConfig{}}
            : config.networks;
        config.audio.enabled = true;
        config.graphics.adapter.clear();
        config.graphics.vga = normalizeArchitecture(config.architecture) == "m68k" ? QString() : "std";
        config.graphics.ramMb = normalizeArchitecture(config.architecture) == "m68k" ? 2 : 8;
        config.graphics.openGl = false;
        config.graphics.autoResize = true;
        config.graphics.dynamicResolution = false;
        config.graphics.retina = false;
        if (config.disks.isEmpty() && config.cdroms.isEmpty()) {
            config.disks.clear();
        }
    } else if (isLegacyPcUi(config.guestOs)) {
        config.architecture = supportedArchitectureOrFallback("i386");
        config.machine = "pc";
        config.accelerator = "tcg";
        config.cpuModel = guest.contains("dos") ? "486" : "pentium";
        config.cpuCores = 1;
        const qint64 maximumKb = (guest.contains("dos") ? 16 : 64) * 1024;
        setMemoryKb(config, qMin(maximumKb, effectiveMemoryKb(config)));
        config.tpm.enabled = false;
        config.networks = config.networks.isEmpty()
            ? QVector<NetworkConfig>{NetworkConfig{}}
            : config.networks;
        config.audio = AudioConfig{};
        config.graphics.adapter.clear();
        config.graphics.vga = "cirrus";
        config.graphics.ramMb = guest.contains("dos") ? 1 : 4;
        config.graphics.openGl = false;
        config.graphics.autoResize = true;
        config.graphics.dynamicResolution = false;
        config.graphics.retina = false;
    } else if (guest.contains("windows")) {
        config.architecture = supportedArchitectureOrFallback("x86_64");
        config.machine = "pc";
        config.accelerator = "auto";
        config.tpm.enabled = true;
        setMemoryKb(config, qMax<qint64>(4096 * 1024, effectiveMemoryKb(config)));
        config.graphics.adapter.clear();
        config.graphics.vga = "std";
        config.graphics.openGl = false;
        config.graphics.ramMb = 128;
        config.networks = config.networks.isEmpty() ? QVector<NetworkConfig>{NetworkConfig{}} : config.networks;
        config.audio.enabled = true;
        config.audio.model = config.audio.model.isEmpty() ? "ich9-intel-hda" : config.audio.model;
        for (DiskConfig& disk : config.disks) {
            if (disk.media == "disk"
                && (disk.interfaceName.trimmed().isEmpty() || disk.interfaceName == "virtio")) {
                disk.interfaceName = "sata";
            }
        }
    } else if (guest.contains("macos")) {
        const HostProfile host = detectHostProfile();
        config.architecture = supportedArchitectureOrFallback(host.arch == "aarch64" ? "aarch64" : "x86_64");
        config.machine = defaultMachineForArch(config.architecture);
        config.accelerator = "auto";
        config.graphics.ramMb = 1024;
        config.graphics.retina = true;
        config.graphics.dynamicResolution = true;
        config.graphics.autoResize = true;
        config.networks = config.networks.isEmpty() ? QVector<NetworkConfig>{NetworkConfig{}} : config.networks;
        config.audio.enabled = true;
    } else if (guest.contains("linux")) {
        config.machine = defaultMachineForArch(config.architecture);
        config.graphics.adapter = "virtio-vga";
        config.graphics.ramMb = 128;
        config.networks = config.networks.isEmpty() ? QVector<NetworkConfig>{NetworkConfig{}} : config.networks;
        config.audio.enabled = true;
    } else {
        config.graphics.ramMb = config.graphics.ramMb > 0 ? config.graphics.ramMb : 128;
        if (!isSystemSoftwareUi(config.guestOs) && !isClassicMacOsUi(config.guestOs)) {
            config.networks = config.networks.isEmpty() ? QVector<NetworkConfig>{NetworkConfig{}} : config.networks;
            config.audio.enabled = true;
        }
    }
    if (!isLegacyPcUi(config.guestOs) && !isClassicMacOsUi(config.guestOs) && hostHasDiscreteGpu()) {
        config.graphics.openGl = true;
        if (config.graphics.adapter.isEmpty() || config.graphics.adapter == "virtio-vga") {
            config.graphics.adapter = "virtio-vga-gl";
        }
        config.graphics.renderer = "discrete";
    }
}

VmConfig VirtualWorldWindow::createVmWithWizard() {
    QDialog dialog(this);
    dialog.setWindowTitle("创建虚拟机");
    QSize dialogSize(760, 640);
    if (QScreen* screen = QApplication::primaryScreen()) {
        const QRect available = screen->availableGeometry();
        dialogSize.setWidth(qMin(dialogSize.width(), qMax(560, int(available.width() * 0.72))));
        dialogSize.setHeight(qMin(dialogSize.height(), qMax(460, int(available.height() * 0.78))));
    }
    dialog.resize(dialogSize);
    dialog.setStyleSheet(QString("QDialog{background:#f7f9fc;color:#1f2937;font-size:14px;}"
                                 "QLineEdit,QComboBox,QDoubleSpinBox{background:white;border:1px solid #d7deea;border-radius:8px;padding:9px;min-height:30px;}"
                                 "QLabel{background:transparent;}")
                         + buttonStyle());
    auto* layout = new QVBoxLayout(&dialog);
    layout->setContentsMargins(24, 22, 24, 18);
    layout->setSpacing(14);
    auto* title = new QLabel("新建虚拟机");
    title->setStyleSheet("QLabel{font-size:22px;font-weight:700;color:#111827;}");
    layout->addWidget(title);

    auto* scroll = new QScrollArea();
    scroll->setWidgetResizable(true);
    scroll->setFrameShape(QFrame::NoFrame);
    scroll->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    scroll->setVerticalScrollBarPolicy(Qt::ScrollBarAsNeeded);
    scroll->setStyleSheet("QScrollArea{background:transparent;border:0;}");
    auto* scrollContent = new QWidget();
    auto* scrollLayout = new QVBoxLayout(scrollContent);
    scrollLayout->setContentsMargins(0, 0, 8, 0);
    scrollLayout->setSpacing(12);

    auto* form = new QFormLayout();
    form->setHorizontalSpacing(18);
    form->setVerticalSpacing(10);
    form->setFieldGrowthPolicy(QFormLayout::ExpandingFieldsGrow);
    form->setLabelAlignment(Qt::AlignRight | Qt::AlignVCenter);
    auto* name = new QLineEdit("macOS");
    auto* storage = new QLineEdit();
    storage->setPlaceholderText("留空则自动使用系统默认虚拟机文件夹");
    auto* storageBrowse = new QPushButton("选择...");
    auto* storageRow = new QWidget();
    auto* storageLayout = new QHBoxLayout(storageRow);
    storageLayout->setContentsMargins(0, 0, 0, 0);
    storageLayout->setSpacing(8);
    storageLayout->addWidget(storage, 1);
    storageLayout->addWidget(storageBrowse);
    auto* os = new QComboBox();
    os->addItems({"macOS", "Classic MacOS", "Macintosh 128K", "Macintosh 512K", "Mac Plus", "Twiggy", "System Software 0", "System Software 1", "System Software 2", "System Software 3", "System Software 4", "System Software 5", "System Software 6", "Windows", "Old Windows", "Linux", "BSD", "DOS", "Other"});
    auto* arch = new QComboBox();
    arch->addItems(availableArchitectures());
    arch->setEditable(true);
    auto* inputMode = new QComboBox();
    inputMode->addItems({"平板指针 + 键盘", "USB 键盘鼠标", "PS/2 键盘鼠标", "ADB 键盘鼠标"});
    auto* networkEnabled = new QCheckBox("启用虚拟网络");
    networkEnabled->setChecked(true);
    auto* audioEnabled = new QCheckBox("启用虚拟声卡");
    audioEnabled->setChecked(true);
    auto* passthrough = new QCheckBox("创建后默认使用 GPU 直通");
    auto* passthroughGpu = new QComboBox();
    const QVector<HostGpuInfo> wizardGpus = detectHostGpus();
    for (const HostGpuInfo& gpu : wizardGpus) {
        passthroughGpu->addItem(gpuDisplayText(gpu), gpu.id);
        passthroughGpu->setItemData(passthroughGpu->count() - 1, gpuDisplayText(gpu), Qt::ToolTipRole);
    }
    passthroughGpu->setMinimumContentsLength(18);
    passthroughGpu->setSizeAdjustPolicy(QComboBox::AdjustToMinimumContentsLengthWithIcon);
    passthroughGpu->setMaximumWidth(420);
    passthroughGpu->setEnabled(!wizardGpus.isEmpty());
    auto* keepVirtualDisplay = new QCheckBox("保留虚拟显卡用于安装和救援显示");
    keepVirtualDisplay->setChecked(true);
    auto* cpu = new QDoubleSpinBox();
    cpu->setDecimals(0);
    cpu->setRange(1, 128);
    cpu->setValue(4);
    auto* memory = new QDoubleSpinBox();
    memory->setDecimals(2);
    memory->setRange(1, 1048576);
    memory->setValue(4);
    auto* memoryUnit = new QComboBox();
    memoryUnit->addItems({"GB", "MB", "KB"});
    auto* memoryRow = new QWidget();
    auto* memoryLayout = new QHBoxLayout(memoryRow);
    memoryLayout->setContentsMargins(0, 0, 0, 0);
    memoryLayout->addWidget(memory);
    memoryLayout->addWidget(memoryUnit);
    auto* diskSize = new QDoubleSpinBox();
    diskSize->setDecimals(2);
    diskSize->setRange(1, 1048576);
    diskSize->setValue(64);
    auto* diskUnit = new QComboBox();
    diskUnit->addItems({"GB", "MB", "KB"});
    auto* diskRow = new QWidget();
    auto* diskLayout = new QHBoxLayout(diskRow);
    diskLayout->setContentsMargins(0, 0, 0, 0);
    diskLayout->addWidget(diskSize);
    diskLayout->addWidget(diskUnit);
    auto* interfaceName = new QComboBox();
    interfaceName->addItems({"virtio", "ide", "sata", "scsi", "nvme", "usb"});
    auto* installImage = new QLineEdit();
    installImage->setPlaceholderText("可选：ISO、软盘、磁盘镜像或 IPSW");
    auto* installImageBrowse = new QPushButton("选择...");
    auto* installImageRow = new QWidget();
    auto* installImageLayout = new QHBoxLayout(installImageRow);
    installImageLayout->setContentsMargins(0, 0, 0, 0);
    installImageLayout->setSpacing(8);
    installImageLayout->addWidget(installImage, 1);
    installImageLayout->addWidget(installImageBrowse);
    auto* floppyB = new QLineEdit();
    floppyB->setPlaceholderText("可选：Floppy B 软盘镜像");
    auto* floppyBBrowse = new QPushButton("选择...");
    auto* floppyBRow = new QWidget();
    auto* floppyBLayout = new QHBoxLayout(floppyBRow);
    floppyBLayout->setContentsMargins(0, 0, 0, 0);
    floppyBLayout->addWidget(floppyB, 1);
    floppyBLayout->addWidget(floppyBBrowse);
    auto* systemSoftwareRom = new QLineEdit();
    systemSoftwareRom->setPlaceholderText("System Software ROM");
    auto* systemSoftwareRomBrowse = new QPushButton("选择...");
    auto* systemSoftwareRomRow = new QWidget();
    auto* systemSoftwareRomLayout = new QHBoxLayout(systemSoftwareRomRow);
    systemSoftwareRomLayout->setContentsMargins(0, 0, 0, 0);
    systemSoftwareRomLayout->setSpacing(8);
    systemSoftwareRomLayout->addWidget(systemSoftwareRom, 1);
    systemSoftwareRomLayout->addWidget(systemSoftwareRomBrowse);
    auto* download = new QCheckBox("创建后下载/获取安装镜像");
    auto* import = new QCheckBox("创建后再选择镜像");

    form->addRow("名称", name);
    form->addRow("保存位置", storageRow);
    form->addRow("操作系统", os);
    form->addRow("架构", arch);
    form->addRow("CPU 核心", cpu);
    form->addRow("内存", memoryRow);
    form->addRow("网络", networkEnabled);
    form->addRow("声卡", audioEnabled);
    auto* diskSizeLabel = new QLabel("硬盘大小");
    auto* interfaceLabel = new QLabel("磁盘接口");
    form->addRow(diskSizeLabel, diskRow);
    form->addRow(interfaceLabel, interfaceName);
    form->addRow("启动/安装镜像", installImageRow);
    form->addRow("Floppy B", floppyBRow);
    form->addRow("System Software ROM", systemSoftwareRomRow);
    form->addRow("输入方式", inputMode);
    form->addRow("", passthrough);
    form->addRow("直通 GPU", passthroughGpu);
    form->addRow("", keepVirtualDisplay);
    form->addRow("", download);
    form->addRow("", import);
    scrollLayout->addLayout(form);

    auto* hint = new QLabel();
    hint->setWordWrap(true);
    hint->setStyleSheet("QLabel{color:#667085;}");
    scrollLayout->addWidget(hint);
    scrollLayout->addStretch(1);
    scroll->setWidget(scrollContent);
    layout->addWidget(scroll, 1);

    auto* buttons = new QDialogButtonBox(QDialogButtonBox::Cancel | QDialogButtonBox::Ok);
    buttons->button(QDialogButtonBox::Ok)->setText("创建");
    buttons->button(QDialogButtonBox::Cancel)->setText("取消");
    layout->addWidget(buttons);
    connect(buttons, &QDialogButtonBox::accepted, &dialog, &QDialog::accept);
    connect(buttons, &QDialogButtonBox::rejected, &dialog, &QDialog::reject);
    connect(storageBrowse, &QPushButton::clicked, &dialog, [this, storage] {
        const QString initial = storage->text().trimmed().isEmpty() ? defaultStorageRoot() : storage->text().trimmed();
        const QString path = QFileDialog::getExistingDirectory(this, "选择虚拟机保存文件夹", initial);
        if (!path.isEmpty()) {
            storage->setText(path);
        }
    });
    connect(installImageBrowse, &QPushButton::clicked, &dialog, [&dialog, installImage] {
        const QString initial = installImage->text().trimmed().isEmpty()
                                    ? QStandardPaths::writableLocation(QStandardPaths::HomeLocation)
                                    : QFileInfo(installImage->text()).absolutePath();
        const QString path = QFileDialog::getOpenFileName(
            &dialog,
            "选择启动/安装镜像",
            initial,
            bootImageFilter());
        if (!path.isEmpty()) {
            installImage->setText(path);
        }
    });
    connect(systemSoftwareRomBrowse, &QPushButton::clicked, &dialog, [&dialog, systemSoftwareRom] {
        const QString initial = systemSoftwareRom->text().trimmed().isEmpty()
                                    ? QStandardPaths::writableLocation(QStandardPaths::HomeLocation)
                                    : QFileInfo(systemSoftwareRom->text()).absolutePath();
        const QString path = QFileDialog::getOpenFileName(&dialog, "选择 System Software ROM", initial, "ROM / 镜像 (*)");
        if (!path.isEmpty()) systemSoftwareRom->setText(path);
    });
    connect(floppyBBrowse, &QPushButton::clicked, &dialog, [&dialog, floppyB] {
        const QString path = QFileDialog::getOpenFileName(&dialog, "选择 Floppy B", QDir::homePath(), "软盘镜像 (*.dsk *.ima *.img *.flp);;所有文件 (*)");
        if (!path.isEmpty()) floppyB->setText(path);
    });
    auto updateInstallOptions = [os, arch, hint, download, import, installImage, installImageRow, floppyBRow, systemSoftwareRom, diskSizeLabel, interfaceLabel, diskRow, interfaceName] {
        const bool systemSoftware = isSystemSoftwareUi(os->currentText());
        const QString systemVersion = systemSoftwareVersionFromUi(os->currentText());
        const bool disklessSystemSoftware = systemSoftware && isDisklessSystemSoftwareVersion(systemVersion);
        installImage->setPlaceholderText(
            systemSoftware
                ? (disklessSystemSoftware ? "可选：Floppy A 软盘镜像" : "Floppy A 软盘镜像（必选）")
                : "可选：ISO、软盘、磁盘镜像或 IPSW");
        installImageRow->setVisible(!systemSoftware || systemSoftware);
        floppyBRow->setVisible(systemSoftware);
        diskSizeLabel->setVisible(!systemSoftware);
        diskRow->setVisible(!systemSoftware);
        interfaceLabel->setVisible(!systemSoftware);
        interfaceName->setVisible(!systemSoftware);
        systemSoftwareRom->setEnabled(systemSoftware);
        if (!systemSoftware) systemSoftwareRom->clear();
        if (systemSoftware) {
            download->setChecked(false);
            import->setChecked(false);
            download->setEnabled(false);
            import->setEnabled(false);
            hint->setText(
                disklessSystemSoftware
                    ? "System Software 0-3 使用独立的 Mini vMac 兼容运行时。ROM 可自动生成，启动磁盘可留空；不会创建或转换 QEMU 磁盘。\n"
                    : "System Software 4-6 使用独立的 Mini vMac 兼容运行时。请选择对应 ROM 和启动磁盘镜像；不会创建或转换 QEMU 磁盘。\n");
            return;
        }
        const QString guest = os->currentText();
        if (isModernMacOsUi(guest)) {
            hint->setText(macOsInstallMethodText(arch->currentText()));
            const bool ipswAvailable = canUseNativeMacIpswFlow(arch->currentText());
            download->setEnabled(ipswAvailable);
            download->setToolTip(ipswAvailable ? QString() : "当前平台或架构不支持自动 IPSW 路径，请手动导入镜像。");
            if (!ipswAvailable) {
                download->setChecked(false);
                import->setChecked(true);
            }
            return;
        }
        download->setEnabled(true);
        download->setToolTip(QString());
        hint->setText("Classic MacOS、Linux 和其他系统镜像需要手动导入。Windows 将打开微软官方下载页；Apple Silicon macOS 可获取 IPSW 候选。");
    };

    auto applyOsDefaults = [name, arch, interfaceName, inputMode, networkEnabled, audioEnabled, cpu, memory, memoryUnit, diskSize, diskUnit, updateInstallOptions](const QString& value) {
        const QString key = value.toLower();
        name->setText(displayNameForGuest(value));
        if (isSystemSoftwareUi(value)) {
            arch->setCurrentText("m68k");
            interfaceName->setCurrentText("floppy");
            inputMode->setCurrentText("ADB 键盘鼠标");
            cpu->setValue(1);
            memoryUnit->setCurrentText("KB");
            memory->setValue(systemSoftwarePresetMemoryKb(value));
            diskUnit->setCurrentText("KB");
            diskSize->setValue(1);
            networkEnabled->setChecked(false);
            audioEnabled->setChecked(false);
        } else if (isClassicMacOsUi(value)) {
            arch->setCurrentText(supportedArchitectureOrFallback("ppc"));
            interfaceName->setCurrentText("ide");
            inputMode->setCurrentText("ADB 键盘鼠标");
            cpu->setValue(1);
            memoryUnit->setCurrentText("MB");
            memory->setValue(128);
            diskUnit->setCurrentText("GB");
            diskSize->setValue(2);
            networkEnabled->setChecked(true);
            audioEnabled->setChecked(true);
        } else if (isOldWindowsUi(value)) {
            arch->setCurrentText(supportedArchitectureOrFallback("i386"));
            interfaceName->setCurrentText("ide");
            inputMode->setCurrentText("PS/2 键盘鼠标");
            cpu->setValue(1);
            memoryUnit->setCurrentText("MB");
            memory->setValue(64);
            diskUnit->setCurrentText("GB");
            diskSize->setValue(2);
            networkEnabled->setChecked(true);
            audioEnabled->setChecked(false);
        } else if (key.contains("macos")) {
            arch->setCurrentText(supportedArchitectureOrFallback(detectHostProfile().arch == "aarch64" ? "aarch64" : "x86_64"));
            interfaceName->setCurrentText("nvme");
            inputMode->setCurrentText("平板指针 + 键盘");
            cpu->setValue(qMax(4, int(cpu->value())));
            memoryUnit->setCurrentText("GB");
            if (memory->value() < 8) {
                memory->setValue(8);
            }
        } else if (key.contains("old windows")) {
            arch->setCurrentText(supportedArchitectureOrFallback("i386"));
            interfaceName->setCurrentText("ide");
            inputMode->setCurrentText("PS/2 键盘鼠标");
            cpu->setValue(1);
            memoryUnit->setCurrentText("MB");
            memory->setValue(64);
            diskUnit->setCurrentText("MB");
            diskSize->setValue(64);
        } else if (key.contains("windows")) {
            arch->setCurrentText(supportedArchitectureOrFallback("x86_64"));
            interfaceName->setCurrentText("sata");
            inputMode->setCurrentText("平板指针 + 键盘");
            cpu->setValue(qMax(4, int(cpu->value())));
            memoryUnit->setCurrentText("GB");
            if (memory->value() < 4) {
                memory->setValue(4);
            }
            networkEnabled->setChecked(true);
            audioEnabled->setChecked(true);
        } else if (key.contains("dos")) {
            arch->setCurrentText(supportedArchitectureOrFallback("i386"));
            interfaceName->setCurrentText("ide");
            inputMode->setCurrentText("PS/2 键盘鼠标");
            cpu->setValue(1);
            memoryUnit->setCurrentText("MB");
            memory->setValue(16);
            diskUnit->setCurrentText("MB");
            diskSize->setValue(512);
            networkEnabled->setChecked(false);
            audioEnabled->setChecked(false);
        } else if (key.contains("linux") || key.contains("bsd")) {
            cpu->setValue(qMax(2, int(cpu->value())));
            memoryUnit->setCurrentText("GB");
            if (memory->value() < 2) {
                memory->setValue(2);
            }
            networkEnabled->setChecked(true);
            audioEnabled->setChecked(true);
        } else {
            cpu->setValue(qMax(2, int(cpu->value())));
            memoryUnit->setCurrentText("GB");
            if (memory->value() < 2) {
                memory->setValue(2);
            }
            networkEnabled->setChecked(true);
            audioEnabled->setChecked(true);
        }
        updateInstallOptions();
    };

    connect(os, &QComboBox::currentTextChanged, &dialog, applyOsDefaults);
    connect(arch, &QComboBox::currentTextChanged, &dialog, [updateInstallOptions] {
        updateInstallOptions();
    });
    applyOsDefaults(os->currentText());

    VmConfig config;
    config.name.clear();
    if (dialog.exec() != QDialog::Accepted) {
        return config;
    }
    config.name = name->text().trimmed().isEmpty() ? "VirtualWorld VM" : name->text().trimmed();
    config.storageDir = storage->text().trimmed();
    if (config.storageDir.isEmpty()) {
        config.storageDir = defaultStorageDirForVm(config.name);
    }
    config.guestOs = os->currentText();
    config.backend = isSystemSoftwareUi(config.guestOs) ? "system-software" : "qemu";
    config.systemSoftwareVersion = isSystemSoftwareUi(config.guestOs)
                                       ? systemSoftwareVersionFromUi(config.guestOs)
                                       : QString();
    config.systemSoftwareRomPath = isSystemSoftwareUi(config.guestOs) ? systemSoftwareRom->text().trimmed() : QString();
    config.systemSoftwareMachine = isSystemSoftwareUi(config.guestOs) ? systemSoftwareMachineFromUi(config.guestOs) : QString();
    config.systemSoftwareRomType = isSystemSoftwareUi(config.guestOs) ? systemSoftwareRomTypeFromUi(config.guestOs) : QString();
    config.architecture = supportedArchitectureOrFallback(arch->currentText());
    config.cpuCores = int(cpu->value());
    setMemoryKb(config, toKb(memory->value(), memoryUnit->currentText()));
    config.machine = defaultMachineForArch(config.architecture);
    config.accelerator = "auto";
    config.graphics.autoResize = true;
    config.graphics.dynamicResolution = true;
    config.graphics.retina = true;
    if (hostHasDiscreteGpu()) {
        config.graphics.openGl = true;
        config.graphics.adapter = "virtio-vga-gl";
        config.graphics.renderer = "discrete";
    }
    applyInputPreset(config, inputMode->currentText());
    applyGuestPreset(config);
    if (isSystemSoftwareUi(config.guestOs)) {
        config.networks.clear();
        config.audio = AudioConfig{};
    } else {
        if (networkEnabled->isChecked()) {
            if (config.networks.isEmpty()) config.networks.append(NetworkConfig{});
        } else {
            config.networks.clear();
        }
        config.audio.enabled = audioEnabled->isChecked();
        if (!config.audio.enabled) {
            config.audio = AudioConfig{};
        }
    }
    DiskConfig systemDisk;
    systemDisk.path = QDir(config.storageDir).filePath("disk0.qcow2");
    systemDisk.interfaceName = interfaceName->currentText();
    if (isClassicMacOsUi(config.guestOs)
        && normalizeArchitecture(config.architecture) == "ppc"
        && machineBase(config.machine) == "mac99") {
        systemDisk.interfaceName = "ide";
    }
    systemDisk.media = "disk";
    systemDisk.format = "qcow2";
    systemDisk.readonly = false;
    systemDisk.sizeMb = toMb(diskSize->value(), diskUnit->currentText());
    if (isSystemSoftwareUi(config.guestOs)) {
        config.disks.clear();
        const auto appendFloppy = [&config](const QString& path, const QString& role) {
            if (path.trimmed().isEmpty()) return;
            DiskConfig disk;
            disk.path = QFileInfo(path.trimmed()).absoluteFilePath();
            disk.interfaceName = "floppy";
            disk.format = QFileInfo(disk.path).suffix().toLower();
            disk.media = "floppy";
            disk.role = role;
            config.disks.append(disk);
        };
        appendFloppy(installImage->text(), "floppy-a");
        appendFloppy(floppyB->text(), "floppy-b");
    } else if (!systemDisk.path.isEmpty()) {
        config.disks.append(systemDisk);
    }
    if (passthrough->isChecked() && passthroughGpu->currentIndex() >= 0) {
        applyGpuSelection(config, wizardGpus.value(passthroughGpu->currentIndex()), true, keepVirtualDisplay->isChecked());
    }
    diskInterfaceCombo_->setCurrentText(interfaceName->currentText());
    diskSizeSpin_->setValue(diskSize->value());
    diskSizeUnitCombo_->setCurrentText(diskUnit->currentText());
    if (!isSystemSoftwareUi(config.guestOs) && !installImage->text().trimmed().isEmpty()) {
        config.extraArgs << "__virtualworld_install_image__=" + QFileInfo(installImage->text()).absoluteFilePath();
    }
    if (download->isChecked()) {
        config.extraArgs << "__virtualworld_download_after_create__";
    } else if (import->isChecked()) {
        config.extraArgs << "__virtualworld_import_after_create__";
    }
    return config;
}

int VirtualWorldWindow::memoryValueMb() const {
    return int((memoryValueKb() + 1023) / 1024);
}

qint64 VirtualWorldWindow::memoryValueKb() const {
    return toKb(memorySpin_->value(), memoryUnitCombo_->currentText());
}

qint64 VirtualWorldWindow::sizeValueMb() const {
    return toMb(diskSizeSpin_->value(), diskSizeUnitCombo_->currentText());
}

void VirtualWorldWindow::refreshVmList() {
    const bool blocked = vmList_->blockSignals(true);
    vmList_->clear();
    for (const VmConfig& vm : vms_) {
        vmList_->addItem(vm.name);
    }
    vmList_->blockSignals(blocked);
}

void VirtualWorldWindow::loadSavedLibrary() {
    vms_.clear();
    QSet<QString> seenConfigPaths;
    auto loadConfigPath = [this, &seenConfigPaths](const QString& rawPath) {
        const QFileInfo info(rawPath);
        if (!info.exists() || !info.isFile()) {
            return;
        }
        const QString configPath = info.canonicalFilePath().isEmpty() ? info.absoluteFilePath() : info.canonicalFilePath();
        if (seenConfigPaths.contains(configPath)) {
            return;
        }
        try {
            VmConfig config = manager_.loadConfig(configPath);
            if (config.storageDir.trimmed().isEmpty()) {
                config.storageDir = info.dir().absolutePath();
            }
            QString recoveryError;
            if (ensureRecoveryGuardDisk(config, &recoveryError)) {
                manager_.saveConfig(configPath, config, nullptr);
            }
            vms_.append(config);
            seenConfigPaths.insert(configPath);
        } catch (...) {
            // Broken VM configs should not prevent the library from opening.
        }
    };

    QFile indexFile(libraryIndexPath());
    if (indexFile.open(QIODevice::ReadOnly)) {
        const QJsonDocument document = QJsonDocument::fromJson(indexFile.readAll());
        const QJsonArray paths = document.object().value("configs").toArray();
        for (const QJsonValue& item : paths) {
            loadConfigPath(item.toString());
        }
    }

    for (const QString& rootPath : candidateStorageRoots()) {
        QDir root(rootPath);
        if (!root.exists()) {
            continue;
        }
        loadConfigPath(root.filePath("config.json"));

        QDirIterator configs(root.absolutePath(),
                             {"config.json"},
                             QDir::Files | QDir::Readable,
                             QDirIterator::Subdirectories);
        while (configs.hasNext()) {
            loadConfigPath(configs.next());
        }
    }
    saveLibraryIndex();
    refreshVmList();
    if (!vms_.isEmpty()) {
        vmList_->setCurrentRow(0);
        setEditorAvailable(true);
    } else {
        showEmptyLibrary();
    }
}

void VirtualWorldWindow::saveLibraryIndex() const {
    QJsonArray configs;
    QSet<QString> seen;
    for (const VmConfig& vm : vms_) {
        const QString storageDir = vm.storageDir.trimmed().isEmpty() ? defaultStorageDirForVm(vm.name) : vm.storageDir.trimmed();
        const QString path = QFileInfo(QDir(storageDir).filePath("config.json")).absoluteFilePath();
        if (!seen.contains(path)) {
            configs.append(path);
            seen.insert(path);
        }
    }
    QFile file(libraryIndexPath());
    if (!file.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        return;
    }
    file.write(QJsonDocument(QJsonObject{{"configs", configs}}).toJson(QJsonDocument::Indented));
}

void VirtualWorldWindow::persistCurrentVm() {
    if (currentIndex_ < 0 || currentIndex_ >= vms_.size()) {
        return;
    }
    VmConfig config = editorToVm();
    if (config.storageDir.trimmed().isEmpty()) {
        config.storageDir = defaultStorageDirForVm(config.name);
    }
    QString storageError;
    if (!prepareStorageDirectory(&config.storageDir, &storageError)) {
        appendIssueText({{"error", "storage", storageError}});
        return;
    }
    if (!ensureSystemSoftwareRom(config, &storageError)) {
        appendIssueText({{"error", "system_software_rom_generation", storageError}});
        return;
    }
    normalizeBootMediaOrder(config);
    QString recoveryError;
    if (!ensureRecoveryGuardDisk(config, &recoveryError)) {
        appendIssueText({{"error", "recovery_guard", recoveryError}});
        return;
    }
    const QDir dir(config.storageDir);
    QString error;
    if (manager_.saveConfig(dir.filePath("config.json"), config, &error)) {
        vms_[currentIndex_] = config;
        saveLibraryIndex();
    }
}

bool VirtualWorldWindow::ensureSystemSoftwareRom(VmConfig& config, QString* error) const {
    if (!isSystemSoftwareConfig(config)) {
        return true;
    }
    return vw::ensureSystemSoftwareRom(&config.systemSoftwareRomPath, config.storageDir, error,
                                       config.systemSoftwareRomType);
}

void VirtualWorldWindow::loadVmToEditor(const VmConfig& config) {
    loadingEditor_ = true;
    VmConfig editable = manager_.normaliseConfig(config);
    normalizeBootMediaOrder(editable);
    if (currentIndex_ >= 0 && currentIndex_ < vms_.size()) {
        vms_[currentIndex_] = editable;
    }
    nameEdit_->setText(editable.name);
    storageEdit_->setText(editable.storageDir);
    archCombo_->setCurrentText(supportedArchitectureOrFallback(editable.architecture));
    guestOsCombo_->setCurrentText(editable.guestOs);
    machineCombo_->setCurrentText(editable.machine.isEmpty() ? defaultMachineForArch(editable.architecture) : machineBase(editable.machine));
    cpuEdit_->setText(editable.cpuModel);
    coresSpin_->setValue(editable.cpuCores);
    const qint64 memoryKb = effectiveMemoryKb(editable);
    if (memoryKb % (1024 * 1024) == 0) {
        memoryUnitCombo_->setCurrentText("GB");
        memorySpin_->setValue(double(memoryKb) / (1024.0 * 1024.0));
    } else if (memoryKb % 1024 == 0) {
        memoryUnitCombo_->setCurrentText("MB");
        memorySpin_->setValue(double(memoryKb) / 1024.0);
    } else {
        memoryUnitCombo_->setCurrentText("KB");
        memorySpin_->setValue(memoryKb);
    }
    const qint64 suggestedDiskMb = defaultDiskSizeMbForConfig(editable);
    if (suggestedDiskMb % 1024 == 0) {
        diskSizeUnitCombo_->setCurrentText("GB");
        diskSizeSpin_->setValue(double(suggestedDiskMb) / 1024.0);
    } else {
        diskSizeUnitCombo_->setCurrentText("MB");
        diskSizeSpin_->setValue(suggestedDiskMb);
    }
    acceleratorCombo_->setCurrentText(editable.accelerator.isEmpty() ? "auto" : editable.accelerator);
    const bool hasNetwork = !editable.networks.isEmpty();
    networkEnabledCheck_->setChecked(hasNetwork);
    NetworkConfig network;
    if (hasNetwork) network = editable.networks.first();
    networkModeCombo_->setCurrentText(network.mode.isEmpty() ? "user" : network.mode);
    networkModelCombo_->setCurrentText(network.model.isEmpty() ? "virtio-net-pci" : network.model);
    networkBridgeEdit_->setText(network.bridge);
    networkMacEdit_->setText(network.mac);
    networkForwardsEdit_->setText(network.hostForwards.join(","));
    audioEnabledCheck_->setChecked(editable.audio.enabled);
    audioDriverCombo_->setCurrentText(editable.audio.driver.isEmpty() ? "auto" : editable.audio.driver);
    audioModelCombo_->setCurrentText(editable.audio.model.isEmpty() ? "ich9-intel-hda" : editable.audio.model);
    graphicsCombo_->setCurrentText(editable.graphics.adapter.isEmpty()
                                       ? (editable.graphics.vga.isEmpty() ? "virtio-vga" : editable.graphics.vga)
                                       : editable.graphics.adapter);
    inputCombo_->setCurrentText(inputPresetFromConfig(editable));
    refreshGpuPassthroughChoices();
    gpuPassthroughCheck_->setChecked(editable.gpuPassthrough.enabled);
    keepVirtualDisplayCheck_->setChecked(editable.gpuPassthrough.keepVirtualDisplay);
    int gpuIndex = gpuPassthroughCombo_->findData(editable.gpuPassthrough.deviceId);
    if (gpuIndex < 0 && !editable.gpuPassthrough.name.isEmpty()) {
        gpuIndex = gpuPassthroughCombo_->findText(editable.gpuPassthrough.name, Qt::MatchContains);
    }
    if (gpuIndex >= 0) {
        gpuPassthroughCombo_->setCurrentIndex(gpuIndex);
    }
    openGlCheck_->setChecked(editable.graphics.openGl);
    autoResizeCheck_->setChecked(editable.graphics.autoResize);
    retinaCheck_->setChecked(editable.graphics.retina);
    tpmCheck_->setChecked(editable.tpm.enabled);
    tpmSocketEdit_->setText(editable.tpm.emulatorSocket);
    kernelEdit_->setText(editable.boot.kernelPath);
    initrdEdit_->setText(editable.boot.initrdPath);
    appendEdit_->setText(editable.boot.kernelAppend);
    systemSoftwareRomEdit_->setText(editable.systemSoftwareRomPath);
    systemSoftwareFloppyAEdit_->clear();
    systemSoftwareFloppyBEdit_->clear();
    for (const DiskConfig& disk : editable.disks) {
        if (disk.media.compare("floppy", Qt::CaseInsensitive) != 0) continue;
        if (disk.role.compare("floppy-b", Qt::CaseInsensitive) == 0) systemSoftwareFloppyBEdit_->setText(disk.path);
        else if (systemSoftwareFloppyAEdit_->text().isEmpty()) systemSoftwareFloppyAEdit_->setText(disk.path);
        else if (systemSoftwareFloppyBEdit_->text().isEmpty()) systemSoftwareFloppyBEdit_->setText(disk.path);
    }
    loadingEditor_ = false;
    const bool systemSoftware = isSystemSoftwareUi(editable.guestOs);
    networkEnabledCheck_->setEnabled(!systemSoftware);
    networkModeCombo_->setEnabled(!systemSoftware);
    networkModelCombo_->setEnabled(!systemSoftware);
    networkBridgeEdit_->setEnabled(!systemSoftware);
    networkMacEdit_->setEnabled(!systemSoftware);
    networkForwardsEdit_->setEnabled(!systemSoftware);
    audioEnabledCheck_->setEnabled(!systemSoftware);
    audioDriverCombo_->setEnabled(!systemSoftware);
    audioModelCombo_->setEnabled(!systemSoftware);
    if (qemuMediaControls_) qemuMediaControls_->setVisible(!systemSoftware);
    if (systemSoftwareFloppyWidget_) systemSoftwareFloppyWidget_->setVisible(systemSoftware);
    if (mediaList_) mediaList_->setVisible(!systemSoftware);
    refreshMediaList();
    refreshPreview();
}

VmConfig VirtualWorldWindow::editorToVm() const {
    VmConfig config = currentIndex_ >= 0 && currentIndex_ < vms_.size() ? vms_[currentIndex_] : VmConfig{};
    config.name = nameEdit_->text().trimmed().isEmpty() ? "VirtualWorld VM" : nameEdit_->text().trimmed();
    config.storageDir = storageEdit_->text().trimmed();
    if (config.storageDir.isEmpty()) {
        config.storageDir = defaultStorageDirForVm(config.name);
    }
    config.guestOs = guestOsCombo_->currentText().trimmed().isEmpty() ? "generic" : guestOsCombo_->currentText().trimmed();
    config.backend = isSystemSoftwareUi(config.guestOs) ? "system-software" : "qemu";
    config.systemSoftwareVersion = isSystemSoftwareUi(config.guestOs)
                                       ? systemSoftwareVersionFromUi(config.guestOs)
                                       : QString();
    config.systemSoftwareRomPath = isSystemSoftwareUi(config.guestOs)
                                       ? systemSoftwareRomEdit_->text().trimmed()
                                       : QString();
    config.systemSoftwareMachine = isSystemSoftwareUi(config.guestOs)
                                       ? systemSoftwareMachineFromUi(config.guestOs)
                                       : QString();
    config.systemSoftwareRomType = isSystemSoftwareUi(config.guestOs)
                                       ? systemSoftwareRomTypeFromUi(config.guestOs)
                                       : QString();
    config.architecture = supportedArchitectureOrFallback(archCombo_->currentText());
    config.machine = machineCombo_->currentText();
    config.cpuModel = cpuEdit_->text().trimmed().isEmpty() ? "max" : cpuEdit_->text().trimmed();
    config.cpuCores = int(coresSpin_->value());
    setMemoryKb(config, memoryValueKb());
    config.accelerator = acceleratorCombo_->currentText();
    if (isSystemSoftwareUi(config.guestOs)) {
        config.networks.clear();
        config.audio = AudioConfig{};
    } else {
        if (networkEnabledCheck_->isChecked()) {
            NetworkConfig network;
            if (!config.networks.isEmpty()) network = config.networks.first();
            network.mode = networkModeCombo_->currentText().trimmed().isEmpty() ? "user" : networkModeCombo_->currentText().trimmed();
            network.model = networkModelCombo_->currentText().trimmed().isEmpty() ? "virtio-net-pci" : networkModelCombo_->currentText().trimmed();
            network.bridge = networkBridgeEdit_->text().trimmed();
            network.mac = networkMacEdit_->text().trimmed();
            network.hostForwards.clear();
            for (const QString& item : networkForwardsEdit_->text().split(',', Qt::SkipEmptyParts)) {
                if (!item.trimmed().isEmpty()) network.hostForwards << item.trimmed();
            }
            if (config.networks.isEmpty()) config.networks.append(network);
            else config.networks[0] = network;
        } else {
            config.networks.clear();
        }
        config.audio.enabled = audioEnabledCheck_->isChecked();
        config.audio.driver = audioDriverCombo_->currentText().trimmed().isEmpty() ? "auto" : audioDriverCombo_->currentText().trimmed();
        config.audio.model = audioModelCombo_->currentText().trimmed();
        config.audio.id = config.audio.id.trimmed().isEmpty() ? "audio0" : config.audio.id.trimmed();
    }
    config.graphics.adapter = graphicsCombo_->currentText();
    if (normalizeArchitecture(config.architecture) == "ppc"
        || normalizeArchitecture(config.architecture) == "ppc64") {
        const QString selectedGraphics = config.graphics.adapter.trimmed().toLower();
        if (selectedGraphics == "std" || selectedGraphics == "vga"
            || selectedGraphics == "cirrus" || selectedGraphics == "vmware"
            || selectedGraphics == "qxl" || selectedGraphics == "virtio") {
            config.graphics.adapter.clear();
            config.graphics.vga = selectedGraphics == "cirrus" ? "cirrus" : "std";
        }
    }
    applyInputPreset(config, inputCombo_->currentText());
    const QString selectedGpuId = gpuPassthroughCombo_->currentData().toString();
    HostGpuInfo selectedGpu = gpuById(selectedGpuId);
    applyGpuSelection(config, selectedGpu, gpuPassthroughCheck_->isChecked(), keepVirtualDisplayCheck_->isChecked());
    config.graphics.openGl = openGlCheck_->isChecked();
    config.graphics.autoResize = autoResizeCheck_->isChecked();
    config.graphics.dynamicResolution = autoResizeCheck_->isChecked();
    config.graphics.retina = retinaCheck_->isChecked();
    config.tpm.enabled = tpmCheck_->isChecked();
    config.tpm.emulatorSocket = tpmSocketEdit_->text().trimmed();
    config.boot.kernelPath = kernelEdit_->text().trimmed();
    config.boot.initrdPath = initrdEdit_->text().trimmed();
    config.boot.kernelAppend = appendEdit_->text().trimmed();
    config.sourceFormat = "virtualworld";
    if (isSystemSoftwareUi(config.guestOs)) {
        config.disks.clear();
        const auto appendFloppy = [&config](const QString& path, const QString& role) {
            if (path.trimmed().isEmpty()) return;
            DiskConfig disk;
            disk.path = QFileInfo(path.trimmed()).absoluteFilePath();
            disk.interfaceName = "floppy";
            disk.format = QFileInfo(disk.path).suffix().toLower();
            disk.media = "floppy";
            disk.role = role;
            config.disks.append(disk);
        };
        appendFloppy(systemSoftwareFloppyAEdit_->text(), "floppy-a");
        appendFloppy(systemSoftwareFloppyBEdit_->text(), "floppy-b");
        config.cdroms.clear();
    }
    normalizeBootMediaOrder(config);
    return config;
}

void VirtualWorldWindow::refreshPreview() {
    if (loadingEditor_) return;
    if (currentIndex_ < 0 || currentIndex_ >= vms_.size()) {
        setEditorAvailable(false);
        return;
    }
    setEditorAvailable(true);
    vms_[currentIndex_] = editorToVm();
    refreshVmList();
    const bool blocked = vmList_->blockSignals(true);
    vmList_->setCurrentRow(currentIndex_);
    vmList_->blockSignals(blocked);
    if (isSystemSoftwareConfig(vms_[currentIndex_])) {
        SystemSoftwareEngine engine;
        commandPreview_->setPlainText(engine.renderCommand(vms_[currentIndex_]));
        QVector<ValidationIssue> issues;
        const RuntimePath runtime = engine.detect(vms_[currentIndex_]);
        if (runtime.available) {
            issues.append({"info", "system_software_runtime",
                           QString("System Software %1 运行时：%2（%3）")
                               .arg(vms_[currentIndex_].systemSoftwareVersion, runtime.executable, runtime.source)});
        } else {
            issues.append({"error", "system_software_runtime_missing",
                           QString("未找到 System Software 运行时，请放入 %1 或设置 VIRTUALWORLD_SYSTEM_SOFTWARE_RUNTIME。")
                               .arg(runtime.expectedPath)});
        }
        if (!QFileInfo::exists(vms_[currentIndex_].systemSoftwareRomPath)) {
            issues.append({"error", "system_software_rom_missing", "未选择有效的 System Software ROM。"});
        }
        appendIssueText(issues);
        return;
    }
    const ConversionResult converted = convertToQemu(vms_[currentIndex_], detectHostProfile().platform);
    commandPreview_->setPlainText(converted.text);
    QVector<ValidationIssue> issues = converted.issues;
    const QemuEngineInfo runtime = manager_.detectRuntime(vms_[currentIndex_].architecture);
    if (runtime.systemEmulator.available) {
        issues.append({"info",
                       "qemu_runtime_source",
                       QString("QEMU 后端：%1（%2）").arg(runtime.systemEmulator.executable, runtime.systemEmulator.source)});
    } else {
        issues.append({"error",
                       "qemu_runtime_missing",
                       QString("未找到可运行的 QEMU 后端。请放入 %1 或安装 qemu-system-* 到 PATH。").arg(runtime.systemEmulator.expectedPath)});
    }
    if (runtime.imageTool.available) {
        issues.append({"info",
                       "qemu_img_source",
                       QString("qemu-img：%1（%2）").arg(runtime.imageTool.executable, runtime.imageTool.source)});
    } else {
        issues.append({"error",
                       "qemu_img_missing",
                       QString("未找到可运行的 qemu-img。请放入 %1 或安装 qemu-img 到 PATH。").arg(runtime.imageTool.expectedPath)});
    }
    appendIssueText(issues);
}

void VirtualWorldWindow::refreshMediaList() {
    const QSignalBlocker blocker(mediaList_->model());
    mediaList_->clear();
    if (currentIndex_ < 0 || currentIndex_ >= vms_.size()) return;
    const VmConfig& config = vms_[currentIndex_];
    int bootOrder = 1;
    auto addDiskItem = [this](const QString& text, const DiskConfig& disk) {
        auto* item = new QListWidgetItem(text);
        item->setData(MediaTypeRole, disk.media);
        item->setData(MediaPathRole, disk.path);
        item->setData(MediaInterfaceRole, disk.interfaceName);
        item->setData(MediaFormatRole, disk.format);
        item->setData(MediaReadonlyRole, disk.readonly);
        item->setData(MediaSizeRole, disk.sizeMb);
        item->setData(MediaRoleRole, disk.role);
        item->setFlags(item->flags() | Qt::ItemIsDragEnabled | Qt::ItemIsDropEnabled | Qt::ItemIsEnabled | Qt::ItemIsSelectable);
        mediaList_->addItem(item);
    };
    for (const DiskConfig& disk : config.disks) {
        const QString mediaName = disk.media == "floppy"
                                      ? "Floppy"
                                      : (disk.media == "cdrom" ? "CD/DVD" : "Disk");
        const QString protectedText = isRecoveryGuardDisk(disk) ? " · 受保护恢复盘（只读）" : QString();
        addDiskItem(QString("启动 %1 · %2 [%3]: %4%5")
                        .arg(QString::number(bootOrder++),
                             mediaName,
                             disk.interfaceName,
                             disk.path,
                             protectedText),
                    disk);
    }
    for (const QString& cdrom : config.cdroms) {
        DiskConfig disk;
        disk.path = cdrom;
        disk.media = "cdrom";
        disk.interfaceName = "cdrom";
        disk.format = QFileInfo(cdrom).suffix().toLower();
        disk.readonly = true;
        addDiskItem(QString("启动 %1 · CD/DVD: %2").arg(QString::number(bootOrder++), cdrom), disk);
    }
}

void VirtualWorldWindow::applyMediaListOrder() {
    if (loadingEditor_ || currentIndex_ < 0 || currentIndex_ >= vms_.size()) {
        return;
    }
    VmConfig config = editorToVm();
    config.disks.clear();
    config.cdroms.clear();
    for (int i = 0; i < mediaList_->count(); ++i) {
        QListWidgetItem* item = mediaList_->item(i);
        if (!item) {
            continue;
        }
        DiskConfig disk;
        disk.media = item->data(MediaTypeRole).toString();
        disk.path = item->data(MediaPathRole).toString();
        disk.interfaceName = item->data(MediaInterfaceRole).toString();
        disk.format = item->data(MediaFormatRole).toString();
        disk.readonly = item->data(MediaReadonlyRole).toBool();
        disk.sizeMb = item->data(MediaSizeRole).toLongLong();
        disk.role = item->data(MediaRoleRole).toString();
        if (disk.path.trimmed().isEmpty()) {
            continue;
        }
        if (disk.media == "cdrom" && disk.interfaceName == "cdrom") {
            disk.interfaceName = "ide";
        }
        config.disks.append(disk);
    }
    const VmConfig previous = editorToVm();
    for (const DiskConfig& existing : previous.disks) {
        if (!isRecoveryGuardDisk(existing)) {
            continue;
        }
        bool present = false;
        for (const DiskConfig& disk : config.disks) {
            if (isRecoveryGuardDisk(disk)) {
                present = true;
                break;
            }
        }
        if (!present) {
            config.disks.append(existing);
        }
        break;
    }
    normalizeBootMediaOrder(config);
    vms_[currentIndex_] = config;
    persistCurrentVm();
    refreshMediaList();
    refreshPreview();
}

void VirtualWorldWindow::removeSelectedMedia() {
    if (loadingEditor_ || !mediaList_) {
        return;
    }

    const int row = mediaList_->currentRow();
    if (row < 0) {
        return;
    }

    QListWidgetItem* item = mediaList_->item(row);
    if (item && isRecoveryGuardDisk(DiskConfig{
                    item->data(MediaPathRole).toString(),
                    item->data(MediaInterfaceRole).toString(),
                    item->data(MediaFormatRole).toString(),
                    item->data(MediaReadonlyRole).toBool(),
                    item->data(MediaTypeRole).toString(),
                    item->data(MediaSizeRole).toLongLong(),
                    item->data(MediaRoleRole).toString()})) {
        showMessage("恢复保护盘", "Windows 恢复保护盘由 VirtualWorld 自动维护，不能从媒体列表中移除。");
        return;
    }
    delete mediaList_->takeItem(row);
    applyMediaListOrder();
}

void VirtualWorldWindow::appendIssueText(const QVector<ValidationIssue>& issues) {
    QStringList lines;
    for (const ValidationIssue& issue : issues) {
        lines << QString("[%1] %2").arg(issue.level, issue.message);
    }
    issueText_->setPlainText(lines.join('\n'));
}

void VirtualWorldWindow::showMessage(const QString& title, const QString& message) {
    QMessageBox::information(this, title, message);
}

bool VirtualWorldWindow::prepareStorageDirectory(QString* path, QString* error) const {
    if (!path) {
        if (error) {
            *error = "虚拟机保存位置无效。";
        }
        return false;
    }

    const QString requested = path->trimmed();
    if (requested.isEmpty()) {
        if (error) {
            *error = "虚拟机保存位置不能为空。";
        }
        return false;
    }

    const QFileInfo requestedInfo(requested);
    if (!requestedInfo.isAbsolute()) {
        if (error) {
            *error = "虚拟机保存位置必须是绝对路径，请重新选择外置卷中的文件夹。";
        }
        return false;
    }

    const QString absolutePath = QDir::cleanPath(requestedInfo.absoluteFilePath());
    QDir directory(absolutePath);
    if (!directory.exists() && !directory.mkpath(".")) {
        if (error) {
            *error = QString("无法创建虚拟机保存文件夹：%1\n请确认外置磁盘已挂载且当前用户拥有写入权限。")
                         .arg(absolutePath);
        }
        return false;
    }

    const QFileInfo directoryInfo(absolutePath);
    if (!directoryInfo.exists() || !directoryInfo.isDir()) {
        if (error) {
            *error = "虚拟机保存位置不是文件夹：" + absolutePath;
        }
        return false;
    }

    // On macOS, the sealed system volume is reported as read-only for some
    // paths under the writable Data volume. Do not classify the default
    // Application Support directory as an external disk. Only apply the
    // removable-volume checks when the user explicitly selected /Volumes.
    const QString nativePath = QDir::toNativeSeparators(absolutePath);
#if defined(Q_OS_MACOS)
    const bool explicitExternalVolume = nativePath == "/Volumes"
        || nativePath.startsWith("/Volumes/");
#else
    const bool explicitExternalVolume = false;
#endif
    if (explicitExternalVolume) {
        const QStorageInfo volume(absolutePath);
        if (!volume.isValid() || !volume.isReady()) {
            if (error) {
                *error = QString("外置磁盘尚未准备好：%1\n请重新插入或挂载该磁盘后再试。")
                             .arg(absolutePath);
            }
            return false;
        }
        if (volume.isReadOnly()) {
            if (error) {
                *error = QString("外置磁盘是只读的，无法创建虚拟机：%1\n请在访达中确认磁盘权限或改用可写卷。")
                             .arg(volume.rootPath());
            }
            return false;
        }
    }

    // QStorageInfo can report a writable volume even when ACLs, ownership,
    // or removable-volume permissions prevent this specific directory from
    // being written. Probe the actual target before creating VM files.
    const QString probePath = directory.filePath(
        ".virtualworld-write-test-" + QUuid::createUuid().toString(QUuid::WithoutBraces));
    QFile probe(probePath);
    if (!probe.open(QIODevice::WriteOnly | QIODevice::NewOnly)) {
        if (error) {
            *error = explicitExternalVolume
                ? QString("无法写入外置卷中的虚拟机保存位置：%1\n请在系统设置中允许 VirtualWorld 访问该外置磁盘。")
                      .arg(absolutePath)
                : QString("无法写入虚拟机保存位置：%1\n请确认当前用户拥有该目录的写入权限。")
                      .arg(absolutePath);
        }
        return false;
    }
    const QByteArray marker("VirtualWorld storage probe\n");
    const bool wrote = probe.write(marker) == marker.size();
    probe.close();
    const bool removed = QFile::remove(probePath);
    if (!wrote || !removed) {
        QFile::remove(probePath);
        if (error) {
            *error = "虚拟机保存位置无法完成写入测试：" + absolutePath;
        }
        return false;
    }

    *path = directoryInfo.absoluteFilePath();
    return true;
}

bool VirtualWorldWindow::ensureWritableDiskImages(VmConfig& config, QString* error) const {
    const bool classicPpc = isClassicMacOsUi(config.guestOs)
                            && normalizeArchitecture(config.architecture) == "ppc"
                            && machineBase(config.machine) == "mac99";
    QString storageDir = config.storageDir.trimmed();
    for (DiskConfig& disk : config.disks) {
        if (disk.media != "disk" || disk.readonly || disk.path.trimmed().isEmpty()) {
            continue;
        }

        if (QFileInfo(disk.path).isRelative()) {
            if (storageDir.isEmpty()) {
                storageDir = defaultStorageDirForVm(config.name);
                config.storageDir = storageDir;
            }
            QDir storage(storageDir);
            if (!storage.exists() && !storage.mkpath(".")) {
                if (error) {
                    *error = "无法创建虚拟机保存文件夹：" + storageDir;
                }
                return false;
            }
            disk.path = storage.filePath(disk.path);
        }

        const QFileInfo diskInfo(disk.path);
        if (diskInfo.exists()) {
            if (!diskInfo.isFile()) {
                if (error) {
                    *error = "虚拟硬盘路径不是普通文件：" + disk.path;
                }
                return false;
            }
            const QString existingFormat = disk.format.trimmed().toLower();
            if (classicPpc && (existingFormat.isEmpty() || existingFormat == "qcow2")) {
                bool blank = false;
                QString inspectionError;
                if (manager_.isDiskBlank(disk.path, &blank, &inspectionError) && blank) {
                    const QString token = QUuid::createUuid().toString(QUuid::WithoutBraces);
                    const QString formattedPath = disk.path + ".virtualworld-format-" + token + ".qcow2";
                    QString formatError;
                    const qint64 diskSizeMb = disk.sizeMb > 0 ? disk.sizeMb : defaultDiskSizeMbForConfig(config);
                    if (!manager_.createClassicMacDisk(formattedPath, diskSizeMb, &formatError)) {
                        if (error) {
                            *error = "检测到未初始化的 PPC 系统盘，但无法修复为 Apple 分区表/HFS+：\n" + formatError;
                        }
                        return false;
                    }
                    const QString backupPath = disk.path + ".virtualworld-unformatted-" + token + ".qcow2";
                    if (!QFile::rename(disk.path, backupPath)) {
                        QFile::remove(formattedPath);
                        if (error) {
                            *error = "无法替换未初始化的 PPC 系统盘：" + disk.path;
                        }
                        return false;
                    }
                    if (!QFile::rename(formattedPath, disk.path)) {
                        QFile::rename(backupPath, disk.path);
                        QFile::remove(formattedPath);
                        if (error) {
                            *error = "无法安装已格式化的 PPC 系统盘：" + disk.path;
                        }
                        return false;
                    }
                    QFile::remove(backupPath);
                }
            }
            continue;
        }

        const QString format = disk.format.trimmed().toLower();
        if (!format.isEmpty() && format != "qcow2") {
            if (error) {
                *error = QString("找不到硬盘镜像：%1\n自动创建仅适用于 qcow2 磁盘，请重新导入此 %2 镜像。")
                             .arg(disk.path, format);
            }
            return false;
        }

        disk.format = "qcow2";
        disk.sizeMb = disk.sizeMb > 0 ? disk.sizeMb : defaultDiskSizeMbForConfig(config);
        QString createError;
        const bool created = classicPpc
                                 ? manager_.createClassicMacDisk(disk.path, disk.sizeMb, &createError)
                                 : manager_.createDisk(disk.path, disk.sizeMb, &createError);
        if (!created) {
            if (error) {
                *error = "无法创建虚拟硬盘：" + disk.path + "\n" + createError;
            }
            return false;
        }

        const QFileInfo createdDisk(disk.path);
        if (!createdDisk.exists() || !createdDisk.isFile()) {
            if (error) {
                *error = "qemu-img 已返回成功，但未生成硬盘文件：" + disk.path;
            }
            return false;
        }
    }
    return true;
}

bool VirtualWorldWindow::ensureRecoveryGuardDisk(VmConfig& config, QString* error) const {
    if (isSystemSoftwareConfig(config)) {
        return true;
    }
    const bool windowsGuest = isSupportedWindowsGuest(config);
    const bool linuxGuest = isSupportedLinuxGuest(config);
    const bool macosGuest = isSupportedMacosGuest(config);
    if (!windowsGuest && !linuxGuest && !macosGuest) {
        return true;
    }

    config.recoveryGuard.enabled = true;
    QString storageDir = config.storageDir.trimmed();
    if (storageDir.isEmpty()) {
        storageDir = defaultStorageDirForVm(config.name);
        config.storageDir = storageDir;
    }
    QDir storage(storageDir);
    if (!storage.exists() && !storage.mkpath(".")) {
        if (error) {
            *error = "无法创建恢复保护盘目录：" + storageDir;
        }
        return false;
    }

    int guardIndex = -1;
    for (int index = 0; index < config.disks.size(); ++index) {
        if (!isRecoveryGuardDisk(config.disks.at(index))) {
            continue;
        }
        if (guardIndex < 0) {
            guardIndex = index;
        } else {
            config.disks.removeAt(index);
            --index;
        }
    }

    DiskConfig guard;
    if (guardIndex >= 0) {
        guard = config.disks.at(guardIndex);
    }
    if (windowsGuest) {
        guard.path = sharedRecoveryGuardDiskPath();
    } else if (linuxGuest) {
        guard.path = sharedLinuxRecoveryGuardDiskPath();
    } else {
        guard.path = sharedMacosRecoveryGuardDiskPath();
    }
    const QString arch = normalizeArchitecture(config.architecture);
    guard.interfaceName = (arch == "x86_64" || arch == "i386") ? "ide" : "virtio";
    guard.format = "qcow2";
    guard.readonly = true;
    guard.media = "disk";
    guard.sizeMb = 64;
    if (windowsGuest) {
        guard.role = "recovery-guard";
    } else if (linuxGuest) {
        guard.role = "linux-recovery-guard";
    } else {
        guard.role = "macos-recovery-guard";
    }

    bool trusted = false;
    if (windowsGuest) {
        trusted = ensureTrustedSharedRecoveryGuardDisk(error);
    } else if (linuxGuest) {
        trusted = ensureTrustedSharedLinuxRecoveryGuardDisk(error);
    } else {
        trusted = ensureTrustedSharedMacosRecoveryGuardDisk(error);
    }
    if (!trusted) {
        return false;
    }

    if (guardIndex >= 0) {
        config.disks[guardIndex] = guard;
        config.disks.move(guardIndex, config.disks.size() - 1);
    } else {
        config.disks.append(guard);
    }
    return true;
}

void VirtualWorldWindow::newVm() {
    VmConfig config = createVmWithWizard();
    if (config.name.isEmpty()) return;
    const bool downloadAfterCreate = config.extraArgs.removeAll("__virtualworld_download_after_create__") > 0;
    const bool importAfterCreate = config.extraArgs.removeAll("__virtualworld_import_after_create__") > 0;
    QString selectedInstallImage;
    for (int i = config.extraArgs.size() - 1; i >= 0; --i) {
        const QString prefix = "__virtualworld_install_image__=";
        if (config.extraArgs.at(i).startsWith(prefix)) {
            selectedInstallImage = config.extraArgs.takeAt(i).mid(prefix.size()).trimmed();
            break;
        }
    }
    if (config.storageDir.trimmed().isEmpty()) {
        config.storageDir = defaultStorageDirForVm(config.name);
    }
    QString storageError;
    if (!prepareStorageDirectory(&config.storageDir, &storageError)) {
        showMessage("创建失败", storageError);
        return;
    }
    QDir dir(config.storageDir);
    if (isSystemSoftwareConfig(config)) {
        QString romError;
        if (!ensureSystemSoftwareRom(config, &romError)) {
            showMessage("创建失败", romError);
            return;
        }
    }
    if (!selectedInstallImage.isEmpty()) {
        prependInstallImage(config, selectedInstallImage, diskInterfaceCombo_->currentText());
    } else if (importAfterCreate) {
        const QString path = QFileDialog::getOpenFileName(
            this,
            "选择启动镜像",
            QString(),
            bootImageFilter());
        if (path.isEmpty()) {
            const auto answer = QMessageBox::warning(
                this,
                "缺少启动镜像",
                "还没有选择安装/启动镜像。保存后虚拟机可能只能进入 TianoCore 或空白启动界面。\n\n是否仍然创建？",
                QMessageBox::Yes | QMessageBox::No,
                QMessageBox::No);
            if (answer != QMessageBox::Yes) {
                return;
            }
        } else {
            prependInstallImage(config, path, diskInterfaceCombo_->currentText());
        }
    } else if (!downloadAfterCreate && guestNeedsInstallImageAtCreate(config) && config.cdroms.isEmpty()) {
        bool hasBootMedia = false;
        for (const DiskConfig& disk : config.disks) {
            if ((disk.media == "cdrom" || disk.media == "floppy") && !disk.path.trimmed().isEmpty()) {
                hasBootMedia = true;
                break;
            }
        }
        if (!hasBootMedia) {
            const auto answer = QMessageBox::warning(
                this,
                "缺少启动镜像",
                "当前配置没有安装/启动镜像。建议先选择“创建后手动导入镜像”或“下载/获取安装镜像”。\n\n是否继续保存空启动配置？",
                QMessageBox::Yes | QMessageBox::No,
                QMessageBox::No);
            if (answer != QMessageBox::Yes) {
                return;
            }
        }
    }
    if (!isSystemSoftwareConfig(config)) {
        QString diskError;
        if (!ensureRecoveryGuardDisk(config, &diskError)) {
            showMessage("创建恢复保护盘失败", diskError);
            return;
        }
        if (!ensureWritableDiskImages(config, &diskError)) {
            showMessage("创建硬盘失败", diskError);
            return;
        }
    }
    QString saveError;
    if (!manager_.saveConfig(dir.filePath("config.json"), config, &saveError)) {
        showMessage("保存失败", saveError);
        return;
    }
    vms_.append(config);
    saveLibraryIndex();
    refreshVmList();
    vmList_->setCurrentRow(vms_.size() - 1);
    setEditorAvailable(true);
    if (downloadAfterCreate) {
        downloadImage();
    }
}

void VirtualWorldWindow::deleteVm() {
    if (currentIndex_ < 0 || currentIndex_ >= vms_.size()) {
        return;
    }
    const VmConfig config = vms_[currentIndex_];
    const QString storageDir = config.storageDir.trimmed();
    QMessageBox box(this);
    box.setWindowTitle("删除虚拟机");
    box.setText(QString("要删除“%1”吗？").arg(config.name));
    box.setInformativeText("可以只从列表移除，也可以同时删除虚拟机文件夹。删除文件夹会移除其中的配置和虚拟硬盘。");
    auto* removeOnly = box.addButton("仅从列表移除", QMessageBox::AcceptRole);
    auto* deleteFiles = box.addButton("同时删除文件夹", QMessageBox::DestructiveRole);
    box.addButton("取消", QMessageBox::RejectRole);
    box.exec();
    if (box.clickedButton() == nullptr || box.buttonRole(box.clickedButton()) == QMessageBox::RejectRole) {
        return;
    }
    const bool shouldDeleteFiles = box.clickedButton() == deleteFiles;
    if (shouldDeleteFiles) {
        if (storageDir.isEmpty()) {
            showMessage("删除失败", "虚拟机没有保存文件夹记录。");
            return;
        }
        const auto confirm = QMessageBox::warning(this,
                                                  "确认删除文件",
                                                  QString("确定删除这个文件夹吗？\n%1").arg(storageDir),
                                                  QMessageBox::Yes | QMessageBox::No,
                                                  QMessageBox::No);
        if (confirm != QMessageBox::Yes) {
            return;
        }
        QDir dir(storageDir);
        if (dir.exists() && !dir.removeRecursively()) {
            showMessage("删除失败", "无法删除虚拟机文件夹：" + storageDir);
            return;
        }
    } else if (box.clickedButton() != removeOnly) {
        return;
    }
    const int removedRow = currentIndex_;
    vms_.removeAt(currentIndex_);
    currentIndex_ = -1;
    if (shouldDeleteFiles) {
        const bool hasSupportedWindowsVm = std::any_of(
            vms_.cbegin(),
            vms_.cend(),
            [](const VmConfig& vm) { return isSupportedWindowsGuest(vm); });
        const bool hasSupportedLinuxVm = std::any_of(
            vms_.cbegin(),
            vms_.cend(),
            [](const VmConfig& vm) { return isSupportedLinuxGuest(vm); });
        if (!hasSupportedWindowsVm) {
            const QString sharedGuardPath = sharedRecoveryGuardDiskPath();
            const QString sharedGuardManifest = sharedRecoveryGuardManifestPath();
            if (QFileInfo::exists(sharedGuardPath) && !QFile::remove(sharedGuardPath)) {
                showMessage("删除恢复盘失败",
                            "虚拟机已删除，但无法删除共享恢复盘：\n" + sharedGuardPath);
            }
            if (QFileInfo::exists(sharedGuardManifest) && !QFile::remove(sharedGuardManifest)) {
                showMessage("删除清单失败",
                            "无法删除共享恢复盘清单：\n" + sharedGuardManifest);
            }
        }
        if (!hasSupportedLinuxVm) {
            const QString sharedLinuxPath = sharedLinuxRecoveryGuardDiskPath();
            const QString sharedLinuxManifest = sharedLinuxRecoveryGuardManifestPath();
            if (QFileInfo::exists(sharedLinuxPath) && !QFile::remove(sharedLinuxPath)) {
                showMessage("删除恢复盘失败",
                            "虚拟机已删除，但无法删除共享 Linux Support Disk：\n" + sharedLinuxPath);
            }
            if (QFileInfo::exists(sharedLinuxManifest) && !QFile::remove(sharedLinuxManifest)) {
                showMessage("删除清单失败",
                            "无法删除共享 Linux Support Disk 清单：\n" + sharedLinuxManifest);
            }
        }
        const bool hasSupportedMacosVm = std::any_of(
            vms_.cbegin(),
            vms_.cend(),
            [](const VmConfig& vm) { return isSupportedMacosGuest(vm); });
        if (!hasSupportedMacosVm) {
            const QString sharedMacosPath = sharedMacosRecoveryGuardDiskPath();
            const QString sharedMacosManifest = sharedMacosRecoveryGuardManifestPath();
            if (QFileInfo::exists(sharedMacosPath) && !QFile::remove(sharedMacosPath)) {
                showMessage("删除恢复盘失败",
                            "虚拟机已删除，但无法删除共享 macOS Support Disk：\n" + sharedMacosPath);
            }
            if (QFileInfo::exists(sharedMacosManifest) && !QFile::remove(sharedMacosManifest)) {
                showMessage("删除清单失败",
                            "无法删除共享 macOS Support Disk 清单：\n" + sharedMacosManifest);
            }
        }
    }
    saveLibraryIndex();
    refreshVmList();
    if (!vms_.isEmpty()) {
        vmList_->setCurrentRow(qMin(removedRow, vms_.size() - 1));
        setEditorAvailable(true);
    } else {
        showEmptyLibrary();
    }
}

void VirtualWorldWindow::importQemu() {
    const QString path = QFileDialog::getOpenFileName(this, "导入 QEMU 命令或脚本");
    if (path.isEmpty()) return;
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) {
        showMessage("导入失败", file.errorString());
        return;
    }
    VmConfig config = parseQemuCommand(QString::fromUtf8(file.readAll()));
    QString recoveryError;
    if (!ensureRecoveryGuardDisk(config, &recoveryError)) {
        showMessage("导入 Support Disk 失败", recoveryError);
        return;
    }
    vms_.append(config);
    refreshVmList();
    vmList_->setCurrentRow(vms_.size() - 1);
    setEditorAvailable(true);
}

void VirtualWorldWindow::importUtm() {
    const QString path = QFileDialog::getExistingDirectory(this, "选择 .utm 包，或取消后选择 config.plist");
    QString source = path;
    if (source.isEmpty()) {
        source = QFileDialog::getOpenFileName(this, "选择 UTM config.plist", QString(), "plist (*.plist);;All files (*)");
    }
    if (source.isEmpty()) return;
    VmConfig config = parseUtmSource(source);
    QString recoveryError;
    if (!ensureRecoveryGuardDisk(config, &recoveryError)) {
        showMessage("导入 Support Disk 失败", recoveryError);
        return;
    }
    vms_.append(config);
    refreshVmList();
    vmList_->setCurrentRow(vms_.size() - 1);
    setEditorAvailable(true);
}

void VirtualWorldWindow::importVirtualWorld() {
    const QString path = QFileDialog::getOpenFileName(this, "导入 VirtualWorld 配置", QString(), "VirtualWorld JSON (*.json);;All files (*)");
    if (path.isEmpty()) return;
    VmConfig config = manager_.loadConfig(path);
    QString recoveryError;
    if (!ensureRecoveryGuardDisk(config, &recoveryError)) {
        showMessage("导入 Support Disk 失败", recoveryError);
        return;
    }
    vms_.append(config);
    refreshVmList();
    vmList_->setCurrentRow(vms_.size() - 1);
    setEditorAvailable(true);
}

void VirtualWorldWindow::saveVirtualWorld() {
    if (currentIndex_ < 0) return;
    VmConfig config = editorToVm();
    const QString initialDir = config.storageDir.trimmed().isEmpty() ? defaultStorageDirForVm(config.name) : config.storageDir.trimmed();
    QDir(initialDir).mkpath(".");
    const QString path = QFileDialog::getSaveFileName(this, "保存 VirtualWorld 配置", QDir(initialDir).filePath("config.json"), "VirtualWorld JSON (*.json)");
    if (path.isEmpty()) return;
    QString error;
    config.storageDir = QFileInfo(path).dir().absolutePath();
    if (!prepareStorageDirectory(&config.storageDir, &error)) {
        showMessage("保存失败", error);
        return;
    }
    if (!ensureSystemSoftwareRom(config, &error)) {
        showMessage("保存失败", error);
        return;
    }
    if (!ensureRecoveryGuardDisk(config, &error)) {
        showMessage("保存 Support Disk 失败", error);
        return;
    }
    if (!manager_.saveConfig(path, config, &error)) {
        showMessage("保存失败", error);
        return;
    }
    manager_.saveConfig(QDir(config.storageDir).filePath("config.json"), config, nullptr);
    vms_[currentIndex_] = config;
    saveLibraryIndex();
    loadVmToEditor(config);
    showMessage("保存完成", path);
}

void VirtualWorldWindow::exportQemu() {
    if (currentIndex_ < 0) return;
    if (isSystemSoftwareConfig(vms_[currentIndex_])) {
        showMessage("无法导出 QEMU", "System Software 使用独立的 Mini vMac 兼容运行时，不会生成 QEMU 命令。");
        return;
    }
    const QString path = QFileDialog::getSaveFileName(this, "导出 QEMU 脚本", vms_[currentIndex_].name + ".sh", "Shell (*.sh);;All files (*)");
    if (path.isEmpty()) return;
    QFile file(path);
    if (!file.open(QIODevice::WriteOnly | QIODevice::Text | QIODevice::Truncate)) {
        showMessage("导出失败", file.errorString());
        return;
    }
    file.write(manager_.renderQemuCommand(editorToVm()).toUtf8());
    showMessage("导出完成", path);
}

void VirtualWorldWindow::exportUtm() {
    if (currentIndex_ < 0) return;
    if (isSystemSoftwareConfig(vms_[currentIndex_])) {
        showMessage("无法导出 UTM", "System Software 后端不属于 QEMU 或 Virtualization.framework 配置。");
        return;
    }
    const QString path = QFileDialog::getSaveFileName(this, "导出 UTM 包", vms_[currentIndex_].name + ".utm", "UTM Package (*.utm)");
    if (path.isEmpty()) return;
    QVector<ValidationIssue> issues;
    const QString plist = renderUtmPlist(editorToVm(), QString(), &issues);
    QString error;
    if (!writeUtmPackage(path, plist, &error)) {
        showMessage("导出失败", error);
        return;
    }
    appendIssueText(issues);
    showMessage("导出完成", path + "\n包内文件名为 config.plist。替换旧 UTM 配置前请备份原文件。");
}

void VirtualWorldWindow::startVm() {
    if (currentIndex_ < 0) return;
    VmConfig config = editorToVm();
    for (auto it = vmWindows_.begin(); it != vmWindows_.end();) {
        if (!*it || !(*it)->isRunning()) {
            it = vmWindows_.erase(it);
        } else {
            ++it;
        }
    }
    if (isSystemSoftwareConfig(config)) {
        for (const QPointer<VirtualMachineWindow>& runningWindow : vmWindows_) {
            if (runningWindow && runningWindow->isRunning() && runningWindow->matchesConfig(config)) {
                runningWindow->activateRuntimeWindow();
                return;
            }
        }
    }
    if (!isSystemSoftwareConfig(config) && !maybeAskGpuPassthrough(config)) {
        return;
    }
    if (!isSystemSoftwareConfig(config)) {
        config = manager_.normaliseConfig(config);
    }
    QString diskError;
    if (!prepareStorageDirectory(&config.storageDir, &diskError)) {
        showMessage("虚拟机保存位置不可用", diskError);
        return;
    }
    if (isSystemSoftwareConfig(config) && !ensureSystemSoftwareRom(config, &diskError)) {
        showMessage("System Software ROM 不可用", diskError);
        return;
    }
    if (!isSystemSoftwareConfig(config)) {
        if (!ensureRecoveryGuardDisk(config, &diskError)) {
            showMessage("恢复保护盘不可用", diskError);
            return;
        }
        if (!ensureWritableDiskImages(config, &diskError)) {
            showMessage("硬盘不可用", diskError);
            return;
        }
    } else {
        SystemSoftwareEngine engine;
        if (!engine.validate(config, &diskError)) {
            showMessage("System Software 无法启动", diskError);
            return;
        }
    }
    vms_[currentIndex_] = config;
    persistCurrentVm();
    loadVmToEditor(config);
    const QString guestOs = config.guestOs.toLower();
    if (isSystemSoftwareConfig(config)) {
        // ROM and disk validation above fully covers this backend.
    } else {
    if (isClassicMacOsUi(config.guestOs)
        && !hasBootMedia(config)
        && !hasWritableSystemDisk(config)) {
        QMessageBox::information(this, "需要 Classic MacOS 镜像", "Classic MacOS 需要手动导入安装光盘、软盘或磁盘镜像。");
        return;
    }
    if (isModernMacOsUi(config.guestOs)
        && (config.macRecovery.ipswPath.trimmed().isEmpty() || !hasBootMedia(config))) {
        if (!canUseNativeMacIpswFlow(config.architecture)) {
            QMessageBox::information(this, "macOS 创建说明", macOsInstallMethodText(config.architecture));
            return;
        }
        const auto answer = QMessageBox::question(this, "需要 macOS IPSW", "未选择 macOS IPSW 或恢复镜像。是否自动下载适配当前架构的最新 IPSW？");
        if (answer == QMessageBox::Yes) {
            downloadImage();
        }
        return;
    }
    if (isModernMacOsUi(config.guestOs) && !config.macRecovery.sourceVerified) {
        QMessageBox::information(
            this,
            "macOS 镜像未验证",
            "当前 IPSW 不是通过 Apple 官方 HTTPS 直链下载并验证的镜像。请使用“下载镜像”重新获取后再启动。");
        return;
    }
    if (guestOs.contains("windows") && !hasBootMedia(config) && config.disks.isEmpty()) {
        QMessageBox::information(this, "需要 Windows 镜像", "未选择 Windows ISO。将打开微软官方 Windows 下载页，请下载后手动导入 ISO。");
        QDesktopServices::openUrl(QUrl(microsoftWindows11DownloadPage()));
        return;
    }
    if (!guestOs.contains("dos")) {
        if (!hasWritableSystemDisk(config)) {
            QMessageBox::information(this, "需要虚拟硬盘", "此虚拟机还没有可写系统盘。请先在右侧“创建硬盘”或重新创建虚拟机。");
            return;
        }
    }
    }
    VmConfig launchConfig = config;
    if (config.recoveryGuard.quarantineNextStart) {
        launchConfig.networks.clear();
        launchConfig.sharedDirectories.clear();
    }
    auto* window = new VirtualMachineWindow(launchConfig);
    window->setAttribute(Qt::WA_DeleteOnClose);
    const int launchedIndex = currentIndex_;
    connect(window, &VirtualMachineWindow::configChanged, this, [this, launchedIndex](const VmConfig& updated) {
        if (launchedIndex < 0 || launchedIndex >= vms_.size()) {
            return;
        }
        vms_[launchedIndex] = updated;
        if (currentIndex_ == launchedIndex) {
            loadVmToEditor(updated);
        }
        persistCurrentVm();
        refreshMediaList();
        refreshPreview();
    });
    connect(window, &VirtualMachineWindow::bootFailed, this,
            [this, launchedIndex, window](const QString& detail) {
                handleBootFailure(launchedIndex, window, detail);
            });
    connect(window, &VirtualMachineWindow::firstFrameReady, this, [this, launchedIndex] {
        handleFirstFrameReady(launchedIndex);
    });
    QString error;
    if (!window->start(&error)) {
        window->deleteLater();
        showMessage("启动失败", error);
        return;
    }
    runningPid_ = window->processId();
    const qint64 launchedPid = runningPid_;
    connect(window, &QObject::destroyed, this, [this, launchedPid] {
        if (runningPid_ == launchedPid) runningPid_ = 0;
    });
    vmWindows_.append(QPointer<VirtualMachineWindow>(window));
    if (isSystemSoftwareConfig(config)) {
        // The bundled runtime supplies the native guest window. Keep this
        // controller alive but hidden so there is no duplicate blank window.
        window->activateRuntimeWindow();
    } else {
        window->show();
        showMessage("已启动",
                    config.recoveryGuard.quarantineNextStart
                        ? QString("%1 已在离线隔离模式下启动。").arg(config.name)
                        : QString("%1 已在 VirtualWorld 中启动。").arg(config.name));
    }
}

void VirtualWorldWindow::handleBootFailure(int launchedIndex,
                                           VirtualMachineWindow* window,
                                           const QString& detail) {
    if (launchedIndex < 0 || launchedIndex >= vms_.size()) {
        return;
    }

    VmConfig config = vms_.at(launchedIndex);
    if (!config.recoveryGuard.enabled) {
        return;
    }
    config.recoveryGuard.consecutiveBootFailures++;
    config.recoveryGuard.quarantineNextStart = true;
    config.recoveryGuard.lastFailureReason = detail.left(1200);
    vms_[launchedIndex] = config;
    QString saveError;
    manager_.saveConfig(QDir(config.storageDir).filePath("config.json"), config, &saveError);
    if (currentIndex_ == launchedIndex) {
        loadVmToEditor(config);
    }

    if (window) {
        window->close();
    }
    runningPid_ = 0;

    const RecoveryDiagnostic diagnostic = isSupportedLinuxGuest(config)
        ? diagnoseLinuxBootFailure(config, detail)
        : isSupportedMacosGuest(config)
            ? diagnoseMacosBootFailure(config, detail)
            : diagnoseWindowsBootFailure(config, detail);
    QMessageBox box(this);
    box.setWindowTitle(trText("self_check_title"));
    switch (diagnostic.kind) {
    case RecoveryDiagnosticKind::Configuration:
        box.setText(trText("self_check_config"));
        box.setInformativeText(trText("self_check_config_hint")
                               + "\n\n" + trText("quarantine")
                               + "\n\n" + diagnostic.technicalDetail);
        break;
    case RecoveryDiagnosticKind::Disk:
        box.setText(trText("self_check_disk"));
        box.setInformativeText(trText("self_check_disk_hint")
                               + "\n\n" + trText("quarantine")
                               + "\n\n" + diagnostic.technicalDetail);
        break;
    case RecoveryDiagnosticKind::GuestSystemDamage:
        box.setText(trText("self_check_guest"));
        box.setInformativeText(trText("self_check_guest_hint")
                               + "\n\n" + trText("quarantine")
                               + "\n\n" + diagnostic.technicalDetail);
        break;
    }
    QPushButton* retry = box.addButton(trText("offline_retry"), QMessageBox::AcceptRole);
    QPushButton* install = nullptr;
    if (diagnostic.kind == RecoveryDiagnosticKind::GuestSystemDamage) {
        install = box.addButton(trText("choose_install"), QMessageBox::ActionRole);
    }
    box.addButton(trText("close"), QMessageBox::RejectRole);
    box.exec();

    if (box.clickedButton() == retry) {
        QTimer::singleShot(0, this, [this] { startVm(); });
        return;
    }
    if (install && box.clickedButton() == install) {
        const QString path = QFileDialog::getOpenFileName(
            this,
            trText("choose_install"),
            QString(),
            "Images (*.iso *.img *.raw *.qcow2);;All files (*)");
        if (path.isEmpty()) {
            return;
        }
        VmConfig updated = vms_.at(launchedIndex);
        prependInstallImage(updated, path, "cdrom");
        vms_[launchedIndex] = updated;
        if (currentIndex_ == launchedIndex) {
            loadVmToEditor(updated);
        }
        persistCurrentVm();
        QTimer::singleShot(0, this, [this] { startVm(); });
    }
}

void VirtualWorldWindow::handleFirstFrameReady(int launchedIndex) {
    if (launchedIndex < 0 || launchedIndex >= vms_.size()) {
        return;
    }
    VmConfig config = vms_.at(launchedIndex);
    if (!config.recoveryGuard.enabled || !config.recoveryGuard.quarantineNextStart) {
        return;
    }

    config.recoveryGuard.consecutiveBootFailures = 0;
    config.recoveryGuard.lastFailureReason.clear();
    const auto answer = QMessageBox::question(
        this,
        trText("self_check_title"),
        trText("restore_network_question"),
        QMessageBox::Yes | QMessageBox::No,
        QMessageBox::No);
    if (answer == QMessageBox::Yes) {
        config.recoveryGuard.quarantineNextStart = false;
    }
    vms_[launchedIndex] = config;
    if (currentIndex_ == launchedIndex) {
        loadVmToEditor(config);
    }
    persistCurrentVm();
}

void VirtualWorldWindow::stopVm() {
    for (const QPointer<VirtualMachineWindow>& window : vmWindows_) {
        if (window) {
            window->close();
            window->deleteLater();
        }
    }
    vmWindows_.clear();
    runningPid_ = 0;
    showMessage("已停止", "虚拟机进程已停止。");
}

void VirtualWorldWindow::addDisk() {
    if (currentIndex_ < 0) return;
    const QString path = QFileDialog::getOpenFileName(this, "导入硬盘镜像", QString(), "Disk images (*.qcow2 *.img *.raw *.vhd *.vhdx *.vmdk *.vdi *.hdd);;All files (*)");
    if (path.isEmpty()) return;
    VmConfig config = editorToVm();
    const QString interfaceName = diskInterfaceCombo_->currentText();
    const QString media = interfaceName == "floppy" ? "floppy" : "disk";
    config.disks.append(DiskConfig{path, interfaceName, QFileInfo(path).suffix().toLower(), false, media});
    vms_[currentIndex_] = config;
    persistCurrentVm();
    refreshMediaList();
    refreshPreview();
}

void VirtualWorldWindow::addCdrom() {
    if (currentIndex_ < 0) return;
    const QString path = QFileDialog::getOpenFileName(this, "选择 ISO/启动镜像", QString(), "Images (*.iso *.cdr *.dmg *.ipsw *.img *.raw *.qcow2 *.dsk *.hfv *.toast);;All files (*)");
    if (path.isEmpty()) return;
    VmConfig config = editorToVm();
    const QString interfaceName = diskInterfaceCombo_->currentText();
    const QString suffix = QFileInfo(path).suffix().toLower();
    if (interfaceName == "floppy"
        || suffix == "dsk"
        || suffix == "ima"
        || suffix == "flp"
        || (suffix == "img" && isLegacyPcUi(config.guestOs))) {
        prependInstallImage(config, path, "floppy");
    } else if (interfaceName == "cdrom" || suffix == "iso" || suffix == "cdr" || suffix == "dmg" || suffix == "ipsw" || suffix == "toast") {
        prependInstallImage(config, path, "cdrom");
    } else {
        prependInstallImage(config, path, interfaceName);
    }
    vms_[currentIndex_] = config;
    persistCurrentVm();
    refreshMediaList();
    refreshPreview();
}

void VirtualWorldWindow::createDiskImage() {
    if (currentIndex_ < 0) return;
    VmConfig current = editorToVm();
    const QString dirPath = current.storageDir.trimmed().isEmpty() ? defaultStorageDirForVm(current.name) : current.storageDir.trimmed();
    QString preparedDirPath = dirPath;
    QString error;
    if (!prepareStorageDirectory(&preparedDirPath, &error)) {
        showMessage("创建失败", error);
        return;
    }
    const QString defaultPath = QDir(preparedDirPath).filePath(sanitizeFileName(current.name) + ".qcow2");
    const QString path = QFileDialog::getSaveFileName(this, "创建 qcow2 硬盘", defaultPath, "qcow2 (*.qcow2)");
    if (path.isEmpty()) return;
    VmConfig config = editorToVm();
    QString targetDir = QFileInfo(path).dir().absolutePath();
    if (!prepareStorageDirectory(&targetDir, &error)) {
        showMessage("创建失败", error);
        return;
    }
    const bool classicPpc = isClassicMacOsUi(config.guestOs)
                            && normalizeArchitecture(config.architecture) == "ppc"
                            && machineBase(config.machine) == "mac99";
    const qint64 requestedSizeMb = sizeValueMb();
    const bool created = classicPpc
                             ? manager_.createClassicMacDisk(path, requestedSizeMb, &error)
                             : manager_.createDisk(path, requestedSizeMb, &error);
    if (!created) {
        showMessage("创建失败", error);
        return;
    }
    QString interfaceName = diskInterfaceCombo_->currentText();
    if (classicPpc) {
        interfaceName = "ide";
    }
    config.disks.append(DiskConfig{path, interfaceName, "qcow2", false, "disk", sizeValueMb()});
    vms_[currentIndex_] = config;
    persistCurrentVm();
    refreshMediaList();
    refreshPreview();
    showMessage("创建完成",
                classicPpc
                    ? path + "\n已初始化 Apple 分区表和 HFS+ 卷 Macintosh HD。"
                    : path);
}

void VirtualWorldWindow::downloadImage() {
    if (currentIndex_ < 0) return;
    const VmConfig config = editorToVm();
    const QString guestOs = config.guestOs.toLower();

    if (isClassicMacOsUi(config.guestOs) || guestOs.contains("linux") || guestOs.contains("bsd") || guestOs.contains("dos") || guestOs.contains("other")) {
        showMessage("需要手动导入", "Classic MacOS、Linux 及其他系统镜像暂不提供内置下载。请使用“添加镜像”导入 ISO、软盘、内核或磁盘镜像。");
        return;
    }

    if (guestOs.contains("windows")) {
        const QVector<ImageInfo> images = downloads_.builtInImages("windows", config.architecture);
        QMessageBox::information(this, "Windows 官方下载", images.first().notes);
        QDesktopServices::openUrl(QUrl(images.first().url));
        return;
    }

    if (!guestOs.contains("macos")) {
        showMessage("暂不支持", "当前系统类型没有内置镜像下载源，请手动导入。");
        return;
    }

    if (!canUseNativeMacIpswFlow(config.architecture)) {
        showMessage("macOS 创建说明", macOsInstallMethodText(config.architecture));
        return;
    }

    auto* progress = new QProgressDialog("正在获取 macOS IPSW 列表...", "取消", 0, 0, this);
    progress->setWindowModality(Qt::WindowModal);
    progress->show();
    downloads_.fetchMacOsIpswList(config.architecture, [this, progress](QVector<ImageInfo> images, QString error) {
        progress->close();
        progress->deleteLater();
        if (!error.isEmpty()) {
            showMessage("获取失败", error);
            return;
        }
        if (images.isEmpty()) {
            showMessage("没有可用镜像", "未获取到适配当前架构的 macOS IPSW。请手动导入镜像。");
            return;
        }
        QStringList names;
        for (const ImageInfo& image : images) {
            names << image.name;
        }
        bool ok = false;
        const QString selected = QInputDialog::getItem(this, "选择 macOS IPSW", "镜像", names, 0, false, &ok);
        if (!ok || selected.isEmpty()) return;
        const int index = names.indexOf(selected);
        const ImageInfo image = images.value(index);
        QString fileName = image.name;
        fileName.replace("/", "-").replace("\\", "-").replace(":", "-");
        const VmConfig current = editorToVm();
        const QString baseDir = current.storageDir.trimmed().isEmpty() ? suggestedDownloadDir() : current.storageDir.trimmed();
        QDir(baseDir).mkpath(".");
        const QString target = QFileDialog::getSaveFileName(this, "保存 IPSW", QDir(baseDir).filePath(fileName + ".ipsw"), "IPSW (*.ipsw)");
        if (target.isEmpty()) return;
        auto* downloadProgress = new QProgressDialog("正在下载 IPSW...", "取消", 0, 100, this);
        downloadProgress->setWindowModality(Qt::WindowModal);
        downloadProgress->show();
        downloads_.download(image, target,
            [downloadProgress](qint64 received, qint64 total) {
                if (total > 0) {
                    downloadProgress->setMaximum(100);
                    downloadProgress->setValue(int(received * 100 / total));
                }
            },
            [this, downloadProgress, target, image](QString error) {
                downloadProgress->close();
                downloadProgress->deleteLater();
                if (!error.isEmpty()) {
                    showMessage("下载失败", error);
                    return;
                }
                VmConfig updated = editorToVm();
                updated.macRecovery.ipswPath = QFileInfo(target).absoluteFilePath();
                updated.macRecovery.sourceUrl = image.url;
                updated.macRecovery.sourceVerified =
                    isOfficialAppleIpswUrl(QUrl(image.url));
                prependInstallImage(updated, target, "cdrom");
                vms_[currentIndex_] = updated;
                persistCurrentVm();
                refreshMediaList();
                refreshPreview();
                showMessage("下载完成", target);
            });
    });
}

void VirtualWorldWindow::switchBootImage() {
    if (currentIndex_ < 0) return;
    const QString path = QFileDialog::getOpenFileName(this, "切换启动镜像", QString(), "Images (*.iso *.cdr *.dmg *.ipsw *.img *.raw *.qcow2 *.dsk *.hfv *.toast);;All files (*)");
    if (path.isEmpty()) return;
    VmConfig config = editorToVm();
    config.cdroms.clear();
    for (int i = config.disks.size() - 1; i >= 0; --i) {
        if (config.disks.at(i).media == "cdrom" || config.disks.at(i).media == "floppy") {
            config.disks.removeAt(i);
        }
    }
    const QString suffix = QFileInfo(path).suffix().toLower();
    prependInstallImage(config,
                        path,
                        (isLegacyPcUi(config.guestOs)
                         && (suffix == "img" || suffix == "dsk" || suffix == "ima" || suffix == "flp"))
                            ? "floppy"
                            : "cdrom");
    if (config.guestOs.trimmed().toLower().contains("macos")
        && !isClassicMacOsUi(config.guestOs)) {
        config.macRecovery.ipswPath = QFileInfo(path).absoluteFilePath();
        config.macRecovery.sourceUrl.clear();
        config.macRecovery.sourceVerified = false;
    }
    vms_[currentIndex_] = config;
    persistCurrentVm();
    refreshMediaList();
    refreshPreview();
}

} // namespace vw
