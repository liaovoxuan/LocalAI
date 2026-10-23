#pragma once

#include "VmConfig.hpp"

#include <QString>

namespace vw {

VmConfig parseQemuCommand(const QString& command);
QStringList splitCommandLine(const QString& command);
QString commandFromScriptText(const QString& text);

} // namespace vw
