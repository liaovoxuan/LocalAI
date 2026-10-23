// WindowsHypervisorEngineTests — logic, probe-injection and failure-path tests
// for the Windows Hypervisor Platform (WHPX) backend.
//
// Design of this suite:
//
//   * Nothing here launches a guest, and nothing needs a Windows host. The
//     whole decision surface is reached by injecting a scripted
//     WindowsEnvironment, which is exactly the seam the engine was built
//     around.
//   * The suite is deliberately free of the heavy QEMU plumbing: it links only
//     Qt Core plus the shared capability/platform/config translation units, so
//     it builds and runs on macOS, Linux and Windows.
//   * The honesty invariants are asserted directly:
//       - a non-Windows host must never be reported as supported;
//       - cross-architecture guests are REJECTED (supportsGuestArch() false,
//         validate() -> guest-arch-unsupported), not silently degraded to TCG,
//         so the registry can fall through to qemu-tcg;
//       - the backend tier is ALWAYS Native: it names the mechanism the backend
//         wraps (the WHPX host hypervisor API), not the outcome on this host.
//         The honest per-host/per-guest answer lives in
//         accelerator / nativeExecution / executionTierFor();
//       - ARM64 WHPX is reported as "degraded" with a warning;
//       - start() must never return true without a real, positive process id;
//       - the backend never claims cross-architecture hardware acceleration.
//
// Anything that can only be checked on a real host (the real probe against the
// real platform layer) lives in the separate integration check documented in
// docs/windows-virtualization.md, not here.

#include "WindowsHypervisorEngine.hpp"

#include "VirtualizationEngineRegistry.hpp"

#include <QCoreApplication>
#include <QStringList>

#include <cstdio>

namespace {

using namespace vw;

int g_checks = 0;
int g_failures = 0;
int g_skips = 0;

void check(bool condition, const QString& label) {
    ++g_checks;
    if (!condition) {
        ++g_failures;
        std::fprintf(stderr, "FAIL: %s\n", label.toUtf8().constData());
    }
}

void checkEqual(const QString& actual, const QString& expected, const QString& label) {
    ++g_checks;
    if (actual != expected) {
        ++g_failures;
        std::fprintf(stderr,
                     "FAIL: %s (expected \"%s\", got \"%s\")\n",
                     label.toUtf8().constData(),
                     expected.toUtf8().constData(),
                     actual.toUtf8().constData());
    }
}

void checkEqual(int actual, int expected, const QString& label) {
    ++g_checks;
    if (actual != expected) {
        ++g_failures;
        std::fprintf(stderr,
                     "FAIL: %s (expected %d, got %d)\n",
                     label.toUtf8().constData(),
                     expected,
                     actual);
    }
}

void skip(const QString& label) {
    ++g_skips;
    std::fprintf(stdout, "SKIP: %s\n", label.toUtf8().constData());
}

// ---------------------------------------------------------------------------
// Scripted environment
// ---------------------------------------------------------------------------

class FakeWindowsEnvironment : public WindowsEnvironment {
public:
    WindowsHostSnapshot snap;
    QString programValue = QStringLiteral("C:\\QEMU\\qemu-system-x86_64.exe");
    QStringList argumentsValue = {QStringLiteral("-machine"), QStringLiteral("q35,accel=whpx")};
    bool startResult = true;
    qint64 startPid = 4242;
    QString startError;
    bool stopResult = true;
    QString stopError;

    mutable int snapshotCalls = 0;
    mutable int programCalls = 0;
    mutable int argumentCalls = 0;
    int startCalls = 0;
    int stopCalls = 0;
    // Recorded so tests can assert the accelerator decision the engine applied
    // *before* handing the configuration to the environment.
    mutable VmConfig lastProgramConfig;
    mutable VmConfig lastArgumentConfig;
    VmConfig lastStartConfig;
    qint64 lastStoppedPid = 0;

    WindowsHostSnapshot snapshot() const override {
        ++snapshotCalls;
        return snap;
    }
    QString program(const VmConfig& config) const override {
        ++programCalls;
        lastProgramConfig = config;
        return programValue;
    }
    QStringList arguments(const VmConfig& config) const override {
        ++argumentCalls;
        lastArgumentConfig = config;
        return argumentsValue;
    }
    bool startProcess(const VmConfig& config, qint64* processId, QString* error) override {
        ++startCalls;
        lastStartConfig = config;
        if (!startResult) {
            if (error) {
                *error = startError;
            }
            return false;
        }
        if (processId) {
            *processId = startPid;
        }
        return true;
    }
    bool stopProcess(qint64 processId, QString* error) override {
        ++stopCalls;
        lastStoppedPid = processId;
        if (!stopResult) {
            if (error) {
                *error = stopError;
            }
            return false;
        }
        return true;
    }
};

// ---------------------------------------------------------------------------
// Snapshot builders
// ---------------------------------------------------------------------------

WindowsHostSnapshot windowsHost(const QStringList& runnableArches = {QStringLiteral("x86_64")},
                                 const QString& hostArch = QStringLiteral("x86_64")) {
    WindowsHostSnapshot snap;
    snap.hostIsWindows = true;
    snap.hostFamily = QStringLiteral("windows");
    snap.hostName = QStringLiteral("Windows");
    snap.hostArch = hostArch;
    snap.apiLoadable = true;
    snap.hypervisorPresent = true;
    snap.whpxUsable = true;
    snap.qemuFound = !runnableArches.isEmpty();
    snap.qemuExecutable = !runnableArches.isEmpty();
    snap.qemuPath = QStringLiteral("C:\\QEMU\\qemu-system-x86_64.exe");
    snap.runnableGuestArches = runnableArches;
    snap.detail = QStringLiteral("test snapshot");
    return snap;
}

WindowsHostSnapshot nonWindowsHost() {
    WindowsHostSnapshot snap;
    snap.hostIsWindows = false;
    snap.hostFamily = QStringLiteral("darwin");
    snap.hostName = QStringLiteral("macOS");
    snap.hostArch = QStringLiteral("aarch64");
    return snap;
}

WindowsHostSnapshot whpxApiMissingHost(const QStringList& runnableArches = {QStringLiteral("x86_64")}) {
    WindowsHostSnapshot snap = windowsHost(runnableArches);
    snap.apiLoadable = false;
    snap.hypervisorPresent = false;
    snap.whpxUsable = false;
    snap.reason = QStringLiteral("winhvplatform.dll not found");
    return snap;
}

WindowsHostSnapshot whpxHypervisorNotPresentHost(const QStringList& runnableArches = {QStringLiteral("x86_64")}) {
    WindowsHostSnapshot snap = windowsHost(runnableArches);
    snap.apiLoadable = true;
    snap.hypervisorPresent = false;
    snap.whpxUsable = false;
    snap.reason = QStringLiteral("Hyper-V not enabled");
    return snap;
}

WindowsHostSnapshot whpxDegradedHost(const QStringList& runnableArches = {QStringLiteral("aarch64")}) {
    WindowsHostSnapshot snap = windowsHost(runnableArches, QStringLiteral("aarch64"));
    // ARM64 WHPX: apiLoadable=true, hypervisorPresent=true, whpxUsable=true
    // but the state machine will classify it as WhpxDegraded.
    return snap;
}

WindowsHostSnapshot runtimeMissingHost() {
    WindowsHostSnapshot snap;
    snap.hostIsWindows = true;
    snap.hostFamily = QStringLiteral("windows");
    snap.hostName = QStringLiteral("Windows");
    snap.hostArch = QStringLiteral("x86_64");
    snap.apiLoadable = true;
    snap.hypervisorPresent = true;
    snap.whpxUsable = true;
    snap.qemuFound = false;
    snap.qemuExecutable = false;
    snap.runnableGuestArches = QStringList{};
    snap.detail = QStringLiteral("no QEMU");
    return snap;
}

VmConfig guestConfig(const QString& arch = QStringLiteral("x86_64")) {
    VmConfig config;
    config.guestOs = QStringLiteral("windows");
    config.architecture = arch;
    config.machine = QStringLiteral("q35");
    config.cpuModel = QStringLiteral("max");
    config.cpuCores = 4;
    config.memoryMb = 4096;
    DiskConfig disk;
    disk.path = QStringLiteral("C:\\VMs\\vw-test-disk.qcow2");
    disk.interfaceName = QStringLiteral("virtio");
    disk.format = QStringLiteral("qcow2");
    config.disks.push_back(disk);
    return config;
}

bool hasIssue(const QVector<EngineIssue>& issues, const QString& code) {
    return hasEngineCode(issues, code);
}

bool hasIssueAtLevel(const QVector<EngineIssue>& issues, const QString& code, EngineIssueLevel level) {
    for (const EngineIssue& issue : issues) {
        if (issue.code == code && issue.level == level) {
            return true;
        }
    }
    return false;
}

// ---------------------------------------------------------------------------
// Identity and architecture policy
// ---------------------------------------------------------------------------

void testIdentity() {
    checkEqual(backend_id::windowsHypervisor(), QStringLiteral("windows-hypervisor"),
               "shared backend id constant");

    FakeWindowsEnvironment environment;
    environment.snap = windowsHost();
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));
    checkEqual(engine.backendId(), backend_id::windowsHypervisor(), "engine backend id");
    check(!engine.displayName().isEmpty(), "engine display name is set");
    check(engine.displayName().contains(QStringLiteral("WHPX"))
              || engine.displayName().contains(QStringLiteral("Hypervisor")),
          "display name mentions WHPX or Hypervisor");
    check(!engine.runsInProcess(), "WHPX does not run in-process");
}

void testCandidateArchitectures() {
    checkEqual(windowsCanonicalGuestArch(QStringLiteral("x86_64")), QStringLiteral("x86_64"),
               "x86_64 accepted");
    checkEqual(windowsCanonicalGuestArch(QStringLiteral("amd64")), QStringLiteral("x86_64"),
               "amd64 canonicalizes to x86_64");
    checkEqual(windowsCanonicalGuestArch(QStringLiteral("aarch64")), QStringLiteral("aarch64"),
               "aarch64 accepted");
    checkEqual(windowsCanonicalGuestArch(QStringLiteral("arm64")), QStringLiteral("aarch64"),
               "arm64 canonicalizes to aarch64");

    // riscv64 is not a candidate for this backend.
    check(windowsCanonicalGuestArch(QStringLiteral("riscv64")).isEmpty(), "riscv64 rejected");
    check(windowsCanonicalGuestArch(QStringLiteral("ppc64")).isEmpty(), "ppc64 rejected");
    check(windowsCanonicalGuestArch(QString()).isEmpty(), "empty arch rejected");

    const QStringList candidates = windowsCandidateGuestArches();
    checkEqual(candidates.size(), 2, "two candidate architectures");
    check(candidates.contains(QStringLiteral("x86_64")), "x86_64 is a candidate");
    check(candidates.contains(QStringLiteral("aarch64")), "aarch64 is a candidate");
}

// ---------------------------------------------------------------------------
// Host state machine
// ---------------------------------------------------------------------------

void testStateMachine() {
    // NotWindowsHost
    check(windowsHostState(nonWindowsHost()) == WindowsHostState::NotWindowsHost,
          "non-windows host state");
    check(!windowsStateIsUsable(WindowsHostState::NotWindowsHost), "non-windows is not usable");
    check(!windowsStateIsNative(WindowsHostState::NotWindowsHost), "non-windows is not native");
    checkEqual(windowsStateCode(WindowsHostState::NotWindowsHost),
               QStringLiteral("host-unsupported"), "non-windows code");

    // WhpxApiMissing
    check(windowsHostState(whpxApiMissingHost()) == WindowsHostState::WhpxApiMissing,
          "api-missing state");
    check(!windowsStateIsUsable(WindowsHostState::WhpxApiMissing), "api-missing is not usable");
    check(!windowsStateIsNative(WindowsHostState::WhpxApiMissing), "api-missing is not native");
    check(windowsStateIsTcgFallback(WindowsHostState::WhpxApiMissing), "api-missing is tcg fallback");
    checkEqual(windowsStateCode(WindowsHostState::WhpxApiMissing),
               QStringLiteral("whpx-api-missing"), "api-missing code");

    // WhpxHypervisorNotPresent
    check(windowsHostState(whpxHypervisorNotPresentHost()) == WindowsHostState::WhpxHypervisorNotPresent,
          "hypervisor-not-present state");
    check(!windowsStateIsUsable(WindowsHostState::WhpxHypervisorNotPresent), "hypervisor-not-present is not usable");
    check(!windowsStateIsNative(WindowsHostState::WhpxHypervisorNotPresent), "hypervisor-not-present is not native");
    check(windowsStateIsTcgFallback(WindowsHostState::WhpxHypervisorNotPresent), "hypervisor-not-present is tcg fallback");
    checkEqual(windowsStateCode(WindowsHostState::WhpxHypervisorNotPresent),
               QStringLiteral("whpx-hypervisor-not-present"), "hypervisor-not-present code");

    // WhpxDegraded (ARM64)
    check(windowsHostState(whpxDegradedHost()) == WindowsHostState::WhpxDegraded,
          "degraded state for ARM64");
    check(windowsStateIsUsable(WindowsHostState::WhpxDegraded), "degraded is usable");
    check(windowsStateIsNative(WindowsHostState::WhpxDegraded), "degraded is native");
    check(!windowsStateIsTcgFallback(WindowsHostState::WhpxDegraded), "degraded is not tcg fallback");
    checkEqual(windowsStateCode(WindowsHostState::WhpxDegraded),
               QStringLiteral("whpx-degraded"), "degraded code");

    // WhpxAvailable
    check(windowsHostState(windowsHost()) == WindowsHostState::WhpxAvailable,
          "available state for x86_64");
    check(windowsStateIsUsable(WindowsHostState::WhpxAvailable), "available is usable");
    check(windowsStateIsNative(WindowsHostState::WhpxAvailable), "available is native");
    check(!windowsStateIsTcgFallback(WindowsHostState::WhpxAvailable), "available is not tcg fallback");
    checkEqual(windowsStateCode(WindowsHostState::WhpxAvailable),
               QStringLiteral("available"), "available code");

    // Every state message must be non-empty.
    const WindowsHostState all[] = {WindowsHostState::NotWindowsHost,
                                     WindowsHostState::WhpxApiMissing,
                                     WindowsHostState::WhpxHypervisorNotPresent,
                                     WindowsHostState::WhpxDegraded,
                                     WindowsHostState::WhpxAvailable};
    for (WindowsHostState state : all) {
        check(!windowsStateMessage(state, QStringLiteral("x86_64")).isEmpty(),
              QStringLiteral("state %1 has a message").arg(static_cast<int>(state)));
    }
}

// ---------------------------------------------------------------------------
// Accelerator policy
// ---------------------------------------------------------------------------

void testAcceleratorPolicy() {
    // Same-ISA on x86_64 with WHPX available → whpx
    WindowsHostSnapshot x86Host = windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64"));
    checkEqual(windowsAcceleratorFor(x86Host, QStringLiteral("x86_64")),
               QStringLiteral("whpx"), "same-ISA x86_64 gets whpx");

    // Cross-arch on x86_64 host → tcg (even with WHPX available)
    checkEqual(windowsAcceleratorFor(x86Host, QStringLiteral("aarch64")),
               QStringLiteral("tcg"), "cross-arch aarch64 on x86_64 gets tcg");

    // Same-ISA on ARM64 with WHPX degraded → whpx
    WindowsHostSnapshot armHost = whpxDegradedHost({QStringLiteral("aarch64")});
    checkEqual(windowsAcceleratorFor(armHost, QStringLiteral("aarch64")),
               QStringLiteral("whpx"), "same-ISA aarch64 on ARM64 gets whpx (degraded)");

    // Cross-arch on ARM64 host → tcg
    checkEqual(windowsAcceleratorFor(armHost, QStringLiteral("x86_64")),
               QStringLiteral("tcg"), "cross-arch x86_64 on ARM64 gets tcg");

    // WHPX API missing → always tcg
    WindowsHostSnapshot apiMissing = whpxApiMissingHost({QStringLiteral("x86_64")});
    checkEqual(windowsAcceleratorFor(apiMissing, QStringLiteral("x86_64")),
               QStringLiteral("tcg"), "api-missing same-ISA gets tcg");
    checkEqual(windowsAcceleratorFor(apiMissing, QStringLiteral("aarch64")),
               QStringLiteral("tcg"), "api-missing cross-ISA gets tcg");

    // Non-Windows host → always tcg
    WindowsHostSnapshot nonWin = nonWindowsHost();
    checkEqual(windowsAcceleratorFor(nonWin, QStringLiteral("x86_64")),
               QStringLiteral("tcg"), "non-Windows gets tcg");
}

// ---------------------------------------------------------------------------
// Execution tier policy (per guest) vs. backend tier (never moves)
// ---------------------------------------------------------------------------

void testExecutionTierPolicy() {
    // windowsExecutionTierFor() answers "what will this guest really run with".
    WindowsHostSnapshot x86Host = windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64"));
    check(windowsExecutionTierFor(x86Host, QStringLiteral("x86_64")) == VirtualizationTier::Native,
          "same-ISA x86_64 executes natively via WHPX");

    // Cross-ISA never executes natively — the backend refuses the guest, and the
    // policy layer states the only accelerator it could ever request there.
    check(windowsExecutionTierFor(x86Host, QStringLiteral("aarch64")) == VirtualizationTier::SoftwareEmulation,
          "cross-ISA aarch64 would only ever execute as SoftwareEmulation");

    // ARM64 degraded same-ISA still executes natively (with a warning).
    WindowsHostSnapshot armHost = whpxDegradedHost({QStringLiteral("aarch64")});
    check(windowsExecutionTierFor(armHost, QStringLiteral("aarch64")) == VirtualizationTier::Native,
          "ARM64 same-ISA executes natively (degraded)");
    check(windowsExecutionTierFor(armHost, QStringLiteral("x86_64")) == VirtualizationTier::SoftwareEmulation,
          "ARM64 cross-ISA would only ever execute as SoftwareEmulation");

    // WHPX API missing → same-ISA guest falls back to TCG execution.
    WindowsHostSnapshot apiMissing = whpxApiMissingHost({QStringLiteral("x86_64")});
    check(windowsExecutionTierFor(apiMissing, QStringLiteral("x86_64")) == VirtualizationTier::SoftwareEmulation,
          "api-missing same-ISA executes as SoftwareEmulation");

    // Non-Windows → SoftwareEmulation.
    check(windowsExecutionTierFor(nonWindowsHost(), QStringLiteral("x86_64")) == VirtualizationTier::SoftwareEmulation,
          "non-Windows executes as SoftwareEmulation");
}

// The backend tier is a property of the mechanism (WHPX host hypervisor API),
// not of the current host or guest. It must never move, because the registry
// sorts candidates by tier and demoting this backend to emulation would push it
// *after* qemu-tcg and break the "qemu-tcg is the last resort" contract.
void testBackendTierNeverMoves() {
    struct Case {
        WindowsHostSnapshot snapshot;
        const char* label;
    };
    const WindowsHostSnapshot apiMissing = whpxApiMissingHost({QStringLiteral("x86_64")});
    const WindowsHostSnapshot hypervisorMissing = whpxHypervisorNotPresentHost({QStringLiteral("x86_64")});
    const WindowsHostSnapshot runtimeMissing = runtimeMissingHost();
    const Case cases[] = {
        {windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64")), "WHPX available"},
        {whpxDegradedHost(), "ARM64 degraded"},
        {apiMissing, "WHPX API missing"},
        {hypervisorMissing, "hypervisor not present"},
        {runtimeMissing, "QEMU runtime missing"},
        {nonWindowsHost(), "non-Windows host"},
    };
    for (const Case& testCase : cases) {
        FakeWindowsEnvironment environment;
        environment.snap = testCase.snapshot;
        WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));
        check(engine.tier() == VirtualizationTier::Native,
              QStringLiteral("backend tier stays Native (%1)").arg(QString::fromUtf8(testCase.label)));
        check(engine.probe().tier == VirtualizationTier::Native,
              QStringLiteral("probe tier stays Native (%1)").arg(QString::fromUtf8(testCase.label)));
    }
}

// ---------------------------------------------------------------------------
// Cross-architecture guests are REJECTED (not degraded to TCG)
// ---------------------------------------------------------------------------

void testCrossArchIsRejected() {
    FakeWindowsEnvironment environment;
    environment.snap = windowsHost({QStringLiteral("x86_64"), QStringLiteral("aarch64")}, QStringLiteral("x86_64"));
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    // Cross-ISA is refused, and the reason points at qemu-tcg.
    QString reason;
    check(!engine.supportsGuestArch(QStringLiteral("aarch64"), &reason),
          "cross-ISA aarch64 is rejected on x86_64 host");
    check(reason.contains(QStringLiteral("qemu-tcg")),
          "cross-ISA rejection points at qemu-tcg");

    // Same-ISA is supported. (supportsGuestArch() only fills the reason on
    // failure, so clear it first to avoid reading a stale rejection.)
    reason.clear();
    check(engine.supportsGuestArch(QStringLiteral("x86_64"), &reason),
          "same-ISA x86_64 is supported on x86_64 host");
    check(reason.isEmpty(), "same-ISA support has no rejection reason");

    // probeFor must not leak a host-level native verdict onto a refused guest.
    const EngineProbeResult crossProbe = engine.probeFor(QStringLiteral("aarch64"));
    check(!crossProbe.capabilities.guestArchSupported, "cross-ISA probeFor guest not supported");
    check(crossProbe.capabilities.accelerator.isEmpty(), "cross-ISA probeFor has no accelerator");
    check(!crossProbe.capabilities.nativeExecution, "cross-ISA nativeExecution is false");
    check(!crossProbe.capabilities.hardwareAcceleration, "cross-ISA hardwareAcceleration is false");
    check(!crossProbe.capabilities.canStart, "cross-ISA cannot be started");
    check(crossProbe.tier == VirtualizationTier::Native,
          "cross-ISA probeFor still reports the backend's own Native tier");
    check(hasIssue(crossProbe.diagnostics, QStringLiteral("guest-arch-unsupported")),
          "cross-ISA probeFor reports guest-arch-unsupported");

    // probeFor for a same-ISA guest reports the native execution path.
    const EngineProbeResult sameProbe = engine.probeFor(QStringLiteral("x86_64"));
    check(sameProbe.capabilities.guestArchSupported, "same-ISA probeFor guest supported");
    check(sameProbe.tier == VirtualizationTier::Native, "same-ISA probeFor tier is Native");
    checkEqual(sameProbe.capabilities.accelerator, QStringLiteral("whpx"), "same-ISA accelerator is whpx");
    check(sameProbe.capabilities.nativeExecution, "same-ISA nativeExecution is true");
    check(sameProbe.capabilities.hardwareAcceleration, "same-ISA hardwareAcceleration is true");
    check(sameProbe.capabilities.canStart, "same-ISA can be started");
}

// ---------------------------------------------------------------------------
// ARM64 WHPX degraded state
// ---------------------------------------------------------------------------

void testArm64Degraded() {
    FakeWindowsEnvironment environment;
    environment.snap = whpxDegradedHost({QStringLiteral("aarch64"), QStringLiteral("x86_64")});
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    // ARM64 same-ISA is supported with WHPX.
    QString reason;
    check(engine.supportsGuestArch(QStringLiteral("aarch64"), &reason),
          "ARM64 same-ISA aarch64 is supported");

    // Cross-ISA on ARM64 is refused, not silently run at TCG speed.
    check(!engine.supportsGuestArch(QStringLiteral("x86_64"), &reason),
          "ARM64 cross-ISA x86_64 is rejected (qemu-tcg owns it)");
    check(reason.contains(QStringLiteral("qemu-tcg")),
          "ARM64 cross-ISA rejection points at qemu-tcg");

    // probeFor reports the degraded warning for the same-ISA guest.
    const EngineProbeResult sameProbe = engine.probeFor(QStringLiteral("aarch64"));
    check(sameProbe.tier == VirtualizationTier::Native, "ARM64 same-ISA tier is Native");
    checkEqual(sameProbe.capabilities.accelerator, QStringLiteral("whpx"), "ARM64 same-ISA accelerator is whpx");
    check(hasIssue(sameProbe.diagnostics, QStringLiteral("whpx-degraded")),
          "ARM64 same-ISA probe reports whpx-degraded");

    // ARM64 cross-ISA probe reports the refusal, with no accelerator.
    const EngineProbeResult crossProbe = engine.probeFor(QStringLiteral("x86_64"));
    check(!crossProbe.capabilities.guestArchSupported, "ARM64 cross-ISA guest not supported");
    check(crossProbe.capabilities.accelerator.isEmpty(), "ARM64 cross-ISA has no accelerator");
    check(hasIssue(crossProbe.diagnostics, QStringLiteral("guest-arch-unsupported")),
          "ARM64 cross-ISA probe reports guest-arch-unsupported");

    // Validate reports the degraded warning for the same-ISA config.
    const QVector<EngineIssue> issues = engine.validate(guestConfig(QStringLiteral("aarch64")));
    check(hasIssueAtLevel(issues, QStringLiteral("whpx-degraded"), EngineIssueLevel::Warning),
          "ARM64 validate reports whpx-degraded warning");
}

// ---------------------------------------------------------------------------
// Non-Windows host is never supported
// ---------------------------------------------------------------------------

void testNonWindowsHostIsNeverSupported() {
    FakeWindowsEnvironment environment;
    environment.snap = nonWindowsHost();
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    QString reason;
    check(!engine.isHostSupported(&reason), "non-Windows host unsupported");
    check(!reason.isEmpty(), "non-Windows reason is filled");

    check(!engine.supportsGuestArch(QStringLiteral("x86_64"), &reason), "non-Windows arch unsupported");
    check(!reason.isEmpty(), "non-Windows arch reason filled");

    const EngineProbeResult probe = engine.probe();
    check(!probe.hostSupported, "non-Windows probe hostSupported false");
    check(!probe.capabilities.canStart, "non-Windows canStart false");
    check(!probe.capabilities.canStop, "non-Windows canStop false");
    check(!probe.capabilities.guestArchSupported, "non-Windows guestArchSupported false");
    check(!probe.summary.isEmpty(), "non-Windows summary present");
    checkEqual(engine.statusCode(guestConfig()), QStringLiteral("host-unsupported"),
               "non-Windows status code");
    // The backend tier names the wrapped mechanism, so it stays Native even on a
    // host where it can never run anything: the *outcome* is reported through
    // hostSupported/canStart/executionTierFor, never by demoting the tier.
    check(engine.tier() == VirtualizationTier::Native, "non-Windows backend tier stays Native");
    check(engine.executionTierFor(QStringLiteral("x86_64")) == VirtualizationTier::SoftwareEmulation,
          "non-Windows execution tier is SoftwareEmulation");
    check(engine.probeFor(QStringLiteral("x86_64")).capabilities.accelerator.isEmpty(),
          "non-Windows probeFor has no accelerator");

    check(hasIssue(engine.validate(guestConfig()), QStringLiteral("host-unsupported")),
          "non-Windows validate reports host-unsupported");

    // The critical guarantee: nothing is ever spawned off-platform.
    qint64 pid = 0;
    QString error;
    check(!engine.start(guestConfig(), &pid, &error), "non-Windows start fails");
    checkEqual(environment.startCalls, 0, "non-Windows start never spawns a process");
    check(pid == 0, "non-Windows start leaves pid untouched");
    check(!error.isEmpty(), "non-Windows start error is filled");
}

// ---------------------------------------------------------------------------
// WHPX API missing / hypervisor not present
// ---------------------------------------------------------------------------

void testWhpxApiMissing() {
    FakeWindowsEnvironment environment;
    environment.snap = whpxApiMissingHost({QStringLiteral("x86_64")});
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    // Host is still supported (QEMU TCG is available).
    QString reason;
    check(engine.isHostSupported(&reason), "api-missing host is still supported (TCG available)");

    // The same-ISA guest arch is supported (via TCG).
    check(engine.supportsGuestArch(QStringLiteral("x86_64"), &reason), "api-missing same-ISA guest arch supported");

    // Backend tier never moves; the honest per-guest answer is SoftwareEmulation.
    check(engine.tier() == VirtualizationTier::Native, "api-missing backend tier stays Native");
    check(engine.executionTierFor(QStringLiteral("x86_64")) == VirtualizationTier::SoftwareEmulation,
          "api-missing same-ISA execution tier is SoftwareEmulation");
    checkEqual(engine.acceleratorFor(QStringLiteral("x86_64")), QStringLiteral("tcg"),
               "api-missing same-ISA accelerator is tcg");

    // Validate reports a warning (not error) for api-missing, plus the explicit
    // statement of which accelerator will be used instead.
    const QVector<EngineIssue> issues = engine.validate(guestConfig());
    check(hasIssueAtLevel(issues, QStringLiteral("whpx-api-missing"), EngineIssueLevel::Warning),
          "api-missing validate reports warning");
    check(hasIssueAtLevel(issues, QStringLiteral("tcg-fallback"), EngineIssueLevel::Warning),
          "api-missing validate reports tcg-fallback warning");
    check(!hasEngineErrors(issues), "api-missing config is still runnable (no errors)");

    // Status code.
    checkEqual(engine.statusCode(guestConfig()), QStringLiteral("whpx-api-missing"), "api-missing status code");
}

void testWhpxHypervisorNotPresent() {
    FakeWindowsEnvironment environment;
    environment.snap = whpxHypervisorNotPresentHost({QStringLiteral("x86_64")});
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    QString reason;
    check(engine.isHostSupported(&reason), "hypervisor-not-present host is still supported (TCG)");
    check(engine.tier() == VirtualizationTier::Native, "hypervisor-not-present backend tier stays Native");
    check(engine.executionTierFor(QStringLiteral("x86_64")) == VirtualizationTier::SoftwareEmulation,
          "hypervisor-not-present execution tier is SoftwareEmulation");

    const QVector<EngineIssue> issues = engine.validate(guestConfig());
    check(hasIssueAtLevel(issues, QStringLiteral("whpx-hypervisor-not-present"), EngineIssueLevel::Warning),
          "hypervisor-not-present validate reports warning");
    check(hasIssueAtLevel(issues, QStringLiteral("tcg-fallback"), EngineIssueLevel::Warning),
          "hypervisor-not-present validate reports tcg-fallback warning");

    checkEqual(engine.statusCode(guestConfig()), QStringLiteral("whpx-hypervisor-not-present"),
               "hypervisor-not-present status code");
}

// ---------------------------------------------------------------------------
// Runtime missing
// ---------------------------------------------------------------------------

void testRuntimeMissing() {
    FakeWindowsEnvironment environment;
    environment.snap = runtimeMissingHost();
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    QString reason;
    check(!engine.isHostSupported(&reason), "missing runtime is unsupported");
    check(reason.contains(QStringLiteral("QEMU")), "missing runtime reason mentions QEMU");

    check(!engine.supportsGuestArch(QStringLiteral("x86_64"), &reason),
          "missing runtime arch unsupported");
    check(!reason.isEmpty(), "missing runtime arch reason filled");

    const QVector<EngineIssue> issues = engine.validate(guestConfig());
    check(hasEngineErrors(issues), "missing runtime has errors");
    checkEqual(engine.statusCode(guestConfig()), QStringLiteral("runtime-missing"),
               "missing runtime status code");

    // Precedence: an unsupported guest architecture outranks "runtime missing",
    // because the guest could never run here even with a runtime installed.
    checkEqual(engine.statusCode(guestConfig(QStringLiteral("aarch64"))),
               QStringLiteral("guest-arch-unsupported"),
               "cross-ISA outranks runtime-missing in the status code");
    checkEqual(engine.statusCode(guestConfig(QStringLiteral("riscv64"))),
               QStringLiteral("guest-arch-unsupported"),
               "an architecture this backend never offers is guest-arch-unsupported");

    qint64 pid = 0;
    QString error;
    check(!engine.start(guestConfig(), &pid, &error), "missing runtime start fails");
    checkEqual(environment.startCalls, 0, "missing runtime never spawns");
    check(!error.isEmpty(), "missing runtime start error filled");
}

// ---------------------------------------------------------------------------
// Start / stop with environment injection
// ---------------------------------------------------------------------------

void testStartStopWithEnvironment() {
    FakeWindowsEnvironment environment;
    environment.snap = windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64"));
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    qint64 pid = 0;
    QString error;
    check(engine.start(guestConfig(QStringLiteral("x86_64")), &pid, &error), "start succeeds with WHPX");
    checkEqual(pid, 4242, "start returns the fake pid");
    checkEqual(environment.startCalls, 1, "start called once");
    checkEqual(environment.lastStartConfig.accelerator, QStringLiteral("whpx"),
               "start config has whpx accelerator");

    // Cross-ISA start is refused and never reaches the spawner.
    environment.startCalls = 0;
    environment.snap.runnableGuestArches = QStringList{QStringLiteral("x86_64"), QStringLiteral("aarch64")};
    engine.invalidateProbe();
    error.clear();
    pid = 0;
    check(!engine.start(guestConfig(QStringLiteral("aarch64")), &pid, &error),
          "cross-ISA start is refused");
    checkEqual(environment.startCalls, 0, "cross-ISA start never spawns a process");
    check(pid == 0, "cross-ISA start never reports a pid");
    check(error.contains(QStringLiteral("qemu-tcg")), "cross-ISA start error points at qemu-tcg");

    // Stop.
    check(engine.stop(4242, &error), "stop succeeds");
    checkEqual(environment.stopCalls, 1, "stop called once");
    checkEqual(environment.lastStoppedPid, 4242, "stop received the pid");
}

// The same engine instance must survive a start/stop cycle repeated many times:
// nothing may be cached in a way that makes the second cycle behave differently.
void testRepeatedStartStop() {
    FakeWindowsEnvironment environment;
    environment.snap = windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64"));
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    for (int round = 0; round < 3; ++round) {
        qint64 pid = 0;
        QString error;
        check(engine.start(guestConfig(QStringLiteral("x86_64")), &pid, &error),
              QStringLiteral("start succeeds on round %1").arg(round));
        checkEqual(pid, 4242, QStringLiteral("round %1 returns the fake pid").arg(round));
        check(engine.stop(pid, &error), QStringLiteral("stop succeeds on round %1").arg(round));
        checkEqual(environment.lastStoppedPid, 4242,
                   QStringLiteral("round %1 stops the pid it started").arg(round));
    }
    checkEqual(environment.startCalls, 3, "three starts reached the environment");
    checkEqual(environment.stopCalls, 3, "three stops reached the environment");

    // Invalid process ids are refused without touching the environment.
    environment.stopCalls = 0;
    QString error;
    check(!engine.stop(0, &error), "stop(0) is refused");
    check(!engine.stop(-5, &error), "stop(negative) is refused");
    checkEqual(environment.stopCalls, 0, "invalid stop never reaches the environment");
}

// A failed start must leave the engine fully usable: the next attempt must go
// through, and the backend must never have reported a pid for the failure.
void testFailureRecovery() {
    FakeWindowsEnvironment environment;
    environment.snap = windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64"));
    environment.startResult = false;
    environment.startError = QStringLiteral("spawn failed");
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    qint64 pid = 12345;
    QString error;
    check(!engine.start(guestConfig(QStringLiteral("x86_64")), &pid, &error), "first start fails");
    checkEqual(pid, 12345, "failed start leaves the caller's pid untouched");
    checkEqual(error, QStringLiteral("spawn failed"), "failure reason is forwarded verbatim");
    checkEqual(environment.startCalls, 1, "the failing attempt did reach the environment");

    // Environment recovers — the very next start must succeed with no reset call.
    environment.startResult = true;
    pid = 0;
    error.clear();
    check(engine.start(guestConfig(QStringLiteral("x86_64")), &pid, &error), "second start succeeds after recovery");
    checkEqual(pid, 4242, "recovered start returns the pid");
    check(error.isEmpty(), "recovered start leaves no error");

    // A spawner that claims success without a pid is not accepted either.
    environment.startPid = 0;
    pid = 0;
    error.clear();
    check(!engine.start(guestConfig(QStringLiteral("x86_64")), &pid, &error),
          "a success without a positive pid is rejected");
    check(pid == 0, "no pid is reported for an unverifiable start");
    check(!error.isEmpty(), "unverifiable start explains itself");
}

void testStartFailure() {
    FakeWindowsEnvironment environment;
    environment.snap = windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64"));
    environment.startResult = false;
    environment.startError = QStringLiteral("QEMU process failed to start");
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    qint64 pid = 999;
    QString error;
    check(!engine.start(guestConfig(), &pid, &error), "start fails when environment fails");
    checkEqual(pid, 999, "failed start leaves pid untouched");
    checkEqual(error, QStringLiteral("QEMU process failed to start"), "start error is forwarded");
}

// ---------------------------------------------------------------------------
// Null environment guard
// ---------------------------------------------------------------------------

void testNullEnvironment() {
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(nullptr));

    qint64 pid = 0;
    QString error;
    check(!engine.start(guestConfig(), &pid, &error), "start fails with null environment");
    check(!error.isEmpty(), "null environment start error is filled");

    check(!engine.stop(1234, &error), "stop fails with null environment");
    check(!error.isEmpty(), "null environment stop error is filled");
}

// ---------------------------------------------------------------------------
// Probe and capabilities
// ---------------------------------------------------------------------------

void testProbe() {
    FakeWindowsEnvironment environment;
    environment.snap = windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64"));
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    const EngineProbeResult probe = engine.probe();
    checkEqual(probe.backendId, backend_id::windowsHypervisor(), "probe backend id");
    check(!probe.displayName.isEmpty(), "probe display name is set");
    check(probe.tier == VirtualizationTier::Native, "probe tier is the backend's Native tier");
    check(probe.hostSupported, "probe host supported");
    check(probe.capabilities.hostSupported, "probe capabilities host supported");
    check(probe.capabilities.nativeExecution, "probe nativeExecution true");
    check(probe.capabilities.hardwareAcceleration, "probe hardwareAcceleration true");
    check(probe.capabilities.guestArchSupported, "probe guest arch supported");
    check(probe.capabilities.canStart, "probe can start");
    check(probe.capabilities.canStop, "probe can stop");
    checkEqual(probe.capabilities.accelerator, QStringLiteral("whpx"), "probe accelerator is whpx");
    check(!probe.summary.isEmpty(), "probe has summary");
    check(hasIssue(probe.diagnostics, QStringLiteral("whpx-available")),
          "probe reports whpx-available");
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

void testValidation() {
    FakeWindowsEnvironment environment;
    environment.snap = windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64"));
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    // System Software configs must always be rejected.
    VmConfig systemSoftware = guestConfig();
    systemSoftware.backend = backend_id::systemSoftware();
    check(hasIssue(engine.validate(systemSoftware), QStringLiteral("backend-mismatch")),
          "system-software config rejected by WHPX");

    // Cross-ISA is REJECTED: this backend cannot accelerate it, and running it
    // at TCG speed under a WHPX-scoped backend would mislead auto-selection.
    environment.snap.runnableGuestArches = QStringList{QStringLiteral("x86_64"), QStringLiteral("aarch64")};
    const QVector<EngineIssue> crossIssues = engine.validate(guestConfig(QStringLiteral("aarch64")));
    check(hasIssueAtLevel(crossIssues, QStringLiteral("guest-arch-unsupported"), EngineIssueLevel::Error),
          "cross-ISA config is rejected as an error");
    check(!hasIssue(crossIssues, QStringLiteral("cross-arch-tcg-fallback")),
          "the old cross-arch-tcg-fallback issue is gone");
    // ... and the rejection points the user at the backend that owns it.
    for (const EngineIssue& issue : crossIssues) {
        if (issue.code == QStringLiteral("guest-arch-unsupported")) {
            check(issue.message.contains(QStringLiteral("qemu-tcg")),
                  "cross-ISA rejection message points at qemu-tcg");
        }
    }
    checkEqual(engine.statusCode(guestConfig(QStringLiteral("aarch64"))),
               QStringLiteral("guest-arch-unsupported"), "cross-ISA status code");

    // Same-ISA is accepted (WHPX available in this snapshot).
    const QVector<EngineIssue> sameIssues = engine.validate(guestConfig(QStringLiteral("x86_64")));
    check(!hasEngineErrors(sameIssues), "same-ISA config has no errors");
    check(hasIssue(sameIssues, QStringLiteral("whpx-available")), "same-ISA config reports whpx-available");

    // Unsupported arch (riscv64) is rejected.
    VmConfig riscvConfig = guestConfig(QStringLiteral("riscv64"));
    const QVector<EngineIssue> riscvIssues = engine.validate(riscvConfig);
    check(hasIssueAtLevel(riscvIssues, QStringLiteral("guest-arch-unsupported"), EngineIssueLevel::Error),
          "riscv64 config rejected as unsupported");

    // An empty architecture is rejected before anything else is considered.
    VmConfig emptyArch = guestConfig();
    emptyArch.architecture.clear();
    check(hasIssueAtLevel(engine.validate(emptyArch), QStringLiteral("guest-arch-missing"),
                          EngineIssueLevel::Error),
          "empty architecture is rejected");

    // A config whose CPU count is invalid is rejected.
    VmConfig noCpus = guestConfig();
    noCpus.cpuCores = 0;
    check(hasIssueAtLevel(engine.validate(noCpus), QStringLiteral("cpu-count-invalid"),
                          EngineIssueLevel::Error),
          "zero CPU count is rejected");
}

// ---------------------------------------------------------------------------
// Diagnose and requirements
// ---------------------------------------------------------------------------

void testDiagnose() {
    FakeWindowsEnvironment environment;
    environment.snap = windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64"));
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    const QString diagnosis = engine.diagnose(guestConfig());
    check(!diagnosis.isEmpty(), "diagnosis is not empty");
    check(diagnosis.contains(QStringLiteral("WHPX")) || diagnosis.contains(QStringLiteral("whpx"))
              || diagnosis.contains(QStringLiteral("Hypervisor")),
          "diagnosis mentions WHPX/Hypervisor");
    check(diagnosis.contains(QStringLiteral("宿主")), "diagnosis mentions the host");
    check(diagnosis.contains(QStringLiteral("加速器")), "diagnosis states the accelerator for this config");

    // A cross-ISA guest must be *labelled* as not this backend's, not quietly
    // reported as running at TCG speed.
    environment.snap = windowsHost({QStringLiteral("x86_64"), QStringLiteral("aarch64")}, QStringLiteral("x86_64"));
    engine.invalidateProbe();
    const QString crossDiagnosis = engine.diagnose(guestConfig(QStringLiteral("aarch64")));
    check(crossDiagnosis.contains(QStringLiteral("不承担")),
          "diagnosis says the backend does not take cross-ISA guests");
    check(crossDiagnosis.contains(QStringLiteral("qemu-tcg")),
          "cross-ISA diagnosis points at qemu-tcg");
    check(!crossDiagnosis.contains(QStringLiteral("  加速器:")),
          "cross-ISA diagnosis claims no accelerator");
}

void testRequirements() {
    FakeWindowsEnvironment environment;
    environment.snap = windowsHost();
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    const QStringList requirements = engine.requirements();
    check(!requirements.isEmpty(), "requirements are not empty");
    check(requirements.size() >= 2, "at least two requirements listed");

    const QString joined = requirements.join(QStringLiteral(" | "));
    check(joined.contains(QStringLiteral("Windows")), "requirements mention Windows");
    check(joined.contains(QStringLiteral("QEMU")), "requirements mention QEMU dependency");
    check(joined.contains(QStringLiteral("-accel tcg")),
          "requirements state the TCG fallback for same-ISA guests when WHPX is absent");
    check(joined.contains(QStringLiteral("同架构")), "requirements state the same-ISA restriction");
    check(joined.contains(QStringLiteral("qemu-tcg")), "requirements point cross-arch guests at qemu-tcg");
    check(joined.contains(QStringLiteral("winhvplatform.dll")), "requirements name the WHPX API");
}

// ---------------------------------------------------------------------------
// Registry integration
// ---------------------------------------------------------------------------

void testRegistryIntegration() {
    VirtualizationEngineRegistry& registry = VirtualizationEngineRegistry::instance();
    registry.registerBuiltinBackends();

    check(registry.hasBackend(backend_id::windowsHypervisor()),
          "WHPX backend is registered in the registry");

    VirtualizationEnginePtr registered = registry.backend(backend_id::windowsHypervisor());
    check(!registered.isNull(), "registry returns the WHPX backend");
    if (registered) {
        checkEqual(registered->backendId(), backend_id::windowsHypervisor(), "registered backend id");
        // The shared ordering contract: a backend that wraps a real host
        // hypervisor API must report the Native tier. Demoting it to emulation
        // would sort it after qemu-tcg and break "qemu-tcg is the last resort".
        check(registered->tier() == VirtualizationTier::Native,
              "windows-hypervisor reports the Native tier");
    }

    const QStringList ids = registry.backendIds();
    check(ids.contains(backend_id::windowsHypervisor()), "backend id list contains windows-hypervisor");

    // qemu-tcg, if present in this binary, must be pinned to the very end.
    const QVector<VirtualizationEnginePtr> ordered = registry.backendsInPreferenceOrder();
    check(!ordered.isEmpty(), "preference order is non-empty");
    int tcgIndex = -1;
    for (int i = 0; i < ordered.size(); ++i) {
        if (ordered.at(i)->backendId() == backend_id::qemuTcg()) {
            tcgIndex = i;
            break;
        }
    }
    if (tcgIndex >= 0) {
        checkEqual(tcgIndex, ordered.size() - 1, "qemu-tcg is the last candidate in preference order");
    }
    // Whatever the backend set of this binary is, the WHPX backend must never be
    // the last-resort candidate: qemu-tcg owns software emulation, not this.
    check(ordered.last()->backendId() != backend_id::windowsHypervisor(),
          "windows-hypervisor is never the last-resort candidate");
}

// ---------------------------------------------------------------------------
// Legacy probe (backward compatibility)
// ---------------------------------------------------------------------------

void testLegacyProbeResult() {
    const WhpxProbeResult probed = probeWhpx();

    // On any platform, the probe must return a deterministic result.
    check(!probed.hostArch.isEmpty(), "probe always reports a host arch");

    if (probed.hostIsWindows) {
        check(probed.hostArch == QStringLiteral("x86_64")
                  || probed.hostArch == QStringLiteral("aarch64"),
              "Windows host arch is x86_64 or aarch64 for WHPX");

        if (probed.whpxUsable) {
            check(probed.apiLoadable, "usable WHPX implies API is loadable");
            check(probed.hypervisorPresent, "usable WHPX implies hypervisor is present");
            check(probed.reason.isEmpty(), "usable WHPX has no rejection reason");
        }

        if (!probed.whpxUsable) {
            check(!probed.reason.isEmpty(), "unusable WHPX carries a reason");
        }
    } else {
        check(!probed.whpxUsable, "non-Windows host never claims usable WHPX");
        check(!probed.reason.isEmpty(), "non-Windows WHPX rejection carries a reason");
    }

    check(!probed.detail.isEmpty(), "probe detail is always populated");
}

// ---------------------------------------------------------------------------
// Argument safety
// ---------------------------------------------------------------------------

void testArgumentSafety() {
    check(windowsArgumentsAreWellFormed({QStringLiteral("qemu-system-x86_64"), QStringLiteral("-m"), QStringLiteral("1024")}),
          "well-formed arguments pass");

    QString problem;
    check(!windowsArgumentsAreWellFormed({}, &problem), "empty arguments rejected");
    check(!problem.isEmpty(), "empty arguments problem is filled");

    check(!windowsArgumentsAreWellFormed({QString()}, &problem), "null element rejected");

    check(!windowsArgumentsAreWellFormed({QStringLiteral("test\ncrash")}, &problem),
          "newline in argument rejected");

    check(!windowsArgumentsAreWellFormed({QStringLiteral("-machine q35,accel=whpx")}, &problem),
          "option-like argument with space rejected");
}

// ---------------------------------------------------------------------------
// Snapshot caching and invalidation
// ---------------------------------------------------------------------------

void testSnapshotCaching() {
    FakeWindowsEnvironment environment;
    environment.snap = windowsHost();
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    // First access triggers snapshot.
    const WindowsHostSnapshot& first = engine.hostSnapshot();
    checkEqual(environment.snapshotCalls, 1, "first access calls snapshot");
    check(first.hostIsWindows, "first snapshot is Windows host");

    // Second access uses cache.
    const WindowsHostSnapshot& second = engine.hostSnapshot();
    checkEqual(environment.snapshotCalls, 1, "second access uses cache");
    check(&first == &second, "repeated access returns the cached object itself");

    // Invalidation forces re-read.
    engine.invalidateProbe();
    const WindowsHostSnapshot& third = engine.hostSnapshot();
    checkEqual(environment.snapshotCalls, 2, "after invalidation, snapshot is re-read");
}

// The probe cache must be a pure optimisation: once invalidated, a changed
// environment snapshot must change every downstream answer (accelerator,
// execution tier, status code, canStart). This is what makes "plug the WHPX
// feature in and re-probe" work without restarting the application.
void testEnvironmentSnapshotChange() {
    FakeWindowsEnvironment environment;
    environment.snap = whpxApiMissingHost({QStringLiteral("x86_64")});
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    // Start out with WHPX missing: same-ISA guest runs, but only via TCG.
    check(engine.isHostSupported(), "host supported while WHPX is missing");
    check(engine.supportsGuestArch(QStringLiteral("x86_64")), "same-ISA guest supported while WHPX is missing");
    checkEqual(engine.acceleratorFor(QStringLiteral("x86_64")), QStringLiteral("tcg"),
               "accelerator is tcg while WHPX is missing");
    check(engine.executionTierFor(QStringLiteral("x86_64")) == VirtualizationTier::SoftwareEmulation,
          "execution tier is SoftwareEmulation while WHPX is missing");
    checkEqual(engine.statusCode(guestConfig()), QStringLiteral("whpx-api-missing"),
               "status is whpx-api-missing before the environment changes");
    check(!engine.probeFor(QStringLiteral("x86_64")).capabilities.nativeExecution,
          "nativeExecution is false while WHPX is missing");

    // The host changes (the user enabled the Virtual Machine Platform feature).
    environment.snap = windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64"));

    // Without invalidation the cached snapshot still answers the old question.
    checkEqual(engine.acceleratorFor(QStringLiteral("x86_64")), QStringLiteral("tcg"),
               "cached snapshot keeps answering the pre-change question");
    checkEqual(environment.snapshotCalls, 1, "no extra snapshot read happened");

    // After invalidation, everything flips to the native path.
    engine.invalidateProbe();
    checkEqual(engine.acceleratorFor(QStringLiteral("x86_64")), QStringLiteral("whpx"),
               "accelerator becomes whpx after invalidateProbe()");
    check(engine.executionTierFor(QStringLiteral("x86_64")) == VirtualizationTier::Native,
          "execution tier becomes Native after invalidateProbe()");
    checkEqual(engine.statusCode(guestConfig()), QStringLiteral("available"),
               "status becomes available after invalidateProbe()");
    check(engine.probeFor(QStringLiteral("x86_64")).capabilities.nativeExecution,
          "nativeExecution becomes true after invalidateProbe()");
    checkEqual(environment.snapshotCalls, 2, "exactly one re-read happened");
    // The backend tier still never moves.
    check(engine.tier() == VirtualizationTier::Native, "backend tier stays Native across the change");
}

// A host that loses its runtime must stop claiming canStart, and must start
// claiming it again once the runtime comes back — all through the same engine.
void testRuntimeDisappearsAndReturns() {
    FakeWindowsEnvironment environment;
    environment.snap = windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64"));
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    check(engine.isHostSupported(), "host supported with a runtime");
    check(engine.probe().capabilities.guestArchSupported, "guest arch supported with a runtime");

    environment.snap = runtimeMissingHost();
    engine.invalidateProbe();
    check(!engine.isHostSupported(), "host unsupported once the runtime is gone");
    check(!engine.supportsGuestArch(QStringLiteral("x86_64")), "guest arch unsupported once the runtime is gone");
    checkEqual(engine.statusCode(guestConfig()), QStringLiteral("runtime-missing"),
               "status is runtime-missing once the runtime is gone");

    qint64 pid = 0;
    QString error;
    check(!engine.start(guestConfig(), &pid, &error), "start fails once the runtime is gone");
    checkEqual(environment.startCalls, 0, "no spawn attempt without a runtime");

    environment.snap = windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64"));
    engine.invalidateProbe();
    check(engine.isHostSupported(), "host supported again once the runtime returns");
    check(engine.start(guestConfig(), &pid, &error), "start succeeds again once the runtime returns");
    checkEqual(pid, 4242, "the recovered start reports a pid");
}

// ---------------------------------------------------------------------------
// Fault injection: probe cache invalidation under environment mutation
// ---------------------------------------------------------------------------

// Rapidly toggling the environment between two incompatible states while
// invalidating the probe cache must never produce a stale answer: every
// query after invalidateProbe() must reflect the new state.
void testProbeCacheInvalidationUnderMutation() {
    FakeWindowsEnvironment environment;
    environment.snap = whpxApiMissingHost({QStringLiteral("x86_64")});
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    // Fixed-seed repeatable loop: toggle 10 times, check after each invalidation.
    const int kRounds = 10;
    for (int round = 0; round < kRounds; ++round) {
        const bool whpxAvailable = (round % 2 == 0);
        if (whpxAvailable) {
            environment.snap = windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64"));
        } else {
            environment.snap = whpxApiMissingHost({QStringLiteral("x86_64")});
        }
        engine.invalidateProbe();

        const QString accelerator = engine.acceleratorFor(QStringLiteral("x86_64"));
        const VirtualizationTier tier = engine.executionTierFor(QStringLiteral("x86_64"));
        const QString statusCode = engine.statusCode(guestConfig());

        if (whpxAvailable) {
            checkEqual(accelerator, QStringLiteral("whpx"),
                       QStringLiteral("round %1: accelerator is whpx after invalidation").arg(round));
            check(tier == VirtualizationTier::Native,
                  QStringLiteral("round %1: tier is Native after invalidation").arg(round));
            checkEqual(statusCode, QStringLiteral("available"),
                       QStringLiteral("round %1: status is available after invalidation").arg(round));
        } else {
            checkEqual(accelerator, QStringLiteral("tcg"),
                       QStringLiteral("round %1: accelerator is tcg after invalidation").arg(round));
            check(tier == VirtualizationTier::SoftwareEmulation,
                  QStringLiteral("round %1: tier is SoftwareEmulation after invalidation").arg(round));
            checkEqual(statusCode, QStringLiteral("whpx-api-missing"),
                       QStringLiteral("round %1: status is whpx-api-missing after invalidation").arg(round));
        }

        // The backend tier never moves regardless of the host state.
        check(engine.tier() == VirtualizationTier::Native,
              QStringLiteral("round %1: backend tier stays Native").arg(round));
    }
    // Exactly kRounds snapshot reads happened (one per invalidateProbe + query).
    checkEqual(environment.snapshotCalls, kRounds,
               "probe cache invalidation triggers exactly one re-read per round");
}

// ---------------------------------------------------------------------------
// Fault injection: validate error propagation with every host state
// ---------------------------------------------------------------------------

// Every WindowsHostState must produce a non-empty reason from
// isHostSupported() when it returns false, and statusCode() must be stable.
void testValidateErrorPropagationPerState() {
    struct TestCase {
        WindowsHostSnapshot snapshot;
        QString expectedCode;
        bool hostSupported;
        QString label;
    };

    const QVector<TestCase> cases = {
        {nonWindowsHost(), QStringLiteral("host-unsupported"), false,
         QStringLiteral("non-windows host")},
        {whpxApiMissingHost({QStringLiteral("x86_64")}), QStringLiteral("whpx-api-missing"), true,
         QStringLiteral("whpx-api-missing host")},
        {whpxHypervisorNotPresentHost({QStringLiteral("x86_64")}), QStringLiteral("whpx-hypervisor-not-present"), true,
         QStringLiteral("whpx-hypervisor-not-present host")},
        {runtimeMissingHost(), QStringLiteral("runtime-missing"), false,
         QStringLiteral("runtime-missing host")},
    };

    for (const TestCase& tc : cases) {
        FakeWindowsEnvironment environment;
        environment.snap = tc.snapshot;
        WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

        QString reason;
        const bool supported = engine.isHostSupported(&reason);
        checkEqual(supported, tc.hostSupported,
                   QStringLiteral("%1: isHostSupported matches").arg(tc.label));
        if (!tc.hostSupported) {
            check(!reason.isEmpty(),
                  QStringLiteral("%1: unsupported reason is non-empty").arg(tc.label));
        }
        checkEqual(engine.statusCode(guestConfig()), tc.expectedCode,
                   QStringLiteral("%1: statusCode is stable").arg(tc.label));

        // Every unsupported/error reason must be non-empty.
        const QVector<EngineIssue> issues = engine.validate(guestConfig());
        for (const EngineIssue& issue : issues) {
            if (issue.level == EngineIssueLevel::Error) {
                check(!issue.message.isEmpty(),
                      QStringLiteral("%1: error issue message is non-empty (code=%2)")
                          .arg(tc.label, issue.code));
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Fault injection: start failure modes
// ---------------------------------------------------------------------------

// A start that fails immediately must leave the engine fully usable and
// must never report a pid.
void testStartImmediateFailure() {
    FakeWindowsEnvironment environment;
    environment.snap = windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64"));
    environment.startResult = false;
    environment.startError = QStringLiteral("CreateProcess failed: error 2");
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    qint64 pid = 0;
    QString error;
    check(!engine.start(guestConfig(), &pid, &error), "immediate start failure is reported");
    checkEqual(pid, 0, "immediate failure leaves pid at 0");
    check(!error.isEmpty(), "immediate failure fills error");
    check(error.contains(QStringLiteral("CreateProcess")),
          "immediate failure error mentions CreateProcess");

    // The engine must be usable after the failure.
    environment.startResult = true;
    pid = 0;
    error.clear();
    check(engine.start(guestConfig(), &pid, &error), "engine recovers after immediate failure");
    checkEqual(pid, 4242, "recovered start reports correct pid");
}

// A start that succeeds but returns pid=0 must be rejected.
// The engine does not modify the caller's pid on failure, so the caller
// must treat the pid as meaningful only when start() returns true.
void testStartSuccessWithZeroPid() {
    FakeWindowsEnvironment environment;
    environment.snap = windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64"));
    environment.startPid = 0;
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    qint64 pid = 999;
    QString error;
    check(!engine.start(guestConfig(), &pid, &error), "start with zero pid is rejected");
    // pid is not modified on failure — caller must check the return value.
    check(!error.isEmpty(), "zero-pid rejection fills error");
}

// A start that succeeds but returns a negative pid must be rejected.
void testStartSuccessWithNegativePid() {
    FakeWindowsEnvironment environment;
    environment.snap = windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64"));
    environment.startPid = -42;
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    qint64 pid = 999;
    QString error;
    check(!engine.start(guestConfig(), &pid, &error), "start with negative pid is rejected");
    // pid is not modified on failure — caller must check the return value.
    check(!error.isEmpty(), "negative-pid rejection fills error");
}

// ---------------------------------------------------------------------------
// Fault injection: stop failure modes
// ---------------------------------------------------------------------------

void testStopFailure() {
    FakeWindowsEnvironment environment;
    environment.snap = windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64"));
    environment.stopResult = false;
    environment.stopError = QStringLiteral("TerminateProcess failed: access denied");
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    QString error;
    check(!engine.stop(1234, &error), "stop failure is reported");
    check(!error.isEmpty(), "stop failure fills error");
    check(error.contains(QStringLiteral("TerminateProcess")),
          "stop failure error mentions TerminateProcess");
    checkEqual(environment.stopCalls, 1, "stop failure still called the environment");
    checkEqual(environment.lastStoppedPid, 1234, "stop failure forwarded the pid");
}

// ---------------------------------------------------------------------------
// Fault injection: probe/invalidate collision (repeated rapid invalidation)
// ---------------------------------------------------------------------------

// Repeatedly calling invalidateProbe() without any intervening query must
// not corrupt the cache state. The next query must still trigger exactly
// one snapshot read.
void testRepeatedInvalidation() {
    FakeWindowsEnvironment environment;
    environment.snap = windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64"));
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    // First query triggers one snapshot read.
    engine.probe();
    checkEqual(environment.snapshotCalls, 1, "first probe triggers one read");

    // Rapid invalidation without queries.
    for (int i = 0; i < 5; ++i) {
        engine.invalidateProbe();
    }
    // The next query must trigger exactly one read.
    engine.probe();
    checkEqual(environment.snapshotCalls, 2, "after repeated invalidation, one more read");
}

// ---------------------------------------------------------------------------
// Lifecycle stress: repeated start/stop with invalidation interleaved
// ---------------------------------------------------------------------------

void testStartStopWithProbeInvalidation() {
    FakeWindowsEnvironment environment;
    environment.snap = windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64"));
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    const int kRounds = 5;
    for (int round = 0; round < kRounds; ++round) {
        // Start.
        qint64 pid = 0;
        QString error;
        check(engine.start(guestConfig(QStringLiteral("x86_64")), &pid, &error),
              QStringLiteral("start succeeds on round %1").arg(round));
        checkEqual(pid, 4242, QStringLiteral("round %1: pid is correct").arg(round));

        // Interleave a probe invalidation (simulates runtime state change).
        engine.invalidateProbe();
        check(engine.isHostSupported(),
              QStringLiteral("round %1: host still supported after invalidation").arg(round));

        // Stop.
        check(engine.stop(pid, &error),
              QStringLiteral("stop succeeds on round %1").arg(round));
    }
    checkEqual(environment.startCalls, kRounds, "start called kRounds times");
    checkEqual(environment.stopCalls, kRounds, "stop called kRounds times");
}

// ---------------------------------------------------------------------------
// Lifecycle stress: many start/stop cycles with fixed seed
// ---------------------------------------------------------------------------

void testStressStartStopCycles() {
    FakeWindowsEnvironment environment;
    environment.snap = windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64"));
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    const int kCycles = 20;
    const qint64 kExpectedPid = 4242;
    for (int cycle = 0; cycle < kCycles; ++cycle) {
        qint64 pid = 0;
        QString error;
        check(engine.start(guestConfig(QStringLiteral("x86_64")), &pid, &error),
              QStringLiteral("stress cycle %1: start succeeds").arg(cycle));
        checkEqual(pid, kExpectedPid,
                   QStringLiteral("stress cycle %1: pid is stable").arg(cycle));
        check(engine.stop(pid, &error),
              QStringLiteral("stress cycle %1: stop succeeds").arg(cycle));
    }
    checkEqual(environment.startCalls, kCycles, "stress: start call count matches cycles");
    checkEqual(environment.stopCalls, kCycles, "stress: stop call count matches cycles");

    // After all cycles, the engine must still produce correct answers.
    check(engine.isHostSupported(), "stress: host still supported after all cycles");
    checkEqual(engine.statusCode(guestConfig()), QStringLiteral("available"),
               "stress: status code stable after all cycles");
}

// ---------------------------------------------------------------------------
// Windows-specific: WHPX DLL missing (apiLoadable=false)
// ---------------------------------------------------------------------------

void testWhpxDllMissing() {
    FakeWindowsEnvironment environment;
    environment.snap = whpxApiMissingHost({QStringLiteral("x86_64")});
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    // The host is still supported (TCG fallback).
    check(engine.isHostSupported(), "dll-missing host is still supported");
    checkEqual(engine.acceleratorFor(QStringLiteral("x86_64")), QStringLiteral("tcg"),
               "dll-missing accelerator is tcg");

    // Validate reports the specific issue.
    const QVector<EngineIssue> issues = engine.validate(guestConfig());
    check(hasIssueAtLevel(issues, QStringLiteral("whpx-api-missing"), EngineIssueLevel::Warning),
          "dll-missing validate reports whpx-api-missing warning");
    check(hasIssueAtLevel(issues, QStringLiteral("tcg-fallback"), EngineIssueLevel::Warning),
          "dll-missing validate reports tcg-fallback warning");

    // Start still works via TCG.
    qint64 pid = 0;
    QString error;
    check(engine.start(guestConfig(), &pid, &error), "dll-missing start succeeds via TCG");
    checkEqual(environment.lastStartConfig.accelerator, QStringLiteral("tcg"),
               "dll-missing start uses tcg accelerator");
}

// ---------------------------------------------------------------------------
// Windows-specific: WHvGetCapability reports hypervisor not present
// ---------------------------------------------------------------------------

void testWhpxGetCapabilityNoHypervisor() {
    FakeWindowsEnvironment environment;
    environment.snap = whpxHypervisorNotPresentHost({QStringLiteral("x86_64")});
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    // Hyper-V not enabled: WHPX API loads but hypervisor is not present.
    check(engine.isHostSupported(), "hypervisor-not-present host is supported (TCG)");
    checkEqual(engine.acceleratorFor(QStringLiteral("x86_64")), QStringLiteral("tcg"),
               "hypervisor-not-present accelerator is tcg");

    const QVector<EngineIssue> issues = engine.validate(guestConfig());
    check(hasIssueAtLevel(issues, QStringLiteral("whpx-hypervisor-not-present"), EngineIssueLevel::Warning),
          "hypervisor-not-present reports warning");
    check(!hasEngineErrors(issues), "hypervisor-not-present has no hard errors (TCG fallback)");

    // Start works via TCG.
    qint64 pid = 0;
    QString error;
    check(engine.start(guestConfig(), &pid, &error), "hypervisor-not-present start via TCG");
    checkEqual(environment.lastStartConfig.accelerator, QStringLiteral("tcg"),
               "hypervisor-not-present start uses tcg");
}

// ---------------------------------------------------------------------------
// Windows-specific: ARM64 same-ISA with WHPX degraded
// ---------------------------------------------------------------------------

void testArm64SameIsaDegraded() {
    FakeWindowsEnvironment environment;
    environment.snap = whpxDegradedHost({QStringLiteral("aarch64")});
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    // ARM64 WHPX is usable but degraded.
    check(engine.isHostSupported(), "ARM64 degraded host is supported");
    check(engine.supportsGuestArch(QStringLiteral("aarch64")), "ARM64 same-ISA guest supported");
    checkEqual(engine.acceleratorFor(QStringLiteral("aarch64")), QStringLiteral("whpx"),
               "ARM64 same-ISA accelerator is whpx");
    check(engine.executionTierFor(QStringLiteral("aarch64")) == VirtualizationTier::Native,
          "ARM64 same-ISA execution tier is Native");

    const QVector<EngineIssue> issues = engine.validate(guestConfig(QStringLiteral("aarch64")));
    check(hasIssueAtLevel(issues, QStringLiteral("whpx-degraded"), EngineIssueLevel::Warning),
          "ARM64 validate reports whpx-degraded warning");
    check(!hasEngineErrors(issues), "ARM64 degraded has no hard errors");

    // Start uses WHPX.
    qint64 pid = 0;
    QString error;
    check(engine.start(guestConfig(QStringLiteral("aarch64")), &pid, &error),
          "ARM64 degraded start succeeds via WHPX");
    checkEqual(environment.lastStartConfig.accelerator, QStringLiteral("whpx"),
               "ARM64 degraded start uses whpx");
}

// ---------------------------------------------------------------------------
// Windows-specific: QEMU not found / not executable
// ---------------------------------------------------------------------------

void testQemuNotFound() {
    FakeWindowsEnvironment environment;
    WindowsHostSnapshot snap;
    snap.hostIsWindows = true;
    snap.hostFamily = QStringLiteral("windows");
    snap.hostName = QStringLiteral("Windows");
    snap.hostArch = QStringLiteral("x86_64");
    snap.apiLoadable = true;
    snap.hypervisorPresent = true;
    snap.whpxUsable = true;
    snap.qemuFound = false;
    snap.qemuExecutable = false;
    snap.runnableGuestArches = QStringList{};
    snap.detail = QStringLiteral("QEMU not found");
    environment.snap = snap;
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    QString reason;
    check(!engine.isHostSupported(&reason), "QEMU missing: host unsupported");
    check(!reason.isEmpty(), "QEMU missing: reason is non-empty");

    const QVector<EngineIssue> issues = engine.validate(guestConfig());
    check(hasEngineErrors(issues), "QEMU missing: validate has errors");
    checkEqual(engine.statusCode(guestConfig()), QStringLiteral("runtime-missing"),
               "QEMU missing: status code is runtime-missing");

    // Start must fail without reaching the environment.
    qint64 pid = 0;
    QString error;
    check(!engine.start(guestConfig(), &pid, &error), "QEMU missing: start fails");
    checkEqual(environment.startCalls, 0, "QEMU missing: start never spawns");
    check(!error.isEmpty(), "QEMU missing: start error is filled");
}

void testQemuFoundNotExecutable() {
    FakeWindowsEnvironment environment;
    WindowsHostSnapshot snap;
    snap.hostIsWindows = true;
    snap.hostFamily = QStringLiteral("windows");
    snap.hostName = QStringLiteral("Windows");
    snap.hostArch = QStringLiteral("x86_64");
    snap.apiLoadable = true;
    snap.hypervisorPresent = true;
    snap.whpxUsable = true;
    snap.qemuFound = true;
    snap.qemuExecutable = false;
    snap.runnableGuestArches = QStringList{};
    snap.detail = QStringLiteral("QEMU found but not executable");
    environment.snap = snap;
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    QString reason;
    check(!engine.isHostSupported(&reason), "QEMU not executable: host unsupported");
    check(!reason.isEmpty(), "QEMU not executable: reason is non-empty");
    checkEqual(engine.statusCode(guestConfig()), QStringLiteral("runtime-missing"),
               "QEMU not executable: status code is runtime-missing");
}

// ---------------------------------------------------------------------------
// Windows-specific: cross-ISA start is refused and points at qemu-tcg
// ---------------------------------------------------------------------------

void testCrossIsaStartRefused() {
    FakeWindowsEnvironment environment;
    environment.snap = windowsHost({QStringLiteral("x86_64"), QStringLiteral("aarch64")}, QStringLiteral("x86_64"));
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    // Cross-ISA: x86_64 host, aarch64 guest.
    qint64 pid = 0;
    QString error;
    check(!engine.start(guestConfig(QStringLiteral("aarch64")), &pid, &error),
          "cross-ISA start is refused");
    checkEqual(environment.startCalls, 0, "cross-ISA start never reaches the spawner");
    check(pid == 0, "cross-ISA start reports no pid");
    check(error.contains(QStringLiteral("qemu-tcg")),
          "cross-ISA start error points at qemu-tcg");

    // Same-ISA start succeeds.
    pid = 0;
    error.clear();
    check(engine.start(guestConfig(QStringLiteral("x86_64")), &pid, &error),
          "same-ISA start succeeds");
    checkEqual(pid, 4242, "same-ISA start reports pid");
}

// ---------------------------------------------------------------------------
// Windows-specific: all unsupported/error reasons must be non-empty
// ---------------------------------------------------------------------------

void testAllReasonsNonEmpty() {
    // Exhaustive check: for every host state that returns false from
    // isHostSupported or supportsGuestArch, the reason must be non-empty.
    struct Case {
        WindowsHostSnapshot snapshot;
        QString guestArch;
        bool expectHostSupported;
        bool expectGuestSupported;
        QString label;
    };

    const QVector<Case> cases = {
        {nonWindowsHost(), QStringLiteral("x86_64"), false, false,
         QStringLiteral("non-windows")},
        {runtimeMissingHost(), QStringLiteral("x86_64"), false, false,
         QStringLiteral("runtime-missing")},
        {whpxApiMissingHost({QStringLiteral("x86_64")}), QStringLiteral("aarch64"), true, false,
         QStringLiteral("whpx-api-missing cross-ISA")},
        {windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64")), QStringLiteral("riscv64"), true, false,
         QStringLiteral("whpx-available unsupported arch")},
    };

    for (const Case& c : cases) {
        FakeWindowsEnvironment environment;
        environment.snap = c.snapshot;
        WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

        QString hostReason;
        const bool hostSupported = engine.isHostSupported(&hostReason);
        checkEqual(hostSupported, c.expectHostSupported,
                   QStringLiteral("%1: isHostSupported").arg(c.label));
        if (!hostSupported) {
            check(!hostReason.isEmpty(),
                  QStringLiteral("%1: host reason is non-empty").arg(c.label));
        }

        QString archReason;
        const bool archSupported = engine.supportsGuestArch(c.guestArch, &archReason);
        checkEqual(archSupported, c.expectGuestSupported,
                   QStringLiteral("%1: supportsGuestArch").arg(c.label));
        if (!archSupported) {
            check(!archReason.isEmpty(),
                  QStringLiteral("%1: arch reason is non-empty").arg(c.label));
        }
    }
}

// ---------------------------------------------------------------------------
// Windows-specific: statusCode stability across repeated queries
// ---------------------------------------------------------------------------

void testStatusCodeStability() {
    FakeWindowsEnvironment environment;
    environment.snap = windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64"));
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    // Query statusCode 10 times — must always return the same value.
    const QString expected = QStringLiteral("available");
    for (int i = 0; i < 10; ++i) {
        checkEqual(engine.statusCode(guestConfig()), expected,
                   QStringLiteral("statusCode is stable on query %1").arg(i));
    }
    // Only one snapshot read should have happened (cached).
    checkEqual(environment.snapshotCalls, 1, "statusCode stability: only one snapshot read");
}

// ---------------------------------------------------------------------------
// Windows-specific: probe diagnostics are non-empty
// ---------------------------------------------------------------------------

void testProbeDiagnosticsNonEmpty() {
    FakeWindowsEnvironment environment;
    environment.snap = windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64"));
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    const EngineProbeResult probe = engine.probe();
    check(!probe.summary.isEmpty(), "probe summary is non-empty");
    for (const EngineIssue& issue : probe.diagnostics) {
        check(!issue.message.isEmpty(),
              QStringLiteral("probe diagnostic message is non-empty (code=%1)").arg(issue.code));
    }
}

// ---------------------------------------------------------------------------
// Windows-specific: validate with malformed config
// ---------------------------------------------------------------------------

void testValidateMalformedConfig() {
    FakeWindowsEnvironment environment;
    environment.snap = windowsHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64"));
    WindowsHypervisorEngine engine(QSharedPointer<WindowsEnvironment>(&environment, [](WindowsEnvironment*) {}));

    // Empty architecture.
    VmConfig emptyArch = guestConfig();
    emptyArch.architecture.clear();
    const QVector<EngineIssue> emptyIssues = engine.validate(emptyArch);
    check(hasIssueAtLevel(emptyIssues, QStringLiteral("guest-arch-missing"), EngineIssueLevel::Error),
          "empty architecture rejected");
    check(!emptyIssues.isEmpty(), "empty architecture has issues");

    // Zero CPU count.
    VmConfig noCpu = guestConfig();
    noCpu.cpuCores = 0;
    const QVector<EngineIssue> noCpuIssues = engine.validate(noCpu);
    check(hasIssueAtLevel(noCpuIssues, QStringLiteral("cpu-count-invalid"), EngineIssueLevel::Error),
          "zero CPU count rejected");

    // Disk path with NUL byte.
    VmConfig nulDisk = guestConfig();
    nulDisk.disks[0].path = QStringLiteral("C:\\VMs") + QChar(0) + QStringLiteral("\\disk.qcow2");
    const QVector<EngineIssue> nulIssues = engine.validate(nulDisk);
    check(hasIssueAtLevel(nulIssues, QStringLiteral("path-invalid"), EngineIssueLevel::Error),
          "NUL in disk path rejected");

    // All error issues must have non-empty messages.
    for (const EngineIssue& issue : emptyIssues + noCpuIssues + nulIssues) {
        if (issue.level == EngineIssueLevel::Error) {
            check(!issue.message.isEmpty(),
                  QStringLiteral("error issue message non-empty (code=%1)").arg(issue.code));
        }
    }
}

} // namespace

int main(int argc, char** argv) {
    QCoreApplication application(argc, argv);
    std::fprintf(stdout, "WindowsHypervisorEngineTests\n");

    testIdentity();
    testCandidateArchitectures();
    testStateMachine();
    testAcceleratorPolicy();
    testExecutionTierPolicy();
    testBackendTierNeverMoves();
    testCrossArchIsRejected();
    testArm64Degraded();
    testNonWindowsHostIsNeverSupported();
    testWhpxApiMissing();
    testWhpxHypervisorNotPresent();
    testRuntimeMissing();
    testStartStopWithEnvironment();
    testRepeatedStartStop();
    testFailureRecovery();
    testStartFailure();
    testNullEnvironment();
    testProbe();
    testValidation();
    testDiagnose();
    testRequirements();
    testRegistryIntegration();
    testLegacyProbeResult();
    testArgumentSafety();
    testSnapshotCaching();
    testEnvironmentSnapshotChange();
    testRuntimeDisappearsAndReturns();

    // Fault injection, lifecycle stress, and Windows-specific coverage.
    testProbeCacheInvalidationUnderMutation();
    testValidateErrorPropagationPerState();
    testStartImmediateFailure();
    testStartSuccessWithZeroPid();
    testStartSuccessWithNegativePid();
    testStopFailure();
    testRepeatedInvalidation();
    testStartStopWithProbeInvalidation();
    testStressStartStopCycles();
    testWhpxDllMissing();
    testWhpxGetCapabilityNoHypervisor();
    testArm64SameIsaDegraded();
    testQemuNotFound();
    testQemuFoundNotExecutable();
    testCrossIsaStartRefused();
    testAllReasonsNonEmpty();
    testStatusCodeStability();
    testProbeDiagnosticsNonEmpty();
    testValidateMalformedConfig();

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