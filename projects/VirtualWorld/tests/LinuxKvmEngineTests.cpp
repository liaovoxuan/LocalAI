// LinuxKvmEngineTests.cpp — logic, probe-injection and failure-path tests for
// the Linux KVM/QEMU compatibility backend.
//
// Design of this suite:
//
//   * Nothing here launches a guest, and nothing needs a Linux host. The whole
//     decision surface is reached by injecting a scripted LinuxEnvironment,
//     which is exactly the seam the engine was built around.
//   * The suite is deliberately free of the heavy QEMU plumbing: it links only
//     Qt Core plus the shared capability/platform/config translation units, so
//     it builds and runs on macOS, Linux and Windows.
//   * The honesty invariants are asserted directly:
//       - this backend must never report VirtualizationTier::Native;
//       - a non-Linux host must never be reported as supported;
//       - cross-architecture guests are REJECTED (supportsGuestArch() false,
//         validate() -> guest-arch-unsupported), not silently degraded to TCG,
//         so the registry can fall through to qemu-tcg;
//       - hardware acceleration requires a confirmed, ABI-compatible KVM API
//         version (12) — not merely a present, readable /dev/kvm;
//       - an unsupported/degraded state must never reach the process spawner;
//       - start() must never return true without a real, positive process id.
//
// Some assertions depend on what is installed on the machine running the suite
// (a QEMU runtime on PATH, a real /dev/kvm). Those are written so that they hold
// in both directions: they assert the honest outcome for the environment they
// actually find, instead of assuming a runtime is present.
//
// Anything that can only be checked on a real host (the real probe against the
// real platform layer) lives in the separate integration check documented in
// docs/linux-virtualization.md, not here.

#include "LinuxKvmEngine.hpp"

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

class FakeLinuxEnvironment : public LinuxEnvironment {
public:
    LinuxHostSnapshot snap;
    QString programValue = QStringLiteral("/opt/qemu/bin/qemu-system-aarch64");
    QStringList argumentsValue = {QStringLiteral("-machine"), QStringLiteral("virt,accel=tcg")};
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

    LinuxHostSnapshot snapshot() const override {
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

LinuxHostSnapshot linuxHost(const QStringList& runnableArches = {QStringLiteral("aarch64")},
                             const QString& hostArch = QStringLiteral("aarch64")) {
    LinuxHostSnapshot snap;
    snap.hostIsLinux = true;
    snap.hostFamily = QStringLiteral("linux");
    snap.hostName = QStringLiteral("Linux");
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

LinuxHostSnapshot kvmHost(const QStringList& runnableArches = {QStringLiteral("aarch64")},
                           const QString& hostArch = QStringLiteral("aarch64"),
                           int apiVersion = 12,
                           int maxVcpus = 0,
                           int maxMemorySlots = 0) {
    LinuxHostSnapshot snap = linuxHost(runnableArches, hostArch);
    snap.kvmDevicePresent = true;
    snap.kvmDeviceAccessible = true;
    snap.kvmApiProbed = true;
    snap.kvmApiVersion = apiVersion;
    snap.kvmMaxVcpus = maxVcpus;
    snap.kvmMaxMemorySlots = maxMemorySlots;
    return snap;
}

LinuxHostSnapshot nonLinuxHost() {
    LinuxHostSnapshot snap;
    snap.hostIsLinux = false;
    snap.hostFamily = QStringLiteral("darwin");
    snap.hostName = QStringLiteral("macOS");
    snap.hostPlatformName = QStringLiteral("macos");
    snap.hostArch = QStringLiteral("aarch64");
    return snap;
}

LinuxHostSnapshot blockedHost() {
    LinuxHostSnapshot snap = linuxHost({});
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
    disk.path = QStringLiteral("/tmp/vw-linux-test-disk.qcow2");
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
    checkEqual(backend_id::linuxKvm(), QStringLiteral("linux-kvm"),
               "shared backend id constant");

    FakeLinuxEnvironment environment;
    environment.snap = linuxHost();
    LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));
    checkEqual(engine.backendId(), backend_id::linuxKvm(), "engine backend id");
    check(!engine.displayName().isEmpty(), "engine display name is set");
    check(engine.displayName().contains(QStringLiteral("Linux")), "display name mentions Linux");
    check(engine.displayName().contains(QStringLiteral("KVM")), "display name mentions KVM");
    check(engine.displayName().contains(QStringLiteral("QEMU")), "display name admits QEMU");
}

void testCandidateArchitectures() {
    checkEqual(linuxCanonicalGuestArch(QStringLiteral("arm64")), QStringLiteral("aarch64"),
               "arm64 canonicalizes to aarch64");
    checkEqual(linuxCanonicalGuestArch(QStringLiteral("aarch64")), QStringLiteral("aarch64"),
               "aarch64 accepted");
    checkEqual(linuxCanonicalGuestArch(QStringLiteral("amd64")), QStringLiteral("x86_64"),
               "amd64 canonicalizes to x86_64");
    checkEqual(linuxCanonicalGuestArch(QStringLiteral("x86_64")), QStringLiteral("x86_64"),
               "x86_64 accepted");
    checkEqual(linuxCanonicalGuestArch(QStringLiteral("riscv64")), QStringLiteral("riscv64"),
               "riscv64 accepted");
    checkEqual(linuxCanonicalGuestArch(QStringLiteral("riscv64gc")), QStringLiteral("riscv64"),
               "riscv64gc canonicalizes to riscv64");

    // Everything else is a QEMU-backend job, never this backend's.
    check(linuxCanonicalGuestArch(QStringLiteral("ppc64")).isEmpty(), "ppc64 rejected");
    check(linuxCanonicalGuestArch(QStringLiteral("i386")).isEmpty(), "i386 rejected");
    check(linuxCanonicalGuestArch(QStringLiteral("m68k")).isEmpty(), "m68k rejected");
    check(linuxCanonicalGuestArch(QString()).isEmpty(), "empty arch rejected");

    const QStringList candidates = linuxCandidateGuestArches();
    checkEqual(candidates.size(), 3, "three candidate architectures");
    check(candidates.contains(QStringLiteral("aarch64")), "aarch64 is a candidate");
    check(candidates.contains(QStringLiteral("x86_64")), "x86_64 is a candidate");
    check(candidates.contains(QStringLiteral("riscv64")), "riscv64 is a candidate");
}

// ---------------------------------------------------------------------------
// Host state machine
// ---------------------------------------------------------------------------

void testStateMachine() {
    check(linuxHostState(nonLinuxHost(), QStringLiteral("aarch64")) == LinuxHostState::NotLinuxHost,
          "non-linux host state");
    check(!linuxStateIsUsable(LinuxHostState::NotLinuxHost), "non-linux is not usable");
    checkEqual(linuxStateCode(LinuxHostState::NotLinuxHost),
               QStringLiteral("host-unsupported"), "non-linux code");

    check(linuxHostState(linuxHost({}), QStringLiteral("aarch64")) == LinuxHostState::RuntimeMissing,
          "missing runtime state");
    checkEqual(linuxStateCode(LinuxHostState::RuntimeMissing),
               QStringLiteral("runtime-missing"), "missing runtime code");

    check(linuxHostState(blockedHost(), QStringLiteral("aarch64")) == LinuxHostState::RuntimeNotRunnable,
          "blocked runtime state");
    checkEqual(linuxStateCode(LinuxHostState::RuntimeNotRunnable),
               QStringLiteral("runtime-not-runnable"), "blocked runtime code");
    check(!linuxStateIsUsable(LinuxHostState::RuntimeNotRunnable), "blocked runtime is not usable");

    LinuxHostSnapshot tcg = linuxHost();
    check(linuxHostState(tcg, QStringLiteral("aarch64")) == LinuxHostState::TcgOnly, "tcg-only state");
    checkEqual(linuxStateCode(LinuxHostState::TcgOnly), QStringLiteral("degraded"), "tcg code is degraded");
    check(linuxStateIsUsable(LinuxHostState::TcgOnly), "tcg is usable");
    check(!linuxStateIsAccelerated(LinuxHostState::TcgOnly), "tcg is not accelerated");

    LinuxHostSnapshot kvm = kvmHost();
    check(linuxHostState(kvm, QStringLiteral("aarch64")) == LinuxHostState::Accelerated,
          "same-isa kvm with api probe is accelerated");
    checkEqual(linuxStateCode(LinuxHostState::Accelerated),
               QStringLiteral("available"), "accelerated code");
    check(linuxHostHasAcceleration(kvm), "host has acceleration");

    // Cross-architecture is never accelerated, even with a working /dev/kvm.
    kvm.runnableGuestArches = {QStringLiteral("aarch64"), QStringLiteral("x86_64")};
    check(linuxHostState(kvm, QStringLiteral("x86_64")) == LinuxHostState::TcgOnly,
          "x86_64 guest on aarch64 host stays tcg");
    checkEqual(linuxAcceleratorFor(kvm, QStringLiteral("x86_64")), QStringLiteral("tcg"),
               "cross-arch accelerator is tcg");
    checkEqual(linuxAcceleratorFor(kvm, QStringLiteral("aarch64")), QStringLiteral("kvm"),
               "same-arch accelerator is kvm");

    // riscv64 has no hardware acceleration on an ARM64 host.
    kvm.runnableGuestArches = {QStringLiteral("aarch64"), QStringLiteral("riscv64")};
    check(linuxHostState(kvm, QStringLiteral("riscv64")) == LinuxHostState::TcgOnly,
          "riscv64 guest stays tcg");
    check(!linuxHostHasAcceleration(linuxHost()), "no kvm means no acceleration");

    // Every state message must be non-empty: it is forwarded verbatim as
    // `QString* reason` by the shared contract.
    const LinuxHostState all[] = {LinuxHostState::NotLinuxHost,
                                  LinuxHostState::RuntimeMissing,
                                  LinuxHostState::RuntimeNotRunnable,
                                  LinuxHostState::TcgOnly,
                                  LinuxHostState::Accelerated};
    for (LinuxHostState state : all) {
        check(!linuxStateMessage(state, QStringLiteral("aarch64")).isEmpty(),
              QStringLiteral("state %1 has a message").arg(static_cast<int>(state)));
    }
}

// The RuntimeMissing message interpolates the architecture through %1 markers.
// Qt's arg() replaces every occurrence of the lowest-numbered marker in one call,
// so exactly one .arg() may be used; a second one (or the two-value form) makes
// Qt print "Argument missing" to stderr at runtime.
// This test pins the correct behaviour and, importantly, the resulting text.
void testStateMessageInterpolation() {
    const QString missing = linuxStateMessage(LinuxHostState::RuntimeMissing,
                                              QStringLiteral("aarch64"));
    check(!missing.contains(QStringLiteral("%1")), "no placeholder survives interpolation");
    check(!missing.contains(QStringLiteral("%2")), "no stray second placeholder");
    check(missing.contains(QStringLiteral("aarch64")),
          "the architecture is interpolated");
    check(missing.contains(QStringLiteral("qemu-system-aarch64")),
          "the message names the expected binary");
    check(missing.contains(QStringLiteral("qemu-tcg")), "the message names the fallback backend");

    // An unknown/unsupported architecture must still produce readable text.
    const QString unknown = linuxStateMessage(LinuxHostState::RuntimeMissing, QStringLiteral("ppc64"));
    check(!unknown.contains(QStringLiteral("%1")), "unknown arch message has no placeholder");
    check(!unknown.isEmpty(), "unknown arch message is non-empty");

    // And the empty case must not produce a broken sentence.
    const QString empty = linuxStateMessage(LinuxHostState::RuntimeMissing, QString());
    check(!empty.contains(QStringLiteral("%1")), "empty arch message has no placeholder");
    check(!empty.isEmpty(), "empty arch message is non-empty");
}

// ---------------------------------------------------------------------------
// The engine never over-claims
// ---------------------------------------------------------------------------

void testNeverClaimsNativeTier() {
    const QVector<LinuxHostSnapshot> hosts = {nonLinuxHost(), linuxHost({}), blockedHost(),
                                               linuxHost(), linuxHost()};
    LinuxHostSnapshot kvm = kvmHost();

    QVector<LinuxHostSnapshot> all = hosts;
    all.push_back(kvm);
    for (const LinuxHostSnapshot& snap : all) {
        FakeLinuxEnvironment environment;
        environment.snap = snap;
        LinuxKvmEngine engine(
            QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));
        check(engine.tier() != VirtualizationTier::Native,
              QStringLiteral("tier is never Native (state for aarch64 = %1)")
                  .arg(linuxStateCode(linuxHostState(snap, QStringLiteral("aarch64")))));
        check(!engine.probe().capabilities.nativeExecution,
              QStringLiteral("nativeExecution is never claimed"));
    }
}

void testNonLinuxHostIsNeverSupported() {
    FakeLinuxEnvironment environment;
    environment.snap = nonLinuxHost();
    LinuxKvmEngine engine(
        QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    QString reason;
    check(!engine.isHostSupported(&reason), "non-linux host unsupported");
    check(!reason.isEmpty(), "non-linux reason is filled");
    check(reason.contains(QStringLiteral("QEMU")), "non-linux reason points at QEMU");

    check(!engine.supportsGuestArch(QStringLiteral("aarch64"), &reason), "non-linux arch unsupported");
    check(!reason.isEmpty(), "non-linux arch reason filled");

    const EngineProbeResult probe = engine.probe();
    check(!probe.hostSupported, "non-linux probe hostSupported false");
    check(!probe.capabilities.canStart, "non-linux canStart false");
    check(!probe.capabilities.canStop, "non-linux canStop false");
    check(!probe.capabilities.guestArchSupported, "non-linux guestArchSupported false");
    check(!probe.capabilities.nativeExecution, "non-linux nativeExecution false");
    check(!probe.summary.isEmpty(), "non-linux summary present");
    checkEqual(engine.statusCode(guestConfig()), QStringLiteral("host-unsupported"),
               "non-linux status code");
    check(engine.tier() == VirtualizationTier::SoftwareEmulation, "non-linux tier is emulation");

    check(hasIssue(engine.validate(guestConfig()), QStringLiteral("host-unsupported")),
          "non-linux validate reports host-unsupported");

    // The critical guarantee: nothing is ever spawned off-platform.
    qint64 pid = 0;
    QString error;
    check(!engine.start(guestConfig(), &pid, &error), "non-linux start fails");
    checkEqual(environment.startCalls, 0, "non-linux start never spawns a process");
    checkEqual(environment.argumentCalls, 0, "non-linux start never builds arguments");
    checkEqual(environment.programCalls, 0, "non-linux start never resolves a program");
    check(pid == 0, "non-linux start leaves pid untouched");
    check(!error.isEmpty(), "non-linux start error is filled");
}

// ---------------------------------------------------------------------------
// Degraded / unsupported paths
// ---------------------------------------------------------------------------

void testRuntimeMissing() {
    FakeLinuxEnvironment environment;
    environment.snap = linuxHost({});
    LinuxKvmEngine engine(
        QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

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
    FakeLinuxEnvironment environment;
    environment.snap = blockedHost();
    LinuxKvmEngine engine(
        QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

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
    check(engine.statusCode(guestConfig()) != QStringLiteral("available"),
          "blocked runtime never reports available");
}

// A wedged QEMU binary must surface as a bounded, diagnosable timeout rather than
// hanging the capability query or being silently treated as "available".
void testRuntimeProbeTimeout() {
    FakeLinuxEnvironment environment;
    environment.snap = linuxHost({});
    environment.snap.qemuFound = true;
    environment.snap.qemuExecutable = false;
    environment.snap.qemuProbeTimedOut = true;
    environment.snap.notes << QStringLiteral("QEMU --version 探测超时。");
    LinuxKvmEngine engine(
        QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

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
    FakeLinuxEnvironment environment;
    environment.snap = linuxHost();
    LinuxKvmEngine engine(
        QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

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

void testKvmAcceleratedIsUsableAndHonest() {
    FakeLinuxEnvironment environment;
    environment.snap = kvmHost();
    LinuxKvmEngine engine(
        QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    check(engine.tier() == VirtualizationTier::HardwareAccelerated,
          "kvm-available tier is hardware-accelerated");
    check(engine.probe().capabilities.hardwareAcceleration, "kvm has hardware acceleration");
    checkEqual(engine.probe().capabilities.accelerator, QStringLiteral("kvm"), "kvm accelerator reported");
    check(engine.isHostSupported(), "kvm host is supported");
    check(engine.supportsGuestArch(QStringLiteral("aarch64")), "kvm supports its arch");
    check(!engine.probe().capabilities.nativeExecution, "kvm never claims nativeExecution");

    const QVector<EngineIssue> issues = engine.validate(guestConfig());
    check(!hasEngineErrors(issues), "kvm validate has no errors");
    check(hasIssueAtLevel(issues, QStringLiteral("hardware-acceleration"), EngineIssueLevel::Info),
          "kvm reports hardware acceleration info");
    checkEqual(engine.statusCode(guestConfig()), QStringLiteral("available"), "kvm status is available");

    const QString diagnosis = engine.diagnose(guestConfig());
    check(diagnosis.contains(QStringLiteral("kvm")), "diagnosis mentions kvm");
    check(diagnosis.contains(QStringLiteral("/dev/kvm")), "diagnosis mentions /dev/kvm");
}

// ---------------------------------------------------------------------------
// Configuration validation
// ---------------------------------------------------------------------------

void testConfigValidation() {
    FakeLinuxEnvironment environment;
    environment.snap = linuxHost();
    LinuxKvmEngine engine(
        QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

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
    check(!linuxArgumentsAreWellFormed({}), "empty argument vector rejected");
    check(linuxArgumentsAreWellFormed({QStringLiteral("-machine"), QStringLiteral("virt,accel=tcg")}),
          "separate flag and value accepted");
    // A dash-prefixed element containing whitespace is the signature of several
    // flags having been concatenated into one string.
    check(!linuxArgumentsAreWellFormed({QStringLiteral("-machine virt,accel=tcg")}),
          "concatenated flags rejected");
    // A path or command line with spaces is fine: it is its own element.
    check(linuxArgumentsAreWellFormed({QStringLiteral("-kernel"),
                                       QStringLiteral("/tmp/My VMs/vmlinuz")}),
          "path containing spaces accepted");
    check(linuxArgumentsAreWellFormed({QStringLiteral("-append"),
                                       QStringLiteral("console=ttyS0 root=/dev/vda")}),
          "kernel command line containing spaces accepted");
    check(!linuxArgumentsAreWellFormed({QStringLiteral("-kernel"),
                                        QStringLiteral("/tmp/x") + QChar(0)}),
          "NUL in argument rejected");
    check(!linuxArgumentsAreWellFormed({QStringLiteral("-kernel"), QStringLiteral("a\nb")}),
          "newline in argument rejected");
    check(!linuxArgumentsAreWellFormed({QString()}), "null argument rejected");

    QString problem;
    check(!linuxArgumentsAreWellFormed({}, &problem), "problem out-param set on failure");
    check(!problem.isEmpty(), "problem message filled");
    problem.clear();
    check(linuxArgumentsAreWellFormed({QStringLiteral("-m"), QStringLiteral("2048")}, &problem),
          "valid vector reports well formed");
    check(problem.isEmpty(), "problem cleared on success");

    check(linuxContainsUnsafeCharacter(QStringLiteral("a") + QChar(0)), "NUL detected");
    check(linuxContainsUnsafeCharacter(QStringLiteral("a\nb")), "newline detected");
    check(linuxContainsUnsafeCharacter(QStringLiteral("a\rb")), "carriage return detected");
    check(!linuxContainsUnsafeCharacter(QStringLiteral("/tmp/normal path/disk.qcow2")),
          "ordinary path is safe");
}

void testBuildArgumentsAppliesAccelerator() {
    FakeLinuxEnvironment environment;
    environment.snap = linuxHost();
    environment.argumentsValue = {QStringLiteral("-machine"), QStringLiteral("virt,accel=tcg"),
                                  QStringLiteral("-smp"), QStringLiteral("4")};
    LinuxKvmEngine engine(
        QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    const QStringList arguments = engine.buildArguments(guestConfig());
    check(!arguments.isEmpty(), "buildArguments returns the environment vector");
    check(linuxArgumentsAreWellFormed(arguments), "environment vector is well formed");
    // Without a usable accelerator the backend must ask for tcg, never kvm.
    checkEqual(environment.lastArgumentConfig.accelerator, QStringLiteral("tcg"),
               "no kvm means the backend requests tcg");
    check(environment.programCalls == 0, "buildArguments does not resolve the program");

    // With a usable same-ISA accelerator it must request kvm instead.
    environment.snap = kvmHost();
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
    FakeLinuxEnvironment environment;
    environment.snap = linuxHost();
    // An injected vector with shell metacharacters must survive as data, not be
    // reinterpreted: it stays one element and is rejected only if it looks like
    // concatenated flags.
    environment.argumentsValue = {QStringLiteral("-append"),
                                  QStringLiteral("root=/dev/vda; rm -rf /")};
    LinuxKvmEngine engine(
        QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));
    const QStringList arguments = engine.buildArguments(guestConfig());
    checkEqual(arguments.size(), 2, "semicolon payload stays inside one element");
    check(arguments.at(1).contains(QStringLiteral(";")), "payload preserved as data");
    check(linuxArgumentsAreWellFormed(arguments), "semicolon payload is still a valid vector");
}

// ---------------------------------------------------------------------------
// Lifecycle failure paths
// ---------------------------------------------------------------------------

void testStartSuccessAndFailurePaths() {
    // --- success -------------------------------------------------------------
    {
        FakeLinuxEnvironment environment;
        environment.snap = linuxHost();
        environment.startPid = 987654;
        LinuxKvmEngine engine(
            QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));
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
        FakeLinuxEnvironment environment;
        environment.snap = linuxHost();
        environment.startResult = false;
        environment.startError = QStringLiteral("spawn denied by device policy");
        LinuxKvmEngine engine(
            QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));
        qint64 pid = 0;
        QString error;
        check(!engine.start(guestConfig(), &pid, &error), "start fails when the environment refuses");
        checkEqual(error, QStringLiteral("spawn denied by device policy"), "spawn error propagated");
        check(pid == 0, "failed start leaves pid untouched");
    }

    // --- spawner gives no pid: never claim started ---------------------------
    {
        FakeLinuxEnvironment environment;
        environment.snap = linuxHost();
        environment.startPid = 0;
        LinuxKvmEngine engine(
            QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));
        qint64 pid = 0;
        QString error;
        check(!engine.start(guestConfig(), &pid, &error), "start refuses a zero pid");
        check(!error.isEmpty(), "zero pid error explained");
        check(error.contains(QStringLiteral("进程 ID")), "zero pid error mentions the process id");
        check(environment.startCalls == 1, "spawn was attempted once");
    }

    // --- malformed argument vector: refuse before spawning -------------------
    {
        FakeLinuxEnvironment environment;
        environment.snap = linuxHost();
        environment.argumentsValue = {QStringLiteral("-machine virt,accel=tcg")};
        LinuxKvmEngine engine(
            QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));
        qint64 pid = 0;
        QString error;
        check(!engine.start(guestConfig(), &pid, &error), "start refuses a malformed vector");
        checkEqual(environment.startCalls, 0, "malformed vector never reaches the spawner");
        check(!error.isEmpty(), "malformed vector error filled");
    }

    // --- no resolvable program ----------------------------------------------
    {
        FakeLinuxEnvironment environment;
        environment.snap = linuxHost();
        environment.programValue.clear();
        LinuxKvmEngine engine(
            QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));
        qint64 pid = 0;
        QString error;
        check(!engine.start(guestConfig(), &pid, &error), "start refuses without a program");
        checkEqual(environment.startCalls, 0, "missing program never reaches the spawner");
        check(!error.isEmpty(), "missing program error filled");
    }
}

void testStopPaths() {
    FakeLinuxEnvironment environment;
    environment.snap = linuxHost();
    LinuxKvmEngine engine(
        QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    QString error;
    check(!engine.stop(0, &error), "stop rejects pid 0");
    check(!error.isEmpty(), "pid 0 error filled");
    error.clear();
    check(!engine.stop(-5, &error), "stop rejects a negative pid");
    checkEqual(environment.stopCalls, 0, "invalid pids never reach the environment");

    check(engine.stop(4321, &error), "stop delegates a valid pid");
    checkEqual(environment.stopCalls, 1, "stop called once");
    checkEqual(static_cast<int>(environment.lastStoppedPid), 4321, "stop forwards the pid");

    environment.stopResult = false;
    environment.stopError = QStringLiteral("kill failed");
    check(!engine.stop(4321, &error), "stop propagates environment failure");
    checkEqual(error, QStringLiteral("kill failed"), "stop error propagated");

    // An engine explicitly built without an environment must fail cleanly rather
    // than dereference null. The null is honoured literally (it does not install
    // the real environment), so this cannot accidentally spawn a process.
    LinuxKvmEngine noEnvironment{QSharedPointer<LinuxEnvironment>()};
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
    FakeLinuxEnvironment environment;
    environment.snap = linuxHost();
    LinuxKvmEngine engine(
        QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    check(engine.isHostSupported(), "cached probe: supported");
    engine.probe();
    engine.tier();
    engine.supportsGuestArch(QStringLiteral("aarch64"));
    checkEqual(environment.snapshotCalls, 1, "host is read exactly once");

    engine.invalidateProbe();
    engine.isHostSupported();
    checkEqual(environment.snapshotCalls, 2, "invalidateProbe forces a re-read");
}

// The probe cache must be a pure optimisation: once invalidated, a changed host
// must change every downstream answer (accelerator, state, status code, tier).
// This is what makes "/dev/kvm was just made accessible" work without restarting
// the application.
void testEnvironmentSnapshotChange() {
    FakeLinuxEnvironment environment;
    environment.snap = linuxHost({QStringLiteral("aarch64")});
    LinuxKvmEngine engine(
        QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    // Start with QEMU but no usable KVM: software emulation.
    check(engine.stateFor(QStringLiteral("aarch64")) == LinuxHostState::TcgOnly,
          "starts in TcgOnly");
    engine.buildArguments(guestConfig());  // records the launched config
    checkEqual(environment.lastArgumentConfig.accelerator, QStringLiteral("tcg"),
               "the argument vector is built for tcg");
    check(engine.tier() == VirtualizationTier::SoftwareEmulation, "starts at the emulation tier");
    checkEqual(engine.statusCode(guestConfig()), QStringLiteral("degraded"), "starts degraded");

    // /dev/kvm becomes available with a valid KVM API.
    environment.snap = kvmHost({QStringLiteral("aarch64")});

    // Without invalidation the cached snapshot keeps answering the old question.
    check(engine.stateFor(QStringLiteral("aarch64")) == LinuxHostState::TcgOnly,
          "the cache keeps the pre-change answer");
    checkEqual(environment.snapshotCalls, 1, "no extra snapshot read happened");

    // After invalidation everything flips to hardware acceleration.
    engine.invalidateProbe();
    check(engine.stateFor(QStringLiteral("aarch64")) == LinuxHostState::Accelerated,
          "state becomes Accelerated after invalidateProbe()");
    checkEqual(engine.statusCode(guestConfig()), QStringLiteral("available"),
               "status becomes available after invalidateProbe()");
    check(engine.tier() == VirtualizationTier::HardwareAccelerated,
          "tier becomes HardwareAccelerated after invalidateProbe()");
    check(engine.hostSnapshot().kvmApiVersion == 12, "the new KVM API version is visible");
    checkEqual(environment.snapshotCalls, 2, "exactly one re-read happened");

    // And the accelerator decision reaches the spawner.
    qint64 pid = 0;
    QString error;
    check(engine.start(guestConfig(), &pid, &error), "start succeeds after the change");
    checkEqual(environment.lastStartConfig.accelerator, QStringLiteral("kvm"),
               "start now requests kvm");
}

// A host that loses its runtime must stop claiming support, then claim it again
// once the runtime returns — all through the same engine instance.
void testRuntimeDisappearsAndReturns() {
    FakeLinuxEnvironment environment;
    environment.snap = kvmHost({QStringLiteral("aarch64")});
    LinuxKvmEngine engine(
        QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    check(engine.isHostSupported(), "supported with a runtime");
    check(engine.supportsGuestArch(QStringLiteral("aarch64")), "guest arch supported with a runtime");

    // The runtime disappears (uninstalled / moved).
    environment.snap = linuxHost({});
    environment.snap.kvmDevicePresent = true;
    environment.snap.kvmDeviceAccessible = true;
    environment.snap.kvmApiProbed = true;
    environment.snap.kvmApiVersion = 12;
    engine.invalidateProbe();
    check(!engine.isHostSupported(), "unsupported once the runtime is gone");
    check(!engine.supportsGuestArch(QStringLiteral("aarch64")),
          "guest arch unsupported once the runtime is gone");
    checkEqual(engine.statusCode(guestConfig()), QStringLiteral("runtime-missing"),
               "status is runtime-missing once the runtime is gone");
    qint64 pid = 0;
    QString error;
    check(!engine.start(guestConfig(), &pid, &error), "start fails once the runtime is gone");
    checkEqual(environment.startCalls, 0, "no spawn attempt without a runtime");

    // The runtime comes back.
    environment.snap = kvmHost({QStringLiteral("aarch64")});
    engine.invalidateProbe();
    check(engine.isHostSupported(), "supported again once the runtime returns");
    check(engine.supportsGuestArch(QStringLiteral("aarch64")), "guest arch supported again");
    check(engine.start(guestConfig(), &pid, &error), "start succeeds again once the runtime returns");
    checkEqual(static_cast<int>(pid), 4242, "the recovered start reports a pid");
    checkEqual(environment.lastStartConfig.accelerator, QStringLiteral("kvm"),
               "the recovered start still requests kvm");
}

// The same engine instance must survive a start/stop cycle repeated many times.
void testRepeatedStartStop() {
    FakeLinuxEnvironment environment;
    environment.snap = kvmHost({QStringLiteral("aarch64")});
    LinuxKvmEngine engine(
        QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    for (int round = 0; round < 3; ++round) {
        qint64 pid = 0;
        QString error;
        check(engine.start(guestConfig(), &pid, &error),
              QStringLiteral("start succeeds on round %1").arg(round));
        checkEqual(static_cast<int>(pid), 4242, QStringLiteral("round %1 reports the pid").arg(round));
        check(engine.stop(pid, &error), QStringLiteral("stop succeeds on round %1").arg(round));
        checkEqual(static_cast<int>(environment.lastStoppedPid), 4242,
                   QStringLiteral("round %1 stops the pid it started").arg(round));
    }
    checkEqual(environment.startCalls, 3, "three starts reached the environment");
    checkEqual(environment.stopCalls, 3, "three stops reached the environment");
}

void testRequirementsAreHonest() {
    FakeLinuxEnvironment environment;
    environment.snap = linuxHost();
    LinuxKvmEngine engine(
        QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));
    const QStringList requirements = engine.requirements();
    check(!requirements.isEmpty(), "requirements are not empty");
    const QString joined = requirements.join(QStringLiteral(" | "));
    check(joined.contains(QStringLiteral("/dev/kvm")), "requirements mention /dev/kvm");
    check(joined.contains(QStringLiteral("QEMU")), "requirements mention QEMU");
    check(joined.contains(QStringLiteral("Linux")), "requirements name the host family");
}

void testProbeForArchitecture() {
    FakeLinuxEnvironment environment;
    environment.snap = linuxHost({QStringLiteral("aarch64"), QStringLiteral("x86_64")});
    LinuxKvmEngine engine(
        QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    const EngineProbeResult arm = engine.probeFor(QStringLiteral("arm64"));
    check(arm.capabilities.guestArchSupported, "probeFor accepts a runnable arch");
    check(arm.capabilities.canStart, "probeFor marks canStart for a runnable arch");

    const EngineProbeResult ppc = engine.probeFor(QStringLiteral("ppc64"));
    check(!ppc.capabilities.guestArchSupported, "probeFor rejects an unsupported arch");
    check(!ppc.capabilities.canStart, "probeFor clears canStart for an unsupported arch");
    check(!ppc.summary.isEmpty(), "probeFor explains the rejection");

    environment.snap = kvmHost({QStringLiteral("aarch64")});
    engine.invalidateProbe();
    const EngineProbeResult x86 = engine.probeFor(QStringLiteral("x86_64"));
    // A cross-ISA guest is refused outright — not reported as "supported but
    // unaccelerated", which would let auto-selection pick this backend for a
    // guest it can never accelerate.
    check(!x86.capabilities.guestArchSupported, "cross-ISA guest is refused by probeFor");
    check(!x86.capabilities.canStart, "cross-ISA guest cannot be started");
    check(x86.capabilities.accelerator.isEmpty(), "cross-ISA guest reports no accelerator");
    check(!x86.capabilities.hardwareAcceleration, "cross-ISA guest is never hardware accelerated");
    check(!x86.capabilities.nativeExecution, "cross-ISA guest is never native");
    check(hasIssue(x86.diagnostics, QStringLiteral("guest-arch-unsupported")),
          "cross-ISA probeFor reports guest-arch-unsupported");
    check(x86.summary.contains(QStringLiteral("qemu-tcg")),
          "cross-ISA probeFor points at qemu-tcg");
    check(engine.statusCode(guestConfig(QStringLiteral("x86_64"))) == QStringLiteral("guest-arch-unsupported"),
          "cross-ISA status code is guest-arch-unsupported");

    // Same-ISA on a KVM-capable host must still come back accelerated, so the
    // refusal above is specific to cross-ISA and not a blanket downgrade.
    const EngineProbeResult same = engine.probeFor(QStringLiteral("aarch64"));
    check(same.capabilities.guestArchSupported, "same-ISA guest remains supported");
    checkEqual(same.capabilities.accelerator, QStringLiteral("kvm"),
               "same-ISA guest still requests kvm");
    check(same.capabilities.hardwareAcceleration, "same-ISA guest is still hardware accelerated");
}

// The engine-level refusal must not depend on the guest being merely "not in the
// runnable list": even a host that has a runnable cross-ISA emulator refuses it,
// because KVM is a same-ISA API and TCG is qemu-tcg's job.
void testCrossIsaIsRefusedEvenWithEmulator() {
    FakeLinuxEnvironment environment;
    environment.snap = kvmHost({QStringLiteral("aarch64"), QStringLiteral("x86_64")});
    LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    QString reason;
    check(!engine.supportsGuestArch(QStringLiteral("x86_64"), &reason),
          "cross-ISA guest refused even though an x86_64 emulator is runnable");
    check(reason.contains(QStringLiteral("qemu-tcg")), "cross-ISA refusal points at qemu-tcg");

    reason.clear();
    check(!engine.supportsGuestArch(QStringLiteral("riscv64"), &reason),
          "riscv64 guest on an aarch64 host is refused");
    check(reason.contains(QStringLiteral("qemu-tcg")), "riscv64 refusal points at qemu-tcg");

    check(engine.supportsGuestArch(QStringLiteral("aarch64")), "same-ISA aarch64 is supported");

    // The policy layer still states the only accelerator it could ever request
    // for a cross-ISA guest — and it is never "kvm".
    checkEqual(linuxAcceleratorFor(environment.snap, QStringLiteral("x86_64")), QStringLiteral("tcg"),
               "cross-ISA accelerator token is tcg, never kvm");

    // validate() refuses the cross-ISA config before anything is spawned.
    const QVector<EngineIssue> issues = engine.validate(guestConfig(QStringLiteral("x86_64")));
    check(hasIssueAtLevel(issues, QStringLiteral("guest-arch-unsupported"), EngineIssueLevel::Error),
          "cross-ISA config is rejected");
    qint64 pid = 0;
    QString error;
    check(!engine.start(guestConfig(QStringLiteral("x86_64")), &pid, &error), "cross-ISA start is refused");
    checkEqual(environment.startCalls, 0, "cross-ISA start never reaches the spawner");
    check(pid == 0, "cross-ISA start never reports a pid");
}

void testRealHostIsNotMisreported() {
    // This runs on whatever machine executes the suite. The backend is scoped to
    // Linux, so on macOS/Windows it must report host-unsupported. The only
    // exception is a genuine Linux host, which is reported as a skip instead of
    // a guess.
    const HostProfile host = detectHostProfile();
    const bool linuxFamily = host.family.compare(QStringLiteral("linux"), Qt::CaseInsensitive) == 0;
    if (linuxFamily) {
        skip(QStringLiteral("real-host check: this machine really is %1").arg(host.family));
        return;
    }

    // A snapshot built the same way the real probe builds it off-platform.
    FakeLinuxEnvironment environment;
    environment.snap = nonLinuxHost();
    environment.snap.hostFamily = host.family;
    environment.snap.hostArch = canonicalHostArch(host.arch);
    LinuxKvmEngine engine(
        QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    QString reason;
    check(!engine.isHostSupported(&reason),
          QStringLiteral("real host (%1 / %2) is not misreported as Linux-supported")
              .arg(host.family, host.arch));
    checkEqual(engine.statusCode(guestConfig(host.arch)), QStringLiteral("host-unsupported"),
               "real host status code is host-unsupported");
    check(engine.tier() != VirtualizationTier::Native, "real host tier is never Native");
}

// ---------------------------------------------------------------------------
// Real integration: the actual environment, factory and registry
// ---------------------------------------------------------------------------

bool realHostIsLinux() {
    const HostProfile host = detectHostProfile();
    return host.family.compare(QStringLiteral("linux"), Qt::CaseInsensitive) == 0;
}

void testRealEnvironment() {
    QSharedPointer<LinuxEnvironment> environment = makeLinuxEnvironment();
    check(!environment.isNull(), "makeLinuxEnvironment returns a real environment");
    if (environment.isNull()) {
        return;
    }

    const LinuxHostSnapshot snap = environment->snapshot();
    check(!snap.hostFamily.isEmpty(), "real snapshot has a host family");
    check(!snap.hostArch.isEmpty(), "real snapshot has a host arch");
    check(snap.hostIsLinux == realHostIsLinux(),
          "real snapshot agrees with the shared host probe about the Linux family");
    if (!snap.hostIsLinux) {
        // The important off-platform guarantee: no QEMU probing happened at all,
        // so a macOS/Windows capability query stays cheap and side-effect free.
        check(!snap.qemuProbePerformed, "off-platform snapshot performed no QEMU probe");
        check(snap.runnableGuestArches.isEmpty(), "off-platform snapshot has no runnable arches");
        check(!snap.detail.isEmpty(), "off-platform snapshot explains itself");
    }
}

void testFactoryAndRealEngine() {
    if (!realHostIsLinux()) {
        check(!linuxKvmEngineAvailable(),
              "factory reports unavailable on a non-Linux host");
    }

    VirtualizationEnginePtr engine = makeLinuxKvmEngine();
    check(!engine.isNull(), "factory returns an engine");
    if (engine.isNull()) {
        return;
    }
    checkEqual(engine->backendId(), backend_id::linuxKvm(), "factory engine id");
    check(engine->tier() != VirtualizationTier::Native, "factory engine is never native tier");
    check(!engine->requirements().isEmpty(), "factory engine lists requirements");

    if (realHostIsLinux()) {
        skip(QStringLiteral("factory/real-engine off-platform assertions: this host really is Linux"));
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

    // Self-registration from LinuxKvmEngine.cpp happens before main()
    // and must not depend on registerBuiltinBackends().
    check(registry.hasBackend(backend_id::linuxKvm()),
          "linux-kvm backend self-registered into the registry");
    VirtualizationEnginePtr registered = registry.backend(backend_id::linuxKvm());
    check(!registered.isNull(), "registry returns the linux-kvm backend");
    if (registered) {
        check(registered->tier() != VirtualizationTier::Native, "registered backend tier is honest");
    }

    const QStringList ids = registry.backendIds();
    check(ids.contains(backend_id::linuxKvm()), "backend id list contains linux-kvm");
    check(ids.contains(backend_id::qemuTcg()), "qemu-tcg remains available as the fallback");

    // The point of the honest host-unsupported state: off-platform,
    // auto-selection must never land on this backend, and the audit trail must
    // record why it was skipped.
    //
    // Whether selection *succeeds* depends on whether any backend can really run
    // the config on this machine — on macOS that means a QEMU runtime on PATH,
    // which the suite does not require. Both outcomes are asserted honestly.
    if (realHostIsLinux()) {
        skip(QStringLiteral("registry selection assertions: this host really is Linux"));
        return;
    }
    const EngineSelection selection = registry.select(guestConfig());
    check(selection.backendId != backend_id::linuxKvm(),
          "auto-selection never chooses the linux-kvm backend off-platform");

    bool sawLinuxKvm = false;
    bool sawUsableAlternative = false;
    for (const BackendChoice& choice : selection.considered) {
        if (choice.backendId == backend_id::linuxKvm()) {
            sawLinuxKvm = true;
            check(!choice.hostSupported, "linux-kvm backend recorded as host-unsupported");
            check(!choice.guestArchSupported, "linux-kvm backend recorded as guest-arch-unsupported");
            check(!choice.selected, "linux-kvm backend recorded as not selected");
            check(!choice.reason.isEmpty(), "linux-kvm rejection reason recorded");
        } else if (choice.hostSupported && choice.guestArchSupported && choice.configAccepted) {
            sawUsableAlternative = true;
        }
    }
    check(sawLinuxKvm, "linux-kvm backend appears in the selection audit trail");

    // Consistency: a failed selection must mean *nothing* could run the config,
    // never that this backend was skipped while a usable one was available.
    if (!selection.ok) {
        check(!sawUsableAlternative,
              "a failed auto-selection means no other backend could run the config");
    }
    // And the ordering contract holds whatever the outcome.
    const QVector<VirtualizationEnginePtr> ordered = registry.backendsInPreferenceOrder();
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
}

// ---------------------------------------------------------------------------
// KVM API probe determines Accelerated state
// ---------------------------------------------------------------------------

void testKvmApiProbeDeterminesAccelerated() {
    // Without KVM API probe (kvmApiProbed = false), even with /dev/kvm present
    // and accessible, the state must be TcgOnly — not Accelerated.
    LinuxHostSnapshot noProbe = linuxHost();
    noProbe.kvmDevicePresent = true;
    noProbe.kvmDeviceAccessible = true;
    // kvmApiProbed stays false, kvmApiVersion stays 0
    check(linuxHostState(noProbe, QStringLiteral("aarch64")) == LinuxHostState::TcgOnly,
          "kvm without api probe is tcg-only");
    check(!linuxHostHasAcceleration(noProbe), "no api probe means no acceleration claim");

    // With KVM API probe but version 0 (ioctl failed), still TcgOnly.
    LinuxHostSnapshot probeFailed = linuxHost();
    probeFailed.kvmDevicePresent = true;
    probeFailed.kvmDeviceAccessible = true;
    probeFailed.kvmApiProbed = true;
    probeFailed.kvmApiVersion = 0;
    check(linuxHostState(probeFailed, QStringLiteral("aarch64")) == LinuxHostState::TcgOnly,
          "kvm with api version 0 is tcg-only");
    check(!linuxHostHasAcceleration(probeFailed), "api version 0 means no acceleration claim");

    // With KVM API probe and version > 0, Accelerated.
    LinuxHostSnapshot probeOk = kvmHost();
    check(linuxHostState(probeOk, QStringLiteral("aarch64")) == LinuxHostState::Accelerated,
          "kvm with api version > 0 is accelerated");
    check(linuxHostHasAcceleration(probeOk), "api version > 0 enables acceleration claim");

    // Negative api version must not be treated as success.
    LinuxHostSnapshot negVersion = linuxHost();
    negVersion.kvmDevicePresent = true;
    negVersion.kvmDeviceAccessible = true;
    negVersion.kvmApiProbed = true;
    negVersion.kvmApiVersion = -1;
    check(linuxHostState(negVersion, QStringLiteral("aarch64")) == LinuxHostState::TcgOnly,
          "negative api version is tcg-only");
    check(!linuxHostHasAcceleration(negVersion), "negative api version means no acceleration");
}

// ---------------------------------------------------------------------------
// KVM errno diagnostics
// ---------------------------------------------------------------------------

void testKvmErrnoDiagnostics() {
    // No errno → no diagnostic.
    LinuxHostSnapshot noErrno = kvmHost();
    check(!linuxKvmHasErrno(noErrno), "no errno means no kvm errno diagnostic");
    check(linuxKvmErrnoMessage(0).isEmpty(), "errno 0 produces empty message");

    // errno present but kvmDevicePresent = false → not surfaced (device wasn't expected).
    LinuxHostSnapshot errnoNoDevice = linuxHost();
    errnoNoDevice.kvmOpenErrno = 13;
    check(!linuxKvmHasErrno(errnoNoDevice), "errno without kvm device is not surfaced");

    // errno present and kvmDevicePresent = true → surfaced.
    LinuxHostSnapshot errnoWithDevice = kvmHost();
    errnoWithDevice.kvmOpenErrno = 13;
    check(linuxKvmHasErrno(errnoWithDevice), "errno with kvm device is surfaced");

    // Specific errno messages.
    check(linuxKvmErrnoMessage(2).contains(QStringLiteral("ENOENT")),
          "errno 2 mentions ENOENT");
    check(linuxKvmErrnoMessage(13).contains(QStringLiteral("EACCES")),
          "errno 13 mentions EACCES");
    check(linuxKvmErrnoMessage(16).contains(QStringLiteral("EBUSY")),
          "errno 16 mentions EBUSY");
    check(linuxKvmErrnoMessage(22).contains(QStringLiteral("EINVAL")),
          "errno 22 mentions EINVAL");
    check(linuxKvmErrnoMessage(25).contains(QStringLiteral("ENOTTY")),
          "errno 25 mentions ENOTTY");
    check(linuxKvmErrnoMessage(28).contains(QStringLiteral("ENOSPC")),
          "errno 28 mentions ENOSPC");
    check(linuxKvmErrnoMessage(999).contains(QStringLiteral("999")),
          "unknown errno includes the number");

    // errno propagates to validate() as a warning.
    FakeLinuxEnvironment environment;
    environment.snap = kvmHost();
    environment.snap.kvmOpenErrno = 13;
    LinuxKvmEngine engine(
        QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));
    const QVector<EngineIssue> issues = engine.validate(guestConfig());
    check(hasIssueAtLevel(issues, QStringLiteral("kvm-api-error"), EngineIssueLevel::Warning),
          "kvm errno appears as warning in validate");
    check(!hasEngineErrors(issues), "kvm errno alone is not a hard error");

    // errno propagates to probe() diagnostics.
    const EngineProbeResult probe = engine.probe();
    check(hasIssue(probe.diagnostics, QStringLiteral("kvm-api-error")),
          "kvm errno appears in probe diagnostics");
}

// ---------------------------------------------------------------------------
// vCPU exceeds KVM max
// ---------------------------------------------------------------------------

void testVcpuExceedsKvmMax() {
    // When KVM max vCPUs is unknown (0), no warning.
    LinuxHostSnapshot unknown = kvmHost();
    check(!linuxVcpuExceedsKvmMax(guestConfig(), unknown),
          "unknown max vcpus produces no warning");

    // When KVM max vCPUs is known and config is within limit, no warning.
    LinuxHostSnapshot within = kvmHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"), 12, 8, 32);
    VmConfig config4 = guestConfig();
    config4.cpuCores = 4;
    check(!linuxVcpuExceedsKvmMax(config4, within), "4 vcpus within limit of 8");

    // When config exceeds KVM max vCPUs, warning.
    VmConfig config16 = guestConfig();
    config16.cpuCores = 16;
    check(linuxVcpuExceedsKvmMax(config16, within), "16 vcpus exceeds limit of 8");

    // Exact match is not a warning.
    VmConfig config8 = guestConfig();
    config8.cpuCores = 8;
    check(!linuxVcpuExceedsKvmMax(config8, within), "8 vcpus equals limit of 8, not exceeded");

    // Warning only applies in Accelerated state (not TcgOnly).
    LinuxHostSnapshot tcg = linuxHost();
    tcg.kvmMaxVcpus = 2;
    VmConfig config4tcg = guestConfig();
    config4tcg.cpuCores = 4;
    check(!linuxVcpuExceedsKvmMax(config4tcg, tcg),
          "vcpu check does not apply in tcg-only state");

    // Warning propagates to validate().
    FakeLinuxEnvironment environment;
    environment.snap = kvmHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"), 12, 2, 32);
    LinuxKvmEngine engine(
        QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));
    VmConfig overVcpu = guestConfig();
    overVcpu.cpuCores = 8;
    const QVector<EngineIssue> issues = engine.validate(overVcpu);
    check(hasIssueAtLevel(issues, QStringLiteral("kvm-vcpu-exceeds-max"), EngineIssueLevel::Warning),
          "vcpu exceeds max appears as warning in validate");
    check(!hasEngineErrors(issues), "vcpu exceeds max is not a hard error");
}

// ---------------------------------------------------------------------------
// riscv64 KVM maturity
// ---------------------------------------------------------------------------

void testRiscv64KvmMaturity() {
    // x86_64 and aarch64 are mature.
    check(linuxKvmIsMatureForArch(QStringLiteral("x86_64")), "x86_64 is mature for kvm");
    check(linuxKvmIsMatureForArch(QStringLiteral("aarch64")), "aarch64 is mature for kvm");

    // riscv64 is not mature.
    check(!linuxKvmIsMatureForArch(QStringLiteral("riscv64")), "riscv64 is not mature for kvm");

    // Unknown arches are not mature.
    check(!linuxKvmIsMatureForArch(QStringLiteral("ppc64")), "ppc64 is not mature for kvm");
    check(!linuxKvmIsMatureForArch(QString()), "empty arch is not mature for kvm");

    // riscv64 on a riscv64 host with KVM: Accelerated state + maturity warning.
    LinuxHostSnapshot riscv = kvmHost({QStringLiteral("riscv64")}, QStringLiteral("riscv64"));
    check(linuxHostState(riscv, QStringLiteral("riscv64")) == LinuxHostState::Accelerated,
          "riscv64 same-isa kvm is accelerated");
    check(linuxHostHasAcceleration(riscv), "riscv64 host has acceleration");

    // Maturity warning in validate().
    FakeLinuxEnvironment environment;
    environment.snap = kvmHost({QStringLiteral("riscv64")}, QStringLiteral("riscv64"));
    LinuxKvmEngine engine(
        QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));
    VmConfig riscvConfig = guestConfig(QStringLiteral("riscv64"));
    const QVector<EngineIssue> issues = engine.validate(riscvConfig);
    check(hasIssueAtLevel(issues, QStringLiteral("kvm-maturity-experimental"), EngineIssueLevel::Warning),
          "riscv64 kvm maturity warning in validate");
    check(!hasEngineErrors(issues), "riscv64 maturity is a warning, not error");

    // Maturity warning in probe() diagnostics.
    const EngineProbeResult probe = engine.probe();
    check(hasIssue(probe.diagnostics, QStringLiteral("kvm-maturity-experimental")),
          "riscv64 kvm maturity warning in probe diagnostics");

    // aarch64 host with KVM does NOT get maturity warning.
    FakeLinuxEnvironment armEnv;
    armEnv.snap = kvmHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"));
    LinuxKvmEngine armEngine(
        QSharedPointer<LinuxEnvironment>(&armEnv, [](LinuxEnvironment*) {}));
    const QVector<EngineIssue> armIssues = armEngine.validate(guestConfig());
    check(!hasIssue(armIssues, QStringLiteral("kvm-maturity-experimental")),
          "aarch64 kvm has no maturity warning");
}

// ---------------------------------------------------------------------------
// KVM API version in probe output
// ---------------------------------------------------------------------------

void testKvmApiVersionInProbe() {
    // When KVM API is probed successfully, probe() includes version info.
    FakeLinuxEnvironment environment;
    environment.snap = kvmHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"), 12, 128, 509);
    LinuxKvmEngine engine(
        QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));
    const EngineProbeResult probe = engine.probe();
    check(hasIssue(probe.diagnostics, QStringLiteral("kvm-api-version")),
          "probe includes kvm api version info");

    // When KVM API is not probed, no version info in probe.
    FakeLinuxEnvironment noProbeEnv;
    noProbeEnv.snap = linuxHost();
    noProbeEnv.snap.kvmDevicePresent = true;
    noProbeEnv.snap.kvmDeviceAccessible = true;
    LinuxKvmEngine noProbeEngine(
        QSharedPointer<LinuxEnvironment>(&noProbeEnv, [](LinuxEnvironment*) {}));
    const EngineProbeResult noProbeResult = noProbeEngine.probe();
    check(!hasIssue(noProbeResult.diagnostics, QStringLiteral("kvm-api-version")),
          "no kvm api probe means no version info in probe");

    // diagnose() includes KVM API info.
    const QString diagnosis = engine.diagnose(guestConfig());
    check(diagnosis.contains(QStringLiteral("KVM API")), "diagnose mentions KVM API");
    check(diagnosis.contains(QStringLiteral("版本 12")), "diagnose mentions API version 12");
    check(diagnosis.contains(QStringLiteral("128")), "diagnose mentions max vcpus 128");
    check(diagnosis.contains(QStringLiteral("509")), "diagnose mentions max memory slots 509");
}

// ---------------------------------------------------------------------------
// Fault injection: probe cache invalidation under environment mutation
// ---------------------------------------------------------------------------

void testProbeCacheInvalidationUnderMutation() {
    FakeLinuxEnvironment environment;
    environment.snap = linuxHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"));
    LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    const int kRounds = 10;
    for (int round = 0; round < kRounds; ++round) {
        const bool kvmAvailable = (round % 2 == 0);
        if (kvmAvailable) {
            environment.snap = kvmHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"));
        } else {
            environment.snap = linuxHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"));
        }
        engine.invalidateProbe();

        const QString accelerator = linuxAcceleratorFor(engine.hostSnapshot(), QStringLiteral("aarch64"));
        const VirtualizationTier tier = engine.executionTierFor(QStringLiteral("aarch64"));

        if (kvmAvailable) {
            checkEqual(accelerator, QStringLiteral("kvm"),
                       QStringLiteral("round %1: accelerator is kvm after invalidation").arg(round));
            check(tier == VirtualizationTier::HardwareAccelerated,
                  QStringLiteral("round %1: tier is HardwareAccelerated after invalidation").arg(round));
        } else {
            checkEqual(accelerator, QStringLiteral("tcg"),
                       QStringLiteral("round %1: accelerator is tcg after invalidation").arg(round));
            check(tier == VirtualizationTier::SoftwareEmulation,
                  QStringLiteral("round %1: tier is SoftwareEmulation after invalidation").arg(round));
        }

        // Linux backend tier() is dynamic, never Native.
        check(engine.tier() != VirtualizationTier::Native,
              QStringLiteral("round %1: backend tier is never Native").arg(round));
    }
    checkEqual(environment.snapshotCalls, kRounds,
               "probe cache invalidation triggers exactly one re-read per round");
}

// ---------------------------------------------------------------------------
// Fault injection: validate error propagation per LinuxHostState
// ---------------------------------------------------------------------------

void testValidateErrorPropagationPerState() {
    struct TestCase {
        LinuxHostSnapshot snapshot;
        QString expectedCode;
        bool hostSupported;
        QString label;
    };

    const QVector<TestCase> cases = {
        {nonLinuxHost(), QStringLiteral("host-unsupported"), false,
         QStringLiteral("non-linux host")},
        {linuxHost({}), QStringLiteral("runtime-missing"), false,
         QStringLiteral("runtime-missing host")},
        {blockedHost(), QStringLiteral("runtime-not-runnable"), false,
         QStringLiteral("runtime-not-runnable host")},
    };

    for (const TestCase& tc : cases) {
        FakeLinuxEnvironment environment;
        environment.snap = tc.snapshot;
        LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

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

        // Every error issue must have a non-empty message.
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

void testStartImmediateFailure() {
    FakeLinuxEnvironment environment;
    environment.snap = kvmHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"));
    environment.startResult = false;
    environment.startError = QStringLiteral("fork failed: Resource temporarily unavailable");
    LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    qint64 pid = 0;
    QString error;
    check(!engine.start(guestConfig(), &pid, &error), "immediate start failure is reported");
    check(!error.isEmpty(), "immediate failure fills error");
    check(error.contains(QStringLiteral("fork")),
          "immediate failure error mentions fork");

    // The engine must be usable after the failure.
    environment.startResult = true;
    pid = 0;
    error.clear();
    check(engine.start(guestConfig(), &pid, &error), "engine recovers after immediate failure");
    checkEqual(pid, 4242, "recovered start reports correct pid");
}

void testStartSuccessWithZeroPid() {
    FakeLinuxEnvironment environment;
    environment.snap = kvmHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"));
    environment.startPid = 0;
    LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    qint64 pid = 999;
    QString error;
    check(!engine.start(guestConfig(), &pid, &error), "start with zero pid is rejected");
    // pid is not modified on failure — caller must check the return value.
    check(!error.isEmpty(), "zero-pid rejection fills error");
}

void testStartSuccessWithNegativePid() {
    FakeLinuxEnvironment environment;
    environment.snap = kvmHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"));
    environment.startPid = -1;
    LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    qint64 pid = 999;
    QString error;
    check(!engine.start(guestConfig(), &pid, &error), "start with negative pid is rejected");
    check(!error.isEmpty(), "negative-pid rejection fills error");
}

// ---------------------------------------------------------------------------
// Fault injection: stop failure modes
// ---------------------------------------------------------------------------

void testStopFailure() {
    FakeLinuxEnvironment environment;
    environment.snap = kvmHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"));
    environment.stopResult = false;
    environment.stopError = QStringLiteral("kill failed: ESRCH");
    LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    QString error;
    check(!engine.stop(4242, &error), "stop failure is reported");
    check(!error.isEmpty(), "stop failure fills error");
    check(error.contains(QStringLiteral("ESRCH")),
          "stop failure error mentions ESRCH");
    checkEqual(environment.stopCalls, 1, "stop failure still called the environment");
    checkEqual(environment.lastStoppedPid, 4242, "stop failure forwarded the pid");
}

void testStopWithZeroPid() {
    FakeLinuxEnvironment environment;
    environment.snap = kvmHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"));
    LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    QString error;
    check(!engine.stop(0, &error), "stop with zero pid is rejected");
    check(!error.isEmpty(), "stop with zero pid fills error");
    checkEqual(environment.stopCalls, 0, "stop with zero pid never reaches the environment");
}

void testStopWithNegativePid() {
    FakeLinuxEnvironment environment;
    environment.snap = kvmHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"));
    LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    QString error;
    check(!engine.stop(-1, &error), "stop with negative pid is rejected");
    check(!error.isEmpty(), "stop with negative pid fills error");
    checkEqual(environment.stopCalls, 0, "stop with negative pid never reaches the environment");
}

// ---------------------------------------------------------------------------
// Fault injection: repeated invalidation without queries
// ---------------------------------------------------------------------------

void testRepeatedInvalidation() {
    FakeLinuxEnvironment environment;
    environment.snap = kvmHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"));
    LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    engine.probe();
    checkEqual(environment.snapshotCalls, 1, "first probe triggers one read");

    for (int i = 0; i < 5; ++i) {
        engine.invalidateProbe();
    }
    engine.probe();
    checkEqual(environment.snapshotCalls, 2, "after repeated invalidation, one more read");
}

// ---------------------------------------------------------------------------
// Lifecycle stress: start/stop with probe invalidation interleaved
// ---------------------------------------------------------------------------

void testStartStopWithProbeInvalidation() {
    FakeLinuxEnvironment environment;
    environment.snap = kvmHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"));
    LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    const int kRounds = 5;
    for (int round = 0; round < kRounds; ++round) {
        qint64 pid = 0;
        QString error;
        check(engine.start(guestConfig(QStringLiteral("aarch64")), &pid, &error),
              QStringLiteral("start succeeds on round %1").arg(round));
        checkEqual(pid, 4242, QStringLiteral("round %1: pid is correct").arg(round));

        engine.invalidateProbe();
        check(engine.isHostSupported(),
              QStringLiteral("round %1: host still supported after invalidation").arg(round));

        check(engine.stop(pid, &error),
              QStringLiteral("stop succeeds on round %1").arg(round));
    }
    checkEqual(environment.startCalls, kRounds, "start called kRounds times");
    checkEqual(environment.stopCalls, kRounds, "stop called kRounds times");
}

// ---------------------------------------------------------------------------
// Lifecycle stress: many start/stop cycles
// ---------------------------------------------------------------------------

void testStressStartStopCycles() {
    FakeLinuxEnvironment environment;
    environment.snap = kvmHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"));
    LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    const int kCycles = 20;
    const qint64 kExpectedPid = 4242;
    for (int cycle = 0; cycle < kCycles; ++cycle) {
        qint64 pid = 0;
        QString error;
        check(engine.start(guestConfig(QStringLiteral("aarch64")), &pid, &error),
              QStringLiteral("stress cycle %1: start succeeds").arg(cycle));
        checkEqual(pid, kExpectedPid,
                   QStringLiteral("stress cycle %1: pid is stable").arg(cycle));
        check(engine.stop(pid, &error),
              QStringLiteral("stress cycle %1: stop succeeds").arg(cycle));
    }
    checkEqual(environment.startCalls, kCycles, "stress: start call count matches cycles");
    checkEqual(environment.stopCalls, kCycles, "stress: stop call count matches cycles");

    check(engine.isHostSupported(), "stress: host still supported after all cycles");
    checkEqual(engine.statusCode(guestConfig()), QStringLiteral("available"),
               "stress: status code stable after all cycles");
}

// ---------------------------------------------------------------------------
// Linux-specific: /dev/kvm missing (kvmDevicePresent=false)
// ---------------------------------------------------------------------------

void testKvmDeviceMissing() {
    FakeLinuxEnvironment environment;
    LinuxHostSnapshot snap = linuxHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"));
    snap.kvmDevicePresent = false;
    snap.kvmDeviceAccessible = false;
    snap.kvmApiProbed = false;
    environment.snap = snap;
    LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    check(engine.isHostSupported(), "kvm-missing host is still supported (TCG)");
    checkEqual(linuxAcceleratorFor(engine.hostSnapshot(), QStringLiteral("aarch64")), QStringLiteral("tcg"),
               "kvm-missing accelerator is tcg");
    check(engine.executionTierFor(QStringLiteral("aarch64")) == VirtualizationTier::SoftwareEmulation,
          "kvm-missing tier is SoftwareEmulation");

    const QVector<EngineIssue> issues = engine.validate(guestConfig());
    check(!hasEngineErrors(issues), "kvm-missing has no hard errors (TCG fallback)");
check(hasIssueAtLevel(issues, QStringLiteral("software-emulation"), EngineIssueLevel::Warning),
          "kvm-missing reports software-emulation warning");

    // Start works via TCG.
    qint64 pid = 0;
    QString error;
    check(engine.start(guestConfig(), &pid, &error), "kvm-missing start succeeds via TCG");
    checkEqual(environment.lastStartConfig.accelerator, QStringLiteral("tcg"),
               "kvm-missing start uses tcg accelerator");
}

// ---------------------------------------------------------------------------
// Linux-specific: /dev/kvm permission denied (EACCES)
// ---------------------------------------------------------------------------

void testKvmDevicePermissionDenied() {
    FakeLinuxEnvironment environment;
    LinuxHostSnapshot snap = linuxHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"));
    snap.kvmDevicePresent = true;
    snap.kvmDeviceAccessible = false;
    snap.kvmApiProbed = false;
    snap.kvmOpenErrno = EACCES;
    environment.snap = snap;
    LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    check(engine.isHostSupported(), "kvm-permission-denied host is still supported (TCG)");
    checkEqual(linuxAcceleratorFor(engine.hostSnapshot(), QStringLiteral("aarch64")), QStringLiteral("tcg"),
               "kvm-permission-denied accelerator is tcg");

    const QVector<EngineIssue> issues = engine.validate(guestConfig());
    check(!hasEngineErrors(issues), "kvm-permission-denied has no hard errors");
check(hasIssue(issues, QStringLiteral("kvm-api-error")),
          "kvm-permission-denied reports kvm-api-error issue");

    // Diagnose mentions EACCES.
    const QString diag = engine.diagnose(guestConfig());
    check(diag.contains(QStringLiteral("EACCES")) || diag.contains(QStringLiteral("13")),
          "diagnose mentions EACCES or errno 13");
}

// ---------------------------------------------------------------------------
// Linux-specific: KVM API version 0 (kernel too old)
// ---------------------------------------------------------------------------

void testKvmApiVersionZero() {
    FakeLinuxEnvironment environment;
    LinuxHostSnapshot snap = kvmHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"), 0);
    environment.snap = snap;
    LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    // API version 0 is not supported → TCG fallback.
    check(engine.isHostSupported(), "kvm-api-version-0 host is supported (TCG)");
    checkEqual(linuxAcceleratorFor(engine.hostSnapshot(), QStringLiteral("aarch64")), QStringLiteral("tcg"),
               "kvm-api-version-0 accelerator is tcg");
    check(engine.executionTierFor(QStringLiteral("aarch64")) == VirtualizationTier::SoftwareEmulation,
          "kvm-api-version-0 tier is SoftwareEmulation");
}

// ---------------------------------------------------------------------------
// Linux-specific: KVM API version negative (corrupted probe)
// ---------------------------------------------------------------------------

void testKvmApiVersionNegative() {
    FakeLinuxEnvironment environment;
    LinuxHostSnapshot snap = kvmHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"), -1);
    environment.snap = snap;
    LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    check(engine.isHostSupported(), "kvm-api-version-negative host is supported (TCG)");
    checkEqual(linuxAcceleratorFor(engine.hostSnapshot(), QStringLiteral("aarch64")), QStringLiteral("tcg"),
               "kvm-api-version-negative accelerator is tcg");
    check(engine.executionTierFor(QStringLiteral("aarch64")) == VirtualizationTier::SoftwareEmulation,
          "kvm-api-version-negative tier is SoftwareEmulation");
}

// ---------------------------------------------------------------------------
// Linux-specific: x86_64 same-ISA with KVM
// ---------------------------------------------------------------------------

void testX86_64SameIsaKvm() {
    FakeLinuxEnvironment environment;
    environment.snap = kvmHost({QStringLiteral("x86_64")}, QStringLiteral("x86_64"));
    LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    check(engine.supportsGuestArch(QStringLiteral("x86_64")), "x86_64 same-ISA guest supported");
    checkEqual(linuxAcceleratorFor(engine.hostSnapshot(), QStringLiteral("x86_64")), QStringLiteral("kvm"),
               "x86_64 same-ISA accelerator is kvm");
    check(engine.executionTierFor(QStringLiteral("x86_64")) == VirtualizationTier::HardwareAccelerated,
          "x86_64 same-ISA tier is HardwareAccelerated");

    qint64 pid = 0;
    QString error;
    check(engine.start(guestConfig(QStringLiteral("x86_64")), &pid, &error),
          "x86_64 same-ISA start succeeds");
    checkEqual(environment.lastStartConfig.accelerator, QStringLiteral("kvm"),
               "x86_64 same-ISA start uses kvm");
}

// ---------------------------------------------------------------------------
// Linux-specific: cross-ISA start is refused
// ---------------------------------------------------------------------------

void testCrossIsaStartRefused() {
    FakeLinuxEnvironment environment;
    environment.snap = kvmHost({QStringLiteral("aarch64"), QStringLiteral("x86_64")}, QStringLiteral("aarch64"));
    LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    // Cross-ISA: aarch64 host, x86_64 guest.
    qint64 pid = 0;
    QString error;
    check(!engine.start(guestConfig(QStringLiteral("x86_64")), &pid, &error),
          "cross-ISA start is refused");
    checkEqual(environment.startCalls, 0, "cross-ISA start never reaches the spawner");
    check(error.contains(QStringLiteral("qemu-tcg")),
          "cross-ISA start error points at qemu-tcg");

    // Same-ISA start succeeds.
    pid = 0;
    error.clear();
    check(engine.start(guestConfig(QStringLiteral("aarch64")), &pid, &error),
          "same-ISA start succeeds");
    checkEqual(pid, 4242, "same-ISA start reports pid");
}

// ---------------------------------------------------------------------------
// Linux-specific: RISC-V64 not falsely reported as mature
// ---------------------------------------------------------------------------

void testRiscv64NotFalselyMature() {
    FakeLinuxEnvironment environment;
    environment.snap = kvmHost({QStringLiteral("riscv64")}, QStringLiteral("riscv64"));
    LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    // RISC-V64 KVM is usable but not falsely claimed as production-mature.
    check(engine.supportsGuestArch(QStringLiteral("riscv64")), "riscv64 guest supported");
    checkEqual(linuxAcceleratorFor(engine.hostSnapshot(), QStringLiteral("riscv64")), QStringLiteral("kvm"),
               "riscv64 accelerator is kvm");

    const QVector<EngineIssue> issues = engine.validate(guestConfig(QStringLiteral("riscv64")));
    // Must have a maturity warning, not an error.
    check(!hasEngineErrors(issues), "riscv64 has no hard errors");
check(hasIssue(issues, QStringLiteral("kvm-maturity-experimental")),
          "riscv64 reports maturity warning");
}

// ---------------------------------------------------------------------------
// Linux-specific: all unsupported/error reasons must be non-empty
// ---------------------------------------------------------------------------

void testAllReasonsNonEmpty() {
    struct Case {
        LinuxHostSnapshot snapshot;
        QString guestArch;
        bool expectHostSupported;
        bool expectGuestSupported;
        QString label;
    };

    const QVector<Case> cases = {
        {nonLinuxHost(), QStringLiteral("aarch64"), false, false,
         QStringLiteral("non-linux")},
        {linuxHost({}), QStringLiteral("aarch64"), false, false,
         QStringLiteral("runtime-missing")},
        {blockedHost(), QStringLiteral("aarch64"), false, false,
         QStringLiteral("runtime-not-runnable")},
        {kvmHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64")), QStringLiteral("x86_64"), true, false,
         QStringLiteral("kvm-available cross-ISA")},
        {linuxHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64")), QStringLiteral("riscv64"), true, false,
         QStringLiteral("tcg-only unsupported arch")},
    };

    for (const Case& c : cases) {
        FakeLinuxEnvironment environment;
        environment.snap = c.snapshot;
        LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

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
// Linux-specific: statusCode stability across repeated queries
// ---------------------------------------------------------------------------

void testStatusCodeStability() {
    FakeLinuxEnvironment environment;
    environment.snap = kvmHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"));
    LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    const QString expected = QStringLiteral("available");
    for (int i = 0; i < 10; ++i) {
        checkEqual(engine.statusCode(guestConfig()), expected,
                   QStringLiteral("statusCode is stable on query %1").arg(i));
    }
    checkEqual(environment.snapshotCalls, 1, "statusCode stability: only one snapshot read");
}

// ---------------------------------------------------------------------------
// Linux-specific: probe diagnostics are non-empty
// ---------------------------------------------------------------------------

void testProbeDiagnosticsNonEmpty() {
    FakeLinuxEnvironment environment;
    environment.snap = kvmHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"));
    LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    const EngineProbeResult probe = engine.probe();
    check(!probe.summary.isEmpty(), "probe summary is non-empty");
    for (const EngineIssue& issue : probe.diagnostics) {
        check(!issue.message.isEmpty(),
              QStringLiteral("probe diagnostic message is non-empty (code=%1)").arg(issue.code));
    }
}

// ---------------------------------------------------------------------------
// Linux-specific: validate with malformed config
// ---------------------------------------------------------------------------

void testValidateMalformedConfig() {
    FakeLinuxEnvironment environment;
    environment.snap = kvmHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"));
    LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

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
    nulDisk.disks[0].path = QStringLiteral("/tmp") + QChar(0) + QStringLiteral("/disk.qcow2");
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

// ---------------------------------------------------------------------------
// Linux-specific: device disappears mid-session
// ---------------------------------------------------------------------------

void testKvmDeviceDisappearsAndReturns() {
    FakeLinuxEnvironment environment;
    environment.snap = kvmHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"));
    LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    // Initially accelerated.
    checkEqual(linuxAcceleratorFor(engine.hostSnapshot(), QStringLiteral("aarch64")), QStringLiteral("kvm"),
               "initially accelerated");

    // /dev/kvm disappears.
    LinuxHostSnapshot noKvm = linuxHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"));
    noKvm.kvmDevicePresent = false;
    noKvm.kvmDeviceAccessible = false;
    noKvm.kvmApiProbed = false;
    environment.snap = noKvm;
    engine.invalidateProbe();

checkEqual(linuxAcceleratorFor(engine.hostSnapshot(), QStringLiteral("aarch64")), QStringLiteral("tcg"),
               "after device disappears: tcg");
    checkEqual(engine.statusCode(guestConfig()), QStringLiteral("degraded"),
               "after device disappears: status is degraded");

    // /dev/kvm returns.
    environment.snap = kvmHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"));
    engine.invalidateProbe();

checkEqual(linuxAcceleratorFor(engine.hostSnapshot(), QStringLiteral("aarch64")), QStringLiteral("kvm"),
               "after device returns: kvm");
    checkEqual(engine.statusCode(guestConfig()), QStringLiteral("available"),
               "after device returns: status is available");
}

// ---------------------------------------------------------------------------
// Linux-specific: KVM errno propagation in diagnostics
// ---------------------------------------------------------------------------

void testKvmErrnoPropagation() {
    FakeLinuxEnvironment environment;
    LinuxHostSnapshot snap = linuxHost({QStringLiteral("aarch64")}, QStringLiteral("aarch64"));
    snap.kvmDevicePresent = true;
    snap.kvmDeviceAccessible = false;
    snap.kvmApiProbed = false;
    snap.kvmOpenErrno = EACCES;
    environment.snap = snap;
    LinuxKvmEngine engine(QSharedPointer<LinuxEnvironment>(&environment, [](LinuxEnvironment*) {}));

    const QString diag = engine.diagnose(guestConfig());
    // The diagnostics must mention the errno value.
    check(diag.contains(QStringLiteral("EACCES")) || diag.contains(QStringLiteral("13")),
          "diagnose propagates EACCES errno");

    // Verify the issue code is specific.
    const QVector<EngineIssue> issues = engine.validate(guestConfig());
check(hasIssue(issues, QStringLiteral("kvm-api-error")),
          "validate reports kvm-api-error");
}

} // namespace

int main(int argc, char** argv) {
    QCoreApplication application(argc, argv);
    std::fprintf(stdout, "LinuxKvmEngineTests\n");

    testIdentity();
    testCandidateArchitectures();
    testStateMachine();
    testStateMessageInterpolation();
    testNeverClaimsNativeTier();
    testNonLinuxHostIsNeverSupported();
    testRuntimeMissing();
    testRuntimeNotRunnable();
    testRuntimeProbeTimeout();
    testTcgDegradedIsUsableButHonest();
    testKvmAcceleratedIsUsableAndHonest();
    testKvmApiProbeDeterminesAccelerated();
    testKvmErrnoDiagnostics();
    testVcpuExceedsKvmMax();
    testRiscv64KvmMaturity();
    testKvmApiVersionInProbe();
    testConfigValidation();
    testArgumentVectorSafety();
    testBuildArgumentsAppliesAccelerator();
    testNoShellStringIsProduced();
    testStartSuccessAndFailurePaths();
    testStopPaths();
    testRepeatedStartStop();
    testProbeIsCached();
    testEnvironmentSnapshotChange();
    testRuntimeDisappearsAndReturns();
    testRequirementsAreHonest();
    testProbeForArchitecture();
    testCrossIsaIsRefusedEvenWithEmulator();
    testRealHostIsNotMisreported();
    testRealEnvironment();
    testFactoryAndRealEngine();
    testRegistryRegistration();

    // Fault injection, lifecycle stress, and Linux-specific coverage.
    testProbeCacheInvalidationUnderMutation();
    testValidateErrorPropagationPerState();
    testStartImmediateFailure();
    testStartSuccessWithZeroPid();
    testStartSuccessWithNegativePid();
    testStopFailure();
    testStopWithZeroPid();
    testStopWithNegativePid();
    testRepeatedInvalidation();
    testStartStopWithProbeInvalidation();
    testStressStartStopCycles();
    testKvmDeviceMissing();
    testKvmDevicePermissionDenied();
    testKvmApiVersionZero();
    testKvmApiVersionNegative();
    testX86_64SameIsaKvm();
    testCrossIsaStartRefused();
    testRiscv64NotFalselyMature();
    testAllReasonsNonEmpty();
    testStatusCodeStability();
    testProbeDiagnosticsNonEmpty();
    testValidateMalformedConfig();
    testKvmDeviceDisappearsAndReturns();
    testKvmErrnoPropagation();

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