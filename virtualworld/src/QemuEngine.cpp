#include "QemuEngine.hpp"
#include "Translator.hpp"

#include <QDir>
#include <QFileInfo>
#include <QProcess>
#include <QProcessEnvironment>
#include <QtGlobal>
#include <utility>

namespace vw {

static void prependEnvPath(QProcessEnvironment& env, const QString& key, const QString& path) {
    if (path.trimmed().isEmpty()) {
        return;
    }
#if defined(Q_OS_WIN)
    const QString separator = ";";
#else
    const QString separator = ":";
#endif
    const QString current = env.value(key);
    env.insert(key, current.isEmpty() ? path : path + separator + current);
}

static QProcessEnvironment runtimeEnvironment(const RuntimeManager& runtime, const RuntimePath& executable) {
    QProcessEnvironment env = QProcessEnvironment::systemEnvironment();
    QStringList dirs = runtime.runtimeBinDirs();
    const QString executableDir = QFileInfo(executable.executable).absolutePath();
    if (!executableDir.isEmpty()) {
        dirs.prepend(executableDir);
    }
    dirs.removeDuplicates();
    for (const QString& dir : dirs) {
        const QFileInfo info(dir);
        if (info.exists() && info.isDir()) {
            prependEnvPath(env, "PATH", info.absoluteFilePath());
#if defined(Q_OS_MACOS)
            prependEnvPath(env, "DYLD_LIBRARY_PATH", info.absoluteFilePath());
#elif !defined(Q_OS_WIN)
            prependEnvPath(env, "LD_LIBRARY_PATH", info.absoluteFilePath());
#endif
        }
    }
    return env;
}

static bool pathExistsIfSet(const QString& path, const QString& label, QString* error) {
    if (path.trimmed().isEmpty()) {
        return true;
    }
    const QFileInfo info(path);
    if (info.exists()) {
        return true;
    }
    if (error) {
        *error = QString("%1 does not exist: %2").arg(label, path);
    }
    return false;
}

static bool validateStartFiles(const VmConfig& config, QString* error) {
    if (!pathExistsIfSet(config.firmware, "Firmware", error)) return false;
    if (!pathExistsIfSet(config.boot.kernelPath, "Kernel", error)) return false;
    if (!pathExistsIfSet(config.boot.initrdPath, "Initrd", error)) return false;
    if (!pathExistsIfSet(config.efi.codePath, "EFI code", error)) return false;
    if (!pathExistsIfSet(config.efi.varsPath, "EFI variables", error)) return false;
    for (const DiskConfig& disk : config.disks) {
        if (!pathExistsIfSet(disk.path, "Disk image", error)) return false;
    }
    for (const QString& cdrom : config.cdroms) {
        if (!pathExistsIfSet(cdrom, "CD/DVD image", error)) return false;
    }
    return true;
}

QemuEngine::QemuEngine(RuntimeManager runtime)
    : runtime_(std::move(runtime)) {}

QemuEngineInfo QemuEngine::detect(const QString& guestArch) const {
    return {runtime_.findSystemEmulator(guestArch), runtime_.findImageTool()};
}

VmConfig QemuEngine::normaliseConfig(const VmConfig& config) const {
    return convertToQemu(config, detectHostProfile().platform).config;
}

QStringList QemuEngine::buildArguments(const VmConfig& config) const {
    QStringList parts = buildQemuCommandParts(normaliseConfig(config));
    if (!parts.isEmpty()) {
        parts.removeFirst();
    }
    return parts;
}

QString QemuEngine::renderCommand(const VmConfig& config) const {
    const HostPlatform target = detectHostProfile().platform;
    return renderQemuCommand(normaliseConfig(config), target);
}

bool QemuEngine::createQcow2(const QString& imagePath, qint64 sizeMb, QString* error) const {
    const QemuEngineInfo info = detect("x86_64");
    if (!info.canCreateImages()) {
        if (error) {
            *error = QString("qemu-img not found. Put QEMU under %1 or add qemu-img to PATH.")
                         .arg(info.imageTool.expectedPath);
        }
        return false;
    }

    const QFileInfo target(imagePath);
    if (!target.dir().exists() && !target.dir().mkpath(".")) {
        if (error) {
            *error = QString("Cannot create image directory: %1").arg(target.dir().absolutePath());
        }
        return false;
    }

    const QString size = QString::number(qMax<qint64>(1, sizeMb)) + "M";
    QProcess process;
    process.start(info.imageTool.executable, {"create", "-f", "qcow2", imagePath, size});
    if (!process.waitForFinished(60000) || process.exitStatus() != QProcess::NormalExit || process.exitCode() != 0) {
        if (error) {
            *error = QString::fromUtf8(process.readAllStandardError()).trimmed();
            if (error->isEmpty()) {
                *error = "qemu-img failed to create qcow2 image.";
            }
        }
        return false;
    }
    return true;
}

bool QemuEngine::prepareProcess(QProcess& process, const VmConfig& config, const QStringList& extraArgs, QString* error) const {
    const VmConfig normalized = normaliseConfig(config);
    const QemuEngineInfo info = detect(normalized.architecture);
    if (!info.canRunVm()) {
        if (error) {
            *error = QString("QEMU runtime not found. Put QEMU under %1 or add qemu-system-* to PATH.")
                         .arg(info.systemEmulator.expectedPath);
        }
        return false;
    }
    if (!validateStartFiles(normalized, error)) {
        return false;
    }
    QStringList args = buildArguments(normalized);
    args << extraArgs;
    process.setProgram(info.systemEmulator.executable);
    process.setArguments(args);
    process.setWorkingDirectory(QFileInfo(info.systemEmulator.executable).absolutePath());
    process.setProcessEnvironment(runtimeEnvironment(runtime_, info.systemEmulator));
    return true;
}

bool QemuEngine::startVm(const VmConfig& config, qint64* processId, QString* error) const {
    QProcess process;
    if (!prepareProcess(process, config, {}, error)) {
        return false;
    }
    qint64 pid = 0;
    const bool ok = process.startDetached(&pid);
    if (!ok) {
        if (error) {
            *error = "QEMU failed to start.";
        }
        return false;
    }
    if (processId) {
        *processId = pid;
    }
    return true;
}

bool QemuEngine::stopVm(qint64 processId, QString* error) const {
    if (processId <= 0) {
        if (error) {
            *error = "Invalid QEMU process id.";
        }
        return false;
    }
#if defined(Q_OS_WIN)
    const int code = QProcess::execute("taskkill", {"/PID", QString::number(processId), "/T", "/F"});
#else
    const int code = QProcess::execute("kill", {QString::number(processId)});
#endif
    if (code != 0) {
        if (error) {
            *error = QString("Failed to stop QEMU process %1.").arg(processId);
        }
        return false;
    }
    return true;
}

bool QemuEngine::startTest(const QString& guestArch, QString* output, QString* error) const {
    const QemuEngineInfo info = detect(guestArch);
    if (!info.canRunVm()) {
        if (error) {
            *error = QString("QEMU runtime not found. Expected: %1").arg(info.systemEmulator.expectedPath);
        }
        return false;
    }
    QProcess process;
    process.setProcessEnvironment(runtimeEnvironment(runtime_, info.systemEmulator));
    process.start(info.systemEmulator.executable, {"--version"});
    if (!process.waitForFinished(10000) || process.exitStatus() != QProcess::NormalExit || process.exitCode() != 0) {
        if (error) {
            *error = QString::fromUtf8(process.readAllStandardError()).trimmed();
            if (error->isEmpty()) {
                *error = "QEMU runtime test failed.";
            }
        }
        return false;
    }
    if (output) {
        *output = QString::fromUtf8(process.readAllStandardOutput()).trimmed();
    }
    return true;
}

QemuEngineInfo findQemuEngine(const QString& guestArch) {
    return QemuEngine().detect(guestArch);
}

bool startQemuVm(const VmConfig& config, QString* error) {
    return QemuEngine().startVm(config, nullptr, error);
}

} // namespace vw
