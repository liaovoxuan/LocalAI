#include "VMManager.hpp"

#include "SystemSoftwareEngine.hpp"

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

bool VMManager::createClassicMacDisk(const QString& imagePath, qint64 sizeMb, QString* error) const {
    return images_.createClassicMacQcow2(imagePath, sizeMb, error);
}

bool VMManager::isDiskBlank(const QString& imagePath, bool* blank, QString* error) const {
    return images_.isQcow2Blank(imagePath, blank, error);
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

// ---------------------------------------------------------------------------
// Virtualization backend layer
// ---------------------------------------------------------------------------

VirtualizationEngineRegistry& VMManager::virtualizationRegistry() const {
    VirtualizationEngineRegistry& registry = VirtualizationEngineRegistry::instance();
    registry.registerBuiltinBackends();
    return registry;
}

EngineSelection VMManager::selectBackend(const VmConfig& config, const QString& preference) const {
    return virtualizationRegistry().select(config, preference);
}

bool VMManager::startWithBackend(const VmConfig& config,
                                 const QString& preference,
                                 qint64* processId,
                                 QString* error,
                                 EngineSelection* selection) const {
    if (isSystemSoftwareConfig(config)) {
        if (error) {
            *error = QStringLiteral("System Software 配置不使用虚拟化后端注册表。");
        }
        return false;
    }

    const EngineSelection chosen = selectBackend(config, preference);
    if (selection) {
        *selection = chosen;
    }
    if (!chosen.ok || chosen.engine.isNull()) {
        if (error) {
            *error = chosen.reason.isEmpty() ? QStringLiteral("没有可用的虚拟化后端。") : chosen.reason;
        }
        return false;
    }
    return chosen.engine->start(config, processId, error);
}

QString VMManager::backendReportJson(const VmConfig& config, const QString& guestArch) const {
    return virtualizationRegistry().toJson(config, guestArch);
}

} // namespace vw
