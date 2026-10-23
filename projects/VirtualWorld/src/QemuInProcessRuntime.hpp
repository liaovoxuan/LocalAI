/*
 * QemuInProcessRuntime.hpp
 *
 * C++ wrapper around the in-process QEMU core (qemu-source/qemu-inprocess).
 *
 * Hard guarantees (enforced by design and verified by a static scan in the
 * build, see build-qemu-inprocess/CMakeLists.txt):
 *   - QEMU runs as a statically linked native library on a worker thread of
 *     the host process. There is NO QProcess / fork / exec / system / popen /
 *     startDetached / shell anywhere in this file or its dependencies here.
 *   - A VM is addressed by an OPAQUE session handle, never by a PID.
 *   - At most one in-process QEMU instance may be active per process
 *     (QEMU's global state is not multi-init safe). A second concurrent
 *     create() returns QemuErrorCode::Busy with a stable error.
 *   - stop() and destroy() are reentrant and idempotent.
 *   - A failed init releases every resource it grabbed (no leaked core).
 *   - The accelerator policy never reports hardware acceleration for a
 *     cross-ISA guest.
 *
 * This wrapper does NOT bundle any guest ROM or OS image. Disk/ROM contents
 * are supplied at runtime through the block callbacks.
 */
#pragma once

#include <cstdint>
#include <functional>
#include <string>

// The in-process core C ABI. No subprocess machinery is exposed here.
#include "qemu_inprocess.h"

namespace vw {
namespace qemu {

// Guest/host instruction-set architectures we reason about for the
// same-ISA / cross-ISA accelerator policy.
enum class Isa { Unknown, X86_64, AArch64, ARM, RISCV64, LoongArch64 };

enum class Accel { KVM, TCG };

enum class SessionState {
    Idle,
    Initialising,
    Running,
    Stopping,
    Stopped,
    Error,
    Destroyed
};

enum class QemuErrorCode {
    Ok = 0,
    Busy,             // single-instance conflict
    InvalidArgument,
    InitFailed,
    NotInitialised,
    AlreadyStopped,
    NotFound,         // handle not known to this runtime
};

struct AccelDecision {
    Accel accel;
    bool hardwareAccelerated; // true ONLY for same-ISA KVM
    const char* reason;
};

// Host-supplied frontend callbacks. All are invoked from QEMU worker/IO
// threads; implement them thread-safely on the host side.
struct QemuCallbacks {
    std::function<void(const void* pixels, int w, int h, int stride, int format)> displayUpdate;
    std::function<int(const void* event, size_t len)> inputEvent;
    std::function<void(const int16_t* samples, size_t frames, int channels, int rate)> audioOut;
    std::function<int(int16_t* samples, size_t frames, int channels, int rate)> audioIn;
    std::function<int(uint32_t diskId, uint64_t sector, void* buf, size_t len)> blockRead;
    std::function<int(uint32_t diskId, uint64_t sector, const void* buf, size_t len)> blockWrite;
};

struct QemuLaunchConfig {
    Isa targetIsa = Isa::Unknown;
    uint64_t memoryBytes = 0;
    // Raw qemu-system style arguments (machine, devices, ...). The wrapper
    // translates relevant ones; the rest are forwarded verbatim to the core.
    std::string machine;
    std::string extraArgs; // space-separated, appended to the core argv
};

// Opaque session handle. Internals live in the .cpp (pimpl). External code
// never sees a PID or any core pointer.
struct QemuSession;
using QemuSessionHandle = QemuSession*;

class QemuInProcessRuntime {
public:
    QemuInProcessRuntime();
    ~QemuInProcessRuntime();

    QemuInProcessRuntime(const QemuInProcessRuntime&) = delete;
    QemuInProcessRuntime& operator=(const QemuInProcessRuntime&) = delete;

    // Create a session for the given tag. Returns Busy if an ACTIVE session
    // with the same tag already exists in this process. The handle is opaque.
    QemuErrorCode create(const std::string& tag, QemuSessionHandle& outHandle);

    // Set the host ISA used by the accelerator policy. Defaults to AArch64
    // (the HarmonyOS/OpenHarmony target). Call before init().
    void setHostIsa(Isa hostIsa);

    // Pure policy: choose the accelerator.
    //   same-ISA + KVM node accessible        -> KVM (hardware accelerated)
    //   same-ISA + KVM node NOT accessible    -> TCG
    //   cross-ISA                             -> TCG (NEVER hardware accel)
    static AccelDecision decideAccel(Isa hostIsa, Isa targetIsa,
                                     bool kvmNodeAccessible);

    // Initialise the in-process core (argv derived from cfg + accel decision).
    // On failure releases resources and returns InitFailed (session stays valid
    // but in Error state, safe to destroy).
    QemuErrorCode init(QemuSessionHandle handle, const QemuLaunchConfig& cfg);

    // Spawn the worker thread that runs the QEMU main loop.
    QemuErrorCode start(QemuSessionHandle handle);

    // Reentrant, idempotent stop. Safe to call before init, after stop, or
    // concurrently from several threads.
    QemuErrorCode stop(QemuSessionHandle handle);

    // Reentrant, idempotent destroy. Joins the worker thread if needed,
    // releases the core, unregisters the tag.
    QemuErrorCode destroy(QemuSessionHandle handle);

    SessionState state(QemuSessionHandle handle) const;
    void setCallbacks(QemuSessionHandle handle, const QemuCallbacks& cb);

    // Returns the accelerator decision used (or would-be-used) for a session.
    AccelDecision accel(QemuSessionHandle handle) const;

    // PID of the worker thread that runs the QEMU main loop, or -1 before
    // start(). The worker is a std::thread of THIS process, so a value > 0
    // that equals the host PID is direct proof QEMU executes in-process
    // (there is no child process to compare against).
    int workerPid(QemuSessionHandle handle) const;

private:
    struct Impl;
    Impl* impl_;
};

} // namespace qemu
} // namespace vw
