#pragma once

// VirtualizationEngine — the shared backend contract.
//
// Every virtualization backend in VirtualWorld implements this interface. The
// contract deliberately mirrors what the callers actually need:
//
//   * identity          backend id + display name + tier
//   * host support      can this backend run on this machine at all?
//   * guest support     can it run the requested guest architecture?
//   * capabilities      probe() -> machine-readable capability report
//   * validation        validate(config) -> errors/warnings before start
//   * lifecycle         start(config) / stop(pid)
//   * diagnostics       diagnose(config) -> human-readable explanation
//
// Honesty rules baked into this contract:
//   * A backend must never report cross-architecture hardware acceleration.
//     Same-ISA only; everything else is TCG's job.
//   * A backend that cannot actually run a config must say so in validate()
//     so the registry can fall back instead of failing at start time.
//   * Native backends that are not compiled in (missing platform sources) are
//     simply absent from the registry; QEMU remains the guaranteed fallback.

#include "VirtualizationCapabilities.hpp"

#include <QSharedPointer>
#include <QString>
#include <QVector>

namespace vw {

class VirtualizationEngine {
public:
    virtual ~VirtualizationEngine() = default;

    // --- identity -----------------------------------------------------------
    virtual QString backendId() const = 0;
    virtual QString displayName() const = 0;
    virtual VirtualizationTier tier() const = 0;

    // The tier this backend would really achieve for *one* guest architecture,
    // as opposed to `tier()`, which names the mechanism the backend wraps and
    // does not depend on the host state.
    //
    // The distinction matters for backends that can degrade: WHPX is by nature
    // a native mechanism, but on a host where the hypervisor is off — or for a
    // guest of another ISA — the guest is really run by QEMU TCG. Reporting
    // `Native` there would be a lie, so those backends override this and the
    // public layer surfaces the honest value (JSON `execution_tier`, CLI
    // `execution_tier=`). Backends with a single fixed mechanism inherit the
    // default, which simply answers `tier()`.
    virtual VirtualizationTier executionTierFor(const QString& guestArch) const;

    // --- capability ---------------------------------------------------------
    virtual bool isHostSupported(QString* reason = nullptr) const = 0;
    virtual bool supportsGuestArch(const QString& guestArch, QString* reason = nullptr) const = 0;
    virtual EngineProbeResult probe() const = 0;

    // Optional fast path: probe once for a specific guest architecture.
    virtual EngineProbeResult probeFor(const QString& guestArch) const;

    // --- validation ---------------------------------------------------------
    virtual QVector<EngineIssue> validate(const VmConfig& config) const = 0;

    // Convenience: true when validate() produced no errors.
    bool canRun(const VmConfig& config) const;

    // --- lifecycle ----------------------------------------------------------
    // start() must return false (and fill `error`) rather than pretend success.
    virtual bool start(const VmConfig& config, qint64* processId = nullptr,
                       QString* error = nullptr) const = 0;
    virtual bool stop(qint64 processId, QString* error = nullptr) const = 0;

    // --- diagnostics --------------------------------------------------------
    // Human-readable explanation of why this backend would or would not run
    // `config`. Implementations may combine probe() and validate() output.
    virtual QString diagnose(const VmConfig& config) const;

    // Stable, machine-readable summary used by `--list-backends --json`.
    virtual QString statusCode(const VmConfig& config) const;

    // Extra facts surfaced by the CLI (dependency/permission hints).
    virtual QStringList requirements() const;

    // True only for backends that own the guest inside this process. Such a VM
    // cannot outlive the VirtualWorld process, which the CLI reports verbatim.
    virtual bool runsInProcess() const { return false; }

    // For in-process backends: true while the guest is still running.
    virtual bool isRunning() const { return false; }
};

using VirtualizationEnginePtr = QSharedPointer<VirtualizationEngine>;

// ---------------------------------------------------------------------------
// QEMU-backed engines (hardware acceleration + TCG fallback)
// ---------------------------------------------------------------------------

// QEMU driven with the host hardware accelerator (hvf / whpx / kvm) when the
// guest is the same ISA as the host, otherwise this engine reports itself as
// unavailable so the registry falls through to TCG.
class QemuHardwareEngine : public VirtualizationEngine {
public:
    QString backendId() const override { return backend_id::qemuHardware(); }
    QString displayName() const override;
    VirtualizationTier tier() const override { return VirtualizationTier::HardwareAccelerated; }

    bool isHostSupported(QString* reason = nullptr) const override;
    bool supportsGuestArch(const QString& guestArch, QString* reason = nullptr) const override;
    EngineProbeResult probe() const override;
    EngineProbeResult probeFor(const QString& guestArch) const override;
    QVector<EngineIssue> validate(const VmConfig& config) const override;
    bool start(const VmConfig& config, qint64* processId = nullptr, QString* error = nullptr) const override;
    bool stop(qint64 processId, QString* error = nullptr) const override;
    QString diagnose(const VmConfig& config) const override;
    QString statusCode(const VmConfig& config) const override;
    QStringList requirements() const override;

private:
    QString acceleratorFor(const VmConfig& config, QString* reason = nullptr) const;
};

// QEMU TCG: no host virtualization required, any guest architecture, always the
// last fallback.
class QemuTcgEngine : public VirtualizationEngine {
public:
    QString backendId() const override { return backend_id::qemuTcg(); }
    QString displayName() const override;
    VirtualizationTier tier() const override { return VirtualizationTier::SoftwareEmulation; }

    bool isHostSupported(QString* reason = nullptr) const override;
    bool supportsGuestArch(const QString& guestArch, QString* reason = nullptr) const override;
    EngineProbeResult probe() const override;
    EngineProbeResult probeFor(const QString& guestArch) const override;
    QVector<EngineIssue> validate(const VmConfig& config) const override;
    bool start(const VmConfig& config, qint64* processId = nullptr, QString* error = nullptr) const override;
    bool stop(qint64 processId, QString* error = nullptr) const override;
    QString diagnose(const VmConfig& config) const override;
    QString statusCode(const VmConfig& config) const override;
    QStringList requirements() const override;
};

// ---------------------------------------------------------------------------
// Platform-specialized engines
// ---------------------------------------------------------------------------
//
// `available()` never lies: it returns false when the platform sources are not
// compiled into this build, so the registry can fall back to QEMU.
//
// LinuxKvmEngine and HarmonyVirtualizationEngine are owned by other workstreams.
// Nothing in this file includes their headers, so the tree builds whether or not
// their sources exist yet. When they do exist they join the registry by
// self-registering from their own translation unit:
//
//     #include "VirtualizationEngine.hpp"
//     #include "VirtualizationEngineRegistry.hpp"
//     namespace {
//     const bool registered = [] {
//         vw::VirtualizationEngineRegistry::instance().registerBackend(
//             vw::VirtualizationEnginePtr(new vw::LinuxKvmEngine()));
//         return true;
//     }();
//     }
//
// Until then (and whenever they are unavailable at runtime) QEMU hardware
// acceleration and TCG remain the fallback.

class WindowsHypervisorEngine;  // src/WindowsHypervisorEngine.hpp

// ---------------------------------------------------------------------------
// Platform backend factory surface
// ---------------------------------------------------------------------------
//
// The definitions live in the backend's own translation unit, which is only
// compiled when the platform sources are present (see the
// VIRTUALWORLD_HAVE_*_BACKEND definitions in CMakeLists.txt). These declarations
// are always visible so the registry can name the contract; the *calls* are
// always guarded, so a build without those sources never references them.

// macOS: Apple Virtualization.framework (src/MacVirtualizationEngine.hpp/.mm).
// Defined by src/VirtualizationEngine.cpp, which owns the Qt adapter.
bool macOSVirtualizationEngineAvailable();
VirtualizationEnginePtr makeMacOSVirtualizationEngine();

// HarmonyOS/OpenHarmony: QEMU compatibility backend.
// Defined by src/HarmonyVirtualizationEngine.cpp when it is compiled in.
// Never Native tier: the backend runs QEMU with KVM when the kernel exposes it.
bool harmonyVirtualizationEngineAvailable();
VirtualizationEnginePtr makeHarmonyVirtualizationEngine();

// True when the Harmony backend's factory is linked into this build. Safe to
// call on every platform; always false when the sources are absent.
bool harmonyBackendCompiledIn();

// Linux: KVM/QEMU compatibility backend (src/LinuxKvmEngine.hpp/.cpp).
// Defined by src/LinuxKvmEngine.cpp when it is compiled in. Never Native tier
// either — it reports HardwareAccelerated only when /dev/kvm is really usable
// for a same-ISA guest, and SoftwareEmulation otherwise.
bool linuxKvmEngineAvailable();
VirtualizationEnginePtr makeLinuxKvmEngine();

// True when the Linux KVM backend's factory is linked into this build.
bool linuxKvmBackendCompiledIn();

// Windows/Linux backends (WindowsHypervisorEngine, LinuxKvmEngine) join the
// catalogue through self-registration from their own translation units rather
// than through a factory declared here. That keeps the shared layer buildable
// whether they are present, absent, or still being rewritten.

// Windows: WHPX (src/WindowsHypervisorEngine.hpp/.cpp, _WIN32 only).
bool windowsHypervisorEngineAvailable();
VirtualizationEnginePtr makeWindowsHypervisorEngine();

// Raw WHPX capability probe. Safe to call on every platform; returns false with
// a reason when the host is not Windows or WHPX is not usable.
bool whpxAvailable(QString* reason = nullptr);

} // namespace vw
