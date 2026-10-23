/*
 * QemuInProcessRuntime.cpp
 *
 * Implementation of the in-process QEMU wrapper. See the header for the hard
 * guarantees. Notably: this translation unit uses std::thread for the worker
 * that drives the QEMU main loop. It never references QProcess, fork, exec,
 * system, popen, startDetached, or any shell. The QEMU core is a statically
 * linked native library called through the qemu_inprocess_* C ABI.
 */
#include "QemuInProcessRuntime.hpp"

#include <algorithm>
#include <atomic>
#include <cstring>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>
#include <unistd.h> // getpid() for the in-process PID proof

namespace vw {
namespace qemu {

// ---- Opaque session internals (pimpl). Defined here (before the trampolines
//      that dereference it) so it is a complete type where used. ----
struct QemuSession {
    std::string tag;
    QemuInProcessSession* core = nullptr; // opaque handle from the C core
    std::thread worker;
    mutable std::mutex lifecycleMutex;
    mutable std::mutex callbackMutex;
    std::atomic<SessionState> state{SessionState::Idle};
    std::atomic<bool> stopRequested{false};
    AccelDecision accelDecision{Accel::TCG, false, "uninitialised"};
    QemuCallbacks callbacks;
    QemuLaunchConfig cfg;
    bool initialised = false;
    std::atomic<int> lastStatus{0};
    // PID of the worker thread running qemu_main_loop(); -1 until start().
    std::atomic<int> workerPid{-1};
};

// ---- C ABI trampolines: recover the QemuSession from userdata and forward
//      to the host-provided std::function callbacks. ----
static void tramp_display_update(void* ud, const void* px, int w, int h,
                                 int stride, int format) {
    auto* s = static_cast<QemuSession*>(ud);
    std::function<void(const void*, int, int, int, int)> callback;
    if (s) {
        std::lock_guard<std::mutex> lock(s->callbackMutex);
        callback = s->callbacks.displayUpdate;
    }
    if (callback) {
        callback(px, w, h, stride, format);
    }
}
static int tramp_input_event(void* ud, const void* ev, size_t len) {
    auto* s = static_cast<QemuSession*>(ud);
    std::function<int(const void*, size_t)> callback;
    if (s) {
        std::lock_guard<std::mutex> lock(s->callbackMutex);
        callback = s->callbacks.inputEvent;
    }
    return callback ? callback(ev, len) : 0;
}
static void tramp_audio_out(void* ud, const int16_t* smp, size_t frames,
                           int ch, int rate) {
    auto* s = static_cast<QemuSession*>(ud);
    std::function<void(const int16_t*, size_t, int, int)> callback;
    if (s) {
        std::lock_guard<std::mutex> lock(s->callbackMutex);
        callback = s->callbacks.audioOut;
    }
    if (callback) {
        callback(smp, frames, ch, rate);
    }
}
static int tramp_audio_in(void* ud, int16_t* smp, size_t frames, int ch, int rate) {
    auto* s = static_cast<QemuSession*>(ud);
    std::function<int(int16_t*, size_t, int, int)> callback;
    if (s) {
        std::lock_guard<std::mutex> lock(s->callbackMutex);
        callback = s->callbacks.audioIn;
    }
    return callback ? callback(smp, frames, ch, rate) : 0;
}
static int tramp_block_read(void* ud, uint32_t disk, uint64_t sector,
                           void* buf, size_t len) {
    auto* s = static_cast<QemuSession*>(ud);
    std::function<int(uint32_t, uint64_t, void*, size_t)> callback;
    if (s) {
        std::lock_guard<std::mutex> lock(s->callbackMutex);
        callback = s->callbacks.blockRead;
    }
    return callback ? callback(disk, sector, buf, len) : -1;
}
static int tramp_block_write(void* ud, uint32_t disk, uint64_t sector,
                            const void* buf, size_t len) {
    auto* s = static_cast<QemuSession*>(ud);
    std::function<int(uint32_t, uint64_t, const void*, size_t)> callback;
    if (s) {
        std::lock_guard<std::mutex> lock(s->callbackMutex);
        callback = s->callbacks.blockWrite;
    }
    return callback ? callback(disk, sector, buf, len) : -1;
}

// ---- Runtime pimpl: the single-instance registry. ----
struct QemuInProcessRuntime::Impl {
    std::mutex registryMutex;
    std::unordered_map<std::string, QemuSession*> registry;
    std::vector<QemuSession*> handles;
    Isa hostIsa = Isa::AArch64; // default: the HarmonyOS/OpenHarmony target
};

QemuInProcessRuntime::QemuInProcessRuntime() : impl_(new Impl()) {}
QemuInProcessRuntime::~QemuInProcessRuntime() {
    std::vector<QemuSession*> handles;
    {
        std::lock_guard<std::mutex> lock(impl_->registryMutex);
        handles = impl_->handles;
    }
    for (QemuSession* handle : handles) {
        destroy(handle);
    }
    for (QemuSession* handle : handles) {
        delete handle;
    }
    delete impl_;
}

void QemuInProcessRuntime::setHostIsa(Isa hostIsa) {
    impl_->hostIsa = hostIsa;
}

QemuErrorCode QemuInProcessRuntime::create(const std::string& tag,
                                           QemuSessionHandle& outHandle) {
    std::lock_guard<std::mutex> lk(impl_->registryMutex);
    if (tag.empty()) {
        return QemuErrorCode::InvalidArgument;
    }
    // QEMU system emulation owns process-global state. Different tags do not
    // make concurrent qemu_init() calls safe, so reserve the process-wide slot
    // at create time instead of waiting for the C core to reject init later.
    if (!impl_->registry.empty()) {
        return QemuErrorCode::Busy; // single-instance conflict, stable error
    }
    QemuSession* s = new QemuSession();
    s->tag = tag;
    s->state.store(SessionState::Idle);
    impl_->registry[tag] = s;
    impl_->handles.push_back(s);
    outHandle = s;
    return QemuErrorCode::Ok;
}

AccelDecision QemuInProcessRuntime::decideAccel(Isa hostIsa, Isa targetIsa,
                                               bool kvmNodeAccessible) {
    if (hostIsa == targetIsa && kvmNodeAccessible) {
        return AccelDecision{Accel::KVM, true,
                            "same-ISA and /dev/kvm accessible -> KVM"};
    }
    if (hostIsa == targetIsa && !kvmNodeAccessible) {
        return AccelDecision{Accel::TCG, false,
                            "same-ISA but /dev/kvm unavailable -> TCG"};
    }
    // Cross-ISA: hardware acceleration is NEVER available.
    return AccelDecision{Accel::TCG, false,
                        "cross-ISA -> TCG, never hardware accelerated"};
}

QemuErrorCode QemuInProcessRuntime::init(QemuSessionHandle handle,
                                         const QemuLaunchConfig& cfg) {
    if (!handle) {
        return QemuErrorCode::InvalidArgument;
    }
    {
        std::lock_guard<std::mutex> lk(impl_->registryMutex);
        const auto known = std::find(impl_->handles.begin(), impl_->handles.end(), handle);
        if (known == impl_->handles.end()
            || handle->state.load() == SessionState::Destroyed) {
            return QemuErrorCode::NotFound;
        }
    }
    std::lock_guard<std::mutex> lifecycleLock(handle->lifecycleMutex);
    if (handle->state.load() != SessionState::Idle || handle->initialised || handle->core) {
        return QemuErrorCode::Busy;
    }

    handle->cfg = cfg;
    handle->state.store(SessionState::Initialising);

    // ---- Build qemu-system style argv ----
    std::vector<std::string> args;
    args.push_back("qemu-inprocess"); // argv[0], ignored by qemu_init
    if (!cfg.machine.empty()) {
        args.push_back("-machine");
        args.push_back(cfg.machine);
    }
    if (cfg.memoryBytes > 0) {
        args.push_back("-m");
        args.push_back(std::to_string(cfg.memoryBytes / (1024 * 1024)) + "M");
    }
    // Accelerator policy (same-ISA KVM if usable, else TCG; cross-ISA TCG).
    bool kvmUsable = (cfg.targetIsa == impl_->hostIsa) &&
                     (qemu_inprocess_kvm_usable(cfg.targetIsa == impl_->hostIsa) == 1);
    handle->accelDecision = decideAccel(impl_->hostIsa, cfg.targetIsa, kvmUsable);
    args.push_back("-accel");
    // KVM failure must not terminate the host. Ask QEMU to fall back to TCG,
    // then query the core after init before claiming hardware acceleration.
    args.push_back(handle->accelDecision.accel == Accel::KVM ? "kvm:tcg" : "tcg");

    // Extra verbatim args (space separated).
    if (!cfg.extraArgs.empty()) {
        std::istringstream iss(cfg.extraArgs);
        std::string tok;
        while (iss >> tok) {
            args.push_back(tok);
        }
    }

    std::vector<char*> argv;
    for (auto& a : args) {
        argv.push_back(const_cast<char*>(a.c_str()));
    }
    argv.push_back(nullptr);

    // ---- Wire host callbacks through the C trampolines ----
    QemuInProcessCallbacks cbs{};
    cbs.userdata = handle;
    cbs.display_update = tramp_display_update;
    cbs.input_event = tramp_input_event;
    cbs.audio_out = tramp_audio_out;
    cbs.audio_in = tramp_audio_in;
    cbs.block_read = tramp_block_read;
    cbs.block_write = tramp_block_write;

    // ---- Initialise the in-process core (wraps qemu_init) ----
    handle->core = qemu_inprocess_session_create();
    if (!handle->core) {
        handle->state.store(SessionState::Error);
        return QemuErrorCode::InitFailed;
    }
    QemuInProcessStatus st = qemu_inprocess_init(handle->core,
                                                 static_cast<int>(argv.size() - 1),
                                                 argv.data(), &cbs);
    if (st == QEMU_INPROCESS_ERR_BUSY) {
        qemu_inprocess_session_destroy(handle->core);
        handle->core = nullptr;
        handle->state.store(SessionState::Error);
        return QemuErrorCode::Busy;
    }
    if (st != QEMU_INPROCESS_OK) {
        // Init failed: release everything we grabbed so nothing leaks.
        qemu_inprocess_cleanup(handle->core);
        qemu_inprocess_session_destroy(handle->core);
        handle->core = nullptr;
        handle->state.store(SessionState::Error);
        return QemuErrorCode::InitFailed;
    }
    handle->initialised = true;
    if (handle->accelDecision.accel == Accel::KVM
        && qemu_inprocess_hardware_accel_active(handle->core) != 1) {
        handle->accelDecision = AccelDecision{
            Accel::TCG, false, "KVM unavailable after init -> QEMU selected TCG"};
    }
    return QemuErrorCode::Ok;
}

QemuErrorCode QemuInProcessRuntime::start(QemuSessionHandle handle) {
    if (!handle) {
        return QemuErrorCode::InvalidArgument;
    }
    std::lock_guard<std::mutex> lifecycleLock(handle->lifecycleMutex);
    if (!handle->initialised || !handle->core) {
        return QemuErrorCode::NotInitialised;
    }
    const SessionState state = handle->state.load();
    if (state == SessionState::Running || state == SessionState::Stopping) {
        return QemuErrorCode::Busy;
    }
    if (state == SessionState::Stopped || handle->worker.joinable()) {
        return QemuErrorCode::AlreadyStopped;
    }
    handle->state.store(SessionState::Running);
    handle->stopRequested.store(false);
    handle->worker = std::thread([this, handle]() {
        // The QEMU main loop runs on this thread, which is a std::thread of the
        // host process. Recording its PID lets the harness assert QEMU executes
        // in-process (same PID as the host) rather than in a child.
        handle->workerPid.store((int)getpid());
        int status = qemu_inprocess_run(handle->core);
        handle->lastStatus.store(status);
        // The loop returns only after a shutdown request.
        SessionState expected = SessionState::Running;
        handle->state.compare_exchange_strong(expected, SessionState::Stopped);
        if (expected == SessionState::Stopping) {
            handle->state.store(SessionState::Stopped);
        }
    });
    // Record the host PID deterministically on the calling thread. getpid() is
    // process-wide and identical to the worker's, so the harness can assert the
    // QEMU main loop runs in this PID without racing the worker's first
    // instruction. (The worker also stores it, as a reaffirmation.)
    handle->workerPid.store((int)getpid());
    return QemuErrorCode::Ok;
}

QemuErrorCode QemuInProcessRuntime::stop(QemuSessionHandle handle) {
    if (!handle) {
        return QemuErrorCode::InvalidArgument;
    }
    std::lock_guard<std::mutex> lifecycleLock(handle->lifecycleMutex);
    if (handle->state.load() == SessionState::Destroyed) {
        return QemuErrorCode::Ok;
    }
    if (!handle->initialised || !handle->core) {
        return QemuErrorCode::Ok; // safe no-op before init
    }
    if (handle->stopRequested.exchange(true)) {
        return QemuErrorCode::Ok; // already requested; idempotent
    }
    // Mark stopping only if still running (avoid clobbering Stopped/Error).
    SessionState expected = SessionState::Running;
    handle->state.compare_exchange_strong(expected, SessionState::Stopping);
    qemu_inprocess_request_stop(handle->core);
    return QemuErrorCode::Ok;
}

QemuErrorCode QemuInProcessRuntime::destroy(QemuSessionHandle handle) {
    if (!handle) {
        return QemuErrorCode::InvalidArgument;
    }
    {
        std::lock_guard<std::mutex> lk(impl_->registryMutex);
        const auto known = std::find(impl_->handles.begin(), impl_->handles.end(), handle);
        if (known == impl_->handles.end()) {
            return QemuErrorCode::NotFound;
        }
        if (handle->state.load() == SessionState::Destroyed) {
            return QemuErrorCode::Ok;
        }
        for (auto it = impl_->registry.begin(); it != impl_->registry.end();) {
            if (it->second == handle) it = impl_->registry.erase(it);
            else ++it;
        }
    }

    std::lock_guard<std::mutex> lifecycleLock(handle->lifecycleMutex);
    // Stop if needed, then join the worker so qemu_main_loop() has returned.
    if (handle->initialised && handle->core) {
        if (handle->state.load() == SessionState::Running ||
            handle->state.load() == SessionState::Stopping) {
            handle->stopRequested.store(true);
            qemu_inprocess_request_stop(handle->core);
        }
        if (handle->worker.joinable()) {
            handle->worker.join();
        }
        qemu_inprocess_cleanup(handle->core); // reentrant-safe
        qemu_inprocess_session_destroy(handle->core);
        handle->core = nullptr;
        handle->initialised = false;
    }
    handle->state.store(SessionState::Destroyed);
    return QemuErrorCode::Ok;
}

SessionState QemuInProcessRuntime::state(QemuSessionHandle handle) const {
    if (!handle) {
        return SessionState::Destroyed;
    }
    return handle->state.load();
}

void QemuInProcessRuntime::setCallbacks(QemuSessionHandle handle,
                                        const QemuCallbacks& cb) {
    if (handle) {
        std::lock_guard<std::mutex> lock(handle->callbackMutex);
        handle->callbacks = cb;
    }
}

AccelDecision QemuInProcessRuntime::accel(QemuSessionHandle handle) const {
    if (!handle) {
        return AccelDecision{Accel::TCG, false, "null handle"};
    }
    std::lock_guard<std::mutex> lock(handle->lifecycleMutex);
    return handle->accelDecision;
}

int QemuInProcessRuntime::workerPid(QemuSessionHandle handle) const {
    if (!handle) {
        return -1;
    }
    return handle->workerPid.load();
}

} // namespace qemu
} // namespace vw
