#include "SystemSoftwareEngine.hpp"

#include <QByteArray>
#include <QDir>
#include <QFile>
#include <QFileInfo>

#include <algorithm>
#include <cstdint>

namespace vw {

namespace {

int floppyRoleOrder(const DiskConfig& disk) {
    if (disk.role.compare(QStringLiteral("floppy-a"), Qt::CaseInsensitive) == 0) return 0;
    if (disk.role.compare(QStringLiteral("floppy-b"), Qt::CaseInsensitive) == 0) return 1;
    return 2;
}

QVector<const DiskConfig*> orderedFloppies(const VmConfig& config) {
    QVector<const DiskConfig*> result;
    for (const DiskConfig& disk : config.disks) {
        if (disk.path.trimmed().isEmpty()) continue;
        if (disk.media.trimmed().compare(QStringLiteral("floppy"), Qt::CaseInsensitive) != 0) continue;
        result.append(&disk);
    }
    std::stable_sort(result.begin(), result.end(), [](const DiskConfig* left, const DiskConfig* right) {
        return floppyRoleOrder(*left) < floppyRoleOrder(*right);
    });
    return result;
}

} // namespace

SystemSoftwareEngine::SystemSoftwareEngine(RuntimeManager runtime)
    : runtime_(std::move(runtime)) {}

bool isSystemSoftwareConfig(const VmConfig& config) {
    return config.backend.trimmed().compare("system-software", Qt::CaseInsensitive) == 0;
}

bool isSupportedSystemSoftwareVersion(const QString& version) {
    const QString normalized = version.trimmed();
    return normalized.size() == 1 && normalized.at(0) >= QChar('0') && normalized.at(0) <= QChar('6');
}

bool isDisklessSystemSoftwareVersion(const QString& version) {
    const QString normalized = version.trimmed();
    return normalized == QStringLiteral("0")
        || normalized == QStringLiteral("1")
        || normalized == QStringLiteral("2")
        || normalized == QStringLiteral("3");
}

RuntimePath SystemSoftwareEngine::detect() const {
    // The in-process engine is always present (it is a linked static library),
    // so detection no longer probes the filesystem for an external executable.
    RuntimePath result;
    result.source = QStringLiteral("in-process-engine");
    result.available = true;
    result.executable.clear();
    result.expectedPath = QStringLiteral("in-process Mini vMac engine (libvwminivmac)");
    return result;
}

QString systemSoftwareRuntimeVariant(const VmConfig& config) {
    const QString machine = config.systemSoftwareMachine.trimmed().toLower();
    const QString romType = config.systemSoftwareRomType.trimmed().toLower();
    const QString romName = QFileInfo(config.systemSoftwareRomPath).fileName().toLower();
    if (machine.contains("twiggy") || romType == "twiggy" || romName.contains("twiggy")) return "twiggy";
    if (machine.contains("512") || romType == "mac512k" || romName.contains("512")) return "512ke";
    if (machine.contains("128") || romType == "mac128k" || romName.contains("128")) return "128k";
    if (machine.contains("plus") || romType == "macplus" || romName.contains("plus")) return "plus";
    if (config.systemSoftwareVersion.trimmed() == "0") return "twiggy";
    if (config.systemSoftwareVersion.trimmed() == "1") return "128k";
    if (config.systemSoftwareVersion.trimmed() == "2") return "512ke";
    return "plus";
}

RuntimePath SystemSoftwareEngine::detect(const VmConfig& config) const {
    Q_UNUSED(config);
    return detect();
}

QStringList SystemSoftwareEngine::buildArguments(const VmConfig& config) const {
    // Informational only: what the in-process engine consumes. Not a process
    // command line — the engine reads these from memory, not argv.
    QStringList args;
    args << QStringLiteral("rom:") + config.systemSoftwareRomPath;
    for (const DiskConfig* disk : orderedFloppies(config)) {
        args << QStringLiteral("disk:") + disk->path;
    }
    return args;
}

QString SystemSoftwareEngine::renderCommand(const VmConfig& config) const {
    // No external executable is launched. This string is for the command-preview
    // UI only; it documents that the engine runs in-process.
    return QStringLiteral("in-process Mini vMac engine (libvwminivmac) — variant=%1 — 不派生外部进程")
        .arg(systemSoftwareRuntimeVariant(config));
}

bool SystemSoftwareEngine::validate(const VmConfig& config, QString* error) const {
    if (!isSystemSoftwareConfig(config)) {
        if (error) *error = "不是 System Software 配置。";
        return false;
    }
    if (!isSupportedSystemSoftwareVersion(config.systemSoftwareVersion)) {
        if (error) *error = "System Software 版本必须为 0、1、2、3、4、5 或 6。";
        return false;
    }
    // The in-process engine is always available; no external binary to find.
    const QFileInfo rom(config.systemSoftwareRomPath);
    if (!rom.exists() || !rom.isFile()) {
        if (error) *error = QString("System Software ROM 不存在：%1").arg(config.systemSoftwareRomPath);
        return false;
    }
    bool hasFloppy = false;
    for (const DiskConfig& disk : config.disks) {
        if (disk.path.trimmed().isEmpty()) continue;
        if (disk.media.trimmed().compare("floppy", Qt::CaseInsensitive) != 0) {
            if (error) *error = "System Software 只支持 Floppy A/B，不支持 IDE、SATA 或其他硬盘设备。";
            return false;
        }
        hasFloppy = true;
        if (!QFileInfo::exists(disk.path)) {
            if (error) *error = QString("System Software 启动磁盘不存在：%1").arg(disk.path);
            return false;
        }
    }
    if (!hasFloppy && !isDisklessSystemSoftwareVersion(config.systemSoftwareVersion)) {
        if (error) *error = "System Software 需要至少一个启动磁盘镜像。";
        return false;
    }
    return true;
}

namespace {

// Read a whole file into a QByteArray. Returns false on any failure.
bool readWholeFile(const QString& path, QByteArray* out, QString* error) {
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        if (error) *error = QString("无法读取文件：%1 (%2)").arg(path, file.errorString());
        return false;
    }
    *out = file.readAll();
    file.close();
    return true;
}

} // namespace

bool SystemSoftwareEngine::start(const VmConfig& config, QString* error) {
    if (!validate(config, error)) {
        return false;
    }

    MvStatus s = core_.create();
    if (s != MvStatus::Ok) {
        lastStatus_ = s;
        if (error) {
            *error = QString("进程内引擎启动失败（create）：%1").arg(mvStatusName(s));
        }
        return false;
    }

    // ROM — caller-supplied, never distributed. The engine requires exactly
    // kROM_Size (0x20000); it does NOT pad short images and does NOT relax its
    // validation to accept a pseudo-ROM. A mismatched size is a real, reported
    // error rather than a silent substitution.
    QByteArray rom;
    if (!readWholeFile(config.systemSoftwareRomPath, &rom, error)) {
        lastStatus_ = MvStatus::RomMissing;
        core_.stop();
        return false;
    }
    s = core_.loadRom(reinterpret_cast<const uint8_t*>(rom.constData()),
                      static_cast<size_t>(rom.size()));
    if (s != MvStatus::Ok) {
        lastStatus_ = s;
        if (error) {
            const size_t need = MiniVmacInProcessCore::romSize();
            *error = QString("ROM 被引擎拒绝（%1）。引擎要求恰好 %2 字节 (0x%3) 且不会补零或放宽校验；"
                             "当前 ROM 大小为 %4 字节 (0x%5)。合成 ROM 不是 Macintosh ROM，不会启动 Mac OS。")
                         .arg(mvStatusName(s))
                         .arg(need)
                         .arg(need, 0, 16)
                         .arg(rom.size())
                         .arg(rom.size(), 0, 16);
        }
        core_.stop();
        return false;
    }

    // Disks — attached from memory, never via a host file handle.
    int drive = 0;
    for (const DiskConfig* disk : orderedFloppies(config)) {
        if (drive >= 6) break;
        QByteArray img;
        if (!readWholeFile(disk->path, &img, error)) {
            lastStatus_ = MvStatus::BadArgument;
            core_.stop();
            return false;
        }
        s = core_.attachDisk(static_cast<unsigned>(drive),
                             reinterpret_cast<const uint8_t*>(img.constData()),
                             static_cast<size_t>(img.size()),
                             true,
                             QFileInfo(disk->path).fileName().toStdString());
        if (s != MvStatus::Ok) {
            lastStatus_ = s;
            if (error) *error = QString("软盘 %1 加载失败：%2").arg(drive).arg(mvStatusName(s));
            core_.stop();
            return false;
        }
        ++drive;
    }

    // The frame callback is registered by the caller (VirtualMachineWindow)
    // BEFORE start() so it fires exactly once per completed frame on the engine
    // worker thread. We must NOT install a default callback here: doing so would
    // silently overwrite the caller's and drop every frame.
    s = core_.start();
    if (s != MvStatus::Ok) {
        lastStatus_ = s;
        if (error) *error = QString("引擎 start 失败：%1").arg(mvStatusName(s));
        core_.stop();
        return false;
    }
    lastStatus_ = MvStatus::Ok;
    return true;
}

void SystemSoftwareEngine::stop() {
    core_.stop();
}

bool SystemSoftwareEngine::isRunning() const {
    return core_.isRunning();
}

MvStatus SystemSoftwareEngine::lastStatus() const {
    return lastStatus_;
}

bool SystemSoftwareEngine::setKey(uint8_t macKeyCode, bool down) {
    return core_.setKey(macKeyCode, down) == MvStatus::Ok;
}

bool SystemSoftwareEngine::setMouse(int32_t x, int32_t y) {
    return core_.setMouse(x, y) == MvStatus::Ok;
}

bool SystemSoftwareEngine::setMouseButton(bool down) {
    return core_.setMouseButton(down) == MvStatus::Ok;
}

bool SystemSoftwareEngine::setMouseWheel(int32_t dx, int32_t dy) {
    return core_.setMouseWheel(dx, dy) == MvStatus::Ok;
}

bool SystemSoftwareEngine::latestFrame(MvFrame* out) const {
    return core_.latestFrame(out);
}

MvStats SystemSoftwareEngine::stats() const {
    return core_.stats();
}

void SystemSoftwareEngine::setFrameCallback(std::function<void(const MvFrame&)> cb) {
    core_.setFrameCallback(std::move(cb));
}

} // namespace vw
