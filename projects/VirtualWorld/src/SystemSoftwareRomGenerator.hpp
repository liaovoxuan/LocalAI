#pragma once

#include <QString>
#include <QStringList>

namespace vw {

// Generate a Mini vMac-compatible Twiggy ROM only for System Software VMs.
// If romPath is already set, it is left unchanged.
bool ensureSystemSoftwareRom(QString* romPath,
                             const QString& storageDir,
                             QString* error = nullptr,
                             const QString& romType = QString());

QStringList systemSoftwareRomSourceDirectories();

} // namespace vw
