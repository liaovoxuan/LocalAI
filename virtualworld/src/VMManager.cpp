#include "VMManager.hpp"

#include <utility>

namespace vw {

VMManager::VMManager(QemuEngine engine, ConfigManager configs, ImageManager images)
    : engine_(std::move(engine)),
      configs_(std::move(configs)),
      images_(std::move(images)) {}

QemuEngineInfo VMManager::detectRuntime(const QString& guestArch) const {
    return engine_.detect(guestArch);
}

VmConfig VMManager::normaliseConfig(const VmConfig& config) const {
    return engine_.normaliseConfig(config);
}

VmConfig VMManager::loadConfig(const QString& path) const {
    return configs_.readVmConfig(path);
}

bool VMManager::saveConfig(const QString& path, const VmConfig& config, QString* error) const {
    return configs_.writeVmConfig(path, config, error);
}

bool VMManager::createDisk(const QString& imagePath, qint64 sizeMb, QString* error) const {
    return images_.createQcow2(imagePath, sizeMb, error);
}

QString VMManager::renderQemuCommand(const VmConfig& config) const {
    return engine_.renderCommand(config);
}

bool VMManager::start(const VmConfig& config, qint64* processId, QString* error) const {
    return engine_.startVm(config, processId, error);
}

bool VMManager::stop(qint64 processId, QString* error) const {
    return engine_.stopVm(processId, error);
}

bool VMManager::testRuntime(const QString& guestArch, QString* output, QString* error) const {
    return engine_.startTest(guestArch, output, error);
}

} // namespace vw
