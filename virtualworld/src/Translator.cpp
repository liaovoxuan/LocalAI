#include "Translator.hpp"

#include <QtGlobal>

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

static QString displayStringForConfig(const GraphicsConfig& graphics, HostPlatform target) {
    QString display = graphics.display.trimmed();
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

static QString graphicsAdapterForConfig(const VmConfig& config) {
    QString adapter = config.graphics.adapter.trimmed();
    if (isLegacyPcGuest(config)) {
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
    if ((machine == "virt" || arch == "aarch64" || arch == "arm" || arch == "riscv64")
        && (adapter == "virtio-vga" || adapter == "virtio-vga-gl" || adapter == "VGA")) {
        adapter = config.graphics.openGl ? "virtio-gpu-gl-pci" : "virtio-gpu-pci";
    }
    if ((config.architecture == "ppc" || config.architecture == "ppc64")
        && adapter.startsWith("virtio", Qt::CaseInsensitive)) {
        adapter = "VGA";
    }
    return adapter;
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

static int minimumMemoryMb(const VmConfig& config) {
    const QString arch = normalizeArchitecture(config.architecture);
    if (isLegacyPcGuest(config)) {
        return 1;
    }
    if (arch == "i386") {
        return 8;
    }
    if (arch == "ppc") {
        return 64;
    }
    if (arch == "m68k") {
        return 16;
    }
    return 128;
}

static QString safeDiskFormat(const DiskConfig& disk) {
    QString format = disk.format.trimmed().toLower();
    if (format == "iso") {
        return "raw";
    }
    if (format == "img") {
        return "raw";
    }
    if (format == "dsk" || format == "hfv" || format == "toast") {
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
    return machine == "virt" || arch == "aarch64" || arch == "arm" || arch == "riscv64";
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

static void appendDiskArgs(QStringList& args, const DiskConfig& disk, int index, const VmConfig& config, bool& hasUsbController, bool& hasScsiController) {
    QString iface = disk.interfaceName.trimmed().toLower();
    if (iface.isEmpty()) {
        iface = "virtio";
    }
    if (disk.media == "floppy") {
        args << "-drive" << driveBaseParts(disk, true, "floppy").join(',');
        return;
    }
    if (iface == "cdrom") {
        iface = "ide";
    }
    if (disk.media == "cdrom" && iface != "ide" && iface != "scsi" && iface != "usb") {
        iface = "ide";
    }
    if (disk.media == "cdrom" && iface == "ide" && prefersVirtMachineStorage(config.architecture, config.machine)) {
        iface = "scsi";
    }
    if (iface == "sata") {
        iface = prefersVirtMachineStorage(config.architecture, config.machine) ? "scsi" : "ide";
    }
    if (iface == "nvme") {
        const QString driveId = "vw_nvme" + QString::number(index);
        QStringList parts = driveBaseParts(disk, false, {});
        parts << "if=none" << "id=" + driveId;
        args << "-drive" << parts.join(',')
             << "-device" << QString("nvme,drive=%1,serial=VWNVME%2").arg(driveId, QString::number(index));
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
        args << "-drive" << parts.join(',')
             << "-device" << QString("usb-storage,drive=%1").arg(driveId);
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
        args << "-drive" << parts.join(',')
             << "-device" << QString("%1,drive=%2,bus=vw_scsi0.0")
                                .arg(disk.media == "cdrom" ? "scsi-cd" : "scsi-hd", driveId);
        return;
    }
    args << "-drive" << driveBaseParts(disk, true, iface).join(',');
}

QVector<ValidationIssue> validateConfig(const VmConfig& config, HostPlatform target) {
    QVector<ValidationIssue> issues;
    HostProfile host = detectHostProfile();
    host.platform = target;
    if (config.cpuCores < 1) {
        issues.append({"error", "invalid_cpu_count", "CPU 核心数必须至少为 1。"});
    }
    if (config.memoryMb < 256 && !isLegacyPcGuest(config)) {
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
            issues.append({"warning", "classic_m68k_video_audio", "m68k/Q800 不使用 PC VGA 参数，且已禁用默认音频以提高启动兼容性。"});
            if (config.firmware.trimmed().isEmpty()) {
                issues.append({"warning", "classic_m68k_rom_required", "m68k Classic MacOS 通常需要提供合法来源的 MacROM.bin，并在固件/BIOS 路径中配置。"});
            }
        }
    } else if (guestOs.contains("macos")) {
        issues.append({"warning", "macos_host_serial", "macOS 虚拟机会尝试使用本机序列号。请确认这符合您的授权和隐私预期。"});
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
        config.tpm.enabled = false;
        config.networks.clear();
        config.graphics.openGl = false;
        config.graphics.adapter.clear();
        if (config.graphics.vga.trimmed().isEmpty()) {
            config.graphics.vga = "cirrus";
        }
    }
    if (isClassicMacOsGuest(config.guestOs)) {
        config.accelerator = "tcg";
        if (machineBase(config.machine).isEmpty() || machineBase(config.machine) == "q35" || machineBase(config.machine) == "pc" || machineBase(config.machine) == "virt") {
            config.machine = defaultMachineForArch(config.architecture);
        }
        if (config.cpuModel.trimmed().isEmpty() || config.cpuModel == "max" || config.cpuModel == "host") {
            config.cpuModel = normalizeArchitecture(config.architecture) == "m68k" ? "m68040" : "G3";
        }
        config.cpuCores = 1;
        config.tpm.enabled = false;
        config.networks.clear();
        config.graphics.openGl = false;
        if (config.graphics.adapter.startsWith("virtio", Qt::CaseInsensitive)) {
            config.graphics.adapter.clear();
        }
        if (config.graphics.vga.trimmed().isEmpty()) {
            config.graphics.vga = normalizeArchitecture(config.architecture) == "m68k" ? QString() : "VGA";
        }
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
    }
    if (config.networks.isEmpty() && !isLegacyPcGuest(config) && !isClassicMacOsGuest(config.guestOs)) {
        const QString guestOs = normalizedGuestOs(config.guestOs);
        if (guestOs.contains("windows") || guestOs.contains("linux") || guestOs.contains("macos")) {
            config.networks.append(NetworkConfig{});
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
    args << executableForArch(config.architecture)
         << "-name" << (config.name.trimmed().isEmpty() ? "VirtualWorld" : config.name.trimmed())
         << "-machine" << QString("%1,accel=%2").arg(config.machine, config.accelerator)
         << "-cpu" << (config.cpuModel.isEmpty() ? "max" : config.cpuModel)
         << "-smp" << QString::number(qMax(1, config.cpuCores))
         << "-m" << QString::number(qMax(minimumMemoryMb(config), config.memoryMb));

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
    if (!config.efi.codePath.isEmpty()) {
        args << "-drive" << "if=pflash,format=raw,readonly=on,file=" + config.efi.codePath;
    }
    if (!config.efi.varsPath.isEmpty()) {
        args << "-drive" << "if=pflash,format=raw,file=" + config.efi.varsPath;
    }
    bool hasUsbController = config.usb.controller == "usb";
    bool hasScsiController = false;
    for (int i = 0; i < config.disks.size(); ++i) {
        appendDiskArgs(args, config.disks[i], i, config, hasUsbController, hasScsiController);
    }
    for (const QString& cdrom : config.cdroms) {
        args << "-cdrom" << cdrom;
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
        const QString netId = "net" + QString::number(i);
        QStringList netParts;
        netParts << normalizeNetMode(net.mode) << "id=" + netId;
        if (!net.bridge.isEmpty()) netParts << "br=" + net.bridge;
        for (const QString& fwd : net.hostForwards) netParts << "hostfwd=" + fwd;
        args << "-netdev" << netParts.join(',');
        QStringList devParts;
        devParts << (net.model.isEmpty() ? "virtio-net-pci" : net.model) << "netdev=" + netId;
        if (!net.mac.isEmpty()) devParts << "mac=" + net.mac;
        args << "-device" << devParts.join(',');
    }
    if (!config.usb.controller.isEmpty()) {
        if (config.usb.controller == "usb") args << "-usb";
        else args << "-device" << config.usb.controller;
    }
    for (const QString& device : config.usb.devices) {
        args << "-device" << device;
    }
    if (!config.graphics.vga.isEmpty() && normalizeArchitecture(config.architecture) != "m68k") args << "-vga" << config.graphics.vga;
    const QString adapter = graphicsAdapterForConfig(config);
    if (!adapter.isEmpty()) args << "-device" << adapter;
    if (isClassicMacOsGuest(config.guestOs) && normalizeArchitecture(config.architecture) == "m68k") {
        args << "-audio" << "none";
    }
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
