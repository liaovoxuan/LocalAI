#include "RuntimeManager.hpp"
#include "VmConfig.hpp"

#include <QCoreApplication>
#include <QDir>
#include <QFileInfo>
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
            result.executable = candidate;
            result.source = "runtime/qemu";
            result.available = true;
            return result;
        }
    }

    const QString found = QStandardPaths::findExecutable(exe);
    if (!found.isEmpty()) {
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

RuntimePath RuntimeManager::findImageTool() const {
    return findExecutable("qemu-img");
}

} // namespace vw
