#include "UtmParser.hpp"
#include "QemuParser.hpp"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QRegularExpression>
#include <QSysInfo>
#include <QTextStream>
#include <QtGlobal>
#include <stdexcept>

namespace vw {

static QString readTextFile(const QString& path) {
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) {
        throw std::runtime_error(QString("Cannot read %1").arg(path).toStdString());
    }
    return QString::fromUtf8(file.readAll());
}

static QString valueForKey(const QString& xml, const QString& key) {
    const QString escapedKey = QRegularExpression::escape(key);
    QRegularExpression rx(
        "<key>\\s*" + escapedKey + "\\s*</key>\\s*"
        "(?:<string>(.*?)</string>|<integer>(.*?)</integer>|<real>(.*?)</real>|<(true|false)\\s*/>)",
        QRegularExpression::DotMatchesEverythingOption | QRegularExpression::CaseInsensitiveOption);
    const auto match = rx.match(xml);
    if (!match.hasMatch()) {
        return {};
    }
    for (int i = 1; i <= 4; ++i) {
        const QString value = match.captured(i);
        if (!value.isEmpty()) {
            return value.trimmed();
        }
    }
    return {};
}

static QStringList arrayDictBlocks(const QString& xml, const QString& key) {
    QStringList blocks;
    QRegularExpression arrayRx(
        "<key>\\s*" + QRegularExpression::escape(key) + "\\s*</key>\\s*<array>(.*?)</array>",
        QRegularExpression::DotMatchesEverythingOption | QRegularExpression::CaseInsensitiveOption);
    const auto arrayMatch = arrayRx.match(xml);
    if (!arrayMatch.hasMatch()) {
        return blocks;
    }
    QRegularExpression dictRx("<dict>(.*?)</dict>", QRegularExpression::DotMatchesEverythingOption | QRegularExpression::CaseInsensitiveOption);
    auto it = dictRx.globalMatch(arrayMatch.captured(1));
    while (it.hasNext()) {
        blocks << it.next().captured(1);
    }
    return blocks;
}

static QString resolvePath(const QString& value, const QString& baseDir) {
    if (value.isEmpty()) {
        return value;
    }
    QFileInfo info(value);
    if (info.isAbsolute() || baseDir.isEmpty()) {
        return value;
    }
    QDir base(baseDir);
    if (QDir(base.filePath("Data")).exists()) {
        return QDir(base.filePath("Data")).filePath(value);
    }
    return base.filePath(value);
}

static QString normalizedDiskInterface(const QString& value) {
    const QString text = value.trimmed().toLower();
    if (text == "virtio" || text == "virtio-blk") return "virtio";
    if (text == "ide") return "ide";
    if (text == "sata") return "sata";
    if (text == "scsi") return "scsi";
    if (text == "nvme") return "nvme";
    if (text == "usb") return "usb";
    return text.isEmpty() ? "virtio" : text;
}

static QString guessFormat(const QString& path) {
    const QString suffix = QFileInfo(path).suffix().toLower();
    if (suffix == "qcow2") return "qcow2";
    if (suffix == "img" || suffix == "raw") return "raw";
    if (suffix == "vhd" || suffix == "vhdx") return suffix;
    return {};
}

static bool looksLikeCdrom(const QString& block, const QString& path) {
    const QString type = valueForKey(block, "ImageType").toLower();
    if (type == "cd" || type == "cdrom") {
        return true;
    }
    const QString suffix = QFileInfo(path).suffix().toLower();
    return suffix == "iso";
}

static bool keyIsTrue(const QString& xml, const QString& key) {
    return valueForKey(xml, key).toLower() == "true";
}

VmConfig parseUtmPlistXml(const QString& xml, const QString& baseDir) {
    if (!xml.contains("<plist") && !xml.contains("<dict")) {
        throw std::runtime_error("UTM plist must be XML text. Binary plist needs to be exported as XML first.");
    }

    VmConfig config;
    config.sourceFormat = "utm";
    config.name = valueForKey(xml, "Name");
    if (config.name.isEmpty()) {
        config.name = "VirtualWorld Imported";
    }
    config.architecture = normalizeArchitecture(valueForKey(xml, "Architecture"));
    config.machine = valueForKey(xml, "Target");
    if (config.machine.isEmpty()) {
        config.machine = defaultMachineForArch(config.architecture);
    }
    config.cpuModel = valueForKey(xml, "CPU");
    if (config.cpuModel.isEmpty()) {
        config.cpuModel = valueForKey(xml, "CPUModel");
    }
    if (config.cpuModel.isEmpty()) {
        config.cpuModel = "max";
    }
    bool ok = false;
    int cores = valueForKey(xml, "CPUCount").toInt(&ok);
    if (!ok) {
        cores = valueForKey(xml, "CPUCores").toInt(&ok);
    }
    if (!ok || cores < 1) {
        config.cpuCores = 2;
    } else {
        config.cpuCores = cores;
    }
    config.memoryMb = parseMemoryMb(valueForKey(xml, "MemorySize"));
    if (keyIsTrue(xml, "Hypervisor")) {
        config.accelerator = "auto";
    } else if (xml.contains("<key>Hypervisor</key>")) {
        config.accelerator = "tcg";
    }

    const QStringList flags = arrayDictBlocks(xml, "CPUFlagsAdd");
    Q_UNUSED(flags);
    QRegularExpression flagsArrayRx("<key>\\s*CPUFlagsAdd\\s*</key>\\s*<array>(.*?)</array>", QRegularExpression::DotMatchesEverythingOption);
    const auto flagsMatch = flagsArrayRx.match(xml);
    if (flagsMatch.hasMatch()) {
        QRegularExpression stringRx("<string>(.*?)</string>", QRegularExpression::DotMatchesEverythingOption);
        auto it = stringRx.globalMatch(flagsMatch.captured(1));
        while (it.hasNext()) {
            const QString flag = it.next().captured(1).trimmed();
            if (!flag.isEmpty() && !config.cpuModel.contains("+" + flag)) {
                config.cpuModel += ",+" + flag;
            }
        }
    }

    for (const QString& block : arrayDictBlocks(xml, "Drive") + arrayDictBlocks(xml, "Drives")) {
        QString path = valueForKey(block, "ImagePath");
        if (path.isEmpty()) path = valueForKey(block, "ImageName");
        if (path.isEmpty()) path = valueForKey(block, "Path");
        const QString type = valueForKey(block, "ImageType").toLower();
        if (type == "bios" || type == "firmware") {
            config.firmware = resolvePath(path, baseDir);
            continue;
        }
        if (path.isEmpty() && !looksLikeCdrom(block, path)) {
            continue;
        }
        if (looksLikeCdrom(block, path)) {
            if (!path.isEmpty()) {
                config.cdroms << resolvePath(path, baseDir);
            }
            continue;
        }
        DiskConfig disk;
        disk.path = resolvePath(path, baseDir);
        disk.interfaceName = normalizedDiskInterface(valueForKey(block, "Interface"));
        disk.format = guessFormat(path);
        disk.readonly = keyIsTrue(block, "ReadOnly") || keyIsTrue(block, "Readonly");
        config.disks.append(disk);
    }

    for (const QString& block : arrayDictBlocks(xml, "Display")) {
        QString hardware = valueForKey(block, "Hardware");
        if (!hardware.isEmpty()) {
            config.graphics.adapter = hardware;
        }
        config.graphics.openGl = hardware.contains("-gl", Qt::CaseInsensitive)
            || keyIsTrue(block, "OpenGL")
            || keyIsTrue(block, "RendererOpenGL");
        if (block.contains("<key>DynamicResolution</key>")) {
            config.graphics.dynamicResolution = keyIsTrue(block, "DynamicResolution");
            config.graphics.autoResize = config.graphics.dynamicResolution;
        }
        if (block.contains("<key>NativeResolution</key>") || block.contains("<key>Retina</key>")) {
            config.graphics.retina = keyIsTrue(block, "NativeResolution") || keyIsTrue(block, "Retina");
        }
        const QString backend = valueForKey(block, "RendererBackend");
        if (!backend.isEmpty()) {
            config.graphics.display = backend.toLower();
        }
        break;
    }

    if (xml.contains("<key>Network</key>")) {
        NetworkConfig network;
        QString mode = valueForKey(xml, "NetworkMode");
        network.mode = mode.isEmpty() ? "user" : mode.toLower().replace("shared", "user");
        network.model = valueForKey(xml, "NetworkCard");
        if (network.model.isEmpty()) {
            network.model = config.architecture.startsWith("ppc") ? "rtl8139" : "virtio-net-pci";
        }
        network.mac = valueForKey(xml, "MACAddress");
        config.networks.append(network);
    }

    if (keyIsTrue(xml, "UsbSharing") || keyIsTrue(xml, "USBTablet") || keyIsTrue(xml, "USBKeyboard")) {
        config.usb.controller = "qemu-xhci";
        if (keyIsTrue(xml, "USBTablet")) config.usb.devices << "usb-tablet";
        if (keyIsTrue(xml, "USBKeyboard")) config.usb.devices << "usb-kbd";
    }

    if (keyIsTrue(xml, "UEFIBoot") || keyIsTrue(xml, "UEFI")) {
        config.efi.secureBoot = keyIsTrue(xml, "SecureBoot") || keyIsTrue(xml, "UEFISecureBoot");
    }
    config.efi.codePath = resolvePath(valueForKey(xml, "UEFICodePath"), baseDir);
    config.efi.varsPath = resolvePath(valueForKey(xml, "UEFIVariablesPath"), baseDir);

    const QString sharePath = valueForKey(xml, "DirectorySharePath");
    if (!sharePath.isEmpty()) {
        config.sharedDirectories.append(SharedDirectoryConfig{sharePath});
    }

    QRegularExpression addArgsRx("<key>\\s*AdditionalArguments\\s*</key>\\s*<array>(.*?)</array>", QRegularExpression::DotMatchesEverythingOption);
    const auto addArgsMatch = addArgsRx.match(xml);
    if (addArgsMatch.hasMatch()) {
        QRegularExpression stringRx("<string>(.*?)</string>", QRegularExpression::DotMatchesEverythingOption);
        auto it = stringRx.globalMatch(addArgsMatch.captured(1));
        while (it.hasNext()) {
            config.extraArgs << splitCommandLine(it.next().captured(1));
        }
    }

    config.architecture = normalizeArchitecture(config.architecture);
    return config;
}

VmConfig parseUtmSource(const QString& source) {
    QFileInfo info(source);
    if (info.exists()) {
        if (info.isDir() && info.suffix().toLower() == "utm") {
            const QString path = QDir(source).filePath("config.plist");
            return parseUtmPlistXml(readTextFile(path), source);
        }
        if (info.isFile()) {
            const QString text = readTextFile(source);
            return parseUtmPlistXml(text, info.dir().absolutePath());
        }
    }
    return parseUtmPlistXml(source);
}

static QString boolTag(bool value) {
    return value ? "<true/>" : "<false/>";
}

static QString utmInterface(const QString& value) {
    const QString lower = value.toLower();
    if (lower == "ide") return "IDE";
    if (lower == "sata") return "SATA";
    if (lower == "scsi") return "SCSI";
    if (lower == "nvme") return "NVMe";
    if (lower == "usb") return "USB";
    return "VirtIO";
}

static bool shouldEnableVirtualization(const VmConfig& config) {
    const QString arch = normalizeArchitecture(config.architecture);
    if (arch == "ppc" || arch == "ppc64" || arch == "riscv64") {
        return false;
    }
    return arch == normalizeArchitecture(QSysInfo::currentCpuArchitecture());
}

QString renderUtmPlist(const VmConfig& input, const QString& guestOs, QVector<ValidationIssue>* issues) {
    VmConfig config = input;
    config.architecture = normalizeArchitecture(config.architecture);
    if (machineBase(config.machine).isEmpty()) {
        config.machine = defaultMachineForArch(config.architecture);
    }

    const bool virt = shouldEnableVirtualization(config);
    const QString guestOsLower = guestOs.toLower();
    const bool linuxGuest = guestOsLower.contains("linux");
    const bool enableOpenGl = config.graphics.openGl && !linuxGuest
        && config.architecture != "ppc" && config.architecture != "ppc64" && config.architecture != "riscv64";
    if (issues) {
        issues->append({"warning", "utm_save_name", "UTM 输出已固定使用包内 config.plist。替换旧虚拟机时请备份原 config.plist 后再覆盖。"});
        if (virt) {
            issues->append({"warning", "utm_virtualization_enabled", "客户机架构与本机一致，已加入 UTM/QEMU 虚拟化加速配置。"});
        } else {
            issues->append({"warning", "utm_virtualization_disabled", "客户机架构与本机不一致或不适合硬件虚拟化，已使用 QEMU 解释执行。"});
        }
        if (linuxGuest) {
            issues->append({"warning", "linux_opengl_disabled", "Linux 客户机未启用 OpenGL 加速，以避免部分发行版显示崩溃。"});
        }
        if (config.graphics.openGl && !enableOpenGl) {
            issues->append({"warning", "opengl_disabled_for_utm", "OpenGL 图形加速因客户机系统或架构兼容性风险未写入 UTM 配置。"});
        }
    }

    QString xml;
    QTextStream out(&xml);
    out << "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n";
    out << "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n";
    out << "<plist version=\"1.0\">\n<dict>\n";
    out << "\t<key>Backend</key><string>QEMU</string>\n";
    out << "\t<key>ConfigurationVersion</key><integer>4</integer>\n";
    out << "\t<key>Information</key><dict><key>Name</key><string>" << xmlEscape(config.name) << "</string></dict>\n";
    out << "\t<key>System</key><dict>\n";
    out << "\t\t<key>Architecture</key><string>" << xmlEscape(config.architecture) << "</string>\n";
    out << "\t\t<key>Target</key><string>" << xmlEscape(machineBase(config.machine)) << "</string>\n";
    out << "\t\t<key>CPU</key><string>" << xmlEscape(config.cpuModel) << "</string>\n";
    out << "\t\t<key>CPUCount</key><integer>" << qMax(1, config.cpuCores) << "</integer>\n";
    out << "\t\t<key>MemorySize</key><integer>" << qMax(128, config.memoryMb) << "</integer>\n";
    out << "\t</dict>\n";
    out << "\t<key>QEMU</key><dict>\n";
    out << "\t\t<key>Hypervisor</key>" << boolTag(virt) << "\n";
    out << "\t\t<key>UEFIBoot</key>" << boolTag(config.architecture == "x86_64" || config.architecture == "aarch64" || config.efi.secureBoot) << "\n";
    if (!config.extraArgs.isEmpty() || !config.unsupportedArgs.isEmpty()) {
        out << "\t\t<key>AdditionalArguments</key><array>\n";
        for (const QString& arg : config.extraArgs + config.unsupportedArgs) {
            out << "\t\t\t<string>" << xmlEscape(arg) << "</string>\n";
        }
        out << "\t\t</array>\n";
    }
    out << "\t</dict>\n";
    out << "\t<key>Display</key><array><dict>\n";
    QString displayHardware = config.graphics.adapter.isEmpty() ? "virtio-vga" : config.graphics.adapter;
    if (enableOpenGl && displayHardware == "virtio-vga") {
        displayHardware = "virtio-vga-gl";
    }
    if ((config.architecture == "ppc" || config.architecture == "ppc64") && displayHardware.startsWith("virtio", Qt::CaseInsensitive)) {
        displayHardware = "VGA";
    }
    out << "\t\t<key>Hardware</key><string>" << xmlEscape(displayHardware) << "</string>\n";
    out << "\t\t<key>OpenGL</key>" << boolTag(enableOpenGl) << "\n";
    out << "\t\t<key>DynamicResolution</key>" << boolTag(config.graphics.dynamicResolution || config.graphics.autoResize) << "\n";
    out << "\t\t<key>NativeResolution</key>" << boolTag(config.graphics.retina) << "\n";
    out << "\t</dict></array>\n";
    out << "\t<key>Input</key><dict><key>USBTablet</key><true/><key>USBKeyboard</key><true/></dict>\n";
    out << "\t<key>Network</key><array>\n";
    QVector<NetworkConfig> nets = config.networks;
    if (nets.isEmpty()) {
        nets.append(NetworkConfig{});
    }
    for (const auto& net : nets) {
        out << "\t\t<dict><key>NetworkMode</key><string>Shared</string><key>NetworkCard</key><string>"
            << xmlEscape(net.model.isEmpty() ? "virtio-net-pci" : net.model)
            << "</string>";
        if (!net.mac.isEmpty()) {
            out << "<key>MACAddress</key><string>" << xmlEscape(net.mac) << "</string>";
        }
        out << "</dict>\n";
    }
    out << "\t</array>\n";
    out << "\t<key>Drive</key><array>\n";
    for (const auto& disk : config.disks) {
        out << "\t\t<dict><key>ImagePath</key><string>" << xmlEscape(disk.path)
            << "</string><key>ImageType</key><string>" << (disk.media == "cdrom" ? "CD" : "Disk") << "</string><key>Interface</key><string>"
            << utmInterface(disk.interfaceName) << "</string><key>ReadOnly</key>"
            << boolTag(disk.readonly || disk.media == "cdrom") << "</dict>\n";
    }
    for (const QString& cdrom : config.cdroms) {
        out << "\t\t<dict><key>ImagePath</key><string>" << xmlEscape(cdrom)
            << "</string><key>ImageType</key><string>CD</string><key>Interface</key><string>USB</string><key>ReadOnly</key><true/></dict>\n";
    }
    out << "\t</array>\n";
    if (!config.sharedDirectories.isEmpty()) {
        out << "\t<key>Sharing</key><dict><key>DirectoryShareMode</key><string>Single</string><key>DirectorySharePath</key><string>"
            << xmlEscape(config.sharedDirectories.first().path) << "</string></dict>\n";
    } else {
        out << "\t<key>Sharing</key><dict><key>DirectoryShareMode</key><string>None</string></dict>\n";
    }
    out << "</dict>\n</plist>\n";
    return xml;
}

bool writeUtmPackage(const QString& packagePath, const QString& plistXml, QString* error) {
    QString path = packagePath;
    if (!path.endsWith(".utm", Qt::CaseInsensitive)) {
        path += ".utm";
    }
    QDir dir(path);
    if (!dir.exists() && !QDir().mkpath(path)) {
        if (error) *error = "Cannot create UTM package.";
        return false;
    }
    QFile file(dir.filePath("config.plist"));
    if (!file.open(QIODevice::WriteOnly | QIODevice::Text | QIODevice::Truncate)) {
        if (error) *error = file.errorString();
        return false;
    }
    file.write(plistXml.toUtf8());
    return true;
}

} // namespace vw
