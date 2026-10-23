#pragma once

// VirtualizationCapabilities — the shared vocabulary every VirtualWorld
// virtualization backend speaks.
//
// This header is deliberately Qt-based (QString/QVector) because the rest of the
// application (VmConfig, VMManager, the Qt GUI and the CLI) is Qt-based. Native
// platform backends that use std::string internally get adapted at the
// VirtualizationEngine layer, not here.
//
// The model is intentionally honest:
//   * "host supported" means this process can create a VM with this backend on
//     this machine right now.
//   * "guest arch supported" means the backend can run that guest architecture
//     at all. Native backends are 1:1 only; they never claim cross-architecture
//     hardware acceleration.
//   * Emulation of a foreign architecture is always QEMU TCG's job.

#include "Platform.hpp"
#include "VmConfig.hpp"

#include <QString>
#include <QStringList>
#include <QVector>

namespace vw {

// ---------------------------------------------------------------------------
// Architecture normalization
// ---------------------------------------------------------------------------

// Canonical architecture identifiers used across VirtualWorld:
//   x86_64, i386, aarch64, arm, riscv64, ppc, ppc64, m68k, loongarch64, ...
//
// This is stricter than vw::normalizeArchitecture(): it never folds a narrower
// ISA into a wider one, because a native hypervisor can only expose the host ISA
// at full width. "arm64" and "aarch64" collapse to "aarch64"; "x64"/"amd64"
// collapse to "x86_64"; "riscv64"/"riscv64gc" collapse to "riscv64".
QString canonicalGuestArch(const QString& value);
QString canonicalHostArch(const QString& value);

// True when the two canonical names describe the same host ISA for the purposes
// of native (1:1) virtualization. i386/x86_64 and arm/aarch64 are NOT the same
// ISA width for a native hypervisor.
bool isSameNativeIsa(const QString& hostArch, const QString& guestArch);

// The single guest architecture a native backend on this host could ever run,
// or an empty string when the host ISA has no native backend.
QString nativeGuestArchForHost(const QString& hostArch);

// ---------------------------------------------------------------------------
// Well-known backend identifiers
// ---------------------------------------------------------------------------
//
// Backends owned by other workstreams are listed here so the registry can wire
// them up without owning their sources.

// Stable machine-readable status codes.
//
// The **selector** (`selection.considered[].status`) emits only the canonical
// codes listed first below. A backend's own `backends[].status` may be more
// specific than that — for example WHPX distinguishes "the API dll is missing"
// from "the hypervisor is not enabled", and the QEMU-compatibility backends
// distinguish "degraded to TCG" from "the runtime cannot be executed at all".
// Those extended codes are listed second and are just as stable.
//
// Anything a script needs to branch on should use `status_class`, which folds
// every code — canonical, extended, or a backend-specific validation issue code
// — back into one of the canonical values.
namespace backend_status {

// --- canonical selector codes ---------------------------------------------
inline QString available() { return QStringLiteral("available"); }
inline QString hostUnsupported() { return QStringLiteral("host-unsupported"); }
inline QString guestArchUnsupported() { return QStringLiteral("guest-arch-unsupported"); }
inline QString acceleratorUnavailable() { return QStringLiteral("accelerator-unavailable"); }
inline QString runtimeMissing() { return QStringLiteral("runtime-missing"); }
inline QString configInvalid() { return QStringLiteral("config-invalid"); }
inline QString backendMismatch() { return QStringLiteral("backend-mismatch"); }
inline QString excludedByPolicy() { return QStringLiteral("excluded-by-policy"); }
inline QString noBackend() { return QStringLiteral("no-backend"); }
inline QString notRegistered() { return QStringLiteral("not-registered"); }

// --- extended backend codes -----------------------------------------------
// The backend can run the configuration, but only in software emulation.
inline QString degraded() { return QStringLiteral("degraded"); }
// A QEMU runtime was found but this platform forbids executing it.
inline QString runtimeNotRunnable() { return QStringLiteral("runtime-not-runnable"); }
// Windows Hypervisor Platform specifics.
inline QString whpxApiMissing() { return QStringLiteral("whpx-api-missing"); }
inline QString whpxHypervisorNotPresent() { return QStringLiteral("whpx-hypervisor-not-present"); }
inline QString whpxDegraded() { return QStringLiteral("whpx-degraded"); }

} // namespace backend_status

// Folds any status code — canonical, extended, or a backend-specific
// validation issue code — into one of the canonical values. An unrecognised
// code never maps to `available`: a code we do not know cannot be trusted to
// mean "this backend can run the guest", so unknown codes map to
// `config-invalid`.
QString statusClass(const QString& statusCode);

// Version of the `--list-backends --json` / `--check --json` document.
// 1: initial schema.
// 2: HarmonyOS/OpenHarmony rows report `hardware` (QEMU + KVM) instead of
//    `native`; the backend never claims a host hypervisor API it does not have.
// 3: `backends[].status_class` added and the extended WHPX / QEMU-compat status
//    codes documented, so `status` can stay backend-specific without breaking
//    scripts.
inline QString backendReportSchema() { return QStringLiteral("virtualworld.virtualization/3"); }

namespace backend_id {

inline QString macOSVirtualization() { return QStringLiteral("macos-virtualization"); }
inline QString windowsHypervisor() { return QStringLiteral("windows-hypervisor"); }
inline QString linuxKvm() { return QStringLiteral("linux-kvm"); }
inline QString harmonyVirtualization() { return QStringLiteral("harmony-virtualization"); }
inline QString qemuHardware() { return QStringLiteral("qemu-hardware"); }
inline QString qemuTcg() { return QStringLiteral("qemu-tcg"); }

// Legacy/compatibility aliases understood by the selector.
inline QString qemuLegacy() { return QStringLiteral("qemu"); }
inline QString autoSelect() { return QStringLiteral("auto"); }
inline QString systemSoftware() { return QStringLiteral("system-software"); }

} // namespace backend_id

// ---------------------------------------------------------------------------
// Backend tiers
// ---------------------------------------------------------------------------

// Auto-selection order: Native -> HardwareAccelerated -> SoftwareEmulation.
enum class VirtualizationTier {
    Native = 0,             // host hypervisor API (VZ.framework, WHPX, KVM)
    HardwareAccelerated = 1,// QEMU + host accelerator (hvf / whpx / kvm)
    SoftwareEmulation = 2   // QEMU TCG, always available as the last resort
};

QString tierName(VirtualizationTier tier);
QString tierDescription(VirtualizationTier tier);
int tierRank(VirtualizationTier tier);

// True only for backends that drive the host hypervisor API directly
// (Virtualization.framework / WHPX / KVM ioctls). A QEMU process that happens to
// be accelerated by KVM is *not* native, and must report
// VirtualizationTier::HardwareAccelerated instead.
bool tierIsNative(VirtualizationTier tier);

// True when a status code means "this backend can run the configuration".
bool statusIsUsable(const QString& statusCode);

// ---------------------------------------------------------------------------
// Diagnostics
// ---------------------------------------------------------------------------

enum class EngineIssueLevel { Info, Warning, Error };

QString engineIssueLevelName(EngineIssueLevel level);

struct EngineIssue {
    EngineIssueLevel level = EngineIssueLevel::Info;
    QString code;     // stable machine-readable identifier
    QString message;  // human readable, safe to show in the GUI

    bool isError() const { return level == EngineIssueLevel::Error; }
    bool isWarning() const { return level == EngineIssueLevel::Warning; }
};

EngineIssue engineIssue(EngineIssueLevel level, const QString& code, const QString& message);

bool hasEngineErrors(const QVector<EngineIssue>& issues);
bool hasEngineCode(const QVector<EngineIssue>& issues, const QString& code);
QString joinEngineIssues(const QVector<EngineIssue>& issues);

// ---------------------------------------------------------------------------
// Host virtualization capability probe
// ---------------------------------------------------------------------------

// What the host offers, independent of any single backend.
struct HostVirtualizationProfile {
    HostProfile host;              // from vw::detectHostProfile()
    QString hostArch;              // canonicalized
    bool hypervisorApiPresent = false; // VZ.framework / WHPX / KVM present
    bool kvmDevicePresent = false;     // /dev/kvm readable (Linux family)
    bool hvFSupported = false;         // macOS HVF possible for same-ISA guest
    bool whpxSupported = false;        // Windows WHPX possible
    QString detail;                // short human-readable explanation
    QStringList notes;             // extra diagnostics
};

HostVirtualizationProfile detectHostVirtualizationProfile();

// The accelerator QEMU would use on this host for a same-ISA guest, or "tcg".
QString hardwareAcceleratorForHost(const HostProfile& host);
QString hardwareAcceleratorForHost(const HostVirtualizationProfile& profile);

// ---------------------------------------------------------------------------
// Per-backend capability report
// ---------------------------------------------------------------------------

struct EngineCapabilities {
    bool hostSupported = false;      // backend usable on this host at all
    bool guestArchSupported = false; // backend can run the requested guest arch
    bool hardwareAcceleration = false;
    bool nativeExecution = false;    // runs the guest without QEMU emulation
    bool canStart = false;           // start() is expected to work
    bool canStop = false;            // stop() is expected to work
    QString accelerator;             // "hvf" / "whpx" / "kvm" / "tcg" / ""
    QString hostArch;
    QString summary;                 // one-line human readable status
    QVector<EngineIssue> issues;
};

// Probe result: capabilities plus the host description that produced them.
struct EngineProbeResult {
    bool hostSupported = false;
    QString backendId;
    QString displayName;
    VirtualizationTier tier = VirtualizationTier::SoftwareEmulation;
    EngineCapabilities capabilities;
    QVector<EngineIssue> diagnostics;
    QString summary;
};

// ---------------------------------------------------------------------------
// Versioned support matrix (documentation + CLI reporting)
// ---------------------------------------------------------------------------

// One row of the host/guest support matrix. `verified` is false for every
// platform combination that has not actually been exercised on real hardware;
// the CLI and the README both surface this verbatim.
struct SupportMatrixRow {
    QString hostPlatform;  // windows / macos / linux / harmonyos
    QString hostArch;      // x86_64 / aarch64 / riscv64
    QString guestArch;     // x86_64 / aarch64 / riscv64 / ...
    QString backendId;
    VirtualizationTier tier = VirtualizationTier::SoftwareEmulation;
    QString status;        // native / hardware / emulation / unsupported
    bool verified = false; // actually exercised on real hardware
    QString note;
};

QVector<SupportMatrixRow> defaultSupportMatrix();

} // namespace vw
