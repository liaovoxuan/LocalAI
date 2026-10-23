#pragma once

// HarmonyVirtualizationEngine — HarmonyOS / OpenHarmony virtualisation backend.
//
// What this backend honestly is
// -----------------------------
// HarmonyOS NEXT and OpenHarmony ship no public, stable, general-purpose
// hypervisor API that a third-party app may call the way macOS apps call
// Virtualization.framework or Windows apps call WHPX. There is therefore no
// native backend to write here, and this file does NOT pretend otherwise.
//
// Instead this is a *QEMU compatibility backend* scoped to HarmonyOS/OpenHarmony
// hosts running their Linux-compatible mode:
//
//   * If a linked in-process QEMU core exists AND the kernel exposes a usable
//     /dev/kvm for a same-ISA guest, the backend drives QEMU with hardware
//     acceleration (`-machine <m>,accel=kvm`) and reports
//     VirtualizationTier::HardwareAccelerated.
//   * Otherwise it drives QEMU with TCG software emulation and reports
//     VirtualizationTier::SoftwareEmulation — never "native".
//   * When the linked core is unavailable, the backend fails closed. It never
//     searches for or starts an external qemu-system executable and never
//     claims to have started a guest.
//
// Everything below is deliberately split so it can be unit-tested without a
// HarmonyOS device and without launching anything:
//
//   * Pure policy: HarmonyHostState / capability / validation helpers, all
//     `inline`, driven entirely by an injected HarmonyHostSnapshot.
//   * A single environment seam (HarmonyEnvironment) that owns the four things
//     which cannot be pure: reading the host, rendering the QEMU arguments, and
//     owning the in-process session. Tests inject a fake; the real implementation
//     lives in the .cpp and has no external-process fallback.
//   * The engine itself only combines the two, so start()/stop() failure paths
//     are exercised in tests without a real VM.
//
// Command construction
// --------------------
// There is no shell or process launcher anywhere in the real backend. A guest
// is described by a QStringList argument vector consumed by the linked core.
//
// Integration
// -----------
// The shared registry (src/VirtualizationEngineRegistry.hpp) supports
// self-registration from a backend's own translation unit, which is what this
// backend does at the bottom of the .cpp. harmonyVirtualizationEngineAvailable()
// and makeHarmonyVirtualizationEngine() are also declared here so the registry
// integrator can wire it explicitly instead. See docs/harmonyos-virtualization.md.

#include "VirtualizationCapabilities.hpp"
#include "SystemSoftwareEngine.hpp"  // isSystemSoftwareConfig, matches the other backends
#include "VirtualizationEngine.hpp"
#include "VmConfig.hpp"

#include <QSharedPointer>
#include <QString>
#include <QStringList>
#include <QVector>
#include <QMutex>
#include <QSet>

#include <functional>
#include <mutex>
#include <utility>

namespace vw {

// ---------------------------------------------------------------------------
// Guest architectures this backend may offer
// ---------------------------------------------------------------------------
//
// This is the *candidate* set, not a claim: whether any of them is actually
// runnable is decided per host by QEMU runtime probing (see
// HarmonyHostSnapshot::runnableGuestArches). ARM64 is the priority target
// because it is the architecture HarmonyOS devices actually ship.

inline QStringList harmonyCandidateGuestArches() {
    return {QStringLiteral("aarch64"), QStringLiteral("x86_64"), QStringLiteral("riscv64")};
}

// Canonical guest architecture, or an empty string when it is not one this
// backend can ever offer (ppc, m68k, i386, ... stay with the QEMU backends).
inline QString harmonyCanonicalGuestArch(const QString& value) {
    const QString canonical = canonicalGuestArch(value);
    if (canonical.isEmpty()) {
        return QString();
    }
    return harmonyCandidateGuestArches().contains(canonical) ? canonical : QString();
}

// ---------------------------------------------------------------------------
// Host snapshot
// ---------------------------------------------------------------------------
//
// A complete, side-effect-free description of everything the policy layer needs.
// The real probe fills it once per engine instance; tests fill it by hand.

struct HarmonyHostSnapshot {
    // --- host identity -------------------------------------------------------
    bool hostIsHarmony = false;   // HarmonyOS or OpenHarmony (Linux-compatible mode)
    QString hostFamily;           // e.g. "harmonyos-linux-compatible"
    QString hostName;             // human readable, e.g. "HarmonyOS (Linux-compatible mode)"
    QString hostPlatformName;     // "linux" / "macos" / "windows"
    QString hostArch;             // canonical host architecture

    // --- QEMU runtime --------------------------------------------------------
    bool qemuProbePerformed = false;   // false on non-Harmony hosts: we never probe there
    bool qemuFound = false;            // at least one candidate binary exists on disk
    bool qemuExecutable = false;       // ... and at least one of them actually ran (--version)
    bool qemuProbeTimedOut = false;    // --version exceeded the probe budget
    QString qemuPath;                  // the runnable binary, when qemuExecutable
    // Where the binary came from. Vocabulary: "runtime/qemu" (inside the
    // application's runtime tree), "PATH", "unrunnable" (a binary exists on disk
    // but this device refused to execute it — the HarmonyOS NEXT sandbox case),
    // or "missing". Never blank: the snapshot always states where it looked.
    QString qemuSource;
    QStringList runnableGuestArches;   // arches with a probe-verified emulator

    // --- hardware acceleration ----------------------------------------------
    bool kvmDevicePresent = false;     // /dev/kvm exists
    bool kvmDeviceAccessible = false;  // /dev/kvm is readable and writable

    // --- narration -----------------------------------------------------------
    QString detail;        // one-line human readable summary
    QStringList notes;     // extra diagnostics, safe to surface in the GUI
};

// ---------------------------------------------------------------------------
// Host state machine
// ---------------------------------------------------------------------------
//
// Every decision the backend makes about "can we run this guest" collapses to
// one of these five states. The names are chosen so that no state can be
// mistaken for native virtualisation.

enum class HarmonyHostState {
    NotHarmonyHost,      // not a HarmonyOS/OpenHarmony host at all
    RuntimeMissing,      // no QEMU system emulator for this guest architecture
    RuntimeNotRunnable,  // a QEMU binary exists but cannot be executed (sandbox/lockdown)
    TcgOnly,             // usable, software emulation (TCG)
    Accelerated,         // usable, same-ISA hardware acceleration (KVM)
};

inline bool harmonyStateIsUsable(HarmonyHostState state) {
    return state == HarmonyHostState::TcgOnly || state == HarmonyHostState::Accelerated;
}

inline bool harmonyStateIsAccelerated(HarmonyHostState state) {
    return state == HarmonyHostState::Accelerated;
}

// Stable machine-readable code. `degraded` is deliberately distinct from
// `available`: it means "this will work, but by software emulation only".
inline QString harmonyStateCode(HarmonyHostState state) {
    switch (state) {
    case HarmonyHostState::NotHarmonyHost:
        return QStringLiteral("host-unsupported");
    case HarmonyHostState::RuntimeMissing:
        return QStringLiteral("runtime-missing");
    case HarmonyHostState::RuntimeNotRunnable:
        return QStringLiteral("runtime-not-runnable");
    case HarmonyHostState::TcgOnly:
        return QStringLiteral("degraded");
    case HarmonyHostState::Accelerated:
    default:
        return QStringLiteral("available");
    }
}

// Human-readable explanation. Always non-empty so callers can surface it
// verbatim as the `QString* reason` of the shared contract.
inline QString harmonyStateMessage(HarmonyHostState state, const QString& guestArch) {
    const QString arch = harmonyCanonicalGuestArch(guestArch).isEmpty()
        ? (guestArch.trimmed().isEmpty() ? QStringLiteral("（未指定）") : guestArch)
        : harmonyCanonicalGuestArch(guestArch);
    switch (state) {
    case HarmonyHostState::NotHarmonyHost:
        return QStringLiteral("宿主不是 HarmonyOS/OpenHarmony，本后端不适用；请使用 QEMU 后端"
                              "（qemu-hardware / qemu-tcg）。");
    case HarmonyHostState::RuntimeMissing:
        // Both `%1` markers carry the same value and Qt's arg() replaces every
        // occurrence of the lowest-numbered marker in a single call, so exactly
        // one .arg() is correct here. A second call would warn "Argument missing"
        // and `.arg(a, b)` would warn about the unused `%2`.
        return QStringLiteral("宿主是 HarmonyOS/OpenHarmony，但未连入可用的进程内 QEMU %1 核心；"
                              "出于沙箱安全约束，不会回退到 qemu-system-%1 外部子进程。")
            .arg(arch);
    case HarmonyHostState::RuntimeNotRunnable:
        return QStringLiteral("找到了 QEMU 可执行文件，但无法运行它（HarmonyOS NEXT 沙箱/设备策略通常"
                              "禁止任意进程执行）；本后端不会声称已启动客户机，请改用 qemu-tcg 或申请"
                              "相应的执行权限。");
    case HarmonyHostState::TcgOnly:
        return QStringLiteral("可运行 QEMU %1 客户机，但没有可用的同架构硬件加速（/dev/kvm 未暴露或"
                              "不可读写），将使用 TCG 软件模拟（tier=emulation，不是原生虚拟化）。")
            .arg(arch);
    case HarmonyHostState::Accelerated:
    default:
        return QStringLiteral("可运行 QEMU %1 客户机，且 /dev/kvm 对同架构客户机可用，将请求 KVM 硬件"
                              "加速（仍由 QEMU 提供设备模型，不是宿主原生虚拟化接口）。")
            .arg(arch);
    }
}

// The whole host/guest decision, as a pure function of the snapshot.
inline HarmonyHostState harmonyHostState(const HarmonyHostSnapshot& snapshot,
                                         const QString& guestArch) {
    if (!snapshot.hostIsHarmony) {
        return HarmonyHostState::NotHarmonyHost;
    }
    const QString arch = harmonyCanonicalGuestArch(guestArch);
    if (arch.isEmpty()) {
        // Not an architecture this backend can ever offer.
        return HarmonyHostState::RuntimeMissing;
    }
    if (snapshot.qemuFound && !snapshot.qemuExecutable) {
        return HarmonyHostState::RuntimeNotRunnable;
    }
    if (!snapshot.runnableGuestArches.contains(arch)) {
        return HarmonyHostState::RuntimeMissing;
    }
    // Hardware acceleration is same-ISA only. Cross-architecture guests are
    // always TCG; there is no cross-architecture hardware acceleration anywhere.
    const QString hostArch = canonicalHostArch(snapshot.hostArch);
    const bool sameIsa = !hostArch.isEmpty() && hostArch == arch;
    if (sameIsa && snapshot.kvmDevicePresent && snapshot.kvmDeviceAccessible) {
        return HarmonyHostState::Accelerated;
    }
    return HarmonyHostState::TcgOnly;
}

// The accelerator token the backend requests. It is fed into
// VmConfig::accelerator, which the shared translator renders as
// `-machine <machine>,accel=<token>` — and which the shared
// selectAccelerator() may still downgrade to "tcg" if the host cannot honour
// it. That double check is intentional: this function states the intent, the
// translator enforces reality.
inline QString harmonyAcceleratorFor(const HarmonyHostSnapshot& snapshot,
                                     const QString& guestArch) {
    return harmonyStateIsAccelerated(harmonyHostState(snapshot, guestArch))
        ? QStringLiteral("kvm")
        : QStringLiteral("tcg");
}

// True when the host can hardware-accelerate EVERY guest architecture it
// advertises. Hardware acceleration is same-ISA only (KVM is bound to one
// architecture), so a host that reports a cross-ISA guest it cannot accelerate
// must NOT be reported as "has acceleration": that claim would be misleading for
// the cross-ISA guest, which will always run TCG. This mirrors requirement #2 —
// hardware acceleration is reported only when truly available AND same-ISA.
inline bool harmonyHostHasAcceleration(const HarmonyHostSnapshot& snapshot) {
    if (!snapshot.hostIsHarmony || !snapshot.kvmDevicePresent || !snapshot.kvmDeviceAccessible) {
        return false;
    }
    const QString hostArch = canonicalHostArch(snapshot.hostArch);
    if (hostArch.isEmpty() || !harmonyCandidateGuestArches().contains(hostArch)) {
        return false;
    }
    if (snapshot.runnableGuestArches.isEmpty()) {
        return false;
    }
    // Every advertised guest must be the host's own (same-ISA) architecture. Any
    // cross-ISA guest means the host cannot accelerate everything it offers.
    for (const QString& arch : snapshot.runnableGuestArches) {
        if (canonicalHostArch(arch) != hostArch) {
            return false;
        }
    }
    return true;
}

// ---------------------------------------------------------------------------
// Argument-vector safety
// ---------------------------------------------------------------------------

// True when `value` carries a character that must never reach a process argument
// (an embedded NUL or a line break). Used for guest paths and command lines.
inline bool harmonyContainsUnsafeCharacter(const QString& value) {
    return value.contains(QChar(0)) || value.contains(QLatin1Char('\n'))
        || value.contains(QLatin1Char('\r'));
}

// Structural guarantee for the argument vector. This backend never builds a
// command string, so the invariants worth asserting are:
//   * the vector is non-empty and every element is a real (non-null) string;
//   * no element smuggles a NUL or a line break;
//   * no element starting with '-' contains whitespace, which is the signature
//     of several flags having been concatenated into one string.
// Legitimate values that contain spaces (a disk path, a kernel command line)
// are unaffected because they are separate, non-dash-prefixed elements.
inline bool harmonyArgumentsAreWellFormed(const QStringList& arguments, QString* problem = nullptr) {
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
        if (harmonyContainsUnsafeCharacter(argument)) {
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

inline QVector<EngineIssue> harmonyValidateConfig(const VmConfig& config,
                                                  const HarmonyHostSnapshot& snapshot,
                                                  const QString& guestArch) {
    QVector<EngineIssue> issues;

    if (isSystemSoftwareConfig(config)) {
        issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("backend-mismatch"),
                              QStringLiteral("System Software 配置不使用虚拟化后端。"));
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
            QStringLiteral("本后端仅提供 aarch64 / x86_64 / riscv64 客户机（架构 \"%1\" 请使用 QEMU 后端）。")
                .arg(requestedArch));
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
        if (harmonyContainsUnsafeCharacter(value)) {
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

    // Extra/unsupported arguments are already a vector, so they cannot inject a
    // second command. They are still checked for smuggled control characters.
    for (const QString& argument : config.extraArgs + config.unsupportedArgs) {
        if (harmonyContainsUnsafeCharacter(argument)) {
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

    const HarmonyHostState state = harmonyHostState(snapshot, guestArch);
    const QString message = harmonyStateMessage(state, guestArch);
    switch (state) {
    case HarmonyHostState::NotHarmonyHost:
        issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("host-unsupported"), message);
        break;
    case HarmonyHostState::RuntimeMissing:
        issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("runtime-missing"), message);
        break;
    case HarmonyHostState::RuntimeNotRunnable:
        issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("runtime-not-runnable"), message);
        break;
    case HarmonyHostState::TcgOnly:
        // Degraded, not broken: usable, so this must stay a warning or the
        // registry would skip the only backend that can actually run here.
        issues << engineIssue(EngineIssueLevel::Warning, QStringLiteral("software-emulation"), message);
        break;
    case HarmonyHostState::Accelerated:
    default:
        issues << engineIssue(EngineIssueLevel::Info, QStringLiteral("hardware-acceleration"), message);
        break;
    }

    return issues;
}

// ---------------------------------------------------------------------------
// Environment seam
// ---------------------------------------------------------------------------
//
// The four things that cannot be pure. The real implementation (in the .cpp) is
// a fail-closed seam for the linked in-process QEMU core; tests inject a
// scripted fake so every branch of start()/stop()/probe() is reachable without
// a HarmonyOS device or a VM start.

class HarmonyEnvironment {
public:
    virtual ~HarmonyEnvironment() = default;

    // Reads the host once. On a non-Harmony host this must not probe QEMU at
    // all: it returns hostIsHarmony == false immediately.
    virtual HarmonyHostSnapshot snapshot() const = 0;

    // The resolved QEMU program and its argument vector for `config`. Used for
    // diagnostics and for the pre-flight well-formedness check. Empty when no
    // runtime could be resolved.
    virtual QString program(const VmConfig& config) const = 0;
    virtual QStringList arguments(const VmConfig& config) const = 0;

    // Starts the in-process guest. The legacy method/token names are retained
    // for the shared VirtualizationEngine ABI; the positive token is not a PID.
    virtual bool startProcess(const VmConfig& config, qint64* processId, QString* error) = 0;

    // Stops a previously started in-process guest session.
    virtual bool stopProcess(qint64 processId, QString* error) = 0;
};

// Real environment: host probe + the application's QEMU runtime. Defined in
// HarmonyVirtualizationEngine.cpp.
QSharedPointer<HarmonyEnvironment> makeHarmonyEnvironment();

// ---------------------------------------------------------------------------
// Frontend callback gate (Display / Input / Audio / Block)
// ---------------------------------------------------------------------------
//
// The in-process QEMU core (QemuInProcessRuntime) exposes a QemuCallbacks ABI
// for display / input / audio / block. That ABI is NOT yet truly wired to a live
// QEMU frontend in this build — it is owned by the same DeepSeek workstream that
// owns the safe-init gate. Per the integration contract this backend must
// therefore NEVER fake a connected callback: every registration returns
// NotSupported (or NotReady once the ABI exists but no session is live) and
// stores nothing. A silent success here would let a caller believe a
// framebuffer / disk is live when it is not — exactly the "假回调" this
// integration forbids.

enum class HarmonyFrontendStatus {
    NotSupported,  // callback ABI not connected to a real QEMU frontend in this build
    NotReady       // ABI exists but no live in-process session carries the data yet
};

struct HarmonyFrontendCallbacks {
    std::function<void(const void* pixels, int w, int h, int stride, int format)> displayUpdate;
    std::function<int(const void* event, size_t len)> inputEvent;
    std::function<void(const int16_t* samples, size_t frames, int channels, int rate)> audioOut;
    std::function<int(int16_t* samples, size_t frames, int channels, int rate)> audioIn;
    std::function<int(uint32_t diskId, uint64_t sector, void* buf, size_t len)> blockRead;
    std::function<int(uint32_t diskId, uint64_t sector, const void* buf, size_t len)> blockWrite;
};

// ---------------------------------------------------------------------------
// The engine
// ---------------------------------------------------------------------------

class HarmonyVirtualizationEngine : public VirtualizationEngine {
public:
    HarmonyVirtualizationEngine()
        : environment_(makeHarmonyEnvironment()) {}

    // Injection seam. Passing null is honoured literally (it does NOT silently
    // install the real environment), so tests can exercise the "engine has no
    // environment" guards instead of accidentally spawning a real process.
    explicit HarmonyVirtualizationEngine(QSharedPointer<HarmonyEnvironment> environment)
        : environment_(std::move(environment)) {}

    // --- identity ------------------------------------------------------------
    QString backendId() const override { return backend_id::harmonyVirtualization(); }

    QString displayName() const override {
        return QStringLiteral("HarmonyOS/OpenHarmony QEMU 兼容后端（KVM/TCG）");
    }

    // Honest tier: hardware-accelerated only when this host really exposes a
    // usable same-ISA accelerator; otherwise this backend IS software emulation.
    VirtualizationTier tier() const override {
        return harmonyHostHasAcceleration(hostSnapshot()) ? VirtualizationTier::HardwareAccelerated
                                                          : VirtualizationTier::SoftwareEmulation;
    }

    // --- inspection seams ----------------------------------------------------
    // Returns a *copy*, not a reference. The capability queries below bind the
    // result to a local and keep using it while the object stays alive, and they
    // are const, so they may legitimately be reached from several threads at once
    // (the CLI enumerates every backend while the GUI thread can probe
    // independently). Handing out a reference into the cache would let a
    // concurrent invalidateProbe() reassign cachedSnapshot_ — and wipe the
    // QStringList it aliases — under a reader. That exact race is what this
    // signature exists to prevent; returning by value copies under the lock, so
    // the snapshot a caller holds is immutable for as long as they hold it.
    HarmonyHostSnapshot hostSnapshot() const {
        std::lock_guard<std::mutex> lock(snapshotMutex_);
        if (!hasSnapshot_) {
            cachedSnapshot_ = environment_ ? environment_->snapshot() : HarmonyHostSnapshot();
            hasSnapshot_ = true;
        }
        return cachedSnapshot_;
    }

    // Drops the cached probe so the next query re-reads the host.
    void invalidateProbe() const {
        std::lock_guard<std::mutex> lock(snapshotMutex_);
        hasSnapshot_ = false;
        cachedSnapshot_ = HarmonyHostSnapshot();
    }

    HarmonyHostState stateFor(const QString& guestArch) const {
        return harmonyHostState(hostSnapshot(), guestArch);
    }

    QString programFor(const VmConfig& config) const {
        return environment_ ? environment_->program(launchedConfig(config)) : QString();
    }

    // The exact argument vector that start() will hand to the in-process core,
    // with the backend's accelerator decision applied. Never a shell string.
    QStringList buildArguments(const VmConfig& config) const {
        return environment_ ? environment_->arguments(launchedConfig(config)) : QStringList();
    }

    // --- capability ----------------------------------------------------------
    bool isHostSupported(QString* reason = nullptr) const override {
        const HarmonyHostSnapshot& host = hostSnapshot();
        if (!host.hostIsHarmony) {
            if (reason) {
                *reason = harmonyStateMessage(HarmonyHostState::NotHarmonyHost, QString());
            }
            return false;
        }
        if (!host.runnableGuestArches.isEmpty()) {
            return true;
        }
        if (reason) {
            const HarmonyHostState state = host.qemuFound ? HarmonyHostState::RuntimeNotRunnable
                                                          : HarmonyHostState::RuntimeMissing;
            *reason = harmonyStateMessage(state, host.hostArch);
            if (!host.notes.isEmpty()) {
                *reason += QStringLiteral(" ") + host.notes.join(QStringLiteral(" "));
            }
        }
        return false;
    }

    bool supportsGuestArch(const QString& guestArch, QString* reason = nullptr) const override {
        const QString arch = harmonyCanonicalGuestArch(guestArch);
        if (arch.isEmpty()) {
            if (reason) {
                *reason = guestArch.trimmed().isEmpty()
                    ? QStringLiteral("未指定客户机架构。")
                    : QStringLiteral("本后端仅提供 aarch64 / x86_64 / riscv64 客户机，"
                                     "架构 \"%1\" 请使用 QEMU 后端。")
                          .arg(guestArch.trimmed());
            }
            return false;
        }
        const HarmonyHostState state = stateFor(arch);
        if (harmonyStateIsUsable(state)) {
            return true;
        }
        if (reason) {
            *reason = harmonyStateMessage(state, arch);
        }
        return false;
    }

    EngineProbeResult probe() const override {
        const HarmonyHostSnapshot& host = hostSnapshot();

        EngineProbeResult result;
        result.backendId = backendId();
        result.displayName = displayName();
        result.tier = tier();
        result.hostSupported = isHostSupported();

        EngineCapabilities& capabilities = result.capabilities;
        capabilities.hostSupported = result.hostSupported;
        capabilities.hostArch = canonicalHostArch(host.hostArch);
        capabilities.hardwareAcceleration = harmonyHostHasAcceleration(host);
        // Never true: this backend always runs the guest inside QEMU, even when
        // KVM is accelerating the CPU. Claiming otherwise would be dishonest.
        capabilities.nativeExecution = false;

        QStringList usableArches;
        for (const QString& arch : harmonyCandidateGuestArches()) {
            if (harmonyStateIsUsable(harmonyHostState(host, arch))) {
                usableArches << arch;
            }
        }
        capabilities.guestArchSupported = !usableArches.isEmpty();
        capabilities.canStart = capabilities.guestArchSupported;
        capabilities.canStop = capabilities.guestArchSupported;
        capabilities.accelerator = capabilities.hardwareAcceleration ? QStringLiteral("kvm")
                                                                     : QStringLiteral("tcg");

        QStringList summary;
        if (!host.hostIsHarmony) {
            summary << QStringLiteral("宿主不是 HarmonyOS/OpenHarmony（%1）：本后端不适用，回退 QEMU 后端。")
                           .arg(host.hostName.isEmpty() ? QStringLiteral("unknown") : host.hostName);
        } else if (usableArches.isEmpty()) {
            summary << (host.qemuFound
                            ? QStringLiteral("检测到 QEMU 但无法执行：设备策略可能禁止任意进程执行，"
                                             "回退 qemu-tcg 或申请执行权限。")
                            : QStringLiteral("进程内 QEMU 核心未连入；已禁止外部进程回退。"));
        } else {
            summary << QStringLiteral("HarmonyOS/OpenHarmony 兼容后端可用：客户机 %1，加速器 %2。")
                           .arg(usableArches.join(QStringLiteral(", ")),
                                capabilities.accelerator);
            if (!capabilities.hardwareAcceleration) {
                summary << QStringLiteral("（无同架构硬件加速，明确使用 TCG 软件模拟，不是原生虚拟化。）");
            }
        }
        result.summary = summary.join(QStringLiteral(" "));
        if (!host.detail.isEmpty()) {
            result.diagnostics << engineIssue(EngineIssueLevel::Info, QStringLiteral("host-detail"),
                                              host.detail);
        }
        for (const QString& note : host.notes) {
            result.diagnostics << engineIssue(EngineIssueLevel::Info, QStringLiteral("host-note"), note);
        }
        if (host.qemuProbeTimedOut) {
            result.diagnostics << engineIssue(EngineIssueLevel::Warning,
                                              QStringLiteral("runtime-probe-timeout"),
                                              QStringLiteral("QEMU 运行时探测超时，结果可能不完整。"));
        }
        if (host.hostIsHarmony) {
            const HarmonyHostState state = usableArches.isEmpty()
                ? (host.qemuFound ? HarmonyHostState::RuntimeNotRunnable
                                  : HarmonyHostState::RuntimeMissing)
                : (capabilities.hardwareAcceleration ? HarmonyHostState::Accelerated
                                                     : HarmonyHostState::TcgOnly);
            const EngineIssueLevel level = harmonyStateIsUsable(state)
                ? (harmonyStateIsAccelerated(state) ? EngineIssueLevel::Info
                                                    : EngineIssueLevel::Warning)
                : EngineIssueLevel::Error;
            result.diagnostics << engineIssue(level, harmonyStateCode(state),
                                              harmonyStateMessage(state, host.hostArch));
        }
        return result;
    }

    // Per-architecture fast path: the default implementation would keep a
    // per-host canStart, which is wrong for a backend that owns a different
    // QEMU binary per guest architecture.
    EngineProbeResult probeFor(const QString& guestArch) const override {
        EngineProbeResult result = probe();
        const QString arch = harmonyCanonicalGuestArch(guestArch);
        QString reason;
        const bool supported = !guestArch.trimmed().isEmpty() && supportsGuestArch(guestArch, &reason);
        result.capabilities.guestArchSupported = supported;
        result.capabilities.hardwareAcceleration = supported && harmonyStateIsAccelerated(stateFor(arch));
        result.capabilities.accelerator = result.capabilities.hardwareAcceleration
            ? QStringLiteral("kvm")
            : QStringLiteral("tcg");
        result.capabilities.canStart = supported;
        result.capabilities.canStop = supported;
        if (!supported && !reason.isEmpty()) {
            result.diagnostics << engineIssue(EngineIssueLevel::Error,
                                              QStringLiteral("guest-arch-unavailable"), reason);
            result.summary = reason;
        }
        return result;
    }

    // --- validation ----------------------------------------------------------
    QVector<EngineIssue> validate(const VmConfig& config) const override {
        return harmonyValidateConfig(config, hostSnapshot(), harmonyCanonicalGuestArch(config.architecture));
    }

    // --- lifecycle -----------------------------------------------------------
    // The `processId` out-parameter is an OPAQUE session token, NOT an OS PID.
    // This backend drives QEMU in-process through QemuInProcessRuntime and never
    // calls workerPid(); it never exposes or compares a process id. The token is
    // only ever tested for equality by stop()/diagnostics.
    bool start(const VmConfig& config, qint64* processId = nullptr,
               QString* error = nullptr) const override {
        const QVector<EngineIssue> issues = validate(config);
        if (hasEngineErrors(issues)) {
            if (error) {
                *error = joinEngineIssues(issues);
            }
            return false;
        }
        if (!environment_) {
            if (error) {
                *error = QStringLiteral("内部错误：虚拟化环境未初始化。");
            }
            return false;
        }

        const VmConfig launch = launchedConfig(config);
        const QString program = environment_->program(launch);
        if (program.trimmed().isEmpty()) {
            if (error) {
                *error = harmonyStateMessage(HarmonyHostState::RuntimeMissing,
                                             harmonyCanonicalGuestArch(config.architecture));
            }
            return false;
        }
        QString problem;
        const QStringList arguments = environment_->arguments(launch);
        if (!harmonyArgumentsAreWellFormed(arguments, &problem)) {
            if (error) {
                *error = QStringLiteral("QEMU 参数数组未通过安全校验：") + problem;
            }
            return false;
        }

        qint64 sessionToken = 0;
        if (!environment_->startProcess(launch, &sessionToken, error)) {
            return false;
        }
        if (sessionToken <= 0) {
            // The runtime did not give us an opaque session token, so we cannot
            // honestly claim a guest is running.
            if (error) {
                *error = QStringLiteral("进程内 QEMU 未返回有效的会话令牌，未声称客户机已启动。");
            }
            return false;
        }
        {
            // Record the issued token so a later stop() can prove provenance and
            // refuse unknown / never-started tokens without touching the environment.
            QMutexLocker lock(&activeSessionsMutex_);
            activeSessions_.insert(sessionToken);
        }
        if (processId) {
            *processId = sessionToken;
        }
        return true;
    }

    // `processId` here is the opaque session token returned by start(); it is not
    // a PID and is never interpreted as one.
    bool stop(qint64 processId, QString* error = nullptr) const override {
        if (!environment_) {
            if (error) {
                *error = QStringLiteral("内部错误：虚拟化环境未初始化。");
            }
            return false;
        }
        if (processId <= 0) {
            if (error) {
                *error = QStringLiteral("无效的会话令牌，没有可停止的客户机。");
            }
            return false;
        }
        // Reject tokens this engine never issued. An unknown token must never reach
        // the environment (no spawn, no fake stop, no core call with a bogus id).
        {
            QMutexLocker lock(&activeSessionsMutex_);
            if (!activeSessions_.contains(processId)) {
                if (error) {
                    *error = QStringLiteral("该会话令牌并非由此引擎启动，无法停止。");
                }
                return false;
            }
        }
        return environment_->stopProcess(processId, error);
    }

    // --- diagnostics ---------------------------------------------------------
    QString diagnose(const VmConfig& config) const override {
        const HarmonyHostSnapshot& host = hostSnapshot();
        QStringList lines;
        lines << QStringLiteral("%1 (%2) — %3").arg(displayName(), backendId(), tierDescription(tier()));
        lines << QStringLiteral("宿主: %1 [%2] arch=%3")
                     .arg(host.hostName.isEmpty() ? QStringLiteral("unknown") : host.hostName,
                          host.hostFamily.isEmpty() ? QStringLiteral("unknown") : host.hostFamily,
                          canonicalHostArch(host.hostArch));
        if (!host.hostIsHarmony) {
            lines << QStringLiteral("结论: 宿主不是 HarmonyOS/OpenHarmony，本后端不适用，请使用 QEMU 后端。");
            return lines.join(QLatin1Char('\n'));
        }
        lines << QStringLiteral("QEMU: %1")
                     .arg(host.qemuExecutable
                              ? QStringLiteral("%1（来源 %2）").arg(host.qemuPath, host.qemuSource)
                              : (host.qemuFound ? QStringLiteral("已找到但无法执行")
                                                : QStringLiteral("未找到")));
        lines << QStringLiteral("可用客户机架构: %1")
                     .arg(host.runnableGuestArches.isEmpty()
                              ? QStringLiteral("无")
                              : host.runnableGuestArches.join(QStringLiteral(", ")));
        lines << QStringLiteral("/dev/kvm: %1")
                     .arg(!host.kvmDevicePresent
                              ? QStringLiteral("不存在")
                              : (host.kvmDeviceAccessible ? QStringLiteral("可用（同架构客户机）")
                                                          : QStringLiteral("存在但不可读写")));
        lines << QStringLiteral("加速器: %1")
                     .arg(harmonyHostHasAcceleration(host) ? QStringLiteral("kvm") : QStringLiteral("tcg"));
        lines << QStringLiteral("注意: 本后端始终通过 QEMU 运行客户机；即使使用 KVM 也不是宿主原生虚拟化接口。");
        lines << VirtualizationEngine::diagnose(config).split(QLatin1Char('\n'));
        return lines.join(QLatin1Char('\n'));
    }

    QString statusCode(const VmConfig& config) const override {
        const QVector<EngineIssue> issues = validate(config);
        if (hasEngineErrors(issues)) {
            // Prefer the most specific error code so the CLI can act on it.
            for (const EngineIssue& issue : issues) {
                if (issue.isError()) {
                    return issue.code;
                }
            }
            return QStringLiteral("config-invalid");
        }
        const QString arch = harmonyCanonicalGuestArch(config.architecture);
        const HarmonyHostState state = harmonyHostState(hostSnapshot(), arch);
        return harmonyStateCode(state);
    }

    QStringList requirements() const override {
        return {
            QStringLiteral("宿主必须是 HarmonyOS 或 OpenHarmony（Linux 兼容模式）；本后端不适用于其他平台。"),
            QStringLiteral("需要与应用静态链接的 QEMU 进程内核心；禁止使用 PATH 或 runtime/qemu 中的外部可执行文件。"),
            QStringLiteral("硬件加速需要可读写的 /dev/kvm 且客户机与宿主同架构；否则明确使用 TCG 软件模拟。"),
            QStringLiteral("进程内核心未就绪时本后端失败关闭，不会派生子进程或伪报启动成功。"),
        };
    }

    bool runsInProcess() const override { return true; }

    // --- frontend callbacks (hard gate) -----------------------------------
    // Honest readiness of the Display / Input / Audio / Block frontend. Always
    // NotSupported while the in-process core's callback ABI is not connected.
    HarmonyFrontendStatus frontendStatus() const;

    // Registers frontend callbacks. Returns NotSupported and stores nothing
    // while the ABI is unconnected: a silent success here would let a caller
    // believe a framebuffer / disk is live when it is not. Never faked.
    HarmonyFrontendStatus setFrontendCallbacks(const HarmonyFrontendCallbacks& callbacks);

private:
    // Applies the backend's accelerator decision to a caller configuration. The
    // shared translator may still downgrade "kvm" to "tcg" if the host cannot
    // honour it, so a config is never lied to about acceleration.
    VmConfig launchedConfig(const VmConfig& config) const {
        VmConfig launch = config;
        const QString arch = harmonyCanonicalGuestArch(config.architecture);
        launch.accelerator = harmonyAcceleratorFor(hostSnapshot(), arch);
        return launch;
    }

    QSharedPointer<HarmonyEnvironment> environment_;
    // Guards the cached probe. Capability queries are const and thread-safe, so
    // the cache has to be too.
    mutable std::mutex snapshotMutex_;
    mutable HarmonyHostSnapshot cachedSnapshot_;
    mutable bool hasSnapshot_ = false;

    // Tokens this engine has actually issued via start(). stop() only ever
    // forwards a token that lives here, so an unknown / never-started token is
    // rejected without reaching the environment (no spawn, no fake stop). The set
    // is intentionally append-only within the engine's lifetime: a token is never
    // removed, because the underlying core treats stop()/destroy() as idempotent
    // and a caller may legally stop the same session several times. Tokens come
    // from the environment (the gated core's monotonically increasing session id,
    // or the test fake's scripted id) — never an OS PID.
    mutable QMutex activeSessionsMutex_;
    mutable QSet<qint64> activeSessions_;
};

// ---------------------------------------------------------------------------
// Factory surface
// ---------------------------------------------------------------------------
//
// Declared here rather than in the shared header so the shared header stays
// untouched. The registry integrator may either rely on the self-registration
// performed by HarmonyVirtualizationEngine.cpp, or call these explicitly (for
// example inside VirtualizationEngineRegistry::registerBuiltinBackends()).
// Both paths are safe: the registry replaces a backend that shares an id.

bool harmonyVirtualizationEngineAvailable();
VirtualizationEnginePtr makeHarmonyVirtualizationEngine();

} // namespace vw
