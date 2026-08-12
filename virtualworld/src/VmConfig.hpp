#pragma once

#include <QString>
#include <QStringList>
#include <QVector>
#include <QMap>

namespace vw {

struct DiskConfig {
    QString path;
    QString interfaceName = "virtio";
    QString format;
    bool readonly = false;
    QString media = "disk";
};

struct BootConfig {
    QString kernelPath;
    QString initrdPath;
    QString kernelAppend;
};

struct TpmConfig {
    bool enabled = false;
    QString emulatorSocket;
};

struct NetworkConfig {
    QString mode = "user";
    QString model = "virtio-net-pci";
    QString mac;
    QString bridge;
    QStringList hostForwards;
};

struct UsbConfig {
    QString controller;
    QStringList devices;
};

struct GraphicsConfig {
    QString adapter;
    QString display;
    QString vga;
    int ramMb = 0;
    bool openGl = false;
    bool dynamicResolution = true;
    bool autoResize = true;
    bool retina = true;
    QString renderer = "auto";
};

struct GpuPassthroughConfig {
    bool enabled = false;
    bool askOnStart = true;
    bool keepVirtualDisplay = true;
    QString deviceId;
    QString name;
    QString pciAddress;
    QString backend;
};

struct SpiceConfig {
    bool enabled = false;
    int port = 0;
    QString addr;
    bool disableTicketing = false;
};

struct SharedDirectoryConfig {
    QString path;
    QString tag = "share";
    QString securityModel = "mapped-xattr";
};

struct EfiConfig {
    QString codePath;
    QString varsPath;
    bool secureBoot = false;
};

struct ValidationIssue {
    QString level;
    QString code;
    QString message;
};

struct VmConfig {
    QString name = "VirtualWorld VM";
    QString storageDir;
    QString guestOs = "generic";
    QString architecture = "x86_64";
    QString machine = "q35";
    QString cpuModel = "max";
    int cpuCores = 2;
    int memoryMb = 4096;
    QString accelerator = "auto";
    QString firmware;
    BootConfig boot;
    TpmConfig tpm;
    QVector<DiskConfig> disks;
    QStringList cdroms;
    QVector<NetworkConfig> networks;
    UsbConfig usb;
    GraphicsConfig graphics;
    GpuPassthroughConfig gpuPassthrough;
    SpiceConfig spice;
    QVector<SharedDirectoryConfig> sharedDirectories;
    EfiConfig efi;
    QStringList extraArgs;
    QStringList unsupportedArgs;
    QString sourceFormat = "qemu";
};

struct ConversionResult {
    QString text;
    VmConfig config;
    QVector<ValidationIssue> issues;

    bool hasErrors() const {
        for (const auto& issue : issues) {
            if (issue.level == "error") {
                return true;
            }
        }
        return false;
    }
};

QString normalizeArchitecture(QString value);
QString defaultMachineForArch(const QString& arch);
QString machineBase(const QString& machine);
QString executableForArch(const QString& arch);
QString shellQuote(const QString& value);
QString xmlEscape(const QString& value);
QStringList splitOptions(const QString& value);
QMap<QString, QString> parseOptions(const QString& value);
int parseMemoryMb(const QString& value, int fallback = 4096);
int parseSmp(const QString& value, int fallback = 2);

} // namespace vw
