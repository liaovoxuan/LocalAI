#pragma once

// VirtualizationEngineRegistry — backend catalogue and auto-selection.
//
// Selection order (fixed, never reordered by configuration):
//
//   1. Native virtualization for the same architecture (VZ.framework / WHPX /
//      KVM), when the backend exists in this build AND passes capability probing
//      AND validates the configuration without errors.
//   2. QEMU with the host hardware accelerator (hvf / whpx / kvm), same-ISA only.
//   3. QEMU TCG software emulation — always present, any architecture.
//
// A backend that fails validation is skipped and recorded in
// `EngineSelection::considered` with the reason, so `--list-backends` and
// `--check-engines` can explain exactly why the fallback happened.

#include "VirtualizationEngine.hpp"

#include <QSharedPointer>
#include <QString>
#include <QStringList>
#include <QVector>

namespace vw {

// How the caller wants the backend chosen.
enum class BackendPreference {
    Auto,       // native -> hardware -> TCG
    QemuOnly,   // hardware -> TCG (legacy `--start` behaviour, keeps QEMU detach semantics)
    Explicit    // one named backend, with the normal chain as a fallback
};

struct BackendChoice {
    QString backendId;
    QString displayName;
    VirtualizationTier tier = VirtualizationTier::SoftwareEmulation;
    bool hostSupported = false;
    bool guestArchSupported = false;
    bool configAccepted = false;  // validate() produced no errors
    bool selected = false;
    QString statusCode;           // machine-readable outcome
    QString reason;               // why it was or was not chosen
};

struct EngineSelection {
    bool ok = false;
    VirtualizationEnginePtr engine;
    QString backendId;
    QString displayName;
    VirtualizationTier tier = VirtualizationTier::SoftwareEmulation;
    QString reason;
    bool fellBack = false;              // true when a higher tier was rejected
    QString fallbackFrom;               // backend id of the preferred-but-rejected tier
    QVector<BackendChoice> considered;  // evaluated in order

    QStringList consideredIds() const;
};

class VirtualizationEngineRegistry {
public:
    static VirtualizationEngineRegistry& instance();

    // Registration. Later registrations with the same id replace earlier ones.
    void registerBackend(VirtualizationEnginePtr engine);
    bool hasBackend(const QString& backendId) const;

    QVector<VirtualizationEnginePtr> backends() const;
    QStringList backendIds() const;
    VirtualizationEnginePtr backend(const QString& backendId) const;

    // Registers the built-ins for this build. Idempotent.
    void registerBuiltinBackends();

    // Test seam: drops every registered backend and forgets that the built-ins
    // were registered, so an integration test can build a catalogue from
    // scratch (including the "nothing is registered" case). Production code
    // never calls this.
    void clear();

    // Backends sorted by tier: native, hardware, TCG.
    QVector<VirtualizationEnginePtr> backendsInPreferenceOrder() const;

    // Auto-selection for a configuration.
    EngineSelection select(const VmConfig& config,
                           BackendPreference preference = BackendPreference::Auto,
                           const QString& explicitBackendId = QString()) const;

    EngineSelection select(const VmConfig& config, const QString& preferenceText) const;

    // Capability report for CLI/GUI. `guestArch` empty means "no specific guest".
    QVector<EngineProbeResult> describeBackends(const QString& guestArch = QString()) const;

    // Machine-readable JSON describing every backend, the selection for a config
    // and the support matrix. Never throws.
    QString toJson(const VmConfig& config = VmConfig(),
                   const QString& guestArch = QString()) const;

private:
    VirtualizationEngineRegistry() = default;

    QVector<VirtualizationEnginePtr> backends_;
    bool builtinsRegistered_ = false;
};

// Preference parsing: "", "auto", "qemu", a backend id, or "qemu-tcg".
BackendPreference parseBackendPreference(const QString& value, bool* ok = nullptr);

// Resolves the effective preference from an explicit request, the config's
// `backend` field, and the VIRTUALWORLD_BACKEND environment variable.
QString resolveBackendPreference(const QString& requested, const VmConfig& config);

} // namespace vw
