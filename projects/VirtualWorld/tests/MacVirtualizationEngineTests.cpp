// MacVirtualizationEngineTests.cpp — logic and capability tests for the macOS
// native virtualization backend.
//
// The tests are intentionally split into two groups:
//
//   * Pure policy tests: run everywhere (including Linux/Windows) because the
//     policy layer in MacVirtualizationEngine.hpp is framework-free.
//   * Host/capability tests: only meaningful on a real macOS host. When the host
//     cannot support Virtualization.framework (older macOS, non-macOS CI, no
//     framework, missing sandbox entitlement) they are reported as SKIP instead
//     of failing, so the suite is reliable on every machine.
//
// No test ever boots a real VM: the kernel/IPSW files used below are either
// non-existent (validation must reject them) or empty regular files used only to
// exercise the validation plumbing. Capability probing is read-only.

#include "MacVirtualizationEngine.hpp"

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <type_traits>
#include <vector>

#if defined(__unix__) || defined(__APPLE__)
#include <unistd.h>
#endif

namespace {

using namespace vw::macvirt;

int g_checks = 0;
int g_failures = 0;
int g_skips = 0;

void check(bool condition, const std::string& label) {
    ++g_checks;
    if (!condition) {
        ++g_failures;
        std::fprintf(stderr, "FAIL: %s\n", label.c_str());
    }
}

void checkEqual(const std::string& actual, const std::string& expected, const std::string& label) {
    ++g_checks;
    if (actual != expected) {
        ++g_failures;
        std::fprintf(stderr,
                     "FAIL: %s (expected \"%s\", got \"%s\")\n",
                     label.c_str(),
                     expected.c_str(),
                     actual.c_str());
    }
}

void checkEqual(int actual, int expected, const std::string& label) {
    ++g_checks;
    if (actual != expected) {
        ++g_failures;
        std::fprintf(stderr,
                     "FAIL: %s (expected %d, got %d)\n",
                     label.c_str(),
                     expected,
                     actual);
    }
}

void skip(const std::string& label) {
    ++g_skips;
    std::fprintf(stdout, "SKIP: %s\n", label.c_str());
}

Host syntheticHost(const std::string& arch, int macOSMajor, bool framework, bool appleSilicon) {
    Host host;
    host.macOS = true;
    host.arch = arch;
    host.appleSilicon = appleSilicon;
    host.macOSMajor = macOSMajor;
    host.macOSMinor = 0;
    host.osVersion = "macOS " + std::to_string(macOSMajor);
    host.virtualizationFrameworkAvailable = framework;
    host.frameworkVersion = "1.0";
    return host;
}

Config linuxConfig(const std::string& arch) {
    Config config;
    config.guestOs = "linux";
    config.architecture = arch;
    config.kernelPath = "/tmp/vw-macvirt-kernel";
    config.cpuCount = 2;
    config.memoryMb = 2048;
    return config;
}

class TempTree {
public:
    TempTree() {
        static int counter = 0;
#if defined(__unix__) || defined(__APPLE__)
        const long pid = static_cast<long>(::getpid());
#else
        const long pid = 0;
#endif
        root_ = std::filesystem::temp_directory_path()
            / ("vw-macvirt-tests-" + std::to_string(pid) + "-" + std::to_string(counter++));
        std::error_code error;
        std::filesystem::create_directories(root_, error);
    }

    ~TempTree() {
        std::error_code error;
        std::filesystem::remove_all(root_, error);
    }

    std::string file(const std::string& name) const {
        const std::filesystem::path path = root_ / name;
        std::ofstream stream(path, std::ios::binary);
        stream << "virtualworld-test";
        return path.string();
    }

    // Returns a path inside the temp tree without creating it.
    std::string path(const std::string& name) const {
        return (root_ / name).string();
    }

    std::string directory(const std::string& name) const {
        const std::filesystem::path path = root_ / name;
        std::error_code error;
        std::filesystem::create_directories(path, error);
        return path.string();
    }

private:
    std::filesystem::path root_;
};

// ---------------------------------------------------------------------------
// Pure policy tests
// ---------------------------------------------------------------------------

void testIdentity() {
    checkEqual(backendId(), "macos-virtualization", "backendId");
    checkEqual(backendName(), "Apple Virtualization.framework", "backendName");
    checkEqual(MacVirtualizationEngine::backendIdentifier(),
               "macos-virtualization",
               "engine backendIdentifier");
    checkEqual(MacVirtualizationEngine::backendDisplayName(),
               "Apple Virtualization.framework",
               "engine backendDisplayName");
    const BackendDescriptor& descriptor = backendDescriptor();
    checkEqual(descriptor.id, "macos-virtualization", "descriptor id");
    checkEqual(descriptor.name, "Apple Virtualization.framework", "descriptor name");
    check(descriptor.isHostSupported != nullptr, "descriptor isHostSupported");
    check(descriptor.supportsGuestArch != nullptr, "descriptor supportsGuestArch");
    check(descriptor.probe != nullptr, "descriptor probe");
    check(descriptor.capabilities != nullptr, "descriptor capabilities");
    check(descriptor.validate != nullptr, "descriptor validate");
    check(descriptor.start != nullptr, "descriptor start");
    check(descriptor.stop != nullptr, "descriptor stop");
}

void testArchNormalization() {
    checkEqual(normalizeArch("arm64"), "aarch64", "normalize arm64");
    checkEqual(normalizeArch("ARM64E"), "aarch64", "normalize ARM64E");
    checkEqual(normalizeArch("aarch64"), "aarch64", "normalize aarch64");
    checkEqual(normalizeArch("amd64"), "x86_64", "normalize amd64");
    checkEqual(normalizeArch("x86_64"), "x86_64", "normalize x86_64");
    checkEqual(normalizeArch("i686"), "i386", "normalize i686");
    checkEqual(normalizeArch("armv7"), "arm", "normalize armv7");
    checkEqual(normalizeArch("ppc64"), "ppc", "normalize ppc64");
    checkEqual(normalizeArch(""), "", "normalize empty");
}

void testAppleSiliconPolicy() {
    const Host host = syntheticHost("aarch64", 13, true, true);
    check(hostSupportsGuestArch("arm64", host), "Apple Silicon supports arm64");
    check(hostSupportsGuestArch("aarch64", host), "Apple Silicon supports aarch64");
    check(!hostSupportsGuestArch("x86_64", host), "Apple Silicon rejects x86_64");
    check(!hostSupportsGuestArch("amd64", host), "Apple Silicon rejects amd64");
    check(!hostSupportsGuestArch("i386", host), "Apple Silicon rejects i386");
    check(!hostSupportsGuestArch("ppc", host), "Apple Silicon rejects ppc");

    const Capabilities caps = evaluateCapabilities(host);
    check(caps.frameworkAvailable, "Apple Silicon framework available");
    check(caps.nativeArm64Guest, "Apple Silicon native arm64 guest");
    check(!caps.nativeX86_64Guest, "Apple Silicon not native x86_64 guest");
    check(caps.linuxGuest.supported, "Apple Silicon Linux guest supported");
    check(caps.linuxGuest.code == "linux-native-boot", "Apple Silicon Linux code");
    check(caps.macOSGuestIPSW.supported, "Apple Silicon macOS IPSW probe supported");
    check(caps.macOSGuestIPSW.code == "macos-ipsw-probe-only", "macOS IPSW honest code");
}

void testIntelPolicy() {
    const Host host = syntheticHost("x86_64", 13, true, false);
    check(hostSupportsGuestArch("x86_64", host), "Intel supports x86_64");
    check(!hostSupportsGuestArch("aarch64", host), "Intel rejects aarch64");
    check(!hostSupportsGuestArch("arm64", host), "Intel rejects arm64");

    const Capabilities caps = evaluateCapabilities(host);
    check(caps.frameworkAvailable, "Intel framework available");
    check(caps.nativeX86_64Guest, "Intel native x86_64 guest");
    check(!caps.nativeArm64Guest, "Intel not native arm64 guest");
    check(caps.linuxGuest.supported, "Intel Linux guest supported");
    check(!caps.macOSGuestIPSW.supported, "Intel macOS IPSW unsupported");
    checkEqual(caps.macOSGuestIPSW.code,
               "macos-guest-apple-silicon-only",
               "Intel macOS IPSW code");
    check(!caps.macOSGuestIPSW.reason.empty(), "Intel macOS IPSW reason present");
    check(!caps.macOSGuestBoot.supported, "Intel macOS boot unsupported");
}

void testMacOSVersionGating() {
    const Host macOS11 = syntheticHost("aarch64", 11, true, true);
    check(hostSupportsGuestArch("aarch64", macOS11), "macOS 11 supports arm64 Linux");
    const Capabilities caps11 = evaluateCapabilities(macOS11);
    check(!caps11.macOSGuestIPSW.supported, "macOS 11 rejects macOS IPSW");
    checkEqual(caps11.macOSGuestIPSW.code, "macos-12-required", "macOS 11 IPSW code");
    check(!caps11.directorySharing.supported, "macOS 11 no directory sharing");
    checkEqual(caps11.directorySharing.code, "macos-12-required", "macOS 11 sharing code");

    const Host macOS10 = syntheticHost("x86_64", 10, true, false);
    check(!hostSupportsGuestArch("x86_64", macOS10), "macOS 10 rejects Linux");
    const Capabilities caps10 = evaluateCapabilities(macOS10);
    check(!caps10.frameworkAvailable, "macOS 10 framework unavailable");
    check(!caps10.linuxGuest.supported, "macOS 10 Linux unsupported");
    checkEqual(caps10.linuxGuest.code, "macos-11-required", "macOS 10 Linux code");

    const Host noFramework = syntheticHost("aarch64", 13, false, true);
    check(!hostSupportsGuestArch("aarch64", noFramework), "missing framework rejects guest");
    const Capabilities capsNoFramework = evaluateCapabilities(noFramework);
    checkEqual(capsNoFramework.linuxGuest.code,
               "framework-unavailable",
               "missing framework Linux code");
}

void testNonMacHost() {
    Host host;
    host.macOS = false;
    host.arch = "x86_64";
    const ProbeResult result = probeHost(host);
    check(!result.hostSupported, "non-mac host unsupported");
    check(!result.capabilities.linuxGuest.supported, "non-mac Linux unsupported");
    checkEqual(result.capabilities.linuxGuest.code, "host-not-macos", "non-mac Linux code");
    check(hasCode(result.diagnostics, "host-not-macos"), "non-mac diagnostic code");
    check(!result.summary.empty(), "non-mac summary present");
}

void testDiagnostics() {
    const Host apple = syntheticHost("aarch64", 14, true, true);
    const std::vector<Issue> issues = diagnoseHost(apple);
    check(hasCode(issues, "framework-available"), "diagnose framework available");
    check(hasCode(issues, "cross-arch-qemu-tcg"), "diagnose cross-arch fallback");
    check(!hasErrors(issues), "healthy host has no errors");

    Host sandboxed = apple;
    sandboxed.sandboxed = true;
    sandboxed.hasVirtualizationEntitlement = false;
    const std::vector<Issue> sandboxIssues = diagnoseHost(sandboxed);
    check(hasCode(sandboxIssues, "sandbox-entitlement-missing"), "diagnose sandbox missing entitlement");
    check(hasErrors(sandboxIssues), "sandboxed without entitlement is an error");

    Host entitled = sandboxed;
    entitled.hasVirtualizationEntitlement = true;
    check(!hasErrors(diagnoseHost(entitled)), "sandboxed with entitlement is fine");

    Host nested = apple;
    nested.virtualizingNested = true;
    check(hasCode(diagnoseHost(nested), "nested-virtualization"), "diagnose nested virtualization");
}

void testValidateLinux() {
    const Host apple = syntheticHost("aarch64", 13, true, true);
    const Host intel = syntheticHost("x86_64", 13, true, false);

    const std::vector<Issue> ok = validateConfig(linuxConfig("aarch64"), apple);
    check(!hasErrors(ok), "valid Linux/arm64 config on Apple Silicon");

    check(!hasErrors(validateConfig(linuxConfig("x86_64"), intel)), "valid Linux/x86_64 config on Intel");

    const std::vector<Issue> cross = validateConfig(linuxConfig("x86_64"), apple);
    check(hasCode(cross, "guest-arch-unsupported"), "cross-arch config rejected");
    check(hasErrors(cross), "cross-arch config has errors");

    Config missingKernel = linuxConfig("aarch64");
    missingKernel.kernelPath.clear();
    check(hasCode(validateConfig(missingKernel, apple), "kernel-required"), "missing kernel rejected");

    Config missingOs = linuxConfig("aarch64");
    missingOs.guestOs.clear();
    check(hasCode(validateConfig(missingOs, apple), "guest-os-missing"), "missing guestOs rejected");

    Config badCpu = linuxConfig("aarch64");
    badCpu.cpuCount = 0;
    check(hasCode(validateConfig(badCpu, apple), "cpu-count-invalid"), "invalid CPU count rejected");

    Config badMemory = linuxConfig("aarch64");
    badMemory.memoryMb = 128;
    check(hasCode(validateConfig(badMemory, apple), "memory-too-small"), "small memory rejected");

    Config emptyDisk = linuxConfig("aarch64");
    emptyDisk.diskPaths.push_back("");
    check(hasCode(validateConfig(emptyDisk, apple), "disk-path-empty"), "empty disk path rejected");

    Config unknownOs = linuxConfig("aarch64");
    unknownOs.guestOs = "freebsd";
    check(hasCode(validateConfig(unknownOs, apple), "guest-os-unsupported"), "unknown guest OS rejected");
}

void testValidateMacOSGuest() {
    const Host apple = syntheticHost("aarch64", 13, true, true);
    const Host intel = syntheticHost("x86_64", 13, true, false);

    Config ipsw;
    ipsw.guestOs = "macos";
    ipsw.architecture = "aarch64";
    ipsw.restoreFromIPSW = true;
    ipsw.ipswPath = "/tmp/vw-macvirt.ipsw";
    ipsw.cpuCount = 4;
    ipsw.memoryMb = 8192;
    const std::vector<Issue> appleIssues = validateConfig(ipsw, apple);
    check(!hasErrors(appleIssues), "macOS IPSW config valid on Apple Silicon");
    check(!hasCode(appleIssues, "macos-guest-unsupported"), "Apple Silicon macOS guest allowed");

    const std::vector<Issue> intelIssues = validateConfig(ipsw, intel);
    check(hasCode(intelIssues, "macos-guest-unsupported"), "Intel rejects macOS guest");
    check(hasErrors(intelIssues), "Intel macOS guest has errors");

    Config missingIpsw = ipsw;
    missingIpsw.ipswPath.clear();
    check(hasCode(validateConfig(missingIpsw, apple), "ipsw-required"), "missing IPSW rejected");

    const Host macOS11 = syntheticHost("aarch64", 11, true, true);
    check(hasCode(validateConfig(ipsw, macOS11), "macos-guest-unsupported"), "macOS 11 rejects macOS guest");
}

void testRunResultHelpers() {
    const RunResult ok = RunResult::success("done");
    check(ok.ok, "success ok");
    checkEqual(ok.code, "ok", "success code");
    const RunResult failure = RunResult::failure("boom", "message", "detail");
    check(!failure.ok, "failure not ok");
    checkEqual(failure.code, "boom", "failure code");
    checkEqual(failure.detail, "detail", "failure detail");
}

void testIssueFormatting() {
    std::vector<Issue> issues;
    issues.push_back({IssueLevel::Info, "info", "info message"});
    issues.push_back({IssueLevel::Error, "error", "error message"});
    issues.push_back({IssueLevel::Warning, "warning", "warning message"});
    check(hasErrors(issues), "hasErrors detects error");
    check(hasCode(issues, "warning"), "hasCode detects warning");
    const std::string details = joinIssueDetails(issues);
    check(details.find("error:error:error message") != std::string::npos, "details contain error");
    check(details.find("warning:warning:warning message") != std::string::npos, "details contain warning");
    check(details.find("info message") == std::string::npos, "details omit info");
}

// The shared contract passes a `QString* reason` out-parameter; describe*() is
// the Qt-free twin the adapter forwards. It must clear the reason on success and
// always fill it on failure.
void testReasonOutParameters() {
    const Host apple = syntheticHost("aarch64", 13, true, true);
    const Host broken = syntheticHost("aarch64", 13, false, true);

    std::string reason = "stale";
    check(describeHostSupport(apple, &reason), "describeHostSupport true for healthy host");
    check(reason.empty(), "describeHostSupport clears reason on success");

    reason = "stale";
    check(!describeHostSupport(broken, &reason), "describeHostSupport false without framework");
    check(!reason.empty(), "describeHostSupport fills reason on failure");

    reason = "stale";
    check(describeGuestArchSupport("arm64", apple, &reason), "describeGuestArchSupport native ok");
    check(reason.empty(), "describeGuestArchSupport clears reason on success");

    reason = "stale";
    check(!describeGuestArchSupport("x86_64", apple, &reason), "describeGuestArchSupport cross-arch");
    check(reason.find("QEMU") != std::string::npos, "cross-arch reason mentions QEMU");
    check(reason.find("aarch64") != std::string::npos, "cross-arch reason names the native arch");

    reason = "stale";
    check(!describeGuestArchSupport("x86_64", broken, &reason), "describeGuestArchSupport no framework");
    check(!reason.empty(), "no-framework guest reason is filled");

    // A host ISA the framework cannot boot at all.
    const Host ppc = syntheticHost("ppc64", 13, true, false);
    reason = "stale";
    check(!describeGuestArchSupport("ppc", ppc, &reason), "describeGuestArchSupport unsupported host");
    check(reason.find("QEMU") != std::string::npos, "unsupported-host reason mentions QEMU");

    // nullptr must be accepted on every reason-out entry point.
    check(describeHostSupport(apple, nullptr), "describeHostSupport accepts nullptr");
    check(describeGuestArchSupport("arm64", apple, nullptr), "describeGuestArchSupport nullptr ok");
    check(MacVirtualizationEngine::supportsGuestArch("x86_64", nullptr)
              == hostSupportsGuestArch("x86_64", currentHost()),
          "engine guest-arch reason overload matches policy");

    reason = "stale";
    check(MacVirtualizationEngine::supportsGuestArch("x86_64", &reason)
              == hostSupportsGuestArch("x86_64", currentHost()),
          "engine guest-arch reason overload fills reason consistently");
}

// canStart/canStop must reflect what start() really does, not what the host could
// theoretically do: the macOS restore path is probe-only, so it never sets them.
void testCanStartHonesty() {
    const Host apple = syntheticHost("aarch64", 13, true, true);
    const Capabilities caps = evaluateCapabilities(apple);
    check(caps.linuxGuest.supported, "Linux guest is supported on Apple Silicon");
    check(caps.canStart, "canStart true when Linux boot is available");
    check(caps.canStop, "canStop true when a VM can be running");
    check(caps.macOSGuestIPSW.supported, "macOS IPSW capability is still advertised");
    checkEqual(backendTierName(), "native", "backend tier maps to VirtualizationTier::Native");

    const Host intel = syntheticHost("x86_64", 13, true, false);
    const Capabilities intelCaps = evaluateCapabilities(intel);
    check(intelCaps.canStart, "Intel canStart true for native Linux");
    check(!intelCaps.macOSGuestIPSW.supported, "Intel has no macOS guest path");

    const Host broken = syntheticHost("aarch64", 10, true, true);
    const Capabilities brokenCaps = evaluateCapabilities(broken);
    check(!brokenCaps.canStart, "canStart false when the framework is unusable");
    check(!brokenCaps.canStop, "canStop false when no VM can run");
}

void testGuestClassification() {
    Config config;
    config.guestOs = "linux";
    check(classifyGuest(config) == GuestKind::Linux, "classify linux");
    config.guestOs = "";
    check(classifyGuest(config) == GuestKind::Linux, "classify empty defaults to linux");
    config.guestOs = "MacOS";
    check(classifyGuest(config) == GuestKind::MacOSIPSW, "classify macOS is case insensitive");
    config.guestOs = "darwin";
    check(classifyGuest(config) == GuestKind::MacOSIPSW, "classify darwin");
    config.guestOs = "freebsd";
    check(classifyGuest(config) == GuestKind::Unknown, "classify unknown OS");
    config.guestOs = "linux";
    config.restoreFromIPSW = true;
    check(classifyGuest(config) == GuestKind::MacOSIPSW, "restoreFromIPSW wins over guestOs");
}

void testUnsupportedHostArchitecture() {
    // A Mac whose CPU cannot be virtualized by the framework at all. The backend
    // must say so instead of pretending, and must not claim any native guest.
    const Host host = syntheticHost("ppc64", 13, true, false);
    check(nativeGuestArch(host).empty(), "ppc host has no native guest arch");
    check(!hostSupportsGuestArch("ppc", host), "ppc host rejects ppc guest");
    check(!hostSupportsGuestArch("aarch64", host), "ppc host rejects aarch64 guest");
    check(!hostSupportsGuestArch("x86_64", host), "ppc host rejects x86_64 guest");

    const Capabilities caps = evaluateCapabilities(host);
    check(!caps.nativeArm64Guest, "ppc host not native arm64");
    check(!caps.nativeX86_64Guest, "ppc host not native x86_64");
    check(!caps.linuxGuest.supported, "ppc host Linux unsupported");
    checkEqual(caps.linuxGuest.code, "host-arch-unsupported", "ppc host Linux code");
    check(hasErrors(diagnoseHost(host)), "ppc host diagnostics report an error");
    check(hasCode(diagnoseHost(host), "host-arch-unsupported"), "ppc host diagnostic code");

    // Intel Macs really are x86_64-only: arm64 must never be advertized as native.
    const Host intel = syntheticHost("x86_64", 14, true, false);
    const Capabilities intelCaps = evaluateCapabilities(intel);
    check(intelCaps.nativeX86_64Guest && !intelCaps.nativeArm64Guest, "Intel is x86_64-only");
    check(hasCode(diagnoseHost(intel), "cross-arch-qemu-tcg"), "Intel advertizes QEMU fallback");
}

// The macOS-guest surface must never be more optimistic than the implementation:
// whenever the IPSW capability is offered, start() has to refuse it explicitly.
void testMacOSGuestHonesty() {
    const Host apple = syntheticHost("aarch64", 13, true, true);
    const Capabilities caps = evaluateCapabilities(apple);
    checkEqual(caps.macOSGuestBoot.code, caps.macOSGuestIPSW.code, "macOS boot mirrors IPSW gating");
    check(caps.macOSGuestBoot.supported == caps.macOSGuestIPSW.supported,
          "macOS boot support mirrors IPSW support");
    if (caps.macOSGuestIPSW.supported) {
        check(!caps.macOSGuestIPSW.reason.empty(), "probe-only IPSW capability explains itself");
        checkEqual(caps.macOSGuestIPSW.code,
                   "macos-ipsw-probe-only",
                   "IPSW capability is explicitly probe-only");
        check(caps.macOSGuestIPSW.reason.find("unsupported") != std::string::npos,
              "probe-only reason mentions the unsupported start()");
    }

    // Intel: macOS guests belong to QEMU, and the reason must say so.
    const Host intel = syntheticHost("x86_64", 13, true, false);
    const Capabilities intelCaps = evaluateCapabilities(intel);
    checkEqual(intelCaps.macOSGuestIPSW.code,
               "macos-guest-apple-silicon-only",
               "Intel macOS guest points at Apple Silicon");
    check(intelCaps.macOSGuestIPSW.reason.find("QEMU") != std::string::npos,
          "Intel macOS guest reason mentions QEMU");
}

void testEngineConstructionAndLifetime() {
    static_assert(!std::is_copy_constructible<MacVirtualizationEngine>::value,
                  "MacVirtualizationEngine must not be copyable");
    static_assert(!std::is_copy_assignable<MacVirtualizationEngine>::value,
                  "MacVirtualizationEngine must not be copy-assignable");

    // Repeated construction/destruction must be clean even though the engine owns
    // a shared async control block and may have a pending worker.
    for (int i = 0; i < 8; ++i) {
        MacVirtualizationEngine engine;
        checkEqual(std::string(stateName(engine.state())), "not-started", "fresh engine state");
        check(!engine.hostProfile().arch.empty(), "fresh engine has a host arch");
        const RunResult stopResult = engine.stop();
        check(!stopResult.ok, "fresh engine stop fails");
        checkEqual(stopResult.code, "not-running", "fresh engine stop code");
    }

    // An engine whose start() failed must still tear down cleanly.
    {
        MacVirtualizationEngine engine;
        Config invalid = linuxConfig("ppc64");
        invalid.kernelPath = "/tmp/vw-macvirt-definitely-missing-kernel";
        const RunResult result = engine.start(invalid);
        check(!result.ok, "unsupported-arch start fails");
        checkEqual(std::string(stateName(engine.state())), "not-started", "failed start keeps state");
    }
}

// The async entry points must report validation failures exactly once and must
// never leave the engine wedged in a busy state, even when called repeatedly.
void testAsyncCompletionContract() {
    MacVirtualizationEngine engine;

    Config invalid = linuxConfig("ppc64");
    invalid.kernelPath = "/tmp/vw-macvirt-definitely-missing-kernel";

    for (int i = 0; i < 3; ++i) {
        int callbacks = 0;
        RunResult seen;
        engine.startAsync(invalid, [&](const RunResult& result) {
            ++callbacks;
            seen = result;
        });
        check(callbacks == 1, "startAsync invalid config invokes callback exactly once");
        check(!seen.ok, "startAsync invalid config fails");
        check(!seen.code.empty(), "startAsync invalid config reports a stable code");
        check(seen.code != "already-running", "startAsync does not wedge the engine");
    }

    int stopCallbacks = 0;
    engine.stopAsync([&](const RunResult&) { ++stopCallbacks; });
    check(stopCallbacks == 1, "stopAsync on idle engine invokes callback exactly once");

    // The engine remains usable after the async failures above.
    Config valid = linuxConfig("ppc64");
    valid.kernelPath = "/tmp/vw-macvirt-definitely-missing-kernel";
    const RunResult sync = engine.start(valid);
    check(!sync.ok, "sync start still refuses unsupported arch");
    check(sync.code != "already-running", "sync start not wedged by async failures");
}

// Repeated start/stop cycles on the *same* engine must be deterministic. Every
// cycle has to fail identically, the engine must never wedge in a busy state
// (which would surface as a spurious "already-running"), and a stop that follows
// a rejected start has to say not-running rather than inventing a success.
//
// Every config here is rejected during preflight, so this drives the lifecycle
// bookkeeping without ever touching Virtualization.framework.
void testRepeatedStartStopIsIdempotent() {
    constexpr int kCycles = 5;
    MacVirtualizationEngine engine;

    std::string firstStartCode;
    for (int i = 0; i < kCycles; ++i) {
        const std::string label = "cycle " + std::to_string(i);
        Config invalid = linuxConfig("aarch64");
        // A different missing path each cycle, so a result that accidentally
        // depended on one specific path's error text cannot pass by coincidence.
        invalid.kernelPath = "/tmp/vw-macvirt-missing-kernel-" + std::to_string(i);

        const RunResult started = engine.start(invalid);
        check(!started.ok, label + ": start rejected");
        check(!started.code.empty(), label + ": start reports a stable code");
        if (i == 0) {
            firstStartCode = started.code;
        }
        checkEqual(started.code, firstStartCode, label + ": repeated start returns an identical code");

        const RunResult stopped = engine.stop();
        check(!stopped.ok, label + ": stop after a rejected start fails");
        checkEqual(stopped.code, std::string("not-running"), label + ": stop reports not-running");
        checkEqual(std::string(stateName(engine.state())), std::string("not-started"),
                   label + ": engine is not wedged in a busy state");

        // Stopping twice in a row must be just as honest as the first time.
        const RunResult stoppedAgain = engine.stop();
        checkEqual(stoppedAgain.code, std::string("not-running"), label + ": repeated stop stays not-running");
    }

    // The engine still refuses to start a second guest after all of the above.
    Config crossArch = linuxConfig("x86_64");
    crossArch.kernelPath = "/tmp/vw-macvirt-missing-kernel-final";
    const RunResult afterCycles = engine.start(crossArch);
    check(!afterCycles.ok, "engine rejects a cross-arch guest after the lifecycle stress");
    check(afterCycles.code != std::string("already-running"),
          "lifecycle stress never left the engine claiming a running guest");
}

// A sandboxed process without com.apple.security.virtualization cannot create a
// VM at all, so the backend must not advertise host support for it. Before this
// invariant existed, probe() reported hostSupported/canStart true and the failure
// only surfaced at start() time, which is exactly what the shared contract
// forbids ("a backend that cannot actually run a config must say so in validate").
void testEntitlementHonesty() {
    Host sandboxed = syntheticHost("aarch64", 14, true, true);
    sandboxed.sandboxed = true;
    sandboxed.hasVirtualizationEntitlement = false;

    check(!hostMayCreateVirtualMachine(sandboxed),
          "sandboxed host without entitlement cannot create a VM");
    check(!hostSupportsGuestArch("aarch64", sandboxed),
          "sandboxed host without entitlement supports no guest arch");

    const ProbeResult probe = probeHost(sandboxed);
    check(!probe.hostSupported, "probe does not claim support without the entitlement");
    check(!probe.capabilities.canStart, "canStart is false without the entitlement");
    check(!probe.capabilities.canStop, "canStop is false without the entitlement");
    check(!probe.capabilities.linuxGuest.supported, "Linux guest is unsupported without entitlement");
    checkEqual(probe.capabilities.linuxGuest.code,
               "sandbox-entitlement-missing",
               "capability names the missing entitlement");
    check(!probe.capabilities.linuxGuest.reason.empty(), "capability explains the missing entitlement");
    check(hasCode(probe.diagnostics, "sandbox-entitlement-missing"),
          "diagnostics report the missing entitlement");
    check(!probe.summary.empty(), "summary is still produced");

    // The framework itself is present, so that axis stays truthful.
    check(probe.capabilities.frameworkAvailable, "framework availability is still reported");

    // And validate() must reject rather than let the registry pick this backend.
    const std::vector<Issue> issues = validateConfig(linuxConfig("aarch64"), sandboxed);
    check(hasErrors(issues), "sandboxed-without-entitlement config is rejected");

    // Granting the entitlement restores support.
    Host entitled = sandboxed;
    entitled.hasVirtualizationEntitlement = true;
    check(hostMayCreateVirtualMachine(entitled), "entitled sandbox can create a VM");
    check(probeHost(entitled).hostSupported, "entitled sandbox reports support");
    check(probeHost(entitled).capabilities.canStart, "entitled sandbox can start");

    // Not sandboxed at all: no entitlement needed.
    check(hostMayCreateVirtualMachine(syntheticHost("aarch64", 14, true, true)),
          "non-sandboxed host needs no entitlement");

    // The macOS-guest capability follows the same gate.
    check(!probeHost(sandboxed).capabilities.macOSGuestIPSW.supported,
          "macOS guest restore is gated by the entitlement too");
    checkEqual(probeHost(sandboxed).capabilities.macOSGuestIPSW.code,
               "sandbox-entitlement-missing",
               "macOS guest capability names the missing entitlement");
}

// Explicit same-architecture / cross-architecture matrix. There is no
// cross-architecture hardware acceleration anywhere, and 32-bit guests are a
// QEMU job even though QEMU could run them.
void testArchSupportMatrix() {
    struct Row {
        const char* hostArch;
        bool appleSilicon;
        const char* guestArch;
        bool expected;
    };
    const Row rows[] = {
        {"aarch64", true, "aarch64", true},
        {"aarch64", true, "arm64", true},
        {"aarch64", true, "x86_64", false},
        {"aarch64", true, "amd64", false},
        {"aarch64", true, "riscv64", false},
        {"aarch64", true, "i386", false},
        {"aarch64", true, "arm", false},
        {"x86_64", false, "x86_64", true},
        {"x86_64", false, "amd64", true},
        {"x86_64", false, "aarch64", false},
        {"x86_64", false, "arm64", false},
        {"x86_64", false, "riscv64", false},
        {"x86_64", false, "i386", false},
        {"x86_64", false, "ppc64", false},
    };
    for (const Row& row : rows) {
        const Host host = syntheticHost(row.hostArch, 14, true, row.appleSilicon);
        check(hostSupportsGuestArch(row.guestArch, host) == row.expected,
              std::string("matrix: host ") + row.hostArch + " guest " + row.guestArch
                  + (row.expected ? " supported" : " rejected"));
        if (row.expected) {
            check(nativeGuestArch(host) == normalizeArch(row.hostArch),
                  std::string("matrix: native arch for ") + row.hostArch);
        }
    }

    // Same result through the real host probe.
    const Host real = currentHost();
    const std::string native = nativeGuestArch(real);
    if (!native.empty()) {
        check(hostSupportsGuestArch(native, real), "real host supports its own ISA");
        const std::string other = native == "aarch64" ? "x86_64" : "aarch64";
        check(!hostSupportsGuestArch(other, real), "real host rejects the other ISA");
    }
}

// Every unsupported outcome must explain itself and be actionable, since the
// upper layer can only fall back if it is told to.
void testFallbackHints() {
    const Host apple = syntheticHost("aarch64", 14, true, true);
    const Host intel = syntheticHost("x86_64", 14, true, false);
    const Host old = syntheticHost("aarch64", 10, true, true);
    const Host noFramework = syntheticHost("aarch64", 14, false, true);
    const Host ppc = syntheticHost("ppc64", 14, true, false);
    Host noEntitlement = apple;
    noEntitlement.sandboxed = true;
    noEntitlement.hasVirtualizationEntitlement = false;

    // Every host in this list has an unusable Linux path, so the rejection must
    // always name the backend to fall back to. (Intel is deliberately absent:
    // there the Linux guest IS supported, so there is nothing to fall back from.)
    const Host hosts[] = {old, noFramework, ppc, noEntitlement};
    for (const Host& host : hosts) {
        const Capabilities caps = evaluateCapabilities(host);
        const Capability all[] = {caps.linuxGuest, caps.macOSGuestIPSW, caps.macOSGuestBoot,
                                  caps.networking, caps.directorySharing, caps.graphics, caps.audio};
        for (const Capability& capability : all) {
            if (!capability.supported) {
                check(!capability.reason.empty(), "unsupported capability has a reason");
                check(!capability.code.empty(), "unsupported capability has a stable code");
            }
        }
        check(!caps.linuxGuest.supported, "listed host cannot run a Linux guest natively");
        check(caps.linuxGuest.reason.find("QEMU") != std::string::npos,
              "Linux guest rejection names the QEMU fallback");
    }

    // Intel Macs do support same-arch Linux, so that capability carries no
    // fallback advice — it is not a rejection.
    const Capabilities intelCaps = evaluateCapabilities(intel);
    check(intelCaps.linuxGuest.supported, "Intel supports its own ISA for Linux");
    check(intelCaps.canStart, "Intel canStart is true");

    // Cross-architecture validation must name QEMU explicitly.
    const std::vector<Issue> cross = validateConfig(linuxConfig("x86_64"), apple);
    bool found = false;
    for (const Issue& issue : cross) {
        if (issue.code == "guest-arch-unsupported") {
            found = true;
            check(issue.message.find("QEMU") != std::string::npos, "cross-arch message names QEMU TCG");
        }
    }
    check(found, "cross-arch validation reported");
}

// The engine's lifecycle bookkeeping is guarded by the shared AsyncControl block.
// This drives the public API from several threads at once, including destroying
// engines while they are being used, so a regression in that locking protocol
// shows up under TSan/ASan. Every config here is rejected during preflight, so no
// VM is ever created and the framework is never touched.
void testConcurrentLifecycleSafety() {
    constexpr int kThreads = 4;
    constexpr int kIterations = 15;

    Config invalid = linuxConfig("ppc64");
    invalid.kernelPath = "/tmp/vw-macvirt-definitely-missing-kernel";

    std::atomic<int> started{0};
    std::atomic<int> rejected{0};
    std::atomic<int> callbacks{0};

    std::vector<std::thread> workers;
    workers.reserve(kThreads);
    for (int t = 0; t < kThreads; ++t) {
        workers.emplace_back([&]() {
            for (int i = 0; i < kIterations; ++i) {
                auto engine = std::make_unique<MacVirtualizationEngine>();
                (void)engine->state();
                (void)engine->probe();
                (void)engine->capabilities();
                (void)engine->hostProfile();
                (void)engine->validate(invalid);
                if (engine->start(invalid).ok) {
                    ++started;
                } else {
                    ++rejected;
                }
                engine->startAsync(invalid, [&](const RunResult&) { ++callbacks; });
                engine->stopAsync([&](const RunResult&) { ++callbacks; });
                (void)engine->stop();
                // Destroyed here while the other threads keep working.
            }
        });
    }
    for (std::thread& worker : workers) {
        worker.join();
    }

    check(started.load() == 0, "concurrency stress never started a VM");
    check(rejected.load() == kThreads * kIterations, "every concurrent start was rejected");
    check(callbacks.load() == kThreads * kIterations * 2,
          "each async call invoked its callback exactly once");
}

// ---------------------------------------------------------------------------
// Real host / engine tests (skippable)
// ---------------------------------------------------------------------------

void testRealHostProbe(const ProbeResult& probe) {
    if (!probe.hostSupported) {
        skip("real host probe: Virtualization.framework unavailable (" + probe.summary + ")");
        return;
    }
    const Host& host = probe.host;
    check(host.macOS, "real host is macOS");
    check(host.macOSMajor >= 11, "real host macOS >= 11");
    check(host.virtualizationFrameworkAvailable, "real host framework available");
    check(probe.capabilities.frameworkAvailable, "real capabilities framework available");
    check(probe.capabilities.linuxGuest.supported, "real host Linux guest supported");

    const std::string native = nativeGuestArch(host);
    check(!native.empty(), "real host has a native guest arch");
    check(hostSupportsGuestArch(native, host), "real host supports native arch");
    check(MacVirtualizationEngine::supportsGuestArch(native), "engine supports native arch");
    const std::string other = native == "aarch64" ? "x86_64" : "aarch64";
    check(!hostSupportsGuestArch(other, host), "real host rejects cross arch");
    check(!MacVirtualizationEngine::supportsGuestArch(other), "engine rejects cross arch");

    check(probe.capabilities.networking.supported, "real host networking supported");

    // Every unsupported capability must explain itself; this is the "never claim
    // silently" invariant.
    const Capability all[] = {
        probe.capabilities.linuxGuest,
        probe.capabilities.macOSGuestIPSW,
        probe.capabilities.macOSGuestBoot,
        probe.capabilities.networking,
        probe.capabilities.directorySharing,
        probe.capabilities.graphics,
        probe.capabilities.audio,
    };
    for (const Capability& capability : all) {
        if (!capability.supported) {
            check(!capability.reason.empty(), "unsupported capability has reason");
            check(!capability.code.empty(), "unsupported capability has code");
        }
    }
}

void testEngineOnRealHost(const ProbeResult& probe, TempTree& temp) {
    if (!probe.hostSupported) {
        skip("real host engine tests: Virtualization.framework unavailable");
        return;
    }
    MacVirtualizationEngine engine;

    // probe()/capabilities()/hostProfile() must agree with the free functions.
    const ProbeResult engineProbe = MacVirtualizationEngine::probe();
    check(engineProbe.hostSupported, "engine probe host supported");
    checkEqual(engineProbe.capabilities.hostArch,
               probe.capabilities.hostArch,
               "engine probe host arch");
    check(engine.hostProfile().macOS, "engine host profile is macOS");
    check(engine.capabilities().frameworkAvailable, "engine capabilities framework available");

    // validate() adds filesystem existence checks on top of the pure policy.
    Config valid = linuxConfig(nativeGuestArch(probe.host));
    valid.kernelPath = temp.file("kernel.img");
    valid.initrdPath = temp.file("initrd.img");
    valid.diskPaths.push_back(temp.file("disk.raw"));
    valid.directorySharingEnabled = true;
    valid.sharedDirectoryPath = temp.directory("share");
    const std::vector<Issue> validIssues = engine.validate(valid);
    check(!hasErrors(validIssues), "engine validate accepts existing files");

    Config missing = valid;
    missing.kernelPath = temp.path("does-not-exist-kernel");
    check(hasCode(engine.validate(missing), "kernel-missing"), "engine validate detects missing kernel");

    Config badDisk = valid;
    badDisk.diskPaths = {temp.path("missing-disk")};
    check(hasCode(engine.validate(badDisk), "disk-missing"), "engine validate detects missing disk");

    // start() must reject invalid configs without touching the framework.
    const RunResult invalidStart = engine.start(missing);
    check(!invalidStart.ok, "start invalid config fails");
    checkEqual(invalidStart.code, "config-invalid", "start invalid config code");
    check(!invalidStart.issues.empty(), "start invalid config carries issues");
    checkEqual(std::string(stateName(engine.state())), "not-started", "invalid start leaves state");

    // Cross-architecture start is refused with the fallback hint.
    const std::string other = probe.capabilities.hostArch == "aarch64" ? "x86_64" : "aarch64";
    Config cross = valid;
    cross.architecture = other;
    const RunResult crossStart = engine.start(cross);
    check(!crossStart.ok, "cross-arch start fails");
    checkEqual(crossStart.code, "guest-arch-unsupported", "cross-arch start code");
    check(crossStart.message.find("QEMU") != std::string::npos, "cross-arch message mentions QEMU");

    // macOS guest is either honestly unsupported (Apple Silicon, probe-only) or
    // rejected by validation (Intel / old macOS). It must never pretend to start.
    Config macGuest;
    macGuest.guestOs = "macos";
    macGuest.architecture = probe.capabilities.hostArch;
    macGuest.restoreFromIPSW = true;
    macGuest.ipswPath = temp.file("restore.ipsw");
    macGuest.cpuCount = 2;
    macGuest.memoryMb = 4096;
    const RunResult macStart = engine.start(macGuest);
    check(!macStart.ok, "macOS guest start never pretends to succeed");
    if (probe.capabilities.macOSGuestIPSW.supported) {
        checkEqual(macStart.code, "macos-ipsw-restore-unsupported", "macOS restore honest unsupported");
    } else {
        checkEqual(macStart.code, "config-invalid", "macOS guest rejected on this host");
    }

    // stop() without a running VM and its async twin.
    const RunResult stopResult = engine.stop();
    check(!stopResult.ok, "stop without start fails");
    checkEqual(stopResult.code, "not-running", "stop without start code");

    int stopCallbacks = 0;
    RunResult asyncStopResult;
    engine.stopAsync([&](const RunResult& result) {
        ++stopCallbacks;
        asyncStopResult = result;
    });
    check(stopCallbacks == 1, "stopAsync invokes callback exactly once");
    check(!asyncStopResult.ok, "stopAsync result is failure");
    checkEqual(asyncStopResult.code, "not-running", "stopAsync code");
    checkEqual(std::string(stateName(engine.state())), "not-started", "engine state after no-op stop");

    // Async validation errors are reported synchronously and once.
    int startCallbacks = 0;
    RunResult asyncStartResult;
    engine.startAsync(missing, [&](const RunResult& result) {
        ++startCallbacks;
        asyncStartResult = result;
    });
    check(startCallbacks == 1, "startAsync invalid config callback once");
    checkEqual(asyncStartResult.code, "config-invalid", "startAsync invalid config code");
}

// The hardest async-lifetime case: the engine is destroyed while a start worker
// is genuinely still in flight. The worker must notice `abandoned` and stop
// touching the engine, must not leak a running guest, and must report the
// documented `engine-destroyed` code.
//
// The test gate blocks the worker *before* it reaches Virtualization.framework,
// so the framework is never touched and no VM is ever created. Without the gate
// this path would be unreachable without booting a guest.
void testAsyncWorkerAbandonedOnDestruction(const ProbeResult& probe, TempTree& temp) {
    if (!probe.hostSupported) {
        skip("async abandonment: Virtualization.framework unavailable");
        return;
    }
    const std::string native = nativeGuestArch(probe.host);
    if (native.empty()) {
        skip("async abandonment: host has no native guest arch");
        return;
    }

    // A configuration that passes preflight, so the worker really is spawned.
    Config valid = linuxConfig(native);
    valid.kernelPath = temp.file("abandon-kernel.img");

    std::mutex gateMutex;
    std::condition_variable gateCv;
    bool gateEntered = false;
    bool gateRelease = false;

    std::mutex doneMutex;
    std::condition_variable doneCv;
    bool done = false;
    int callbackCount = 0;
    RunResult observed;

    {
        auto engine = std::make_unique<MacVirtualizationEngine>();
        engine->setStartGateForTesting([&](const Config&, RunResult* abortResult) {
            if (abortResult) {
                *abortResult = RunResult::failure("gate-abort", "测试闸门中止");
            }
            std::unique_lock<std::mutex> lock(gateMutex);
            gateEntered = true;
            gateCv.notify_all();
            gateCv.wait(lock, [&]() { return gateRelease; });
            return false;  // abort: never reach Virtualization.framework
        });

        engine->startAsync(valid, [&](const RunResult& result) {
            std::lock_guard<std::mutex> lock(doneMutex);
            ++callbackCount;
            observed = result;
            done = true;
            doneCv.notify_all();
        });

        // Wait until the worker is parked inside the gate, then let the engine go
        // out of scope. Its destructor sets `abandoned` while the worker is still
        // in flight — exactly the window the AsyncControl protocol covers.
        {
            std::unique_lock<std::mutex> lock(gateMutex);
            const bool entered = gateCv.wait_for(lock, std::chrono::seconds(10), [&]() {
                return gateEntered;
            });
            check(entered, "async worker reached the start gate");
            if (!entered) {
                gateRelease = true;
            }
        }
    }  // <-- engine destroyed here, worker still blocked in the gate

    // Release the worker. It must complete without touching the freed engine;
    // ASan fails the suite if it does.
    {
        std::lock_guard<std::mutex> lock(gateMutex);
        gateRelease = true;
    }
    gateCv.notify_all();

    {
        std::unique_lock<std::mutex> lock(doneMutex);
        const bool finished = doneCv.wait_for(lock, std::chrono::seconds(10), [&]() { return done; });
        check(finished, "abandoned worker still invoked its completion");
    }
    check(callbackCount == 1, "abandoned start invoked its callback exactly once");
    checkEqual(observed.code, "engine-destroyed", "abandoned start reports engine-destroyed");
    check(!observed.ok, "abandoned start is reported as a failure");
    check(!observed.message.empty(), "abandoned start explains itself");
}

void testDescriptorThroughRealHost(const ProbeResult& probe) {
    const BackendDescriptor& descriptor = backendDescriptor();
    check(descriptor.isHostSupported() == probe.hostSupported,
          "descriptor isHostSupported matches probe");
    const ProbeResult descriptorProbe = descriptor.probe();
    check(descriptorProbe.hostSupported == probe.hostSupported, "descriptor probe matches");
    const std::string native = nativeGuestArch(probe.host);
    if (!native.empty()) {
        check(descriptor.supportsGuestArch(native), "descriptor supports native arch");
    }

    Config bad;
    bad.guestOs = "linux";
    bad.architecture = "ppc64";
    bad.kernelPath = "/tmp/vw-macvirt-descriptor-kernel";
    const std::vector<Issue> issues = descriptor.validate(bad);
    check(hasCode(issues, "guest-arch-unsupported"), "descriptor validate cross-arch");
}

} // namespace

// ---------------------------------------------------------------------------
// Chaos: deterministic lifecycle stress
// ---------------------------------------------------------------------------
//
// Every operation below is driven by a *fixed-seed* pseudo-random sequence, so
// the interleaving is reproducible run to run: the same seed always produces the
// same order of start / stop / destroy / invalidate, on any host, under Release,
// ASan or TSan. Nothing depends on accidental thread scheduling — each async
// operation is waited for on a condition variable before the next one starts —
// and no guest is ever booted.
//
// Reported parameters: kChaosRounds rounds, kChaosThreads concurrent stoppers.

constexpr int kChaosRounds = 60;
constexpr int kChaosThreads = 8;
constexpr int kChaosSeed = 0x5EED1234;

// A gate abort returns a non-ok result and leaves the engine in Error (only a
// preflight rejection leaves it untouched at NotStarted). Both are "idle" in the
// sense that no guest is busy starting/running/stopping, which is the invariant
// the chaos loops actually care about.
static bool chaosStateIdle(const std::string& name) {
    return name != "starting" && name != "running" && name != "stopping";
}

// Builds a config that clears the filesystem preflight, so the start path reaches
// the real gate / stub seam instead of being rejected before it gets there. No
// guest is ever booted. This does not weaken production validation: the same
// checks still run; we only hand them a file that exists.
static Config validGuestConfig(TempTree& temp, const std::string& tag) {
    Config config = linuxConfig("aarch64");
    config.kernelPath = temp.file(tag);
    return config;
}

// A tiny LCG. Kept local and deterministic on purpose: the stress order must not
// depend on <random> implementations, which are free to change between toolchains.
int chaosNext(int* state, int bound) {
    *state = (static_cast<long long>(*state) * 1103515245LL + 12345LL) & 0x7FFFFFFF;
    return (*state % bound);
}

// Waits for `pending` to become false, or fails the check. Every async op in the
// chaos loops is awaited through this, which is what keeps the run deterministic
// instead of racing.
bool chaosWaitFor(std::mutex& mutex, std::condition_variable& cv, const std::function<bool()>& done,
                  const std::string& label) {
    std::unique_lock<std::mutex> lock(mutex);
    return cv.wait_for(lock, std::chrono::seconds(10), done) ? true
                                                             : (check(false, label + ": timed out"), false);
}

// The repeatable core of the chaos suite: interleave start / startAsync / stop /
// stopAsync / destroy in a seed-fixed order and assert that the engine is never
// wedged, never invents a success, and calls every async completion exactly once.
void testChaosDeterministicLifecycleLoop() {
    int seed = kChaosSeed;
    int callbacks = 0;
    int asyncIssued = 0;
    int wedgeFreeRounds = 0;
    TempTree temp;

    for (int round = 0; round < kChaosRounds; ++round) {
        const int op = chaosNext(&seed, 5);
        const std::string label = "chaos round " + std::to_string(round) + " op " + std::to_string(op);

        MacVirtualizationEngine engine;
        // A gate that always aborts: the worker never reaches
        // Virtualization.framework, so the run is bounded and reproducible while
        // still exercising the real async bookkeeping.
        engine.setStartGateForTesting([](const Config&, RunResult* abortResult) {
            if (abortResult) {
                *abortResult = RunResult::failure("chaos-gate", "混沌闸门中止");
            }
            return false;
        });

        const std::string stateBefore = std::string(stateName(engine.state()));
        check(stateBefore == "not-started", label + ": fresh engine starts not-started");

        // Reaching the gate requires a config that survives preflight; a missing
        // kernel path would be rejected before the gate and would not exercise it.
        const Config valid = validGuestConfig(temp, "loop-" + std::to_string(round) + ".img");

        if (op == 0) {
            const RunResult started = engine.start(valid);
            check(!started.ok, label + ": sync start rejected at the gate");
            check(chaosStateIdle(std::string(stateName(engine.state()))),
                  label + ": aborted sync start leaves the engine idle");
        } else if (op == 1) {
            ++asyncIssued;
            std::mutex mutex;
            std::condition_variable cv;
            int local = 0;
            engine.startAsync(valid, [&](const RunResult& result) {
                {
                    std::lock_guard<std::mutex> lock(mutex);
                    ++local;
                    ++callbacks;
                    // Read every captured reference before notify_all(): the
                    // callback runs on a worker thread that may still be inside
                    // the lambda after the main thread returns from chaosWaitFor
                    // and tears down this loop iteration's stack frame.
                    check(!result.ok, label + ": async start reported a failure");
                    check(!result.code.empty(), label + ": async start reports a stable code");
                }
                cv.notify_all();
            });
            chaosWaitFor(mutex, cv, [&]() { return local == 1; }, label + ": async start");
            check(chaosStateIdle(std::string(stateName(engine.state()))),
                  label + ": aborted async start leaves the engine idle");
        } else if (op == 2) {
            const RunResult stopped = engine.stop();
            checkEqual(stopped.code, std::string("not-running"), label + ": stop on idle engine");
        } else if (op == 3) {
            ++asyncIssued;
            std::mutex mutex;
            std::condition_variable cv;
            int local = 0;
            engine.stopAsync([&](const RunResult& result) {
                {
                    std::lock_guard<std::mutex> lock(mutex);
                    ++local;
                    ++callbacks;
                    checkEqual(result.code, std::string("not-running"),
                               label + ": stopAsync on idle engine reports not-running");
                }
                cv.notify_all();
            });
            chaosWaitFor(mutex, cv, [&]() { return local == 1; }, label + ": async stop");
        } else {
            // Destroy an engine that was never started; the destructor must not
            // touch the framework and must not leave anything behind.
            checkEqual(std::string(stateName(engine.state())), std::string("not-started"),
                       label + ": untouched engine is not-started before destruction");
        }

        const std::string stateAfter = std::string(stateName(engine.state()));
        const bool idle = chaosStateIdle(stateAfter);
        check(idle, label + ": engine never wedges into a busy state");
        if (idle) {
            ++wedgeFreeRounds;
        }
    }

    check(callbacks == asyncIssued,
          "chaos loop invoked exactly one completion per issued async operation");
    check(wedgeFreeRounds == kChaosRounds, "chaos loop left every engine idle");
}

// N threads hammering stop() on the same idle engine. Every one of them must
// report "not-running" — a re-entrant stop must never be reported as a success
// and must never race the session teardown (ASan/TSan fail the suite if it does).
void testChaosConcurrentStopReentrancy() {
    for (int round = 0; round < 5; ++round) {
        MacVirtualizationEngine engine;
        std::mutex mutex;
        std::condition_variable cv;
        std::vector<int> results(kChaosThreads, 0);
        int done = 0;

        std::vector<std::thread> threads;
        for (int thread = 0; thread < kChaosThreads; ++thread) {
            threads.emplace_back([&, thread]() {
                const RunResult result = engine.stop();
                {
                    std::lock_guard<std::mutex> lock(mutex);
                    results[thread] = result.ok ? 1 : (result.code == "not-running" ? -1 : 2);
                    ++done;
                }
                cv.notify_all();
            });
        }
        for (std::thread& thread : threads) {
            thread.join();
        }
        checkEqual(done, kChaosThreads, "concurrent stop: every thread reported");
        int notRunning = 0;
        for (const int outcome : results) {
            check(outcome != 2, "concurrent stop: no stop invented another outcome");
            if (outcome == -1) {
                ++notRunning;
            }
        }
        checkEqual(notRunning, kChaosThreads,
                   "concurrent stop: every stop reports not-running, exactly once per thread");
        checkEqual(std::string(stateName(engine.state())), std::string("not-started"),
                   "concurrent stop: engine stays idle");
    }
}

// A late/aborted start must not wedge the engine, must still invoke its
// completion exactly once, and a start issued afterwards must reach the gate
// again rather than being answered from a stale "already-running".
void testChaosLateCallbackAndReentrantStart() {
    TempTree temp;
    Config valid = linuxConfig("aarch64");
    valid.kernelPath = temp.file("late-callback-kernel.img");
    MacVirtualizationEngine engine;
    engine.setStartGateForTesting([](const Config&, RunResult* abortResult) {
        if (abortResult) {
            *abortResult = RunResult::failure("chaos-gate", "混沌闸门中止");
        }
        return false;
    });

    for (int round = 0; round < 4; ++round) {
        std::mutex mutex;
        std::condition_variable cv;
        int calls = 0;
        RunResult seen;
        engine.startAsync(valid, [&](const RunResult& result) {
            {
                std::lock_guard<std::mutex> lock(mutex);
                ++calls;
                seen = result;
            }
            cv.notify_all();
        });
        chaosWaitFor(mutex, cv, [&]() { return calls == 1; }, "late callback round");
        checkEqual(calls, 1, "late-abort start invoked its completion exactly once");
        checkEqual(seen.code, std::string("chaos-gate"), "late-abort start reports the gate code");
        check(!seen.message.empty(), "late-abort start explains itself");

        engine.startAsync(valid, [](const RunResult&) {});
        check(chaosStateIdle(std::string(stateName(engine.state()))),
              "late-abort start leaves the engine idle");
    }
}

// After a failed build the runner handle must have no owner that outlives the
// engine: stopping, stopping twice, and destroying in every order must stay
// clean and must never report a success it did not earn.
void testChaosFailBuildReferenceOwnership() {
    for (int round = 0; round < 10; ++round) {
        const std::string label = "fail-build round " + std::to_string(round);
        MacVirtualizationEngine engine;
        Config invalid = linuxConfig("aarch64");
        invalid.kernelPath = "/tmp/vw-macvirt-chaos-missing-" + std::to_string(round);

        const RunResult started = engine.start(invalid);
        check(!started.ok, label + ": bad build start fails");
        check(!started.code.empty(), label + ": bad build start reports a code");

        const RunResult stopped = engine.stop();
        checkEqual(stopped.code, std::string("not-running"), label + ": stop after bad build");
        const RunResult stoppedTwice = engine.stop();
        checkEqual(stoppedTwice.code, std::string("not-running"), label + ": stop stays not-running");

        std::mutex mutex;
        std::condition_variable cv;
        int calls = 0;
        engine.stopAsync([&](const RunResult& result) {
            {
                std::lock_guard<std::mutex> lock(mutex);
                ++calls;
            }
            cv.notify_all();
        });
        chaosWaitFor(mutex, cv, [&]() { return calls == 1; }, label + ": async stop after bad build");
    }
}

#if VW_MACVIRT_HAS_FRAMEWORK
// Regression stress for the Session::runnerRaw ownership fix (see
// MacVirtualizationEngine.mm).
//
// The counted stand-in is installed through the production install path, so the
// stop / ownership-transfer / release / destructor arithmetic running in this
// loop is the real code; only the Virtualization.framework handshake is
// replaced. The invariant is exact: every stand-in installed by start() must be
// destroyed exactly once. A shortfall is a leaked +1; an overflow is the double
// release that used to happen while vwStopSession() read the field unlocked and
// ~Session() freed the same reference.
void testChaosRunnerOwnershipInvariant() {
    constexpr int kOwnerRounds = 12;
    constexpr int kOwnerThreads = 4;

    // The chaos seam sits *after* preflight, so the configuration has to pass the
    // filesystem validation first: a kernel path that does not exist is rejected
    // before the gate is ever reached. Real (empty) files, no guest.
    TempTree temp;

    int seed = kChaosSeed ^ 0xABCDEF;
    for (int round = 0; round < kOwnerRounds; ++round) {
        const int op = chaosNext(&seed, 3);
        Config valid = linuxConfig("aarch64");
        valid.kernelPath = temp.file("chaos-kernel-" + std::to_string(round) + ".img");

        if (op == 0) {
            // start -> stop: the ownership transfer path. A clean stop releases the
            // stand-in exactly once, so installed == destroyed at the end of round.
            MacVirtualizationEngine running;
            running.setRunnerStubForTesting(true);
            const RunResult started = running.start(valid);
            check(started.ok, "runner stub: sync start succeeds without the framework");
            checkEqual(std::string(stateName(running.state())), std::string("running"),
                       "runner stub: engine reports running");
            const RunResult stopped = running.stop();
            check(stopped.ok, "runner stub: stop releases the stand-in");
            checkEqual(std::string(stateName(running.state())), std::string("stopped"),
                       "runner stub: engine reports stopped after stop");
        } else if (op == 1) {
            // start -> destroy with no stop: the ~Session() release path.
            MacVirtualizationEngine running;
            running.setRunnerStubForTesting(true);
            const RunResult started = running.start(valid);
            check(started.ok, "runner stub: sync start (destroy-only round)");
        } else {
            // startAsync -> destroy while the worker is in flight: the abandoned
            // worker must roll the stand-in back exactly once.
            std::mutex mutex;
            std::condition_variable cv;
            int calls = 0;
            {
                auto running = std::make_unique<MacVirtualizationEngine>();
                running->setRunnerStubForTesting(true);
                running->startAsync(valid, [&](const RunResult& result) {
                    {
                        std::lock_guard<std::mutex> lock(mutex);
                        ++calls;
                        checkEqual(result.code, std::string("engine-destroyed"),
                                   "abandoned runner stub reports engine-destroyed");
                    }
                    cv.notify_all();
                });
            }
            chaosWaitFor(mutex, cv, [&]() { return calls == 1; }, "abandoned runner stub");
        }

        // Attribute the invariant to a specific round, so a regression is reported
        // with the round that broke it instead of only at the end of the loop.
        MacVirtualizationEngine counter;
        check(counter.runnerStubInstalled() == counter.runnerStubDestroyed(),
              "runner stub: installed == destroyed at the end of " + std::to_string(round));
    }

    MacVirtualizationEngine probe;
    const int installed = probe.runnerStubInstalled();
    const int destroyed = probe.runnerStubDestroyed();
    check(installed > 0, "runner stub: the loop installed at least one stand-in");
    check(installed == destroyed,
          "runner stub: every installed stand-in was destroyed exactly once");

    // N threads stopping a running guest at once: no invented success, no
    // double release, and the engine ends stopped rather than wedged.
    for (int round = 0; round < 3; ++round) {
        Config storm = linuxConfig("aarch64");
        storm.kernelPath = temp.file("storm-kernel-" + std::to_string(round) + ".img");

        MacVirtualizationEngine running;
        running.setRunnerStubForTesting(true);
        const RunResult started = running.start(storm);
        check(started.ok, "runner stub: storm round start");

        std::mutex mutex;
        std::condition_variable cv;
        std::vector<int> outcomes(kOwnerThreads, 0);
        int done = 0;
        std::vector<std::thread> threads;
        for (int thread = 0; thread < kOwnerThreads; ++thread) {
            threads.emplace_back([&, thread]() {
                const RunResult result = running.stop();
                {
                    std::lock_guard<std::mutex> lock(mutex);
                    outcomes[thread] = result.ok ? 1 : 0;
                    ++done;
                }
                cv.notify_all();
            });
        }
        for (std::thread& thread : threads) {
            thread.join();
        }
        checkEqual(done, kOwnerThreads, "runner stub: every stopper reported");
        int succeeded = 0;
        for (const int outcome : outcomes) {
            succeeded += outcome;
        }
        check(succeeded >= 1, "runner stub: at least one stop succeeded");
        check(succeeded < kOwnerThreads, "runner stub: a running guest is not stopped kOwnerThreads times");
        const std::string state = std::string(stateName(running.state()));
        check(state == "stopped" || state == "error",
              "runner stub: engine is not wedged after the stop storm");
    }

    probe.setRunnerStubForTesting(false);
}
#endif  // VW_MACVIRT_HAS_FRAMEWORK

int main() {
    std::fprintf(stdout,
                 "MacVirtualizationEngineTests (VW_MACVIRT_HAS_FRAMEWORK=%d)\n",
                 VW_MACVIRT_HAS_FRAMEWORK);

    testIdentity();
    testArchNormalization();
    testAppleSiliconPolicy();
    testIntelPolicy();
    testMacOSVersionGating();
    testNonMacHost();
    testDiagnostics();
    testValidateLinux();
    testValidateMacOSGuest();
    testRunResultHelpers();
    testIssueFormatting();
    testReasonOutParameters();
    testCanStartHonesty();
    testGuestClassification();
    testUnsupportedHostArchitecture();
    testMacOSGuestHonesty();
    testEngineConstructionAndLifetime();
    testAsyncCompletionContract();
    testRepeatedStartStopIsIdempotent();
    testEntitlementHonesty();
    testArchSupportMatrix();
    testFallbackHints();
    testConcurrentLifecycleSafety();
    testChaosDeterministicLifecycleLoop();
    testChaosConcurrentStopReentrancy();
    testChaosLateCallbackAndReentrantStart();
    testChaosFailBuildReferenceOwnership();
#if VW_MACVIRT_HAS_FRAMEWORK
    testChaosRunnerOwnershipInvariant();
#endif

    const ProbeResult probe = MacVirtualizationEngine::probe();
    std::fprintf(stdout, "Host: arch=%s os=%s macOS=%d framework=%d sandboxed=%d entitlement=%d\n",
                 probe.host.arch.c_str(),
                 probe.host.osVersion.c_str(),
                 probe.host.macOSMajor,
                 probe.host.virtualizationFrameworkAvailable ? 1 : 0,
                 probe.host.sandboxed ? 1 : 0,
                 probe.host.hasVirtualizationEntitlement ? 1 : 0);
    std::fprintf(stdout, "Summary: %s\n", probe.summary.c_str());

    testRealHostProbe(probe);
    testDescriptorThroughRealHost(probe);

    TempTree temp;
    testEngineOnRealHost(probe, temp);
    testAsyncWorkerAbandonedOnDestruction(probe, temp);

    std::fprintf(stdout,
                 "Checks: %d, failures: %d, skips: %d\n",
                 g_checks,
                 g_failures,
                 g_skips);
    if (g_failures != 0) {
        std::fprintf(stderr, "%d check(s) failed.\n", g_failures);
        return 1;
    }
    return 0;
}
