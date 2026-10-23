#pragma once

// LinuxKvmEngine — Linux KVM virtualisation backend.
//
// What this backend honestly is
// -----------------------------
// This is a *QEMU compatibility backend* scoped to Linux hosts:
//
//   * If a usable same-ISA QEMU system emulator exists AND the kernel exposes a
//     usable /dev/kvm whose KVM_GET_API_VERSION reports the documented API
//     version, the backend drives QEMU with hardware acceleration
//     (`-machine <m>,accel=kvm`) and reports
//     VirtualizationTier::HardwareAccelerated.
//   * Otherwise it drives QEMU with TCG software emulation and reports
//     VirtualizationTier::SoftwareEmulation — never "native". QEMU always owns
//     the device model, so this backend is never a native hypervisor backend.
//   * On a host without /dev/kvm or without QEMU installed, the backend reports
//     a stable, diagnosable unsupported/degraded result and tells the caller to
//     keep using the QEMU TCG backend. It never claims to have started a guest.
//
// Cross-architecture guests: rejected here, served by qemu-tcg
// -----------------------------------------------------------
// KVM is a same-ISA hypervisor API: cross-architecture hardware acceleration
// does not exist on any platform. A cross-architecture guest is therefore *not*
// claimed by this backend — supportsGuestArch() returns false and validate()
// reports `guest-arch-unsupported`, which is what lets the registry fall through
// to qemu-tcg. Claiming "supported, but only at TCG speed" would make
// auto-selection pick a KVM-scoped backend for a guest it can never accelerate.
// The candidate list below is therefore a list of *possible same-ISA* guest
// architectures: which one applies depends on the host ISA.
//
// Everything below is deliberately split so it can be unit-tested without a
// Linux host and without launching anything:
//
//   * Pure policy: LinuxHostState / capability / validation helpers, all
//     `inline`, driven entirely by an injected LinuxHostSnapshot.
//   * A single environment seam (LinuxEnvironment) that owns the four things
//     which cannot be pure: reading the host, resolving the QEMU program,
//     building its argument vector, and spawning/reaping the process. Tests
//     inject a fake; the real implementation lives in the .cpp and is a thin
//     wrapper over the application's existing QEMU runtime plumbing.
//   * The engine itself only combines the two, so start()/stop() failure paths
//     are exercised in tests without a real process.
//
// Command construction
// --------------------
// There is no shell anywhere in this backend. A guest is described by a program
// path plus a QStringList argument vector that is handed straight to
// QProcess::start(program, arguments). No argument is ever built by
// concatenating untrusted text into a command line, so shell metacharacters in
// a guest path or kernel command line cannot become a second command.
//
// Integration
// -----------
// The shared registry (src/VirtualizationEngineRegistry.hpp) supports
// self-registration from a backend's own translation unit, which is what this
// backend does at the bottom of the .cpp. linuxKvmEngineAvailable() and
// makeLinuxKvmEngine() are also declared here so the registry integrator can
// wire it explicitly instead. See docs/linux-virtualization.md.

#include "VirtualizationCapabilities.hpp"
#include "SystemSoftwareEngine.hpp"  // isSystemSoftwareConfig, matches the other backends
#include "VirtualizationEngine.hpp"
#include "VmConfig.hpp"

#include <QSharedPointer>
#include <QString>
#include <QStringList>
#include <QVector>

#include <utility>

namespace vw {

// ---------------------------------------------------------------------------
// Guest architectures this backend may offer
// ---------------------------------------------------------------------------
//
// This is the *candidate* set, not a claim: whether any of them is actually
// runnable is decided per host by QEMU runtime probing (see
// LinuxHostSnapshot::runnableGuestArches).

inline QStringList linuxCandidateGuestArches() {
    return {QStringLiteral("aarch64"), QStringLiteral("x86_64"), QStringLiteral("riscv64")};
}

// Canonical guest architecture, or an empty string when it is not one this
// backend can ever offer (ppc, m68k, i386, ... stay with the QEMU backends).
inline QString linuxCanonicalGuestArch(const QString& value) {
    const QString canonical = canonicalGuestArch(value);
    if (canonical.isEmpty()) {
        return QString();
    }
    return linuxCandidateGuestArches().contains(canonical) ? canonical : QString();
}

// ---------------------------------------------------------------------------
// Host snapshot
// ---------------------------------------------------------------------------
//
// A complete, side-effect-free description of everything the policy layer needs.
// The real probe fills it once per engine instance; tests fill it by hand.

struct LinuxHostSnapshot {
    // --- host identity -------------------------------------------------------
    bool hostIsLinux = false;       // Linux host (not macOS, Windows, HarmonyOS)
    QString hostFamily;             // e.g. "linux"
    QString hostName;               // human readable, e.g. "Linux"
    QString hostPlatformName;       // "linux"
    QString hostArch;               // canonical host architecture

    // --- QEMU runtime --------------------------------------------------------
    bool qemuProbePerformed = false;   // false on non-Linux hosts: we never probe there
    bool qemuFound = false;            // at least one candidate binary exists on disk
    bool qemuExecutable = false;       // ... and at least one of them actually ran (--version)
    bool qemuProbeTimedOut = false;    // --version exceeded the probe budget
    QString qemuPath;                  // the runnable binary, when qemuExecutable
    QString qemuSource;                // "runtime/qemu" | "PATH" | "missing"
    QStringList runnableGuestArches;   // arches with a probe-verified emulator

    // --- hardware acceleration ----------------------------------------------
    bool kvmDevicePresent = false;     // /dev/kvm exists
    bool kvmDeviceAccessible = false;  // /dev/kvm is readable and writable

    // --- KVM device + API (populated only on Linux hosts) --------------------
    // These fields are filled by the environment layer from a *single*
    // open("/dev/kvm", O_RDWR | O_CLOEXEC) followed by ioctl. `kvmApiProbed`
    // records that the open was attempted on a Linux host — it is true even
    // when the device is missing, which is what makes kvmOpenErrno meaningful
    // ("present but not accessible" vs. "not present at all").
    // On non-Linux hosts every field keeps its default, so the state machine
    // can never claim hardware acceleration there.
    bool kvmApiProbed = false;         // open(/dev/kvm) was attempted (Linux hosts only)
    int kvmApiVersion = 0;             // KVM_GET_API_VERSION result (expected: 12); 0 = not probed
    int kvmMaxVcpus = 0;               // KVM_CAP_MAX_VCPUS; 0 = unknown / not probed
    int kvmMaxMemorySlots = 0;         // KVM_CAP_NR_MEMSLOTS; 0 = unknown / not probed
    int kvmOpenErrno = 0;              // errno from open(/dev/kvm); 0 = no error or not attempted

    // --- narration -----------------------------------------------------------
    QString detail;        // one-line human readable summary
    QStringList notes;     // extra diagnostics, safe to surface in the GUI
};

// ---------------------------------------------------------------------------
// Same-ISA / cross-ISA guest classification
// ---------------------------------------------------------------------------
//
// KVM only ever accelerates a guest whose instruction set *is* the host's
// instruction set. These two predicates are the single place that decides
// whether a guest is in scope for this backend at all, so that probe(),
// probeFor(), validate(), supportsGuestArch(), start() and statusCode() can
// never drift apart. A guest architecture we do not even recognise counts as
// neither same-ISA nor cross-ISA: it is simply out of scope and gets rejected
// by the canonicalisation step elsewhere.

inline bool linuxGuestIsSameIsa(const LinuxHostSnapshot& host, const QString& guestArch) {
    const QString canonical = linuxCanonicalGuestArch(guestArch);
    if (canonical.isEmpty()) {
        return false;
    }
    const QString hostArch = canonicalHostArch(host.hostArch);
    return !hostArch.isEmpty() && canonical == hostArch;
}

inline bool linuxGuestIsCrossIsa(const LinuxHostSnapshot& host, const QString& guestArch) {
    const QString canonical = linuxCanonicalGuestArch(guestArch);
    if (canonical.isEmpty()) {
        return false;
    }
    const QString hostArch = canonicalHostArch(host.hostArch);
    return !hostArch.isEmpty() && canonical != hostArch;
}

// ---------------------------------------------------------------------------
// KVM maturity per architecture
// ---------------------------------------------------------------------------
//
// KVM on x86_64 and aarch64 is production-quality. On riscv64 it is still
// maturing: the upstream kernel has KVM support but not all distros ship it
// and some features (e.g. SBI firmware passthrough) are under active
// development. The backend allows riscv64 KVM acceleration but attaches a
// maturity warning so the UI can surface it.

inline bool linuxKvmIsMatureForArch(const QString& canonicalArch) {
    return canonicalArch == QStringLiteral("x86_64")
        || canonicalArch == QStringLiteral("aarch64");
}

// ---------------------------------------------------------------------------
// KVM API version
// ---------------------------------------------------------------------------
//
// KVM_GET_API_VERSION is ABI-frozen at 12; every kernel from 2.6.9 onwards
// reports it. Any other positive value means this process and that kernel do
// not speak the same KVM ABI, so claiming hardware acceleration would be a
// guess dressed up as a fact. 0 and negatives both mean "not established".

inline constexpr int kLinuxKvmApiVersion = 12;

inline bool linuxKvmApiVersionSupported(int apiVersion) {
    return apiVersion == kLinuxKvmApiVersion;
}

// ---------------------------------------------------------------------------
// Host state machine
// ---------------------------------------------------------------------------
//
// Every decision the backend makes about "can we run this guest" collapses to
// one of these five states. The names are chosen so that no state can be
// mistaken for native virtualisation.

enum class LinuxHostState {
    NotLinuxHost,        // not a Linux host at all
    RuntimeMissing,      // no QEMU system emulator for this guest architecture
    RuntimeNotRunnable,  // a QEMU binary exists but cannot be executed
    TcgOnly,             // usable, software emulation (TCG)
    Accelerated,         // usable, same-ISA hardware acceleration (KVM)
};

inline bool linuxStateIsUsable(LinuxHostState state) {
    return state == LinuxHostState::TcgOnly || state == LinuxHostState::Accelerated;
}

inline bool linuxStateIsAccelerated(LinuxHostState state) {
    return state == LinuxHostState::Accelerated;
}

// Stable machine-readable code. `degraded` is deliberately distinct from
// `available`: it means "this will work, but by software emulation only".
inline QString linuxStateCode(LinuxHostState state) {
    switch (state) {
    case LinuxHostState::NotLinuxHost:
        return QStringLiteral("host-unsupported");
    case LinuxHostState::RuntimeMissing:
        return QStringLiteral("runtime-missing");
    case LinuxHostState::RuntimeNotRunnable:
        return QStringLiteral("runtime-not-runnable");
    case LinuxHostState::TcgOnly:
        return QStringLiteral("degraded");
    case LinuxHostState::Accelerated:
    default:
        return QStringLiteral("available");
    }
}

// Human-readable explanation. Always non-empty so callers can surface it
// verbatim as the `QString* reason` of the shared contract.
inline QString linuxStateMessage(LinuxHostState state, const QString& guestArch) {
    const QString arch = linuxCanonicalGuestArch(guestArch).isEmpty()
        ? (guestArch.trimmed().isEmpty() ? QStringLiteral("（未指定）") : guestArch)
        : linuxCanonicalGuestArch(guestArch);
    switch (state) {
    case LinuxHostState::NotLinuxHost:
        return QStringLiteral("宿主不是 Linux，本后端不适用；请使用 QEMU 后端"
                              "（qemu-hardware / qemu-tcg）。");
    case LinuxHostState::RuntimeMissing:
        return QStringLiteral("宿主是 Linux，但没有找到可运行的同架构（%1）QEMU 系统模拟器；"
                              "请改用 qemu-tcg，或把 qemu-system-%1 放入 "
                              "runtime/qemu/linux/<host-arch>/bin 或系统 PATH。")
            .arg(arch);
    case LinuxHostState::RuntimeNotRunnable:
        return QStringLiteral("找到了 QEMU 可执行文件，但无法运行它；"
                              "本后端不会声称已启动客户机，请改用 qemu-tcg 或检查执行权限。");
    case LinuxHostState::TcgOnly:
        return QStringLiteral("可运行 QEMU %1 客户机，但没有可用的同架构硬件加速（/dev/kvm 未暴露、"
                              "不可读写，或 KVM API 版本不符），将使用 TCG 软件模拟"
                              "（tier=emulation，不是原生虚拟化）。")
            .arg(arch);
    case LinuxHostState::Accelerated:
    default:
        return QStringLiteral("可运行 QEMU %1 客户机，且 /dev/kvm 对同架构客户机可用，将请求 KVM 硬件"
                              "加速（仍由 QEMU 提供设备模型，不是宿主原生虚拟化接口）。")
            .arg(arch);
    }
}

// The whole host/guest decision, as a pure function of the snapshot.
//
// This answers the *host* question — "how would a same-ISA guest be run here?"
// It deliberately does not decide whether this backend accepts the requested
// guest architecture at all; that is linuxGuestIsSameIsa(), because a foreign
// ISA belongs to qemu-tcg rather than to a KVM-scoped backend.
//
// Accelerated requires a successful KVM_GET_API_VERSION ioctl reporting the
// documented API version, not just /dev/kvm present+accessible. This prevents
// claiming hardware acceleration when the KVM kernel module is loaded but
// broken or speaking a different ABI.
inline LinuxHostState linuxHostState(const LinuxHostSnapshot& snapshot,
                                     const QString& guestArch) {
    if (!snapshot.hostIsLinux) {
        return LinuxHostState::NotLinuxHost;
    }
    const QString arch = linuxCanonicalGuestArch(guestArch);
    if (arch.isEmpty()) {
        // Not an architecture this backend can ever offer.
        return LinuxHostState::RuntimeMissing;
    }
    if (snapshot.qemuFound && !snapshot.qemuExecutable) {
        return LinuxHostState::RuntimeNotRunnable;
    }
    if (!snapshot.runnableGuestArches.contains(arch)) {
        return LinuxHostState::RuntimeMissing;
    }
    // Hardware acceleration requires same-ISA, /dev/kvm accessible, AND a
    // confirmed, ABI-compatible KVM API version. Cross-architecture guests are
    // never accelerated.
    const bool sameIsa = linuxGuestIsSameIsa(snapshot, arch);
    if (sameIsa && snapshot.kvmDevicePresent && snapshot.kvmDeviceAccessible
        && snapshot.kvmApiProbed && linuxKvmApiVersionSupported(snapshot.kvmApiVersion)) {
        return LinuxHostState::Accelerated;
    }
    return LinuxHostState::TcgOnly;
}

// The accelerator token the backend requests. It is fed into
// VmConfig::accelerator, which the shared translator renders as
// `-machine <machine>,accel=<token>` — and which the shared
// selectAccelerator() may still downgrade to "tcg" if the host cannot honour
// it. That double check is intentional: this function states the intent, the
// translator enforces reality.
inline QString linuxAcceleratorFor(const LinuxHostSnapshot& snapshot,
                                   const QString& guestArch) {
    return linuxStateIsAccelerated(linuxHostState(snapshot, guestArch))
        ? QStringLiteral("kvm")
        : QStringLiteral("tcg");
}

// True when the host exposes at least one architecture that can be accelerated.
// Mirrors the Accelerated condition in linuxHostState().
inline bool linuxHostHasAcceleration(const LinuxHostSnapshot& snapshot) {
    if (!snapshot.hostIsLinux || !snapshot.kvmDevicePresent || !snapshot.kvmDeviceAccessible) {
        return false;
    }
    if (!snapshot.kvmApiProbed || !linuxKvmApiVersionSupported(snapshot.kvmApiVersion)) {
        return false;
    }
    const QString hostArch = canonicalHostArch(snapshot.hostArch);
    return linuxCandidateGuestArches().contains(hostArch)
        && snapshot.runnableGuestArches.contains(hostArch);
}

// ---------------------------------------------------------------------------
// Argument-vector safety
// ---------------------------------------------------------------------------

// True when `value` carries a character that must never reach a process argument
// (an embedded NUL or a line break). Used for guest paths and command lines.
inline bool linuxContainsUnsafeCharacter(const QString& value) {
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
inline bool linuxArgumentsAreWellFormed(const QStringList& arguments, QString* problem = nullptr) {
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
        if (linuxContainsUnsafeCharacter(argument)) {
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
// KVM errno diagnostics
// ---------------------------------------------------------------------------
//
// When the KVM API probe fails, the errno captured in LinuxHostSnapshot
// describes *why*. These helpers produce human-readable diagnostics that are
// safe to surface in the GUI and in the probe result.

inline QString linuxKvmErrnoMessage(int errnoValue) {
    if (errnoValue == 0) {
        return QString();
    }
    // Common errno values from KVM operations. The descriptions are
    // deliberately specific to the KVM context rather than generic strerror()
    // text, so the user gets actionable guidance.
    switch (errnoValue) {
    case 2:   // ENOENT
        return QStringLiteral("/dev/kvm 不存在（errno=ENOENT）：内核未加载 KVM 模块。");
    case 13:  // EACCES
        return QStringLiteral("/dev/kvm 权限不足（errno=EACCES）：需要加入 kvm 组或使用 sudo。");
    case 16:  // EBUSY
        return QStringLiteral("/dev/kvm 设备忙（errno=EBUSY）：可能已被其他进程独占。");
    case 22:  // EINVAL
        return QStringLiteral("KVM ioctl 参数无效（errno=EINVAL）：KVM 内核模块版本可能不兼容。");
    case 25:  // ENOTTY
        return QStringLiteral("KVM ioctl 不适用（errno=ENOTTY）：KVM 内核模块可能未正确加载。");
    case 28:  // ENOSPC
        return QStringLiteral("KVM 资源不足（errno=ENOSPC）：可能达到内存槽或 vCPU 上限。");
    default:
        return QStringLiteral("KVM 操作失败（errno=%1）。").arg(errnoValue);
    }
}

// True when the snapshot carries a KVM errno that should be surfaced as a
// diagnostic (i.e. /dev/kvm was expected to work but did not).
inline bool linuxKvmHasErrno(const LinuxHostSnapshot& snapshot) {
    return snapshot.kvmDevicePresent && snapshot.kvmOpenErrno != 0;
}

// ---------------------------------------------------------------------------
// vCPU limit validation
// ---------------------------------------------------------------------------
//
// When KVM reports a max vCPU count, the guest configuration should not exceed
// it. This is a soft limit: QEMU will handle the error at launch time, but
// surfacing it early lets the UI warn the user before they try to start.

inline bool linuxVcpuExceedsKvmMax(const VmConfig& config,
                                   const LinuxHostSnapshot& snapshot) {
    if (snapshot.kvmMaxVcpus <= 0) {
        return false;  // KVM max vCPUs unknown, cannot check
    }
    const QString arch = linuxCanonicalGuestArch(config.architecture);
    const LinuxHostState state = linuxHostState(snapshot, arch);
    if (state != LinuxHostState::Accelerated) {
        return false;  // Only relevant when KVM acceleration is claimed
    }
    return config.cpuCores > snapshot.kvmMaxVcpus;
}

// ---------------------------------------------------------------------------
// Configuration validation (pure)
// ---------------------------------------------------------------------------

inline QVector<EngineIssue> linuxValidateConfig(const VmConfig& config,
                                                 const LinuxHostSnapshot& snapshot,
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

    // Host applicability outranks every guest-architecture question. On a
    // non-Linux host this backend is simply not applicable, and reporting a
    // guest-architecture problem instead would both misdirect the user and hide
    // the real reason. statusCode() already prefers host-unsupported, so saying
    // anything else here would make the two disagree.
    if (!snapshot.hostIsLinux) {
        issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("host-unsupported"),
                              linuxStateMessage(LinuxHostState::NotLinuxHost, guestArch));
        return issues;
    }

    // Cross-ISA guests are rejected, not silently degraded. KVM is a same-ISA
    // hypervisor API; running a foreign ISA is software emulation and therefore
    // qemu-tcg's job. Rejecting here is what lets the registry fall back.
    if (linuxGuestIsCrossIsa(snapshot, guestArch)) {
        issues << engineIssue(
            EngineIssueLevel::Error, QStringLiteral("guest-arch-unsupported"),
            QStringLiteral("客户机 %1 与宿主 %2 架构不同：KVM 只支持同架构虚拟化，"
                           "不存在跨架构硬件加速。请使用 qemu-tcg 后端运行该客户机。")
                .arg(guestArch, canonicalHostArch(snapshot.hostArch)));
        return issues;
    }

    if (config.cpuCores < 1) {
        issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("cpu-count-invalid"),
                              QStringLiteral("CPU 核数必须至少为 1（当前 %1）。").arg(config.cpuCores));
    }
    // vCPU limit: warn when the requested count exceeds KVM's reported maximum.
    // This is a warning rather than an error because QEMU may still succeed
    // (e.g. by silently capping), and the user should be informed rather than
    // blocked.
    if (linuxVcpuExceedsKvmMax(config, snapshot)) {
        issues << engineIssue(EngineIssueLevel::Warning, QStringLiteral("kvm-vcpu-exceeds-max"),
                              QStringLiteral("请求的 CPU 核数（%1）超过 KVM 报告的最大 vCPU 数（%2）；"
                                               "客户机可能无法启动或性能受限。")
                                  .arg(config.cpuCores)
                                  .arg(snapshot.kvmMaxVcpus));
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
        if (linuxContainsUnsafeCharacter(value)) {
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
        if (linuxContainsUnsafeCharacter(argument)) {
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

    const LinuxHostState state = linuxHostState(snapshot, guestArch);
    const QString message = linuxStateMessage(state, guestArch);
    switch (state) {
    case LinuxHostState::NotLinuxHost:
        issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("host-unsupported"), message);
        break;
    case LinuxHostState::RuntimeMissing:
        issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("runtime-missing"), message);
        break;
    case LinuxHostState::RuntimeNotRunnable:
        issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("runtime-not-runnable"), message);
        break;
    case LinuxHostState::TcgOnly:
        // Degraded, not broken: usable, so this must stay a warning or the
        // registry would skip the only backend that can actually run here.
        issues << engineIssue(EngineIssueLevel::Warning, QStringLiteral("software-emulation"), message);
        break;
    case LinuxHostState::Accelerated:
    default:
        issues << engineIssue(EngineIssueLevel::Info, QStringLiteral("hardware-acceleration"), message);
        break;
    }

    // KVM errno diagnostic: if /dev/kvm exists but the KVM API probe failed,
    // surface the errno as a warning so the user knows *why* KVM is degraded.
    if (linuxKvmHasErrno(snapshot)) {
        issues << engineIssue(EngineIssueLevel::Warning, QStringLiteral("kvm-api-error"),
                              linuxKvmErrnoMessage(snapshot.kvmOpenErrno));
    }

    // riscv64 KVM maturity warning: KVM on RISC-V is functional but still
    // maturing. Surface this so the user is aware of potential limitations.
    if (state == LinuxHostState::Accelerated
        && !linuxKvmIsMatureForArch(guestArch)) {
        issues << engineIssue(EngineIssueLevel::Warning, QStringLiteral("kvm-maturity-experimental"),
                              QStringLiteral("架构 %1 的 KVM 支持尚在成熟中，可能存在功能限制或不稳定性。")
                                  .arg(guestArch));
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
// a Linux host and without spawning a process.

class LinuxEnvironment {
public:
    virtual ~LinuxEnvironment() = default;

    // Reads the host once. On a non-Linux host this must not probe QEMU at
    // all: it returns hostIsLinux == false immediately.
    virtual LinuxHostSnapshot snapshot() const = 0;

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
// LinuxKvmEngine.cpp.
QSharedPointer<LinuxEnvironment> makeLinuxEnvironment();

// ---------------------------------------------------------------------------
// The engine
// ---------------------------------------------------------------------------

class LinuxKvmEngine : public VirtualizationEngine {
public:
    LinuxKvmEngine()
        : environment_(makeLinuxEnvironment()) {}

    // Injection seam. Passing null is honoured literally (it does NOT silently
    // install the real environment), so tests can exercise the "engine has no
    // environment" guards instead of accidentally spawning a real process.
    explicit LinuxKvmEngine(QSharedPointer<LinuxEnvironment> environment)
        : environment_(std::move(environment)) {}

    // --- identity ------------------------------------------------------------
    QString backendId() const override { return backend_id::linuxKvm(); }

    QString displayName() const override {
        return QStringLiteral("Linux KVM/QEMU 兼容后端（KVM/TCG）");
    }

    // Honest tier: hardware-accelerated only when this host really exposes a
    // usable same-ISA accelerator; otherwise this backend IS software emulation.
    VirtualizationTier tier() const override {
        return linuxHostHasAcceleration(hostSnapshot()) ? VirtualizationTier::HardwareAccelerated
                                                        : VirtualizationTier::SoftwareEmulation;
    }

    // --- inspection seams ----------------------------------------------------
    const LinuxHostSnapshot& hostSnapshot() const {
        if (!hasSnapshot_) {
            cachedSnapshot_ = environment_ ? environment_->snapshot() : LinuxHostSnapshot();
            hasSnapshot_ = true;
        }
        return cachedSnapshot_;
    }

    // Drops the cached probe so the next query re-reads the host.
    void invalidateProbe() const {
        hasSnapshot_ = false;
        cachedSnapshot_ = LinuxHostSnapshot();
    }

    LinuxHostState stateFor(const QString& guestArch) const {
        return linuxHostState(hostSnapshot(), guestArch);
    }

    QString programFor(const VmConfig& config) const {
        return environment_ ? environment_->program(launchedConfig(config)) : QString();
    }

    // The exact argument vector that start() will hand to the process, with the
    // backend's accelerator decision applied. Never a shell string.
    QStringList buildArguments(const VmConfig& config) const {
        return environment_ ? environment_->arguments(launchedConfig(config)) : QStringList();
    }

    // --- capability ----------------------------------------------------------
    bool isHostSupported(QString* reason = nullptr) const override {
        const LinuxHostSnapshot& host = hostSnapshot();
        if (!host.hostIsLinux) {
            if (reason) {
                *reason = linuxStateMessage(LinuxHostState::NotLinuxHost, QString());
            }
            return false;
        }
        if (!host.runnableGuestArches.isEmpty()) {
            return true;
        }
        if (reason) {
            const LinuxHostState state = host.qemuFound ? LinuxHostState::RuntimeNotRunnable
                                                        : LinuxHostState::RuntimeMissing;
            *reason = linuxStateMessage(state, host.hostArch);
            if (!host.notes.isEmpty()) {
                *reason += QStringLiteral(" ") + host.notes.join(QStringLiteral(" "));
            }
        }
        return false;
    }

    bool supportsGuestArch(const QString& guestArch, QString* reason = nullptr) const override {
        const QString arch = linuxCanonicalGuestArch(guestArch);
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
        const LinuxHostSnapshot& host = hostSnapshot();
        if (!host.hostIsLinux) {
            if (reason) {
                *reason = linuxStateMessage(LinuxHostState::NotLinuxHost, arch);
            }
            return false;
        }
        // KVM is a same-ISA hypervisor API. A foreign ISA is qemu-tcg's job, so
        // this backend refuses it instead of claiming "runnable, but only at
        // TCG speed" — which would let auto-selection pick a KVM-scoped backend
        // for a guest it can never accelerate.
        if (!linuxGuestIsSameIsa(host, arch)) {
            if (reason) {
                *reason = QStringLiteral("客户机 %1 与宿主 %2 架构不同：KVM 只支持同架构虚拟化，"
                                         "跨架构客户机请使用 qemu-tcg 后端。")
                              .arg(arch, canonicalHostArch(host.hostArch));
            }
            return false;
        }
        const LinuxHostState state = stateFor(arch);
        if (linuxStateIsUsable(state)) {
            return true;
        }
        if (reason) {
            *reason = linuxStateMessage(state, arch);
        }
        return false;
    }

    EngineProbeResult probe() const override {
        const LinuxHostSnapshot& host = hostSnapshot();

        EngineProbeResult result;
        result.backendId = backendId();
        result.displayName = displayName();
        result.tier = tier();
        result.hostSupported = isHostSupported();

        EngineCapabilities& capabilities = result.capabilities;
        capabilities.hostSupported = result.hostSupported;
        capabilities.hostArch = canonicalHostArch(host.hostArch);
        capabilities.hardwareAcceleration = linuxHostHasAcceleration(host);
        // Never true: this backend always runs the guest inside QEMU, even when
        // KVM is accelerating the CPU. Claiming otherwise would be dishonest.
        capabilities.nativeExecution = false;

        QStringList usableArches;
        for (const QString& arch : linuxCandidateGuestArches()) {
            // Only same-ISA guests count: a foreign ISA is qemu-tcg's, never
            // this backend's.
            if (linuxGuestIsSameIsa(host, arch)
                && linuxStateIsUsable(linuxHostState(host, arch))) {
                usableArches << arch;
            }
        }
        capabilities.guestArchSupported = !usableArches.isEmpty();
        capabilities.canStart = result.hostSupported && capabilities.guestArchSupported;
        capabilities.canStop = capabilities.canStart;
        capabilities.accelerator = capabilities.hardwareAcceleration ? QStringLiteral("kvm")
                                                                     : QStringLiteral("tcg");

        QStringList summary;
        if (!host.hostIsLinux) {
            summary << QStringLiteral("宿主不是 Linux（%1）：本后端不适用，回退 QEMU 后端。")
                           .arg(host.hostName.isEmpty() ? QStringLiteral("unknown") : host.hostName);
        } else if (usableArches.isEmpty()) {
            // Distinguish the three genuinely different reasons, so the CLI
            // never tells the user to install a QEMU that is already there.
            if (host.qemuFound) {
                summary << QStringLiteral("检测到 QEMU 但无法执行；回退 qemu-tcg 或检查执行权限。");
            } else if (!host.runnableGuestArches.isEmpty()) {
                summary << QStringLiteral("只找到非宿主架构（%1）的 QEMU 模拟器：KVM 只加速同架构客户机，"
                                          "跨架构客户机请使用 qemu-tcg。")
                               .arg(host.runnableGuestArches.join(QStringLiteral(", ")));
            } else {
                summary << QStringLiteral("未找到可运行的 QEMU 系统模拟器，回退 qemu-tcg，"
                                          "或安装 runtime/qemu/linux/<host-arch>/bin。");
            }
        } else {
            summary << QStringLiteral("Linux KVM/QEMU 兼容后端可用：客户机 %1，加速器 %2。")
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
        // KVM API diagnostics: surface the KVM version and errno when relevant.
        if (host.kvmApiProbed && host.kvmApiVersion > 0) {
            result.diagnostics << engineIssue(EngineIssueLevel::Info,
                                              QStringLiteral("kvm-api-version"),
              QStringLiteral("KVM API 版本 %1，最大 vCPU 数 %2，最大内存槽 %3。")
                  .arg(host.kvmApiVersion)
                  .arg(host.kvmMaxVcpus > 0 ? QString::number(host.kvmMaxVcpus)
                                             : QStringLiteral("未知"))
                  .arg(host.kvmMaxMemorySlots > 0 ? QString::number(host.kvmMaxMemorySlots)
                                                   : QStringLiteral("未知")));
            if (!linuxKvmApiVersionSupported(host.kvmApiVersion)) {
                result.diagnostics << engineIssue(
                    EngineIssueLevel::Warning, QStringLiteral("kvm-api-version-unsupported"),
                    QStringLiteral("/dev/kvm 报告的 KVM API 版本为 %1，期望 %2；"
                                   "本次不会声称硬件加速，将使用 TCG。")
                        .arg(host.kvmApiVersion)
                        .arg(kLinuxKvmApiVersion));
            }
        }
        if (linuxKvmHasErrno(host)) {
            result.diagnostics << engineIssue(EngineIssueLevel::Warning,
                                              QStringLiteral("kvm-api-error"),
                                              linuxKvmErrnoMessage(host.kvmOpenErrno));
        }
        // riscv64 KVM maturity warning in probe diagnostics.
        if (capabilities.hardwareAcceleration
            && !linuxKvmIsMatureForArch(host.hostArch)) {
            result.diagnostics << engineIssue(EngineIssueLevel::Warning,
                                              QStringLiteral("kvm-maturity-experimental"),
              QStringLiteral("架构 %1 的 KVM 支持尚在成熟中，可能存在功能限制或不稳定性。")
                  .arg(host.hostArch));
        }
        if (host.hostIsLinux) {
            const LinuxHostState state = usableArches.isEmpty()
                ? (host.qemuFound ? LinuxHostState::RuntimeNotRunnable
                                  : LinuxHostState::RuntimeMissing)
                : (capabilities.hardwareAcceleration ? LinuxHostState::Accelerated
                                                     : LinuxHostState::TcgOnly);
            const EngineIssueLevel level = linuxStateIsUsable(state)
                ? (linuxStateIsAccelerated(state) ? EngineIssueLevel::Info
                                                  : EngineIssueLevel::Warning)
                : EngineIssueLevel::Error;
            result.diagnostics << engineIssue(level, linuxStateCode(state),
                                              linuxStateMessage(state, host.hostArch));
        }
        return result;
    }

    // Per-architecture fast path: the default implementation would keep a
    // per-host canStart, which is wrong for a backend that owns a different
    // QEMU binary per guest architecture.
    EngineProbeResult probeFor(const QString& guestArch) const override {
        EngineProbeResult result = probe();

        // No guest architecture asked for: the host-level probe() answer stands.
        if (guestArch.trimmed().isEmpty()) {
            return result;
        }

        const QString arch = linuxCanonicalGuestArch(guestArch);
        QString reason;
        const bool supported = supportsGuestArch(guestArch, &reason);

        result.capabilities.guestArchSupported = supported;
        // Always recompute from the per-guest decision — never inherit probe()'s
        // host-level answer, which would leave hardwareAcceleration=true on a
        // guest this backend refuses.
        const bool accelerated = supported && linuxStateIsAccelerated(stateFor(arch));
        result.capabilities.hardwareAcceleration = accelerated;
        result.capabilities.accelerator = supported
            ? (accelerated ? QStringLiteral("kvm") : QStringLiteral("tcg"))
            : QString();
        result.capabilities.canStart = supported;
        result.capabilities.canStop = supported;

        if (!supported) {
            result.diagnostics << engineIssue(EngineIssueLevel::Warning,
                                              QStringLiteral("guest-arch-unsupported"),
                                              reason.isEmpty()
                                                  ? QStringLiteral("本后端不支持该客户机架构。")
                                                  : reason);
            result.summary = reason.isEmpty() ? QStringLiteral("本后端不支持该客户机架构。") : reason;
        }
        return result;
    }

    // --- validation ----------------------------------------------------------
    QVector<EngineIssue> validate(const VmConfig& config) const override {
        return linuxValidateConfig(config, hostSnapshot(), linuxCanonicalGuestArch(config.architecture));
    }

    // --- lifecycle -----------------------------------------------------------
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
                *error = linuxStateMessage(LinuxHostState::RuntimeMissing,
                                           linuxCanonicalGuestArch(config.architecture));
            }
            return false;
        }
        QString problem;
        const QStringList arguments = environment_->arguments(launch);
        if (!linuxArgumentsAreWellFormed(arguments, &problem)) {
            if (error) {
                *error = QStringLiteral("QEMU 参数数组未通过安全校验：") + problem;
            }
            return false;
        }

        qint64 pid = 0;
        if (!environment_->startProcess(launch, &pid, error)) {
            return false;
        }
        if (pid <= 0) {
            // The spawner did not give us a process id, so we cannot honestly
            // claim a guest is running.
            if (error) {
                *error = QStringLiteral("QEMU 进程未返回有效的进程 ID，未声称客户机已启动。");
            }
            return false;
        }
        if (processId) {
            *processId = pid;
        }
        return true;
    }

    bool stop(qint64 processId, QString* error = nullptr) const override {
        if (!environment_) {
            if (error) {
                *error = QStringLiteral("内部错误：虚拟化环境未初始化。");
            }
            return false;
        }
        if (processId <= 0) {
            if (error) {
                *error = QStringLiteral("无效的进程 ID，没有可停止的客户机。");
            }
            return false;
        }
        return environment_->stopProcess(processId, error);
    }

    // --- diagnostics ---------------------------------------------------------
    QString diagnose(const VmConfig& config) const override {
        const LinuxHostSnapshot& host = hostSnapshot();
        QStringList lines;
        lines << QStringLiteral("%1 (%2) — %3").arg(displayName(), backendId(), tierDescription(tier()));
        lines << QStringLiteral("宿主: %1 [%2] arch=%3")
                     .arg(host.hostName.isEmpty() ? QStringLiteral("unknown") : host.hostName,
                          host.hostFamily.isEmpty() ? QStringLiteral("unknown") : host.hostFamily,
                          canonicalHostArch(host.hostArch));
        if (!host.hostIsLinux) {
            lines << QStringLiteral("结论: 宿主不是 Linux，本后端不适用，请使用 QEMU 后端。");
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
        // The accelerator that would really be requested for *this* config, not
        // a host-wide summary: a cross-ISA guest is refused outright.
        const QString guestArch = linuxCanonicalGuestArch(config.architecture);
        if (guestArch.isEmpty()) {
            lines << QStringLiteral("客户机架构: 未指定或不支持");
        } else if (linuxGuestIsCrossIsa(host, guestArch)) {
            lines << QStringLiteral("客户机架构: %1（与宿主跨架构，本后端不承担，请使用 qemu-tcg）")
                         .arg(guestArch);
        } else {
            lines << QStringLiteral("加速器: %1").arg(linuxAcceleratorFor(host, guestArch));
        }
        if (host.kvmApiProbed) {
            if (host.kvmApiVersion > 0) {
                lines << QStringLiteral("KVM API: 版本 %1%2")
                             .arg(host.kvmApiVersion)
                             .arg(linuxKvmApiVersionSupported(host.kvmApiVersion)
                                      ? QString()
                                      : QStringLiteral("（与期望的 %1 不符：不会声称硬件加速）")
                                            .arg(kLinuxKvmApiVersion));
                if (host.kvmMaxVcpus > 0) {
                    lines << QStringLiteral("KVM 最大 vCPU: %1").arg(host.kvmMaxVcpus);
                }
                if (host.kvmMaxMemorySlots > 0) {
                    lines << QStringLiteral("KVM 最大内存槽: %1").arg(host.kvmMaxMemorySlots);
                }
            } else {
                lines << QStringLiteral("KVM API: 探测失败（/dev/kvm 可访问但 ioctl 失败）");
            }
        } else if (host.kvmDevicePresent && host.kvmDeviceAccessible) {
            lines << QStringLiteral("KVM API: 未探测（/dev/kvm 可访问但 KVM API 版本未确认）");
        }
        if (host.kvmOpenErrno != 0) {
            lines << QStringLiteral("KVM errno: %1").arg(linuxKvmErrnoMessage(host.kvmOpenErrno));
        }
        if (linuxHostHasAcceleration(host) && !linuxKvmIsMatureForArch(host.hostArch)) {
            lines << QStringLiteral("注意: %1 KVM 支持尚在成熟中，可能存在功能限制。").arg(host.hostArch);
        }
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
        const QString arch = linuxCanonicalGuestArch(config.architecture);
        const LinuxHostSnapshot& host = hostSnapshot();
        if (!arch.isEmpty() && host.hostIsLinux && !linuxGuestIsSameIsa(host, arch)) {
            // A cross-ISA guest is refused, not degraded: say so explicitly
            // rather than leaking the host-wide "accelerated" state.
            return QStringLiteral("guest-arch-unsupported");
        }
        const LinuxHostState state = linuxHostState(host, arch);
        return linuxStateCode(state);
    }

    QStringList requirements() const override {
        return {
            QStringLiteral("宿主必须是 Linux；本后端不适用于 macOS 或 Windows。"),
            QStringLiteral("需要 QEMU 系统模拟器：runtime/qemu/linux/<host-arch>/bin 或系统 PATH 中的 "
                           "qemu-system-aarch64 / qemu-system-x86_64 / qemu-system-riscv64。"),
            QStringLiteral("硬件加速需要可读写的 /dev/kvm、可用的 KVM API（版本 12），"
                           "且客户机与宿主同架构；否则明确使用 TCG 软件模拟。"),
            QStringLiteral("通常需要加入 kvm 组或具备相应权限以访问 /dev/kvm。"),
            QStringLiteral("只支持与宿主同架构的客户机；跨架构客户机请使用 qemu-tcg 后端。"),
        };
    }

private:
    // Applies the backend's accelerator decision to a caller configuration. The
    // shared translator may still downgrade "kvm" to "tcg" if the host cannot
    // honour it, so a config is never lied to about acceleration.
    VmConfig launchedConfig(const VmConfig& config) const {
        VmConfig launch = config;
        const QString arch = linuxCanonicalGuestArch(config.architecture);
        launch.accelerator = linuxAcceleratorFor(hostSnapshot(), arch);
        return launch;
    }

    QSharedPointer<LinuxEnvironment> environment_;
    mutable LinuxHostSnapshot cachedSnapshot_;
    mutable bool hasSnapshot_ = false;
};

// ---------------------------------------------------------------------------
// Factory surface
// ---------------------------------------------------------------------------
//
// Declared here rather than in the shared header so the shared header stays
// untouched. The registry integrator may either rely on the self-registration
// performed by LinuxKvmEngine.cpp, or call these explicitly (for
// example inside VirtualizationEngineRegistry::registerBuiltinBackends()).
// Both paths are safe: the registry replaces a backend that shares an id.

bool linuxKvmEngineAvailable();
VirtualizationEnginePtr makeLinuxKvmEngine();

} // namespace vw