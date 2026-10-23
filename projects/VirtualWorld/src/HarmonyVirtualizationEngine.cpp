// HarmonyVirtualizationEngine.cpp — HarmonyOS/OpenHarmony QEMU compatibility backend.
//
// This file owns the only impure parts of the backend:
//
//   1. Reading the host family, architecture, and /dev/kvm accessibility.
//   2. Rendering the QEMU argument vector for diagnostics only.
//
// HarmonyOS must never probe or launch an external qemu-system executable.
// Until QemuInProcessRuntime is release-ready and linked into the product, the
// real environment reports the runtime unavailable and start() fails closed.
//
// Everything decision-shaped lives in the header, driven by the snapshot this
// file produces, so the backend can be unit-tested on any platform.
//
// IMPORTANT probing rule: on a host that is not HarmonyOS/OpenHarmony this file
// performs NO QEMU probing at all. vw::RuntimeManager runs `<qemu> --version`
// with a timeout, which is far too expensive (and too side-effecting) to do on
// every macOS/Windows/Linux capability query. The snapshot is therefore built in
// two stages and the second stage is skipped entirely off-platform.

#include "HarmonyVirtualizationEngine.hpp"

#include "Platform.hpp"
#include "QemuEngine.hpp"
#include <QFileInfo>
#include <QtGlobal>

#include <QHash>
#include <atomic>
#include <memory>

// The real in-process QEMU core is only reachable when BOTH conditions hold:
//   * HARMONY_INPROCESS_REAL_CORE   — the static QEMU core is linked in, and
//   * DEEPSEEK_HOST_SAFETY_GATE_PASSED — DeepSeek's qemu_init fatal-exit safety
//     gate is verified. Until that gate lands, the core's init() can still call
//     exit() on a malformed config and kill the whole process, so the public
//     build MUST stay fail-closed. Enabling DEEPSEEK_HOST_SAFETY_GATE_PASSED
//     before the fix is unsafe and is intentionally OFF by default.
#if defined(HARMONY_INPROCESS_REAL_CORE) && defined(DEEPSEEK_HOST_SAFETY_GATE_PASSED)
#include "QemuInProcessRuntime.hpp"
#endif

namespace vw {

namespace {

bool isHarmonyFamily(const QString& family) {
    return family.contains(QStringLiteral("harmonyos"), Qt::CaseInsensitive)
        || family.contains(QStringLiteral("openharmony"), Qt::CaseInsensitive);
}

// The real environment: host probe over the shared platform layer plus the
// application's argument translator. It deliberately has no external-process
// fallback.
class QemuHarmonyEnvironment : public HarmonyEnvironment {
public:
    HarmonyHostSnapshot snapshot() const override {
        HarmonyHostSnapshot snapshot;
        const HostProfile host = detectHostProfile();
        snapshot.hostFamily = host.family;
        snapshot.hostName = host.name;
        snapshot.hostPlatformName = platformName(host.platform);
        snapshot.hostArch = canonicalHostArch(host.arch);
        snapshot.hostIsHarmony = isHarmonyFamily(host.family);

        if (!snapshot.hostIsHarmony) {
            // Off-platform: deliberately no QEMU probing, no process spawning,
            // no /dev/kvm lookups. The backend reports host-unsupported cheaply.
            snapshot.detail = QStringLiteral("宿主平台 %1 不是 HarmonyOS/OpenHarmony，未执行 QEMU 探测。")
                                  .arg(snapshot.hostPlatformName);
            return snapshot;
        }

        snapshot.qemuProbePerformed = true;
        snapshot.detail = QStringLiteral("HarmonyOS/OpenHarmony 宿主（%1），架构 %2。")
                              .arg(host.family, snapshot.hostArch);

        // Do not search PATH, execute `--version`, or inspect an external
        // qemu-system binary. The only acceptable Harmony runtime is the linked
        // in-process core. It remains gated off until its release blocker is
        // cleared and the product build links the target archive.
        snapshot.qemuFound = false;
        snapshot.qemuExecutable = false;
        snapshot.qemuProbeTimedOut = false;
        snapshot.qemuPath.clear();
        snapshot.qemuSource = QStringLiteral("in-process-core-not-linked");
        snapshot.runnableGuestArches.clear();

#if defined(Q_OS_WIN)
        snapshot.kvmDevicePresent = false;
        snapshot.kvmDeviceAccessible = false;
#else
        const QFileInfo kvm(QStringLiteral("/dev/kvm"));
        snapshot.kvmDevicePresent = kvm.exists();
        snapshot.kvmDeviceAccessible = snapshot.kvmDevicePresent && kvm.isReadable() && kvm.isWritable();
#endif

        snapshot.notes << QStringLiteral("进程内 QEMU 核心尚未连入产品；为避免 HarmonyOS 拦截或"
                                         "误启动子进程，外部 QEMU 探测和启动已禁用。");
        snapshot.notes << QStringLiteral("DeepSeek 宿主安全门（qemu_init 仍可致命 exit()）尚未通过，"
                                         "公共构建在此明确 fail-closed，不回退外部进程、不伪报启动。");
        if (snapshot.kvmDevicePresent && !snapshot.kvmDeviceAccessible) {
            snapshot.notes << QStringLiteral("/dev/kvm 存在但不可读写；硬件加速不可用，将使用 TCG。");
        }
        if (snapshot.kvmDevicePresent && snapshot.kvmDeviceAccessible
            && !snapshot.runnableGuestArches.contains(snapshot.hostArch)) {
            snapshot.notes << QStringLiteral("/dev/kvm 可用，但缺少宿主同架构（%1）的 QEMU 模拟器，"
                                             "无法利用硬件加速。")
                                  .arg(snapshot.hostArch);
        }
        snapshot.notes << QStringLiteral("HarmonyOS/OpenHarmony 无公开稳定的通用原生虚拟化 API，"
                                         "本后端只接受静态链接的进程内 QEMU 核心。");
        return snapshot;
    }

    QString program(const VmConfig& config) const override {
        Q_UNUSED(config);
        return QString();
    }

    QStringList arguments(const VmConfig& config) const override {
        return QemuEngine().buildArguments(config);
    }

    bool startProcess(const VmConfig& config, qint64* processId, QString* error) override {
        Q_UNUSED(config);
        if (processId) {
            *processId = 0;
        }
        if (error) {
            *error = QStringLiteral("进程内 QEMU 核心尚未连入产品（DeepSeek 宿主安全门未通过："
                                    "qemu_init 仍存在致命 exit() 风险），HarmonyOS 后端禁止回退到"
                                    "外部子进程。");
        }
        return false;
    }

    bool stopProcess(qint64 processId, QString* error) override {
        Q_UNUSED(processId);
        if (error) {
            *error = QStringLiteral("没有已连接的进程内 QEMU 会话。");
        }
        return false;
    }
};

} // namespace

// ---------------------------------------------------------------------------
// Real in-process QEMU environment (gated)
// ---------------------------------------------------------------------------
//
// Active only when HARMONY_INPROCESS_REAL_CORE and DEEPSEEK_HOST_SAFETY_GATE_PASSED
// are both defined. It drives QEMU strictly through QemuInProcessRuntime's
// same-PID API (create / init / start / stop / destroy). It never touches
// QProcess, fork/exec/system/popen/posix_spawn, never launches an external
// qemu-system, and never calls workerPid() — the token it hands back is an
// opaque session id, never a PID.
#if defined(HARMONY_INPROCESS_REAL_CORE) && defined(DEEPSEEK_HOST_SAFETY_GATE_PASSED)

namespace {

vw::qemu::Isa harmonyArchToQemuIsa(const QString& arch) {
    const QString a = canonicalGuestArch(arch);
    if (a == QStringLiteral("aarch64")) return vw::qemu::Isa::AArch64;
    if (a == QStringLiteral("x86_64")) return vw::qemu::Isa::X86_64;
    if (a == QStringLiteral("riscv64")) return vw::qemu::Isa::RISCV64;
    return vw::qemu::Isa::Unknown;
}

// Shared host probe for both the gated and the fail-closed environment.
HarmonyHostSnapshot harmonyProbeHost() {
    HarmonyHostSnapshot snap;
    const HostProfile host = detectHostProfile();
    snap.hostFamily = host.family;
    snap.hostName = host.name;
    snap.hostPlatformName = platformName(host.platform);
    snap.hostArch = canonicalHostArch(host.arch);
    snap.hostIsHarmony = isHarmonyFamily(host.family);
    if (!snap.hostIsHarmony) {
        snap.detail = QStringLiteral("宿主平台 %1 不是 HarmonyOS/OpenHarmony，未执行 QEMU 探测。")
                          .arg(snap.hostPlatformName);
        return snap;
    }
    snap.qemuProbePerformed = true;
    snap.detail = QStringLiteral("HarmonyOS/OpenHarmony 宿主（%1），架构 %2。")
                      .arg(host.family, snap.hostArch);
#if defined(Q_OS_WIN)
    snap.kvmDevicePresent = false;
    snap.kvmDeviceAccessible = false;
#else
    const QFileInfo kvm(QStringLiteral("/dev/kvm"));
    snap.kvmDevicePresent = kvm.exists();
    snap.kvmDeviceAccessible = snap.kvmDevicePresent && kvm.isReadable() && kvm.isWritable();
#endif
    return snap;
}

class QemuInProcessHarmonyEnvironment : public HarmonyEnvironment {
public:
    QemuInProcessHarmonyEnvironment()
        : runtime_(std::make_unique<vw::qemu::QemuInProcessRuntime>()) {}

    HarmonyHostSnapshot snapshot() const override {
        HarmonyHostSnapshot snap = harmonyProbeHost();
        if (!snap.hostIsHarmony) {
            return snap;
        }
        // The linked static core can emulate every candidate arch under TCG and is
        // the only runtime this backend accepts.
        snap.qemuFound = true;
        snap.qemuExecutable = true;
        snap.qemuSource = QStringLiteral("in-process-core-linked");
        if (snap.runnableGuestArches.isEmpty()) {
            snap.runnableGuestArches = harmonyCandidateGuestArches();
        }
        return snap;
    }

    QString program(const VmConfig&) const override { return QString(); }

    QStringList arguments(const VmConfig& config) const override {
        return QemuEngine().buildArguments(config);
    }

    bool startProcess(const VmConfig& config, qint64* processId, QString* error) override {
        const QString guestArch = harmonyCanonicalGuestArch(config.architecture);
        const vw::qemu::Isa targetIsa = harmonyArchToQemuIsa(guestArch);
        if (targetIsa == vw::qemu::Isa::Unknown) {
            if (error) {
                *error = QStringLiteral("客户机架构 %1 无法映射到进程内 QEMU 核心。").arg(config.architecture);
            }
            return false;
        }

        const HarmonyHostSnapshot snap = harmonyProbeHost();
        const vw::qemu::Isa hostIsa = harmonyArchToQemuIsa(snap.hostArch);
        // Hardware acceleration is reported ONLY when the core is truly usable AND
        // the guest is the same ISA as the host. Cross-ISA guests always run TCG.
        const bool kvmAccessible = snap.kvmDevicePresent && snap.kvmDeviceAccessible
                                   && hostIsa == targetIsa;
        const vw::qemu::AccelDecision decision =
            vw::qemu::QemuInProcessRuntime::decideAccel(hostIsa, targetIsa, kvmAccessible);

        vw::qemu::QemuSessionHandle handle = nullptr;
        vw::qemu::QemuErrorCode ec = runtime_->create("harmony", handle);
        if (ec != vw::qemu::QemuErrorCode::Ok) {
            if (error) {
                *error = QStringLiteral("进程内 QEMU 会话创建失败（%1）。").arg(static_cast<int>(ec));
            }
            return false;
        }
        if (hostIsa != vw::qemu::Isa::Unknown) {
            runtime_->setHostIsa(hostIsa);
        }

        vw::qemu::QemuLaunchConfig launch;
        launch.targetIsa = targetIsa;
        launch.memoryBytes = static_cast<uint64_t>(config.memoryMb) * 1024ULL * 1024ULL;
        launch.machine = (config.machine.isEmpty() ? QStringLiteral("virt") : config.machine).toStdString();
        // Minimal, guest-OS-free harness when no extra args are supplied; the
        // caller may forward its own. We never append a guest ROM/OS here.
        launch.extraArgs = config.extraArgs.trimmed().isEmpty()
            ? std::string("-nodefaults -S")
            : config.extraArgs.toStdString();

        ec = runtime_->init(handle, launch);
        if (ec != vw::qemu::QemuErrorCode::Ok) {
            // init() failure must propagate clearly and release the core. This is
            // exactly the path DeepSeek's safety gate hardens: we must NOT swallow
            // it and must NOT fall back to an external process.
            runtime_->destroy(handle);
            if (error) {
                *error = QStringLiteral("进程内 QEMU 初始化失败（%1）：%2")
                             .arg(static_cast<int>(ec),
                                  decision.hardwareAccelerated
                                      ? QStringLiteral("已请求 KVM 硬件加速")
                                      : QStringLiteral("TCG 软件模拟"));
            }
            return false;
        }

        ec = runtime_->start(handle);
        if (ec != vw::qemu::QemuErrorCode::Ok) {
            runtime_->destroy(handle);
            if (error) {
                *error = QStringLiteral("进程内 QEMU 主循环启动失败（%1）。").arg(static_cast<int>(ec));
            }
            return false;
        }

        // Opaque session token only. We deliberately do NOT call workerPid(): the
        // value must never be interpreted as a PID by any caller.
        const qint64 token = nextToken_.fetch_add(1, std::memory_order_relaxed);
        {
            std::lock_guard<std::mutex> lock(mapMutex_);
            sessions_.insert(token, handle);
        }
        if (processId) {
            *processId = token;
        }
        return true;
    }

    bool stopProcess(qint64 processId, QString* error) override {
        vw::qemu::QemuSessionHandle handle = nullptr;
        {
            std::lock_guard<std::mutex> lock(mapMutex_);
            auto it = sessions_.find(processId);
            if (it == sessions_.end()) {
                if (error) {
                    *error = QStringLiteral("没有与该会话令牌对应的进程内 QEMU 会话。");
                }
                return false;
            }
            handle = it.value();
        }
        // stop() and destroy() are reentrant and idempotent in the core: repeated
        // stop/destroy is always safe and must never spawn or crash.
        const vw::qemu::QemuErrorCode stopEc = runtime_->stop(handle);
        const vw::qemu::QemuErrorCode destroyEc = runtime_->destroy(handle);
        {
            std::lock_guard<std::mutex> lock(mapMutex_);
            sessions_.remove(processId);
        }
        if (stopEc != vw::qemu::QemuErrorCode::Ok || destroyEc != vw::qemu::QemuErrorCode::Ok) {
            if (error) {
                *error = QStringLiteral("进程内 QEMU 会话停止/销毁失败（stop=%1 destroy=%2）。")
                             .arg(static_cast<int>(stopEc)).arg(static_cast<int>(destroyEc));
            }
            return false;
        }
        return true;
    }

private:
    std::unique_ptr<vw::qemu::QemuInProcessRuntime> runtime_;
    std::mutex mapMutex_;
    QHash<qint64, vw::qemu::QemuSessionHandle> sessions_;
    std::atomic<qint64> nextToken_{1};
};

} // namespace

QSharedPointer<HarmonyEnvironment> makeHarmonyEnvironment() {
    return QSharedPointer<HarmonyEnvironment>(new QemuInProcessHarmonyEnvironment());
}

#else // !HARMONY_INPROCESS_REAL_CORE || !DEEPSEEK_HOST_SAFETY_GATE_PASSED

QSharedPointer<HarmonyEnvironment> makeHarmonyEnvironment() {
    // Fail-closed: no linked core (or the DeepSeek safety gate is unverified), so
    // we never reach an external spawner and never pretend to have started a guest.
    return QSharedPointer<HarmonyEnvironment>(new QemuHarmonyEnvironment());
}

#endif

bool harmonyVirtualizationEngineAvailable() {
    // Never lies: false when the host is not HarmonyOS/OpenHarmony, and false when
    // no QEMU runtime could be verified for any candidate architecture.
    return HarmonyVirtualizationEngine().isHostSupported();
}

VirtualizationEnginePtr makeHarmonyVirtualizationEngine() {
    return VirtualizationEnginePtr(new HarmonyVirtualizationEngine());
}

HarmonyFrontendStatus HarmonyVirtualizationEngine::frontendStatus() const {
    // Hard gate: the in-process core's Display/Input/Audio/Block callback ABI is
    // not connected to a live QEMU frontend in this build (same DeepSeek
    // workstream that owns the safe-init gate). Returning NotSupported — never
    // Ok — is the honest answer; faking it would mislead a caller into believing
    // a framebuffer / disk is live when it is not.
    return HarmonyFrontendStatus::NotSupported;
}

HarmonyFrontendStatus HarmonyVirtualizationEngine::setFrontendCallbacks(
    const HarmonyFrontendCallbacks&) {
    // Intentionally stores nothing. See frontendStatus().
    return HarmonyFrontendStatus::NotSupported;
}

} // namespace vw

// ---------------------------------------------------------------------------
// Registry self-registration
// ---------------------------------------------------------------------------
//
// The shared registry documents this as the way a backend owned by another
// workstream joins the catalogue: a static initialiser in its own translation
// unit, so VirtualizationEngine.hpp and VirtualizationEngineRegistry.hpp never
// have to include this header.
//
// Registration is unconditional (like WindowsHypervisorEngine) because the
// backend is plain Qt code on every platform and reports `host-unsupported`
// honestly elsewhere, which is what lets `--list-backends` document the whole
// matrix. registerBackend() replaces by id, so an explicit registration from
// registerBuiltinBackends() would be harmless rather than duplicative.

#include "VirtualizationEngineRegistry.hpp"

namespace {

const bool vwHarmonyBackendRegistered = []() {
    vw::VirtualizationEngineRegistry::instance().registerBackend(
        vw::makeHarmonyVirtualizationEngine());
    return true;
}();

} // namespace
