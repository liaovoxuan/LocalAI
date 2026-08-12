#include "Platform.hpp"
#include "VmConfig.hpp"

#include <QFileInfo>
#include <QProcess>
#include <QRegularExpression>
#include <QSysInfo>
#include <QtGlobal>

namespace vw {

namespace {

int estimateGpuYear(const QString& name) {
    const QString lower = name.toLower();
    QRegularExpressionMatch rtx = QRegularExpression("rtx\\s*([2345])\\d{3}").match(lower);
    if (rtx.hasMatch()) {
        const int gen = rtx.captured(1).toInt();
        if (gen >= 5) return 2024;
        if (gen == 4) return 2022;
        if (gen == 3) return 2020;
        return 2018;
    }
    if (lower.contains("rx 7") || lower.contains("rx7")) return 2022;
    if (lower.contains("rx 6") || lower.contains("rx6")) return 2020;
    if (lower.contains("arc")) return 2022;
    if (lower.contains("m1") || lower.contains("m2") || lower.contains("m3") || lower.contains("m4")) return 2020;
    return 2018;
}

int estimateLinkSpeedGbps(const QString& text) {
    const QString lower = text.toLower();
    QRegularExpression explicitGbps("(\\d+(?:\\.\\d+)?)\\s*gt/s");
    const QRegularExpressionMatch match = explicitGbps.match(lower);
    if (match.hasMatch()) {
        return qRound(match.captured(1).toDouble());
    }
    if (lower.contains("thunderbolt 4") || lower.contains("usb4")) return 40;
    if (lower.contains("thunderbolt 3")) return 40;
    if (lower.contains("pcie")) return 16;
    return 0;
}

bool looksIntegrated(const QString& name) {
    const QString lower = name.toLower();
    return lower.contains("integrated")
        || lower.contains("iris")
        || lower.contains("uhd graphics")
        || lower.contains("hd graphics")
        || lower.contains("apple m")
        || lower.contains("apple gpu");
}

void finalizeGpu(HostGpuInfo& gpu, const HostProfile& host, const QString& raw = {}) {
    gpu.estimatedYear = gpu.estimatedYear > 0 ? gpu.estimatedYear : estimateGpuYear(gpu.name);
    gpu.linkSpeedGbps = gpu.linkSpeedGbps > 0 ? gpu.linkSpeedGbps : estimateLinkSpeedGbps(raw + " " + gpu.bus);
    gpu.integrated = gpu.integrated || looksIntegrated(gpu.name);
    gpu.appleSiliconBuiltIn = host.family == "macos-apple-silicon" && gpu.integrated;
    gpu.directPcie = gpu.directPcie || gpu.bus.toLower().contains("pcie") || !gpu.pciAddress.isEmpty();
    const int required = gpu.estimatedYear >= 2020 ? 20 : 10;
    if (gpu.appleSiliconBuiltIn) {
        gpu.passthroughCandidate = false;
        gpu.reason = "Apple Silicon 内建 GPU 不允许直通，避免影响宿主系统。";
    } else if (gpu.integrated) {
        gpu.passthroughCandidate = false;
        gpu.reason = "核显/集显通常由宿主系统占用，不建议直通。";
    } else if (gpu.directPcie || gpu.linkSpeedGbps >= required) {
        gpu.passthroughCandidate = true;
        gpu.reason = "满足直通候选条件。";
    } else {
        gpu.passthroughCandidate = false;
        gpu.reason = QString("链路速度不足或未知：需要至少 %1Gbps。").arg(required);
    }
}

} // namespace

HostProfile detectHostProfile() {
    HostProfile profile;
    profile.arch = normalizeArchitecture(QSysInfo::currentCpuArchitecture());

#if defined(Q_OS_MACOS)
    profile.platform = HostPlatform::MacOS;
    profile.family = profile.arch == "aarch64" ? "macos-apple-silicon" : "macos-intel";
    profile.name = "macOS";
#elif defined(Q_OS_WIN)
    profile.platform = HostPlatform::Windows;
    profile.family = "windows";
    profile.name = "Windows";
#else
    profile.platform = HostPlatform::Linux;
    profile.family = "linux";
    profile.name = "Linux";
    const QByteArray product = QSysInfo::prettyProductName().toLower().toUtf8();
    const QByteArray env = qgetenv("OHOS_SDK_HOME") + qgetenv("HARMONYOS_SDK_HOME") + qgetenv("DEVECO_SDK_HOME");
    const QByteArray marker = product + env.toLower();
    if (marker.contains("openharmony") || marker.contains("ohos")) {
        profile.family = "openharmony-linux-compatible";
        profile.name = "OpenHarmony (Linux-compatible mode)";
    } else if (marker.contains("harmonyos") || marker.contains("harmony os")) {
        profile.family = "harmonyos-linux-compatible";
        profile.name = "HarmonyOS (Linux-compatible mode)";
    } else if (!product.isEmpty()) {
        profile.name = QString::fromUtf8(product);
    }
#endif
    return profile;
}

QString platformName(HostPlatform platform) {
    switch (platform) {
    case HostPlatform::MacOS:
        return "macos";
    case HostPlatform::Windows:
        return "windows";
    case HostPlatform::Linux:
    default:
        return "linux";
    }
}

QString preferredAccelerator(const QString& guestArchValue, HostPlatform platform) {
    HostProfile host = detectHostProfile();
    host.platform = platform;
    return selectAccelerator(guestArchValue, "auto", host);
}

bool isNativeVirtualizationGuest(const QString& guestArchValue, const HostProfile& host) {
    const QString guestArch = normalizeArchitecture(guestArchValue);
    const QString hostArch = normalizeArchitecture(host.arch);
    if (guestArch == "ppc" || guestArch == "ppc64" || guestArch == "riscv64") {
        return false;
    }
    if (guestArch == hostArch) {
        return true;
    }
    if ((guestArch == "x86_64" && hostArch == "i386") || (guestArch == "i386" && hostArch == "x86_64")) {
        return true;
    }
    if ((guestArch == "aarch64" && hostArch == "arm") || (guestArch == "arm" && hostArch == "aarch64")) {
        return true;
    }
    return false;
}

bool isHardwareAcceleratorAvailable(const QString& acceleratorValue, const HostProfile& host) {
    const QString accelerator = acceleratorValue.trimmed().toLower();
    if (accelerator == "tcg") {
        return true;
    }
    if (accelerator == "hvf") {
        return host.platform == HostPlatform::MacOS;
    }
    if (accelerator == "whpx") {
        return host.platform == HostPlatform::Windows;
    }
    if (accelerator == "kvm") {
        if (host.platform != HostPlatform::Linux) {
            return false;
        }
        const QFileInfo kvm("/dev/kvm");
        return kvm.exists() && kvm.isReadable();
    }
    return false;
}

QStringList acceleratorCandidates(const QString& guestArch, const HostProfile& host) {
    QStringList candidates;
    if (!isNativeVirtualizationGuest(guestArch, host)) {
        return {"tcg"};
    }
    switch (host.platform) {
    case HostPlatform::MacOS:
        candidates << "hvf";
        break;
    case HostPlatform::Windows:
        candidates << "whpx";
        break;
    case HostPlatform::Linux:
    default:
        candidates << "kvm";
        break;
    }
    candidates << "tcg";
    candidates.removeDuplicates();
    return candidates;
}

QString selectAccelerator(const QString& guestArch, const QString& requestedValue, const HostProfile& host) {
    const QString requested = requestedValue.trimmed().toLower();
    if (!requested.isEmpty() && requested != "auto") {
        if (requested == "tcg") {
            return "tcg";
        }
        if (isNativeVirtualizationGuest(guestArch, host) && isHardwareAcceleratorAvailable(requested, host)) {
            return requested;
        }
        return "tcg";
    }
    for (const QString& candidate : acceleratorCandidates(guestArch, host)) {
        if (isHardwareAcceleratorAvailable(candidate, host)) {
            return candidate;
        }
    }
    return "tcg";
}

QString hostHardwareSerial() {
#if defined(Q_OS_MACOS)
    QProcess process;
    process.start("ioreg", {"-rd1", "-c", "IOPlatformExpertDevice"});
    if (!process.waitForFinished(3000) || process.exitStatus() != QProcess::NormalExit) {
        return {};
    }
    const QString output = QString::fromUtf8(process.readAllStandardOutput());
    const QString marker = "\"IOPlatformSerialNumber\" = \"";
    const int start = output.indexOf(marker);
    if (start < 0) {
        return {};
    }
    const int valueStart = start + marker.size();
    const int valueEnd = output.indexOf('"', valueStart);
    if (valueEnd <= valueStart) {
        return {};
    }
    return output.mid(valueStart, valueEnd - valueStart).trimmed();
#else
    return {};
#endif
}

bool hostHasDiscreteGpu() {
#if defined(Q_OS_MACOS)
    QProcess process;
    process.start("system_profiler", {"SPDisplaysDataType", "-detailLevel", "mini"});
    if (!process.waitForFinished(2500) || process.exitStatus() != QProcess::NormalExit) {
        return false;
    }
    const QString output = QString::fromUtf8(process.readAllStandardOutput()).toLower();
    return output.contains("bus: pcie") || output.contains("amd radeon") || output.contains("nvidia") || output.contains("intel arc");
#elif defined(Q_OS_WIN)
    QProcess process;
    process.start("wmic", {"path", "win32_VideoController", "get", "Name,AdapterRAM"});
    if (!process.waitForFinished(2500) || process.exitStatus() != QProcess::NormalExit) {
        return false;
    }
    const QString output = QString::fromUtf8(process.readAllStandardOutput()).toLower();
    return output.contains("nvidia") || output.contains("radeon") || output.contains("rx ") || output.contains("rtx ") || output.contains("gtx ") || output.contains("intel arc");
#else
    QProcess process;
    process.start("sh", {"-c", "command -v lspci >/dev/null 2>&1 && lspci | grep -Ei 'vga|3d|display' || true"});
    if (!process.waitForFinished(2500)) {
        return false;
    }
    const QString output = QString::fromUtf8(process.readAllStandardOutput()).toLower();
    return output.contains("nvidia") || output.contains("radeon") || output.contains("amd/ati") || output.contains("intel arc");
#endif
}

QVector<HostGpuInfo> detectHostGpus() {
    QVector<HostGpuInfo> gpus;
    const HostProfile host = detectHostProfile();
#if defined(Q_OS_MACOS)
    QProcess process;
    process.start("system_profiler", {"SPDisplaysDataType", "-detailLevel", "full"});
    if (process.waitForFinished(3500) && process.exitStatus() == QProcess::NormalExit) {
        const QString output = QString::fromUtf8(process.readAllStandardOutput());
        const QStringList blocks = output.split("\n\n", Qt::SkipEmptyParts);
        int index = 0;
        for (const QString& block : blocks) {
            if (!block.contains("Chipset Model:", Qt::CaseInsensitive)) {
                continue;
            }
            HostGpuInfo gpu;
            gpu.id = "gpu" + QString::number(index++);
            const QRegularExpressionMatch name = QRegularExpression("Chipset Model:\\s*([^\\n]+)").match(block);
            const QRegularExpressionMatch bus = QRegularExpression("Bus:\\s*([^\\n]+)").match(block);
            const QRegularExpressionMatch pci = QRegularExpression("PCIe Lane Width:\\s*([^\\n]+)").match(block);
            gpu.name = name.hasMatch() ? name.captured(1).trimmed() : "GPU";
            gpu.bus = bus.hasMatch() ? bus.captured(1).trimmed() : QString();
            if (pci.hasMatch()) gpu.bus += " PCIe " + pci.captured(1).trimmed();
            finalizeGpu(gpu, host, block);
            gpus.append(gpu);
        }
    }
#elif defined(Q_OS_WIN)
    QProcess process;
    process.start("powershell", {"-NoProfile", "-Command", "Get-CimInstance Win32_VideoController | Select-Object Name,PNPDeviceID | ConvertTo-Csv -NoTypeInformation"});
    if (process.waitForFinished(3500) && process.exitStatus() == QProcess::NormalExit) {
        const QStringList lines = QString::fromUtf8(process.readAllStandardOutput()).split('\n', Qt::SkipEmptyParts);
        int index = 0;
        for (int i = 1; i < lines.size(); ++i) {
            const QString line = lines[i].trimmed();
            if (line.isEmpty()) continue;
            const QStringList parts = line.split("\",\"");
            HostGpuInfo gpu;
            gpu.id = "gpu" + QString::number(index++);
            gpu.name = parts.value(0).remove('"').trimmed();
            gpu.pciAddress = parts.value(1).remove('"').trimmed();
            gpu.bus = gpu.pciAddress.contains("PCI", Qt::CaseInsensitive) ? "PCIe" : QString();
            finalizeGpu(gpu, host, line);
            gpus.append(gpu);
        }
    }
#else
    QProcess process;
    process.start("sh", {"-c", "command -v lspci >/dev/null 2>&1 && lspci -D | grep -Ei 'vga|3d|display' || true"});
    if (process.waitForFinished(3500)) {
        const QStringList lines = QString::fromUtf8(process.readAllStandardOutput()).split('\n', Qt::SkipEmptyParts);
        int index = 0;
        for (const QString& line : lines) {
            HostGpuInfo gpu;
            gpu.id = "gpu" + QString::number(index++);
            const int firstSpace = line.indexOf(' ');
            gpu.pciAddress = firstSpace > 0 ? line.left(firstSpace).trimmed() : QString();
            gpu.name = firstSpace > 0 ? line.mid(firstSpace + 1).trimmed() : line.trimmed();
            gpu.bus = gpu.pciAddress.isEmpty() ? QString() : "PCIe";
            finalizeGpu(gpu, host, line);
            gpus.append(gpu);
        }
    }
#endif
    return gpus;
}

} // namespace vw
