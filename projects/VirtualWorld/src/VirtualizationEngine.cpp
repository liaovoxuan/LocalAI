#include "VirtualizationEngine.hpp"

#include "Platform.hpp"
#include "QemuEngine.hpp"
#include "QemuParser.hpp"
#include "SystemSoftwareEngine.hpp"
#include "Translator.hpp"
#include "VmConfig.hpp"
#include "WindowsHypervisorEngine.hpp"

#include <QCoreApplication>
#include <QFileInfo>
#include <QStringList>

// The macOS backend is compiled in only when CMake found both
// src/MacVirtualizationEngine.hpp and src/MacVirtualizationEngine.mm.
#if defined(VIRTUALWORLD_MACOS_BACKEND) && defined(__has_include)
#if __has_include("MacVirtualizationEngine.hpp")
#define VW_HAVE_MACVIRT_BACKEND 1
#include "MacVirtualizationEngine.hpp"
#endif
#endif

// The HarmonyOS/OpenHarmony backend is optional. Nothing below calls its
// factory unless CMake confirmed both of its sources are in the build, so a
// tree without those files links cleanly and simply falls back to QEMU.
#if defined(VIRTUALWORLD_HAVE_HARMONY_BACKEND) && defined(__has_include)
#if __has_include("HarmonyVirtualizationEngine.hpp")
#define VW_HAVE_HARMONY_BACKEND 1
#include "HarmonyVirtualizationEngine.hpp"
#endif
#endif

// The Linux KVM backend is optional in exactly the same way: the header is
// only pulled in when CMake confirmed both of its sources are in the build, so
// a tree without them links cleanly and falls back to QEMU.
#if defined(VIRTUALWORLD_HAVE_LINUX_KVM_BACKEND) && defined(__has_include)
#if __has_include("LinuxKvmEngine.hpp")
#define VW_HAVE_LINUX_KVM_BACKEND 1
#include "LinuxKvmEngine.hpp"
#endif
#endif

namespace vw {

namespace {

QString toQString(const std::string& value) {
    return QString::fromStdString(value);
}

QStringList qemuRuntimeRequirements() {
    return {
        QStringLiteral("QEMU 运行时：runtime/qemu/<platform>/<host-arch>/bin 下的 qemu-system-* 与 qemu-img，"
                       "或系统 PATH 中可用的同名可执行文件。"),
    };
}

QString runtimeMissingError(const QString& guestArch) {
    const QemuEngineInfo info = QemuEngine().detect(guestArch);
    return QStringLiteral("QEMU runtime not found. Expected: %1").arg(info.systemEmulator.expectedPath);
}

bool qemuRuntimeCanRun(const QString& guestArch) {
    return QemuEngine().detect(guestArch).canRunVm();
}

} // namespace

// ---------------------------------------------------------------------------
// VirtualizationEngine defaults
// ---------------------------------------------------------------------------

EngineProbeResult VirtualizationEngine::probeFor(const QString& guestArch) const {
    EngineProbeResult result = probe();
    const bool supported = result.capabilities.hostSupported
        && (guestArch.trimmed().isEmpty() || supportsGuestArch(guestArch));
    result.capabilities.guestArchSupported = supported;
    if (!supported) {
        result.capabilities.canStart = false;
    }
    return result;
}

VirtualizationTier VirtualizationEngine::executionTierFor(const QString& guestArch) const {
    // Default: a backend achieves its mechanism tier only for a guest it can
    // actually run here. Anything it rejects is not accelerated by it at all —
    // the registry hands that guest to TCG — so claiming the mechanism tier
    // there would advertise acceleration that cannot happen.
    //
    // Degradable backends (WHPX, KVM) override this for the cases the capability
    // queries above cannot see, e.g. a supported guest on a host whose
    // hypervisor was switched off.
    if (!isHostSupported()) {
        return VirtualizationTier::SoftwareEmulation;
    }
    const QString arch = guestArch.trimmed();
    if (!arch.isEmpty() && !supportsGuestArch(arch)) {
        return VirtualizationTier::SoftwareEmulation;
    }
    return tier();
}

bool VirtualizationEngine::canRun(const VmConfig& config) const {
    return !hasEngineErrors(validate(config));
}

QString VirtualizationEngine::diagnose(const VmConfig& config) const {
    const EngineProbeResult probed = probeFor(config.architecture);
    QStringList lines;
    lines << QStringLiteral("%1 (%2) — %3")
                 .arg(displayName(), backendId(), tierDescription(tier()));
    lines << QStringLiteral("宿主支持: %1").arg(probed.capabilities.hostSupported ? QStringLiteral("是") : QStringLiteral("否"));
    lines << QStringLiteral("客户机架构 %1: %2")
                 .arg(canonicalGuestArch(config.architecture),
                      probed.capabilities.guestArchSupported ? QStringLiteral("支持") : QStringLiteral("不支持"));
    const QVector<EngineIssue> issues = validate(config);
    for (const EngineIssue& issue : issues) {
        lines << QStringLiteral("%1: %2: %3")
                     .arg(engineIssueLevelName(issue.level), issue.code, issue.message);
    }
    if (probed.summary.isEmpty() && issues.isEmpty()) {
        lines << QStringLiteral("无额外诊断信息。");
    }
    return lines.join('\n');
}

QString VirtualizationEngine::statusCode(const VmConfig& config) const {
    const QVector<EngineIssue> issues = validate(config);
    if (hasEngineErrors(issues)) {
        return QStringLiteral("config-invalid");
    }
    const EngineProbeResult probed = probeFor(config.architecture);
    if (!probed.capabilities.hostSupported) {
        return QStringLiteral("host-unsupported");
    }
    if (!probed.capabilities.guestArchSupported) {
        return QStringLiteral("guest-arch-unsupported");
    }
    return QStringLiteral("available");
}

QStringList VirtualizationEngine::requirements() const {
    return {};
}

// ---------------------------------------------------------------------------
// QEMU hardware-accelerated engine
// ---------------------------------------------------------------------------

QString QemuHardwareEngine::displayName() const {
    return QStringLiteral("QEMU 硬件加速（HFV/WHPX/KVM）");
}

QString QemuHardwareEngine::acceleratorFor(const VmConfig& config, QString* reason) const {
    const HostVirtualizationProfile profile = detectHostVirtualizationProfile();
    const QString guestArch = canonicalGuestArch(config.architecture);
    const QString accelerator = hardwareAcceleratorForHost(profile);

    if (!isSameNativeIsa(profile.hostArch, guestArch)) {
        if (reason) {
            *reason = QStringLiteral("宿主 %1 与客户机 %2 架构不同，硬件加速不可用（不存在跨架构硬件加速）。")
                          .arg(profile.hostArch, guestArch);
        }
        return QString();
    }

    bool available = false;
    QString unavailableReason;
    if (accelerator == QStringLiteral("hvf")) {
        available = profile.hvFSupported;
        unavailableReason = QStringLiteral("宿主未暴露 HVF。");
    } else if (accelerator == QStringLiteral("whpx")) {
        available = whpxAvailable(&unavailableReason);
    } else {
        available = profile.kvmDevicePresent;
        unavailableReason = QStringLiteral("未发现可用的 /dev/kvm。");
    }

    if (!available) {
        if (reason) {
            *reason = unavailableReason.isEmpty()
                          ? QStringLiteral("宿主未提供 %1 硬件加速。").arg(accelerator.toUpper())
                          : unavailableReason;
        }
        return QString();
    }
    return accelerator;
}

bool QemuHardwareEngine::isHostSupported(QString* reason) const {
    const HostVirtualizationProfile profile = detectHostVirtualizationProfile();
    const QString accelerator = hardwareAcceleratorForHost(profile);
    bool available = false;
    QString detail;
    if (accelerator == QStringLiteral("hvf")) {
        available = profile.hvFSupported;
        detail = QStringLiteral("宿主未暴露 HVF。");
    } else if (accelerator == QStringLiteral("whpx")) {
        available = whpxAvailable(&detail);
    } else {
        available = profile.kvmDevicePresent;
        detail = QStringLiteral("未发现可用的 /dev/kvm。");
    }
    if (!available && reason) {
        *reason = detail.isEmpty() ? QStringLiteral("宿主硬件加速不可用。") : detail;
    }
    return available;
}

bool QemuHardwareEngine::supportsGuestArch(const QString& guestArch, QString* reason) const {
    const HostVirtualizationProfile profile = detectHostVirtualizationProfile();
    if (!isSameNativeIsa(profile.hostArch, guestArch)) {
        if (reason) {
            *reason = QStringLiteral("QEMU 硬件加速只对同架构客户机有效：宿主 %1，客户机 %2。")
                          .arg(profile.hostArch, canonicalGuestArch(guestArch));
        }
        return false;
    }
    return isHostSupported(reason);
}

EngineProbeResult QemuHardwareEngine::probe() const {
    const HostVirtualizationProfile profile = detectHostVirtualizationProfile();
    const QString accelerator = hardwareAcceleratorForHost(profile);

    EngineProbeResult result;
    result.backendId = backendId();
    result.displayName = displayName();
    result.tier = tier();

    EngineCapabilities& caps = result.capabilities;
    caps.hostArch = profile.hostArch;
    caps.accelerator = accelerator;
    caps.hardwareAcceleration = true;
    caps.nativeExecution = false;
    caps.hostSupported = isHostSupported(&caps.summary);
    caps.guestArchSupported = caps.hostSupported;
    caps.canStart = caps.hostSupported;
    caps.canStop = caps.hostSupported;

    if (caps.hostSupported) {
        caps.summary = QStringLiteral("QEMU %1 硬件加速可用于宿主 %2 的同架构客户机。")
                           .arg(accelerator.toUpper(), profile.hostArch);
        result.diagnostics << engineIssue(EngineIssueLevel::Info, QStringLiteral("accelerator-available"),
                                          caps.summary);
    } else {
        result.diagnostics << engineIssue(EngineIssueLevel::Warning,
                                          QStringLiteral("accelerator-unavailable"), caps.summary);
    }
    result.diagnostics << engineIssue(
        EngineIssueLevel::Info, QStringLiteral("cross-arch-not-hardware"),
        QStringLiteral("跨架构客户机不使用硬件加速，交给 QEMU TCG。"));
    if (!profile.notes.isEmpty()) {
        for (const QString& note : profile.notes) {
            result.diagnostics << engineIssue(EngineIssueLevel::Info, QStringLiteral("host-note"), note);
        }
    }
    result.hostSupported = caps.hostSupported;
    result.summary = caps.summary;
    return result;
}

EngineProbeResult QemuHardwareEngine::probeFor(const QString& guestArch) const {
    EngineProbeResult result = probe();
    if (!guestArch.trimmed().isEmpty() && !supportsGuestArch(guestArch, &result.summary)) {
        result.capabilities.guestArchSupported = false;
        result.capabilities.canStart = false;
        result.diagnostics << engineIssue(EngineIssueLevel::Warning,
                                          QStringLiteral("guest-arch-unsupported"), result.summary);
    } else {
        result.capabilities.guestArchSupported = result.capabilities.hostSupported;
    }
    return result;
}

QVector<EngineIssue> QemuHardwareEngine::validate(const VmConfig& config) const {
    QVector<EngineIssue> issues;
    if (isSystemSoftwareConfig(config)) {
        issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("backend-mismatch"),
                              QStringLiteral("System Software 配置不使用 QEMU 后端。"));
        return issues;
    }
    QString reason;
    const QString accelerator = acceleratorFor(config, &reason);
    if (accelerator.isEmpty()) {
        issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("accelerator-unavailable"),
                              reason.isEmpty() ? QStringLiteral("宿主硬件加速不可用。") : reason);
        return issues;
    }
    const QString guestArch = canonicalGuestArch(config.architecture);
    if (!qemuRuntimeCanRun(guestArch)) {
        issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("runtime-missing"),
                              runtimeMissingError(guestArch));
        return issues;
    }
    issues << engineIssue(EngineIssueLevel::Info, QStringLiteral("accelerator-selected"),
                          QStringLiteral("将使用 QEMU %1 硬件加速。").arg(accelerator.toUpper()));
    return issues;
}

bool QemuHardwareEngine::start(const VmConfig& config, qint64* processId, QString* error) const {
    QString reason;
    const QString accelerator = acceleratorFor(config, &reason);
    if (accelerator.isEmpty()) {
        if (error) {
            *error = reason;
        }
        return false;
    }
    VmConfig launch = config;
    launch.accelerator = accelerator;
    return QemuEngine().startVm(launch, processId, error);
}

bool QemuHardwareEngine::stop(qint64 processId, QString* error) const {
    return QemuEngine().stopVm(processId, error);
}

QString QemuHardwareEngine::diagnose(const VmConfig& config) const {
    QString reason;
    const QString accelerator = acceleratorFor(config, &reason);
    const QStringList base = VirtualizationEngine::diagnose(config).split('\n');
    QStringList lines;
    lines << QStringLiteral("加速器: %1").arg(accelerator.isEmpty() ? QStringLiteral("不可用") : accelerator);
    if (!reason.isEmpty()) {
        lines << QStringLiteral("原因: %1").arg(reason);
    }
    lines << base;
    return lines.join('\n');
}

QString QemuHardwareEngine::statusCode(const VmConfig& config) const {
    QString reason;
    if (acceleratorFor(config, &reason).isEmpty()) {
        return QStringLiteral("accelerator-unavailable");
    }
    if (!qemuRuntimeCanRun(canonicalGuestArch(config.architecture))) {
        return QStringLiteral("runtime-missing");
    }
    return QStringLiteral("available");
}

QStringList QemuHardwareEngine::requirements() const {
    QStringList requirements = qemuRuntimeRequirements();
    const HostVirtualizationProfile profile = detectHostVirtualizationProfile();
    const QString accelerator = hardwareAcceleratorForHost(profile);
    if (accelerator == QStringLiteral("hvf")) {
        requirements << QStringLiteral("macOS：需要宿主 HVF（kern.hv_support=1）。");
    } else if (accelerator == QStringLiteral("whpx")) {
        requirements << QStringLiteral("Windows：需要启用 Windows Hypervisor Platform（WHPX）"
                                       "且“虚拟机平台”/Hyper-V 相关功能可用。");
    } else {
        requirements << QStringLiteral("Linux：需要 /dev/kvm 可访问（通常要求加入 kvm 组或具备 root 权限）。");
    }
    return requirements;
}

// ---------------------------------------------------------------------------
// QEMU TCG engine
// ---------------------------------------------------------------------------

QString QemuTcgEngine::displayName() const {
    return QStringLiteral("QEMU TCG 软件模拟");
}

bool QemuTcgEngine::isHostSupported(QString* reason) const {
    if (!qemuRuntimeCanRun(QStringLiteral("x86_64")) && !qemuRuntimeCanRun(QStringLiteral("aarch64"))
        && !qemuRuntimeCanRun(QStringLiteral("riscv64"))) {
        if (reason) {
            *reason = QStringLiteral("QEMU 运行时不可用：找不到任何 qemu-system-* 可执行文件。");
        }
        return false;
    }
    return true;
}

bool QemuTcgEngine::supportsGuestArch(const QString& guestArch, QString* reason) const {
    const QString arch = canonicalGuestArch(guestArch);
    if (arch.isEmpty()) {
        if (reason) {
            *reason = QStringLiteral("客户机架构为空。");
        }
        return false;
    }
    if (!qemuRuntimeCanRun(arch)) {
        if (reason) {
            *reason = QStringLiteral("找不到 %1 对应的 QEMU 系统模拟器：%2")
                          .arg(arch, executableForArch(arch));
        }
        return false;
    }
    return true;
}

EngineProbeResult QemuTcgEngine::probe() const {
    const HostVirtualizationProfile profile = detectHostVirtualizationProfile();

    EngineProbeResult result;
    result.backendId = backendId();
    result.displayName = displayName();
    result.tier = tier();

    EngineCapabilities& caps = result.capabilities;
    caps.hostArch = profile.hostArch;
    caps.accelerator = QStringLiteral("tcg");
    caps.hardwareAcceleration = false;
    caps.nativeExecution = false;
    caps.hostSupported = isHostSupported(&caps.summary);
    caps.guestArchSupported = caps.hostSupported;
    caps.canStart = caps.hostSupported;
    caps.canStop = caps.hostSupported;
    caps.summary = caps.hostSupported
        ? QStringLiteral("QEMU TCG 软件模拟可用：任意客户机架构，性能低于硬件加速。")
        : caps.summary;

    result.diagnostics << engineIssue(
        caps.hostSupported ? EngineIssueLevel::Info : EngineIssueLevel::Error,
        QStringLiteral("tcg-fallback"),
        QStringLiteral("TCG 是最终兜底：不依赖宿主虚拟化接口，可运行任意受支持的客户机架构。"));
    result.hostSupported = caps.hostSupported;
    result.summary = caps.summary;
    return result;
}

EngineProbeResult QemuTcgEngine::probeFor(const QString& guestArch) const {
    EngineProbeResult result = probe();
    QString reason;
    const bool ok = guestArch.trimmed().isEmpty() || supportsGuestArch(guestArch, &reason);
    result.capabilities.guestArchSupported = ok && result.capabilities.hostSupported;
    result.capabilities.canStart = result.capabilities.guestArchSupported;
    if (!result.capabilities.guestArchSupported && !reason.isEmpty()) {
        result.summary = reason;
        result.diagnostics << engineIssue(EngineIssueLevel::Error,
                                          QStringLiteral("guest-arch-unsupported"), reason);
    }
    return result;
}

QVector<EngineIssue> QemuTcgEngine::validate(const VmConfig& config) const {
    QVector<EngineIssue> issues;
    if (isSystemSoftwareConfig(config)) {
        issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("backend-mismatch"),
                              QStringLiteral("System Software 配置不使用 QEMU 后端。"));
        return issues;
    }
    const QString guestArch = canonicalGuestArch(config.architecture);
    QString reason;
    if (guestArch.isEmpty()) {
        issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("guest-arch-missing"),
                              QStringLiteral("客户机架构为空，无法选择 QEMU 模拟器。"));
        return issues;
    }
    if (!supportsGuestArch(guestArch, &reason)) {
        issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("runtime-missing"), reason);
        return issues;
    }
    issues << engineIssue(EngineIssueLevel::Info, QStringLiteral("tcg-selected"),
                          QStringLiteral("将使用 QEMU TCG 软件模拟运行 %1 客户机。").arg(guestArch));
    return issues;
}

bool QemuTcgEngine::start(const VmConfig& config, qint64* processId, QString* error) const {
    VmConfig launch = config;
    launch.accelerator = QStringLiteral("tcg");
    return QemuEngine().startVm(launch, processId, error);
}

bool QemuTcgEngine::stop(qint64 processId, QString* error) const {
    return QemuEngine().stopVm(processId, error);
}

QString QemuTcgEngine::diagnose(const VmConfig& config) const {
    QStringList lines;
    lines << QStringLiteral("加速器: tcg（软件模拟，无宿主虚拟化依赖）");
    lines << VirtualizationEngine::diagnose(config).split('\n');
    return lines.join('\n');
}

QString QemuTcgEngine::statusCode(const VmConfig& config) const {
    QString reason;
    if (!supportsGuestArch(config.architecture, &reason)) {
        return QStringLiteral("runtime-missing");
    }
    return QStringLiteral("available");
}

QStringList QemuTcgEngine::requirements() const {
    return qemuRuntimeRequirements();
}

// ---------------------------------------------------------------------------
// macOS factory
// ---------------------------------------------------------------------------
//
// The Windows factory (windowsHypervisorEngineAvailable() /
// makeWindowsHypervisorEngine()) is defined by
// src/WindowsHypervisorEngine.cpp, which is always part of the build. Keeping
// the definition next to the backend avoids a duplicate symbol and lets that
// backend self-register from its own translation unit.

#if defined(VW_HAVE_MACVIRT_BACKEND)

namespace {

class MacVirtualizationEngineAdapter : public VirtualizationEngine {
public:
    QString backendId() const override { return backend_id::macOSVirtualization(); }
    QString displayName() const override { return QStringLiteral("Apple Virtualization.framework"); }
    VirtualizationTier tier() const override { return VirtualizationTier::Native; }

    bool isHostSupported(QString* reason = nullptr) const override {
        // Forward the backend's own reason string verbatim: it is produced by
        // the same policy that decides support, so it can never disagree.
        std::string detail;
        const bool supported = macvirt::MacVirtualizationEngine::isHostSupported(&detail);
        if (!supported && reason) {
            *reason = detail.empty()
                ? QStringLiteral("宿主不支持 Apple Virtualization.framework。")
                : toQString(detail);
        }
        return supported;
    }

    bool supportsGuestArch(const QString& guestArch, QString* reason) const override {
        const std::string arch = canonicalGuestArch(guestArch).toStdString();
        std::string detail;
        const bool supported = macvirt::MacVirtualizationEngine::supportsGuestArch(arch, &detail);
        if (!supported && reason) {
            *reason = detail.empty()
                ? QStringLiteral("Virtualization.framework 只能原生运行与宿主同架构的客户机；"
                                 "跨架构请使用 QEMU TCG。")
                : toQString(detail);
        }
        return supported;
    }

    EngineProbeResult probe() const override {
        const macvirt::ProbeResult probed = macvirt::MacVirtualizationEngine::probe();
        const macvirt::Capabilities caps = probed.capabilities;

        EngineProbeResult result;
        result.backendId = backendId();
        result.displayName = displayName();
        result.tier = tier();
        result.hostSupported = probed.hostSupported;

        EngineCapabilities& out = result.capabilities;
        out.hostArch = toQString(caps.hostArch);
        out.hostSupported = probed.hostSupported;
        out.nativeExecution = true;
        out.hardwareAcceleration = true;
        out.guestArchSupported = caps.nativeArm64Guest || caps.nativeX86_64Guest;
        // Mirrors the backend's own capabilities: macOSGuestIPSW may be
        // "supported" while start() is still not wired, so canStart stays false.
        out.canStart = caps.canStart;
        out.canStop = caps.canStop;
        out.accelerator = QStringLiteral("vz");
        out.summary = toQString(probed.summary);

        for (const macvirt::Issue& issue : probed.diagnostics) {
            EngineIssueLevel level = EngineIssueLevel::Info;
            if (issue.level == macvirt::IssueLevel::Error) {
                level = EngineIssueLevel::Error;
            } else if (issue.level == macvirt::IssueLevel::Warning) {
                level = EngineIssueLevel::Warning;
            }
            out.issues << engineIssue(level, toQString(issue.code), toQString(issue.message));
            result.diagnostics << engineIssue(level, toQString(issue.code), toQString(issue.message));
        }

        if (!caps.macOSGuestIPSW.supported) {
            result.diagnostics << engineIssue(
                EngineIssueLevel::Warning, QStringLiteral("macos-ipsw-unsupported"),
                toQString(caps.macOSGuestIPSW.reason));
        }
        result.summary = out.summary;
        return result;
    }

    QVector<EngineIssue> validate(const VmConfig& config) const override {
        QVector<EngineIssue> issues;
        if (isSystemSoftwareConfig(config)) {
            issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("backend-mismatch"),
                                  QStringLiteral("System Software 配置不使用 Virtualization.framework。"));
            return issues;
        }
        QString reason;
        if (!supportsGuestArch(config.architecture, &reason)) {
            issues << engineIssue(EngineIssueLevel::Error, QStringLiteral("guest-arch-unsupported"), reason);
            return issues;
        }
        const macvirt::Config converted = toMacConfig(config);
        for (const macvirt::Issue& issue : macvirt::MacVirtualizationEngine().validate(converted)) {
            EngineIssueLevel level = EngineIssueLevel::Info;
            if (issue.level == macvirt::IssueLevel::Error) {
                level = EngineIssueLevel::Error;
            } else if (issue.level == macvirt::IssueLevel::Warning) {
                level = EngineIssueLevel::Warning;
            }
            issues << engineIssue(level, toQString(issue.code), toQString(issue.message));
        }
        return issues;
    }

    bool start(const VmConfig& config, qint64* processId, QString* error) const override {
        const macvirt::Config converted = toMacConfig(config);
        const macvirt::RunResult result = macvirt::MacVirtualizationEngine().start(converted);
        if (!result.ok) {
            if (error) {
                *error = result.detail.empty()
                             ? toQString(result.message)
                             : QStringLiteral("%1 (%2)").arg(toQString(result.message), toQString(result.detail));
            }
            return false;
        }
        if (processId) {
            // The guest runs inside this process; report the host process id so
            // `--stop` can still be issued, and so the CLI can warn about the
            // lifetime coupling.
            *processId = QCoreApplication::applicationPid();
        }
        return true;
    }

    bool stop(qint64, QString* error) const override {
        const macvirt::RunResult result = macvirt::MacVirtualizationEngine().stop();
        if (!result.ok) {
            if (error) {
                *error = toQString(result.message);
            }
            return false;
        }
        return true;
    }

    bool runsInProcess() const override { return true; }

    QStringList requirements() const override {
        return {
            QStringLiteral("macOS 11 或更高版本，且宿主暴露 Virtualization.framework。"),
            QStringLiteral("非沙盒进程，或已声明 com.apple.security.virtualization 权限。"),
            QStringLiteral("Linux 客户机需要内核镜像（kernel）与可选的 initrd；macOS IPSW 恢复流程尚未接线。"),
        };
    }

    // Shared conversion used by both validate() and start().
    static macvirt::Config toMacConfig(const VmConfig& config) {
        macvirt::Config converted;
        converted.guestOs = config.guestOs.trimmed().toLower().toStdString();
        converted.architecture = canonicalGuestArch(config.architecture).toStdString();
        converted.kernelPath = config.boot.kernelPath.toStdString();
        converted.initrdPath = config.boot.initrdPath.toStdString();
        converted.kernelCommandLine = config.boot.kernelAppend.toStdString();
        converted.ipswPath = config.macRecovery.ipswPath.toStdString();
        converted.restoreFromIPSW = !config.macRecovery.ipswPath.trimmed().isEmpty();
        converted.cpuCount = config.cpuCores > 0 ? config.cpuCores : 1;
        const qint64 memoryMb = effectiveMemoryKb(config) > 0 ? effectiveMemoryKb(config) / 1024 : config.memoryMb;
        converted.memoryMb = memoryMb > 0 ? static_cast<int>(memoryMb) : 4096;
        for (const DiskConfig& disk : config.disks) {
            converted.diskPaths.push_back(disk.path.toStdString());
        }
        for (const QString& cdrom : config.cdroms) {
            converted.diskPaths.push_back(cdrom.toStdString());
        }
        converted.networkEnabled = !config.networks.isEmpty();
        converted.directorySharingEnabled = !config.sharedDirectories.isEmpty();
        if (!config.sharedDirectories.isEmpty()) {
            converted.sharedDirectoryPath = config.sharedDirectories.first().path.toStdString();
        }
        converted.graphicsEnabled = true;
        converted.audioEnabled = config.audio.enabled;
        return converted;
    }
};

} // namespace

bool macOSVirtualizationEngineAvailable() {
    return true;
}

VirtualizationEnginePtr makeMacOSVirtualizationEngine() {
    return VirtualizationEnginePtr(new MacVirtualizationEngineAdapter());
}

#else // !VW_HAVE_MACVIRT_BACKEND

bool macOSVirtualizationEngineAvailable() {
    return false;
}

VirtualizationEnginePtr makeMacOSVirtualizationEngine() {
    return VirtualizationEnginePtr();
}

#endif // VW_HAVE_MACVIRT_BACKEND

// ---------------------------------------------------------------------------
// HarmonyOS / OpenHarmony
// ---------------------------------------------------------------------------

bool harmonyBackendCompiledIn() {
#if defined(VW_HAVE_HARMONY_BACKEND)
    return true;
#else
    return false;
#endif
}

#if !defined(VW_HAVE_HARMONY_BACKEND)
// Fallback definitions so a build without the Harmony sources still links any
// caller that names the contract. They report "not compiled in" rather than
// pretending the backend exists.
bool harmonyVirtualizationEngineAvailable() {
    return false;
}

VirtualizationEnginePtr makeHarmonyVirtualizationEngine() {
    return VirtualizationEnginePtr();
}
#endif

// ---------------------------------------------------------------------------
// Linux KVM
// ---------------------------------------------------------------------------

bool linuxKvmBackendCompiledIn() {
#if defined(VW_HAVE_LINUX_KVM_BACKEND)
    return true;
#else
    return false;
#endif
}

#if !defined(VW_HAVE_LINUX_KVM_BACKEND)
// Fallback definitions, same contract as the Harmony ones above: a build
// without the Linux KVM sources still links, and honestly reports that the
// backend is not there.
bool linuxKvmEngineAvailable() {
    return false;
}

VirtualizationEnginePtr makeLinuxKvmEngine() {
    return VirtualizationEnginePtr();
}
#endif

} // namespace vw
