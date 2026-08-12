#include "QemuParser.hpp"

#include <QFileInfo>
#include <stdexcept>

namespace vw {

QStringList splitCommandLine(const QString& command) {
    QString text = command;
    text.replace("\\\n", " ");
    text.replace("^\n", " ");

    QStringList tokens;
    QString buffer;
    QChar quote;
    bool escaped = false;
    for (const QChar ch : text) {
        if (escaped) {
            buffer += ch;
            escaped = false;
            continue;
        }
        if (ch == '\\') {
            escaped = true;
            continue;
        }
        if (!quote.isNull()) {
            if (ch == quote) {
                quote = QChar();
            } else {
                buffer += ch;
            }
            continue;
        }
        if (ch == '\'' || ch == '"') {
            quote = ch;
            continue;
        }
        if (ch.isSpace()) {
            if (!buffer.isEmpty()) {
                tokens << buffer;
                buffer.clear();
            }
            continue;
        }
        buffer += ch;
    }
    if (!buffer.isEmpty()) {
        tokens << buffer;
    }
    return tokens;
}

QString commandFromScriptText(const QString& text) {
    QStringList lines;
    for (QString line : text.split('\n')) {
        line = line.trimmed();
        if (line.isEmpty() || line.startsWith('#') || line.startsWith("REM ", Qt::CaseInsensitive)) {
            continue;
        }
        if (line.endsWith('\\') || line.endsWith('^')) {
            line.chop(1);
        }
        lines << line.trimmed();
    }
    return lines.join(' ');
}

static void parseMachine(const QString& value, VmConfig& config) {
    const QStringList parts = splitOptions(value);
    if (!parts.isEmpty()) {
        config.machine = parts.first();
    }
    for (const QString& part : parts.mid(1)) {
        if (part.startsWith("accel=")) {
            config.accelerator = part.mid(QString("accel=").size()).split(':').first();
        }
    }
}

static DiskConfig parseDrive(const QString& value) {
    const auto options = parseOptions(value);
    DiskConfig disk;
    disk.path = options.value("file", value);
    disk.media = options.value("media", "disk");
    disk.interfaceName = options.value("if", options.value("interface", "virtio"));
    disk.format = options.value("format");
    disk.readonly = QString(options.value("readonly")).toLower().contains("on")
        || QString(options.value("readonly")).toLower() == "true"
        || QString(options.value("readonly")).toLower() == "yes";
    return disk;
}

static NetworkConfig parseNetwork(const QString& value, bool nic) {
    const QStringList parts = splitOptions(value);
    const auto options = parseOptions(value);
    NetworkConfig network;
    network.mode = parts.value(0, "user");
    network.model = nic ? options.value("model", "virtio-net-pci") : "virtio-net-pci";
    network.mac = options.value("mac");
    network.bridge = options.value("br", options.value("bridge"));
    for (const QString& part : parts) {
        if (part.startsWith("hostfwd=")) {
            network.hostForwards << part.mid(QString("hostfwd=").size());
        }
    }
    return network;
}

static void parseDevice(const QString& value, VmConfig& config) {
    const QString name = splitOptions(value).value(0, value);
    const auto options = parseOptions(value);
    if (name == "virtio-net-pci" || name == "e1000" || name == "rtl8139" || name == "vmxnet3") {
        if (config.networks.isEmpty()) {
            config.networks.append(NetworkConfig{});
        }
        config.networks.last().model = name;
        if (options.contains("mac")) {
            config.networks.last().mac = options.value("mac");
        }
    } else if (name == "qemu-xhci" || name == "nec-usb-xhci" || name == "usb-ehci") {
        config.usb.controller = name;
    } else if (name.startsWith("usb-")) {
        config.usb.devices << value;
    } else if (name == "virtio-gpu-pci" || name == "virtio-gpu-gl-pci"
               || name == "virtio-vga" || name == "virtio-vga-gl"
               || name == "qxl-vga" || name == "VGA" || name == "ramfb") {
        config.graphics.adapter = name;
        if (name.contains("-gl")) {
            config.graphics.openGl = true;
        }
    } else {
        config.unsupportedArgs << "-device" << value;
    }
}

static SpiceConfig parseSpice(const QString& value) {
    const auto options = parseOptions(value);
    SpiceConfig spice;
    spice.enabled = true;
    spice.port = options.value("port").toInt();
    spice.addr = options.value("addr");
    spice.disableTicketing = options.value("disable-ticketing").toLower() == "on"
        || options.value("disable-ticketing").toLower() == "true";
    return spice;
}

static SharedDirectoryConfig parseVirtfs(const QString& value) {
    const auto options = parseOptions(value);
    SharedDirectoryConfig share;
    share.path = options.value("path");
    share.tag = options.value("mount_tag", options.value("tag", "share"));
    share.securityModel = options.value("security_model", "mapped-xattr");
    return share;
}

VmConfig parseQemuCommand(const QString& command) {
    const QStringList tokens = splitCommandLine(commandFromScriptText(command));
    if (tokens.isEmpty()) {
        throw std::runtime_error("QEMU command is empty.");
    }

    VmConfig config;
    config.sourceFormat = "qemu";
    const QString exe = QFileInfo(tokens.first()).fileName();
    if (exe.contains("x86_64")) config.architecture = "x86_64";
    else if (exe.contains("i386")) config.architecture = "i386";
    else if (exe.contains("aarch64")) config.architecture = "aarch64";
    else if (exe.endsWith("-arm")) config.architecture = "arm";
    else if (exe.contains("ppc64")) config.architecture = "ppc64";
    else if (exe.contains("ppc")) config.architecture = "ppc";
    else if (exe.contains("riscv64")) config.architecture = "riscv64";

    for (int i = 1; i < tokens.size();) {
        const QString key = tokens.value(i);
        const QString value = tokens.value(i + 1);
        if (key == "-m" && !value.isEmpty()) {
            config.memoryMb = parseMemoryMb(value);
            i += 2;
        } else if (key == "-smp" && !value.isEmpty()) {
            config.cpuCores = parseSmp(value);
            i += 2;
        } else if (key == "-cpu" && !value.isEmpty()) {
            config.cpuModel = value;
            i += 2;
        } else if ((key == "-machine" || key == "-M") && !value.isEmpty()) {
            parseMachine(value, config);
            i += 2;
        } else if (key == "-accel" && !value.isEmpty()) {
            config.accelerator = value.split(',').first();
            i += 2;
        } else if (key == "-drive" && !value.isEmpty()) {
            DiskConfig disk = parseDrive(value);
            config.disks.append(disk);
            i += 2;
        } else if (key == "-cdrom" && !value.isEmpty()) {
            config.cdroms << value;
            i += 2;
        } else if (key == "-hda" && !value.isEmpty()) {
            config.disks.append(DiskConfig{value, "ide"});
            i += 2;
        } else if (key == "-bios" && !value.isEmpty()) {
            config.firmware = value;
            i += 2;
        } else if (key == "-pflash" && !value.isEmpty()) {
            if (config.efi.codePath.isEmpty()) config.efi.codePath = value;
            else if (config.efi.varsPath.isEmpty()) config.efi.varsPath = value;
            else config.unsupportedArgs << key << value;
            i += 2;
        } else if (key == "-netdev" && !value.isEmpty()) {
            config.networks.append(parseNetwork(value, false));
            i += 2;
        } else if ((key == "-nic" || key == "-net") && !value.isEmpty()) {
            config.networks.append(parseNetwork(value, true));
            i += 2;
        } else if (key == "-device" && !value.isEmpty()) {
            parseDevice(value, config);
            i += 2;
        } else if (key == "-usb") {
            config.usb.controller = config.usb.controller.isEmpty() ? "usb" : config.usb.controller;
            i += 1;
        } else if (key == "-usbdevice" && !value.isEmpty()) {
            config.usb.controller = config.usb.controller.isEmpty() ? "usb" : config.usb.controller;
            config.usb.devices << (value == "keyboard" ? "usb-kbd" : "usb-" + value);
            i += 2;
        } else if (key == "-vga" && !value.isEmpty()) {
            config.graphics.vga = value;
            i += 2;
        } else if (key == "-display" && !value.isEmpty()) {
            config.graphics.display = value;
            config.graphics.openGl = config.graphics.openGl || value.contains("gl=on", Qt::CaseInsensitive);
            config.graphics.autoResize = value.contains("zoom-to-fit=on", Qt::CaseInsensitive)
                || value.contains("resize=on", Qt::CaseInsensitive);
            i += 2;
        } else if (key == "-spice" && !value.isEmpty()) {
            config.spice = parseSpice(value);
            i += 2;
        } else if (key == "-virtfs" && !value.isEmpty()) {
            config.sharedDirectories.append(parseVirtfs(value));
            i += 2;
        } else if (key.startsWith('-') && !value.isEmpty() && !value.startsWith('-')) {
            config.unsupportedArgs << key << value;
            i += 2;
        } else {
            config.unsupportedArgs << key;
            i += 1;
        }
    }
    config.architecture = normalizeArchitecture(config.architecture);
    if (config.machine.isEmpty()) {
        config.machine = defaultMachineForArch(config.architecture);
    }
    return config;
}

} // namespace vw
