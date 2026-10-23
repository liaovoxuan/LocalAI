#pragma once

// WindowsHypervisorEngine — the Windows native virtualization backend (WHPX).
//
// What this backend honestly is
// -----------------------------
// This backend *wraps the WHPX host hypervisor API* (Windows Hypervisor
// Platform) and hands same-ISA guests to it:
//
//   * tier() is therefore always VirtualizationTier::Native. The tier names the
//     *backend category* ("drives the host hypervisor API directly"), not the
//     outcome of the current host or guest — exactly like the sibling
//     macos-virtualization backend, which reports Native unconditionally.
//     Whether the native path can actually be used right now is answered by
//     hostSupported / guestArchSupported / accelerator / nativeExecution, never
//     by demoting the tier.
//   * If WHPX is genuinely available (winhvplatform.dll loads, WHvGetCapability
//     reports a hypervisor, the host ISA is known) AND the guest is same-ISA,
//     the backend drives QEMU with `-accel whpx` and reports
//     nativeExecution = true.
//   * If WHPX is not available on a Windows host, the backend still has to run
//     the guest somewhere: it drives QEMU with TCG software emulation and says
//     so through accelerator = "tcg" and nativeExecution = false.
//   * On Windows ARM64, WHPX may be available but with known limitations
//     (degraded). The backend still uses WHPX for aarch64 guests but reports
//     a diagnostic warning.
//   * On a non-Windows host, the backend reports host-unsupported and never
//     claims to have started a guest.
//
// Everything below is deliberately split so it can be unit-tested without a
// Windows host and without launching anything:
//
//   * Pure policy: WindowsHostState / capability / validation helpers, all
//     `inline`, driven entirely by an injected WindowsHostSnapshot.
//   * A single environment seam (WindowsEnvironment) that owns the four things
//     which cannot be pure: reading the host, resolving the QEMU program,
//     building its argument vector, and spawning/reaping the process. Tests
//     inject a fake; the real implementation lives in the .cpp.
//   * The engine itself only combines the two, so start()/stop() failure paths
//     are exercised in tests without a real process.
//
// Cross-architecture guests: rejected here, served by qemu-tcg
// -----------------------------------------------------------
// WHPX only supports same-ISA virtualization (x86_64 host → x86_64 guest,
// aarch64 host → aarch64 guest); cross-architecture hardware acceleration does
// not exist on any platform. A cross-architecture guest is therefore *not*
// claimed by this backend: supportsGuestArch() returns false and validate()
// reports `guest-arch-unsupported`, which is what lets the registry fall
// through to the qemu-tcg backend — the backend that owns software emulation.
// Claiming "supported, but only at TCG speed" would make auto-selection pick
// this WHPX-scoped backend for a guest it can never accelerate.
//
// windowsAcceleratorFor() still returns "tcg" for a cross-ISA guest, so the
// policy layer states the only accelerator this backend could ever request
// there; the accelerator is never "whpx" across ISAs.
//
// ARM64 WHPX degraded state
// -------------------------
// On Windows ARM64, WHPX is newer and may have limitations compared to x86_64.
// The backend detects this and reports a WhpxDegraded state with a diagnostic
// warning. The guest still runs via WHPX, but the user is informed that WHPX
// on ARM64 may have known issues.

#include "VirtualizationCapabilities.hpp"
#include "SystemSoftwareEngine.hpp"  // isSystemSoftwareConfig
#include "VirtualizationEngine.hpp"
#include "VmConfig.hpp"

#include <QSharedPointer>
#include <QString>
#include <QStringList>
#include <QVector>

#include <utility>

namespace vw {

// ---------------------------------------------------------------------------
// Legacy probe (kept for backward compatibility)
// ---------------------------------------------------------------------------

struct WhpxProbeResult {
    bool hostIsWindows = false;
    QString hostArch;             // canonical: x86_64 / aarch64 / other
    bool apiLoadable = false;     // winhvplatform.dll + WHvGetCapability present
    bool hypervisorPresent = false;
    bool whpxUsable = false;      // apiLoadable && hypervisorPresent && known arch
    QString reason;               // why it is not usable, empty when usable
    QString detail;               // human-readable probe summary
};

// Safe to call from any platform; on non-Windows hosts it reports
// `hostIsWindows == false` with an explanatory reason.
WhpxProbeResult probeWhpx();

// ---------------------------------------------------------------------------
// Guest architectures this backend may offer
// ---------------------------------------------------------------------------

inline QStringList windowsCandidateGuestArches() {
    return {QStringLiteral("x86_64"), QStringLiteral("aarch64")};
}

// Canonical guest architecture for this backend, or empty if unsupported.
inline QString windowsCanonicalGuestArch(const QString& value) {
    const QString canonical = canonicalGuestArch(value);
    if (canonical.isEmpty()) {
        return QString();
    }
    return windowsCandidateGuestArches().contains(canonical) ? canonical : QString();
}

// ---------------------------------------------------------------------------
// Host snapshot
// ---------------------------------------------------------------------------
//
// A complete, side-effect-free description of everything the policy layer needs.
// The real probe fills it once per engine instance; tests fill it by hand.

struct WindowsHostSnapshot {
    // --- host identity -------------------------------------------------------
    bool hostIsWindows = false;
    QString hostFamily;             // e.g. "windows"
    QString hostName;               // human readable, e.g. "Windows"
    QString hostArch;               // canonical host architecture

    // --- WHPX probe ----------------------------------------------------------
    bool apiLoadable = false;       // winhvplatform.dll + WHvGetCapability present
    bool hypervisorPresent = false; // WHvGetCapability reports hypervisor present
    bool whpxUsable = false;        // apiLoadable && hypervisorPresent && known arch

    // --- QEMU runtime --------------------------------------------------------
    bool qemuFound = false;         // at least one candidate binary exists on disk
    bool qemuExecutable = false;    // ... and at least one of them actually ran
    QString qemuPath;               // the runnable binary, when qemuExecutable
    QStringList runnableGuestArches; // arches with a probe-verified emulator

    // --- narration -----------------------------------------------------------
    QString reason;        // why WHPX is not usable, empty when usable
    QString detail;        // one-line human readable summary
    QStringList notes;     // extra diagnostics, safe to surface in the GUI
};

// ---------------------------------------------------------------------------
// Host state machine
// ---------------------------------------------------------------------------
//
// Every decision the backend makes about "can we run this guest" collapses to
// one of these five states.

enum class WindowsHostState {
    NotWindowsHost,              // not a Windows host at all
    WhpxApiMissing,              // winhvplatform.dll not found
    WhpxHypervisorNotPresent,    // API loaded but hypervisor not present
    WhpxDegraded,                // WHPX available on ARM64 with known limitations
    WhpxAvailable,               // WHPX fully available (same-ISA native)
};

inline bool windowsStateIsUsable(WindowsHostState state) {
    return state == WindowsHostState::WhpxDegraded
        || state == WindowsHostState::WhpxAvailable;
}

inline bool windowsStateIsNative(WindowsHostState state) {
    return state == WindowsHostState::WhpxDegraded
        || state == WindowsHostState::WhpxAvailable;
}

// True when the host state represents WHPX unavailable but QEMU TCG is still
// possible (the backend can run guests, just not with native acceleration).
inline bool windowsStateIsTcgFallback(WindowsHostState state) {
    return state == WindowsHostState::WhpxApiMissing
        || state == WindowsHostState::WhpxHypervisorNotPresent;
}

// Stable machine-readable code.
inline QString windowsStateCode(WindowsHostState state) {
    switch (state) {
    case WindowsHostState::NotWindowsHost:
        return QStringLiteral("host-unsupported");
    case WindowsHostState::WhpxApiMissing:
        return QStringLiteral("whpx-api-missing");
    case WindowsHostState::WhpxHypervisorNotPresent:
        return QStringLiteral("whpx-hypervisor-not-present");
    case WindowsHostState::WhpxDegraded:
        return QStringLiteral("whpx-degraded");
    case WindowsHostState::WhpxAvailable:
    default:
        return QStringLiteral("available");
    }
}

// Human-readable explanation.
inline QString windowsStateMessage(WindowsHostState state, const QString& guestArch) {
    const QString arch = windowsCanonicalGuestArch(guestArch).isEmpty()
        ? (guestArch.trimmed().isEmpty() ? QStringLiteral("（未指定）") : guestArch)
        : windowsCanonicalGuestArch(guestArch);
    switch (state) {
    case WindowsHostState::NotWindowsHost:
        return QStringLiteral("宿主不是 Windows，本后端不适用；请使用 QEMU 后端"
                               "（qemu-hardware / qemu-tcg）。");
    case WindowsHostState::WhpxApiMissing:
        return QStringLiteral("未找到 winhvplatform.dll，WHPX 不可用；"
                               "同架构客户机 %1 只能通过 QEMU TCG 软件模拟执行。")
            .arg(arch);
    case WindowsHostState::WhpxHypervisorNotPresent:
        return QStringLiteral("WHPX API 已加载但 Windows 未启用虚拟机平台/Hyper-V；"
                               "同架构客户机 %1 只能通过 QEMU TCG 软件模拟执行。")
            .arg(arch);
    case WindowsHostState::WhpxDegraded:
        return QStringLiteral("WHPX 在 ARM64 宿主上可用但可能存在已知限制；"
                               "同架构客户机 %1 将通过 WHPX 原生执行（tier=native，"
                               "nativeExecution=true），并附带 whpx-degraded 警告。")
            .arg(arch);
    case WindowsHostState::WhpxAvailable:
    default:
        return QStringLiteral("WHPX 可用：宿主可原生运行同架构客户机（tier=native）；"
                               "跨架构客户机不由本后端承担，请使用 qemu-tcg。");
    }
}

// The whole host/guest decision, as a pure function of the snapshot.
// This determines the WHPX state for same-ISA guests. Cross-ISA guests
// always fall back to TCG regardless of this state.
inline WindowsHostState windowsHostState(const WindowsHostSnapshot& snapshot) {
    if (!snapshot.hostIsWindows) {
        return WindowsHostState::NotWindowsHost;
    }
    if (!snapshot.apiLoadable) {
        return WindowsHostState::WhpxApiMissing;
    }
    if (!snapshot.hypervisorPresent) {
        return WindowsHostState::WhpxHypervisorNotPresent;
    }
    // ARM64 WHPX is available but with known limitations.
    if (snapshot.hostArch == QStringLiteral("aarch64")) {
        return WindowsHostState::WhpxDegraded;
    }
    return WindowsHostState::WhpxAvailable;
}

// True when `guestArch` names the same ISA as the host, which is the only case
// WHPX can ever accelerate. An empty/unknown guest arch is never same-ISA.
inline bool windowsGuestIsSameIsa(const WindowsHostSnapshot& snapshot,
                                  const QString& guestArch) {
    const QString guest = windowsCanonicalGuestArch(guestArch);
    if (guest.isEmpty()) {
        return false;
    }
    const QString host = canonicalHostArch(snapshot.hostArch);
    return !host.isEmpty() && host == guest;
}

// True when the request is a guest architecture this backend would have to
// reject because it is not the host ISA. Only meaningful for architectures the
// backend offers at all (windowsCanonicalGuestArch() non-empty).
inline bool windowsGuestIsCrossIsa(const WindowsHostSnapshot& snapshot,
                                   const QString& guestArch) {
    const QString guest = windowsCanonicalGuestArch(guestArch);
    if (guest.isEmpty()) {
        return false;
    }
    return !windowsGuestIsSameIsa(snapshot, guest);
}

// The accelerator token the backend requests for a specific guest architecture.
// Same-ISA + WHPX available → "whpx"; everything else → "tcg".
//
// The "tcg" branch is what a cross-ISA guest would get if this backend were
// forced to run it: cross-ISAs are never handed to WHPX. Auto-selection does
// not get that far — supportsGuestArch() rejects cross-ISA guests so the
// registry can pick qemu-tcg instead.
inline QString windowsAcceleratorFor(const WindowsHostSnapshot& snapshot,
                                     const QString& guestArch) {
    const WindowsHostState state = windowsHostState(snapshot);
    if (!windowsStateIsNative(state)) {
        return QStringLiteral("tcg");
    }
    // Same-ISA only: WHPX does not support cross-architecture virtualization.
    return windowsGuestIsSameIsa(snapshot, guestArch) ? QStringLiteral("whpx")
                                                      : QStringLiteral("tcg");
}

// True when the backend can actually run a guest. Same-ISA only: a cross-ISA
// guest is qemu-tcg's job, not this backend's.
inline bool windowsCanRunGuest(const WindowsHostSnapshot& snapshot,
                               const QString& guestArch) {
    const QString guest = windowsCanonicalGuestArch(guestArch);
    if (guest.isEmpty()) {
        return false;
    }
    if (!snapshot.hostIsWindows) {
        return false;
    }
    // Cross-ISA guests are rejected rather than silently degraded: see the
    // "Cross-architecture guests" note at the top of this file.
    if (!windowsGuestIsSameIsa(snapshot, guest)) {
        return false;
    }
    // QEMU must be available for any execution path.
    if (!snapshot.qemuExecutable) {
        return false;
    }
    return snapshot.runnableGuestArches.contains(guest);
}

// The execution tier actually achieved for one guest architecture, i.e. what
// the guest will really be run with. This is NOT the backend tier: the backend
// tier names the mechanism this backend wraps (WHPX → Native) and never moves.
inline VirtualizationTier windowsExecutionTierFor(const WindowsHostSnapshot& snapshot,
                                                  const QString& guestArch) {
    return windowsAcceleratorFor(snapshot, guestArch) == QStringLiteral("whpx")
        ? VirtualizationTier::Native
        : VirtualizationTier::SoftwareEmulation;
}

// ---------------------------------------------------------------------------
// Argument-vector safety
// ---------------------------------------------------------------------------

inline bool windowsContainsUnsafeCharacter(const QString& value) {
    return value.contains(QChar(0)) || value.contains(QLatin1Char('\n'))
        || value.contains(QLatin1Char('\r'));
}

inline bool windowsArgumentsAreWellFormed(const QStringList& arguments,
                                          QString* problem = nullptr) {
    const auto fail = [problem](const QString& why) {
        if (problem) {
            *problem = why;
        }
        return false;
    };
    if (arguments.isEmpty()) {
        return fail(QStringLiteral("参数数组为空：没有可执行的 QEMU 命令行。"));
    }
    for (const QString& argument : arguments) {
        if (argument.isNull()) {
            return fail(QStringLiteral("参数数组中存在空（null）元素。"));
        }
        if (windowsContainsUnsafeCharacter(argument)) {
            return fail(QStringLiteral("参数包含 NUL 或换行符，已拒绝以防注入。"));
        }
        if (argument.startsWith(QLatin1Char('-')) && argument.contains(QLatin1Char(' '))) {
            return fail(QStringLiteral("参数 \"%1\" 以 '-' 开头却包含空格，"
                                       "疑似把多个选项拼接成了一个字符串。")
                            .arg(argument));
        }
    }
    return true;
}

// ---------------------------------------------------------------------------
// Configuration validation (pure)
// ---------------------------------------------------------------------------

inline QVector<EngineIssue> windowsValidateConfig(const VmConfig& config,
                                                   const WindowsHostSnapshot& snapshot,
                                                   const QString& guestArch) {
    QVector<EngineIssue> issues;

    if (isSystemSoftwareConfig(config)) {
        issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("backend-mismatch"),
                              QStringLiteral("System Software 配置不使用 WHPX 后端。"));
        return issues;
    }

    const QString requestedArch = config.architecture.trimmed();
    if (requestedArch.isEmpty()) {
        issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("guest-arch-missing"),
                              QStringLiteral("客户机架构为空，无法选择 QEMU 系统模拟器。"));
        return issues;
    }
    if (guestArch.isEmpty()) {
        issues << engineIssue(
            EngineIssueLevel::Error, QStringLiteral("guest-arch-unsupported"),
            QStringLiteral("本后端仅提供 x86_64 / aarch64 客户机（架构 \"%1\" 请使用 QEMU 后端）。")
                .arg(requestedArch));
        return issues;
    }

    // Host applicability outranks every guest-architecture question. On a
    // non-Windows host this backend is simply not applicable, and reporting a
    // guest-architecture problem instead would both misdirect the user and hide
    // the real reason. statusCode() already prefers host-unsupported, so saying
    // anything else here would make the two disagree.
    if (!snapshot.hostIsWindows) {
        issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("host-unsupported"),
                              windowsStateMessage(WindowsHostState::NotWindowsHost, guestArch));
        return issues;
    }

    // Cross-ISA guests are rejected, not silently degraded. WHPX is a same-ISA
    // hypervisor API; running a foreign ISA is software emulation and therefore
    // qemu-tcg's job. Rejecting here is what lets the registry fall back.
    if (windowsGuestIsCrossIsa(snapshot, guestArch)) {
        issues << engineIssue(
            EngineIssueLevel::Error, QStringLiteral("guest-arch-unsupported"),
            QStringLiteral("客户机 %1 与宿主 %2 架构不同：WHPX 只支持同架构虚拟化，"
                           "不存在跨架构硬件加速。请使用 qemu-tcg 后端运行该客户机。")
                .arg(guestArch, canonicalHostArch(snapshot.hostArch)));
        return issues;
    }

    if (config.cpuCores < 1) {
        issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("cpu-count-invalid"),
                              QStringLiteral("CPU 核数必须至少为 1（当前 %1）。").arg(config.cpuCores));
    }
    if (config.memoryMb <= 0 && config.memoryKb <= 0) {
        issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("memory-too-small"),
                              QStringLiteral("内存必须大于 0。"));
    }

    const auto checkPath = [&issues](const QString& value, const QString& label,
                                     const QString& code) {
        if (value.isEmpty()) {
            return;
        }
        if (windowsContainsUnsafeCharacter(value)) {
            issues << engineIssue(EngineIssueLevel::Error, code,
                                  QStringLiteral("%1 包含 NUL 或换行符，已拒绝以防路径注入。").arg(label));
        }
    };
    checkPath(config.boot.kernelPath, QStringLiteral("内核镜像路径"), QStringLiteral("path-invalid"));
    checkPath(config.boot.initrdPath, QStringLiteral("initrd 路径"), QStringLiteral("path-invalid"));
    checkPath(config.firmware, QStringLiteral("固件路径"), QStringLiteral("path-invalid"));
    checkPath(config.efi.codePath, QStringLiteral("EFI 代码路径"), QStringLiteral("path-invalid"));
    checkPath(config.efi.varsPath, QStringLiteral("EFI 变量路径"), QStringLiteral("path-invalid"));
    for (const DiskConfig& disk : config.disks) {
        if (disk.path.trimmed().isEmpty()) {
            issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("disk-path-empty"),
                                  QStringLiteral("磁盘路径不能为空。"));
            continue;
        }
        checkPath(disk.path, QStringLiteral("磁盘镜像路径"), QStringLiteral("path-invalid"));
    }
    for (const QString& cdrom : config.cdroms) {
        checkPath(cdrom, QStringLiteral("光盘镜像路径"), QStringLiteral("path-invalid"));
    }

    for (const QString& argument : config.extraArgs + config.unsupportedArgs) {
        if (windowsContainsUnsafeCharacter(argument)) {
            issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("argument-invalid"),
                                  QStringLiteral("附加 QEMU 参数包含 NUL 或换行符，已拒绝以防注入。"));
            break;
        }
    }
    if (!config.unsupportedArgs.isEmpty()) {
        issues << engineIssue(EngineIssueLevel::Warning, QStringLiteral("unsupported-arguments"),
                              QStringLiteral("配置中有 %1 个无法等价转换的参数会被原样传递，请自行确认其安全性。")
                                  .arg(config.unsupportedArgs.size()));
    }

    // Host state diagnostics.
    const WindowsHostState state = windowsHostState(snapshot);
    const QString message = windowsStateMessage(state, guestArch);
    switch (state) {
    case WindowsHostState::NotWindowsHost:
        issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("host-unsupported"), message);
        break;
    case WindowsHostState::WhpxApiMissing:
        issues << engineIssue(EngineIssueLevel::Warning, QStringLiteral("whpx-api-missing"), message);
        break;
    case WindowsHostState::WhpxHypervisorNotPresent:
        issues << engineIssue(EngineIssueLevel::Warning, QStringLiteral("whpx-hypervisor-not-present"), message);
        break;
    case WindowsHostState::WhpxDegraded:
        issues << engineIssue(EngineIssueLevel::Warning, QStringLiteral("whpx-degraded"), message);
        break;
    case WindowsHostState::WhpxAvailable:
    default:
        issues << engineIssue(EngineIssueLevel::Info, QStringLiteral("whpx-available"), message);
        break;
    }

    // WHPX is not usable on this Windows host: the guest still has to run, so
    // say plainly which accelerator will be used instead.
    if (!windowsStateIsNative(state)) {
        issues << engineIssue(EngineIssueLevel::Warning, QStringLiteral("tcg-fallback"),
                              QStringLiteral("WHPX 在本宿主上不可用（%1），"
                                             "同架构客户机将通过 QEMU TCG 软件模拟执行。")
                                  .arg(windowsStateCode(state)));
    }

    // QEMU runtime availability — required even when WHPX is present because
    // the backend drives QEMU as the device model regardless of accelerator.
    if (!snapshot.qemuExecutable) {
        issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("runtime-missing"),
                              QStringLiteral("QEMU 运行时不可用；本后端需要 QEMU 提供设备模型。"));
    } else if (!snapshot.runnableGuestArches.contains(guestArch)) {
        issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("runtime-missing"),
                              QStringLiteral("未找到架构 %1 的 QEMU 系统模拟器。").arg(guestArch));
    }

    return issues;
}

// ---------------------------------------------------------------------------
// Environment seam
// ---------------------------------------------------------------------------
//
// The four things that cannot be pure. The real implementation (in the .cpp) is
// a thin wrapper over the application's existing QEMU plumbing; tests inject a
// scripted fake so every branch of start()/stop()/probe() is reachable without
// a Windows host and without spawning a process.

class WindowsEnvironment {
public:
    virtual ~WindowsEnvironment() = default;

    // Reads the host once. On a non-Windows host this must not probe WHPX
    // at all: it returns hostIsWindows == false immediately.
    virtual WindowsHostSnapshot snapshot() const = 0;

    // The resolved QEMU program and its argument vector for `config`. Used for
    // diagnostics and for the pre-flight well-formedness check. Empty when no
    // runtime could be resolved.
    virtual QString program(const VmConfig& config) const = 0;
    virtual QStringList arguments(const VmConfig& config) const = 0;

    // Launches the guest. Must return false (and fill `error`) rather than
    // pretend success. On success it fills a positive process id.
    virtual bool startProcess(const VmConfig& config, qint64* processId, QString* error) = 0;

    // Terminates a previously started guest.
    virtual bool stopProcess(qint64 processId, QString* error) = 0;
};

// Real environment: host probe + the application's QEMU runtime. Defined in
// WindowsHypervisorEngine.cpp.
QSharedPointer<WindowsEnvironment> makeWindowsEnvironment();

// ---------------------------------------------------------------------------
// The engine
// ---------------------------------------------------------------------------

class WindowsHypervisorEngine : public VirtualizationEngine {
public:
    WindowsHypervisorEngine()
        : environment_(makeWindowsEnvironment()) {}

    // Injection seam. Passing null is honoured literally (it does NOT silently
    // install the real environment), so tests can exercise the "engine has no
    // environment" guards.
    explicit WindowsHypervisorEngine(QSharedPointer<WindowsEnvironment> environment)
        : environment_(std::move(environment)) {}

    // --- identity ------------------------------------------------------------
    QString backendId() const override { return backend_id::windowsHypervisor(); }

    QString displayName() const override {
        return QStringLiteral("Windows Hypervisor Platform (WHPX)");
    }

    // The backend's tier is a property of the *mechanism it wraps*, not of the
    // current host or guest: this backend drives the WHPX host hypervisor API
    // (VirtualizationTier::Native), exactly like the sibling
    // macos-virtualization backend reports Native unconditionally.
    //
    // Demoting the tier to SoftwareEmulation when WHPX is absent would break the
    // shared ordering contract: the registry sorts candidates by tier, so an
    // emulation-tier Windows backend would sort *after* qemu-tcg and qemu-tcg
    // would stop being the last candidate. The honest per-host, per-guest answer
    // is carried by hostSupported / guestArchSupported / accelerator /
    // nativeExecution and by executionTierFor() below.
    VirtualizationTier tier() const override { return VirtualizationTier::Native; }

    // --- inspection seams ----------------------------------------------------
    const WindowsHostSnapshot& hostSnapshot() const {
        if (!hasSnapshot_) {
            cachedSnapshot_ = environment_ ? environment_->snapshot() : WindowsHostSnapshot();
            hasSnapshot_ = true;
        }
        return cachedSnapshot_;
    }

    void invalidateProbe() const {
        hasSnapshot_ = false;
        cachedSnapshot_ = WindowsHostSnapshot();
    }

    WindowsHostState stateFor(const QString& guestArch) const {
        Q_UNUSED(guestArch);
        // The WHPX host state does not depend on the guest architecture: the
        // state machine answers "is WHPX usable on this host at all?". Whether a
        // particular guest can use it is windowsCanRunGuest()'s answer.
        return windowsHostState(hostSnapshot());
    }

    QString acceleratorFor(const QString& guestArch) const {
        return windowsAcceleratorFor(hostSnapshot(), guestArch);
    }

    // What this guest would really be executed with, as opposed to the
    // backend's own (never-moving) tier.
    VirtualizationTier executionTierFor(const QString& guestArch) const override {
        return windowsExecutionTierFor(hostSnapshot(), guestArch);
    }

    bool sameIsaGuest(const QString& guestArch) const {
        return windowsGuestIsSameIsa(hostSnapshot(), guestArch);
    }

    QString programFor(const VmConfig& config) const {
        return environment_ ? environment_->program(launchedConfig(config)) : QString();
    }

    QStringList buildArguments(const VmConfig& config) const {
        return environment_ ? environment_->arguments(launchedConfig(config)) : QStringList();
    }

    // --- capability ----------------------------------------------------------
    bool isHostSupported(QString* reason = nullptr) const override;
    bool supportsGuestArch(const QString& guestArch, QString* reason = nullptr) const override;
    EngineProbeResult probe() const override;
    EngineProbeResult probeFor(const QString& guestArch) const override;

    // --- validation ----------------------------------------------------------
    QVector<EngineIssue> validate(const VmConfig& config) const override;

    // --- lifecycle -----------------------------------------------------------
    bool start(const VmConfig& config, qint64* processId = nullptr,
               QString* error = nullptr) const override;
    bool stop(qint64 processId, QString* error = nullptr) const override;

    // --- diagnostics ---------------------------------------------------------
    QString diagnose(const VmConfig& config) const override;
    QString statusCode(const VmConfig& config) const override;
    QStringList requirements() const override;
    bool runsInProcess() const override { return false; }

private:
    // Applies the backend's accelerator decision to a copy of the config.
    VmConfig launchedConfig(const VmConfig& config) const {
        VmConfig launched = config;
        launched.accelerator = windowsAcceleratorFor(hostSnapshot(), config.architecture);
        return launched;
    }

    QSharedPointer<WindowsEnvironment> environment_;
    mutable WindowsHostSnapshot cachedSnapshot_;
    mutable bool hasSnapshot_ = false;
};

// ---------------------------------------------------------------------------
// Factory functions
// ---------------------------------------------------------------------------

bool windowsHypervisorEngineAvailable();
VirtualizationEnginePtr makeWindowsHypervisorEngine();

} // namespace vw