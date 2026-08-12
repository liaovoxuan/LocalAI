#pragma once

#include <QString>
#include <QStringList>
#include <QVector>

namespace vw {

enum class HostPlatform {
    MacOS,
    Windows,
    Linux,
};

struct HostProfile {
    HostPlatform platform = HostPlatform::Linux;
    QString family = "linux";
    QString arch = "x86_64";
    QString name = "Linux";
};

struct HostGpuInfo {
    QString id;
    QString name;
    QString pciAddress;
    QString bus;
    QString type;
    int estimatedYear = 0;
    int linkSpeedGbps = 0;
    bool integrated = false;
    bool appleSiliconBuiltIn = false;
    bool directPcie = false;
    bool passthroughCandidate = false;
    QString reason;
};

HostProfile detectHostProfile();
QString platformName(HostPlatform platform);
QString preferredAccelerator(const QString& guestArch, HostPlatform platform);
bool isNativeVirtualizationGuest(const QString& guestArch, const HostProfile& host);
bool isHardwareAcceleratorAvailable(const QString& accelerator, const HostProfile& host);
QStringList acceleratorCandidates(const QString& guestArch, const HostProfile& host);
QString selectAccelerator(const QString& guestArch, const QString& requested, const HostProfile& host);
QString hostHardwareSerial();
bool hostHasDiscreteGpu();
QVector<HostGpuInfo> detectHostGpus();

} // namespace vw
