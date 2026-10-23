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
    qint64 sizeMb = 0;
    QString role;
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

// Optional QEMU audio device. Disabled by default to preserve existing VM
// command lines; when enabled, Translator emits a backend and the
// system-appropriate device (or connects q800's built-in ASC).
struct AudioConfig {
    bool enabled = false;
    QString driver = "auto";
    QString model;
    QString id = "audio0";
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

struct RecoveryGuardConfig {
    bool enabled = false;
    bool quarantineNextStart = false;
    int consecutiveBootFailures = 0;
    QString lastFailureReason;
};

struct MacRecoveryConfig {
    QString ipswPath;
    QString sourceUrl;
    bool sourceVerified = false;
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
    // memoryMb is retained for older configuration files. memoryKb preserves
    // sub-megabyte legacy PC memory sizes such as 640 KB.
    qint64 memoryKb = 0;
    QString accelerator = "auto";
    QString firmware;
    BootConfig boot;
    TpmConfig tpm;
    QVector<DiskConfig> disks;
    QStringList cdroms;
    QVector<NetworkConfig> networks;
    AudioConfig audio;
    UsbConfig usb;
    GraphicsConfig graphics;
    GpuPassthroughConfig gpuPassthrough;
    SpiceConfig spice;
    QVector<SharedDirectoryConfig> sharedDirectories;
    EfiConfig efi;
    RecoveryGuardConfig recoveryGuard;
    MacRecoveryConfig macRecovery;
    QStringList extraArgs;
    QStringList unsupportedArgs;
    QString sourceFormat = "qemu";
    // "qemu" remains the default. System Software uses a separate
    // Mini vMac-compatible runtime and never enters the QEMU translator.
    QString backend = "qemu";
    QString systemSoftwareVersion;
    QString systemSoftwareRomPath;
    QString systemSoftwareMachine;
    QString systemSoftwareRomType;
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
qint64 effectiveMemoryKb(const VmConfig& config);
void setMemoryKb(VmConfig& config, qint64 value);
qint64 parseMemoryKb(const QString& value, qint64 fallbackKb = 4096 * 1024);
QString formatQemuMemory(qint64 valueKb);
int parseMemoryMb(const QString& value, int fallback = 4096);
int parseSmp(const QString& value, int fallback = 2);

} // namespace vw
