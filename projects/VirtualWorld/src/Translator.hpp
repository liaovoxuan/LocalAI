#pragma once

#include "Platform.hpp"
#include "VmConfig.hpp"

namespace vw {

ConversionResult convertToQemu(VmConfig config, HostPlatform target);
QStringList buildQemuCommandParts(const VmConfig& config);
QStringList buildQemuCommandParts(const VmConfig& config, HostPlatform target);
QString renderQemuCommand(const VmConfig& config, HostPlatform target);
QVector<ValidationIssue> validateConfig(const VmConfig& config, HostPlatform target);

} // namespace vw
