#pragma once

#include "VmConfig.hpp"

#include <QString>

namespace vw {

VmConfig parseUtmSource(const QString& source);
VmConfig parseUtmPlistXml(const QString& xml, const QString& baseDir = QString());
QString renderUtmPlist(const VmConfig& config, const QString& guestOs, QVector<ValidationIssue>* issues = nullptr);
bool writeUtmPackage(const QString& packagePath, const QString& plistXml, QString* error = nullptr);

} // namespace vw
