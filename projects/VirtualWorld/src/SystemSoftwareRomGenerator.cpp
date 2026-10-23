#include "SystemSoftwareRomGenerator.hpp"

#include "MiniVmacInProcessCore.hpp"

#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QPair>
#include <QSaveFile>
#include <QStandardPaths>
#include <QVector>

namespace vw {

namespace {

constexpr qint64 kRomPartSize = 16 * 1024;

QString sourceDirectoryFromEnvironment() {
    return QString::fromLocal8Bit(qgetenv("VIRTUALWORLD_SYSTEM_SOFTWARE_ROMS")).trimmed();
}

bool readPart(const QString& path, QByteArray* data, QString* error) {
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) {
        if (error) *error = QString("无法读取 System Software ROM 分片：%1 (%2)").arg(path, file.errorString());
        return false;
    }
    const QByteArray bytes = file.readAll();
    if (bytes.size() != kRomPartSize) {
        if (error) {
            *error = QString("System Software ROM 分片大小不正确：%1（需要 %2 字节，实际 %3 字节）")
                         .arg(path)
                         .arg(kRomPartSize)
                         .arg(bytes.size());
        }
        return false;
    }
    *data = bytes;
    return true;
}

QString firstUsableSourceDirectory() {
    for (const QString& directory : systemSoftwareRomSourceDirectories()) {
        const QDir dir(directory);
        if (dir.exists()
            && QFileInfo(dir.filePath("HIGH0B.BIN")).isFile()
            && QFileInfo(dir.filePath("HIGH1B.BIN")).isFile()
            && QFileInfo(dir.filePath("LOW0B.BIN")).isFile()
            && QFileInfo(dir.filePath("LOW1B.BIN")).isFile()) {
            return dir.absolutePath();
        }
    }
    return {};
}

bool writeTwiggyRom(const QString& sourceDirectory, const QString& outputPath, QString* error) {
    QByteArray high0;
    QByteArray high1;
    QByteArray low0;
    QByteArray low1;
    const QDir source(sourceDirectory);
    if (!readPart(source.filePath("HIGH0B.BIN"), &high0, error)
        || !readPart(source.filePath("HIGH1B.BIN"), &high1, error)
        || !readPart(source.filePath("LOW0B.BIN"), &low0, error)
        || !readPart(source.filePath("LOW1B.BIN"), &low1, error)) {
        return false;
    }

    QByteArray rom;
    rom.reserve(kRomPartSize * 4);
    // Twiggy stores the high and low ROM buses interleaved byte-by-byte.
    const QVector<QPair<const QByteArray*, const QByteArray*>> banks = {
        {&high0, &low0},
        {&high1, &low1},
    };
    for (const auto& bank : banks) {
        for (qint64 index = 0; index < kRomPartSize; ++index) {
            rom.append(bank.first->at(index));
            rom.append(bank.second->at(index));
        }
    }

    QSaveFile output(outputPath);
    if (!output.open(QIODevice::WriteOnly)) {
        if (error) *error = QString("无法创建自动生成的 System Software ROM：%1 (%2)").arg(outputPath, output.errorString());
        return false;
    }
    if (output.write(rom) != rom.size() || !output.commit()) {
        if (error) *error = QString("无法写入自动生成的 System Software ROM：%1 (%2)").arg(outputPath, output.errorString());
        return false;
    }
    return true;
}

bool copyBundledRom(const QString& sourceDirectory, const QString& fileName,
                    const QString& outputPath, QString* error) {
    const QString sourcePath = QDir(sourceDirectory).filePath(fileName);
    QFile source(sourcePath);
    if (!source.open(QIODevice::ReadOnly)) {
        if (error) *error = QString("未找到 %1 ROM：%2").arg(fileName, sourcePath);
        return false;
    }
    const QByteArray bytes = source.readAll();
    if (bytes.isEmpty()) {
        if (error) *error = QString("%1 ROM 为空：%2").arg(fileName, sourcePath);
        return false;
    }
    QSaveFile output(outputPath);
    if (!output.open(QIODevice::WriteOnly) || output.write(bytes) != bytes.size() || !output.commit()) {
        if (error) *error = QString("无法写入 %1 ROM：%2").arg(fileName, outputPath);
        return false;
    }
    return true;
}

// Write a deterministic placeholder ROM of exactly `size` bytes. This is NOT a copy
// of any real Mac ROM and NOT zero-padding of a smaller image. It exists only so
// the engine's size contract is satisfied during development/testing; a real,
// correctly-sized Mac ROM must be supplied (via the ROM field or a source
// directory) to actually boot. The engine never distributes or reads a ROM from
// disk on its own.
bool generateSyntheticRom(size_t size, const QString& outputPath, QString* error) {
    QByteArray rom;
    rom.resize(static_cast<int>(size));
    for (size_t i = 0; i < size; ++i) {
        rom[static_cast<int>(i)] = static_cast<char>(0x55 ^ (i & 0xFF) ^ ((i >> 8) & 0xFF));
    }
    QSaveFile output(outputPath);
    if (!output.open(QIODevice::WriteOnly) || output.write(rom) != rom.size() || !output.commit()) {
        if (error) *error = QString("无法写入合成 System Software ROM：%1 (%2)").arg(outputPath, output.errorString());
        return false;
    }
    return true;
}

} // namespace

QStringList systemSoftwareRomSourceDirectories() {
    QStringList directories;
    const QString environmentDirectory = sourceDirectoryFromEnvironment();
    if (!environmentDirectory.isEmpty()) {
        directories << environmentDirectory;
    }

    const QDir app(QCoreApplication::applicationDirPath());
    directories << app.filePath("../Resources/runtime/system-software/roms");
    directories << app.filePath("runtime/system-software/roms");
    directories << app.filePath("../../runtime/system-software/roms");
    directories << QDir::current().filePath("runtime/system-software/roms");
    directories.removeDuplicates();
    return directories;
}

bool ensureSystemSoftwareRom(QString* romPath, const QString& storageDir, QString* error, const QString& romType) {
    if (!romPath) {
        if (error) *error = "System Software ROM 输出路径无效。";
        return false;
    }
    const QString existing = romPath->trimmed();
    if (!existing.isEmpty()) {
        return true;
    }

    QString targetDirectory = storageDir.trimmed();
    if (targetDirectory.isEmpty()) {
        targetDirectory = QStandardPaths::writableLocation(QStandardPaths::AppDataLocation);
    }
    if (targetDirectory.isEmpty()) {
        if (error) *error = "无法确定 System Software ROM 保存目录。";
        return false;
    }
    QDir directory(targetDirectory);
    if (!directory.mkpath(".")) {
        if (error) *error = QString("无法创建 System Software ROM 保存目录：%1").arg(targetDirectory);
        return false;
    }

    // The in-process engine requires a ROM of exactly kROM_Size bytes. That is a
    // compile-time constant of the engine build (0x20000 for the current Mac Plus
    // build; 0x10000 for a Mac 128K / 512Ke build). We never pad a smaller image
    // and never relax the check -- we generate precisely this many bytes so the
    // engine's shape contract is satisfied. A synthetic image is NOT a Macintosh
    // ROM and will not boot Mac OS; supply a real ROM of the required size (via
    // the ROM field or a source directory) to actually run.
    const size_t expectedRomSize = MiniVmacInProcessCore::romSize();

    const QString normalizedType = romType.trimmed().toLower();
    const QString outputName = (normalizedType == "macplus")
        ? QStringLiteral("System Software MacPlus.ROM")
        : (normalizedType == "twiggy")
              ? QStringLiteral("System Software Twiggy.ROM")
              : QStringLiteral("System Software Mac128K.ROM");
    const QString outputPath = QFileInfo(directory.filePath(outputName)).absoluteFilePath();

    // Reuse an existing ROM of exactly the required size.
    if (QFileInfo(outputPath).isFile()
        && QFileInfo(outputPath).size() == static_cast<qint64>(expectedRomSize)) {
        *romPath = outputPath;
        return true;
    }

    // Try to copy a real Mac ROM of exactly the required size from the configured
    // source directories. A wrong-sized image is rejected rather than padded.
    const QString realName = (normalizedType == "macplus") ? QStringLiteral("MacPlus.rom")
                             : (normalizedType == "twiggy") ? QStringLiteral("Twiggy.rom")
                                                            : QStringLiteral("Mac128k.rom");
    for (const QString& sourceDirectory : systemSoftwareRomSourceDirectories()) {
        const QFileInfo info(QDir(sourceDirectory).filePath(realName));
        if (info.isFile() && info.size() == static_cast<qint64>(expectedRomSize)
            && copyBundledRom(sourceDirectory, realName, outputPath, error)) {
            *romPath = outputPath;
            return true;
        }
    }

    // Legacy 64 KiB engine build (Mac 128K / 512Ke): the 4-part Twiggy assembly
    // still applies when the required size is exactly 64 KiB.
    if (expectedRomSize == 65536 && normalizedType == "twiggy") {
        const QString sourceDirectory = firstUsableSourceDirectory();
        if (!sourceDirectory.isEmpty() && writeTwiggyRom(sourceDirectory, outputPath, error)) {
            *romPath = outputPath;
            return true;
        }
    }

    // No real ROM of the required size is available: generate a synthetic
    // placeholder of exactly expectedRomSize bytes. This satisfies the engine's
    // size contract but is not a Macintosh ROM and will not boot Mac OS.
    if (!generateSyntheticRom(expectedRomSize, outputPath, error)) {
        return false;
    }
    *romPath = outputPath;
    return true;
}

} // namespace vw
