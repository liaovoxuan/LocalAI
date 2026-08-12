#pragma once

#include "Platform.hpp"

#include <QString>
#include <QStringList>

namespace vw {

struct RuntimePath {
    QString executable;
    QString expectedPath;
    QString source;
    bool available = false;
};

class RuntimeManager {
public:
    explicit RuntimeManager(QString appDir = QString());

    RuntimePath findSystemEmulator(const QString& guestArch) const;
    RuntimePath findImageTool() const;
    QString runtimeRoot() const;
    QString platformRuntimeName() const;
    QString hostArchFolder() const;
    QStringList runtimeBinDirs() const;

private:
    RuntimePath findExecutable(const QString& executableName) const;
    QStringList runtimeRoots() const;

    QString appDir_;
    HostProfile host_;
};

} // namespace vw
