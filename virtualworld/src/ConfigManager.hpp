#pragma once

#include "VmConfig.hpp"

#include <QString>

namespace vw {

class ConfigManager {
public:
    VmConfig readVmConfig(const QString& path) const;
    bool writeVmConfig(const QString& path, const VmConfig& config, QString* error = nullptr) const;
};

} // namespace vw
