#include "ConfigManager.hpp"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QJsonValue>
#include <QtGlobal>
#include <stdexcept>

namespace vw {

static QJsonObject diskToJson(const DiskConfig& disk) {
    return {
        {"path", disk.path},
        {"interface", disk.interfaceName},
        {"format", disk.format},
        {"readonly", disk.readonly},
        {"media", disk.media},
    };
}

static DiskConfig diskFromJson(const QJsonObject& object) {
    DiskConfig disk;
    disk.path = object.value("path").toString();
    disk.interfaceName = object.value("interface").toString("virtio");
    disk.format = object.value("format").toString();
    disk.readonly = object.value("readonly").toBool(false);
    disk.media = object.value("media").toString("disk");
    return disk;
}

static QJsonObject networkToJson(const NetworkConfig& network) {
    QJsonArray forwards;
    for (const QString& item : network.hostForwards) {
        forwards.append(item);
    }
    return {
        {"mode", network.mode},
        {"model", network.model},
        {"mac", network.mac},
        {"bridge", network.bridge},
        {"hostForwards", forwards},
    };
}

static NetworkConfig networkFromJson(const QJsonObject& object) {
    NetworkConfig network;
    network.mode = object.value("mode").toString("user");
    network.model = object.value("model").toString("virtio-net-pci");
    network.mac = object.value("mac").toString();
    network.bridge = object.value("bridge").toString();
    for (const QJsonValue& item : object.value("hostForwards").toArray()) {
        network.hostForwards << item.toString();
    }
    return network;
}

static QJsonArray stringListToJson(const QStringList& items) {
    QJsonArray array;
    for (const QString& item : items) {
        array.append(item);
    }
    return array;
}

static QStringList stringListFromJson(const QJsonArray& array) {
    QStringList out;
    for (const QJsonValue& item : array) {
        out << item.toString();
    }
    return out;
}

static QJsonObject vmToJson(const VmConfig& config) {
    QJsonArray disks;
    for (const DiskConfig& disk : config.disks) {
        disks.append(diskToJson(disk));
    }
    QJsonArray networks;
    for (const NetworkConfig& network : config.networks) {
        networks.append(networkToJson(network));
    }
    QJsonArray shares;
    for (const SharedDirectoryConfig& share : config.sharedDirectories) {
        shares.append(QJsonObject{
            {"path", share.path},
            {"tag", share.tag},
            {"securityModel", share.securityModel},
        });
    }
    return {
        {"schema", "virtualworld.vm.v1"},
        {"name", config.name},
        {"storageDir", config.storageDir},
        {"guestOs", config.guestOs},
        {"architecture", config.architecture},
        {"machine", config.machine},
        {"cpuModel", config.cpuModel},
        {"cpuCores", config.cpuCores},
        {"memoryMb", config.memoryMb},
        {"accelerator", config.accelerator},
        {"firmware", config.firmware},
        {"boot", QJsonObject{
            {"kernelPath", config.boot.kernelPath},
            {"initrdPath", config.boot.initrdPath},
            {"kernelAppend", config.boot.kernelAppend},
        }},
        {"tpm", QJsonObject{
            {"enabled", config.tpm.enabled},
            {"emulatorSocket", config.tpm.emulatorSocket},
        }},
        {"disks", disks},
        {"cdroms", stringListToJson(config.cdroms)},
        {"networks", networks},
        {"usb", QJsonObject{
            {"controller", config.usb.controller},
            {"devices", stringListToJson(config.usb.devices)},
        }},
        {"graphics", QJsonObject{
            {"adapter", config.graphics.adapter},
            {"display", config.graphics.display},
            {"vga", config.graphics.vga},
            {"ramMb", config.graphics.ramMb},
            {"openGl", config.graphics.openGl},
            {"dynamicResolution", config.graphics.dynamicResolution},
            {"autoResize", config.graphics.autoResize},
            {"retina", config.graphics.retina},
            {"renderer", config.graphics.renderer},
        }},
        {"gpuPassthrough", QJsonObject{
            {"enabled", config.gpuPassthrough.enabled},
            {"askOnStart", config.gpuPassthrough.askOnStart},
            {"keepVirtualDisplay", config.gpuPassthrough.keepVirtualDisplay},
            {"deviceId", config.gpuPassthrough.deviceId},
            {"name", config.gpuPassthrough.name},
            {"pciAddress", config.gpuPassthrough.pciAddress},
            {"backend", config.gpuPassthrough.backend},
        }},
        {"spice", QJsonObject{
            {"enabled", config.spice.enabled},
            {"port", config.spice.port},
            {"addr", config.spice.addr},
            {"disableTicketing", config.spice.disableTicketing},
        }},
        {"sharedDirectories", shares},
        {"efi", QJsonObject{
            {"codePath", config.efi.codePath},
            {"varsPath", config.efi.varsPath},
            {"secureBoot", config.efi.secureBoot},
        }},
        {"extraArgs", stringListToJson(config.extraArgs)},
        {"unsupportedArgs", stringListToJson(config.unsupportedArgs)},
        {"sourceFormat", config.sourceFormat},
    };
}

static VmConfig vmFromJson(const QJsonObject& object) {
    VmConfig config;
    config.name = object.value("name").toString(config.name);
    config.storageDir = object.value("storageDir").toString();
    config.guestOs = object.value("guestOs").toString(config.guestOs);
    config.architecture = normalizeArchitecture(object.value("architecture").toString(config.architecture));
    config.machine = object.value("machine").toString(defaultMachineForArch(config.architecture));
    config.cpuModel = object.value("cpuModel").toString(config.cpuModel);
    config.cpuCores = qMax(1, object.value("cpuCores").toInt(config.cpuCores));
    config.memoryMb = qMax(1, object.value("memoryMb").toInt(config.memoryMb));
    config.accelerator = object.value("accelerator").toString(config.accelerator);
    config.firmware = object.value("firmware").toString();
    const QJsonObject boot = object.value("boot").toObject();
    config.boot.kernelPath = boot.value("kernelPath").toString();
    config.boot.initrdPath = boot.value("initrdPath").toString();
    config.boot.kernelAppend = boot.value("kernelAppend").toString();
    const QJsonObject tpm = object.value("tpm").toObject();
    config.tpm.enabled = tpm.value("enabled").toBool(false);
    config.tpm.emulatorSocket = tpm.value("emulatorSocket").toString();
    for (const QJsonValue& item : object.value("disks").toArray()) {
        config.disks.append(diskFromJson(item.toObject()));
    }
    config.cdroms = stringListFromJson(object.value("cdroms").toArray());
    for (const QJsonValue& item : object.value("networks").toArray()) {
        config.networks.append(networkFromJson(item.toObject()));
    }
    const QJsonObject usb = object.value("usb").toObject();
    config.usb.controller = usb.value("controller").toString();
    config.usb.devices = stringListFromJson(usb.value("devices").toArray());
    const QJsonObject graphics = object.value("graphics").toObject();
    config.graphics.adapter = graphics.value("adapter").toString();
    config.graphics.display = graphics.value("display").toString();
    config.graphics.vga = graphics.value("vga").toString();
    config.graphics.ramMb = graphics.value("ramMb").toInt();
    config.graphics.openGl = graphics.value("openGl").toBool(false);
    config.graphics.dynamicResolution = graphics.value("dynamicResolution").toBool(true);
    config.graphics.autoResize = graphics.value("autoResize").toBool(true);
    config.graphics.retina = graphics.value("retina").toBool(true);
    config.graphics.renderer = graphics.value("renderer").toString("auto");
    const QJsonObject passthrough = object.value("gpuPassthrough").toObject();
    config.gpuPassthrough.enabled = passthrough.value("enabled").toBool(false);
    config.gpuPassthrough.askOnStart = passthrough.value("askOnStart").toBool(true);
    config.gpuPassthrough.keepVirtualDisplay = passthrough.value("keepVirtualDisplay").toBool(true);
    config.gpuPassthrough.deviceId = passthrough.value("deviceId").toString();
    config.gpuPassthrough.name = passthrough.value("name").toString();
    config.gpuPassthrough.pciAddress = passthrough.value("pciAddress").toString();
    config.gpuPassthrough.backend = passthrough.value("backend").toString();
    const QJsonObject spice = object.value("spice").toObject();
    config.spice.enabled = spice.value("enabled").toBool(false);
    config.spice.port = spice.value("port").toInt();
    config.spice.addr = spice.value("addr").toString();
    config.spice.disableTicketing = spice.value("disableTicketing").toBool(false);
    for (const QJsonValue& item : object.value("sharedDirectories").toArray()) {
        const QJsonObject shareObject = item.toObject();
        config.sharedDirectories.append(SharedDirectoryConfig{
            shareObject.value("path").toString(),
            shareObject.value("tag").toString("share"),
            shareObject.value("securityModel").toString("mapped-xattr"),
        });
    }
    const QJsonObject efi = object.value("efi").toObject();
    config.efi.codePath = efi.value("codePath").toString();
    config.efi.varsPath = efi.value("varsPath").toString();
    config.efi.secureBoot = efi.value("secureBoot").toBool(false);
    config.extraArgs = stringListFromJson(object.value("extraArgs").toArray());
    config.unsupportedArgs = stringListFromJson(object.value("unsupportedArgs").toArray());
    config.sourceFormat = object.value("sourceFormat").toString(config.sourceFormat);
    return config;
}

VmConfig ConfigManager::readVmConfig(const QString& path) const {
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        throw std::runtime_error(QString("Cannot read VM config: %1").arg(path).toStdString());
    }
    QJsonParseError parseError;
    const QJsonDocument document = QJsonDocument::fromJson(file.readAll(), &parseError);
    if (parseError.error != QJsonParseError::NoError || !document.isObject()) {
        throw std::runtime_error(QString("Invalid VM config JSON: %1").arg(parseError.errorString()).toStdString());
    }
    return vmFromJson(document.object());
}

bool ConfigManager::writeVmConfig(const QString& path, const VmConfig& config, QString* error) const {
    const QFileInfo info(path);
    if (!info.dir().exists() && !info.dir().mkpath(".")) {
        if (error) {
            *error = QString("Cannot create VM config directory: %1").arg(info.dir().absolutePath());
        }
        return false;
    }
    QFile file(path);
    if (!file.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        if (error) {
            *error = QString("Cannot write VM config: %1").arg(path);
        }
        return false;
    }
    file.write(QJsonDocument(vmToJson(config)).toJson(QJsonDocument::Indented));
    return true;
}

} // namespace vw
