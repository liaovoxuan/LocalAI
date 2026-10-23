#pragma once

#include "ConfigManager.hpp"
#include "ImageManager.hpp"
#include "QemuEngine.hpp"
#include "VirtualizationEngine.hpp"
#include "VirtualizationEngineRegistry.hpp"

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
    bool createClassicMacDisk(const QString& imagePath, qint64 sizeMb, QString* error = nullptr) const;
    bool isDiskBlank(const QString& imagePath, bool* blank, QString* error = nullptr) const;
    QString renderQemuCommand(const VmConfig& config) const;
    bool start(const VmConfig& config, qint64* processId = nullptr, QString* error = nullptr) const;
    bool stop(qint64 processId, QString* error = nullptr) const;
    bool testRuntime(const QString& guestArch, QString* output = nullptr, QString* error = nullptr) const;

    // --- virtualization backend layer --------------------------------------
    //
    // `start()` above keeps its historical behaviour: it runs QEMU (hardware
    // acceleration when available, TCG otherwise) and detaches the guest
    // process. The methods below expose the new VirtualizationEngine registry.

    // Registers the built-in backends for this build and returns the catalogue.
    VirtualizationEngineRegistry& virtualizationRegistry() const;

    // Auto-selects a backend for `config`. `preference` accepts "", "auto",
    // "qemu", or a concrete backend id.
    EngineSelection selectBackend(const VmConfig& config,
                                  const QString& preference = QString()) const;

    // Starts through the selected backend. `preference` empty means "use the
    // config's own backend field / VIRTUALWORLD_BACKEND".
    bool startWithBackend(const VmConfig& config,
                          const QString& preference,
                          qint64* processId,
                          QString* error,
                          EngineSelection* selection = nullptr) const;

    // Machine-readable capability report (JSON).
    QString backendReportJson(const VmConfig& config = VmConfig(),
                              const QString& guestArch = QString()) const;

private:
    QemuEngine engine_;
    ConfigManager configs_;
    ImageManager images_;
};

} // namespace vw
