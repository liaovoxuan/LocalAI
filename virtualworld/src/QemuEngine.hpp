#pragma once

#include "RuntimeManager.hpp"
#include "VmConfig.hpp"

#include <QString>
#include <QStringList>

class QProcess;

namespace vw {

struct QemuEngineInfo {
    RuntimePath systemEmulator;
    RuntimePath imageTool;

    bool canRunVm() const { return systemEmulator.available; }
    bool canCreateImages() const { return imageTool.available; }
};

class QemuEngine {
public:
    explicit QemuEngine(RuntimeManager runtime = RuntimeManager());

    QemuEngineInfo detect(const QString& guestArch) const;
    VmConfig normaliseConfig(const VmConfig& config) const;
    QStringList buildArguments(const VmConfig& config) const;
    QString renderCommand(const VmConfig& config) const;
    bool prepareProcess(QProcess& process, const VmConfig& config, const QStringList& extraArgs = {}, QString* error = nullptr) const;
    bool createQcow2(const QString& imagePath, qint64 sizeMb, QString* error = nullptr) const;
    bool startVm(const VmConfig& config, qint64* processId = nullptr, QString* error = nullptr) const;
    bool stopVm(qint64 processId, QString* error = nullptr) const;
    bool startTest(const QString& guestArch, QString* output = nullptr, QString* error = nullptr) const;

private:
    RuntimeManager runtime_;
};

QemuEngineInfo findQemuEngine(const QString& guestArch);
bool startQemuVm(const VmConfig& config, QString* error = nullptr);

} // namespace vw
