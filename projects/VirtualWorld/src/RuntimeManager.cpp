#include "RuntimeManager.hpp"
#include "VmConfig.hpp"

#include <QCoreApplication>
#include <QDir>
#include <QFileInfo>
#include <QProcess>
#include <QStandardPaths>
#include <QtGlobal>
#include <utility>

namespace vw {

RuntimeManager::RuntimeManager(QString appDir)
    : appDir_(appDir.isEmpty() ? QCoreApplication::applicationDirPath() : std::move(appDir)),
      host_(detectHostProfile()) {}

QString RuntimeManager::platformRuntimeName() const {
    if (host_.family.contains("harmonyos", Qt::CaseInsensitive)
        || host_.family.contains("openharmony", Qt::CaseInsensitive)) {
        return "harmonyos";
    }
    return platformName(host_.platform);
}

QString RuntimeManager::hostArchFolder() const {
    if (host_.family == "macos-apple-silicon") {
        return "apple-silicon";
    }
    return host_.arch;
}

QString RuntimeManager::runtimeRoot() const {
    for (const QString& root : runtimeRoots()) {
        if (QFileInfo(root).isDir()) {
            return QFileInfo(root).absoluteFilePath();
        }
    }
    return QDir(appDir_).filePath("runtime/qemu");
}

QStringList RuntimeManager::runtimeRoots() const {
    QStringList roots;
    const QDir app(appDir_);

    const QString envRoot = QString::fromLocal8Bit(qgetenv("VIRTUALWORLD_QEMU_RUNTIME")).trimmed();
    if (!envRoot.isEmpty()) {
        roots << envRoot;
    }

    roots << app.filePath("runtime/qemu");
    roots << app.filePath("../Resources/runtime/qemu");
    roots << app.filePath("../Frameworks/runtime/qemu");
    roots << app.filePath("../../runtime/qemu");
    roots << app.filePath("../../../runtime/qemu");
    roots << QDir::current().filePath("runtime/qemu");
    roots.removeDuplicates();
    return roots;
}

QStringList RuntimeManager::runtimeBinDirs() const {
    QStringList dirs;
    const QString platformDir = platformRuntimeName();
    const QString archDir = hostArchFolder();
    for (const QString& root : runtimeRoots()) {
        if (root.trimmed().isEmpty()) {
            continue;
        }
        const QDir base(root);
        dirs << base.filePath(platformDir + "/" + archDir + "/bin");
        dirs << base.filePath(platformDir + "/" + archDir);
        dirs << base.filePath(platformDir + "/bin");
        dirs << base.filePath(platformDir);
        if (platformDir == "harmonyos") {
            dirs << base.filePath("linux/" + archDir + "/bin");
            dirs << base.filePath("linux/" + archDir);
            dirs << base.filePath("linux/bin");
            dirs << base.filePath("linux");
        }
    }
    dirs.removeDuplicates();
    return dirs;
}

QString RuntimeManager::findFirmwareFile(const QString& fileName) const {
    if (fileName.trimmed().isEmpty()) {
        return {};
    }
    const QString platformDir = platformRuntimeName();
    const QString archDir = hostArchFolder();
    QStringList dirs;
    for (const QString& root : runtimeRoots()) {
        if (root.trimmed().isEmpty()) {
            continue;
        }
        const QDir base(root);
        dirs << base.filePath(platformDir + "/" + archDir + "/share/qemu");
        dirs << base.filePath(platformDir + "/" + archDir + "/share");
        dirs << base.filePath(platformDir + "/share/qemu");
        dirs << base.filePath(platformDir + "/share");
        if (platformDir == "harmonyos") {
            dirs << base.filePath("linux/" + archDir + "/share/qemu");
            dirs << base.filePath("linux/" + archDir + "/share");
        }
    }
    dirs.removeDuplicates();
    for (const QString& dir : dirs) {
        const QString candidate = QDir(dir).filePath(fileName);
        const QFileInfo info(candidate);
        if (info.exists() && info.isFile()) {
            return info.absoluteFilePath();
        }
    }
    return {};
}

bool RuntimeManager::isExecutableHealthy(const QString& path) const {
    if (path.trimmed().isEmpty()) {
        return false;
    }
    QProcess process;
    process.start(path, {"--version"});
    if (!process.waitForFinished(8000)) {
        process.kill();
        process.waitForFinished(1000);
        return false;
    }
    return process.exitStatus() == QProcess::NormalExit && process.exitCode() == 0;
}

RuntimePath RuntimeManager::findExecutable(const QString& executableName) const {
    RuntimePath result;
    result.source = "missing";
#if defined(Q_OS_WIN)
    const QString exe = executableName.endsWith(".exe", Qt::CaseInsensitive) ? executableName : executableName + ".exe";
#else
    const QString exe = executableName;
#endif

    for (const QString& dir : runtimeBinDirs()) {
        const QString candidate = QDir(dir).filePath(exe);
        if (result.expectedPath.isEmpty()) {
            result.expectedPath = candidate;
        }
        const QFileInfo info(candidate);
        if (info.exists() && info.isFile() && info.isExecutable()) {
            if (isExecutableHealthy(candidate)) {
                result.executable = candidate;
                result.source = "runtime/qemu";
                result.available = true;
                return result;
            }
        }
    }

    const QString found = QStandardPaths::findExecutable(exe);
    if (!found.isEmpty() && isExecutableHealthy(found)) {
        result.executable = found;
        result.source = "PATH";
        result.available = true;
        return result;
    }

    result.executable = result.expectedPath;
    return result;
}

RuntimePath RuntimeManager::findSystemEmulator(const QString& guestArch) const {
    return findExecutable(executableForArch(guestArch));
}

RuntimePath RuntimeManager::findSystemSoftwareEmulator(const QString& variant) const {
    Q_UNUSED(variant);
    // NOTE: `variant` is intentionally ignored (deprecated-ignored). It is retained
    // only to keep this public signature stable for callers still mid-migration
    // (JoyCode / window code). The in-process engine serves every machine variant
    // through the same descriptor, so `variant` no longer selects or probes an
    // external binary; removing it now would only churn unrelated call sites.
    // Mini vMac runs IN-PROCESS via MiniVmacInProcessCore (libvwminivmac). There is
    // no external executable to locate and spawning one is forbidden on
    // HarmonyOS/OpenHarmony, so we never probe the filesystem for a minivmac-*
    // binary. Callers must not treat an external binary as a required dependency;
    // if one happens to exist (e.g. a developer's leftover build artifact) the
    // integration ignores it and uses the in-process engine instead.
    RuntimePath result;
    result.source = QStringLiteral("in-process-engine");
    result.available = true;
    result.executable.clear();
    result.expectedPath = QStringLiteral("in-process Mini vMac engine (libvwminivmac)");
    return result;
}

RuntimePath RuntimeManager::findImageTool() const {
    return findExecutable("qemu-img");
}

} // namespace vw
