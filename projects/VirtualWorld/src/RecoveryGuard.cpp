#include "RecoveryGuard.hpp"

#include "SupportTrust.hpp"
#include "Platform.hpp"

#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QStandardPaths>

namespace vw {

bool isSupportedWindowsGuest(const VmConfig& config) {
    const QString guest = config.guestOs.trimmed().toLower();
    if (!guest.contains("windows")) {
        return false;
    }
    return !guest.contains("old windows")
        && !guest.contains("windows 3")
        && !guest.contains("windows 95")
        && !guest.contains("windows 98")
        && !guest.contains("windows me")
        && !guest.contains("windows millennium");
}

bool isSupportedLinuxGuest(const VmConfig& config) {
    return config.guestOs.trimmed().toLower().contains("linux");
}

bool isSupportedMacosGuest(const VmConfig& config) {
    const QString guest = config.guestOs.trimmed().toLower();
    return detectHostProfile().platform == HostPlatform::MacOS
        && guest.contains("macos")
        && !guest.contains("classic");
}

bool isRecoveryGuardDisk(const DiskConfig& disk) {
    return disk.role.compare("recovery-guard", Qt::CaseInsensitive) == 0
        || isLinuxRecoveryGuardDisk(disk)
        || isMacosRecoveryGuardDisk(disk);
}

bool isLinuxRecoveryGuardDisk(const DiskConfig& disk) {
    return disk.role.compare("linux-recovery-guard", Qt::CaseInsensitive) == 0;
}

bool isMacosRecoveryGuardDisk(const DiskConfig& disk) {
    return disk.role.compare("macos-recovery-guard", Qt::CaseInsensitive) == 0;
}

QString recoveryGuardDiskFileName() {
    return "VirtualWorld-RecoveryGuard.qcow2";
}

QString sharedRecoveryGuardDiskPath() {
    QString appData = QStandardPaths::writableLocation(QStandardPaths::AppDataLocation);
    if (appData.isEmpty()) {
        appData = QDir(QDir::homePath()).filePath("Library/Application Support/VirtualWorld");
    }
    return QFileInfo(QDir(appData).filePath("Recovery/" + recoveryGuardDiskFileName())).absoluteFilePath();
}

QString sharedRecoveryGuardManifestPath() {
    return QFileInfo(QDir(QFileInfo(sharedRecoveryGuardDiskPath()).dir())
                         .filePath("VirtualWorld-RecoveryGuard.manifest.json"))
        .absoluteFilePath();
}

QString linuxRecoveryGuardDiskFileName() {
    return "VirtualWorld-LinuxSupportDisk.qcow2";
}

QString sharedLinuxRecoveryGuardDiskPath() {
    QString appData = QStandardPaths::writableLocation(QStandardPaths::AppDataLocation);
    if (appData.isEmpty()) {
        appData = QDir(QDir::homePath()).filePath("Library/Application Support/VirtualWorld");
    }
    return QFileInfo(QDir(appData).filePath("Recovery/" + linuxRecoveryGuardDiskFileName())).absoluteFilePath();
}

QString sharedLinuxRecoveryGuardManifestPath() {
    return QFileInfo(QDir(QFileInfo(sharedLinuxRecoveryGuardDiskPath()).dir())
                         .filePath("VirtualWorld-LinuxSupportDisk.manifest.json"))
        .absoluteFilePath();
}

QString macosRecoveryGuardDiskFileName() {
    return "VirtualWorld-macOSSupportDisk.qcow2";
}

QString sharedMacosRecoveryGuardDiskPath() {
    QString appData = QStandardPaths::writableLocation(QStandardPaths::AppDataLocation);
    if (appData.isEmpty()) {
        appData = QDir(QDir::homePath()).filePath("Library/Application Support/VirtualWorld");
    }
    return QFileInfo(QDir(appData).filePath("Recovery/" + macosRecoveryGuardDiskFileName())).absoluteFilePath();
}

QString sharedMacosRecoveryGuardManifestPath() {
    return QFileInfo(QDir(QFileInfo(sharedMacosRecoveryGuardDiskPath()).dir())
                         .filePath("VirtualWorld-macOSSupportDisk.manifest.json"))
        .absoluteFilePath();
}

namespace {

QString bundledSupportDirectory() {
    const QDir executableDirectory(QCoreApplication::applicationDirPath());
#ifdef Q_OS_MACOS
    return executableDirectory.absoluteFilePath("../Resources/support");
#else
    return executableDirectory.filePath("support");
#endif
}

} // namespace

bool ensureTrustedSupportDisk(const QString& diskFileName,
                              const QString& manifestFileName,
                              const QString& diskPath,
                              const QString& manifestPath,
                              QString* error) {
    const QFileInfo diskInfo(diskPath);
    if (!diskInfo.dir().exists() && !diskInfo.dir().mkpath(".")) {
        if (error) *error = "无法创建 Support Disk 目录：" + diskInfo.dir().absolutePath();
        return false;
    }

    const SupportDiskTrustResult installedTrust =
        (QFileInfo::exists(diskPath) && QFileInfo::exists(manifestPath))
            ? verifySupportDiskManifest(manifestPath, diskPath)
            : SupportDiskTrustResult{false, QStringLiteral("共享 Support Disk 文件不完整")};

    if (!installedTrust.trusted) {
        const QDir bundled(bundledSupportDirectory());
        const QString bundledDisk = bundled.filePath(diskFileName);
        const QString bundledManifest = bundled.filePath(manifestFileName);
        const SupportDiskTrustResult bundledTrust =
            verifySupportDiskManifest(bundledManifest, bundledDisk);
        if (!bundledTrust.trusted) {
            if (error) *error = "内置 Support Disk 不可信：" + bundledTrust.reason;
            return false;
        }

        const QString diskTempPath = diskPath + ".new";
        const QString manifestTempPath = manifestPath + ".new";
        const QString diskBackupPath = diskPath + ".bak";
        const QString manifestBackupPath = manifestPath + ".bak";
        QFile::remove(diskTempPath);
        QFile::remove(manifestTempPath);
        QFile::remove(diskBackupPath);
        QFile::remove(manifestBackupPath);
        if (!QFile::copy(bundledDisk, diskTempPath)
            || !QFile::copy(bundledManifest, manifestTempPath)) {
            QFile::remove(diskTempPath);
            QFile::remove(manifestTempPath);
            if (error) *error = "无法复制 Support Disk 资源：" + bundledDisk;
            return false;
        }

        const SupportDiskTrustResult stagedTrust =
            verifySupportDiskManifest(manifestTempPath, diskTempPath);
        if (!stagedTrust.trusted) {
            QFile::remove(diskTempPath);
            QFile::remove(manifestTempPath);
            if (error) *error = "复制后的 Support Disk 资源校验失败：" + stagedTrust.reason;
            return false;
        }

        const bool hadDisk = QFileInfo::exists(diskPath);
        const bool hadManifest = QFileInfo::exists(manifestPath);
        if (hadDisk && !QFile::rename(diskPath, diskBackupPath)) {
            QFile::remove(diskTempPath);
            QFile::remove(manifestTempPath);
            if (error) *error = "无法备份现有 Support Disk：" + diskPath;
            return false;
        }
        if (hadManifest && !QFile::rename(manifestPath, manifestBackupPath)) {
            if (hadDisk) {
                QFile::rename(diskBackupPath, diskPath);
            }
            QFile::remove(diskTempPath);
            QFile::remove(manifestTempPath);
            if (error) *error = "无法备份现有 Support Disk 清单：" + manifestPath;
            return false;
        }

        auto restoreBackups = [&]() {
            QFile::remove(diskPath);
            QFile::remove(manifestPath);
            if (QFileInfo::exists(diskBackupPath)) {
                QFile::rename(diskBackupPath, diskPath);
            }
            if (QFileInfo::exists(manifestBackupPath)) {
                QFile::rename(manifestBackupPath, manifestPath);
            }
        };

        if (!QFile::rename(diskTempPath, diskPath)) {
            restoreBackups();
            QFile::remove(diskTempPath);
            QFile::remove(manifestTempPath);
            if (error) *error = "无法安装 Support Disk 资源：" + diskPath;
            return false;
        }
        if (!QFile::rename(manifestTempPath, manifestPath)) {
            QFile::remove(diskPath);
            restoreBackups();
            QFile::remove(diskTempPath);
            QFile::remove(manifestTempPath);
            if (error) *error = "无法安装 Support Disk 资源：" + diskPath;
            return false;
        }
        QFile::remove(diskBackupPath);
        QFile::remove(manifestBackupPath);
    }

    const SupportDiskTrustResult finalTrust =
        verifySupportDiskManifest(manifestPath, diskPath);
    if (!finalTrust.trusted) {
        if (error) *error = "共享 Support Disk 校验失败：" + finalTrust.reason;
        return false;
    }
    QFile::setPermissions(diskPath,
                          QFileDevice::ReadOwner | QFileDevice::ReadUser
                              | QFileDevice::ReadGroup | QFileDevice::ReadOther);
    QFile::setPermissions(manifestPath,
                          QFileDevice::ReadOwner | QFileDevice::ReadUser
                              | QFileDevice::ReadGroup | QFileDevice::ReadOther);
    return true;
}

bool ensureTrustedSharedRecoveryGuardDisk(QString* error) {
    return ensureTrustedSupportDisk(
        recoveryGuardDiskFileName(),
        "VirtualWorld-RecoveryGuard.manifest.json",
        sharedRecoveryGuardDiskPath(),
        sharedRecoveryGuardManifestPath(),
        error);
}

bool ensureTrustedSharedLinuxRecoveryGuardDisk(QString* error) {
    return ensureTrustedSupportDisk(
        linuxRecoveryGuardDiskFileName(),
        "VirtualWorld-LinuxSupportDisk.manifest.json",
        sharedLinuxRecoveryGuardDiskPath(),
        sharedLinuxRecoveryGuardManifestPath(),
        error);
}

bool ensureTrustedSharedMacosRecoveryGuardDisk(QString* error) {
    return ensureTrustedSupportDisk(
        macosRecoveryGuardDiskFileName(),
        "VirtualWorld-macOSSupportDisk.manifest.json",
        sharedMacosRecoveryGuardDiskPath(),
        sharedMacosRecoveryGuardManifestPath(),
        error);
}

} // namespace vw
