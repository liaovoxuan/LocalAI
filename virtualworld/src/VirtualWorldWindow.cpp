#include "VirtualWorldWindow.hpp"

#include "ConfigManager.hpp"
#include "QemuParser.hpp"
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
#include <QFile>
#include <QFileDialog>
#include <QFileInfo>
#include <QFormLayout>
#include <QHBoxLayout>
#include <QInputDialog>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QMessageBox>
#include <QPlainTextEdit>
#include <QProgressDialog>
#include <QPushButton>
#include <QRegularExpression>
#include <QScreen>
#include <QStandardPaths>
#include <QStorageInfo>
#include <QSplitter>
#include <QTextEdit>
#include <QtGlobal>
#include <QUrl>
#include <QVBoxLayout>

namespace vw {

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

static QString suggestedDownloadDir() {
    QString dir = QStandardPaths::writableLocation(QStandardPaths::DownloadLocation);
    if (dir.isEmpty()) {
        dir = QDir::homePath();
    }
    return dir;
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

static QString displayNameForGuest(const QString& os) {
    const QString key = os.trimmed().toLower();
    if (key.contains("classic") && key.contains("mac")) return "Classic MacOS";
    if (key.contains("macos")) return "macOS";
    if (key.contains("old windows")) return "Old Windows";
    if (key.contains("windows")) return "Windows";
    if (key.contains("linux")) return "Linux";
    if (key.contains("bsd")) return "BSD";
    if (key.contains("dos")) return "DOS";
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

static bool isLegacyPcUi(const QString& os) {
    return os.trimmed().toLower().contains("dos") || isOldWindowsUi(os);
}

static QString inputPresetFromConfig(const VmConfig& config) {
    const QStringList devices = config.usb.devices;
    if (devices.contains("usb-tablet")) {
        return "平板指针 + 键盘";
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
    showEmptyLibrary();
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
    addToolButton("导出 QEMU", [this] { exportQemu(); });
    addToolButton("导出 UTM", [this] { exportUtm(); });
    toolbar->addStretch(1);
    addToolButton("停止", [this] { stopVm(); });
    addToolButton("启动", [this] { startVm(); }, true);
    root->addLayout(toolbar);

    auto* splitter = new QSplitter();
    splitter->setChildrenCollapsible(false);
    root->addWidget(splitter, 1);

    vmList_ = new QListWidget();
    vmList_->setMinimumWidth(220);
    connect(vmList_, &QListWidget::currentRowChanged, this, [this](int row) {
        if (row < 0 || row >= vms_.size()) return;
        currentIndex_ = row;
        loadVmToEditor(vms_[row]);
    });
    splitter->addWidget(vmList_);

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
    auto* editorLayout = new QVBoxLayout(editorPane_);
    editorLayout->setContentsMargins(0, 0, 0, 0);
    editorLayout->setSpacing(10);
    detailLayout->addWidget(editorPane_, 1);

    auto* form = new QFormLayout();
    form->setHorizontalSpacing(18);
    form->setVerticalSpacing(12);
    form->setFieldGrowthPolicy(QFormLayout::ExpandingFieldsGrow);
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
    guestOsCombo_->addItems({"generic", "macOS", "Classic MacOS", "Windows", "Old Windows", "Linux", "BSD", "DOS", "Other"});
    guestOsCombo_->setEditable(true);
    archCombo_ = new QComboBox();
    archCombo_->addItems({"x86_64", "aarch64", "i386", "arm", "ppc", "ppc64", "m68k", "riscv64"});
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
    graphicsCombo_ = new QComboBox();
    graphicsCombo_->addItems({"virtio-vga", "virtio-vga-gl", "virtio-gpu-pci", "virtio-gpu-gl-pci", "qxl-vga", "VGA", "cirrus", "std", "ramfb"});
    graphicsCombo_->setEditable(true);
    inputCombo_ = new QComboBox();
    inputCombo_->addItems({"平板指针 + 键盘", "USB 键盘鼠标", "PS/2 键盘鼠标", "ADB 键盘鼠标"});
    gpuPassthroughCheck_ = new QCheckBox("启动时使用 GPU 直通");
    gpuPassthroughCombo_ = new QComboBox();
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

    form->addRow("名称", nameEdit_);
    form->addRow("保存位置", storageRow);
    form->addRow("系统", guestOsCombo_);
    form->addRow("架构", archCombo_);
    form->addRow("机型", machineCombo_);
    form->addRow("CPU", cpuEdit_);
    form->addRow("核心", coresSpin_);
    form->addRow("内存", memoryWidget);
    form->addRow("硬件加速", acceleratorCombo_);
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
    connect(guestOsCombo_, &QComboBox::currentTextChanged, this, [this, bindRefresh](const QString& os) {
        const QString key = os.toLower();
        if (!loadingEditor_) {
            nameEdit_->setText(displayNameForGuest(os));
        }
        if (isClassicMacOsUi(os)) {
            tpmCheck_->setChecked(false);
            archCombo_->setCurrentText("ppc");
            machineCombo_->setCurrentText("mac99");
            cpuEdit_->setText("G3");
            acceleratorCombo_->setCurrentText("tcg");
            graphicsCombo_->setCurrentText("VGA");
            inputCombo_->setCurrentText("ADB 键盘鼠标");
            openGlCheck_->setChecked(false);
            memoryUnitCombo_->setCurrentText("MB");
            memorySpin_->setValue(128);
            diskInterfaceCombo_->setCurrentText("ide");
        } else if (isLegacyPcUi(os)) {
            tpmCheck_->setChecked(false);
            archCombo_->setCurrentText("i386");
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
            tpmCheck_->setChecked(true);
            machineCombo_->setCurrentText("q35");
            if (archCombo_->currentText().isEmpty()) archCombo_->setCurrentText("x86_64");
        } else if (key.contains("macos")) {
            archCombo_->setCurrentText(detectHostProfile().arch == "aarch64" ? "aarch64" : "x86_64");
            machineCombo_->setCurrentText(detectHostProfile().arch == "aarch64" ? "virt" : "q35");
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
    connect(initrdEdit_, &QLineEdit::textChanged, this, bindRefresh);
    connect(appendEdit_, &QLineEdit::textChanged, this, bindRefresh);

    auto* mediaRow = new QHBoxLayout();
    mediaList_ = new QListWidget();
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
    mediaButtons->addWidget(new QLabel("默认接口"));
    mediaButtons->addWidget(diskInterfaceCombo_);
    mediaButtons->addWidget(new QLabel("新硬盘大小"));
    mediaButtons->addWidget(diskSizeSpin_);
    mediaButtons->addWidget(diskSizeUnitCombo_);
    mediaButtons->addWidget(addDiskButton);
    mediaButtons->addWidget(addCdButton);
    mediaButtons->addWidget(createDiskButton);
    mediaButtons->addWidget(downloadButton);
    mediaButtons->addWidget(switchButton);
    mediaButtons->addStretch(1);
    mediaRow->addLayout(mediaButtons);
    editorLayout->addWidget(new QLabel("镜像与磁盘"));
    editorLayout->addLayout(mediaRow);

    issueText_ = new QTextEdit();
    issueText_->setReadOnly(true);
    issueText_->setMaximumHeight(100);
    commandPreview_ = new QPlainTextEdit();
    commandPreview_->setReadOnly(true);
    editorLayout->addWidget(new QLabel("兼容性提示"));
    editorLayout->addWidget(issueText_);
    editorLayout->addWidget(new QLabel("启动配置预览"));
    editorLayout->addWidget(commandPreview_, 1);
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
    QString base = QStandardPaths::writableLocation(QStandardPaths::DocumentsLocation);
    if (base.isEmpty()) {
        base = QStandardPaths::writableLocation(QStandardPaths::AppDataLocation);
    }
    if (base.isEmpty()) {
        base = QDir::homePath();
    }
    return QDir(base).filePath("VirtualWorld VMs");
#endif
}

QString VirtualWorldWindow::defaultStorageDirForVm(const QString& name) const {
    return QDir(defaultStorageRoot()).filePath(sanitizeFileName(name));
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
    if (isClassicMacOsUi(config.guestOs)) {
        if (config.architecture.trimmed().isEmpty()) {
            config.architecture = "ppc";
        }
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
        config.memoryMb = qBound(16, config.memoryMb, 512);
        config.tpm.enabled = false;
        config.networks.clear();
        config.graphics.adapter.clear();
        config.graphics.vga = normalizeArchitecture(config.architecture) == "m68k" ? QString() : "VGA";
        config.graphics.openGl = false;
        config.graphics.autoResize = true;
        config.graphics.dynamicResolution = false;
        config.graphics.retina = false;
        if (config.disks.isEmpty() && config.cdroms.isEmpty()) {
            config.disks.clear();
        }
    } else if (isLegacyPcUi(config.guestOs)) {
        config.architecture = "i386";
        config.machine = "pc";
        config.accelerator = "tcg";
        config.cpuModel = guest.contains("dos") ? "486" : "pentium";
        config.cpuCores = 1;
        config.memoryMb = guest.contains("dos") ? qMin(config.memoryMb, 16) : qMin(config.memoryMb, 64);
        if (config.memoryMb < 1) config.memoryMb = guest.contains("dos") ? 16 : 32;
        config.tpm.enabled = false;
        config.networks.clear();
        config.graphics.adapter.clear();
        config.graphics.vga = "cirrus";
        config.graphics.openGl = false;
        config.graphics.autoResize = true;
        config.graphics.dynamicResolution = false;
        config.graphics.retina = false;
    } else if (guest.contains("windows")) {
        config.architecture = config.architecture.isEmpty() ? "x86_64" : config.architecture;
        config.machine = "q35";
        config.tpm.enabled = true;
        config.cpuCores = qMax(4, config.cpuCores);
        config.memoryMb = qMax(4096, config.memoryMb);
        config.graphics.adapter = "virtio-vga";
    } else if (guest.contains("macos")) {
        const HostProfile host = detectHostProfile();
        config.architecture = host.arch == "aarch64" ? "aarch64" : "x86_64";
        config.machine = config.architecture == "aarch64" ? "virt" : "q35";
        config.accelerator = "auto";
        config.graphics.retina = true;
        config.graphics.dynamicResolution = true;
        config.graphics.autoResize = true;
    } else if (guest.contains("linux")) {
        config.machine = defaultMachineForArch(config.architecture);
        config.graphics.adapter = "virtio-vga";
        config.networks = config.networks.isEmpty() ? QVector<NetworkConfig>{NetworkConfig{}} : config.networks;
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
    dialog.resize(720, 560);
    dialog.setStyleSheet(QString("QDialog{background:#f7f9fc;color:#1f2937;font-size:14px;}"
                                 "QLineEdit,QComboBox,QDoubleSpinBox{background:white;border:1px solid #d7deea;border-radius:8px;padding:9px;min-height:30px;}"
                                 "QLabel{background:transparent;}")
                         + buttonStyle());
    auto* layout = new QVBoxLayout(&dialog);
    layout->setContentsMargins(28, 28, 28, 22);
    layout->setSpacing(18);
    auto* title = new QLabel("新建虚拟机");
    title->setStyleSheet("QLabel{font-size:22px;font-weight:700;color:#111827;}");
    layout->addWidget(title);

    auto* form = new QFormLayout();
    form->setHorizontalSpacing(22);
    form->setVerticalSpacing(14);
    form->setFieldGrowthPolicy(QFormLayout::ExpandingFieldsGrow);
    auto* name = new QLineEdit("VirtualWorld VM");
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
    os->addItems({"macOS", "Classic MacOS", "Windows", "Old Windows", "Linux", "BSD", "DOS", "Other"});
    auto* arch = new QComboBox();
    arch->addItems({"x86_64", "aarch64", "i386", "arm", "ppc", "ppc64", "m68k", "riscv64"});
    arch->setEditable(true);
    auto* inputMode = new QComboBox();
    inputMode->addItems({"平板指针 + 键盘", "USB 键盘鼠标", "PS/2 键盘鼠标", "ADB 键盘鼠标"});
    auto* passthrough = new QCheckBox("创建后默认使用 GPU 直通");
    auto* passthroughGpu = new QComboBox();
    const QVector<HostGpuInfo> wizardGpus = detectHostGpus();
    for (const HostGpuInfo& gpu : wizardGpus) {
        passthroughGpu->addItem(gpuDisplayText(gpu), gpu.id);
    }
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
    auto* download = new QCheckBox("创建后下载/获取安装镜像");
    auto* import = new QCheckBox("创建后手动导入镜像");
    import->setChecked(true);

    form->addRow("名称", name);
    form->addRow("保存位置", storageRow);
    form->addRow("操作系统", os);
    form->addRow("架构", arch);
    form->addRow("CPU 核心", cpu);
    form->addRow("内存", memoryRow);
    form->addRow("硬盘大小", diskRow);
    form->addRow("磁盘接口", interfaceName);
    form->addRow("输入方式", inputMode);
    form->addRow("", passthrough);
    form->addRow("直通 GPU", passthroughGpu);
    form->addRow("", keepVirtualDisplay);
    form->addRow("", download);
    form->addRow("", import);
    layout->addLayout(form);

    auto* hint = new QLabel("Classic MacOS、Linux 和其他系统镜像需要手动导入。Windows 将打开微软官方下载页；现代 macOS 将获取 IPSW 候选。");
    hint->setWordWrap(true);
    hint->setStyleSheet("QLabel{color:#667085;}");
    layout->addWidget(hint);

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
    connect(os, &QComboBox::currentTextChanged, &dialog, [name, arch, interfaceName, inputMode, memory, memoryUnit](const QString& value) {
        const QString key = value.toLower();
        name->setText(displayNameForGuest(value));
        if (isClassicMacOsUi(value)) {
            arch->setCurrentText("ppc");
            interfaceName->setCurrentText("ide");
            inputMode->setCurrentText("ADB 键盘鼠标");
            memoryUnit->setCurrentText("MB");
            memory->setValue(128);
        } else if (isOldWindowsUi(value)) {
            arch->setCurrentText("i386");
            interfaceName->setCurrentText("ide");
            inputMode->setCurrentText("PS/2 键盘鼠标");
        } else if (key.contains("macos")) {
            arch->setCurrentText(detectHostProfile().arch == "aarch64" ? "aarch64" : "x86_64");
            interfaceName->setCurrentText("nvme");
            inputMode->setCurrentText("平板指针 + 键盘");
        } else if (key.contains("windows")) {
            arch->setCurrentText(detectHostProfile().arch == "aarch64" ? "aarch64" : "x86_64");
            interfaceName->setCurrentText("virtio");
            inputMode->setCurrentText("平板指针 + 键盘");
        } else if (key.contains("dos")) {
            arch->setCurrentText("i386");
            interfaceName->setCurrentText("ide");
            inputMode->setCurrentText("PS/2 键盘鼠标");
        }
    });

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
    config.architecture = normalizeArchitecture(arch->currentText());
    config.cpuCores = int(cpu->value());
    config.memoryMb = int(toMb(memory->value(), memoryUnit->currentText()));
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
    if (passthrough->isChecked() && passthroughGpu->currentIndex() >= 0) {
        applyGpuSelection(config, wizardGpus.value(passthroughGpu->currentIndex()), true, keepVirtualDisplay->isChecked());
    }
    diskInterfaceCombo_->setCurrentText(interfaceName->currentText());
    diskSizeSpin_->setValue(diskSize->value());
    diskSizeUnitCombo_->setCurrentText(diskUnit->currentText());
    if (download->isChecked()) {
        config.extraArgs << "__virtualworld_download_after_create__";
    } else if (import->isChecked()) {
        config.extraArgs << "__virtualworld_import_after_create__";
    }
    return config;
}

int VirtualWorldWindow::memoryValueMb() const {
    return int(toMb(memorySpin_->value(), memoryUnitCombo_->currentText()));
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

void VirtualWorldWindow::loadVmToEditor(const VmConfig& config) {
    loadingEditor_ = true;
    nameEdit_->setText(config.name);
    storageEdit_->setText(config.storageDir);
    archCombo_->setCurrentText(normalizeArchitecture(config.architecture));
    guestOsCombo_->setCurrentText(config.guestOs);
    machineCombo_->setCurrentText(config.machine.isEmpty() ? defaultMachineForArch(config.architecture) : machineBase(config.machine));
    cpuEdit_->setText(config.cpuModel);
    coresSpin_->setValue(config.cpuCores);
    if (config.memoryMb >= 1024 && config.memoryMb % 1024 == 0) {
        memoryUnitCombo_->setCurrentText("GB");
        memorySpin_->setValue(double(config.memoryMb) / 1024.0);
    } else {
        memoryUnitCombo_->setCurrentText("MB");
        memorySpin_->setValue(config.memoryMb);
    }
    acceleratorCombo_->setCurrentText(config.accelerator.isEmpty() ? "auto" : config.accelerator);
    graphicsCombo_->setCurrentText(config.graphics.adapter.isEmpty() ? "virtio-vga" : config.graphics.adapter);
    inputCombo_->setCurrentText(inputPresetFromConfig(config));
    refreshGpuPassthroughChoices();
    gpuPassthroughCheck_->setChecked(config.gpuPassthrough.enabled);
    keepVirtualDisplayCheck_->setChecked(config.gpuPassthrough.keepVirtualDisplay);
    int gpuIndex = gpuPassthroughCombo_->findData(config.gpuPassthrough.deviceId);
    if (gpuIndex < 0 && !config.gpuPassthrough.name.isEmpty()) {
        gpuIndex = gpuPassthroughCombo_->findText(config.gpuPassthrough.name, Qt::MatchContains);
    }
    if (gpuIndex >= 0) {
        gpuPassthroughCombo_->setCurrentIndex(gpuIndex);
    }
    openGlCheck_->setChecked(config.graphics.openGl);
    autoResizeCheck_->setChecked(config.graphics.autoResize);
    retinaCheck_->setChecked(config.graphics.retina);
    tpmCheck_->setChecked(config.tpm.enabled);
    tpmSocketEdit_->setText(config.tpm.emulatorSocket);
    kernelEdit_->setText(config.boot.kernelPath);
    initrdEdit_->setText(config.boot.initrdPath);
    appendEdit_->setText(config.boot.kernelAppend);
    loadingEditor_ = false;
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
    config.architecture = normalizeArchitecture(archCombo_->currentText());
    config.machine = machineCombo_->currentText();
    config.cpuModel = cpuEdit_->text().trimmed().isEmpty() ? "max" : cpuEdit_->text().trimmed();
    config.cpuCores = int(coresSpin_->value());
    config.memoryMb = memoryValueMb();
    config.accelerator = acceleratorCombo_->currentText();
    config.graphics.adapter = graphicsCombo_->currentText();
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
    const ConversionResult converted = convertToQemu(vms_[currentIndex_], detectHostProfile().platform);
    commandPreview_->setPlainText(converted.text);
    appendIssueText(converted.issues);
}

void VirtualWorldWindow::refreshMediaList() {
    mediaList_->clear();
    if (currentIndex_ < 0 || currentIndex_ >= vms_.size()) return;
    const VmConfig& config = vms_[currentIndex_];
    for (const DiskConfig& disk : config.disks) {
        mediaList_->addItem(QString("%1 [%2]: %3")
                                .arg(disk.media == "cdrom" ? "CD/DVD" : (disk.media == "floppy" ? "Floppy" : "Disk"),
                                     disk.interfaceName,
                                     disk.path));
    }
    for (const QString& cdrom : config.cdroms) {
        mediaList_->addItem("CD/DVD: " + cdrom);
    }
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

void VirtualWorldWindow::newVm() {
    VmConfig config = createVmWithWizard();
    if (config.name.isEmpty()) return;
    const bool downloadAfterCreate = config.extraArgs.removeAll("__virtualworld_download_after_create__") > 0;
    const bool importAfterCreate = config.extraArgs.removeAll("__virtualworld_import_after_create__") > 0;
    if (config.storageDir.trimmed().isEmpty()) {
        config.storageDir = defaultStorageDirForVm(config.name);
    }
    QDir dir(config.storageDir);
    if (!dir.exists() && !dir.mkpath(".")) {
        showMessage("创建失败", "无法创建虚拟机保存文件夹：" + config.storageDir);
        return;
    }
    QString saveError;
    if (!manager_.saveConfig(dir.filePath("config.json"), config, &saveError)) {
        showMessage("保存失败", saveError);
        return;
    }
    vms_.append(config);
    refreshVmList();
    vmList_->setCurrentRow(vms_.size() - 1);
    setEditorAvailable(true);
    if (downloadAfterCreate) {
        downloadImage();
    } else if (importAfterCreate) {
        addCdrom();
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
    vms_.append(parseQemuCommand(QString::fromUtf8(file.readAll())));
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
    vms_.append(parseUtmSource(source));
    refreshVmList();
    vmList_->setCurrentRow(vms_.size() - 1);
    setEditorAvailable(true);
}

void VirtualWorldWindow::importVirtualWorld() {
    const QString path = QFileDialog::getOpenFileName(this, "导入 VirtualWorld 配置", QString(), "VirtualWorld JSON (*.json);;All files (*)");
    if (path.isEmpty()) return;
    vms_.append(manager_.loadConfig(path));
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
    if (!manager_.saveConfig(path, config, &error)) {
        showMessage("保存失败", error);
        return;
    }
    vms_[currentIndex_] = config;
    loadVmToEditor(config);
    showMessage("保存完成", path);
}

void VirtualWorldWindow::exportQemu() {
    if (currentIndex_ < 0) return;
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
    if (!maybeAskGpuPassthrough(config)) {
        return;
    }
    vms_[currentIndex_] = config;
    loadVmToEditor(config);
    const QString guestOs = config.guestOs.toLower();
    if (isClassicMacOsUi(config.guestOs) && config.cdroms.isEmpty() && config.disks.isEmpty()) {
        QMessageBox::information(this, "需要 Classic MacOS 镜像", "Classic MacOS 需要手动导入安装光盘、软盘或磁盘镜像。");
        return;
    }
    if (guestOs.contains("macos") && config.cdroms.isEmpty() && config.disks.isEmpty()) {
        const auto answer = QMessageBox::question(this, "需要 macOS IPSW", "未选择 macOS IPSW 或本地镜像。是否自动下载适配当前架构的最新 IPSW？");
        if (answer == QMessageBox::Yes) {
            downloadImage();
        }
        return;
    }
    if (guestOs.contains("windows") && config.cdroms.isEmpty() && config.disks.isEmpty()) {
        QMessageBox::information(this, "需要 Windows 镜像", "未选择 Windows ISO。将打开微软官方 Windows 下载页，请下载后手动导入 ISO。");
        QDesktopServices::openUrl(QUrl(microsoftWindows11DownloadPage()));
        return;
    }
    auto* window = new VirtualMachineWindow(config);
    window->setAttribute(Qt::WA_DeleteOnClose);
    QString error;
    if (!window->start(&error)) {
        window->deleteLater();
        showMessage("启动失败", error);
        return;
    }
    runningPid_ = window->processId();
    vmWindows_.append(QPointer<VirtualMachineWindow>(window));
    window->show();
    showMessage("已启动", QString("%1 已在 VirtualWorld 中启动。").arg(config.name));
}

void VirtualWorldWindow::stopVm() {
    for (const QPointer<VirtualMachineWindow>& window : vmWindows_) {
        if (window) {
            window->close();
        }
    }
    vmWindows_.clear();
    runningPid_ = 0;
    showMessage("已停止", "虚拟机进程已停止。");
}

void VirtualWorldWindow::addDisk() {
    if (currentIndex_ < 0) return;
    const QString path = QFileDialog::getOpenFileName(this, "选择磁盘镜像", QString(), "Images (*.qcow2 *.img *.raw *.vhd *.vhdx);;All files (*)");
    if (path.isEmpty()) return;
    VmConfig config = editorToVm();
    const QString interfaceName = diskInterfaceCombo_->currentText();
    const QString media = interfaceName == "floppy" ? "floppy" : "disk";
    config.disks.append(DiskConfig{path, interfaceName, QFileInfo(path).suffix().toLower(), false, media});
    vms_[currentIndex_] = config;
    refreshMediaList();
    refreshPreview();
}

void VirtualWorldWindow::addCdrom() {
    if (currentIndex_ < 0) return;
    const QString path = QFileDialog::getOpenFileName(this, "选择 ISO/启动镜像", QString(), "Images (*.iso *.img *.qcow2 *.dsk *.hfv *.toast);;All files (*)");
    if (path.isEmpty()) return;
    VmConfig config = editorToVm();
    const QString interfaceName = diskInterfaceCombo_->currentText();
    const QString suffix = QFileInfo(path).suffix().toLower();
    if (interfaceName == "floppy" || suffix == "dsk") {
        config.disks.append(DiskConfig{path, "floppy", QFileInfo(path).suffix().toLower(), false, "floppy"});
    } else if (interfaceName == "cdrom" || suffix == "iso" || suffix == "toast") {
        config.disks.append(DiskConfig{path, interfaceName == "cdrom" ? "ide" : interfaceName, QFileInfo(path).suffix().toLower(), true, "cdrom"});
    } else {
        config.disks.append(DiskConfig{path, interfaceName, suffix, false, "disk"});
    }
    vms_[currentIndex_] = config;
    refreshMediaList();
    refreshPreview();
}

void VirtualWorldWindow::createDiskImage() {
    if (currentIndex_ < 0) return;
    VmConfig current = editorToVm();
    const QString dirPath = current.storageDir.trimmed().isEmpty() ? defaultStorageDirForVm(current.name) : current.storageDir.trimmed();
    QDir(dirPath).mkpath(".");
    const QString defaultPath = QDir(dirPath).filePath(sanitizeFileName(current.name) + ".qcow2");
    const QString path = QFileDialog::getSaveFileName(this, "创建 qcow2 硬盘", defaultPath, "qcow2 (*.qcow2)");
    if (path.isEmpty()) return;
    QString error;
    if (!manager_.createDisk(path, sizeValueMb(), &error)) {
        showMessage("创建失败", error);
        return;
    }
    VmConfig config = editorToVm();
    config.disks.append(DiskConfig{path, diskInterfaceCombo_->currentText(), "qcow2", false, "disk"});
    vms_[currentIndex_] = config;
    refreshMediaList();
    refreshPreview();
    showMessage("创建完成", path);
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
            [this, downloadProgress, target](QString error) {
                downloadProgress->close();
                downloadProgress->deleteLater();
                if (!error.isEmpty()) {
                    showMessage("下载失败", error);
                    return;
                }
                VmConfig updated = editorToVm();
                updated.cdroms.prepend(target);
                vms_[currentIndex_] = updated;
                refreshMediaList();
                refreshPreview();
                showMessage("下载完成", target);
            });
    });
}

void VirtualWorldWindow::switchBootImage() {
    if (currentIndex_ < 0) return;
    const QString path = QFileDialog::getOpenFileName(this, "切换启动镜像", QString(), "Images (*.iso *.img *.qcow2 *.dsk *.hfv *.toast);;All files (*)");
    if (path.isEmpty()) return;
    VmConfig config = editorToVm();
    if (config.cdroms.isEmpty()) {
        config.cdroms << path;
    } else {
        config.cdroms[0] = path;
    }
    vms_[currentIndex_] = config;
    refreshMediaList();
    refreshPreview();
}

} // namespace vw
