// HarmonyVirtualizationEngineTests.cpp — logic, probe-injection and
// failure-path tests for the HarmonyOS/OpenHarmony compatibility backend.
//
// Design of this suite:
//
//   * Nothing here launches a guest, and nothing needs a HarmonyOS device. The
//     whole decision surface is reached by injecting a scripted
//     HarmonyEnvironment, which is exactly the seam the engine was built around.
//   * The suite is deliberately free of the heavy QEMU plumbing: it links only
//     Qt Core plus the shared capability/platform/config translation units, so
//     it builds and runs on macOS, Linux and Windows.
//   * The honesty invariants are asserted directly:
//       - this backend must never report VirtualizationTier::Native;
//       - a non-HarmonyOS host must never be reported as supported;
//       - an unsupported/degraded state must never reach the process spawner;
//       - start() must never return true without a real, positive process id.
//
// Anything that can only be checked on a real host (the real probe against the
// real platform layer) lives in the separate integration check documented in
// docs/harmonyos-virtualization.md, not here.

#include "HarmonyVirtualizationEngine.hpp"

#include "VirtualizationEngineRegistry.hpp"

#include <QCoreApplication>
#include <QStringList>

#include <atomic>
#include <cstdio>
#include <mutex>
#include <memory>
#include <thread>
#include <vector>

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

// Deterministic LCG for the chaos loop. Seeded so the failure/degradation order
// is reproducible across toolchains (no reliance on <random>).
int harmonyChaosNext(int* state, int bound) {
    *state = (static_cast<long long>(*state) * 1103515245LL + 12345LL) & 0x7FFFFFFF;
    return (*state % bound + bound) % bound;
}

constexpr int kHarmonyChaosRounds = 96;
constexpr int kHarmonyChaosSeed = 0x4A21C0DE;

// ---------------------------------------------------------------------------
// Scripted environment
// ---------------------------------------------------------------------------

class FakeHarmonyEnvironment : public HarmonyEnvironment {
public:
    HarmonyHostSnapshot snap;
    QString programValue = QStringLiteral("/opt/qemu/bin/qemu-system-aarch64");
    QStringList argumentsValue = {QStringLiteral("-machine"), QStringLiteral("virt,accel=tcg")};
    bool startResult = true;
    qint64 startPid = 4242;
    QString startError;
    bool stopResult = true;
    QString stopError;

    // Counters are atomic and the recorded configurations are mutex-guarded
    // because the concurrency test drives this fake from several threads at once.
    // Without that, the fake itself races and TSan noise hides real backend bugs.
    mutable std::atomic<int> snapshotCalls{0};
    mutable std::atomic<int> programCalls{0};
    mutable std::atomic<int> argumentCalls{0};
    std::atomic<int> startCalls{0};
    std::atomic<int> stopCalls{0};
    // Recorded so tests can assert the accelerator decision the engine applied
    // *before* handing the configuration to the environment.
    mutable std::mutex recordMutex_;
    mutable VmConfig lastProgramConfig;
    mutable VmConfig lastArgumentConfig;
    VmConfig lastStartConfig;
    qint64 lastStoppedPid = 0;

    HarmonyHostSnapshot snapshot() const override {
        ++snapshotCalls;
        return snap;
    }
    QString program(const VmConfig& config) const override {
        ++programCalls;
        std::lock_guard<std::mutex> lock(recordMutex_);
        lastProgramConfig = config;
        return programValue;
    }
    QStringList arguments(const VmConfig& config) const override {
        ++argumentCalls;
        std::lock_guard<std::mutex> lock(recordMutex_);
        lastArgumentConfig = config;
        return argumentsValue;
    }
    bool startProcess(const VmConfig& config, qint64* processId, QString* error) override {
        ++startCalls;
        {
            std::lock_guard<std::mutex> lock(recordMutex_);
            lastStartConfig = config;
        }
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

HarmonyHostSnapshot harmonyHost(const QStringList& runnableArches = {QStringLiteral("aarch64")},
                                const QString& hostArch = QStringLiteral("aarch64")) {
    HarmonyHostSnapshot snap;
    snap.hostIsHarmony = true;
    snap.hostFamily = QStringLiteral("harmonyos-linux-compatible");
    snap.hostName = QStringLiteral("HarmonyOS (Linux-compatible mode)");
    snap.hostPlatformName = QStringLiteral("linux");
    snap.hostArch = hostArch;
    snap.qemuProbePerformed = true;
    snap.qemuFound = !runnableArches.isEmpty();
    snap.qemuExecutable = !runnableArches.isEmpty();
    snap.qemuPath = QStringLiteral("/opt/qemu/bin/qemu-system-aarch64");
    snap.qemuSource = QStringLiteral("PATH");
    snap.runnableGuestArches = runnableArches;
    snap.detail = QStringLiteral("test snapshot");
    return snap;
}

HarmonyHostSnapshot nonHarmonyHost() {
    HarmonyHostSnapshot snap;
    snap.hostIsHarmony = false;
    snap.hostFamily = QStringLiteral("macos-apple-silicon");
    snap.hostName = QStringLiteral("macOS");
    snap.hostPlatformName = QStringLiteral("macos");
    snap.hostArch = QStringLiteral("aarch64");
    return snap;
}

HarmonyHostSnapshot blockedHost() {
    HarmonyHostSnapshot snap = harmonyHost({});
    snap.qemuFound = true;
    snap.qemuExecutable = false;
    snap.qemuPath = QStringLiteral("/usr/bin/qemu-system-aarch64");
    snap.qemuSource = QStringLiteral("unrunnable");
    snap.notes << QStringLiteral("QEMU 可执行文件存在但无法运行。");
    return snap;
}

VmConfig guestConfig(const QString& arch = QStringLiteral("aarch64")) {
    VmConfig config;
    config.guestOs = QStringLiteral("linux");
    config.architecture = arch;
    config.machine = QStringLiteral("virt");
    config.cpuModel = QStringLiteral("max");
    config.cpuCores = 4;
    config.memoryMb = 4096;
    DiskConfig disk;
    disk.path = QStringLiteral("/tmp/vw-harmony-test-disk.qcow2");
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
    checkEqual(backend_id::harmonyVirtualization(), QStringLiteral("harmony-virtualization"),
               "shared backend id constant");
    checkEqual(QStringLiteral("harmony-virtualization"), QStringLiteral("harmony-virtualization"),
               "backend id literal");

    FakeHarmonyEnvironment environment;
    environment.snap = harmonyHost();
    HarmonyVirtualizationEngine engine(QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));
    // The lambda deleter keeps the stack-allocated fake alive: we only need the
    // engine to borrow it for the duration of this function.
    checkEqual(engine.backendId(), backend_id::harmonyVirtualization(), "engine backend id");
    check(!engine.displayName().isEmpty(), "engine display name is set");
    check(engine.displayName().contains(QStringLiteral("Harmony")), "display name mentions Harmony");
    check(engine.displayName().contains(QStringLiteral("QEMU")), "display name admits QEMU");
}

void testCandidateArchitectures() {
    checkEqual(harmonyCanonicalGuestArch(QStringLiteral("arm64")), QStringLiteral("aarch64"),
               "arm64 canonicalizes to aarch64");
    checkEqual(harmonyCanonicalGuestArch(QStringLiteral("aarch64")), QStringLiteral("aarch64"),
               "aarch64 accepted");
    checkEqual(harmonyCanonicalGuestArch(QStringLiteral("amd64")), QStringLiteral("x86_64"),
               "amd64 canonicalizes to x86_64");
    checkEqual(harmonyCanonicalGuestArch(QStringLiteral("riscv64")), QStringLiteral("riscv64"),
               "riscv64 accepted");
    checkEqual(harmonyCanonicalGuestArch(QStringLiteral("riscv64gc")), QStringLiteral("riscv64"),
               "riscv64gc canonicalizes to riscv64");

    // Everything else is a QEMU-backend job, never this backend's.
    check(harmonyCanonicalGuestArch(QStringLiteral("ppc64")).isEmpty(), "ppc64 rejected");
    check(harmonyCanonicalGuestArch(QStringLiteral("i386")).isEmpty(), "i386 rejected");
    check(harmonyCanonicalGuestArch(QStringLiteral("m68k")).isEmpty(), "m68k rejected");
    check(harmonyCanonicalGuestArch(QString()).isEmpty(), "empty arch rejected");

    const QStringList candidates = harmonyCandidateGuestArches();
    checkEqual(candidates.size(), 3, "three candidate architectures");
    check(candidates.contains(QStringLiteral("aarch64")), "aarch64 is a candidate");
    check(candidates.contains(QStringLiteral("x86_64")), "x86_64 is a candidate");
    check(candidates.contains(QStringLiteral("riscv64")), "riscv64 is a candidate");
}

// ---------------------------------------------------------------------------
// Host state machine
// ---------------------------------------------------------------------------

void testStateMachine() {
    check(harmonyHostState(nonHarmonyHost(), QStringLiteral("aarch64")) == HarmonyHostState::NotHarmonyHost,
          "non-harmony host state");
    check(!harmonyStateIsUsable(HarmonyHostState::NotHarmonyHost), "non-harmony is not usable");
    checkEqual(harmonyStateCode(HarmonyHostState::NotHarmonyHost),
               QStringLiteral("host-unsupported"), "non-harmony code");

    check(harmonyHostState(harmonyHost({}), QStringLiteral("aarch64")) == HarmonyHostState::RuntimeMissing,
          "missing runtime state");
    checkEqual(harmonyStateCode(HarmonyHostState::RuntimeMissing),
               QStringLiteral("runtime-missing"), "missing runtime code");

    check(harmonyHostState(blockedHost(), QStringLiteral("aarch64")) == HarmonyHostState::RuntimeNotRunnable,
          "blocked runtime state");
    checkEqual(harmonyStateCode(HarmonyHostState::RuntimeNotRunnable),
               QStringLiteral("runtime-not-runnable"), "blocked runtime code");
    check(!harmonyStateIsUsable(HarmonyHostState::RuntimeNotRunnable), "blocked runtime is not usable");

    HarmonyHostSnapshot tcg = harmonyHost();
    check(harmonyHostState(tcg, QStringLiteral("aarch64")) == HarmonyHostState::TcgOnly, "tcg-only state");
    checkEqual(harmonyStateCode(HarmonyHostState::TcgOnly), QStringLiteral("degraded"), "tcg code is degraded");
    check(harmonyStateIsUsable(HarmonyHostState::TcgOnly), "tcg is usable");
    check(!harmonyStateIsAccelerated(HarmonyHostState::TcgOnly), "tcg is not accelerated");

    HarmonyHostSnapshot kvm = harmonyHost();
    kvm.kvmDevicePresent = true;
    kvm.kvmDeviceAccessible = true;
    check(harmonyHostState(kvm, QStringLiteral("aarch64")) == HarmonyHostState::Accelerated,
          "same-isa kvm is accelerated");
    checkEqual(harmonyStateCode(HarmonyHostState::Accelerated),
               QStringLiteral("available"), "accelerated code");
    check(harmonyHostHasAcceleration(kvm), "host has acceleration");

    // Cross-architecture is never accelerated, even with a working /dev/kvm.
    kvm.runnableGuestArches = {QStringLiteral("aarch64"), QStringLiteral("x86_64")};
    check(harmonyHostState(kvm, QStringLiteral("x86_64")) == HarmonyHostState::TcgOnly,
          "x86_64 guest on aarch64 host stays tcg");
    checkEqual(harmonyAcceleratorFor(kvm, QStringLiteral("x86_64")), QStringLiteral("tcg"),
               "cross-arch accelerator is tcg");
    checkEqual(harmonyAcceleratorFor(kvm, QStringLiteral("aarch64")), QStringLiteral("kvm"),
               "same-arch accelerator is kvm");

    // riscv64 has no hardware acceleration on an ARM64 host.
    kvm.runnableGuestArches = {QStringLiteral("aarch64"), QStringLiteral("riscv64")};
    check(harmonyHostState(kvm, QStringLiteral("riscv64")) == HarmonyHostState::TcgOnly,
          "riscv64 guest stays tcg");
    check(!harmonyHostHasAcceleration(harmonyHost()), "no kvm means no acceleration");

    // Every state message must be non-empty: it is forwarded verbatim as
    // `QString* reason` by the shared contract.
    const HarmonyHostState all[] = {HarmonyHostState::NotHarmonyHost,
                                    HarmonyHostState::RuntimeMissing,
                                    HarmonyHostState::RuntimeNotRunnable,
                                    HarmonyHostState::TcgOnly,
                                    HarmonyHostState::Accelerated};
    for (HarmonyHostState state : all) {
        check(!harmonyStateMessage(state, QStringLiteral("aarch64")).isEmpty(),
              QStringLiteral("state %1 has a message").arg(static_cast<int>(state)));
    }
}

// The RuntimeMissing message interpolates the architecture twice through two
// `%1` markers. Qt's arg() replaces every occurrence of the lowest-numbered
// marker in one call, so exactly one .arg() may be used; a second one (or the
// two-value form) makes Qt print "Argument missing" to stderr at runtime.
// This test pins the correct behaviour and, importantly, the resulting text.
void testStateMessageInterpolation() {
    const QString missing = harmonyStateMessage(HarmonyHostState::RuntimeMissing,
                                                QStringLiteral("aarch64"));
    check(!missing.contains(QStringLiteral("%1")), "no placeholder survives interpolation");
    check(!missing.contains(QStringLiteral("%2")), "no stray second placeholder");
    check(missing.count(QStringLiteral("aarch64")) >= 2,
          "the architecture is interpolated into both positions");
    check(missing.contains(QStringLiteral("qemu-system-aarch64")),
          "the message names the expected binary");
    check(missing.contains(QStringLiteral("不会回退")),
          "the message explicitly forbids the external-process fallback");

    // An unknown/unsupported architecture must still produce readable text.
    const QString unknown = harmonyStateMessage(HarmonyHostState::RuntimeMissing, QStringLiteral("ppc64"));
    check(!unknown.contains(QStringLiteral("%1")), "unknown arch message has no placeholder");
    check(!unknown.isEmpty(), "unknown arch message is non-empty");

    // And the empty case must not produce a broken sentence.
    const QString empty = harmonyStateMessage(HarmonyHostState::RuntimeMissing, QString());
    check(!empty.contains(QStringLiteral("%1")), "empty arch message has no placeholder");
    check(!empty.isEmpty(), "empty arch message is non-empty");
}

// ---------------------------------------------------------------------------
// The engine never over-claims
// ---------------------------------------------------------------------------

void testNeverClaimsNativeTier() {
    const QVector<HarmonyHostSnapshot> hosts = {nonHarmonyHost(), harmonyHost({}), blockedHost(),
                                                harmonyHost(), harmonyHost()};
    HarmonyHostSnapshot kvm = harmonyHost();
    kvm.kvmDevicePresent = true;
    kvm.kvmDeviceAccessible = true;

    QVector<HarmonyHostSnapshot> all = hosts;
    all.push_back(kvm);
    for (const HarmonyHostSnapshot& snap : all) {
        FakeHarmonyEnvironment environment;
        environment.snap = snap;
        HarmonyVirtualizationEngine engine(
            QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));
        check(engine.tier() != VirtualizationTier::Native,
              QStringLiteral("tier is never Native (state for aarch64 = %1)")
                  .arg(harmonyStateCode(harmonyHostState(snap, QStringLiteral("aarch64")))));
        check(!engine.probe().capabilities.nativeExecution,
              QStringLiteral("nativeExecution is never claimed"));
    }
}

void testNonHarmonyHostIsNeverSupported() {
    FakeHarmonyEnvironment environment;
    environment.snap = nonHarmonyHost();
    HarmonyVirtualizationEngine engine(
        QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));

    QString reason;
    check(!engine.isHostSupported(&reason), "non-harmony host unsupported");
    check(!reason.isEmpty(), "non-harmony reason is filled");
    check(reason.contains(QStringLiteral("QEMU")), "non-harmony reason points at QEMU");

    check(!engine.supportsGuestArch(QStringLiteral("aarch64"), &reason), "non-harmony arch unsupported");
    check(!reason.isEmpty(), "non-harmony arch reason filled");

    const EngineProbeResult probe = engine.probe();
    check(!probe.hostSupported, "non-harmony probe hostSupported false");
    check(!probe.capabilities.canStart, "non-harmony canStart false");
    check(!probe.capabilities.canStop, "non-harmony canStop false");
    check(!probe.capabilities.guestArchSupported, "non-harmony guestArchSupported false");
    check(!probe.capabilities.nativeExecution, "non-harmony nativeExecution false");
    check(!probe.summary.isEmpty(), "non-harmony summary present");
    checkEqual(engine.statusCode(guestConfig()), QStringLiteral("host-unsupported"),
               "non-harmony status code");
    check(engine.tier() == VirtualizationTier::SoftwareEmulation, "non-harmony tier is emulation");

    check(hasIssue(engine.validate(guestConfig()), QStringLiteral("host-unsupported")),
          "non-harmony validate reports host-unsupported");

    // The critical guarantee: nothing is ever spawned off-platform.
    qint64 pid = 0;
    QString error;
    check(!engine.start(guestConfig(), &pid, &error), "non-harmony start fails");
    checkEqual(environment.startCalls, 0, "non-harmony start never spawns a process");
    checkEqual(environment.argumentCalls, 0, "non-harmony start never builds arguments");
    checkEqual(environment.programCalls, 0, "non-harmony start never resolves a program");
    check(pid == 0, "non-harmony start leaves pid untouched");
    check(!error.isEmpty(), "non-harmony start error is filled");
}

// ---------------------------------------------------------------------------
// Degraded / unsupported paths
// ---------------------------------------------------------------------------

void testRuntimeMissing() {
    FakeHarmonyEnvironment environment;
    environment.snap = harmonyHost({});
    HarmonyVirtualizationEngine engine(
        QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));

    QString reason;
    check(!engine.isHostSupported(&reason), "missing runtime is unsupported");
    check(reason.contains(QStringLiteral("QEMU")), "missing runtime reason mentions QEMU");

    check(!engine.supportsGuestArch(QStringLiteral("aarch64"), &reason),
          "missing runtime arch unsupported");
    check(!reason.isEmpty(), "missing runtime arch reason filled");

    const QVector<EngineIssue> issues = engine.validate(guestConfig());
    check(hasIssueAtLevel(issues, QStringLiteral("runtime-missing"), EngineIssueLevel::Error),
          "missing runtime is an error");
    check(hasEngineErrors(issues), "missing runtime has errors");
    checkEqual(engine.statusCode(guestConfig()), QStringLiteral("runtime-missing"),
               "missing runtime status code");

    qint64 pid = 0;
    QString error;
    check(!engine.start(guestConfig(), &pid, &error), "missing runtime start fails");
    checkEqual(environment.startCalls, 0, "missing runtime never spawns");
    check(!error.isEmpty(), "missing runtime start error filled");
}

void testRuntimeNotRunnable() {
    FakeHarmonyEnvironment environment;
    environment.snap = blockedHost();
    HarmonyVirtualizationEngine engine(
        QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));

    QString reason;
    check(!engine.isHostSupported(&reason), "blocked runtime is unsupported");
    check(reason.contains(QStringLiteral("QEMU")) || reason.contains(QStringLiteral("执行")),
          "blocked runtime reason explains itself");

    const QVector<EngineIssue> issues = engine.validate(guestConfig());
    check(hasIssueAtLevel(issues, QStringLiteral("runtime-not-runnable"), EngineIssueLevel::Error),
          "blocked runtime is an error");
    checkEqual(engine.statusCode(guestConfig()), QStringLiteral("runtime-not-runnable"),
               "blocked runtime status code");

    const EngineProbeResult probe = engine.probe();
    check(!probe.hostSupported, "blocked runtime probe unsupported");
    check(!probe.capabilities.canStart, "blocked runtime cannot start");

    qint64 pid = 0;
    QString error;
    check(!engine.start(guestConfig(), &pid, &error), "blocked runtime start fails");
    checkEqual(environment.startCalls, 0, "blocked runtime never spawns");
    // This is the HarmonyOS NEXT sandbox case: the backend must say so instead of
    // pretending the guest started.
    check(engine.statusCode(guestConfig()) != QStringLiteral("available"),
          "blocked runtime never reports available");
}

// A wedged QEMU binary must surface as a bounded, diagnosable timeout rather than
// hanging the capability query or being silently treated as "available".
void testRuntimeProbeTimeout() {
    FakeHarmonyEnvironment environment;
    environment.snap = harmonyHost({});
    environment.snap.qemuFound = true;
    environment.snap.qemuExecutable = false;
    environment.snap.qemuProbeTimedOut = true;
    environment.snap.notes << QStringLiteral("QEMU --version 探测超时。");
    HarmonyVirtualizationEngine engine(
        QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));

    const EngineProbeResult probe = engine.probe();
    check(hasIssue(probe.diagnostics, QStringLiteral("runtime-probe-timeout")),
          "probe reports the runtime probe timeout");
    check(!probe.hostSupported, "a timed-out probe is not treated as supported");
    check(!probe.capabilities.canStart, "a timed-out probe cannot start");

    const QVector<EngineIssue> issues = engine.validate(guestConfig());
    check(hasEngineErrors(issues), "timed-out runtime is a hard error");
    check(hasIssue(issues, QStringLiteral("runtime-not-runnable")),
          "timed-out runtime is reported as not runnable");

    qint64 pid = 0;
    QString error;
    check(!engine.start(guestConfig(), &pid, &error), "timed-out runtime never starts");
    checkEqual(environment.startCalls, 0, "timed-out runtime never spawns");
}

void testTcgDegradedIsUsableButHonest() {
    FakeHarmonyEnvironment environment;
    environment.snap = harmonyHost();
    HarmonyVirtualizationEngine engine(
        QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));

    check(engine.tier() == VirtualizationTier::SoftwareEmulation, "tcg-only tier is emulation");
    check(!engine.probe().capabilities.hardwareAcceleration, "tcg-only has no hardware acceleration");
    checkEqual(engine.probe().capabilities.accelerator, QStringLiteral("tcg"), "tcg accelerator reported");
    check(engine.isHostSupported(), "tcg-only host is supported");
    check(engine.supportsGuestArch(QStringLiteral("aarch64")), "tcg-only supports its arch");

    const QVector<EngineIssue> issues = engine.validate(guestConfig());
    // Degraded must be a warning, not an error: the registry would otherwise skip
    // the only backend that can actually run here.
    check(!hasEngineErrors(issues), "tcg-only validate has no errors");
    check(hasIssueAtLevel(issues, QStringLiteral("software-emulation"), EngineIssueLevel::Warning),
          "tcg-only reports a degradation warning");
    checkEqual(engine.statusCode(guestConfig()), QStringLiteral("degraded"), "tcg-only status is degraded");

    const QString diagnosis = engine.diagnose(guestConfig());
    check(diagnosis.contains(QStringLiteral("tcg")), "diagnosis mentions tcg");
    check(diagnosis.contains(QStringLiteral("QEMU")) || diagnosis.contains(QStringLiteral("qemu")),
          "diagnosis mentions QEMU");
}

// ---------------------------------------------------------------------------
// Configuration validation
// ---------------------------------------------------------------------------

void testConfigValidation() {
    FakeHarmonyEnvironment environment;
    environment.snap = harmonyHost();
    HarmonyVirtualizationEngine engine(
        QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));

    check(!hasEngineErrors(engine.validate(guestConfig())), "baseline config is valid");

    VmConfig systemSoftware = guestConfig();
    systemSoftware.backend = QStringLiteral("system-software");
    check(hasIssue(engine.validate(systemSoftware), QStringLiteral("backend-mismatch")),
          "system-software config rejected");

    VmConfig noArch = guestConfig();
    noArch.architecture.clear();
    check(hasIssue(engine.validate(noArch), QStringLiteral("guest-arch-missing")),
          "empty architecture rejected");

    VmConfig ppc = guestConfig(QStringLiteral("ppc64"));
    check(hasIssue(engine.validate(ppc), QStringLiteral("guest-arch-unsupported")),
          "unsupported architecture rejected");

    VmConfig badCpu = guestConfig();
    badCpu.cpuCores = 0;
    check(hasIssue(engine.validate(badCpu), QStringLiteral("cpu-count-invalid")),
          "zero cpu count rejected");

    VmConfig emptyDisk = guestConfig();
    emptyDisk.disks[0].path.clear();
    check(hasIssue(engine.validate(emptyDisk), QStringLiteral("disk-path-empty")),
          "empty disk path rejected");

    VmConfig nulDisk = guestConfig();
    nulDisk.disks[0].path = QStringLiteral("/tmp/disk") + QChar(0) + QStringLiteral(".qcow2");
    check(hasIssue(engine.validate(nulDisk), QStringLiteral("path-invalid")),
          "disk path with NUL rejected");

    VmConfig newlineKernel = guestConfig();
    newlineKernel.boot.kernelPath = QStringLiteral("/tmp/vmlinuz\nrm -rf /");
    check(hasIssue(engine.validate(newlineKernel), QStringLiteral("path-invalid")),
          "kernel path with newline rejected");

    VmConfig badExtra = guestConfig();
    badExtra.extraArgs << QStringLiteral("-device\nsomething");
    check(hasIssue(engine.validate(badExtra), QStringLiteral("argument-invalid")),
          "extra argument with newline rejected");

    VmConfig unsupported = guestConfig();
    unsupported.unsupportedArgs << QStringLiteral("-device") << QStringLiteral("pci-test");
    const QVector<EngineIssue> unsupportedIssues = engine.validate(unsupported);
    check(hasIssueAtLevel(unsupportedIssues, QStringLiteral("unsupported-arguments"),
                          EngineIssueLevel::Warning),
          "unsupported args are a warning");
    check(!hasEngineErrors(unsupportedIssues), "unsupported args alone are not fatal");
}

// ---------------------------------------------------------------------------
// Argument construction
// ---------------------------------------------------------------------------

void testArgumentVectorSafety() {
    check(!harmonyArgumentsAreWellFormed({}), "empty argument vector rejected");
    check(harmonyArgumentsAreWellFormed({QStringLiteral("-machine"), QStringLiteral("virt,accel=tcg")}),
          "separate flag and value accepted");
    // A dash-prefixed element containing whitespace is the signature of several
    // flags having been concatenated into one string.
    check(!harmonyArgumentsAreWellFormed({QStringLiteral("-machine virt,accel=tcg")}),
          "concatenated flags rejected");
    // A path or command line with spaces is fine: it is its own element.
    check(harmonyArgumentsAreWellFormed({QStringLiteral("-kernel"),
                                         QStringLiteral("/tmp/My VMs/vmlinuz")}),
          "path containing spaces accepted");
    check(harmonyArgumentsAreWellFormed({QStringLiteral("-append"),
                                         QStringLiteral("console=ttyS0 root=/dev/vda")}),
          "kernel command line containing spaces accepted");
    check(!harmonyArgumentsAreWellFormed({QStringLiteral("-kernel"),
                                          QStringLiteral("/tmp/x") + QChar(0)}),
          "NUL in argument rejected");
    check(!harmonyArgumentsAreWellFormed({QStringLiteral("-kernel"), QStringLiteral("a\nb")}),
          "newline in argument rejected");
    check(!harmonyArgumentsAreWellFormed({QString()}), "null argument rejected");

    QString problem;
    check(!harmonyArgumentsAreWellFormed({}, &problem), "problem out-param set on failure");
    check(!problem.isEmpty(), "problem message filled");
    problem.clear();
    check(harmonyArgumentsAreWellFormed({QStringLiteral("-m"), QStringLiteral("2048")}, &problem),
          "valid vector reports well formed");
    check(problem.isEmpty(), "problem cleared on success");

    check(harmonyContainsUnsafeCharacter(QStringLiteral("a") + QChar(0)), "NUL detected");
    check(harmonyContainsUnsafeCharacter(QStringLiteral("a\nb")), "newline detected");
    check(harmonyContainsUnsafeCharacter(QStringLiteral("a\rb")), "carriage return detected");
    check(!harmonyContainsUnsafeCharacter(QStringLiteral("/tmp/normal path/disk.qcow2")),
          "ordinary path is safe");
}

void testBuildArgumentsAppliesAccelerator() {
    FakeHarmonyEnvironment environment;
    environment.snap = harmonyHost();
    environment.argumentsValue = {QStringLiteral("-machine"), QStringLiteral("virt,accel=tcg"),
                                  QStringLiteral("-smp"), QStringLiteral("4")};
    HarmonyVirtualizationEngine engine(
        QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));

    const QStringList arguments = engine.buildArguments(guestConfig());
    check(!arguments.isEmpty(), "buildArguments returns the environment vector");
    check(harmonyArgumentsAreWellFormed(arguments), "environment vector is well formed");
    // Without a usable accelerator the backend must ask for tcg, never kvm.
    checkEqual(environment.lastArgumentConfig.accelerator, QStringLiteral("tcg"),
               "no kvm means the backend requests tcg");
    check(environment.programCalls == 0, "buildArguments does not resolve the program");

    // With a usable same-ISA accelerator it must request kvm instead.
    environment.snap.kvmDevicePresent = true;
    environment.snap.kvmDeviceAccessible = true;
    engine.invalidateProbe();
    environment.argumentsValue = {QStringLiteral("-machine"), QStringLiteral("virt,accel=kvm")};
    engine.buildArguments(guestConfig());
    checkEqual(environment.lastArgumentConfig.accelerator, QStringLiteral("kvm"),
               "same-isa kvm is requested when available");
    checkEqual(engine.programFor(guestConfig()), environment.programValue,
               "programFor forwards the environment program");
    checkEqual(environment.lastProgramConfig.accelerator, QStringLiteral("kvm"),
               "program resolution sees the accelerator decision too");

    // A cross-architecture guest must never ask for kvm, even with a working
    // /dev/kvm on the host.
    environment.snap.runnableGuestArches = {QStringLiteral("aarch64"), QStringLiteral("x86_64")};
    engine.invalidateProbe();
    engine.buildArguments(guestConfig(QStringLiteral("x86_64")));
    checkEqual(environment.lastArgumentConfig.accelerator, QStringLiteral("tcg"),
               "cross-arch guest always requests tcg");

    // And an architecture with no emulator at all must not resolve a program.
    environment.argumentsValue = {QStringLiteral("-machine"), QStringLiteral("virt,accel=tcg")};
    engine.buildArguments(guestConfig(QStringLiteral("riscv64")));
    checkEqual(environment.lastArgumentConfig.accelerator, QStringLiteral("tcg"),
               "unavailable arch still reports tcg, never kvm");
}

void testNoShellStringIsProduced() {
    // Structural guarantee: the argument surface is always a vector. A single
    // string rendering would have to go through renderCommand-style APIs, which
    // this backend deliberately does not expose or call.
    FakeHarmonyEnvironment environment;
    environment.snap = harmonyHost();
    // An injected vector with shell metacharacters must survive as data, not be
    // reinterpreted: it stays one element and is rejected only if it looks like
    // concatenated flags.
    environment.argumentsValue = {QStringLiteral("-append"),
                                  QStringLiteral("root=/dev/vda; rm -rf /")};
    HarmonyVirtualizationEngine engine(
        QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));
    const QStringList arguments = engine.buildArguments(guestConfig());
    checkEqual(arguments.size(), 2, "semicolon payload stays inside one element");
    check(arguments.at(1).contains(QStringLiteral(";")), "payload preserved as data");
    check(harmonyArgumentsAreWellFormed(arguments), "semicolon payload is still a valid vector");
}

// ---------------------------------------------------------------------------
// Lifecycle failure paths
// ---------------------------------------------------------------------------

void testStartSuccessAndFailurePaths() {
    // --- success -------------------------------------------------------------
    {
        FakeHarmonyEnvironment environment;
        environment.snap = harmonyHost();
        environment.startPid = 987654;
        HarmonyVirtualizationEngine engine(
            QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));
        qint64 pid = 0;
        QString error;
        check(engine.start(guestConfig(), &pid, &error), "start succeeds when the environment does");
        checkEqual(environment.startCalls, 1, "start spawns exactly once");
        checkEqual(static_cast<int>(pid), 987654, "start reports the environment pid");
        check(error.isEmpty(), "successful start leaves the error empty");
        // The accelerator decision must have been applied before spawning.
        checkEqual(environment.lastStartConfig.accelerator, QStringLiteral("tcg"),
                   "start requests tcg without kvm");
    }

    // --- environment refuses to spawn ---------------------------------------
    {
        FakeHarmonyEnvironment environment;
        environment.snap = harmonyHost();
        environment.startResult = false;
        environment.startError = QStringLiteral("spawn denied by device policy");
        HarmonyVirtualizationEngine engine(
            QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));
        qint64 pid = 0;
        QString error;
        check(!engine.start(guestConfig(), &pid, &error), "start fails when the environment refuses");
        checkEqual(error, QStringLiteral("spawn denied by device policy"), "spawn error propagated");
        check(pid == 0, "failed start leaves pid untouched");
    }

    // --- spawner gives no pid: never claim started ---------------------------
    {
        FakeHarmonyEnvironment environment;
        environment.snap = harmonyHost();
        environment.startPid = 0;
        HarmonyVirtualizationEngine engine(
            QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));
        qint64 pid = 0;
        QString error;
        check(!engine.start(guestConfig(), &pid, &error), "start refuses a zero pid");
        check(!error.isEmpty(), "zero pid error explained");
        check(error.contains(QStringLiteral("会话令牌")), "zero token error names the session token");
        check(environment.startCalls == 1, "spawn was attempted once");
    }

    // --- malformed argument vector: refuse before spawning -------------------
    {
        FakeHarmonyEnvironment environment;
        environment.snap = harmonyHost();
        environment.argumentsValue = {QStringLiteral("-machine virt,accel=tcg")};
        HarmonyVirtualizationEngine engine(
            QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));
        qint64 pid = 0;
        QString error;
        check(!engine.start(guestConfig(), &pid, &error), "start refuses a malformed vector");
        checkEqual(environment.startCalls, 0, "malformed vector never reaches the spawner");
        check(!error.isEmpty(), "malformed vector error filled");
    }

    // --- no resolvable program ----------------------------------------------
    {
        FakeHarmonyEnvironment environment;
        environment.snap = harmonyHost();
        environment.programValue.clear();
        HarmonyVirtualizationEngine engine(
            QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));
        qint64 pid = 0;
        QString error;
        check(!engine.start(guestConfig(), &pid, &error), "start refuses without a program");
        checkEqual(environment.startCalls, 0, "missing program never reaches the spawner");
        check(!error.isEmpty(), "missing program error filled");
    }
}

void testStopPaths() {
    FakeHarmonyEnvironment environment;
    environment.snap = harmonyHost();
    HarmonyVirtualizationEngine engine(
        QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));

    QString error;
    check(!engine.stop(0, &error), "stop rejects pid 0");
    check(!error.isEmpty(), "pid 0 error filled");
    error.clear();
    check(!engine.stop(-5, &error), "stop rejects a negative pid");
    checkEqual(environment.stopCalls, 0, "invalid pids never reach the environment");

    // A token must be one this engine actually issued via start() before it is
    // forwarded to the environment (no PID semantics, no forwarding of unknown ids).
    qint64 startedPid = 0;
    check(engine.start(guestConfig(), &startedPid, &error), "stop: start issues a stoppable token");
    check(startedPid > 0, "stop: start returns a positive opaque token");
    check(engine.stop(startedPid, &error), "stop delegates the issued token");
    checkEqual(environment.stopCalls, 1, "stop called once");
    checkEqual(static_cast<int>(environment.lastStoppedPid), static_cast<int>(startedPid),
               "stop forwards the token");

    environment.stopResult = false;
    environment.stopError = QStringLiteral("kill failed");
    check(!engine.stop(startedPid, &error), "stop propagates environment failure");
    checkEqual(error, QStringLiteral("kill failed"), "stop error propagated");

    // An engine explicitly built without an environment must fail cleanly rather
    // than dereference null. The null is honoured literally (it does not install
    // the real environment), so this cannot accidentally spawn a process.
    HarmonyVirtualizationEngine noEnvironment{QSharedPointer<HarmonyEnvironment>()};
    error.clear();
    check(!noEnvironment.stop(1, &error), "engine without environment refuses stop");
    check(!error.isEmpty(), "no-environment stop explains itself");
    error.clear();
    qint64 pid = 0;
    check(!noEnvironment.start(guestConfig(), &pid, &error), "engine without environment refuses start");
    check(!error.isEmpty(), "no-environment start explains itself");
    check(pid == 0, "no-environment start leaves pid untouched");
}

// ---------------------------------------------------------------------------
// Probe caching and requirements
// ---------------------------------------------------------------------------

void testProbeIsCached() {
    FakeHarmonyEnvironment environment;
    environment.snap = harmonyHost();
    HarmonyVirtualizationEngine engine(
        QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));

    check(engine.isHostSupported(), "cached probe: supported");
    engine.probe();
    engine.tier();
    engine.supportsGuestArch(QStringLiteral("aarch64"));
    checkEqual(environment.snapshotCalls, 1, "host is read exactly once");

    engine.invalidateProbe();
    engine.isHostSupported();
    checkEqual(environment.snapshotCalls, 2, "invalidateProbe forces a re-read");
}

void testRequirementsAreHonest() {
    FakeHarmonyEnvironment environment;
    environment.snap = harmonyHost();
    HarmonyVirtualizationEngine engine(
        QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));
    const QStringList requirements = engine.requirements();
    check(!requirements.isEmpty(), "requirements are not empty");
    const QString joined = requirements.join(QStringLiteral(" | "));
    check(joined.contains(QStringLiteral("/dev/kvm")), "requirements mention /dev/kvm");
    check(joined.contains(QStringLiteral("QEMU")), "requirements mention QEMU");
    check(joined.contains(QStringLiteral("Harmony")), "requirements name the host family");
    check(joined.contains(QStringLiteral("禁止使用 PATH")),
          "requirements prohibit external executable fallback");
}

void testProbeForArchitecture() {
    FakeHarmonyEnvironment environment;
    environment.snap = harmonyHost({QStringLiteral("aarch64"), QStringLiteral("x86_64")});
    HarmonyVirtualizationEngine engine(
        QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));

    const EngineProbeResult arm = engine.probeFor(QStringLiteral("arm64"));
    check(arm.capabilities.guestArchSupported, "probeFor accepts a runnable arch");
    check(arm.capabilities.canStart, "probeFor marks canStart for a runnable arch");

    const EngineProbeResult ppc = engine.probeFor(QStringLiteral("ppc64"));
    check(!ppc.capabilities.guestArchSupported, "probeFor rejects an unsupported arch");
    check(!ppc.capabilities.canStart, "probeFor clears canStart for an unsupported arch");
    check(!ppc.summary.isEmpty(), "probeFor explains the rejection");

    environment.snap.runnableGuestArches = {QStringLiteral("aarch64")};
    environment.snap.kvmDevicePresent = true;
    environment.snap.kvmDeviceAccessible = true;
    engine.invalidateProbe();
    const EngineProbeResult x86 = engine.probeFor(QStringLiteral("x86_64"));
    // x86_64 has no emulator in this snapshot, so it is unsupported, not merely
    // unaccelerated.
    check(!x86.capabilities.guestArchSupported || !x86.capabilities.hardwareAcceleration,
          "cross-arch guest is not accelerated");
}

void testRealHostIsNotMisreported() {
    // This runs on whatever machine executes the suite. The backend is scoped to
    // HarmonyOS/OpenHarmony, so on macOS/Linux/Windows it must report
    // host-unsupported. The only exception is a genuine HarmonyOS/OpenHarmony
    // host, which is reported as a skip instead of a guess.
    const HostProfile host = detectHostProfile();
    const bool harmonyFamily = host.family.contains(QStringLiteral("harmonyos"), Qt::CaseInsensitive)
        || host.family.contains(QStringLiteral("openharmony"), Qt::CaseInsensitive);
    if (harmonyFamily) {
        skip(QStringLiteral("real-host check: this machine really is %1").arg(host.family));
        return;
    }

    // A snapshot built the same way the real probe builds it off-platform.
    FakeHarmonyEnvironment environment;
    environment.snap = nonHarmonyHost();
    environment.snap.hostFamily = host.family;
    environment.snap.hostArch = canonicalHostArch(host.arch);
    HarmonyVirtualizationEngine engine(
        QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));

    QString reason;
    check(!engine.isHostSupported(&reason),
          QStringLiteral("real host (%1 / %2) is not misreported as Harmony-supported")
              .arg(host.family, host.arch));
    checkEqual(engine.statusCode(guestConfig(host.arch)), QStringLiteral("host-unsupported"),
               "real host status code is host-unsupported");
    check(engine.tier() != VirtualizationTier::Native, "real host tier is never Native");
}

// ---------------------------------------------------------------------------
// Real integration: the actual environment, factory and registry
// ---------------------------------------------------------------------------
//
// These do touch the real platform layer, but they never launch a guest: on a
// non-HarmonyOS host every path is rejected before the spawner is reached. On a
// real HarmonyOS/OpenHarmony host the parts whose outcome depends on what is
// installed on that device degrade to a skip.

bool realHostIsHarmony() {
    const HostProfile host = detectHostProfile();
    return host.family.contains(QStringLiteral("harmonyos"), Qt::CaseInsensitive)
        || host.family.contains(QStringLiteral("openharmony"), Qt::CaseInsensitive);
}

void testRealEnvironment() {
    QSharedPointer<HarmonyEnvironment> environment = makeHarmonyEnvironment();
    check(!environment.isNull(), "makeHarmonyEnvironment returns a real environment");
    if (environment.isNull()) {
        return;
    }

    const HarmonyHostSnapshot snap = environment->snapshot();
    check(!snap.hostFamily.isEmpty(), "real snapshot has a host family");
    check(!snap.hostArch.isEmpty(), "real snapshot has a host arch");
    check(snap.hostIsHarmony == realHostIsHarmony(),
          "real snapshot agrees with the shared host probe about the Harmony family");
    if (!snap.hostIsHarmony) {
        // The important off-platform guarantee: no QEMU probing happened at all,
        // so a macOS/Linux/Windows capability query stays cheap and side-effect free.
        check(!snap.qemuProbePerformed, "off-platform snapshot performed no QEMU probe");
        check(snap.runnableGuestArches.isEmpty(), "off-platform snapshot has no runnable arches");
        check(!snap.detail.isEmpty(), "off-platform snapshot explains itself");
    }
}

void testFactoryAndRealEngine() {
    if (!realHostIsHarmony()) {
        check(!harmonyVirtualizationEngineAvailable(),
              "factory reports unavailable on a non-HarmonyOS host");
    }

    VirtualizationEnginePtr engine = makeHarmonyVirtualizationEngine();
    check(!engine.isNull(), "factory returns an engine");
    if (engine.isNull()) {
        return;
    }
    checkEqual(engine->backendId(), backend_id::harmonyVirtualization(), "factory engine id");
    check(engine->tier() != VirtualizationTier::Native, "factory engine is never native tier");
    check(!engine->requirements().isEmpty(), "factory engine lists requirements");

    if (realHostIsHarmony()) {
        skip(QStringLiteral("factory/real-engine off-platform assertions: this host really is "
                            "HarmonyOS/OpenHarmony"));
        return;
    }

    QString reason;
    check(!engine->isHostSupported(&reason), "factory engine unsupported off-platform");
    check(!reason.isEmpty(), "factory engine explains the lack of support");
    checkEqual(engine->statusCode(guestConfig()), QStringLiteral("host-unsupported"),
               "factory engine status off-platform");

    // Never claims a start, and never reaches a spawner.
    qint64 pid = 0;
    QString error;
    check(!engine->start(guestConfig(), &pid, &error), "factory engine start fails off-platform");
    check(pid == 0, "factory engine never reports a pid off-platform");
    check(!error.isEmpty(), "factory engine start error filled");
}

void testRegistryRegistration() {
    VirtualizationEngineRegistry& registry = VirtualizationEngineRegistry::instance();
    registry.registerBuiltinBackends();

    // Self-registration from HarmonyVirtualizationEngine.cpp happens before main()
    // and must not depend on registerBuiltinBackends().
    check(registry.hasBackend(backend_id::harmonyVirtualization()),
          "harmony backend self-registered into the registry");
    VirtualizationEnginePtr registered = registry.backend(backend_id::harmonyVirtualization());
    check(!registered.isNull(), "registry returns the harmony backend");
    if (registered) {
        check(registered->tier() != VirtualizationTier::Native, "registered backend tier is honest");
    }

    const QStringList ids = registry.backendIds();
    check(ids.contains(backend_id::harmonyVirtualization()), "backend id list contains harmony");
    check(ids.contains(backend_id::qemuTcg()), "qemu-tcg remains available as the fallback");

    // The point of the honest host-unsupported state: auto-selection must never
    // pick this backend, and wherever the selector lands it must name QEMU as the
    // carrier. Both branches are asserted unconditionally — whether QEMU is
    // installed on this host or not — because "auto-select always succeeds" is
    // not part of the contract, while "never selects a backend that cannot run
    // the guest, and always says why" is.
    if (realHostIsHarmony()) {
        skip(QStringLiteral("registry selection assertions: this host really is HarmonyOS/OpenHarmony"));
        return;
    }
    const EngineSelection selection = registry.select(guestConfig());
    check(selection.backendId != backend_id::harmonyVirtualization(),
          "auto-selection does not choose the harmony backend off-platform");
    if (selection.ok) {
        check(selection.backendId == backend_id::qemuHardware()
                  || selection.backendId == backend_id::qemuTcg(),
              "auto-selection lands on a QEMU backend when it succeeds off-platform");
    } else {
        // No QEMU runtime on this machine means no backend can satisfy the guest
        // configuration, so failing is the correct outcome. It must still be
        // attributed to QEMU rather than to this backend, and every considered
        // backend must have produced a reason.
        check(selection.reason.contains(QStringLiteral("qemu")),
              "failed auto-selection names a QEMU backend as the fallback");
        bool allAttributed = true;
        for (const BackendChoice& choice : selection.considered) {
            if (choice.reason.isEmpty() || choice.statusCode.isEmpty()) {
                allAttributed = false;
            }
        }
        check(allAttributed, "every rejected backend carries a stable code and a reason");
    }
    bool sawHarmony = false;
    for (const BackendChoice& choice : selection.considered) {
        if (choice.backendId == backend_id::harmonyVirtualization()) {
            sawHarmony = true;
            check(!choice.hostSupported, "harmony backend recorded as host-unsupported");
            check(!choice.selected, "harmony backend recorded as not selected");
            check(!choice.reason.isEmpty(), "harmony rejection reason recorded");
            check(choice.statusCode == QStringLiteral("host-unsupported"),
                  "harmony backend recorded with the stable host-unsupported code");
        }
    }
    check(sawHarmony, "harmony backend appears in the selection audit trail");
}

// ---------------------------------------------------------------------------
// Added regression coverage (phase 3)
// ---------------------------------------------------------------------------

// Explicit host x guest architecture matrix. Acceleration is same-ISA only, and
// a guest architecture is only runnable when a probe found its emulator.
void testArchMatrixAcrossHosts() {
    struct Row {
        const char* hostArch;
        const char* guestArch;
        bool runnable;
        bool expectSupported;
        bool expectAccelerated;
    };
    const Row rows[] = {
        // ARM64 host (the priority platform).
        {"aarch64", "aarch64", true, true, true},
        {"aarch64", "x86_64", true, true, false},
        {"aarch64", "riscv64", true, true, false},
        // x86_64 host.
        {"x86_64", "x86_64", true, true, true},
        {"x86_64", "aarch64", true, true, false},
        {"x86_64", "riscv64", true, true, false},
        // Guest emulator missing: not runnable, so not supported at all.
        {"aarch64", "x86_64", false, false, false},
        {"aarch64", "riscv64", false, false, false},
    };

    for (const Row& row : rows) {
        const QStringList runnable =
            row.runnable ? QStringList{QString::fromLatin1(row.guestArch)} : QStringList();
        HarmonyHostSnapshot snap = harmonyHost(runnable, QString::fromLatin1(row.hostArch));
        snap.kvmDevicePresent = true;
        snap.kvmDeviceAccessible = true;

        const QString guest = QString::fromLatin1(row.guestArch);
        const HarmonyHostState state = harmonyHostState(snap, guest);
        const QString label = QStringLiteral("matrix %1 host / %2 guest")
                                  .arg(QString::fromLatin1(row.hostArch), guest);

        check(harmonyStateIsUsable(state) == row.expectSupported, label + " supported");
        check(harmonyStateIsAccelerated(state) == row.expectAccelerated, label + " accelerated");
        check((harmonyAcceleratorFor(snap, guest) == QStringLiteral("kvm")) == row.expectAccelerated,
              label + " accelerator matches acceleration");

        FakeHarmonyEnvironment environment;
        environment.snap = snap;
        HarmonyVirtualizationEngine engine(
            QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));
        QString reason;
        check(engine.supportsGuestArch(guest, &reason) == row.expectSupported, label + " via engine");
        if (row.expectSupported) {
            check(reason.isEmpty(), label + " leaves reason empty when supported");
        } else {
            check(!reason.isEmpty(), label + " explains the rejection");
        }
    }
}

// Repeated start/stop on a healthy injected host: the backend owns no "already
// running" state, it simply delegates each time and reports each real pid. The
// engine must stay usable across cycles (no wedged pending flag).
void testRepeatedStartStopLifecycle() {
    FakeHarmonyEnvironment environment;
    environment.snap = harmonyHost();
    HarmonyVirtualizationEngine engine(
        QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));

    for (int cycle = 1; cycle <= 3; ++cycle) {
        environment.startPid = 5000 + cycle;
        qint64 pid = 0;
        QString error;
        check(engine.start(guestConfig(), &pid, &error), QStringLiteral("cycle %1 start").arg(cycle));
        checkEqual(static_cast<int>(pid), 5000 + cycle, QStringLiteral("cycle %1 pid").arg(cycle));
        check(engine.stop(pid, &error), QStringLiteral("cycle %1 stop").arg(cycle));
        checkEqual(environment.startCalls, cycle, QStringLiteral("cycle %1 start calls").arg(cycle));
        checkEqual(environment.stopCalls, cycle, QStringLiteral("cycle %1 stop calls").arg(cycle));
    }

    // A failing cycle must not poison the next one.
    environment.startResult = false;
    environment.startError = QStringLiteral("temporarily blocked");
    qint64 pid = 0;
    QString error;
    check(!engine.start(guestConfig(), &pid, &error), "failing cycle reports failure");
    environment.startResult = true;
    check(engine.start(guestConfig(), &pid, &error), "engine recovers after a failed cycle");
    check(pid > 0, "recovered cycle reports a pid");
}

// Capability queries are const and can be reached concurrently (the CLI
// enumerates backends while the GUI thread probes). The cached probe is guarded,
// so this must be race-free under TSan and must not over-probe.
void testConcurrentCapabilityQueries() {
    constexpr int kThreads = 4;
    constexpr int kIterations = 25;

    FakeHarmonyEnvironment environment;
    environment.snap = harmonyHost({QStringLiteral("aarch64"), QStringLiteral("x86_64")});
    HarmonyVirtualizationEngine engine(
        QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));

    std::atomic<int> supported{0};
    std::vector<std::thread> workers;
    workers.reserve(kThreads);
    for (int t = 0; t < kThreads; ++t) {
        workers.emplace_back([&]() {
            for (int i = 0; i < kIterations; ++i) {
                if (engine.isHostSupported()) {
                    ++supported;
                }
                (void)engine.tier();
                (void)engine.probe();
                (void)engine.statusCode(guestConfig());
                (void)engine.validate(guestConfig());
                (void)engine.buildArguments(guestConfig());
                QString reason;
                (void)engine.supportsGuestArch(QStringLiteral("aarch64"), &reason);
            }
        });
    }
    for (std::thread& worker : workers) {
        worker.join();
    }

    checkEqual(supported.load(), kThreads * kIterations, "every concurrent probe saw a supported host");
    // The cache is shared, so the environment must not have been probed once per
    // call; a handful of reads is all this allows for (and normally just one).
    check(environment.snapshotCalls >= 1, "host was probed at least once");
    check(environment.snapshotCalls <= kThreads * kIterations,
          "cached probe prevented a per-call host read storm");
}

// The cached snapshot is mutable state shared by every capability query, and
// invalidateProbe() reassigns it. Readers must therefore get a snapshot they can
// hold while another thread drops the cache — which only holds if hostSnapshot()
// hands out a copy. This drives readers and invalidators against each other so
// the tearing race shows up under TSan/ASan instead of in the field.
void testConcurrentProbeInvalidation() {
    constexpr int kReaderThreads = 3;
    constexpr int kInvalidators = 2;
    constexpr int kIterations = 40;

    FakeHarmonyEnvironment environment;
    environment.snap = harmonyHost({QStringLiteral("aarch64"), QStringLiteral("x86_64")});
    HarmonyVirtualizationEngine engine(
        QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));

    std::atomic<int> mismatches{0};
    std::vector<std::thread> threads;
    threads.reserve(kReaderThreads + kInvalidators);

    for (int t = 0; t < kReaderThreads; ++t) {
        threads.emplace_back([&]() {
            for (int i = 0; i < kIterations; ++i) {
                // Each of these binds a snapshot and keeps using it, so a
                // concurrent invalidateProbe() that mutated the cache through a
                // reference would corrupt these reads.
                if (!engine.isHostSupported()) {
                    ++mismatches;
                }
                const EngineProbeResult result = engine.probe();
                if (!result.hostSupported || result.capabilities.nativeExecution) {
                    ++mismatches;
                }
                (void)engine.tier();
                const QVector<EngineIssue> issues = engine.validate(guestConfig());
                if (issues.isEmpty()) {
                    ++mismatches;
                }
            }
        });
    }
    for (int t = 0; t < kInvalidators; ++t) {
        threads.emplace_back([&]() {
            for (int i = 0; i < kIterations * 2; ++i) {
                engine.invalidateProbe();
            }
        });
    }
    for (std::thread& thread : threads) {
        thread.join();
    }

    checkEqual(mismatches.load(), 0, "concurrent invalidation never perturbed a reader's snapshot");

    engine.invalidateProbe();
    (void)engine.probe();
    check(environment.snapshotCalls >= 1, "host was read at least once across the invalidation storm");
    check(engine.isHostSupported(), "engine still reports a supported host after invalidation");
    check(!engine.probe().capabilities.nativeExecution, "nativeExecution stays false after invalidation");
}

// Every non-usable state must tell the caller where to go, because that is the
// only actionable thing the registry can act on.
void testEveryRejectionNamesTheFallback() {
    struct Case {
        HarmonyHostSnapshot snap;
        const char* guestArch;
    };
    const QStringList allArches = harmonyCandidateGuestArches();

    HarmonyHostSnapshot missing = harmonyHost({});
    HarmonyHostSnapshot blocked = blockedHost();
    HarmonyHostSnapshot noHost = nonHarmonyHost();

    const Case cases[] = {
        {missing, "aarch64"},
        {blocked, "aarch64"},
        {noHost, "aarch64"},
        // Unsupported architecture on an otherwise healthy host.
        {harmonyHost(), "ppc64"},
        {harmonyHost(), "i386"},
    };

    for (const Case& item : cases) {
        FakeHarmonyEnvironment environment;
        environment.snap = item.snap;
        HarmonyVirtualizationEngine engine(
            QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));
        QString reason;
        const bool supported = engine.supportsGuestArch(QString::fromLatin1(item.guestArch), &reason);
        check(!supported, QStringLiteral("%1 is not supported here").arg(QString::fromLatin1(item.guestArch)));
        check(!reason.isEmpty(), "rejection has a reason");
        check(reason.contains(QStringLiteral("QEMU")), "rejection names a QEMU fallback");

        const QString status = engine.statusCode(guestConfig(QString::fromLatin1(item.guestArch)));
        check(status != QStringLiteral("available"), "rejection never reports available");
        check(!status.isEmpty(), "rejection has a stable status code");
    }

    // Every state message must be non-empty and, when it is a rejection, name the
    // fallback.
    for (HarmonyHostState state : {HarmonyHostState::NotHarmonyHost,
                                   HarmonyHostState::RuntimeMissing,
                                   HarmonyHostState::RuntimeNotRunnable}) {
        const QString message = harmonyStateMessage(state, QStringLiteral("aarch64"));
        check(!message.isEmpty(), "state message is non-empty");
        check(message.contains(QStringLiteral("QEMU")), "state message names the QEMU fallback");
    }
    for (const QString& arch : allArches) {
        check(!harmonyStateMessage(HarmonyHostState::RuntimeMissing, arch).isEmpty(),
              QStringLiteral("missing-runtime message for %1").arg(arch));
    }
}

// ---------------------------------------------------------------------------
// Deterministic fault-injection / lifecycle chaos loop
// ---------------------------------------------------------------------------
//
// Drives the injected host through every failure and degradation mode the backend
// must survive, interleaving probe / invalidate / start / stop in a fixed-seed
// order, and re-asserts the honesty invariants in each mode:
//
//   * cache storm: every round re-reads the host through invalidateProbe();
//   * runtime found but unrunnable (HarmonyOS NEXT sandbox) is reported, never
//     spawned, and its qemuSource stays "unrunnable";
//   * a KVM permission/device regression degrades to TCG rather than claiming KVM;
//   * same-ISA stays KVM, cross-ISA is always TCG;
//   * a probe timeout is a bounded, diagnosable refusal, not a hang or a win;
//   * start() never returns true without a positive pid, stop() never double-fires;
//   * the tier is never Native and the status code / reason are always non-empty.
void testChaosHarmonyDeterministicLoop() {
    int seed = kHarmonyChaosSeed;
    int usableStarts = 0;
    int refusals = 0;
    int spawnedProcesses = 0;
    int stoppedProcesses = 0;

    for (int round = 0; round < kHarmonyChaosRounds; ++round) {
        const int op = harmonyChaosNext(&seed, 8);
        const QString label = QStringLiteral("harmony chaos round %1 op %2").arg(round).arg(op);

        FakeHarmonyEnvironment environment;
        // Healthy, accelerated baseline; each op mutates one aspect.
        HarmonyHostSnapshot snap = harmonyHost({QStringLiteral("aarch64"), QStringLiteral("x86_64")});
        snap.hostArch = QStringLiteral("aarch64");
        snap.kvmDevicePresent = true;
        snap.kvmDeviceAccessible = true;
        snap.qemuSource = QStringLiteral("runtime/qemu");

        switch (op) {
        case 0:  // accelerated, same-ISA guest
            break;
        case 1:  // no KVM at all -> TCG
            snap.kvmDevicePresent = false;
            snap.kvmDeviceAccessible = false;
            break;
        case 2:  // cross-ISA guest on an accelerated host -> always TCG
            break;
        case 3:  // runtime missing
            snap.runnableGuestArches.clear();
            snap.qemuFound = false;
            snap.qemuExecutable = false;
            snap.qemuSource = QStringLiteral("missing");
            break;
        case 4:  // runtime found but unrunnable (sandbox / device policy)
            snap.runnableGuestArches.clear();
            snap.qemuFound = true;
            snap.qemuExecutable = false;
            snap.qemuSource = QStringLiteral("unrunnable");
            snap.notes << QStringLiteral("QEMU 可执行文件存在但无法运行（设备策略）。");
            break;
        case 5:  // not a HarmonyOS host at all
            snap = nonHarmonyHost();
            break;
        case 6:  // probe timeout: a bounded, diagnosable refusal
            snap.runnableGuestArches.clear();
            snap.qemuFound = true;
            snap.qemuExecutable = false;
            snap.qemuProbeTimedOut = true;
            snap.qemuSource = QStringLiteral("missing");
            snap.notes << QStringLiteral("QEMU --version 探测超时。");
            break;
        case 7:  // KVM present but permission/device gone -> TCG downgrade
            snap.kvmDevicePresent = true;
            snap.kvmDeviceAccessible = false;
            break;
        default:
            break;
        }
        environment.snap = snap;

        const QString guestArch = (op == 2) ? QStringLiteral("x86_64") : QStringLiteral("aarch64");
        HarmonyVirtualizationEngine engine(
            QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));

        const HarmonyHostState state = harmonyHostState(snap, guestArch);

        // Honesty invariants that must hold in every mode, unconditionally.
        check(engine.tier() != VirtualizationTier::Native, label + ": tier never Native");
        const EngineProbeResult probe = engine.probe();
        check(!probe.capabilities.nativeExecution, label + ": nativeExecution never claimed");
        const QString status = engine.statusCode(guestConfig(guestArch));
        check(!status.isEmpty(), label + ": status code non-empty");

        // The accelerator decision must follow the host facts *for this guest
        // architecture*. probe() reports the host-level capability, so the
        // architecture-specific verdict comes from probeFor().
        const EngineProbeResult archProbe = engine.probeFor(guestArch);
        check(!archProbe.capabilities.nativeExecution, label + ": arch probe never claims native");
        if (harmonyStateIsAccelerated(state)) {
            checkEqual(archProbe.capabilities.accelerator, QStringLiteral("kvm"),
                       label + ": accelerated guest requests kvm");
        } else {
            checkEqual(archProbe.capabilities.accelerator, QStringLiteral("tcg"),
                       label + ": non-accelerated guest requests tcg");
        }

        // qemuSource must always be stated wherever QEMU was looked for.
        if (snap.hostIsHarmony) {
            check(!snap.qemuSource.isEmpty(), label + ": qemuSource is stated");
        }

        // Cache storm: a re-read mid-life must not change the verdict.
        engine.invalidateProbe();
        const QString reStatus = engine.statusCode(guestConfig(guestArch));
        checkEqual(reStatus, status, label + ": invalidateProbe does not change the verdict");

        if (harmonyStateIsUsable(state)) {
            qint64 pid = 0;
            QString error;
            check(engine.start(guestConfig(guestArch), &pid, &error), label + ": usable start succeeds");
            check(pid > 0, label + ": usable start yields a positive pid");
            checkEqual(environment.startCalls, 1, label + ": spawned exactly once");
            check(error.isEmpty(), label + ": usable start leaves error empty");
            check(engine.stop(pid, &error), label + ": stop succeeds");
            checkEqual(environment.stopCalls, 1, label + ": stop called exactly once");
            check(status == QStringLiteral("available") || status == QStringLiteral("degraded"),
                  label + ": usable status is available/degraded");
            ++usableStarts;
            spawnedProcesses += environment.startCalls;
            stoppedProcesses += environment.stopCalls;
        } else {
            qint64 pid = 0;
            QString error;
            check(!engine.start(guestConfig(guestArch), &pid, &error), label + ": unusable start refuses");
            checkEqual(environment.startCalls, 0, label + ": unusable start never spawns");
            check(pid == 0, label + ": unusable start leaves pid untouched");
            check(!error.isEmpty(), label + ": unusable start explains itself");
            ++refusals;
        }
    }

    check(usableStarts > 0, "chaos loop exercised at least one usable start");
    check(refusals > 0, "chaos loop exercised at least one refusal");
    checkEqual(spawnedProcesses, stoppedProcesses, "every spawned guest was stopped exactly once");
}

// ---------------------------------------------------------------------------
// Phase-4 regression coverage for the in-process QEMU integration contract
// ---------------------------------------------------------------------------

// Concurrent stop must be race-free and never double-fire or crash. The engine
// owns no "running" state, so every stop simply delegates to the environment; the
// mock's counters are atomic so the assertion is meaningful under TSan.
void testConcurrentStop() {
    constexpr int kThreads = 8;
    FakeHarmonyEnvironment environment;
    environment.snap = harmonyHost();
    HarmonyVirtualizationEngine engine(
        QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));

    qint64 pid = 0;
    QString error;
    check(engine.start(guestConfig(), &pid, &error), "concurrent-stop: start succeeds");
    check(pid > 0, "concurrent-stop: start yields a positive opaque token");

    std::vector<std::thread> workers;
    workers.reserve(kThreads);
    std::atomic<int> okStops{0};
    for (int t = 0; t < kThreads; ++t) {
        workers.emplace_back([&]() {
            QString e;
            // Each thread stops the same session token. The engine must forward
            // every call safely; the core's stop/destroy are idempotent.
            if (engine.stop(pid, &e)) {
                ++okStops;
            }
        });
    }
    for (std::thread& w : workers) {
        w.join();
    }

    checkEqual(okStops.load(), kThreads, "concurrent-stop: every concurrent stop returned true");
    checkEqual(environment.stopCalls, kThreads, "concurrent-stop: environment stop called once per thread");
    checkEqual(static_cast<int>(environment.lastStoppedPid), static_cast<int>(pid),
               "concurrent-stop: opaque token forwarded unchanged (no PID semantics)");
}

// Repeated destroy / stop must be idempotent and safe: invalid tokens are
// rejected, a second stop on an already-stopped session is harmless, and a stop
// after a failed start never crashes or spawns.
void testRepeatedDestroyIsIdempotent() {
    FakeHarmonyEnvironment environment;
    environment.snap = harmonyHost();
    HarmonyVirtualizationEngine engine(
        QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));

    // Invalid / never-started tokens are rejected, never forwarded to a spawner.
    QString error;
    check(!engine.stop(0, &error), "repeated-destroy: pid 0 rejected");
    check(!engine.stop(-1, &error), "repeated-destroy: negative token rejected");
    check(!engine.stop(999999, &error), "repeated-destroy: unknown token rejected");
    checkEqual(environment.stopCalls, 0, "repeated-destroy: invalid tokens never reach the environment");

    // Healthy lifecycle, then repeated stops.
    qint64 pid = 0;
    check(engine.start(guestConfig(), &pid, &error), "repeated-destroy: start succeeds");
    check(engine.stop(pid, &error), "repeated-destroy: first stop succeeds");
    check(engine.stop(pid, &error), "repeated-destroy: second stop (idempotent) succeeds");
    check(engine.stop(pid, &error), "repeated-destroy: third stop (idempotent) succeeds");
    checkEqual(environment.stopCalls, 3, "repeated-destroy: each stop forwarded exactly once");

    // After a FAILED start, stop must still be safe and not spawn.
    environment.startResult = false;
    environment.startError = QStringLiteral("temporarily blocked");
    qint64 badPid = 0;
    check(!engine.start(guestConfig(), &badPid, &error), "repeated-destroy: failed start reported");
    check(!engine.stop(badPid, &error), "repeated-destroy: stop after failed start is safe");
}

// The backend must never spawn a child process. On a host where the linked core
// is not available (the default, fail-closed state), start() must refuse and
// report no PID — and the engine is strictly in-process, so there is no other
// execution context that could have launched a guest.
void testNoChildProcessGuarantee() {
    VirtualizationEnginePtr engine = makeHarmonyVirtualizationEngine();
    check(!engine.isNull(), "no-child-process: engine constructs");
    check(engine->runsInProcess(), "no-child-process: backend is strictly in-process");

    if (realHostIsHarmony()) {
        skip(QStringLiteral("no-child-process: real-host spawn assertion requires a non-Harmony host"));
        return;
    }
    qint64 pid = 0;
    QString error;
    check(!engine->start(guestConfig(), &pid, &error), "no-child-process: start refuses without a linked core");
    check(pid == 0, "no-child-process: start never reports a PID");
    check(!error.isEmpty(), "no-child-process: refusal is explained (no silent success)");
}

// Display/Input/Audio/Block frontend callbacks must NOT be faked. The in-process
// core's callback ABI is not connected in this build, so registration returns
// NotSupported and stores nothing.
void testFrontendCallbacksAreNotFaked() {
    FakeHarmonyEnvironment environment;
    environment.snap = harmonyHost();
    HarmonyVirtualizationEngine engine(
        QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));

    check(engine.frontendStatus() == HarmonyFrontendStatus::NotSupported,
          "frontend: status is NotSupported (ABI not connected)");
    check(engine.setFrontendCallbacks(HarmonyFrontendCallbacks()) == HarmonyFrontendStatus::NotSupported,
          "frontend: registration returns NotSupported (never faked)");
    // A faked success would be the failure mode; assert it is impossible here.
    check(engine.setFrontendCallbacks(HarmonyFrontendCallbacks()) != HarmonyFrontendStatus::NotReady,
          "frontend: registration never reports NotReady while unconnected");
}

// Hardware acceleration is reported ONLY for a same-ISA guest with a usable
// /dev/kvm. Cross-ISA guests (even with KVM present and accessible) run TCG and
// must never be reported as hardware-accelerated.
void testHardwareAccelOnlySameIsa() {
    // 1. Same-ISA + KVM usable -> accelerated.
    {
        HarmonyHostSnapshot snap = harmonyHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"));
        snap.kvmDevicePresent = true;
        snap.kvmDeviceAccessible = true;
        check(harmonyStateIsAccelerated(harmonyHostState(snap, QStringLiteral("aarch64"))),
              "accel: same-ISA + kvm -> accelerated");
        check(harmonyHostHasAcceleration(snap), "accel: same-ISA + kvm -> has acceleration");
        checkEqual(harmonyAcceleratorFor(snap, QStringLiteral("aarch64")), QStringLiteral("kvm"),
                   "accel: requests kvm");
    }
    // 2. Same-ISA, no KVM -> TCG, not accelerated.
    {
        HarmonyHostSnapshot snap = harmonyHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"));
        snap.kvmDevicePresent = false;
        snap.kvmDeviceAccessible = false;
        check(!harmonyStateIsAccelerated(harmonyHostState(snap, QStringLiteral("aarch64"))),
              "accel: same-ISA no kvm -> not accelerated");
        check(!harmonyHostHasAcceleration(snap), "accel: same-ISA no kvm -> no acceleration");
        checkEqual(harmonyAcceleratorFor(snap, QStringLiteral("aarch64")), QStringLiteral("tcg"),
                   "accel: requests tcg");
    }
    // 3. Cross-ISA + KVM present AND accessible -> still TCG, NEVER accelerated.
    {
        HarmonyHostSnapshot snap = harmonyHost({QStringLiteral("aarch64"), QStringLiteral("x86_64")},
                                              QStringLiteral("aarch64"));
        snap.kvmDevicePresent = true;
        snap.kvmDeviceAccessible = true;
        check(!harmonyStateIsAccelerated(harmonyHostState(snap, QStringLiteral("x86_64"))),
              "accel: cross-ISA + kvm -> NOT accelerated");
        check(!harmonyHostHasAcceleration(snap),
              "accel: cross-ISA never reports host acceleration even with kvm");
        checkEqual(harmonyAcceleratorFor(snap, QStringLiteral("x86_64")), QStringLiteral("tcg"),
                   "accel: cross-ISA requests tcg");
    }
    // 4. x86_64 host, riscv64 guest (cross-ISA) with kvm -> TCG.
    {
        HarmonyHostSnapshot snap = harmonyHost({QStringLiteral("riscv64")}, QStringLiteral("x86_64"));
        snap.kvmDevicePresent = true;
        snap.kvmDeviceAccessible = true;
        check(!harmonyStateIsAccelerated(harmonyHostState(snap, QStringLiteral("riscv64"))),
              "accel: x86 host / riscv guest -> NOT accelerated");
        checkEqual(harmonyAcceleratorFor(snap, QStringLiteral("riscv64")), QStringLiteral("tcg"),
                   "accel: x86 host / riscv guest requests tcg");
    }
}

// Init failure (the startProcess / create-init seam) must propagate a clear error
// and must not claim a session started. This is the failure path DeepSeek's
// safety gate hardens: a malformed config must surface as an error, not a crash.
void testInitFailurePropagatesError() {
    FakeHarmonyEnvironment environment;
    environment.snap = harmonyHost();
    environment.startResult = false;
    environment.startError = QStringLiteral("qemu_init refused: malformed machine description");
    HarmonyVirtualizationEngine engine(
        QSharedPointer<HarmonyEnvironment>(&environment, [](HarmonyEnvironment*) {}));

    qint64 pid = 0;
    QString error;
    check(!engine.start(guestConfig(), &pid, &error), "init-failure: start reports failure");
    checkEqual(error, QStringLiteral("qemu_init refused: malformed machine description"),
              "init-failure: error propagated verbatim (no swallowing)");
    check(pid == 0, "init-failure: no opaque token assigned on failure");
    checkEqual(environment.startCalls, 1, "init-failure: the init/create seam was reached exactly once");
}

} // namespace

int main(int argc, char** argv) {
    QCoreApplication application(argc, argv);
    std::fprintf(stdout, "HarmonyVirtualizationEngineTests\n");

    testIdentity();
    testCandidateArchitectures();
    testStateMachine();
    testStateMessageInterpolation();
    testNeverClaimsNativeTier();
    testNonHarmonyHostIsNeverSupported();
    testRuntimeMissing();
    testRuntimeNotRunnable();
    testRuntimeProbeTimeout();
    testTcgDegradedIsUsableButHonest();
    testConfigValidation();
    testArgumentVectorSafety();
    testBuildArgumentsAppliesAccelerator();
    testNoShellStringIsProduced();
    testStartSuccessAndFailurePaths();
    testStopPaths();
    testProbeIsCached();
    testRequirementsAreHonest();
    testProbeForArchitecture();
    testRealHostIsNotMisreported();
    testRealEnvironment();
    testFactoryAndRealEngine();
    testRegistryRegistration();

    // Phase-3 regression coverage.
    testArchMatrixAcrossHosts();
    testRepeatedStartStopLifecycle();
    testConcurrentCapabilityQueries();
    testConcurrentProbeInvalidation();
    testEveryRejectionNamesTheFallback();
    testChaosHarmonyDeterministicLoop();

    // Phase-4 in-process QEMU integration contract.
    testConcurrentStop();
    testRepeatedDestroyIsIdempotent();
    testNoChildProcessGuarantee();
    testFrontendCallbacksAreNotFaked();
    testHardwareAccelOnlySameIsa();
    testInitFailurePropagatesError();

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
