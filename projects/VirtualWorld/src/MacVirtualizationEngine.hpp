#pragma once

// MacVirtualizationEngine — macOS native virtualization backend for VirtualWorld.
//
// This backend is a thin, honest adapter around Apple's Virtualization.framework.
// It is intentionally split into two layers:
//
//   1. A pure C++17 "policy" layer (everything defined inline in this header).
//      It never touches Objective-C or the framework and is fully deterministic,
//      so it can be unit-tested on any host and reused by an upper-layer backend
//      registry.
//   2. A platform layer implemented in MacVirtualizationEngine.mm, which is
//      compiled only on macOS (Objective-C++). It performs the real host probe,
//      entitlement diagnostics, framework validation and VM lifecycle.
//
// Design constraints (see docs/macos-virtualization.md):
//   * Apple Silicon hosts can only 1:1 virtualize arm64 guests.
//   * Intel hosts can only 1:1 virtualize x86_64 guests.
//   * Cross-architecture guests are NOT handled here; the caller must fall back
//     to QEMU TCG. This backend never claims cross-arch support.
//   * Linux guests use VZLinuxBootLoader. macOS guests require an IPSW restore
//     (VZMacOSRestoreImage / VZMacOSInstaller) and are Apple-Silicon-only.
//     The restore/install flow is not fully wired yet, so `start()` returns an
//     explicit `unsupported` result instead of pretending to work.
//
// Integration with the shared contract
// -----------------------------------
// This header is deliberately Qt-free (no QString/QVector/Platform.hpp/VmConfig),
// so it can be unit-tested with a plain compiler and so the platform code stays
// independent of the GUI. The Qt-facing `vw::VirtualizationEngine` subclass and
// the `makeMacOSVirtualizationEngine()` / `macOSVirtualizationEngineAvailable()`
// factories declared in src/VirtualizationEngine.hpp belong to the registry /
// integration workstream; this backend only supplies the surface they need.
//
// The mapping is 1:1:
//
//   vw::VirtualizationEngine          this backend
//   -------------------------------   -----------------------------------------
//   backendId()                       backendId()  ("macos-virtualization")
//   displayName()                     backendName()
//   tier()                            VirtualizationTier::Native (backendTierName())
//   isHostSupported(reason)           isHostSupported(&reason)
//   supportsGuestArch(arch, reason)   supportsGuestArch(arch, &reason)
//   probe()                           probe() -> ProbeResult
//   validate(VmConfig)                validate(Config) -> std::vector<Issue>
//   start(VmConfig, pid, err)         start(Config) -> RunResult
//   stop(pid, err)                    stop() -> RunResult
//   diagnose() / statusCode()         RunResult.code + Issue.code (stable strings)
//   requirements()                    Issue list + Host (entitlement/OS version)
//
// `backendId()` matches vw::backend_id::macOSVirtualization(), and Issue.code
// values are stable identifiers intended to be forwarded as EngineIssue.code.

#include <atomic>
#include <cctype>
#include <cstdint>
#include <functional>
#include <memory>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

#if defined(__APPLE__)
#include <TargetConditionals.h>
#endif

#if defined(__APPLE__) && defined(TARGET_OS_OSX) && TARGET_OS_OSX \
    && defined(__has_include)
#if __has_include(<Virtualization/Virtualization.h>)
#define VW_MACVIRT_HAS_FRAMEWORK 1
#else
#define VW_MACVIRT_HAS_FRAMEWORK 0
#endif
#else
#define VW_MACVIRT_HAS_FRAMEWORK 0
#endif

namespace vw {
namespace macvirt {

// ---------------------------------------------------------------------------
// Identity
// ---------------------------------------------------------------------------

inline std::string backendId() {
    return "macos-virtualization";
}

inline std::string backendName() {
    return "Apple Virtualization.framework";
}

// ---------------------------------------------------------------------------
// Small string helpers (kept dependency-free on purpose: no Qt here)
// ---------------------------------------------------------------------------

inline std::string toLowerAscii(std::string value) {
    for (char& ch : value) {
        ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    }
    return value;
}

inline std::string trimAscii(const std::string& value) {
    const std::string whitespace = " \t\r\n";
    const std::size_t first = value.find_first_not_of(whitespace);
    if (first == std::string::npos) {
        return {};
    }
    const std::size_t last = value.find_last_not_of(whitespace);
    return value.substr(first, last - first + 1);
}

// ---------------------------------------------------------------------------
// Diagnostics
// ---------------------------------------------------------------------------

enum class IssueLevel { Info, Warning, Error };

struct Issue {
    IssueLevel level = IssueLevel::Info;
    std::string code;
    std::string message;
};

inline const char* issueLevelName(IssueLevel level) {
    switch (level) {
    case IssueLevel::Error:
        return "error";
    case IssueLevel::Warning:
        return "warning";
    case IssueLevel::Info:
    default:
        return "info";
    }
}

inline bool hasErrors(const std::vector<Issue>& issues) {
    for (const Issue& issue : issues) {
        if (issue.level == IssueLevel::Error) {
            return true;
        }
    }
    return false;
}

inline bool hasCode(const std::vector<Issue>& issues, const std::string& code) {
    for (const Issue& issue : issues) {
        if (issue.code == code) {
            return true;
        }
    }
    return false;
}

inline std::string joinIssueDetails(const std::vector<Issue>& issues) {
    std::ostringstream stream;
    bool first = true;
    for (const Issue& issue : issues) {
        if (issue.level == IssueLevel::Info) {
            continue;
        }
        if (!first) {
            stream << " | ";
        }
        first = false;
        stream << issueLevelName(issue.level) << ':' << issue.code << ':' << issue.message;
    }
    return stream.str();
}

// ---------------------------------------------------------------------------
// Host descriptor
// ---------------------------------------------------------------------------

struct Host {
    std::string arch = "unknown";  // normalized: "aarch64" / "x86_64" / other
    std::string osVersion = "unknown";
    int macOSMajor = 0;
    int macOSMinor = 0;
    bool macOS = false;
    bool appleSilicon = false;
    bool virtualizingNested = false;
    bool virtualizationFrameworkAvailable = false;
    bool sandboxed = false;
    bool hasVirtualizationEntitlement = false;
    std::string frameworkVersion;
};

// ---------------------------------------------------------------------------
// Guest architecture policy
// ---------------------------------------------------------------------------

// Normalizes user/config spellings to the canonical architecture names used by
// the rest of VirtualWorld (see vw::normalizeArchitecture): aarch64 / x86_64.
// Note that i386 / arm are intentionally NOT folded into x86_64 / aarch64: the
// Virtualization framework only exposes the host ISA at full width, so those
// guests must use QEMU.
inline std::string normalizeArch(const std::string& value) {
    const std::string arch = toLowerAscii(trimAscii(value));
    if (arch.empty()) {
        return {};
    }
    if (arch == "aarch64" || arch == "arm64" || arch == "arm64e" || arch == "armv8"
        || arch == "armv8l" || arch == "armv8-a") {
        return "aarch64";
    }
    if (arch == "x86_64" || arch == "amd64" || arch == "x64") {
        return "x86_64";
    }
    if (arch == "i386" || arch == "i486" || arch == "i586" || arch == "i686"
        || arch == "x86" || arch == "ia32") {
        return "i386";
    }
    if (arch == "arm" || arch == "armv7" || arch == "armv7l" || arch == "armhf") {
        return "arm";
    }
    if (arch == "ppc" || arch == "ppc64" || arch == "ppc64le") {
        return "ppc";
    }
    return arch;
}

inline bool isAppleSiliconHost(const Host& host) {
    return host.appleSilicon || normalizeArch(host.arch) == "aarch64";
}

// The one guest architecture this host can virtualize natively (1:1). Empty when
// the host ISA is not one the Virtualization framework can boot.
inline std::string nativeGuestArch(const Host& host) {
    const std::string arch = normalizeArch(host.arch);
    if (arch == "aarch64" || arch == "x86_64") {
        return arch;
    }
    return {};
}

// True when this process is allowed to create a VM at all. A sandboxed process
// must declare com.apple.security.virtualization; without it, every
// VZVirtualMachineConfiguration validation fails, so the backend must not
// advertise support (the shared contract requires validate() to reject rather
// than let the registry pick a backend that cannot start).
inline bool hostMayCreateVirtualMachine(const Host& host) {
    if (!host.macOS || host.macOSMajor < 11 || !host.virtualizationFrameworkAvailable) {
        return false;
    }
    if (host.sandboxed && !host.hasVirtualizationEntitlement) {
        return false;
    }
    return true;
}

inline bool hostSupportsGuestArch(const std::string& guestArch, const Host& host) {
    if (!hostMayCreateVirtualMachine(host)) {
        return false;
    }
    const std::string native = nativeGuestArch(host);
    if (native.empty()) {
        return false;
    }
    return normalizeArch(guestArch) == native;
}

// ---------------------------------------------------------------------------
// Capabilities
// ---------------------------------------------------------------------------

struct Capability {
    bool supported = false;
    std::string reason;  // human readable, safe to surface in the GUI
    std::string code;    // stable machine code for the upper layer
};

struct Capabilities {
    bool frameworkAvailable = false;
    Capability linuxGuest;       // native Linux boot (VZLinuxBootLoader)
    Capability macOSGuestIPSW;   // restore/install a macOS guest from IPSW
    Capability macOSGuestBoot;   // boot an already-installed macOS guest
    Capability networking;       // NAT virtio-net
    Capability directorySharing; // virtio-fs / directory shares
    Capability graphics;         // virtio-gpu / VM view
    Capability audio;            // virtio-sound
    bool nativeArm64Guest = false;
    bool nativeX86_64Guest = false;
    std::string hostArch;
    std::string frameworkVersion;

    // Does start() actually work, and can the resulting VM be stopped? These
    // mirror EngineCapabilities::canStart / canStop in the shared contract and
    // exist to keep the probe honest: macOSGuestIPSW can be `supported` (the host
    // and framework could do it) while `canStart` is still false, because the
    // restore/install pipeline is not wired up yet.
    bool canStart = false;
    bool canStop = false;
};

// The tier name this backend maps to in the shared contract
// (tierName(VirtualizationTier::Native)).
inline const char* backendTierName() {
    return "native";
}

inline Capability capability(bool supported, std::string reason, std::string code) {
    Capability result;
    result.supported = supported;
    result.reason = std::move(reason);
    result.code = std::move(code);
    return result;
}

// Pure capability evaluation. Every unsupported capability carries a non-empty
// reason and code so the caller can explain the situation and fall back.
inline Capabilities evaluateCapabilities(const Host& host) {
    Capabilities caps;
    caps.hostArch = normalizeArch(host.arch);
    caps.frameworkVersion = host.frameworkVersion;

    const bool osSupported = host.macOS && host.macOSMajor >= 11;
    const bool frameworkReady = osSupported && host.virtualizationFrameworkAvailable;
    const bool entitled = !host.sandboxed || host.hasVirtualizationEntitlement;
    const bool apple = isAppleSiliconHost(host);
    const std::string native = nativeGuestArch(host);

    caps.frameworkAvailable = frameworkReady;
    caps.nativeArm64Guest = apple && native == "aarch64";
    caps.nativeX86_64Guest = !apple && native == "x86_64";

    // --- Linux guest ---------------------------------------------------------
    // Every rejection ends with a concrete fallback instruction: the shared
    // registry can only fall back if the reason tells it to.
    if (!host.macOS) {
        caps.linuxGuest = capability(false,
                                     "Apple Virtualization.framework 仅在 macOS 宿主上可用；"
                                     "请使用 QEMU 后端。",
                                     "host-not-macos");
    } else if (host.macOSMajor < 11) {
        caps.linuxGuest = capability(false,
                                     "Virtualization.framework 需要 macOS 11 (Big Sur) 或更高版本；"
                                     "当前系统请使用 QEMU 后端。",
                                     "macos-11-required");
    } else if (!host.virtualizationFrameworkAvailable) {
        caps.linuxGuest = capability(false,
                                     "当前进程无法加载 Virtualization.framework；请回退到 QEMU 后端。",
                                     "framework-unavailable");
    } else if (!entitled) {
        caps.linuxGuest = capability(false,
                                     "进程已沙盒化但缺少 com.apple.security.virtualization 权限，"
                                     "无法创建虚拟机；请补齐签名权限或回退到 QEMU 后端。",
                                     "sandbox-entitlement-missing");
    } else if (native.empty()) {
        caps.linuxGuest = capability(false,
                                     "宿主 CPU 架构 " + caps.hostArch
                                         + " 不受 Virtualization.framework 原生引导支持；"
                                           "请回退到 QEMU。",
                                     "host-arch-unsupported");
    } else {
        caps.linuxGuest = capability(
            true,
            "可通过 VZLinuxBootLoader 原生引导 Linux " + native + " 客户机。",
            "linux-native-boot");
    }

    // --- macOS guest (IPSW restore / install) --------------------------------
    if (!host.macOS || host.macOSMajor < 12) {
        caps.macOSGuestIPSW = capability(false,
                                         "macOS 客户机恢复需要 macOS 12 (Monterey) 或更高版本；"
                                         "请使用 QEMU 后端。",
                                         "macos-12-required");
    } else if (!host.virtualizationFrameworkAvailable) {
        caps.macOSGuestIPSW = capability(false,
                                         "当前进程无法加载 Virtualization.framework；"
                                         "请回退到 QEMU 后端。",
                                         "framework-unavailable");
    } else if (!entitled) {
        caps.macOSGuestIPSW = capability(false,
                                         "进程已沙盒化但缺少 com.apple.security.virtualization 权限，"
                                         "无法创建虚拟机。",
                                         "sandbox-entitlement-missing");
    } else if (!apple) {
        caps.macOSGuestIPSW = capability(
            false,
            "macOS 客户机恢复仅在 Apple Silicon 宿主上受支持；Intel Mac 请回退到 QEMU。",
            "macos-guest-apple-silicon-only");
    } else {
        caps.macOSGuestIPSW = capability(
            true,
            "可探测 VZMacOSRestoreImage / VZMacOSInstaller；本后端尚未接线安装流程，"
            "start() 会返回明确的 unsupported 而不是伪装成功。",
            "macos-ipsw-probe-only");
    }
    // Booting an installed macOS guest is gated identically: it also needs the
    // VZMacPlatformConfiguration / auxiliary storage plumbing.
    caps.macOSGuestBoot = caps.macOSGuestIPSW;

    // --- Optional devices ----------------------------------------------------
    // These describe what the framework can do, so they follow framework
    // availability; the entitlement gate only decides whether a VM may be
    // created at all (and therefore shows up on linuxGuest / canStart).
    caps.networking = frameworkReady
        ? capability(true, "支持 VZNATNetworkDeviceAttachment 网络。", "networking-supported")
        : capability(false, "网络设备需要可用的 Virtualization.framework。", "framework-unavailable");

    if (frameworkReady && host.macOSMajor >= 12) {
        caps.directorySharing = capability(true,
                                           "支持 VZVirtioFileSystemDeviceConfiguration 目录共享。",
                                           "directory-sharing-supported");
        caps.graphics = capability(true,
                                   "支持 VZVirtualMachineView / virtio 图形设备。",
                                   "graphics-supported");
        caps.audio = capability(true,
                                "支持 virtio-sound 音频设备。",
                                "audio-supported");
    } else if (frameworkReady) {
        caps.directorySharing = capability(false,
                                           "目录共享需要 macOS 12 或更高版本。",
                                           "macos-12-required");
        caps.graphics = capability(false,
                                   "图形设备配置需要 macOS 12 或更高版本。",
                                   "macos-12-required");
        caps.audio = capability(false,
                                "音频设备配置需要 macOS 12 或更高版本。",
                                "macos-12-required");
    } else {
        caps.directorySharing = capability(false,
                                           "目录共享需要可用的 Virtualization.framework。",
                                           "framework-unavailable");
        caps.graphics = capability(false,
                                   "图形输出需要可用的 Virtualization.framework。",
                                   "framework-unavailable");
        caps.audio = capability(false,
                                "音频输出需要可用的 Virtualization.framework。",
                                "framework-unavailable");
    }

    // Honest end-to-end answer: the only lifecycle this backend can actually
    // drive today is native Linux boot. The macOS restore/install path is probed
    // but not implemented, so it must not make canStart true.
    caps.canStart = caps.linuxGuest.supported;
    caps.canStop = caps.linuxGuest.supported;

    return caps;
}

struct ProbeResult {
    bool hostSupported = false;
    Host host;
    Capabilities capabilities;
    std::vector<Issue> diagnostics;
    std::string summary;
};

// OS / framework / entitlement diagnostics. Pure: derived only from the
// descriptor, so tests can feed synthetic hosts.
inline std::vector<Issue> diagnoseHost(const Host& host) {
    std::vector<Issue> issues;

    if (!host.macOS) {
        issues.push_back({IssueLevel::Error,
                          "host-not-macos",
                          "Apple Virtualization.framework 后端仅在 macOS 上运行。"});
        return issues;
    }
    if (host.macOSMajor < 11) {
        issues.push_back({IssueLevel::Error,
                          "macos-11-required",
                          "需要 macOS 11 Big Sur 或更高版本才能使用 Virtualization.framework。"});
    } else if (!host.virtualizationFrameworkAvailable) {
        issues.push_back({IssueLevel::Error,
                          "framework-unavailable",
                          "macOS 版本满足要求，但 Virtualization.framework 不可用（可能被系统策略禁用）。"});
    } else {
        std::string version = host.frameworkVersion.empty() ? std::string("unknown") : host.frameworkVersion;
        issues.push_back({IssueLevel::Info,
                          "framework-available",
                          "Virtualization.framework 可用（系统 " + host.osVersion
                              + "，框架版本 " + version + "）。"});
    }

    if (host.sandboxed) {
        if (host.hasVirtualizationEntitlement) {
            issues.push_back({IssueLevel::Info,
                              "sandbox-entitlement-present",
                              "进程已沙盒化并声明 com.apple.security.virtualization。"});
        } else {
            issues.push_back({IssueLevel::Error,
                              "sandbox-entitlement-missing",
                              "进程已沙盒化但缺少 com.apple.security.virtualization 权限，"
                              "无法创建虚拟机。"});
        }
    } else {
        issues.push_back({IssueLevel::Info,
                          "not-sandboxed",
                          "进程未沙盒化，创建虚拟机不需要 virtualization 权限。"});
    }

    const std::string native = nativeGuestArch(host);
    if (native.empty()) {
        issues.push_back({IssueLevel::Error,
                          "host-arch-unsupported",
                          "宿主架构 " + normalizeArch(host.arch)
                              + " 不受 Virtualization.framework 原生引导支持；请回退到 QEMU。"});
    } else {
        issues.push_back({IssueLevel::Info,
                          "native-guest-arch",
                          "宿主 " + native + " 仅原生支持同架构客户机。"});
    }

    if (isAppleSiliconHost(host)) {
        issues.push_back({IssueLevel::Info,
                          "cross-arch-qemu-tcg",
                          "Apple Silicon 无法通过 Virtualization.framework 运行 x86_64 客户机，"
                          "跨架构请回退到 QEMU TCG。"});
    } else if (!native.empty()) {
        issues.push_back({IssueLevel::Info,
                          "cross-arch-qemu-tcg",
                          "Intel Mac 无法通过 Virtualization.framework 运行 arm64 客户机，"
                          "跨架构请回退到 QEMU TCG。"});
    }

    if (host.virtualizingNested) {
        issues.push_back({IssueLevel::Warning,
                          "nested-virtualization",
                          "检测到嵌套虚拟化环境，Virtualization.framework 可能不可用。"});
    }

    return issues;
}

inline ProbeResult probeHost(const Host& host) {
    ProbeResult result;
    result.host = host;
    result.capabilities = evaluateCapabilities(host);
    result.diagnostics = diagnoseHost(host);
    // hostSupported means "this process can actually create a VM right now", so
    // it must include the sandbox entitlement gate. frameworkAvailable alone
    // would advertise support for a process whose every VM creation fails, which
    // is exactly the dishonesty the shared contract forbids.
    result.hostSupported = hostMayCreateVirtualMachine(host);
    if (result.hostSupported) {
        result.summary = "macOS 原生虚拟化可用：" + result.capabilities.hostArch
            + " 客户机可原生引导；其他架构回退 QEMU TCG。";
    } else {
        result.summary = "macOS 原生虚拟化不可用，请使用 QEMU 后端。";
        for (const Issue& issue : result.diagnostics) {
            if (issue.level == IssueLevel::Error) {
                result.summary += " [" + issue.code + "] " + issue.message;
                break;
            }
        }
    }
    return result;
}

// Reason-out adapters mirroring the shared contract's
// `isHostSupported(QString* reason)` / `supportsGuestArch(arch, QString* reason)`.
//
// Contract: `reason` is cleared first and only filled when the answer is false,
// so an adapter can forward it verbatim as the `QString* reason` out-parameter of
// the shared interface without producing spurious warnings on success.
inline bool describeHostSupport(const Host& host, std::string* reason) {
    const ProbeResult result = probeHost(host);
    if (reason) {
        reason->clear();
        if (!result.hostSupported) {
            *reason = result.summary;
        }
    }
    return result.hostSupported;
}

inline bool describeGuestArchSupport(const std::string& guestArch,
                                     const Host& host,
                                     std::string* reason) {
    const bool supported = hostSupportsGuestArch(guestArch, host);
    if (!reason) {
        return supported;
    }
    reason->clear();
    if (supported) {
        return true;
    }
    // Prefer the host-level explanation when the host itself is the problem.
    for (const Issue& issue : diagnoseHost(host)) {
        if (issue.level == IssueLevel::Error) {
            *reason = issue.message;
            return false;
        }
    }
    const std::string native = nativeGuestArch(host);
    if (native.empty()) {
        *reason = "宿主 CPU 架构 " + normalizeArch(host.arch)
            + " 不受 Virtualization.framework 原生引导支持；请回退到 QEMU。";
    } else {
        *reason = "宿主 " + normalizeArch(host.arch) + " 只能原生虚拟化 " + native
            + " 客户机，无法虚拟化 " + normalizeArch(guestArch) + "；请回退到 QEMU TCG。";
    }
    return false;
}

// ---------------------------------------------------------------------------
// Configuration + validation
// ---------------------------------------------------------------------------

enum class GuestKind { Linux, MacOSIPSW, Unknown };

struct Config {
    std::string guestOs;       // "linux", "macos", ... (free form, normalized)
    std::string architecture;  // requested guest arch
    std::string kernelPath;    // Linux kernel image
    std::string initrdPath;    // optional Linux initrd
    std::string kernelCommandLine;
    std::string ipswPath;      // macOS restore image
    bool restoreFromIPSW = false;
    int cpuCount = 2;
    int memoryMb = 4096;
    std::vector<std::string> diskPaths;
    bool networkEnabled = true;
    bool directorySharingEnabled = false;
    std::string sharedDirectoryPath;
    bool graphicsEnabled = true;
    bool audioEnabled = false;
};

inline GuestKind classifyGuest(const Config& config) {
    const std::string os = toLowerAscii(trimAscii(config.guestOs));
    if (config.restoreFromIPSW || os == "macos" || os == "macosx" || os == "mac os x"
        || os == "darwin") {
        return GuestKind::MacOSIPSW;
    }
    if (os == "linux" || os.empty()) {
        return GuestKind::Linux;
    }
    return GuestKind::Unknown;
}

// Pure, deterministic validation. File existence is checked separately by the
// platform layer so this function stays usable with synthetic hosts.
inline std::vector<Issue> validateConfig(const Config& config, const Host& host) {
    std::vector<Issue> issues;
    const Capabilities caps = evaluateCapabilities(host);
    const GuestKind kind = classifyGuest(config);
    const std::string guestArch = normalizeArch(config.architecture);

    const std::string os = toLowerAscii(trimAscii(config.guestOs));
    if (os.empty() && !config.restoreFromIPSW) {
        issues.push_back({IssueLevel::Error,
                          "guest-os-missing",
                          "未指定 guestOs（例如 linux 或 macos）。"});
    }

    if (kind == GuestKind::Unknown) {
        issues.push_back({IssueLevel::Error,
                          "guest-os-unsupported",
                          "Virtualization.framework 后端不支持 guestOs=\"" + config.guestOs
                              + "\"；请回退到 QEMU。"});
    }

    if (config.cpuCount < 1 || config.cpuCount > 64) {
        issues.push_back({IssueLevel::Error,
                          "cpu-count-invalid",
                          "CPU 数量必须在 1 到 64 之间（当前 " + std::to_string(config.cpuCount) + "）。"});
    }
    if (config.memoryMb < 512) {
        issues.push_back({IssueLevel::Error,
                          "memory-too-small",
                          "内存至少需要 512 MB（当前 " + std::to_string(config.memoryMb) + " MB）。"});
    } else if (config.memoryMb % 2 != 0) {
        issues.push_back({IssueLevel::Info,
                          "memory-odd",
                          "内存大小为奇数 MB；Virtualization.framework 要求 1 MB 对齐，"
                          "请确保上层传入整 MB。"});
    }

    if (kind == GuestKind::Linux) {
        if (guestArch.empty()) {
            issues.push_back({IssueLevel::Error,
                              "guest-arch-missing",
                              "Linux 客户机缺少 architecture。"});
        } else if (!hostSupportsGuestArch(guestArch, host)) {
            std::string reason;
            if (!host.macOS) {
                reason = "宿主不是 macOS。";
            } else if (host.macOSMajor < 11) {
                reason = "需要 macOS 11 或更高版本。";
            } else if (!host.virtualizationFrameworkAvailable) {
                reason = "Virtualization.framework 不可用。";
            } else {
                reason = "宿主 " + normalizeArch(host.arch) + " 只能原生虚拟化 "
                    + nativeGuestArch(host) + " 客户机，无法虚拟化 " + guestArch
                    + "；请回退到 QEMU TCG。";
            }
            issues.push_back({IssueLevel::Error, "guest-arch-unsupported", reason});
        }
        if (config.kernelPath.empty()) {
            issues.push_back({IssueLevel::Error,
                              "kernel-required",
                              "原生 Linux 引导必须提供内核镜像路径 (VZLinuxBootLoader)。"});
        }
        if (!caps.linuxGuest.supported) {
            issues.push_back({IssueLevel::Error,
                              "linux-guest-unsupported",
                              caps.linuxGuest.reason});
        }
    } else if (kind == GuestKind::MacOSIPSW) {
        if (!caps.macOSGuestIPSW.supported) {
            issues.push_back({IssueLevel::Error,
                              "macos-guest-unsupported",
                              caps.macOSGuestIPSW.reason});
        }
        if (config.ipswPath.empty() && !config.restoreFromIPSW) {
            issues.push_back({IssueLevel::Error,
                              "ipsw-required",
                              "macOS 客户机需要 IPSW 恢复镜像路径。"});
        }
        if (config.restoreFromIPSW && config.ipswPath.empty()) {
            issues.push_back({IssueLevel::Error,
                              "ipsw-required",
                              "restoreFromIPSW 为真时必须提供 IPSW 路径。"});
        }
    }

    if (config.directorySharingEnabled) {
        if (!caps.directorySharing.supported) {
            issues.push_back({IssueLevel::Warning,
                              "directory-sharing-unsupported",
                              caps.directorySharing.reason});
        }
        if (config.sharedDirectoryPath.empty()) {
            issues.push_back({IssueLevel::Warning,
                              "directory-sharing-path-missing",
                              "启用目录共享但未提供宿主目录路径。"});
        }
    }

    if (config.graphicsEnabled && !caps.graphics.supported) {
        issues.push_back({IssueLevel::Warning, "graphics-unsupported", caps.graphics.reason});
    }
    if (config.audioEnabled && !caps.audio.supported) {
        issues.push_back({IssueLevel::Warning, "audio-unsupported", caps.audio.reason});
    }
    if (config.networkEnabled && !caps.networking.supported) {
        issues.push_back({IssueLevel::Warning, "network-unsupported", caps.networking.reason});
    }

    for (const std::string& disk : config.diskPaths) {
        if (trimAscii(disk).empty()) {
            issues.push_back({IssueLevel::Error,
                              "disk-path-empty",
                              "磁盘路径不能为空。"});
        }
    }

    return issues;
}

// ---------------------------------------------------------------------------
// Lifecycle results
// ---------------------------------------------------------------------------

enum class State { NotStarted, Starting, Running, Paused, Stopping, Stopped, Error };

inline const char* stateName(State state) {
    switch (state) {
    case State::NotStarted:
        return "not-started";
    case State::Starting:
        return "starting";
    case State::Running:
        return "running";
    case State::Paused:
        return "paused";
    case State::Stopping:
        return "stopping";
    case State::Stopped:
        return "stopped";
    case State::Error:
    default:
        return "error";
    }
}

struct RunResult {
    bool ok = false;
    std::string code;     // "ok" on success; stable machine code on failure
    std::string message;  // short, user-facing
    std::string detail;   // joined validation issues / framework error
    std::vector<Issue> issues;

    static RunResult success(std::string message) {
        RunResult result;
        result.ok = true;
        result.code = "ok";
        result.message = std::move(message);
        return result;
    }

    static RunResult failure(std::string code, std::string message, std::string detail = {}) {
        RunResult result;
        result.ok = false;
        result.code = std::move(code);
        result.message = std::move(message);
        result.detail = std::move(detail);
        return result;
    }
};

// ---------------------------------------------------------------------------
// Engine
// ---------------------------------------------------------------------------

class MacVirtualizationEngine {
public:
    MacVirtualizationEngine();
    ~MacVirtualizationEngine();

    MacVirtualizationEngine(const MacVirtualizationEngine&) = delete;
    MacVirtualizationEngine& operator=(const MacVirtualizationEngine&) = delete;

    // --- backend identity ----------------------------------------------------
    static const char* backendIdentifier();
    static const char* backendDisplayName();

    // --- host/guest capability probe ----------------------------------------
    static bool isHostSupported();
    static bool supportsGuestArch(const std::string& guestArch);
    static ProbeResult probe();

    // Reason-out twins of the two probes above. They mirror the shared contract's
    // `isHostSupported(QString* reason)` / `supportsGuestArch(arch, QString*
    // reason)`: `reason` is cleared first and only filled when the answer is
    // false, so an adapter can forward it verbatim.
    static bool isHostSupported(std::string* reason);
    static bool supportsGuestArch(const std::string& guestArch, std::string* reason);

    // --- runtime state -------------------------------------------------------
    Capabilities capabilities() const;
    Host hostProfile() const;
    State state() const;

    // --- validation ----------------------------------------------------------
    // Combines the pure policy check with real filesystem existence checks.
    std::vector<Issue> validate(const Config& config) const;

    // --- lifecycle -----------------------------------------------------------
    // Synchronous adapters for the shared registry (blocking, bounded by an
    // internal timeout). Errors are always propagated through RunResult.
    RunResult start(const Config& config);
    RunResult stop();

    // --- asynchronous lifecycle ---------------------------------------------
    // The callback is invoked exactly once.
    //
    // * Validation/capability failures are reported synchronously, on the calling
    //   thread, because they never touch the framework.
    // * Otherwise the callback runs on a backend-owned worker thread, so callers
    //   must marshal to their own thread themselves.
    //
    // Asynchronous work never keeps the engine alive. Destroying the engine while
    // a start or stop is still in flight is safe: an in-flight start is rolled
    // back and reported with the stable code "engine-destroyed" instead of being
    // silently abandoned.
    void startAsync(const Config& config,
                    std::function<void(const RunResult&)> completion);
    void stopAsync(std::function<void(const RunResult&)> completion);

    // --- test seam -----------------------------------------------------------
    // Invoked immediately before the Virtualization.framework handshake: on the
    // calling thread for start(), on the worker thread for startAsync().
    // Returning false aborts the start with the RunResult the gate filled in, so
    // the framework is never touched and no VM is created.
    //
    // This exists so the async lifetime protocol — worker in flight while the
    // engine is destroyed, and the "engine-destroyed" rollback — can be
    // regression-tested without booting a guest. Production code never installs a
    // gate, and installing one is never required for correctness.
    using StartGate = std::function<bool(const Config& config, RunResult* abortResult)>;
    void setStartGateForTesting(StartGate gate);

    // --- chaos seam #2 -------------------------------------------------------
    // Arms a *counted stand-in* for the Virtualization.framework runner, which
    // start()/startAsync() install into Session::runnerRaw instead of the real
    // runner. The stand-in follows the same ownership contract as the real one
    // (it goes through the very same +retain / -release helpers, and its dealloc
    // is counted) but it is never handed to Virtualization.framework and no guest
    // is ever booted.
    //
    // That is what makes the ownership contract around Session::runnerRaw — the
    // field whose unlocked read/write caused a double release and a dangling
    // pointer — regression-testable deterministically: after a long interleaving
    // of start / stop / destroy, every installed stand-in must have been
    // destroyed exactly once. One counter short means a released runner was
    // leaked; more than one means the field was released twice.
    //
    // Production code never arms this; the default is "not armed", so every
    // production path is unchanged.
    //
    // Known limitation, stated plainly: while armed, stop() takes the synchronous
    // not-requested branch instead of the framework's asynchronous stop
    // hand-off, and the detach hop onto the machine queue is skipped. The
    // ownership arithmetic is still the production arithmetic — the same
    // vwStopSession() does the transfer — but the asynchronous framework
    // handshake itself is *not* covered by this seam.
    void setRunnerStubForTesting(bool enabled);
    // Stand-ins installed by start() since the counters were last reset.
    int runnerStubInstalled() const;
    // Stand-ins whose Objective-C dealloc has run since the last reset.
    int runnerStubDestroyed() const;
    void resetRunnerStubCountersForTesting();

private:
    // Async lifetime guard shared with in-flight workers; see
    // MacVirtualizationEngine.mm (AsyncControl). Opaque here on purpose so this
    // header stays free of <mutex> and of the .mm's types.
    std::shared_ptr<void> asyncControl_;

    // Published state. Reads are atomic (state() is lock-free); writes happen
    // while holding the async control block's mutex.
    std::atomic<int> stateValue_{static_cast<int>(State::NotStarted)};

    // Running session. Read/written while holding the async control block's
    // mutex.
    std::shared_ptr<void> session_;

    // Optional test gate; read/written while holding the control block's mutex.
    StartGate startGate_;
};

// Real host probe (implemented in MacVirtualizationEngine.mm on Apple hosts).
Host currentHost();

// ---------------------------------------------------------------------------
// Shared-interface adapter descriptor
// ---------------------------------------------------------------------------
//
// An upper-layer registry can consume this without this backend having to know
// the registry's concrete types. The names mirror the agreed shared contract:
// backend id/name, isHostSupported, supportsGuestArch, probe/capabilities,
// validate, start/stop.
struct BackendDescriptor {
    std::string id;
    std::string name;
    bool (*isHostSupported)();
    bool (*supportsGuestArch)(const std::string& guestArch);
    ProbeResult (*probe)();
    Capabilities (*capabilities)();
    std::vector<Issue> (*validate)(const Config& config);
    RunResult (*start)(const Config& config);
    RunResult (*stop)();
};

const BackendDescriptor& backendDescriptor();

} // namespace macvirt
} // namespace vw

// ---------------------------------------------------------------------------
// Portable fallbacks
// ---------------------------------------------------------------------------
//
// On non-Apple hosts (or Apple hosts without the framework headers) the platform
// methods are defined inline so that including this header never produces link
// errors in a Linux/Windows build. On macOS with the framework, they live in
// MacVirtualizationEngine.mm.

#if !VW_MACVIRT_HAS_FRAMEWORK

namespace vw {
namespace macvirt {

inline Host currentHost() {
    Host host;
    host.macOS = false;
    host.arch = "unknown";
    host.osVersion = "non-macos";
    host.virtualizationFrameworkAvailable = false;
    return host;
}

inline MacVirtualizationEngine::MacVirtualizationEngine() = default;
inline MacVirtualizationEngine::~MacVirtualizationEngine() = default;

inline const char* MacVirtualizationEngine::backendIdentifier() {
    return "macos-virtualization";
}

inline const char* MacVirtualizationEngine::backendDisplayName() {
    return "Apple Virtualization.framework";
}

inline bool MacVirtualizationEngine::isHostSupported() {
    return probeHost(currentHost()).hostSupported;
}

inline bool MacVirtualizationEngine::supportsGuestArch(const std::string& guestArch) {
    return hostSupportsGuestArch(guestArch, currentHost());
}

inline bool MacVirtualizationEngine::isHostSupported(std::string* reason) {
    return describeHostSupport(currentHost(), reason);
}

inline bool MacVirtualizationEngine::supportsGuestArch(const std::string& guestArch,
                                                       std::string* reason) {
    return describeGuestArchSupport(guestArch, currentHost(), reason);
}

inline ProbeResult MacVirtualizationEngine::probe() {
    return probeHost(currentHost());
}

inline Capabilities MacVirtualizationEngine::capabilities() const {
    return evaluateCapabilities(currentHost());
}

inline Host MacVirtualizationEngine::hostProfile() const {
    return currentHost();
}

inline State MacVirtualizationEngine::state() const {
    return static_cast<State>(stateValue_.load());
}

inline std::vector<Issue> MacVirtualizationEngine::validate(const Config& config) const {
    return validateConfig(config, currentHost());
}

inline RunResult MacVirtualizationEngine::start(const Config&) {
    return RunResult::failure("host-not-macos",
                              "Apple Virtualization.framework 后端仅在 macOS 上可用。");
}

inline RunResult MacVirtualizationEngine::stop() {
    return RunResult::failure("not-running", "没有正在运行的 macOS 原生虚拟机。");
}

inline void MacVirtualizationEngine::startAsync(
    const Config&,
    std::function<void(const RunResult&)> completion) {
    if (completion) {
        completion(RunResult::failure("host-not-macos",
                                      "Apple Virtualization.framework 后端仅在 macOS 上可用。"));
    }
}

inline void MacVirtualizationEngine::stopAsync(std::function<void(const RunResult&)> completion) {
    if (completion) {
        completion(RunResult::failure("not-running", "没有正在运行的 macOS 原生虚拟机。"));
    }
}

inline const BackendDescriptor& backendDescriptor() {
    static const BackendDescriptor descriptor = {
        backendId(),
        backendName(),
        &MacVirtualizationEngine::isHostSupported,
        &MacVirtualizationEngine::supportsGuestArch,
        &MacVirtualizationEngine::probe,
        []() { return MacVirtualizationEngine().capabilities(); },
        [](const Config& config) { return MacVirtualizationEngine().validate(config); },
        [](const Config& config) { return MacVirtualizationEngine().start(config); },
        []() { return MacVirtualizationEngine().stop(); },
    };
    return descriptor;
}

} // namespace macvirt
} // namespace vw

#endif // !VW_MACVIRT_HAS_FRAMEWORK
