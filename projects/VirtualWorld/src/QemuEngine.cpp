#include "QemuEngine.hpp"
#include "Translator.hpp"
#include "SystemSoftwareEngine.hpp"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QProcess>
#include <QStandardPaths>
#include <QProcessEnvironment>
#include <QTemporaryDir>
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
        const QDir binDir(executableDir);
        dirs.prepend(binDir.filePath("../lib"));
        dirs.prepend(binDir.filePath("../../lib"));
        dirs.prepend(binDir.filePath("../libexec"));
        const QString qemuDataDir = QFileInfo(binDir.filePath("../share/qemu")).absoluteFilePath();
        if (QFileInfo(qemuDataDir).exists()) {
            env.insert("QEMU_DATA_DIR", qemuDataDir);
        }
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

static QString processDetail(QProcess& process, bool finished) {
    const QString stderrText = QString::fromUtf8(process.readAllStandardError()).trimmed();
    const QString stdoutText = QString::fromUtf8(process.readAllStandardOutput()).trimmed();
    if (!stderrText.isEmpty()) {
        return stderrText;
    }
    if (!stdoutText.isEmpty()) {
        return stdoutText;
    }
    return finished ? QString("exit code %1").arg(process.exitCode()) : QStringLiteral("timed out");
}

static bool runTool(const QString& program,
                    const QStringList& arguments,
                    int timeoutMs,
                    QString* detail) {
    QProcess process;
    process.start(program, arguments);
    const bool finished = process.waitForFinished(timeoutMs);
    if (finished && process.exitStatus() == QProcess::NormalExit && process.exitCode() == 0) {
        return true;
    }
    if (detail) {
        *detail = processDetail(process, finished);
    }
    return false;
}

static QStringList imageToolCandidates(const QemuEngineInfo& info) {
    QStringList candidates;
    candidates << info.imageTool.executable;
    const QString pathTool = QStandardPaths::findExecutable("qemu-img");
    if (!pathTool.isEmpty() && pathTool != info.imageTool.executable) {
        candidates << pathTool;
    }
    candidates.removeAll(QString());
    candidates.removeDuplicates();
    return candidates;
}

static QString defaultEfiCodeFileForConfig(const VmConfig& config) {
    const QString arch = normalizeArchitecture(config.architecture);
    const QString guest = config.guestOs.trimmed().toLower();
    if (guest.contains("dos") || guest.contains("old windows") || guest.contains("classic macos")) {
        return {};
    }
    if (guest.contains("windows") && (arch == "x86_64" || arch == "i386")) {
        return {};
    }
    if (arch == "aarch64") {
        return "edk2-aarch64-code.fd";
    }
    if (arch == "arm") {
        return "edk2-arm-code.fd";
    }
    if (arch == "x86_64") {
        return "edk2-x86_64-code.fd";
    }
    if (arch == "i386") {
        return "edk2-i386-code.fd";
    }
    if (arch == "riscv64") {
        return "edk2-riscv-code.fd";
    }
    if (arch == "loongarch64") {
        return "edk2-loongarch64-code.fd";
    }
    return {};
}

static QString defaultEfiVarsFileForConfig(const VmConfig& config) {
    const QString arch = normalizeArchitecture(config.architecture);
    const QString guest = config.guestOs.trimmed().toLower();
    if (guest.contains("dos") || guest.contains("old windows") || guest.contains("classic macos")) {
        return {};
    }
    if (guest.contains("windows") && (arch == "x86_64" || arch == "i386")) {
        return {};
    }
    if (arch == "aarch64" || arch == "arm") {
        return "edk2-arm-vars.fd";
    }
    if (arch == "x86_64" || arch == "i386") {
        return "edk2-i386-vars.fd";
    }
    if (arch == "riscv64") {
        return "edk2-riscv-vars.fd";
    }
    if (arch == "loongarch64") {
        return "edk2-loongarch64-vars.fd";
    }
    return {};
}

static bool looksLikeEfiCodeFirmware(const QString& path) {
    const QString name = QFileInfo(path).fileName().toLower();
    return name.contains("edk2") && name.contains("code") && name.endsWith(".fd")
        || name.contains("ovmf_code")
        || name.contains("ovmf-code");
}

static QString writableEfiVarsPath(const VmConfig& config, const QString& templatePath) {
    if (templatePath.trimmed().isEmpty()) {
        return {};
    }
    QStringList candidateDirs;
    if (!config.storageDir.trimmed().isEmpty()) {
        candidateDirs << config.storageDir.trimmed();
    }
    const QString diskDir = QFileInfo(config.disks.value(0).path).absolutePath();
    if (!diskDir.isEmpty() && diskDir != ".") {
        candidateDirs << diskDir;
    }
    const QString appDir = QStandardPaths::writableLocation(QStandardPaths::AppDataLocation);
    if (!appDir.isEmpty()) {
        candidateDirs << appDir;
    }
    candidateDirs.removeDuplicates();
    for (const QString& candidateDir : candidateDirs) {
        QDir dir(candidateDir);
        if (!dir.exists() && !dir.mkpath(".")) {
            continue;
        }
        const QString target = dir.filePath("efi-vars-" + normalizeArchitecture(config.architecture) + ".fd");
        if (QFileInfo::exists(target)) {
            return target;
        }
        if (!QFile::copy(templatePath, target)) {
            continue;
        }
        QFile::setPermissions(target,
                              QFileDevice::ReadOwner | QFileDevice::WriteOwner |
                              QFileDevice::ReadGroup | QFileDevice::WriteGroup |
                              QFileDevice::ReadOther);
        return target;
    }
    return {};
}

QemuEngine::QemuEngine(RuntimeManager runtime)
    : runtime_(std::move(runtime)) {}

QemuEngineInfo QemuEngine::detect(const QString& guestArch) const {
    return {runtime_.findSystemEmulator(guestArch), runtime_.findImageTool()};
}

VmConfig QemuEngine::normaliseConfig(const VmConfig& config) const {
    if (isSystemSoftwareConfig(config)) {
        return config;
    }
    VmConfig normalized = convertToQemu(config, detectHostProfile().platform).config;
    if (!normalized.firmware.trimmed().isEmpty()
        && normalized.efi.codePath.trimmed().isEmpty()
        && looksLikeEfiCodeFirmware(normalized.firmware)) {
        normalized.efi.codePath = normalized.firmware.trimmed();
        normalized.firmware.clear();
        const QString varsTemplate = runtime_.findFirmwareFile(defaultEfiVarsFileForConfig(normalized));
        normalized.efi.varsPath = writableEfiVarsPath(normalized, varsTemplate);
    }
    if (normalized.firmware.trimmed().isEmpty()
        && normalized.efi.codePath.trimmed().isEmpty()
        && normalized.boot.kernelPath.trimmed().isEmpty()) {
        const QString code = runtime_.findFirmwareFile(defaultEfiCodeFileForConfig(normalized));
        if (!code.isEmpty()) {
            normalized.efi.codePath = code;
            const QString varsTemplate = runtime_.findFirmwareFile(defaultEfiVarsFileForConfig(normalized));
            normalized.efi.varsPath = writableEfiVarsPath(normalized, varsTemplate);
        }
    }
    return normalized;
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
    const QStringList arguments = {"create", "-f", "qcow2", imagePath, size};
    QStringList failures;
    for (const QString& tool : imageToolCandidates(info)) {
        QString detail;
        if (runTool(tool, arguments, 60000, &detail)) {
            return true;
        }
        failures << QString("%1: %2").arg(tool, detail);
    }
    if (error) {
        *error = failures.isEmpty() ? QStringLiteral("qemu-img failed to create qcow2 image.") : failures.join('\n');
    }
    return false;
}

bool QemuEngine::createClassicMacQcow2(const QString& imagePath, qint64 sizeMb, QString* error) const {
#if defined(Q_OS_MACOS)
    const QFileInfo target(imagePath);
    if (target.exists()) {
        if (error) {
            *error = "目标硬盘已存在：" + imagePath;
        }
        return false;
    }
    if (!target.dir().exists() && !target.dir().mkpath(".")) {
        if (error) {
            *error = QString("无法创建硬盘目录：%1").arg(target.dir().absolutePath());
        }
        return false;
    }

    const QemuEngineInfo info = detect("ppc");
    if (!info.canCreateImages()) {
        if (error) {
            *error = "未找到 qemu-img，无法将 Mac 磁盘转换为 qcow2。";
        }
        return false;
    }
    const QString hdiutil = QStandardPaths::findExecutable("hdiutil");
    if (hdiutil.isEmpty()) {
        if (error) {
            *error = "未找到 macOS hdiutil，无法创建 Apple 分区表和 HFS+ 磁盘。";
        }
        return false;
    }

    QTemporaryDir staging;
    if (!staging.isValid()) {
        if (error) {
            *error = "无法创建 Classic MacOS 磁盘临时目录。";
        }
        return false;
    }
    const QString dmgPath = staging.filePath("classic-macos.dmg");
    const qint64 formattedSizeMb = qMax<qint64>(512, sizeMb);
    QString detail;
    if (!runTool(hdiutil,
                 {"create",
                  "-size", QString::number(formattedSizeMb) + "m",
                  "-layout", "SPUD",
                  "-fs", "HFS+",
                  "-volname", "Macintosh HD",
                  dmgPath},
                 180000,
                 &detail)) {
        if (error) {
            *error = "无法创建 Apple 分区表/HFS+ 临时磁盘：" + detail;
        }
        return false;
    }

    QStringList failures;
    // Newer macOS releases can create a raw, Apple-partitioned image here
    // rather than a UDIF container. Try both representations so the on-disk
    // partition map and HFS+ volume are preserved in either case.
    const QStringList sourceFormats = {"dmg", "raw"};
    for (const QString& sourceFormat : sourceFormats) {
        for (const QString& tool : imageToolCandidates(info)) {
            QFile::remove(imagePath);
            if (runTool(tool,
                        {"convert", "-f", sourceFormat, "-O", "qcow2", dmgPath, imagePath},
                        600000,
                        &detail)) {
                const QFileInfo created(imagePath);
                if (created.exists() && created.isFile()) {
                    return true;
                }
                failures << QString("%1 (%2): 转换完成但未生成文件").arg(tool, sourceFormat);
            } else {
                failures << QString("%1 (%2): %3").arg(tool, sourceFormat, detail);
            }
        }
    }
    QFile::remove(imagePath);
    if (error) {
        *error = "无法转换 Classic MacOS 磁盘为 qcow2：" + failures.join('\n');
    }
    return false;
#else
    Q_UNUSED(imagePath);
    Q_UNUSED(sizeMb);
    if (error) {
        *error = "Classic MacOS 磁盘初始化目前仅支持 macOS 宿主机。";
    }
    return false;
#endif
}

bool QemuEngine::isQcow2Blank(const QString& imagePath, bool* blank, QString* error) const {
    if (blank) {
        *blank = false;
    }
    const QFileInfo image(imagePath);
    if (!image.exists() || !image.isFile()) {
        if (error) {
            *error = "找不到 qcow2 磁盘：" + imagePath;
        }
        return false;
    }
    const QemuEngineInfo info = detect("ppc");
    if (!info.canCreateImages()) {
        if (error) {
            *error = "未找到 qemu-img，无法检查磁盘内容。";
        }
        return false;
    }

    QStringList failures;
    for (const QString& tool : imageToolCandidates(info)) {
        QProcess process;
        process.start(tool, {"map", "--output=json", imagePath});
        const bool finished = process.waitForFinished(60000);
        const QByteArray output = process.readAllStandardOutput();
        if (!(finished && process.exitStatus() == QProcess::NormalExit && process.exitCode() == 0)) {
            failures << QString("%1: %2").arg(tool, processDetail(process, finished));
            continue;
        }
        QJsonParseError parseError;
        const QJsonDocument document = QJsonDocument::fromJson(output, &parseError);
        if (parseError.error != QJsonParseError::NoError || !document.isArray()) {
            failures << QString("%1: 无法解析 qemu-img map 输出").arg(tool);
            continue;
        }

        const QJsonArray extents = document.array();
        bool hasExtent = false;
        bool allZeroAndUnallocated = true;
        for (const QJsonValue& value : extents) {
            if (!value.isObject()) {
                allZeroAndUnallocated = false;
                break;
            }
            const QJsonObject extent = value.toObject();
            if (extent.value("length").toVariant().toLongLong() <= 0) {
                continue;
            }
            hasExtent = true;
            const bool zero = extent.value("zero").toBool(false);
            const bool present = extent.value("present").toBool(false);
            const bool data = extent.value("data").toBool(false);
            if (!zero || present || data) {
                allZeroAndUnallocated = false;
                break;
            }
        }
        if (blank) {
            *blank = hasExtent && allZeroAndUnallocated;
        }
        return true;
    }
    if (error) {
        *error = failures.isEmpty() ? "qemu-img 无法检查磁盘。" : failures.join('\n');
    }
    return false;
}

bool QemuEngine::checkDiskImage(const QString& imagePath, QString* detail) const {
    const QFileInfo image(imagePath);
    if (!image.exists() || !image.isFile()) {
        if (detail) {
            *detail = "Disk image is missing or is not a regular file: " + imagePath;
        }
        return false;
    }
    const QemuEngineInfo info = detect("x86_64");
    if (!info.canCreateImages()) {
        if (detail) {
            *detail = "qemu-img is unavailable for disk diagnostics.";
        }
        return false;
    }

    QStringList failures;
    for (const QString& tool : imageToolCandidates(info)) {
        QProcess inspect;
        inspect.setProcessEnvironment(runtimeEnvironment(runtime_, info.imageTool));
        inspect.start(tool, {"info", "--output=json", imagePath});
        const bool inspected = inspect.waitForFinished(60000);
        const QByteArray output = inspect.readAllStandardOutput();
        if (!(inspected && inspect.exitStatus() == QProcess::NormalExit && inspect.exitCode() == 0)) {
            failures << QString("%1: %2").arg(tool, processDetail(inspect, inspected));
            continue;
        }
        QJsonParseError parseError;
        const QJsonDocument document = QJsonDocument::fromJson(output, &parseError);
        if (parseError.error != QJsonParseError::NoError || !document.isObject()) {
            failures << QString("%1: qemu-img info returned invalid JSON").arg(tool);
            continue;
        }

        const QString format = document.object().value("format").toString().toLower();
        if (format != "qcow2") {
            return true;
        }

        QProcess check;
        check.setProcessEnvironment(runtimeEnvironment(runtime_, info.imageTool));
        check.start(tool, {"check", "--output=json", imagePath});
        const bool checked = check.waitForFinished(60000);
        if (checked && check.exitStatus() == QProcess::NormalExit && check.exitCode() == 0) {
            return true;
        }
        failures << QString("%1: %2").arg(tool, processDetail(check, checked));
    }
    if (detail) {
        *detail = failures.isEmpty() ? "qemu-img could not verify the disk image." : failures.join('\n');
    }
    return false;
}

bool QemuEngine::prepareProcess(QProcess& process, const VmConfig& config, const QStringList& extraArgs, QString* error) const {
    if (isSystemSoftwareConfig(config)) {
        if (error) *error = "System Software 配置必须使用 SystemSoftwareEngine，不能交给 QEMU。";
        return false;
    }
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
