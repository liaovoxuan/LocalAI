// LinuxKvmEngine.cpp — Linux KVM virtualisation backend.
//
// This file owns the only impure parts of the backend:
//
//   1. Reading the host: the Linux family marker and host arch come from the
//      shared vw::detectHostProfile(); QEMU availability comes from the shared
//      vw::RuntimeManager (which already knows about the
//      runtime/qemu/linux/<arch> layout); /dev/kvm presence, accessibility and
//      KVM API version come from a single open()+ioctl probe (see below).
//   2. Resolving the QEMU program and its argument vector, by delegating to the
//      application's existing vw::QemuEngine. That is deliberate: it keeps one
//      single source of truth for QEMU arguments, guarantees the "-machine
//      <m>,accel=<a>" accelerator handling stays consistent with every other
//      backend, and means there is no second place where a command line could
//      accidentally be built by string concatenation.
//   3. Spawning and stopping the process (QProcess with an argument vector).
//
// Everything decision-shaped lives in the header, driven by the snapshot this
// file produces, so the backend can be unit-tested on any platform.
//
// IMPORTANT probing rule: on a host that is not Linux this file performs NO
// QEMU probing at all. vw::RuntimeManager runs `<qemu> --version` with a
// timeout, which is far too expensive (and too side-effecting) to do on every
// macOS/Windows capability query. The snapshot is therefore built in two stages
// and the second stage is skipped entirely off-platform.

#include "LinuxKvmEngine.hpp"

#include "Platform.hpp"
#include "QemuEngine.hpp"
#include "RuntimeManager.hpp"

#include <QDir>
#include <QFileInfo>
#include <QProcess>
#include <QProcessEnvironment>
#include <QStandardPaths>
#include <QtGlobal>

namespace vw {

namespace {

// Mirrors RuntimeManager's own probe budget so a wedged binary cannot hang the
// caller; RuntimeManager::isExecutableHealthy() uses the same 8s.
constexpr int kRuntimeProbeTimeoutMs = 8000;

bool isLinuxFamily(const QString& family) {
    return family.compare(QStringLiteral("linux"), Qt::CaseInsensitive) == 0;
}

// Returns a path to an executable named `name` that exists on disk but that we
// have not proven runnable, or an empty string when nothing exists at all.
//
// This is what lets the backend tell "QEMU was never installed" apart from
// "QEMU is installed but this binary refuses to execute" (e.g. wrong ELF
// interpreter, missing lib, or sandbox policy). findSystemEmulator() can only
// answer the former, because it filters on a successful `--version` run.
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

// ---------------------------------------------------------------------------
// /dev/kvm probing
// ---------------------------------------------------------------------------
//
// The only place in the backend that talks to the KVM kernel interface
// directly; everything else goes through QEMU.
//
// A single open() of /dev/kvm is used to establish three facts at once:
//
//   * present     — the character device node exists. open() failing with
//                   ENOENT is the only reliable way to say "no such device";
//                   any other errno means the node is there but unusable
//                   (EACCES without the kvm group, EBUSY while another
//                   component holds it exclusively, ...).
//   * accessible  — this process may open it read/write (open succeeded).
//   * probed      — KVM_GET_API_VERSION / KVM_CHECK_EXTENSION answered.
//
// Reading the node with QFileInfo first would only repeat the same kernel
// check twice and could never report *which* errno the kernel returned, which
// is the one thing that turns "KVM unavailable" into an actionable message.
//
// The descriptor is released on every path — including the early returns —
// through an RAII guard, because a leaked fd here would leak once per
// capability query and capability queries happen on every `--list-backends`.
//
// Nothing in this file ever issues KVM_CREATE_VM: a capability query must not
// have side effects on the host, and it must never start a real VM.

#if defined(Q_OS_LINUX)

#include <cerrno>
#include <fcntl.h>
#include <sys/ioctl.h>
#include <unistd.h>

// KVM ioctl constants. These are a stable ABI defined in <linux/kvm.h>, but we
// spell them out here so this translation unit never depends on the kernel
// headers being installed.
#ifndef KVM_GET_API_VERSION
#define KVM_GET_API_VERSION 0x00
#endif
#ifndef KVM_CHECK_EXTENSION
#define KVM_CHECK_EXTENSION 0x03
#endif
#ifndef KVM_CAP_MAX_VCPUS
#define KVM_CAP_MAX_VCPUS 0x04
#endif
#ifndef KVM_CAP_NR_MEMSLOTS
#define KVM_CAP_NR_MEMSLOTS 0x09
#endif

namespace {

// Closes the KVM descriptor exactly once, on every exit path.
class KvmDescriptor {
public:
    explicit KvmDescriptor(int fd) : fd_(fd) {}
    ~KvmDescriptor() {
        if (fd_ >= 0) {
            ::close(fd_);
        }
    }
    KvmDescriptor(const KvmDescriptor&) = delete;
    KvmDescriptor& operator=(const KvmDescriptor&) = delete;

    int get() const { return fd_; }

private:
    int fd_;
};

} // namespace

void probeKvmDevice(LinuxHostSnapshot& snapshot) {
    snapshot.kvmApiProbed = true;

    errno = 0;
    const int fd = ::open("/dev/kvm", O_RDWR | O_CLOEXEC);
    if (fd < 0) {
        const int openErrno = errno;
        snapshot.kvmOpenErrno = openErrno;
        // ENOENT is the only errno that means "there is no such device node".
        // Anything else (EACCES, EBUSY, EPERM, ...) means the node exists but
        // this process cannot use it, and the diagnostics should say so.
        snapshot.kvmDevicePresent = openErrno != ENOENT;
        snapshot.kvmDeviceAccessible = false;
        snapshot.kvmApiVersion = 0;
        return;
    }
    const KvmDescriptor descriptor(fd);
    snapshot.kvmDevicePresent = true;
    snapshot.kvmDeviceAccessible = true;

    // KVM_GET_API_VERSION is ABI-frozen at 12 on every kernel since 2.6.9. Any
    // other positive value means this process and that kernel do not speak the
    // same KVM ABI, so claiming hardware acceleration would be a guess.
    errno = 0;
    const long version = ::ioctl(descriptor.get(), KVM_GET_API_VERSION, 0);
    if (version < 0) {
        snapshot.kvmOpenErrno = errno;
        snapshot.kvmApiVersion = 0;
        return;
    }
    snapshot.kvmApiVersion = static_cast<int>(version);

    // KVM_CHECK_EXTENSION returns > 0 when the extension is available, 0 when it
    // is not, and < 0 when the extension number is unknown. Only a positive
    // answer is recorded; the call is advisory and must not turn a working KVM
    // into a failed probe.
    errno = 0;
    const long maxVcpus = ::ioctl(descriptor.get(), KVM_CHECK_EXTENSION, KVM_CAP_MAX_VCPUS);
    if (maxVcpus > 0) {
        snapshot.kvmMaxVcpus = static_cast<int>(maxVcpus);
    }
    errno = 0;
    const long maxMemSlots = ::ioctl(descriptor.get(), KVM_CHECK_EXTENSION, KVM_CAP_NR_MEMSLOTS);
    if (maxMemSlots > 0) {
        snapshot.kvmMaxMemorySlots = static_cast<int>(maxMemSlots);
    }
}

#else // not Linux

// Off-platform the probe is a no-op and no POSIX header is included: the
// snapshot keeps its defaults (present=false, accessible=false, probed=false)
// so the state machine can never claim hardware acceleration here, and the file
// still compiles on Windows, macOS and HarmonyOS.
void probeKvmDevice(LinuxHostSnapshot&) {}

#endif

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
    // findSystemEmulator() already ran (and rejected) the health probe; remember
    // whichever candidate existed so the caller can report "found but blocked".
    result.unprovenCandidate = findUnprovenCandidate(runtime, executableForArch(arch));
    if (!result.unprovenCandidate.isEmpty()) {
        // A binary existed but `--version` did not succeed. Distinguish a
        // genuine *timeout* from a hard failure, because the two need different
        // advice ("raise the probe budget" vs. "wrong ELF interpreter / missing
        // library / sandbox policy"). This is the only place that pays for the
        // extra probe, and only on a Linux host with a QEMU binary present.
        QProcess process;
        process.setProgram(result.unprovenCandidate);
        process.setArguments({QStringLiteral("--version")});
        process.start();
        // waitForStarted() first: waitForFinished() also returns false when the
        // process never started at all (FailedToStart), which would otherwise be
        // misreported as a timeout.
        const bool started = process.waitForStarted(kRuntimeProbeTimeoutMs);
        const bool finished = started && process.waitForFinished(kRuntimeProbeTimeoutMs);
        if (!finished) {
            result.timedOut = started;
            // SIGKILL is not catchable, so this wait is bounded in practice; the
            // explicit wait also keeps QProcess from warning about a still-running
            // child when it goes out of scope.
            process.kill();
            process.waitForFinished(2000);
        }
    }
    return result;
}

// The real environment: host probe over the shared platform layer plus the
// application's existing QEMU plumbing.
class QemuLinuxKvmEnvironment : public LinuxEnvironment {
public:
    LinuxHostSnapshot snapshot() const override {
        LinuxHostSnapshot snapshot;
        const HostProfile host = detectHostProfile();
        snapshot.hostFamily = host.family;
        snapshot.hostName = host.name;
        snapshot.hostPlatformName = platformName(host.platform);
        snapshot.hostArch = canonicalHostArch(host.arch);
        snapshot.hostIsLinux = isLinuxFamily(host.family);

        if (!snapshot.hostIsLinux) {
            // Off-platform: deliberately no QEMU probing, no process spawning,
            // no /dev/kvm lookups. The backend reports host-unsupported cheaply.
            snapshot.detail = QStringLiteral("宿主平台 %1 不是 Linux，未执行 QEMU 探测。")
                                  .arg(snapshot.hostPlatformName);
            return snapshot;
        }

        snapshot.qemuProbePerformed = true;
        snapshot.detail = QStringLiteral("Linux 宿主（%1），架构 %2。")
                              .arg(host.family, snapshot.hostArch);

        const RuntimeManager runtime;
        bool anyUnproven = false;
        bool anyTimedOut = false;
        for (const QString& arch : linuxCandidateGuestArches()) {
            const ArchRuntime probed = probeArchRuntime(runtime, arch);
            if (probed.runnable) {
                snapshot.runnableGuestArches << arch;
                if (snapshot.qemuPath.isEmpty()) {
                    snapshot.qemuPath = probed.executable;
                    snapshot.qemuSource = probed.source;
                }
            }
            if (!probed.unprovenCandidate.isEmpty()) {
                anyUnproven = true;
                if (snapshot.qemuPath.isEmpty()) {
                    snapshot.qemuPath = probed.unprovenCandidate;
                    snapshot.qemuSource = QStringLiteral("unrunnable");
                }
            }
            anyTimedOut = anyTimedOut || probed.timedOut;
        }
        snapshot.qemuFound = !snapshot.runnableGuestArches.isEmpty() || anyUnproven;
        snapshot.qemuExecutable = !snapshot.runnableGuestArches.isEmpty();
        snapshot.qemuProbeTimedOut = anyTimedOut;
        if (snapshot.qemuSource.isEmpty()) {
            snapshot.qemuSource = QStringLiteral("missing");
        }

#if defined(Q_OS_LINUX)
        // --- /dev/kvm --------------------------------------------------------
        // One open() establishes presence, accessibility and — when it succeeds —
        // the KVM API version. This is the only /dev/kvm access the backend ever
        // performs, and it never creates a VM.
        probeKvmDevice(snapshot);
#endif

        if (snapshot.runnableGuestArches.isEmpty()) {
            snapshot.notes << (snapshot.qemuFound
                                   ? QStringLiteral("QEMU 可执行文件存在但无法运行：可能是缺少动态库、"
                                                    "错误的 ELF 解释器或沙箱策略限制。")
                                   : QStringLiteral("未找到 QEMU 系统模拟器；请安装到 "
                                                    "runtime/qemu/linux/%1/bin 或系统 PATH。")
                                         .arg(snapshot.hostArch));
        }
        if (snapshot.kvmDevicePresent && !snapshot.kvmDeviceAccessible) {
            // The node is there but open() failed. Say *why*: the fix (join the
            // kvm group, free the device, ...) depends entirely on the errno.
            const QString hint = linuxKvmErrnoMessage(snapshot.kvmOpenErrno);
            snapshot.notes << (hint.isEmpty()
                                   ? QStringLiteral("/dev/kvm 存在但无法打开；硬件加速不可用，将使用 TCG。")
                                   : QStringLiteral("/dev/kvm 存在但无法打开：%1 硬件加速不可用，将使用 TCG。")
                                         .arg(hint));
        }
        if (snapshot.kvmDevicePresent && snapshot.kvmDeviceAccessible
            && !snapshot.runnableGuestArches.contains(snapshot.hostArch)) {
            snapshot.notes << QStringLiteral("/dev/kvm 可用，但缺少宿主同架构（%1）的 QEMU 模拟器，"
                                             "无法利用硬件加速。")
                                   .arg(snapshot.hostArch);
        }
        if (!snapshot.kvmDevicePresent) {
            const QString hint = linuxKvmErrnoMessage(snapshot.kvmOpenErrno);
            snapshot.notes << (hint.isEmpty()
                                   ? QStringLiteral("/dev/kvm 不存在：内核未启用 KVM 模块或硬件不支持"
                                                    "虚拟化扩展。")
                                   : QStringLiteral("/dev/kvm 不可用：%1").arg(hint));
        }
        if (snapshot.kvmDeviceAccessible && snapshot.kvmApiProbed && snapshot.kvmApiVersion <= 0) {
            const QString hint = linuxKvmErrnoMessage(snapshot.kvmOpenErrno);
            snapshot.notes << (hint.isEmpty()
                                   ? QStringLiteral("/dev/kvm 可访问但 KVM API 探测失败："
                                                    "KVM 内核模块可能未正确加载或版本不兼容。")
                                   : QStringLiteral("/dev/kvm 可访问但 KVM API 探测失败：%1").arg(hint));
        }
        if (snapshot.kvmDeviceAccessible && snapshot.kvmApiProbed && snapshot.kvmApiVersion > 0) {
            snapshot.notes << QStringLiteral("KVM API 版本 %1 可用。").arg(snapshot.kvmApiVersion);
            if (!linuxKvmApiVersionSupported(snapshot.kvmApiVersion)) {
                snapshot.notes << QStringLiteral("KVM API 版本与期望的 %1 不符：本次不会声称硬件加速，"
                                                 "将使用 TCG。")
                                      .arg(kLinuxKvmApiVersion);
            }
            if (snapshot.kvmMaxVcpus > 0) {
                snapshot.notes << QStringLiteral("KVM 最大 vCPU 数：%1。").arg(snapshot.kvmMaxVcpus);
            }
        }
        if (snapshot.qemuProbeTimedOut) {
            snapshot.notes << QStringLiteral("QEMU --version 探测超时，运行时结论可能不完整。");
        }
        snapshot.notes << QStringLiteral("本后端一律通过 QEMU 运行客户机；即使使用 KVM 硬件加速，"
                                         "也不是宿主原生虚拟化接口。");
        return snapshot;
    }

    QString program(const VmConfig& config) const override {
        const QString arch = linuxCanonicalGuestArch(config.architecture);
        if (arch.isEmpty()) {
            return QString();
        }
        return QemuEngine().detect(arch).systemEmulator.executable;
    }

    QStringList arguments(const VmConfig& config) const override {
        return QemuEngine().buildArguments(config);
    }

    bool startProcess(const VmConfig& config, qint64* processId, QString* error) override {
        // QemuEngine::startVm() resolves the runtime, validates the boot/disk
        // files, renders the argument vector and calls startDetached(). It never
        // goes through a shell.
        return QemuEngine().startVm(config, processId, error);
    }

    bool stopProcess(qint64 processId, QString* error) override {
        return QemuEngine().stopVm(processId, error);
    }
};

} // namespace

QSharedPointer<LinuxEnvironment> makeLinuxEnvironment() {
    return QSharedPointer<LinuxEnvironment>(new QemuLinuxKvmEnvironment());
}

bool linuxKvmEngineAvailable() {
    // Never lies: false when the host is not Linux, and false when no QEMU
    // runtime could be verified for any candidate architecture.
    return LinuxKvmEngine().isHostSupported();
}

VirtualizationEnginePtr makeLinuxKvmEngine() {
    return VirtualizationEnginePtr(new LinuxKvmEngine());
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
// Registration is unconditional (like WindowsHypervisorEngine and
// HarmonyVirtualizationEngine) because the backend is plain Qt code on every
// platform and reports `host-unsupported` honestly elsewhere, which is what
// lets `--list-backends` document the whole matrix. registerBackend() replaces
// by id, so an explicit registration from registerBuiltinBackends() would be
// harmless rather than duplicative.

#include "VirtualizationEngineRegistry.hpp"

namespace {

const bool vwLinuxKvmBackendRegistered = []() {
    vw::VirtualizationEngineRegistry::instance().registerBackend(
        vw::makeLinuxKvmEngine());
    return true;
}();

} // namespace