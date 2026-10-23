#include "WindowsHypervisorEngine.hpp"

#include "Platform.hpp"
#include "QemuEngine.hpp"
#include "RuntimeManager.hpp"
#include "SystemSoftwareEngine.hpp"

#include <QDir>
#include <QFileInfo>
#include <QProcess>
#include <QProcessEnvironment>
#include <QStandardPaths>
#include <QtGlobal>

#if defined(_WIN32)
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN 1
#endif
#ifndef NOMINMAX
#define NOMINMAX 1
#endif
#include <windows.h>

// PROCESSOR_ARCHITECTURE_ARM64 is missing from older Windows SDKs.
#ifndef PROCESSOR_ARCHITECTURE_ARM64
#define PROCESSOR_ARCHITECTURE_ARM64 12
#endif
#endif

namespace vw {

namespace {

// Windows Hypervisor Platform capability codes (winhvplatform.h). Declared
// locally so this translation unit does not need the SDK header at all; the
// values are part of the documented, stable API surface.
const unsigned int kVwWhvCapabilityCodeHypervisorPresent = 0x00000000;

// One WHPX probe, with the two facts kept apart.
//
// The distinction matters for honesty: "winhvplatform.dll is missing" and
// "the DLL is there but Windows has no hypervisor" are different user problems
// with different fixes, and they are different states in WindowsHostState.
struct WhpxRuntimeProbe {
    bool apiLoadable = false;        // winhvplatform.dll loaded AND exports WHvGetCapability
    bool hypervisorPresent = false;  // WHvGetCapability reported a hypervisor
    QString detail;                  // human readable summary
    QString reason;                  // why it is not usable, empty when it is
};

#if defined(_WIN32)

// `WHvGetCapability` is resolved dynamically: no import library is needed and a
// missing/unavailable platform DLL can be reported honestly.
//   HRESULT WHvGetCapability(WHV_CAPABILITY_CODE CapabilityCode,
//                            VOID* CapabilityBuffer,
//                            UINT32 CapabilityBufferSizeInBytes,
//                            UINT32* WrittenSizeInBytes);
using VwWhvGetCapabilityFn = long(__stdcall*)(unsigned int, void*, unsigned int, unsigned int*);

const wchar_t* const kVwWinHvPlatformDll = L"winhvplatform.dll";

QString nativeWindowsArch() {
    SYSTEM_INFO info;
    ZeroMemory(&info, sizeof(info));
    // GetNativeSystemInfo reports the real host architecture even when this
    // process runs under WOW64 emulation.
    GetNativeSystemInfo(&info);
    switch (info.wProcessorArchitecture) {
    case PROCESSOR_ARCHITECTURE_AMD64:
        return QStringLiteral("x86_64");
    case PROCESSOR_ARCHITECTURE_ARM64:
        return QStringLiteral("aarch64");
    case PROCESSOR_ARCHITECTURE_INTEL:
        return QStringLiteral("i386");
    case PROCESSOR_ARCHITECTURE_ARM:
        return QStringLiteral("arm");
    default:
        return QStringLiteral("unknown");
    }
}

WhpxRuntimeProbe queryWhpxRuntime() {
    WhpxRuntimeProbe probe;

    HMODULE module = LoadLibraryW(kVwWinHvPlatformDll);
    if (module == nullptr) {
        probe.detail = QStringLiteral("winhvplatform.dll 无法加载。");
        probe.reason = QStringLiteral("未找到 winhvplatform.dll：Windows Hypervisor Platform 组件不可用。");
        return probe;
    }

    VwWhvGetCapabilityFn getCapability =
        reinterpret_cast<VwWhvGetCapabilityFn>(reinterpret_cast<void*>(
            GetProcAddress(module, "WHvGetCapability")));
    if (getCapability == nullptr) {
        probe.detail = QStringLiteral("winhvplatform.dll 中缺少 WHvGetCapability。");
        probe.reason = QStringLiteral("winhvplatform.dll 未导出 WHvGetCapability。");
        FreeLibrary(module);
        return probe;
    }

    // The DLL loaded and exports the entry point: the API itself is present.
    probe.apiLoadable = true;

    unsigned int capabilityValue = 0;
    unsigned int written = 0;
    const long status = getCapability(kVwWhvCapabilityCodeHypervisorPresent,
                                      &capabilityValue,
                                      sizeof(capabilityValue),
                                      &written);
    FreeLibrary(module);

    if (status < 0) {
        probe.detail = QStringLiteral("WHvGetCapability 返回 0x%1。")
                           .arg(QString::number(static_cast<unsigned long>(status), 16));
        probe.reason = QStringLiteral("WHvGetCapability 调用失败（HRESULT 0x%1）。")
                           .arg(QString::number(static_cast<unsigned long>(status), 16));
        return probe;
    }

    probe.hypervisorPresent = capabilityValue != 0;
    probe.detail = probe.hypervisorPresent
        ? QStringLiteral("WHvGetCapability 报告 hypervisor 存在。")
        : QStringLiteral("WHvGetCapability 报告 hypervisor 不存在。");
    if (!probe.hypervisorPresent) {
        probe.reason = QStringLiteral("Windows 未启用虚拟机平台/Hyper-V，WHPX 不可用。");
    }
    return probe;
}

#else // !_WIN32

QString nativeWindowsArch() {
    return QStringLiteral("unknown");
}

WhpxRuntimeProbe queryWhpxRuntime() {
    WhpxRuntimeProbe probe;
    probe.detail = QStringLiteral("非 Windows 宿主：不探测 WHPX。");
    probe.reason = QStringLiteral("WHPX 仅在 Windows 上可用。");
    return probe;
}

#endif // _WIN32

// True only for a host ISA WHPX is documented to drive.
bool isKnownWhpxHostArch(const QString& canonicalArch) {
    return canonicalArch == QStringLiteral("x86_64")
        || canonicalArch == QStringLiteral("aarch64");
}


// Mirrors RuntimeManager's own probe budget so a wedged binary cannot hang the
// caller; RuntimeManager::isExecutableHealthy() uses the same 8s.
constexpr int kRuntimeProbeTimeoutMs = 8000;

bool isWindowsFamily(const QString& family) {
    return family.compare(QStringLiteral("windows"), Qt::CaseInsensitive) == 0;
}

// Returns a path to an executable named `name` that exists on disk but that we
// have not proven runnable, or an empty string when nothing exists at all.
QString findUnprovenCandidate(const RuntimeManager& runtime, const QString& executableName) {
    for (const QString& dir : runtime.runtimeBinDirs()) {
        const QString candidate = QDir(dir).filePath(executableName);
        const QFileInfo info(candidate);
        if (info.exists() && info.isFile()) {
            return info.absoluteFilePath();
        }
    }
    const QString onPath = QStandardPaths::findExecutable(executableName);
    return onPath.isEmpty() ? QString() : onPath;
}

// One architecture's runtime verdict.
struct ArchRuntime {
    QString arch;
    bool runnable = false;
    QString executable;
    QString source;
    QString unprovenCandidate;
    bool timedOut = false;
};

ArchRuntime probeArchRuntime(const RuntimeManager& runtime, const QString& arch) {
    ArchRuntime result;
    result.arch = arch;

    const RuntimePath path = runtime.findSystemEmulator(arch);
    if (path.available) {
        result.runnable = true;
        result.executable = path.executable;
        result.source = path.source;
        return result;
    }
    result.source = QStringLiteral("missing");
    result.unprovenCandidate = findUnprovenCandidate(runtime, executableForArch(arch));
    if (!result.unprovenCandidate.isEmpty()) {
        QProcess process;
        process.setProgram(result.unprovenCandidate);
        process.setArguments({QStringLiteral("--version")});
        process.start();
        if (!process.waitForFinished(kRuntimeProbeTimeoutMs)) {
            result.timedOut = true;
            process.kill();
            process.waitForFinished(1000);
        }
    }
    return result;
}

// The real environment: host probe over the shared platform layer plus the
// application's existing QEMU plumbing.
class QemuWindowsEnvironment : public WindowsEnvironment {
public:
    WindowsHostSnapshot snapshot() const override {
        WindowsHostSnapshot snapshot;

        // --- host identity ---------------------------------------------------
        const HostProfile host = detectHostProfile();
        snapshot.hostFamily = host.family;
        snapshot.hostName = host.name;
        snapshot.hostArch = canonicalHostArch(
#if defined(_WIN32)
            nativeWindowsArch()
#else
            host.arch
#endif
        );
        snapshot.hostIsWindows = isWindowsFamily(host.family);

        if (!snapshot.hostIsWindows) {
            // Off-platform: deliberately no WHPX probing, no QEMU probing,
            // no process spawning. The backend reports host-unsupported cheaply.
            snapshot.detail = QStringLiteral("宿主平台 %1 不是 Windows，未执行 WHPX/QEMU 探测。")
                                  .arg(host.family);
            return snapshot;
        }

        // --- WHPX probe ------------------------------------------------------
        // A single probe answers both questions ("is the API there?" and "is
        // there a hypervisor?"), so the two states can never disagree with the
        // DLL that was actually loaded.
        const WhpxRuntimeProbe whpx = queryWhpxRuntime();
        snapshot.apiLoadable = whpx.apiLoadable;
        snapshot.hypervisorPresent = whpx.hypervisorPresent;
        snapshot.detail = whpx.detail;
        snapshot.reason = whpx.reason;

        // WHPX is usable when the API is loadable, a hypervisor is present, and
        // the host architecture is one WHPX is documented to drive.
        snapshot.whpxUsable = snapshot.apiLoadable && snapshot.hypervisorPresent
            && isKnownWhpxHostArch(snapshot.hostArch);

        if (snapshot.whpxUsable) {
            snapshot.reason.clear();
        } else if (snapshot.reason.isEmpty()) {
            // Usable API and hypervisor but an ISA WHPX has no path for.
            snapshot.reason = QStringLiteral("宿主架构 %1 没有对应的 WHPX 原生虚拟化路径。")
                                  .arg(snapshot.hostArch);
        }

        // --- QEMU runtime ----------------------------------------------------
        const RuntimeManager runtime;
        bool anyUnproven = false;
        bool anyTimedOut = false;
        for (const QString& arch : windowsCandidateGuestArches()) {
            const ArchRuntime probed = probeArchRuntime(runtime, arch);
            if (probed.runnable) {
                snapshot.runnableGuestArches << arch;
                if (snapshot.qemuPath.isEmpty()) {
                    snapshot.qemuPath = probed.executable;
                }
            }
            if (!probed.unprovenCandidate.isEmpty()) {
                anyUnproven = true;
                if (snapshot.qemuPath.isEmpty()) {
                    snapshot.qemuPath = probed.unprovenCandidate;
                }
            }
            anyTimedOut = anyTimedOut || probed.timedOut;
        }
        snapshot.qemuFound = !snapshot.runnableGuestArches.isEmpty() || anyUnproven;
        snapshot.qemuExecutable = !snapshot.runnableGuestArches.isEmpty();

        // --- narration -------------------------------------------------------
        const WindowsHostState state = windowsHostState(snapshot);
        snapshot.detail = QStringLiteral("Windows 宿主（%1），架构 %2，WHPX 状态：%3。")
                              .arg(snapshot.hostFamily, snapshot.hostArch, windowsStateCode(state));

        if (snapshot.runnableGuestArches.isEmpty()) {
            snapshot.notes << (snapshot.qemuFound
                                   ? QStringLiteral("QEMU 可执行文件存在但无法运行：可能是缺少动态库或沙箱策略限制。")
                                   : QStringLiteral("未找到 QEMU 系统模拟器；请安装到 "
                                                     "runtime/qemu/windows/%1/bin 或系统 PATH。")
                                         .arg(snapshot.hostArch));
        }
        if (anyTimedOut) {
            snapshot.notes << QStringLiteral("QEMU --version 探测超时，运行时结论可能不完整。");
        }

        // ARM64 WHPX degraded note.
        if (state == WindowsHostState::WhpxDegraded) {
            snapshot.notes << QStringLiteral("WHPX 在 ARM64 宿主上可用但可能存在已知限制；"
                                              "同架构客户机将通过 WHPX 原生执行，跨架构客户机将使用 TCG。");
        }

        snapshot.notes << QStringLiteral("本后端通过 QEMU 运行客户机；使用 WHPX 时 QEMU 提供设备模型，"
                                          "WHPX 提供硬件加速虚拟化。");
        return snapshot;
    }

    QString program(const VmConfig& config) const override {
        const QString arch = windowsCanonicalGuestArch(config.architecture);
        if (arch.isEmpty()) {
            return QString();
        }
        return QemuEngine().detect(arch).systemEmulator.executable;
    }

    QStringList arguments(const VmConfig& config) const override {
        return QemuEngine().buildArguments(config);
    }

    bool startProcess(const VmConfig& config, qint64* processId, QString* error) override {
        return QemuEngine().startVm(config, processId, error);
    }

    bool stopProcess(qint64 processId, QString* error) override {
        return QemuEngine().stopVm(processId, error);
    }
};

} // namespace

// ---------------------------------------------------------------------------
// Legacy probe (backward compatible)
// ---------------------------------------------------------------------------

WhpxProbeResult probeWhpx() {
    WhpxProbeResult result;
#if defined(_WIN32)
    result.hostIsWindows = true;
#else
    result.hostIsWindows = false;
#endif

    result.hostArch = canonicalHostArch(result.hostIsWindows ? nativeWindowsArch()
                                                             : detectHostProfile().arch);
    if (result.hostArch.isEmpty()) {
        result.hostArch = QStringLiteral("unknown");
    }

    const bool knownArch = nativeGuestArchForHost(result.hostArch) == result.hostArch;
    if (result.hostIsWindows && !knownArch) {
        result.reason = QStringLiteral("宿主架构 %1 没有对应的 WHPX 原生虚拟化路径。").arg(result.hostArch);
        result.detail = QStringLiteral("WHPX 后端只覆盖 x86_64 与 ARM64 宿主。");
        return result;
    }

    if (!result.hostIsWindows) {
        const WhpxRuntimeProbe probed = queryWhpxRuntime();
        result.detail = probed.detail;
        result.reason = probed.reason;
        return result;
    }

    // apiLoadable and hypervisorPresent come from the same probe, so they can
    // never contradict the DLL that was actually loaded.
    const WhpxRuntimeProbe probed = queryWhpxRuntime();
    result.detail = probed.detail;
    result.reason = probed.reason;
    result.apiLoadable = probed.apiLoadable;
    result.hypervisorPresent = probed.hypervisorPresent;
    result.whpxUsable = probed.apiLoadable && probed.hypervisorPresent && knownArch;
    if (result.whpxUsable) {
        result.reason.clear();
    } else if (result.reason.isEmpty()) {
        result.reason = QStringLiteral("宿主架构 %1 没有对应的 WHPX 原生虚拟化路径。").arg(result.hostArch);
    }
    return result;
}

bool whpxAvailable(QString* reason) {
    const WhpxProbeResult probed = probeWhpx();
    if (!probed.whpxUsable && reason) {
        *reason = probed.reason.isEmpty() ? QStringLiteral("WHPX 不可用。") : probed.reason;
    }
    return probed.whpxUsable;
}

// ---------------------------------------------------------------------------
// Environment factory
// ---------------------------------------------------------------------------

QSharedPointer<WindowsEnvironment> makeWindowsEnvironment() {
    return QSharedPointer<WindowsEnvironment>(new QemuWindowsEnvironment());
}

// ---------------------------------------------------------------------------
// Engine implementation
// ---------------------------------------------------------------------------

bool WindowsHypervisorEngine::isHostSupported(QString* reason) const {
    const WindowsHostSnapshot& host = hostSnapshot();
    if (!host.hostIsWindows) {
        if (reason) {
            *reason = QStringLiteral("宿主不是 Windows，本后端不适用。");
        }
        return false;
    }
    // The backend drives QEMU for the device model even when WHPX is present,
    // so a Windows host without a runnable QEMU cannot run any guest at all —
    // with WHPX or with TCG.
    if (!host.qemuExecutable) {
        if (reason) {
            *reason = host.qemuFound
                ? QStringLiteral("QEMU 可执行文件存在但无法运行；本后端需要 QEMU 提供设备模型。")
                : QStringLiteral("QEMU 运行时不可用；本后端需要 QEMU 提供设备模型。");
        }
        return false;
    }
    return true;
}

bool WindowsHypervisorEngine::supportsGuestArch(const QString& guestArch, QString* reason) const {
    const WindowsHostSnapshot& host = hostSnapshot();
    const QString guest = windowsCanonicalGuestArch(guestArch);

    if (guest.isEmpty()) {
        if (reason) {
            *reason = guestArch.trimmed().isEmpty()
                ? QStringLiteral("未指定客户机架构。")
                : QStringLiteral("本后端仅提供 x86_64 / aarch64 客户机，"
                                 "架构 \"%1\" 请使用 QEMU 后端。")
                      .arg(guestArch.trimmed());
        }
        return false;
    }

    if (!host.hostIsWindows) {
        if (reason) {
            *reason = QStringLiteral("宿主不是 Windows，本后端不适用。");
        }
        return false;
    }

    // WHPX is a same-ISA hypervisor API: a cross-ISA guest is never this
    // backend's — it belongs to qemu-tcg. Rejecting it here (rather than
    // claiming "supported but slow") is what keeps auto-selection honest.
    if (!windowsGuestIsSameIsa(host, guest)) {
        if (reason) {
            *reason = QStringLiteral("客户机 %1 与宿主 %2 架构不同：WHPX 只支持同架构虚拟化，"
                                     "跨架构客户机请使用 qemu-tcg 后端。")
                          .arg(guest, canonicalHostArch(host.hostArch));
        }
        return false;
    }

    if (!host.runnableGuestArches.contains(guest)) {
        if (reason) {
            *reason = host.qemuFound
                ? QStringLiteral("架构 %1 的 QEMU 系统模拟器存在但无法运行。").arg(guest)
                : QStringLiteral("未找到架构 %1 的 QEMU 系统模拟器。").arg(guest);
        }
        return false;
    }

    return true;
}

EngineProbeResult WindowsHypervisorEngine::probe() const {
    const WindowsHostSnapshot& host = hostSnapshot();
    const WindowsHostState state = windowsHostState(host);

    EngineProbeResult result;
    result.backendId = backendId();
    result.displayName = displayName();
    result.tier = tier();
    result.hostSupported = host.hostIsWindows && host.qemuExecutable;

    EngineCapabilities& caps = result.capabilities;
    caps.hostArch = host.hostArch;
    caps.hostSupported = result.hostSupported;

    // nativeExecution is only true for same-ISA WHPX guests. Without a guest
    // architecture to judge, this is the host-level answer: "WHPX can drive a
    // same-ISA guest here". probeFor() narrows it per guest.
    const bool hasNativeCapability = windowsStateIsNative(state);
    caps.nativeExecution = hasNativeCapability;
    caps.hardwareAcceleration = hasNativeCapability;

    // Host-level guest support: the only ISA this backend can ever run is the
    // host's own, so that is what "guest architecture supported" means here.
    caps.guestArchSupported = host.hostIsWindows
        && windowsCanRunGuest(host, canonicalHostArch(host.hostArch));
    caps.canStart = caps.hostSupported && caps.guestArchSupported;
    caps.canStop = caps.canStart;

    // Default accelerator: whpx when available, tcg otherwise.
    caps.accelerator = hasNativeCapability ? QStringLiteral("whpx") : QStringLiteral("tcg");

    if (hasNativeCapability) {
        caps.summary = QStringLiteral("WHPX 可用：宿主 %1 可原生运行同架构客户机；"
                                      "跨架构客户机由 qemu-tcg 承担。")
                           .arg(host.hostArch);
    } else if (host.qemuExecutable) {
        caps.summary = QStringLiteral("WHPX 不可用（%1），但 QEMU TCG 可用："
                                      "同架构客户机将通过软件模拟执行。")
                           .arg(windowsStateCode(state));
    } else {
        caps.summary = host.detail.isEmpty()
                           ? QStringLiteral("WHPX 不可用且 QEMU 运行时缺失。")
                           : host.detail;
    }

    // Diagnostics based on host state.
    const QString message = windowsStateMessage(state, QString());
    switch (state) {
    case WindowsHostState::NotWindowsHost:
        result.diagnostics << engineIssue(EngineIssueLevel::Error, QStringLiteral("host-unsupported"), message);
        break;
    case WindowsHostState::WhpxApiMissing:
        result.diagnostics << engineIssue(EngineIssueLevel::Warning, QStringLiteral("whpx-api-missing"), message);
        break;
    case WindowsHostState::WhpxHypervisorNotPresent:
        result.diagnostics << engineIssue(EngineIssueLevel::Warning, QStringLiteral("whpx-hypervisor-not-present"), message);
        break;
    case WindowsHostState::WhpxDegraded:
        result.diagnostics << engineIssue(EngineIssueLevel::Warning, QStringLiteral("whpx-degraded"), message);
        break;
    case WindowsHostState::WhpxAvailable:
    default:
        result.diagnostics << engineIssue(EngineIssueLevel::Info, QStringLiteral("whpx-available"), message);
        break;
    }

    // QEMU runtime diagnostics.
    if (host.hostIsWindows && !host.qemuExecutable) {
        result.diagnostics << engineIssue(EngineIssueLevel::Error, QStringLiteral("runtime-missing"),
                                          QStringLiteral("QEMU 运行时不可用；本后端需要 QEMU 提供设备模型。"));
    } else if (host.hostIsWindows && host.qemuExecutable) {
        result.diagnostics << engineIssue(EngineIssueLevel::Info, QStringLiteral("runtime-available"),
                                          QStringLiteral("QEMU 运行时可用：%1").arg(host.qemuPath));
    } else if (host.hostIsWindows) {
        result.diagnostics << engineIssue(EngineIssueLevel::Error, QStringLiteral("runtime-missing"),
                                          QStringLiteral("未找到可运行的 QEMU 系统模拟器。"));
    }

    // Extra notes from the snapshot.
    for (const QString& note : host.notes) {
        result.diagnostics << engineIssue(EngineIssueLevel::Info, QStringLiteral("note"), note);
    }

    result.summary = caps.summary;
    return result;
}

EngineProbeResult WindowsHypervisorEngine::probeFor(const QString& guestArch) const {
    const WindowsHostSnapshot& host = hostSnapshot();

    EngineProbeResult result = probe();

    // No guest architecture asked for: the host-level probe() answer stands.
    // (The shared registry never routes an empty architecture here — it calls
    // probe() instead — but a direct caller may.)
    if (guestArch.trimmed().isEmpty()) {
        return result;
    }

    QString reason;
    const bool supported = supportsGuestArch(guestArch, &reason);

    // The backend tier never moves: it names the mechanism (WHPX), not the
    // outcome for this guest. tier() was already assigned by probe().
    result.capabilities.guestArchSupported = supported;
    result.capabilities.canStart = supported && result.hostSupported;
    result.capabilities.canStop = result.capabilities.canStart;

    // Always recompute from the per-guest decision — never inherit probe()'s
    // host-level answer, which would leave nativeExecution=true on a guest this
    // backend cannot accelerate (or run at all).
    const QString accelerator = supported ? windowsAcceleratorFor(host, guestArch) : QString();
    const bool accelerated = supported && accelerator == QStringLiteral("whpx");
    result.capabilities.accelerator = accelerator;
    result.capabilities.nativeExecution = accelerated;
    result.capabilities.hardwareAcceleration = accelerated;

    if (!supported) {
        result.diagnostics << engineIssue(EngineIssueLevel::Warning,
                                          QStringLiteral("guest-arch-unsupported"), reason);
        result.summary = reason.isEmpty()
            ? QStringLiteral("本后端不支持该客户机架构。")
            : reason;
        return result;
    }

    if (accelerated) {
        result.summary = QStringLiteral("WHPX 可用：客户机 %1 将与宿主同架构原生执行（tier=native）。")
                             .arg(guestArch);
    } else {
        result.summary = QStringLiteral("客户机 %1 与宿主 %2 同架构，但 WHPX 不可用（%3）："
                                        "将由 QEMU TCG 软件模拟执行。")
                             .arg(guestArch, canonicalHostArch(host.hostArch),
                                  windowsStateCode(windowsHostState(host)));
    }
    return result;
}

QVector<EngineIssue> WindowsHypervisorEngine::validate(const VmConfig& config) const {
    const WindowsHostSnapshot& host = hostSnapshot();
    const QString guestArch = windowsCanonicalGuestArch(config.architecture);
    return windowsValidateConfig(config, host, guestArch);
}

bool WindowsHypervisorEngine::start(const VmConfig& config, qint64* processId, QString* error) const {
    const WindowsHostSnapshot& host = hostSnapshot();
    const QString guest = windowsCanonicalGuestArch(config.architecture);

    // Pre-flight: guest arch must be supported.
    if (guest.isEmpty()) {
        if (error) {
            *error = QStringLiteral("本后端仅提供 x86_64 / aarch64 客户机。");
        }
        return false;
    }

    // Pre-flight: host must be Windows with QEMU available.
    if (!host.hostIsWindows) {
        if (error) {
            *error = QStringLiteral("宿主不是 Windows，本后端不适用。");
        }
        return false;
    }
    // Cross-ISA guests are never this backend's: refuse them explicitly rather
    // than quietly launching at TCG speed under a WHPX-scoped backend.
    if (!windowsGuestIsSameIsa(host, guest)) {
        if (error) {
            *error = QStringLiteral("客户机 %1 与宿主 %2 架构不同：WHPX 只支持同架构虚拟化，"
                                    "请改用 qemu-tcg 后端。")
                         .arg(guest, canonicalHostArch(host.hostArch));
        }
        return false;
    }
    if (!host.qemuExecutable) {
        if (error) {
            *error = QStringLiteral("QEMU 运行时不可用，无法启动客户机。");
        }
        return false;
    }
    if (!host.runnableGuestArches.contains(guest)) {
        if (error) {
            *error = QStringLiteral("未找到架构 %1 的 QEMU 系统模拟器。").arg(guest);
        }
        return false;
    }

    // Build the launched config with the correct accelerator.
    VmConfig launched = launchedConfig(config);

    // Argument well-formedness check.
    if (environment_) {
        const QStringList args = environment_->arguments(launched);
        QString problem;
        if (!windowsArgumentsAreWellFormed(args, &problem)) {
            if (error) {
                *error = problem;
            }
            return false;
        }
    }

    // Delegate to the environment seam. The caller's pid is only written on a
    // verified success, so a failed attempt cannot leave a stale-looking pid
    // behind for the GUI to mistake for a running guest.
    if (!environment_) {
        if (error) {
            *error = QStringLiteral("引擎环境未初始化。");
        }
        return false;
    }
    qint64 pid = 0;
    if (!environment_->startProcess(launched, &pid, error)) {
        return false;
    }
    if (pid <= 0) {
        // The spawner did not give us a process id, so we cannot honestly claim
        // a guest is running — no matter what it returned.
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

bool WindowsHypervisorEngine::stop(qint64 processId, QString* error) const {
    if (!environment_) {
        if (error) {
            *error = QStringLiteral("引擎环境未初始化。");
        }
        return false;
    }
    // A non-positive id cannot name a real QEMU process. Refusing here (like the
    // Linux and Harmony backends) keeps a "stop nothing" from being reported as
    // a successful stop, which the GUI would otherwise show as a shut-down VM.
    if (processId <= 0) {
        if (error) {
            *error = QStringLiteral("无效的进程 ID，没有可停止的客户机。");
        }
        return false;
    }
    return environment_->stopProcess(processId, error);
}

QString WindowsHypervisorEngine::diagnose(const VmConfig& config) const {
    const WindowsHostSnapshot& host = hostSnapshot();
    const WindowsHostState state = windowsHostState(host);
    const QString guest = windowsCanonicalGuestArch(config.architecture);

    QStringList lines;
    lines << QStringLiteral("WHPX 后端诊断");
    lines << QStringLiteral("  后端层级: %1（WHPX 宿主 hypervisor API；不随宿主/客户机变化）")
                 .arg(tierName(tier()));
    lines << QStringLiteral("  宿主平台: %1").arg(host.hostIsWindows ? host.hostName : QStringLiteral("非 Windows"));
    lines << QStringLiteral("  宿主架构: %1").arg(host.hostArch);
    lines << QStringLiteral("  WHPX 状态: %1").arg(windowsStateCode(state));
    lines << QStringLiteral("  WHPX API 可加载: %1").arg(host.apiLoadable ? QStringLiteral("是") : QStringLiteral("否"));
    lines << QStringLiteral("  Hypervisor 存在: %1").arg(host.hypervisorPresent ? QStringLiteral("是") : QStringLiteral("否"));
    lines << QStringLiteral("  WHPX 可用: %1").arg(host.whpxUsable ? QStringLiteral("是") : QStringLiteral("否"));
    lines << QStringLiteral("  QEMU 可执行: %1").arg(host.qemuExecutable ? QStringLiteral("是") : QStringLiteral("否"));
    if (host.qemuExecutable) {
        lines << QStringLiteral("  QEMU 路径: %1").arg(host.qemuPath);
    }
    lines << QStringLiteral("  可运行架构: %1").arg(host.runnableGuestArches.isEmpty()
                                                       ? QStringLiteral("（无）")
                                                       : host.runnableGuestArches.join(QStringLiteral(", ")));
    if (!guest.isEmpty()) {
        QString reason;
        const bool supported = supportsGuestArch(guest, &reason);
        lines << QStringLiteral("  客户机架构: %1").arg(guest);
        if (supported) {
            const QString accelerator = windowsAcceleratorFor(host, guest);
            lines << QStringLiteral("  加速器: %1").arg(accelerator);
            lines << QStringLiteral("  执行层级: %1").arg(
                accelerator == QStringLiteral("whpx")
                    ? QStringLiteral("Native (WHPX)")
                    : QStringLiteral("SoftwareEmulation (TCG)"));
        } else {
            lines << QStringLiteral("  本后端不承担该客户机: %1").arg(reason);
        }
    }
    if (!host.reason.isEmpty()) {
        lines << QStringLiteral("  WHPX 不可用原因: %1").arg(host.reason);
    }
    for (const QString& note : host.notes) {
        lines << QStringLiteral("  注: %1").arg(note);
    }
    lines << VirtualizationEngine::diagnose(config).split(QLatin1Char('\n'));
    return lines.join(QLatin1Char('\n'));
}

QString WindowsHypervisorEngine::statusCode(const VmConfig& config) const {
    const WindowsHostSnapshot& host = hostSnapshot();
    const WindowsHostState state = windowsHostState(host);
    const QString guest = windowsCanonicalGuestArch(config.architecture);

    // Order matters: a code must name the *blocking* reason. Without a host, a
    // supported guest architecture, or a QEMU runtime, nothing can run at all —
    // those outrank "which WHPX feature is missing".
    if (!host.hostIsWindows) {
        return QStringLiteral("host-unsupported");
    }
    if (guest.isEmpty()) {
        return QStringLiteral("guest-arch-unsupported");
    }
    if (!windowsGuestIsSameIsa(host, guest)) {
        return QStringLiteral("guest-arch-unsupported");
    }
    if (!host.qemuExecutable || !host.runnableGuestArches.contains(guest)) {
        return QStringLiteral("runtime-missing");
    }
    if (state == WindowsHostState::WhpxApiMissing) {
        return QStringLiteral("whpx-api-missing");
    }
    if (state == WindowsHostState::WhpxHypervisorNotPresent) {
        return QStringLiteral("whpx-hypervisor-not-present");
    }
    if (state == WindowsHostState::WhpxDegraded) {
        return QStringLiteral("whpx-degraded");
    }
    return QStringLiteral("available");
}

QStringList WindowsHypervisorEngine::requirements() const {
    return {
        QStringLiteral("Windows 10 1803 或更高版本（x86_64 或 ARM64 宿主）。"),
        QStringLiteral("启用“虚拟机平台”(Virtual Machine Platform) / Windows Hypervisor Platform 可选功能。"),
        QStringLiteral("winhvplatform.dll 可用且 WHvGetCapability 报告 hypervisor 存在。"),
        QStringLiteral("客户机执行通过 QEMU 完成（-accel whpx 或 -accel tcg），因此仍需 QEMU 运行时。"),
        QStringLiteral("只支持与宿主同架构（x86_64 或 aarch64）的客户机；"
                       "跨架构客户机请使用 qemu-tcg 后端。"),
    };
}

// ---------------------------------------------------------------------------
// Factory functions
// ---------------------------------------------------------------------------

bool windowsHypervisorEngineAvailable() {
    return WindowsHypervisorEngine().isHostSupported();
}

VirtualizationEnginePtr makeWindowsHypervisorEngine() {
    return VirtualizationEnginePtr(new WindowsHypervisorEngine());
}

} // namespace vw

// ---------------------------------------------------------------------------
// Registry self-registration
// ---------------------------------------------------------------------------
//
// Registration is unconditional because the backend is plain Qt code on every
// platform and reports `host-unsupported` honestly elsewhere, which is what
// lets `--list-backends` document the whole matrix. registerBackend() replaces
// by id, so an explicit registration from registerBuiltinBackends() would be
// harmless rather than duplicative.

#include "VirtualizationEngineRegistry.hpp"

namespace {

const bool vwWindowsHypervisorBackendRegistered = []() {
    vw::VirtualizationEngineRegistry::instance().registerBackend(
        vw::makeWindowsHypervisorEngine());
    return true;
}();

} // namespace