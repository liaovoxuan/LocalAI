#include "Translator.hpp"
#include "RecoveryGuard.hpp"

#include <QFileInfo>
#include <QtGlobal>
#include <algorithm>

namespace vw {

static QString normalizeNetMode(QString mode) {
    mode = mode.trimmed().toLower().replace("_", "-");
    if (mode == "shared" || mode == "share" || mode == "nat") return "user";
    if (mode == "bridged") return "bridge";
    if (mode.isEmpty()) return "user";
    return mode;
}

static QString displayBackendForHost(HostPlatform target) {
    switch (target) {
    case HostPlatform::MacOS:
        return "cocoa";
    case HostPlatform::Windows:
        return "sdl";
    case HostPlatform::Linux:
    default:
        return "gtk";
    }
}

static QString audioDriverForConfig(const AudioConfig& audio, HostPlatform target) {
    const QString requested = audio.driver.trimmed().toLower();
    if (!requested.isEmpty() && requested != "auto") {
        return requested;
    }
    switch (target) {
    case HostPlatform::MacOS:
        return "coreaudio";
    case HostPlatform::Windows:
        return "sdl";
    case HostPlatform::Linux:
    default:
        return "pa";
    }
}

static QString displayStringForConfig(const GraphicsConfig& graphics, HostPlatform target) {
    QString display = graphics.display.trimmed();
    if (display == "off" || display == "headless") {
        return {};
    }
    if (display == "none") {
        return "none";
    }
    if (display.isEmpty() || display == "auto") {
        display = displayBackendForHost(target);
    }
    if (graphics.openGl && !display.contains("gl=", Qt::CaseInsensitive)) {
        display += ",gl=on";
    }
    if (graphics.autoResize && display.startsWith("gtk", Qt::CaseInsensitive)
        && !display.contains("zoom-to-fit=", Qt::CaseInsensitive)) {
        display += ",zoom-to-fit=on";
    }
    if (graphics.retina && display.startsWith("cocoa", Qt::CaseInsensitive)
        && !display.contains("zoom-to-fit=", Qt::CaseInsensitive)) {
        display += ",zoom-to-fit=on";
    }
    return display;
}

static bool isOldWindowsGuest(const QString& value);
static bool isLegacyPcGuest(const VmConfig& config);
static bool isClassicMacOsGuest(const QString& value);

static QString legacyVgaOption(QString value) {
    value = value.trimmed().toLower();
    if (value == "vga") {
        return "std";
    }
    if (value == "std" || value == "cirrus" || value == "vmware"
        || value == "qxl" || value == "virtio" || value == "none") {
        return value;
    }
    return {};
}

static QString graphicsAdapterForConfig(const VmConfig& config) {
    QString adapter = config.graphics.adapter.trimmed();
    if (isLegacyPcGuest(config)) {
        return {};
    }
    if (!legacyVgaOption(adapter).isEmpty()) {
        return {};
    }
    if (adapter.isEmpty() && !config.graphics.vga.trimmed().isEmpty()) {
        return {};
    }
    if (isClassicMacOsGuest(config.guestOs) && normalizeArchitecture(config.architecture) == "m68k") {
        return {};
    }
    if (isClassicMacOsGuest(config.guestOs) && !config.graphics.vga.trimmed().isEmpty() && adapter.isEmpty()) {
        return {};
    }
    if (adapter.isEmpty()) {
        adapter = "virtio-vga";
    }
    if (config.graphics.openGl && adapter == "virtio-vga") {
        adapter = "virtio-vga-gl";
    }
    const QString arch = normalizeArchitecture(config.architecture);
    const QString machine = machineBase(config.machine);
    if ((machine == "virt" || arch == "aarch64" || arch == "arm" || arch == "riscv64" || arch == "loongarch64")
        && (adapter == "virtio-vga" || adapter == "virtio-vga-gl" || adapter == "VGA")) {
        adapter = config.graphics.openGl ? "virtio-gpu-gl-pci" : "virtio-gpu-pci";
    }
    if ((config.architecture == "ppc" || config.architecture == "ppc64")
        && adapter.startsWith("virtio", Qt::CaseInsensitive)) {
        adapter = "VGA";
    }
    return adapter;
}

static QString graphicsVgaForConfig(const VmConfig& config) {
    QString vga = config.graphics.vga.trimmed();
    if (vga.isEmpty()) {
        vga = legacyVgaOption(config.graphics.adapter);
    }
    if (isClassicMacOsGuest(config.guestOs)
        && normalizeArchitecture(config.architecture) == "ppc"
        && vga.compare("vga", Qt::CaseInsensitive) == 0) {
        vga = "std";
    }
    return vga;
}

static bool needsXhciController(const VmConfig& config) {
    const QString arch = normalizeArchitecture(config.architecture);
    return machineBase(config.machine) == "virt"
        || arch == "aarch64"
        || arch == "arm"
        || arch == "riscv64"
        || arch == "loongarch64";
}

static QString usbControllerForConfig(const VmConfig& config) {
    const QString controller = config.usb.controller.trimmed();
    if (controller == "usb" && needsXhciController(config)) {
        return "qemu-xhci";
    }
    return controller;
}

static int powerOfTwoFloor(int value) {
    if (value <= 0) {
        return 0;
    }
    int result = 1;
    while (result <= value / 2) {
        result *= 2;
    }
    return result;
}

static QString graphicsDeviceArgument(const VmConfig& config) {
    QString adapter = graphicsAdapterForConfig(config);
    if (adapter.isEmpty()) {
        return {};
    }
    const int ramMb = powerOfTwoFloor(qMax(0, config.graphics.ramMb));
    if (ramMb > 0 && adapter.startsWith("virtio", Qt::CaseInsensitive)) {
        const qint64 bytes = qint64(ramMb) * 1024 * 1024;
        adapter += QString(",hostmem=%1,max_hostmem=%1").arg(bytes);
        if (config.guestOs.toLower().contains("macos")) {
            adapter += ",xres=1920,yres=1200";
        } else {
            adapter += ",xres=1280,yres=800";
        }
    }
    return adapter;
}

static void normalizeLegacyGraphicsArguments(VmConfig& config) {
    auto normalizeList = [&config](QStringList& arguments) {
        QStringList normalized;
        for (int index = 0; index < arguments.size();) {
            if (arguments.at(index) == "-device" && index + 1 < arguments.size()) {
                const QString device = splitOptions(arguments.at(index + 1)).value(0).trimmed();
                const QString legacyVga = legacyVgaOption(device);
                if (!legacyVga.isEmpty()) {
                    if (config.graphics.adapter.trimmed().isEmpty()
                        && config.graphics.vga.trimmed().isEmpty()) {
                        config.graphics.vga = legacyVga;
                    }
                    index += 2;
                    continue;
                }
            }
            normalized << arguments.at(index++);
        }
        arguments = normalized;
    };

    normalizeList(config.extraArgs);
    normalizeList(config.unsupportedArgs);
}

static QString normalizedGuestOs(const QString& value) {
    return value.trimmed().toLower().replace(" ", "");
}

static bool isOldWindowsGuest(const QString& value) {
    const QString guestOs = normalizedGuestOs(value);
    return guestOs.contains("oldwindows")
        || guestOs.contains("windows3")
        || guestOs.contains("windows31")
        || guestOs.contains("windows95")
        || guestOs.contains("windows98")
        || guestOs.contains("windowsme");
}

static bool isClassicMacOsGuest(const QString& value) {
    const QString guestOs = normalizedGuestOs(value);
    return guestOs.contains("classicmac") || guestOs.contains("macosclassic") || guestOs.contains("classicmacos");
}

static bool isLegacyPcGuest(const VmConfig& config) {
    const QString guestOs = normalizedGuestOs(config.guestOs);
    return guestOs.contains("dos") || isOldWindowsGuest(config.guestOs);
}

// These machines expose their network controller as part of the board rather
// than as a PCI device. QEMU wires the user/bridge backend to it through -nic.
static QString builtInNetworkModel(const VmConfig& config) {
    const QString arch = normalizeArchitecture(config.architecture);
    const QString machine = machineBase(config.machine);
    if (isClassicMacOsGuest(config.guestOs)) {
        if (arch == "m68k" && machine == "q800") {
            return "dp83932";
        }
        if ((arch == "ppc" || arch == "ppc64") && machine == "mac99") {
            return "sungem";
        }
        if (arch == "ppc" && (machine == "g3beige" || machine == "g3" || machine == "oldworld")) {
            return "ne2k_pci";
        }
    }
    if (isLegacyPcGuest(config) && (arch == "i386" || arch == "x86_64") && machine == "pc") {
        return "ne2k_pci";
    }
    return {};
}

static QString effectiveNetworkModel(const VmConfig& config, const NetworkConfig& network) {
    const QString builtIn = builtInNetworkModel(config);
    if (!builtIn.isEmpty()) {
        return builtIn;
    }
    return network.model.trimmed().isEmpty() ? "virtio-net-pci" : network.model.trimmed();
}

static bool looksLikeLegacyFloppyImage(const DiskConfig& disk) {
    if (disk.media == "floppy" || disk.path.trimmed().isEmpty()) {
        return false;
    }
    const QString suffix = QFileInfo(disk.path).suffix().trimmed().toLower();
    if (suffix != "img" && suffix != "ima" && suffix != "flp" && suffix != "dsk") {
        return false;
    }
    const QFileInfo info(disk.path);
    // Legacy floppy images are small raw files. Do not reinterpret large
    // .img hard disks as floppies.
    return info.exists() && info.isFile() && info.size() > 0 && info.size() <= 16LL * 1024 * 1024;
}

static qint64 minimumMemoryKb(const VmConfig& config) {
    const QString arch = normalizeArchitecture(config.architecture);
    if (isLegacyPcGuest(config)) {
        // Preserve the configured conventional-memory amount (for example
        // 640 KB for Windows 1.0), but give QEMU one physical megabyte.
        // The PC memory map still exposes only 640 KB of conventional RAM to
        // the guest; below 1 MB QEMU 11 leaves the VGA surface uninitialized.
        return 1024;
    }
    if (arch == "i386") {
        return 8 * 1024;
    }
    if (arch == "ppc") {
        return 64 * 1024;
    }
    if (arch == "m68k") {
        return 16 * 1024;
    }
    return 128 * 1024;
}

static QString safeDiskFormat(const DiskConfig& disk) {
    QString format = disk.format.trimmed().toLower();
    if (format == "iso") {
        return "raw";
    }
    if (format == "img") {
        return "raw";
    }
    if (format == "dsk" || format == "hfv" || format == "toast" || format == "cdr") {
        return "raw";
    }
    if (format == "vhd") {
        return "vpc";
    }
    if (format.isEmpty()) {
        return disk.media == "cdrom" || disk.media == "floppy" ? "raw" : QString();
    }
    return format;
}

static QStringList driveBaseParts(const DiskConfig& disk, bool includeInterface, const QString& interfaceName) {
    QStringList parts;
    parts << "file=" + disk.path;
    if (includeInterface) {
        parts << "if=" + interfaceName;
    }
    if (disk.media == "cdrom") {
        parts << "media=cdrom";
    } else if (disk.media == "floppy") {
        parts << "media=disk";
    }
    const QString format = safeDiskFormat(disk);
    if (!format.isEmpty()) {
        parts << "format=" + format;
    }
    if (disk.readonly) {
        parts << "readonly=on";
    }
    return parts;
}

static bool prefersVirtMachineStorage(const QString& archValue, const QString& machineValue) {
    const QString arch = normalizeArchitecture(archValue);
    const QString machine = machineBase(machineValue);
    return machine == "virt" || arch == "aarch64" || arch == "arm" || arch == "riscv64" || arch == "loongarch64";
}

static bool prefersScsiCdrom(const VmConfig& config) {
    const QString machine = machineBase(config.machine);
    const QString arch = normalizeArchitecture(config.architecture);
    return prefersVirtMachineStorage(config.architecture, config.machine)
        || (machine == "q35" && arch == "i386");
}

static bool prefersAhciCdrom(const VmConfig& config) {
    const QString machine = machineBase(config.machine);
    const QString arch = normalizeArchitecture(config.architecture);
    return machine == "q35" && arch == "x86_64";
}

static bool prefersLegacyIdeStorage(const VmConfig& config) {
    const QString arch = normalizeArchitecture(config.architecture);
    const QString machine = machineBase(config.machine);
    return isLegacyPcGuest(config)
        || arch == "m68k"
        || (arch == "ppc" && machine == "mac99");
}

static bool usesMac99IdeStorage(const VmConfig& config) {
    return normalizeArchitecture(config.architecture) == "ppc"
        && machineBase(config.machine) == "mac99";
}

static bool prefersPseriesStorage(const VmConfig& config) {
    return normalizeArchitecture(config.architecture) == "ppc64"
        && machineBase(config.machine) == "pseries";
}

static bool hasInstallCdrom(const VmConfig& config) {
    if (!config.cdroms.isEmpty()) {
        return true;
    }
    for (const DiskConfig& disk : config.disks) {
        if (disk.media == "cdrom" && !disk.path.trimmed().isEmpty()) {
            return true;
        }
    }
    return false;
}

static bool hasBootFloppy(const VmConfig& config) {
    for (const DiskConfig& disk : config.disks) {
        if (disk.media == "floppy" && !disk.path.trimmed().isEmpty()) {
            return true;
        }
    }
    return false;
}

static QVector<DiskConfig> orderedStorageDevices(const VmConfig& config) {
    QVector<DiskConfig> devices;
    for (const QString& legacyCdrom : config.cdroms) {
        const QString cdrom = legacyCdrom.trimmed();
        if (cdrom.isEmpty()) {
            continue;
        }
        bool alreadyPresent = false;
        for (const DiskConfig& existing : config.disks) {
            if (existing.path == cdrom && (existing.media == "cdrom" || existing.media == "floppy")) {
                alreadyPresent = true;
                break;
            }
        }
        if (alreadyPresent) {
            continue;
        }
        DiskConfig disk;
        disk.path = cdrom;
        disk.media = "cdrom";
        disk.interfaceName = "ide";
        disk.format = QFileInfo(cdrom).suffix().toLower();
        disk.readonly = true;
        devices.append(disk);
    }
    for (const DiskConfig& disk : config.disks) {
        if (!isRecoveryGuardDisk(disk)) {
            devices.append(disk);
        }
    }
    for (const DiskConfig& disk : config.disks) {
        if (isRecoveryGuardDisk(disk)) {
            devices.append(disk);
        }
    }
    return devices;
}

static QString firstBootableMedia(const QVector<DiskConfig>& devices) {
    for (const DiskConfig& disk : devices) {
        if (disk.path.trimmed().isEmpty()) {
            continue;
        }
        if (disk.media == "cdrom" || disk.media == "floppy" || disk.media == "disk") {
            return disk.media;
        }
    }
    return "disk";
}

static QString bootIndexPart(int bootIndex) {
    return bootIndex > 0 ? QString("bootindex=%1").arg(bootIndex) : QString();
}

static QString smpTopology(const VmConfig& config) {
    const int cores = qMax(1, config.cpuCores);
    const QString arch = normalizeArchitecture(config.architecture);
    if (isLegacyPcGuest(config) || arch == "m68k") {
        return "1";
    }
    return QString("cpus=%1,sockets=1,cores=%1,threads=1").arg(cores);
}

static HostGpuInfo selectedPassthroughGpu(const GpuPassthroughConfig& passthrough) {
    for (const HostGpuInfo& gpu : detectHostGpus()) {
        if ((!passthrough.deviceId.isEmpty() && gpu.id == passthrough.deviceId)
            || (!passthrough.pciAddress.isEmpty() && gpu.pciAddress == passthrough.pciAddress)
            || (!passthrough.name.isEmpty() && gpu.name == passthrough.name)) {
            return gpu;
        }
    }
    return {};
}

static void appendDiskArgs(QStringList& args,
                           const DiskConfig& disk,
                           int index,
                           const VmConfig& config,
                           bool& hasUsbController,
                           bool& hasScsiController,
                           bool& hasAhciController,
                           int bootIndex = 0,
                           int mac99IdeIndex = -1) {
    QString iface = disk.interfaceName.trimmed().toLower();
    if (iface.isEmpty()) {
        iface = "virtio";
    }
    if (disk.media == "floppy") {
        args << "-drive" << driveBaseParts(disk, true, "floppy").join(',');
        return;
    }
    if (prefersLegacyIdeStorage(config) && (iface == "virtio" || iface == "sata" || iface == "nvme" || iface == "scsi")) {
        iface = "ide";
    }
    if (prefersPseriesStorage(config) && (iface == "ide" || iface == "sata" || iface == "nvme")) {
        iface = "scsi";
    }
    if (iface == "cdrom") {
        iface = "ide";
    }
    if (disk.media == "cdrom" && iface != "ide" && iface != "scsi" && iface != "usb") {
        iface = prefersAhciCdrom(config) ? "sata" : (prefersScsiCdrom(config) ? "scsi" : "ide");
    }
    if (disk.media == "cdrom" && iface == "ide" && prefersAhciCdrom(config)) {
        iface = "sata";
    }
    if (disk.media == "cdrom" && iface == "ide" && prefersScsiCdrom(config)) {
        iface = "scsi";
    }
    if (usesMac99IdeStorage(config) && iface == "ide") {
        QStringList parts = driveBaseParts(disk, true, "ide");
        if (mac99IdeIndex >= 0) {
            parts << "index=" + QString::number(mac99IdeIndex);
        }
        args << "-drive" << parts.join(',');
        return;
    }
    if (prefersLegacyIdeStorage(config) && iface == "ide") {
        QStringList parts = driveBaseParts(disk, true, "ide");
        // SeaBIOS and pre-PnP Windows boot more reliably from the legacy IDE
        // slots than from dynamically created ide-hd devices with bootindex.
        if (disk.media == "cdrom") {
            parts << "index=2";
        } else {
            parts << "index=" + QString::number(qMin(index, 1));
        }
        args << "-drive" << parts.join(',');
        return;
    }
    if (iface == "sata") {
        if (!hasAhciController) {
            args << "-device" << "ich9-ahci,id=vw_ahci0";
            hasAhciController = true;
        }
        const QString driveId = "vw_sata" + QString::number(index);
        QStringList parts = driveBaseParts(disk, false, {});
        parts << "if=none" << "id=" + driveId;
        QStringList deviceParts;
        deviceParts << (disk.media == "cdrom" ? "ide-cd" : "ide-hd")
                    << "drive=" + driveId
                    << "bus=vw_ahci0." + QString::number(index);
        const QString bootPart = bootIndexPart(bootIndex);
        if (!bootPart.isEmpty()) deviceParts << bootPart;
        args << "-drive" << parts.join(',')
             << "-device" << deviceParts.join(',');
        return;
    }
    if (iface == "ide" && bootIndex > 0) {
        const QString driveId = "vw_ide" + QString::number(index);
        QStringList parts = driveBaseParts(disk, false, {});
        parts << "if=none" << "id=" + driveId;
        QStringList deviceParts;
        deviceParts << (disk.media == "cdrom" ? "ide-cd" : "ide-hd")
                    << "drive=" + driveId
                    << bootIndexPart(bootIndex);
        args << "-drive" << parts.join(',')
             << "-device" << deviceParts.join(',');
        return;
    }
    if (iface == "nvme") {
        const QString driveId = "vw_nvme" + QString::number(index);
        QStringList parts = driveBaseParts(disk, false, {});
        parts << "if=none" << "id=" + driveId;
        QStringList deviceParts;
        deviceParts << "nvme" << "drive=" + driveId << "serial=VWNVME" + QString::number(index);
        const QString bootPart = bootIndexPart(bootIndex);
        if (!bootPart.isEmpty()) deviceParts << bootPart;
        args << "-drive" << parts.join(',')
             << "-device" << deviceParts.join(',');
        return;
    }
    if (iface == "virtio") {
        const QString driveId = "vw_virtio" + QString::number(index);
        QStringList parts = driveBaseParts(disk, false, {});
        parts << "if=none" << "id=" + driveId;
        QStringList deviceParts;
        deviceParts << "virtio-blk-pci" << "drive=" + driveId;
        const QString bootPart = bootIndexPart(bootIndex);
        if (!bootPart.isEmpty()) deviceParts << bootPart;
        args << "-drive" << parts.join(',')
             << "-device" << deviceParts.join(',');
        return;
    }
    if (iface == "usb") {
        if (!hasUsbController) {
            args << "-usb";
            hasUsbController = true;
        }
        const QString driveId = "vw_usb" + QString::number(index);
        QStringList parts = driveBaseParts(disk, false, {});
        parts << "if=none" << "id=" + driveId;
        QStringList deviceParts;
        deviceParts << "usb-storage" << "drive=" + driveId;
        const QString bootPart = bootIndexPart(bootIndex);
        if (!bootPart.isEmpty()) deviceParts << bootPart;
        args << "-drive" << parts.join(',')
             << "-device" << deviceParts.join(',');
        return;
    }
    if (iface == "scsi") {
        if (!hasScsiController) {
            args << "-device" << "virtio-scsi-pci,id=vw_scsi0";
            hasScsiController = true;
        }
        const QString driveId = "vw_scsi" + QString::number(index);
        QStringList parts = driveBaseParts(disk, false, {});
        parts << "if=none" << "id=" + driveId;
        QStringList deviceParts;
        deviceParts << (disk.media == "cdrom" ? "scsi-cd" : "scsi-hd")
                    << "drive=" + driveId
                    << "bus=vw_scsi0.0";
        const QString bootPart = bootIndexPart(bootIndex);
        if (!bootPart.isEmpty()) deviceParts << bootPart;
        args << "-drive" << parts.join(',')
             << "-device" << deviceParts.join(',');
        return;
    }
    QStringList parts = driveBaseParts(disk, true, iface);
    const QString bootPart = bootIndexPart(bootIndex);
    if (!bootPart.isEmpty()) parts << bootPart;
    args << "-drive" << parts.join(',');
}

QVector<ValidationIssue> validateConfig(const VmConfig& config, HostPlatform target) {
    QVector<ValidationIssue> issues;
    HostProfile host = detectHostProfile();
    host.platform = target;
    if (config.cpuCores < 1) {
        issues.append({"error", "invalid_cpu_count", "CPU 核心数必须至少为 1。"});
    }
    if (effectiveMemoryKb(config) < 256 * 1024 && !isLegacyPcGuest(config)) {
        issues.append({"warning", "low_memory", "内存低于 256 MB，虚拟机可能无法启动。"});
    }
    const QString arch = normalizeArchitecture(config.architecture);
    const QString guestOs = normalizedGuestOs(config.guestOs);
    const QString machine = machineBase(config.machine);
    if (arch == "aarch64" && (machine == "q35" || machine == "pc")) {
        issues.append({"warning", "machine_arch_mismatch", "aarch64 通常应使用 virt 机型。"});
    }
    if ((arch == "ppc" || arch == "ppc64") && (machine == "q35" || machine == "pc" || machine == "virt" || machine.isEmpty())) {
        issues.append({"warning", "machine_arch_mismatch", arch + " 通常应使用 mac99/pseries 等 PowerPC 机型。"});
    }
    if (config.accelerator != "tcg" && !isNativeVirtualizationGuest(config.architecture, host)) {
        issues.append({"warning", "accel_arch_mismatch", "客户机架构与宿主机不匹配，硬件加速不可用，已回退或应回退到 TCG。"});
    }
    if (config.accelerator == "kvm" && !isHardwareAcceleratorAvailable("kvm", host)) {
        issues.append({"warning", "kvm_unavailable", "KVM 当前不可用。请检查 /dev/kvm 权限、虚拟化开关，或使用 TCG。"});
    }
    if (config.accelerator == "whpx") {
        issues.append({"warning", "whpx_requires_windows_feature", "WHPX 需要 Windows Hypervisor Platform/虚拟化支持已启用。若启动失败请启用系统功能或改用 TCG。"});
    }
    if (config.accelerator == "hvf" && target != HostPlatform::MacOS) {
        issues.append({"warning", "hvf_macos_only", "HVF 仅适用于 macOS，其他系统应使用 KVM/WHPX/TCG。"});
    }
    if (config.graphics.openGl) {
        if (arch == "ppc" || arch == "ppc64" || arch == "m68k" || arch == "riscv64") {
            issues.append({"warning", "opengl_arch_warning", "当前客户机架构的 OpenGL 图形加速兼容性有限，如显示异常请关闭 OpenGL。"});
        } else {
            issues.append({"warning", "opengl_guest_driver", "OpenGL 图形加速需要客户机驱动和宿主 QEMU 显示后端支持；黑屏或崩溃时请关闭 OpenGL。"});
        }
    }
    if (config.graphics.retina && target == HostPlatform::MacOS) {
        issues.append({"warning", "retina_scaling", "Retina/HiDPI 模式会按当前屏幕缩放，旧系统或旧客户机可能需要手动调整分辨率。"});
    }
    if (config.gpuPassthrough.enabled) {
        const HostGpuInfo gpu = selectedPassthroughGpu(config.gpuPassthrough);
        if (target != HostPlatform::Linux) {
            issues.append({"warning", "gpu_passthrough_platform", "GPU 直通当前仅在 Linux/KVM/VFIO 路径生成 QEMU 参数。macOS/Windows 会保留配置但不会启用直通，避免影响宿主系统。"});
        } else if (gpu.name.isEmpty()) {
            issues.append({"warning", "gpu_passthrough_missing", "已启用 GPU 直通，但未找到已选择的 GPU。"});
        } else if (!gpu.passthroughCandidate) {
            issues.append({"warning", "gpu_passthrough_unsafe", "所选 GPU 不满足安全直通条件：" + gpu.reason});
        } else if (gpu.pciAddress.isEmpty()) {
            issues.append({"warning", "gpu_passthrough_no_pci", "所选 GPU 缺少 PCI 地址，无法生成 vfio-pci 参数。"});
        } else {
            issues.append({"warning", "gpu_passthrough_install_display", "已配置 GPU 直通，但仍保留虚拟显卡。系统安装完成并确认驱动正常后，再依需要调整显示输出。"});
        }
    }
    if (isClassicMacOsGuest(config.guestOs)) {
        issues.append({"warning", "classic_macos_manual_media", "Classic MacOS 需要匹配年代的安装镜像、磁盘接口和输入方式；ADB/PS2 通常由旧机型内置，USB 需客户机支持。"});
        if (arch != "ppc" && arch != "ppc64" && arch != "m68k" && arch != "x86_64") {
            issues.append({"warning", "classic_macos_arch", "Classic MacOS 通常使用 m68k 或 PowerPC；当前架构会保留，但请确认镜像兼容。"});
        }
        if (arch == "m68k") {
            issues.append({"warning", "classic_m68k_video_audio", "m68k/Q800 使用板载 SONIC 网卡和 Apple Sound Chip；音频会按 ASC 兼容模式连接宿主后端。"});
            if (config.firmware.trimmed().isEmpty()) {
                issues.append({"warning", "classic_m68k_rom_required", "m68k Classic MacOS 通常需要提供合法来源的 MacROM.bin，并在固件/BIOS 路径中配置。"});
            }
        }
    } else if (guestOs.contains("macos")) {
        issues.append({"warning", "macos_host_serial", "macOS 虚拟机会尝试使用本机序列号。请确认这符合您的授权和隐私预期。"});
        const QString hostArch = normalizeArchitecture(host.arch);
        if (target != HostPlatform::MacOS) {
            issues.append({"warning", "macos_non_macos_host", "现代 macOS 创建入口已保留，但 Windows/Linux 等宿主系统通常不能使用 Apple 的 IPSW 原生虚拟化路径。请手动导入合法镜像或恢复介质，此配置主要用于迁移、研究或实验性 QEMU 启动。"});
        } else if ((arch == "x86_64" || arch == "aarch64") && hostArch != arch) {
            issues.append({"warning", "macos_cross_arch", "现代 macOS 客户机架构与本机架构不同，例如 Intel Mac 模拟 Apple Silicon 或 Apple Silicon 模拟 Intel Mac，通常只能使用 TCG 实验运行，性能和启动兼容性有限。"});
        }
        if (arch == "ppc" || arch == "ppc64" || arch == "m68k") {
            issues.append({"warning", "macos_legacy_arch_hint", "PPC 或更早架构的 Mac 系统请优先选择 Classic MacOS 类型，以便使用旧机型、旧输入设备和手动镜像流程。"});
        }
    }
    if (config.audio.enabled && config.audio.driver.trimmed().compare("none", Qt::CaseInsensitive) == 0) {
        issues.append({"warning", "audio_driver_none", "音频已启用但后端为 none，QEMU 将保持静音。"});
    }
    if (config.audio.enabled && !config.audio.model.trimmed().isEmpty()) {
        const QString model = config.audio.model.trimmed().toLower();
        if (model != "ac97" && model != "ich9-intel-hda" && model != "intel-hda"
            && model != "hda-duplex" && model != "es1370" && model != "sb16" && model != "gus") {
            issues.append({"warning", "audio_model_unknown", "音频设备型号可能不受当前 QEMU 构建支持：" + config.audio.model});
        }
    }
    if (config.audio.enabled && isClassicMacOsGuest(config.guestOs)
        && (arch == "ppc" || arch == "ppc64")) {
        issues.append({"warning", "classic_ppc_audio_fallback", "Classic PPC 没有统一的板载 QEMU 音频设备，当前使用 ES1370 兼容设备；部分 MacOS 版本可能需要额外驱动。"});
    }
    if (guestOs.contains("windows") && !isOldWindowsGuest(config.guestOs) && !config.tpm.enabled) {
        issues.append({"warning", "windows_tpm_recommended", "新版 Windows 通常需要 TPM 2.0。已建议启用 TPM；老版本 Windows 可关闭。实际运行还需要 swtpm 或等效 TPM 后端。"});
    }
    if (config.tpm.enabled && config.tpm.emulatorSocket.trimmed().isEmpty()) {
        issues.append({"warning", "tpm_socket_missing", "已启用 TPM，但未配置 swtpm socket。QEMU 参数会保留 TPM 意图，启动前请配置 TPM 后端。"});
    }
    if (!config.boot.kernelPath.isEmpty()) {
        issues.append({"warning", "linux_kernel_boot", "已启用内核直启。请确认 kernel/initrd/append 与客户机发行版匹配。"});
    }
    for (const auto& disk : config.disks) {
        if (disk.path.trimmed().isEmpty()) {
            issues.append({"warning", "empty_disk_path", "存在空磁盘路径，请在启动前补全。"});
        }
        if (disk.media == "cdrom"
            && (disk.interfaceName == "ide" || disk.interfaceName == "sata" || disk.interfaceName == "cdrom")
            && prefersVirtMachineStorage(config.architecture, config.machine)) {
            issues.append({"warning", "cdrom_interface_mapped", "当前 ARM/RISC-V virt 机型不支持 IDE CD/DVD，已在 QEMU 参数中自动改用 SCSI CD/DVD。"});
        }
        if (disk.media == "cdrom"
            && machineBase(config.machine) == "q35"
            && (normalizeArchitecture(config.architecture) == "x86_64" || normalizeArchitecture(config.architecture) == "i386")
            && disk.interfaceName != "scsi") {
            issues.append({"warning", "q35_cdrom_scsi_boot", "q35/UEFI 安装介质已自动改用 SCSI CD/DVD 并优先启动，避免停留在 TianoCore 固件界面。"});
        }
        if (disk.interfaceName == "nvme" || disk.interfaceName == "scsi") {
            issues.append({"warning", "disk_controller_check", "磁盘接口 " + disk.interfaceName + " 已保留，请确认目标 QEMU 构建支持对应控制器。"});
        }
    }
    for (const auto& network : config.networks) {
        const QString mode = normalizeNetMode(network.mode);
        if (mode == "tap" || mode == "bridge") {
            issues.append({"warning", "network_privilege", mode + " 网络通常需要管理员权限和宿主机网桥配置。"});
        }
    }
    if (!config.sharedDirectories.isEmpty()) {
        issues.append({"warning", "shared_folder_guest_driver", "共享目录需要客户机安装 9p/virtiofs 支持。"});
    }
    if (!config.unsupportedArgs.isEmpty()) {
        issues.append({"warning", "unsupported_args", "以下参数无法等价转换，已保留供手动检查：" + config.unsupportedArgs.join(' ')});
    }
    Q_UNUSED(target);
    return issues;
}

ConversionResult convertToQemu(VmConfig config, HostPlatform target) {
    config.architecture = normalizeArchitecture(config.architecture);
    normalizeLegacyGraphicsArguments(config);
    HostProfile host = detectHostProfile();
    host.platform = target;
    if (machineBase(config.machine).isEmpty()) {
        config.machine = defaultMachineForArch(config.architecture);
    }
    if (isLegacyPcGuest(config)) {
        config.architecture = "i386";
        config.machine = "pc";
        config.accelerator = "tcg";
        if (config.cpuModel.trimmed().isEmpty() || config.cpuModel == "max" || config.cpuModel == "host") {
            config.cpuModel = normalizedGuestOs(config.guestOs).contains("dos") ? "486" : "pentium";
        }
        config.cpuCores = 1;
        setMemoryKb(config, qMin<qint64>(effectiveMemoryKb(config), 64 * 1024));
        config.tpm.enabled = false;
        config.graphics.openGl = false;
        config.graphics.adapter.clear();
        // std is the most consistently initialized VGA device across the
        // bundled i386 QEMU builds. Cirrus can leave screendump without a
        // display surface on this headless capture path.
        config.graphics.vga = "std";
        if (config.graphics.ramMb <= 0) {
            config.graphics.ramMb = normalizedGuestOs(config.guestOs).contains("dos") ? 1 : 4;
        }
        for (DiskConfig& disk : config.disks) {
            if (looksLikeLegacyFloppyImage(disk)) {
                disk.media = "floppy";
                disk.interfaceName = "floppy";
                disk.format = "img";
                disk.readonly = false;
            }
        }
    }
    if (isClassicMacOsGuest(config.guestOs)) {
        const QString arch = normalizeArchitecture(config.architecture);
        config.accelerator = "tcg";
        if (machineBase(config.machine).isEmpty() || machineBase(config.machine) == "q35" || machineBase(config.machine) == "pc" || machineBase(config.machine) == "virt") {
            config.machine = defaultMachineForArch(config.architecture);
        }
        if (config.cpuModel.trimmed().isEmpty() || config.cpuModel == "max" || config.cpuModel == "host") {
            config.cpuModel = normalizeArchitecture(config.architecture) == "m68k" ? "m68040" : "G3";
        }
        config.cpuCores = 1;
        config.tpm.enabled = false;
        config.graphics.openGl = false;
        if (config.graphics.adapter.startsWith("virtio", Qt::CaseInsensitive)) {
            config.graphics.adapter.clear();
        }
        const QString legacyVga = legacyVgaOption(config.graphics.adapter);
        if (!legacyVga.isEmpty()) {
            config.graphics.adapter.clear();
            if (config.graphics.vga.trimmed().isEmpty()) {
                config.graphics.vga = legacyVga;
            }
        }
        if (config.graphics.vga.trimmed().isEmpty()) {
            config.graphics.vga = arch == "m68k" ? QString() : "std";
        } else if (arch == "ppc" && config.graphics.vga.compare("VGA", Qt::CaseInsensitive) == 0) {
            config.graphics.vga = "std";
        }
        if (config.graphics.ramMb <= 0) {
            config.graphics.ramMb = arch == "m68k" ? 2 : 8;
        }
        if (usesMac99IdeStorage(config)) {
            for (DiskConfig& disk : config.disks) {
                if (disk.media == "floppy") {
                    continue;
                }
                const QString interfaceName = disk.interfaceName.trimmed().toLower();
                if (interfaceName.isEmpty()
                    || interfaceName == "virtio"
                    || interfaceName == "sata"
                    || interfaceName == "nvme"
                    || interfaceName == "scsi"
                    || interfaceName == "cdrom") {
                    disk.interfaceName = "ide";
                }
            }
            bool removedUsbInput = false;
            for (int index = config.usb.devices.size() - 1; index >= 0; --index) {
                const QString device = splitOptions(config.usb.devices.at(index)).value(0).trimmed();
                if (device.compare("usb-kbd", Qt::CaseInsensitive) == 0
                    || device.compare("usb-mouse", Qt::CaseInsensitive) == 0
                    || device.compare("usb-tablet", Qt::CaseInsensitive) == 0) {
                    config.usb.devices.removeAt(index);
                    removedUsbInput = true;
                }
            }
            if (removedUsbInput && config.usb.devices.isEmpty()) {
                config.usb.controller.clear();
            }
        }
    }
    if (!isLegacyPcGuest(config) && !isClassicMacOsGuest(config.guestOs) && config.graphics.ramMb <= 0) {
        config.graphics.ramMb = normalizedGuestOs(config.guestOs).contains("macos") ? 1024 : 128;
    }
    config.accelerator = selectAccelerator(config.architecture, config.accelerator, host);
    const QString base = machineBase(config.machine);
    if (config.architecture == "aarch64" && (base == "q35" || base == "pc")) {
        config.machine = "virt";
    }
    if ((config.architecture == "ppc" || config.architecture == "ppc64") && (base == "q35" || base == "pc" || base == "virt" || base.isEmpty())) {
        config.machine = defaultMachineForArch(config.architecture);
    }
    if (config.cpuModel == "host" && config.accelerator == "tcg") {
        config.cpuModel = "max";
    }
    if (normalizedGuestOs(config.guestOs).contains("windows") && !isOldWindowsGuest(config.guestOs)) {
        config.tpm.enabled = true;
        setMemoryKb(config, qMax<qint64>(4096 * 1024, effectiveMemoryKb(config)));
        if (config.architecture == "x86_64" || config.architecture == "i386") {
            config.efi.codePath.clear();
            config.efi.varsPath.clear();
            config.firmware.clear();
            config.machine = "pc";
            config.graphics.adapter.clear();
            config.graphics.vga = "std";
            config.graphics.openGl = false;
            for (DiskConfig& disk : config.disks) {
                if (disk.media == "disk"
                    && (disk.interfaceName.trimmed().isEmpty() || disk.interfaceName == "virtio")) {
                    disk.interfaceName = "sata";
                }
            }
        }
        if (config.graphics.ramMb <= 0) {
            config.graphics.ramMb = 128;
        }
    }
    config.usb.controller = usbControllerForConfig(config);
    if (config.networks.isEmpty()) {
        const QString guestOs = normalizedGuestOs(config.guestOs);
        if (guestOs.contains("windows") || guestOs.contains("linux") || guestOs.contains("macos")
            || isLegacyPcGuest(config) || isClassicMacOsGuest(config.guestOs)) {
            NetworkConfig network;
            const QString nativeModel = builtInNetworkModel(config);
            if (!nativeModel.isEmpty()) {
                network.model = nativeModel;
            }
            config.networks.append(network);
        }
    }
    for (NetworkConfig& network : config.networks) {
        const QString nativeModel = builtInNetworkModel(config);
        if (!nativeModel.isEmpty()) {
            network.model = nativeModel;
        } else if (network.model.trimmed().isEmpty()) {
            network.model = "virtio-net-pci";
        }
    }
    ConversionResult result;
    result.config = config;
    result.issues = validateConfig(config, target);
    result.text = renderQemuCommand(config, target);
    return result;
}

QStringList buildQemuCommandParts(const VmConfig& config) {
    return buildQemuCommandParts(config, detectHostProfile().platform);
}

QStringList buildQemuCommandParts(const VmConfig& config, HostPlatform target) {
    QStringList args;
    const bool classicM68k = isClassicMacOsGuest(config.guestOs)
        && normalizeArchitecture(config.architecture) == "m68k"
        && machineBase(config.machine) == "q800";
    const QString audioId = config.audio.id.trimmed().isEmpty() ? "audio0" : config.audio.id.trimmed();
    QString machineValue = QString("%1,accel=%2").arg(config.machine, config.accelerator);
    if (classicM68k && config.audio.enabled) {
        // q800 has a built-in ASC/EASC. The classic ASC mode is understood by
        // the older MacOS sound driver and avoids the EASC incompatibility.
        machineValue += QString(",audiodev=%1,easc=off").arg(audioId);
    }
    args << executableForArch(config.architecture)
         << "-name" << (config.name.trimmed().isEmpty() ? "VirtualWorld" : config.name.trimmed())
         << "-machine" << machineValue
         << "-cpu" << (config.cpuModel.isEmpty() ? "max" : config.cpuModel)
         << "-smp" << smpTopology(config)
         << "-m" << formatQemuMemory(qMax(minimumMemoryKb(config), effectiveMemoryKb(config)));

    if (!config.firmware.isEmpty()) {
        args << "-bios" << config.firmware;
    }
    if (!isClassicMacOsGuest(config.guestOs)
        && normalizedGuestOs(config.guestOs).contains("macos")
        && (normalizeArchitecture(config.architecture) == "x86_64" || normalizeArchitecture(config.architecture) == "aarch64")) {
        const QString serial = hostHardwareSerial();
        if (!serial.isEmpty()) {
            args << "-smbios" << QString("type=1,serial=%1").arg(serial);
        }
    }
    if (!config.boot.kernelPath.isEmpty()) {
        args << "-kernel" << config.boot.kernelPath;
    }
    if (!config.boot.initrdPath.isEmpty()) {
        args << "-initrd" << config.boot.initrdPath;
    }
    if (!config.boot.kernelAppend.isEmpty()) {
        args << "-append" << config.boot.kernelAppend;
    }
    const QVector<DiskConfig> storageDevices = orderedStorageDevices(config);
    const QString firstMedia = firstBootableMedia(storageDevices);
    if (firstMedia == "cdrom") {
        args << "-boot" << "order=d,menu=on,strict=on";
    } else if (firstMedia == "floppy") {
        args << "-boot" << "order=a,menu=on,strict=on";
    } else {
        args << "-boot" << "order=c,menu=on,strict=on";
    }
    if (!config.efi.codePath.isEmpty()) {
        args << "-drive" << "if=pflash,format=raw,readonly=on,file=" + config.efi.codePath;
    }
    if (!config.efi.varsPath.isEmpty()) {
        args << "-drive" << "if=pflash,format=raw,file=" + config.efi.varsPath;
    }
    const QString usbController = usbControllerForConfig(config);
    bool hasUsbController = !usbController.isEmpty();
    bool hasScsiController = false;
    bool hasAhciController = false;
    int diskIndex = 0;
    int nextBootIndex = 1;
    int nextMac99DiskIdeIndex = 0;
    int nextMac99CdromIdeIndex = 2;
    for (const DiskConfig& disk : storageDevices) {
        const int bootIndex = disk.path.trimmed().isEmpty() || isRecoveryGuardDisk(disk)
                                  ? 0
                                  : nextBootIndex++;
        int mac99IdeIndex = -1;
        if (usesMac99IdeStorage(config) && disk.media != "floppy") {
            mac99IdeIndex = disk.media == "cdrom"
                                ? nextMac99CdromIdeIndex++
                                : nextMac99DiskIdeIndex++;
        }
        appendDiskArgs(args,
                       disk,
                       diskIndex++,
                       config,
                       hasUsbController,
                       hasScsiController,
                       hasAhciController,
                       bootIndex,
                       mac99IdeIndex);
    }
    if (config.tpm.enabled) {
        const QString socket = config.tpm.emulatorSocket.trimmed();
        if (!socket.isEmpty()) {
            args << "-chardev" << QString("socket,id=chrtpm,path=%1").arg(socket)
                 << "-tpmdev" << "emulator,id=tpm0,chardev=chrtpm"
                 << "-device" << "tpm-tis,tpmdev=tpm0";
        }
    }
    for (int i = 0; i < config.networks.size(); ++i) {
        const auto& net = config.networks[i];
        const QString mode = normalizeNetMode(net.mode);
        if (mode == "none") {
            continue;
        }
        const QString netId = "net" + QString::number(i);
        const QString model = effectiveNetworkModel(config, net);
        QStringList netParts;
        netParts << mode << "id=" + netId;
        if (!net.bridge.isEmpty()) netParts << "br=" + net.bridge;
        for (const QString& fwd : net.hostForwards) netParts << "hostfwd=" + fwd;
        const bool boardNic = !builtInNetworkModel(config).isEmpty();
        if (boardNic) {
            netParts << "model=" + model;
            if (!net.mac.isEmpty()) netParts << "mac=" + net.mac;
            args << "-nic" << netParts.join(',');
        } else {
            args << "-netdev" << netParts.join(',');
            QStringList devParts;
            devParts << model << "netdev=" + netId;
            if (!net.mac.isEmpty()) devParts << "mac=" + net.mac;
            args << "-device" << devParts.join(',');
        }
    }
    if (config.audio.enabled) {
        const QString driver = audioDriverForConfig(config.audio, target);
        args << "-audiodev" << QString("driver=%1,id=%2").arg(driver, audioId);
        if (!classicM68k) {
            QString model = config.audio.model.trimmed();
            if (model.isEmpty()) {
                const bool classicPpc = isClassicMacOsGuest(config.guestOs)
                    && (normalizeArchitecture(config.architecture) == "ppc"
                        || normalizeArchitecture(config.architecture) == "ppc64");
                model = classicPpc ? "ES1370" : (machineBase(config.machine) == "q35" ? "ich9-intel-hda" : "AC97");
            }
            if (model.compare("ich9-intel-hda", Qt::CaseInsensitive) == 0
                || model.compare("intel-hda", Qt::CaseInsensitive) == 0) {
                args << "-device" << "ich9-intel-hda"
                     << "-device" << QString("hda-duplex,audiodev=%1").arg(audioId);
            } else {
                args << "-device" << QString("%1,audiodev=%2").arg(model, audioId);
            }
        }
    }
    if (!usbController.isEmpty()) {
        if (usbController == "usb") args << "-usb";
        else args << "-device" << usbController;
    }
    for (const QString& device : config.usb.devices) {
        args << "-device" << device;
    }
    const QString vga = graphicsVgaForConfig(config);
    if (!vga.isEmpty() && normalizeArchitecture(config.architecture) != "m68k") args << "-vga" << vga;
    const QString adapter = graphicsDeviceArgument(config);
    if (!adapter.isEmpty()) args << "-device" << adapter;
    if (config.gpuPassthrough.enabled && target == HostPlatform::Linux) {
        const HostGpuInfo gpu = selectedPassthroughGpu(config.gpuPassthrough);
        if (gpu.passthroughCandidate && !gpu.pciAddress.isEmpty()) {
            args << "-device" << QString("vfio-pci,host=%1,x-vga=on").arg(gpu.pciAddress);
        }
    }
    const QString display = displayStringForConfig(config.graphics, target);
    if (!display.isEmpty()) args << "-display" << display;
    if (config.spice.enabled) {
        QStringList parts;
        if (config.spice.port > 0) parts << "port=" + QString::number(config.spice.port);
        if (!config.spice.addr.isEmpty()) parts << "addr=" + config.spice.addr;
        if (config.spice.disableTicketing) parts << "disable-ticketing=on";
        args << "-spice" << (parts.isEmpty() ? "disable-ticketing=on" : parts.join(','));
    }
    for (const auto& share : config.sharedDirectories) {
        args << "-virtfs" << QString("local,path=%1,mount_tag=%2,security_model=%3").arg(share.path, share.tag, share.securityModel);
    }
    args << config.extraArgs << config.unsupportedArgs;
    return args;
}

QString renderQemuCommand(const VmConfig& config, HostPlatform target) {
    const QStringList args = buildQemuCommandParts(config, target);

    if (target == HostPlatform::Windows) {
        QStringList quoted;
        for (QString item : args) {
            if (item.contains(' ') || item.contains('&') || item.contains('(') || item.contains(')')) {
                item.replace("\"", "\\\"");
                item = "\"" + item + "\"";
            }
            quoted << item;
        }
        return quoted.join(" ^\n  ");
    }

    QStringList quoted;
    for (const QString& item : args) {
        quoted << shellQuote(item);
    }
    return quoted.join(" \\\n  ");
}

} // namespace vw
