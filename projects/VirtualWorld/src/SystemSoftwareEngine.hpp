#pragma once

#include "MiniVmacInProcessCore.hpp"
#include "RuntimeManager.hpp"
#include "VmConfig.hpp"

#include <QString>
#include <QStringList>
#include <functional>

namespace vw {

/*
	SystemSoftwareEngine — Mac-side System Software (Mini vMac) runner.

	This engine runs Mini vMac INSIDE the VirtualWorld process on a worker
	thread owned by MiniVmacInProcessCore. There is no child process and no PID:
	that is what makes it usable where spawning an external executable is
	forbidden (HarmonyOS/OpenHarmony), and it is the Mac-side replacement for
	the four prebuilt `minivmac-*` binaries that used to be launched via
	QProcess.

	The public surface is intentionally process-free: start/stop/isRunning,
	input (keyboard, mouse position/button/wheel) and a frame surface (latestFrame/stats/
	setFrameCallback). Nothing here forks, execs, or spawns.
*/
class SystemSoftwareEngine {
public:
    explicit SystemSoftwareEngine(RuntimeManager runtime = RuntimeManager());

    RuntimePath detect() const;
    RuntimePath detect(const VmConfig& config) const;
    bool validate(const VmConfig& config, QString* error = nullptr) const;

    // Retained for diagnostics / command-preview only. These never spawn a
    // process; they describe what the in-process engine will load.
    QStringList buildArguments(const VmConfig& config) const;
    QString renderCommand(const VmConfig& config) const;

    // In-process lifecycle. Replaces the old QProcess spawn path.
    bool start(const VmConfig& config, QString* error = nullptr);
    void stop();
    bool isRunning() const;
    MvStatus lastStatus() const;

    // Input + frame surface (safe no-ops when not running).
    bool setKey(uint8_t macKeyCode, bool down);
    bool setMouse(int32_t x, int32_t y);
    bool setMouseButton(bool down);
    bool setMouseWheel(int32_t dx, int32_t dy);
    bool latestFrame(MvFrame* out) const;
    MvStats stats() const;
    void setFrameCallback(std::function<void(const MvFrame&)> cb);

private:
    RuntimeManager runtime_;
    MiniVmacInProcessCore core_;
    MvStatus lastStatus_ = MvStatus::Ok;
};

bool isSystemSoftwareConfig(const VmConfig& config);
bool isSupportedSystemSoftwareVersion(const QString& version);
bool isDisklessSystemSoftwareVersion(const QString& version);
QString systemSoftwareRuntimeVariant(const VmConfig& config);

} // namespace vw
