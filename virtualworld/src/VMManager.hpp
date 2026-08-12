#pragma once

#include "ConfigManager.hpp"
#include "ImageManager.hpp"
#include "QemuEngine.hpp"

namespace vw {

class VMManager {
public:
    VMManager(QemuEngine engine = QemuEngine(),
              ConfigManager configs = ConfigManager(),
              ImageManager images = ImageManager());

    QemuEngineInfo detectRuntime(const QString& guestArch) const;
    VmConfig normaliseConfig(const VmConfig& config) const;
    VmConfig loadConfig(const QString& path) const;
    bool saveConfig(const QString& path, const VmConfig& config, QString* error = nullptr) const;
    bool createDisk(const QString& imagePath, qint64 sizeMb, QString* error = nullptr) const;
    QString renderQemuCommand(const VmConfig& config) const;
    bool start(const VmConfig& config, qint64* processId = nullptr, QString* error = nullptr) const;
    bool stop(qint64 processId, QString* error = nullptr) const;
    bool testRuntime(const QString& guestArch, QString* output = nullptr, QString* error = nullptr) const;

private:
    QemuEngine engine_;
    ConfigManager configs_;
    ImageManager images_;
};

} // namespace vw
