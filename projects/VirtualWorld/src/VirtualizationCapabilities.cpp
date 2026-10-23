#include "VirtualizationCapabilities.hpp"

#include <QFileInfo>
#include <QProcess>
#include <QSysInfo>
#include <QtGlobal>

namespace vw {

namespace {

// Arch alias table. Kept explicit and narrow: a native hypervisor exposes the
// host ISA at full width only, so narrower spellings are never folded upward.
QString canonicalize(const QString& value) {
    QString text = value.trimmed().toLower();
    text.replace('-', '_');
    text.replace(' ', '_');
    while (text.contains("__")) {
        text.replace("__", "_");
    }
    if (text.isEmpty()) {
        return QString();
    }

    if (text == "x86_64" || text == "amd64" || text == "x64" || text == "x86_64h"
        || text == "intel64" || text == "em64t") {
        return QStringLiteral("x86_64");
    }
    if (text == "aarch64" || text == "arm64" || text == "arm64e" || text == "armv8"
        || text == "armv8l" || text == "armv8_a" || text == "armv8a" || text == "evbarm"
        || text == "arm_64") {
        return QStringLiteral("aarch64");
    }
    if (text == "riscv64" || text == "riscv64gc" || text == "rv64" || text == "rv64gc"
        || text == "riscv") {
        return QStringLiteral("riscv64");
    }
    if (text == "i386" || text == "i486" || text == "i586" || text == "i686"
        || text == "x86" || text == "ia32" || text == "i86pc") {
        return QStringLiteral("i386");
    }
    if (text == "arm" || text == "armv7" || text == "armv7l" || text == "armhf"
        || text == "armv6" || text == "arm32") {
        return QStringLiteral("arm");
    }
    if (text == "ppc" || text == "powerpc" || text == "ppc32") {
        return QStringLiteral("ppc");
    }
    if (text == "ppc64" || text == "powerpc64" || text == "ppc64le") {
        return QStringLiteral("ppc64");
    }
    if (text == "m68k" || text == "68000" || text == "m68000") {
        return QStringLiteral("m68k");
    }
    if (text == "loongarch64" || text == "loong64" || text == "loongarch" || text == "la64") {
        return QStringLiteral("loongarch64");
    }
    return text;
}

// Host-only spellings that QSysInfo may report.
QString canonicalizeHost(const QString& value) {
    const QString arch = canonicalize(value);
    if (arch.isEmpty()) {
        return arch;
    }
    // Some platforms report the 32-bit userland even on a 64-bit kernel.
    if (arch == "i386" || arch == "arm") {
        const QString product = QSysInfo::currentCpuArchitecture();
        const QString wider = canonicalize(product);
        if (!wider.isEmpty()) {
            return wider;
        }
    }
    return arch;
}

} // namespace

QString canonicalGuestArch(const QString& value) {
    return canonicalize(value);
}

QString canonicalHostArch(const QString& value) {
    return canonicalizeHost(value);
}

bool isSameNativeIsa(const QString& hostArch, const QString& guestArch) {
    const QString host = canonicalGuestArch(hostArch);
    const QString guest = canonicalGuestArch(guestArch);
    if (host.isEmpty() || guest.isEmpty()) {
        return false;
    }
    // A native hypervisor runs the host ISA at full width. 32-bit guests are a
    // QEMU job: they need real instruction translation or a compatibility mode
    // the host hypervisor APIs do not expose.
    return host == guest;
}

QString nativeGuestArchForHost(const QString& hostArch) {
    const QString host = canonicalGuestArch(hostArch);
    if (host == QStringLiteral("x86_64") || host == QStringLiteral("aarch64")) {
        return host;
    }
    // KVM also drives RISC-V hosts, but there is no host hypervisor API exposed
    // through a VirtualWorld backend on that platform today.
    return QString();
}

QString tierName(VirtualizationTier tier) {
    switch (tier) {
    case VirtualizationTier::Native:
        return QStringLiteral("native");
    case VirtualizationTier::HardwareAccelerated:
        return QStringLiteral("hardware");
    case VirtualizationTier::SoftwareEmulation:
    default:
        return QStringLiteral("emulation");
    }
}

QString tierDescription(VirtualizationTier tier) {
    switch (tier) {
    case VirtualizationTier::Native:
        return QStringLiteral("宿主原生虚拟化接口（同架构，1:1）");
    case VirtualizationTier::HardwareAccelerated:
        return QStringLiteral("QEMU + 宿主硬件加速（同架构）");
    case VirtualizationTier::SoftwareEmulation:
    default:
        return QStringLiteral("QEMU TCG 软件模拟（任意架构，最后兜底）");
    }
}

int tierRank(VirtualizationTier tier) {
    return static_cast<int>(tier);
}

bool tierIsNative(VirtualizationTier tier) {
    return tier == VirtualizationTier::Native;
}

bool statusIsUsable(const QString& statusCode) {
    return statusCode == backend_status::available();
}

QString statusClass(const QString& statusCode) {
    const QString code = statusCode.trimmed();
    if (code.isEmpty()) {
        return backend_status::notRegistered();
    }

    // Canonical and extended codes that are already their own class.
    if (code == backend_status::available()
        || code == backend_status::degraded()
        || code == backend_status::whpxDegraded()) {
        // "degraded" still means the backend can run the guest, just without
        // acceleration. The caller decides whether that is good enough; the
        // class only says nothing is blocking it.
        return backend_status::available();
    }
    if (code == backend_status::hostUnsupported()
        || code == backend_status::notRegistered()
        || code == backend_status::noBackend()) {
        return backend_status::hostUnsupported();
    }
    if (code == backend_status::guestArchUnsupported()) {
        return backend_status::guestArchUnsupported();
    }
    if (code == backend_status::runtimeMissing()
        || code == backend_status::runtimeNotRunnable()) {
        return backend_status::runtimeMissing();
    }
    if (code == backend_status::acceleratorUnavailable()
        || code == backend_status::whpxApiMissing()
        || code == backend_status::whpxHypervisorNotPresent()) {
        return backend_status::acceleratorUnavailable();
    }
    if (code == backend_status::backendMismatch()) {
        return backend_status::backendMismatch();
    }
    if (code == backend_status::excludedByPolicy()) {
        return backend_status::excludedByPolicy();
    }
    if (code == backend_status::configInvalid()) {
        return backend_status::configInvalid();
    }

    // Anything left is a backend-specific code (typically a validation issue
    // code such as `memory-too-small`). It can never be trusted to mean "this
    // backend can run the guest", so it is classified as a rejected config.
    return backend_status::configInvalid();
}

QString engineIssueLevelName(EngineIssueLevel level) {
    switch (level) {
    case EngineIssueLevel::Error:
        return QStringLiteral("error");
    case EngineIssueLevel::Warning:
        return QStringLiteral("warning");
    case EngineIssueLevel::Info:
    default:
        return QStringLiteral("info");
    }
}

EngineIssue engineIssue(EngineIssueLevel level, const QString& code, const QString& message) {
    EngineIssue issue;
    issue.level = level;
    issue.code = code;
    issue.message = message;
    return issue;
}

bool hasEngineErrors(const QVector<EngineIssue>& issues) {
    for (const EngineIssue& issue : issues) {
        if (issue.level == EngineIssueLevel::Error) {
            return true;
        }
    }
    return false;
}

bool hasEngineCode(const QVector<EngineIssue>& issues, const QString& code) {
    for (const EngineIssue& issue : issues) {
        if (issue.code == code) {
            return true;
        }
    }
    return false;
}

QString joinEngineIssues(const QVector<EngineIssue>& issues) {
    QStringList parts;
    for (const EngineIssue& issue : issues) {
        if (issue.level == EngineIssueLevel::Info) {
            continue;
        }
        parts << QStringLiteral("%1:%2:%3")
                     .arg(engineIssueLevelName(issue.level), issue.code, issue.message);
    }
    return parts.join(QStringLiteral(" | "));
}

// ---------------------------------------------------------------------------
// Host probe
// ---------------------------------------------------------------------------

namespace {

// Cached because the probe shells out and is called from several places.
int cachedHvfSupport() {
    static int cached = -2;  // -2 unknown, -1 query failed, 0/1 sysctl answer
    if (cached != -2) {
        return cached;
    }
#if defined(Q_OS_MACOS)
    QProcess process;
    process.start(QStringLiteral("sysctl"), {QStringLiteral("-n"), QStringLiteral("kern.hv_support")});
    if (process.waitForFinished(1500) && process.exitStatus() == QProcess::NormalExit
        && process.exitCode() == 0) {
        const QString output = QString::fromUtf8(process.readAllStandardOutput()).trimmed();
        cached = output == QStringLiteral("1") ? 1 : 0;
        return cached;
    }
    cached = -1;
#else
    cached = -1;
#endif
    return cached;
}

bool kvmDeviceUsable() {
#if defined(Q_OS_WIN)
    return false;
#else
    const QFileInfo kvm(QStringLiteral("/dev/kvm"));
    return kvm.exists();
#endif
}

} // namespace

HostVirtualizationProfile detectHostVirtualizationProfile() {
    HostVirtualizationProfile profile;
    profile.host = detectHostProfile();
    profile.hostArch = canonicalHostArch(profile.host.arch);
    if (profile.hostArch.isEmpty()) {
        profile.hostArch = QStringLiteral("unknown");
    }

    const QString family = profile.host.family;
    const bool linuxFamily = profile.host.platform == HostPlatform::Linux;

    profile.kvmDevicePresent = linuxFamily && kvmDeviceUsable();

    switch (profile.host.platform) {
    case HostPlatform::MacOS: {
        const int hvf = cachedHvfSupport();
        profile.hvFSupported = !nativeGuestArchForHost(profile.hostArch).isEmpty() && hvf != 0;
        profile.hypervisorApiPresent = profile.hvFSupported;
        if (hvf == 1) {
            profile.detail = QStringLiteral("macOS HVF 可用（kern.hv_support=1）。");
        } else if (hvf == 0) {
            profile.detail = QStringLiteral("macOS HVF 不可用（kern.hv_support=0）。");
            profile.notes << QStringLiteral("宿主未暴露 HVF，所有客户机走 QEMU TCG。");
        } else {
            profile.detail = QStringLiteral("无法确认 kern.hv_support，按宿主架构假定 HVF 可用。");
            profile.notes << QStringLiteral("sysctl kern.hv_support 查询失败，HVF 能力为假定值。");
        }
        break;
    }
    case HostPlatform::Windows: {
        // The authoritative WHPX probe lives in WindowsHypervisorEngine, which
        // is the only place allowed to include the Windows SDK. This generic
        // profile only states the architectural pre-condition.
        profile.whpxSupported = !nativeGuestArchForHost(profile.hostArch).isEmpty();
        profile.hypervisorApiPresent = profile.whpxSupported;
        profile.detail = QStringLiteral("Windows 宿主架构满足 WHPX 前置条件；实际可用性由 WindowsHypervisorEngine 探测。");
        break;
    }
    case HostPlatform::Linux:
    default: {
        if (profile.kvmDevicePresent) {
            profile.hypervisorApiPresent = true;
            profile.detail = QStringLiteral("/dev/kvm 存在，KVM 可用于同架构客户机。");
        } else {
            profile.detail = QStringLiteral("未发现 /dev/kvm，KVM 不可用。");
            profile.notes << QStringLiteral("KVM 不可用：所有客户机走 QEMU TCG。");
        }
        if (family == QStringLiteral("harmonyos-linux-compatible")
            || family == QStringLiteral("openharmony-linux-compatible")) {
            profile.notes << QStringLiteral(
                "HarmonyOS/OpenHarmony 以 Linux 兼容模式运行；KVM 是否暴露取决于内核与容器策略。");
        }
        break;
    }
    }

    return profile;
}

QString hardwareAcceleratorForHost(const HostProfile& host) {
    switch (host.platform) {
    case HostPlatform::MacOS:
        return QStringLiteral("hvf");
    case HostPlatform::Windows:
        return QStringLiteral("whpx");
    case HostPlatform::Linux:
    default:
        return QStringLiteral("kvm");
    }
}

QString hardwareAcceleratorForHost(const HostVirtualizationProfile& profile) {
    return hardwareAcceleratorForHost(profile.host);
}

// ---------------------------------------------------------------------------
// Support matrix
// ---------------------------------------------------------------------------

namespace {

struct PlatformHost {
    QString platform;
    QString hostArch;
    QString nativeBackend;
    bool verified = false;
    QString note;
};

void appendRow(QVector<SupportMatrixRow>& rows,
               const QString& hostPlatform,
               const QString& hostArch,
               const QString& guestArch,
               const QString& backendId,
               VirtualizationTier tier,
               const QString& status,
               bool verified,
               const QString& note) {
    SupportMatrixRow row;
    row.hostPlatform = hostPlatform;
    row.hostArch = hostArch;
    row.guestArch = guestArch;
    row.backendId = backendId;
    row.tier = tier;
    row.status = status;
    row.verified = verified;
    row.note = note;
    rows.append(row);
}

const QVector<PlatformHost>& platformHosts() {
    static const QVector<PlatformHost> hosts = {
        {QStringLiteral("windows"), QStringLiteral("x86_64"), backend_id::windowsHypervisor(), false,
         QStringLiteral("Windows x86_64 宿主：WHPX 原生路径，未在本机验证。")},
        {QStringLiteral("windows"), QStringLiteral("aarch64"), backend_id::windowsHypervisor(), false,
         QStringLiteral("Windows ARM64 宿主：WHPX 原生路径，未在本机验证。")},
        {QStringLiteral("macos"), QStringLiteral("x86_64"), backend_id::macOSVirtualization(), false,
         QStringLiteral("Intel Mac：Virtualization.framework 原生路径，未在本机验证。")},
        {QStringLiteral("macos"), QStringLiteral("aarch64"), backend_id::macOSVirtualization(), false,
         QStringLiteral("Apple Silicon：Virtualization.framework 原生路径，已编译未实机验证。")},
        {QStringLiteral("linux"), QStringLiteral("x86_64"), backend_id::linuxKvm(), false,
         QStringLiteral("Linux x86_64：QEMU + KVM 兼容路径（非原生），未在本机验证。")},
        {QStringLiteral("linux"), QStringLiteral("aarch64"), backend_id::linuxKvm(), false,
         QStringLiteral("Linux ARM64：QEMU + KVM 兼容路径（非原生），未在本机验证。")},
        {QStringLiteral("linux"), QStringLiteral("riscv64"), backend_id::linuxKvm(), false,
         QStringLiteral("Linux RISC-V64：QEMU + KVM 兼容路径（非原生），未在本机验证。")},
        {QStringLiteral("harmonyos"), QStringLiteral("x86_64"), backend_id::harmonyVirtualization(), false,
         QStringLiteral("HarmonyOS/OpenHarmony：Linux 兼容回退，未在本机验证。")},
        {QStringLiteral("harmonyos"), QStringLiteral("aarch64"), backend_id::harmonyVirtualization(), false,
         QStringLiteral("HarmonyOS/OpenHarmony：Linux 兼容回退，未在本机验证。")},
        {QStringLiteral("harmonyos"), QStringLiteral("riscv64"), backend_id::harmonyVirtualization(), false,
         QStringLiteral("HarmonyOS/OpenHarmony：Linux 兼容回退，未在本机验证。")},
    };
    return hosts;
}

const QStringList& matrixGuestArches() {
    static const QStringList arches = {
        QStringLiteral("x86_64"),
        QStringLiteral("aarch64"),
        QStringLiteral("riscv64"),
    };
    return arches;
}

} // namespace

QVector<SupportMatrixRow> defaultSupportMatrix() {
    QVector<SupportMatrixRow> rows;
    for (const PlatformHost& host : platformHosts()) {
        // Backends that drive QEMU instead of a host hypervisor API. They can
        // be accelerated by KVM, but a QEMU process accelerated by KVM is not
        // native execution, so they are documented as HardwareAccelerated and
        // never as Native. Both backends say the same in their own headers.
        const bool qemuCompat = host.platform == QStringLiteral("harmonyos")
            || host.platform == QStringLiteral("linux");
        for (const QString& guestArch : matrixGuestArches()) {
            const bool sameIsa = isSameNativeIsa(host.hostArch, guestArch);
            if (sameIsa) {
                if (qemuCompat) {
                    // HarmonyOS/OpenHarmony exposes no public hypervisor API;
                    // LinuxKvmEngine is a QEMU compatibility backend. Both
                    // report HardwareAccelerated when the kernel exposes a
                    // usable /dev/kvm and SoftwareEmulation otherwise. Claiming
                    // a host hypervisor API would be dishonest.
                    const QString note = host.platform == QStringLiteral("harmonyos")
                        ? QStringLiteral("同架构：QEMU + KVM 兼容路径（非原生），"
                                         "取决于 /dev/kvm 是否暴露；不可用则回退 TCG。")
                        : QStringLiteral("同架构：QEMU + KVM 兼容路径（非原生，LinuxKvmEngine 直述 never native），"
                                         "取决于 /dev/kvm 是否暴露；不可用则回退 TCG。");
                    appendRow(rows, host.platform, host.hostArch, guestArch,
                              host.nativeBackend, VirtualizationTier::HardwareAccelerated,
                              QStringLiteral("compat"), false, note);
                    appendRow(rows, host.platform, host.hostArch, guestArch,
                              backend_id::qemuTcg(), VirtualizationTier::SoftwareEmulation,
                              QStringLiteral("emulation"), false,
                              QStringLiteral("最终兜底：QEMU TCG 始终可用。"));
                } else {
                    appendRow(rows, host.platform, host.hostArch, guestArch,
                              host.nativeBackend, VirtualizationTier::Native,
                              QStringLiteral("native"), host.verified, host.note);
                    appendRow(rows, host.platform, host.hostArch, guestArch,
                              backend_id::qemuHardware(), VirtualizationTier::HardwareAccelerated,
                              QStringLiteral("hardware"), host.verified,
                              QStringLiteral("原生后端不可用或配置校验失败时的硬件加速回退。"));
                    // TCG is always the last resort for same-ISA guests too:
                    // the accelerator may still be unavailable at runtime.
                    appendRow(rows, host.platform, host.hostArch, guestArch,
                              backend_id::qemuTcg(), VirtualizationTier::SoftwareEmulation,
                              QStringLiteral("emulation"), false,
                              QStringLiteral("最终兜底：QEMU TCG 始终可用。"));
                }
            } else {
                appendRow(rows, host.platform, host.hostArch, guestArch,
                          backend_id::qemuTcg(), VirtualizationTier::SoftwareEmulation,
                          QStringLiteral("emulation"), false,
                          QStringLiteral("跨架构：只能软件模拟，不存在跨架构硬件加速。"));
            }
        }
    }
    return rows;
}

} // namespace vw
