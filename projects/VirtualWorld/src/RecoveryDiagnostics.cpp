#include "RecoveryDiagnostics.hpp"

#include "QemuEngine.hpp"
#include "RecoveryGuard.hpp"
#include "Translator.hpp"

#include <QFileInfo>

namespace vw {

static RecoveryDiagnostic diagnoseGuestBootFailure(const VmConfig& config,
                                                   const QString& startupDetail,
                                                   const QString& guestLabel) {
    QemuEngine engine;
    const QemuEngineInfo runtime = engine.detect(config.architecture);
    if (!runtime.systemEmulator.available || !runtime.imageTool.available) {
        return {RecoveryDiagnosticKind::Configuration,
                QString("%1 QEMU runtime unavailable: system=%2, qemu-img=%3")
                    .arg(guestLabel,
                         runtime.systemEmulator.available ? runtime.systemEmulator.executable : "missing",
                         runtime.imageTool.available ? runtime.imageTool.executable : "missing")};
    }

    const ConversionResult conversion = convertToQemu(config, detectHostProfile().platform);
    for (const ValidationIssue& issue : conversion.issues) {
        if (issue.level == "error") {
            return {RecoveryDiagnosticKind::Configuration, issue.message};
        }
    }

    bool hasWritableSystemDisk = false;
    for (const DiskConfig& disk : config.disks) {
        if (isRecoveryGuardDisk(disk) || disk.path.trimmed().isEmpty()) {
            continue;
        }
        const QFileInfo file(disk.path);
        if (!file.exists() || !file.isFile()) {
            return {RecoveryDiagnosticKind::Disk, "Disk image is missing or is not a regular file: " + disk.path};
        }
        if (disk.media == "disk" && !disk.readonly) {
            hasWritableSystemDisk = true;
        }
        QString checkDetail;
        if (disk.media == "disk" && !engine.checkDiskImage(disk.path, &checkDetail)) {
            return {RecoveryDiagnosticKind::Disk, checkDetail};
        }
    }
    if (!hasWritableSystemDisk) {
        return {RecoveryDiagnosticKind::Configuration,
                QString("No writable %1 system disk is attached.").arg(guestLabel)};
    }

    const QString startupLower = startupDetail.toLower();
    if (startupLower.contains("invalid device")
        || startupLower.contains("could not open")
        || startupLower.contains("no such file")
        || startupLower.contains("accelerator")
        || startupLower.contains("firmware")) {
        return {RecoveryDiagnosticKind::Configuration, startupDetail.left(1200)};
    }

    return {RecoveryDiagnosticKind::GuestSystemDamage, startupDetail.left(1200)};
}

RecoveryDiagnostic diagnoseWindowsBootFailure(const VmConfig& config, const QString& startupDetail) {
    return diagnoseGuestBootFailure(config, startupDetail, QStringLiteral("Windows"));
}

RecoveryDiagnostic diagnoseLinuxBootFailure(const VmConfig& config, const QString& startupDetail) {
    return diagnoseGuestBootFailure(config, startupDetail, QStringLiteral("Linux"));
}

RecoveryDiagnostic diagnoseMacosBootFailure(const VmConfig& config, const QString& startupDetail) {
    if (!isSupportedMacosGuest(config)) {
        return {RecoveryDiagnosticKind::Configuration,
                QStringLiteral("macOS recovery is only available when VirtualWorld is running on macOS.")};
    }
    if (config.macRecovery.ipswPath.trimmed().isEmpty()
        || !QFileInfo::exists(config.macRecovery.ipswPath)) {
        return {RecoveryDiagnosticKind::Configuration,
                QStringLiteral("No verified Apple IPSW recovery image is attached.")};
    }
    if (!config.macRecovery.sourceVerified) {
        return {RecoveryDiagnosticKind::Configuration,
                QStringLiteral("The macOS IPSW source was not verified as an Apple HTTPS endpoint.")};
    }
    return diagnoseGuestBootFailure(config, startupDetail, QStringLiteral("macOS"));
}
} // namespace vw
