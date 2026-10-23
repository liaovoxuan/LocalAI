/*
	MiniVmacInProcessCore.cpp

	Implementation of the in-process Mini vMac engine wrapper.

	Lifecycle notes that matter for correctness:

	  * The upstream core keeps machine state in process globals, so exactly one
	    instance may be live. A second create() returns MvStatus::Busy.
	  * start() runs the emulator loop on a std::thread; stop() sets the core's
	    ForceMacOff flag and joins. Both are idempotent, so repeated stop() and
	    destruction after stop() are safe.
	  * The frame callback is guarded so it fires exactly once per frame and never
	    after stop() has returned.
	  * No process is created anywhere in this file; there is no PID to manage.

	Copyright (C) 2026 VirtualWorld contributors
	Distributed under version 2 of the GNU General Public License; see
	third_party/minivmac/upstream/COPYING.txt.
*/

#include "MiniVmacInProcessCore.hpp"

#include <atomic>
#include <condition_variable>
#include <cstring>
#include <mutex>
#include <thread>

extern "C" {
#include "vw_minivmac_core.h"
}

namespace vw {

namespace {

/* The upstream core is single-instance: this flag records who owns it. */
std::atomic<bool> g_core_owned{false};

/* 单实例约束下，工作线程的每帧钩子需要找到当前实例。用 void* 保存，
   避免在文件作用域暴露私有嵌套类型。 */
std::atomic<void *> g_active_impl_raw{nullptr};

constexpr size_t kMaxDrives = 6;
constexpr uint32_t kMacScreenWidth = 512;
constexpr uint32_t kMacScreenHeight = 342;

} // namespace

const char *mvStatusName(MvStatus s)
{
    switch (s) {
    case MvStatus::Ok: return "ok";
    case MvStatus::BadArgument: return "bad-argument";
    case MvStatus::OutOfMemory: return "out-of-memory";
    case MvStatus::NotPrepared: return "not-prepared";
    case MvStatus::AlreadyRunning: return "already-running";
    case MvStatus::Unsupported: return "unsupported";
    case MvStatus::Busy: return "engine-busy";
    case MvStatus::NotRunning: return "not-running";
    case MvStatus::RomMissing: return "rom-missing";
    }
    return "unknown";
}

/*
	Lifecycle state machine.

	The previous implementation assigned to `worker` without protection, so a
	second start() while the finished worker was still joinable, and several
	stop() calls joining the same thread concurrently, both reached
	std::thread's "join failed: Invalid argument" path and terminated the
	process. This state machine makes every transition explicit and serialised:

	    Created --start()--> Starting --(thread created)--> Running
	    Running --stop()---> Stopping --(worker returns)--> Exited
	    Exited  --start()--> Starting (the finished thread is reaped first)

	Rules enforced here:
	  * a joinable thread is never overwritten -- it is joined before any new
	    thread is created;
	  * only one caller ever joins; the others wait on the same result;
	  * the worker records its exit code and cannot let an exception escape.
*/
struct MiniVmacInProcessCore::Impl {
    enum class State { Created, Starting, Running, Stopping, Exited };

    mutable std::mutex mtx;
    std::condition_variable cv;
    State state = State::Created;

    std::thread worker;
    /*
		`worker_active` 表示"已发起过一次运行且尚未回收"。它必须在线程对象创建
		之前、持锁时置位——否则 stop() 落在"线程已创建但 worker_joinable 尚未
		置位"的窗口里会提前返回，连停止请求都发不出去（这正是被独立验证证伪的
		缺陷：并发 start()||stop() 时 253/300 次 stop() 静默失效）。
	*/
    bool worker_active = false;     /* guarded by mtx */
    int last_exit_code = 0;
    bool stopping = false;

    std::atomic<bool> owns_core{false};
    std::atomic<bool> created{false};
    std::atomic<bool> rom_loaded{false};
    std::atomic<uint64_t> frames_seen{0};

    std::function<void(const MvFrame &)> frame_cb;
    std::mutex cb_mutex;

    std::vector<std::vector<uint8_t>> disks;

    ~Impl()
    {
        /* Order: stop/join first, then full engine teardown, then ownership. */
        stopAndJoin();
        vw_osglu_set_frame_hook(nullptr);
        g_active_impl_raw.store(nullptr);
        vw_core_dispose();
        if (owns_core.load()) {
            g_core_owned.store(false);
            owns_core.store(false);
        }
    }

    /* Join the worker exactly once. Safe repeatedly and from many threads. */
    void stopAndJoin()
    {
        std::thread to_join;
        {
            std::unique_lock<std::mutex> lock(mtx);
            if (!worker_active) {
                return;                 /* nothing was ever started */
            }
            stopping = true;
            /* Unconditional: a stop request must never be skipped, even if the
			   worker thread has not been scheduled yet. */
            vw_core_request_stop();
            /* The worker clears `state` to Exited as its final act. If it has
			   not started yet it will observe the request on its first tick and
			   exit immediately, so this wait always terminates. */
            cv.wait(lock, [this] { return state == State::Exited; });
            to_join = std::move(worker);
            worker_active = false;
        }
        if (to_join.joinable()) {
            to_join.join();
        }
    }

    /* Called by the worker as its very last action. */
    void noteWorkerFinished(int exit_code)
    {
        std::lock_guard<std::mutex> lock(mtx);
        last_exit_code = exit_code;
        state = State::Exited;
        cv.notify_all();
    }

    void runLoop()
    {
        int code = 0;
        try {
            code = vw_core_run();
        } catch (...) {
            /* Nothing may escape a thread entry point. */
            code = VW_ERR_RUN_FAILED;
        }
        {
            std::lock_guard<std::mutex> lock(mtx);
            state = State::Exited;
        }
        noteWorkerFinished(code);
    }

    /* Reap a finished worker. Caller must hold no lock. */
    void reapIfFinished()
    {
        std::thread to_join;
        {
            std::lock_guard<std::mutex> lock(mtx);
            if (!worker_active) {
                return;
            }
            /* Only a finished worker may be reaped here. */
            if (state != State::Exited) {
                return;
            }
            to_join = std::move(worker);
            worker_active = false;
        }
        if (to_join.joinable()) {
            to_join.join();
        }
    }

    /*
		工作线程每完成一帧调用一次（经引擎的 DoneWithDrawingForTick 钩子）。
		注意：完整帧像素目前没有受支持的获取点——上游的 Screen_OutputFrame 是
		header-only 全局，不改上游就无法从中取回整帧缓冲。因此这里如实投递一帧
		事件，`valid` 表示是否已发布像素，绝不假装有像素。
	*/
    void deliverFrame()
    {
        std::lock_guard<std::mutex> lock(cb_mutex);
        frames_delivered++;
        if (!frame_cb) {
            return;
        }
        unsigned rows = 0;
        unsigned char depth = 0;
        unsigned seq = 0;
        const unsigned char *data = vw_osglu_frame_data(&rows, &depth, &seq);
        MvFrame f;
        f.sequence = seq;
        f.depth = depth;
        f.height = rows ? rows : kMacScreenHeight;
        f.width = kMacScreenWidth;
        f.valid = (data != nullptr && seq != 0);
        if (f.valid) {
            const size_t rowBytes = (depth == 0) ? (f.width / 8) : (size_t)f.width;
            f.pixels.assign(data, data + (rowBytes * f.height));
        }
        frame_cb(f);
    }

    std::atomic<uint64_t> frames_delivered{0};

};

MiniVmacInProcessCore::MiniVmacInProcessCore() : impl_(new Impl()) {}

MiniVmacInProcessCore::~MiniVmacInProcessCore()
{
    if (impl_ != nullptr) {
        /* ~Impl() performs: stop/join -> vw_core_dispose() -> release ownership. */
        delete impl_;
        impl_ = nullptr;
    }
}

bool MiniVmacInProcessCore::coreInUse()
{
    return g_core_owned.load();
}

MvStatus MiniVmacInProcessCore::create()
{
    if (impl_ == nullptr) {
        return MvStatus::OutOfMemory;
    }
    if (impl_->created.load()) {
        return MvStatus::Ok; /* idempotent */
    }
    bool expected = false;
    if (!g_core_owned.compare_exchange_strong(expected, true)) {
        /* The upstream core keeps state in process globals; a second live
		   instance would corrupt the first. Report it honestly. */
        return MvStatus::Busy;
    }
    impl_->owns_core.store(true);

    const int rc = vw_core_prepare();
    if (rc != VW_OK) {
        g_core_owned.store(false);
        impl_->owns_core.store(false);
        return (rc == VW_ERR_OUT_OF_MEMORY) ? MvStatus::OutOfMemory : MvStatus::NotPrepared;
    }
    impl_->disks.assign(kMaxDrives, std::vector<uint8_t>());
    impl_->created.store(true);
    return MvStatus::Ok;
}

MvStatus MiniVmacInProcessCore::loadRom(const uint8_t *bytes, size_t size)
{
    if (impl_ == nullptr || !impl_->created.load()) {
        return MvStatus::NotPrepared;
    }
    if (bytes == nullptr || size == 0) {
        return MvStatus::BadArgument;
    }
    {
        std::lock_guard<std::mutex> lock(impl_->mtx);
        if (impl_->state == Impl::State::Starting || impl_->state == Impl::State::Running) {
            return MvStatus::AlreadyRunning; /* never swap the ROM under a live CPU */
        }
    }
    const unsigned long cap = vw_core_rom_size();
    /*
		Strict size: the engine accepts exactly the core's ROM size. A short
		image is refused rather than zero-padded, because padding would fabricate
		ROM contents the caller never supplied and would let an invalid image
		look acceptable.
	*/
    if (size != cap) {
        /* 调用方可用 romSize() 提前获知期望值；这里不做任何补零。 */
        return MvStatus::BadArgument;
    }
    uint8_t *rom = vw_core_rom_buffer();
    if (rom == nullptr) {
        return MvStatus::NotPrepared;
    }
    std::memcpy(rom, bytes, size);
    impl_->rom_loaded.store(true);
    return MvStatus::Ok;
}

MvStatus MiniVmacInProcessCore::attachDisk(unsigned drive, const uint8_t *bytes,
                                           size_t size, bool writable,
                                           const std::string &name)
{
    if (impl_ == nullptr || !impl_->created.load()) {
        return MvStatus::NotPrepared;
    }
    if (drive >= kMaxDrives || bytes == nullptr || size == 0) {
        return MvStatus::BadArgument;
    }
    const int rc = vw_osglu_attach_disk(drive, bytes, (unsigned long)size, nullptr,
                                        writable ? 1 : 0, name.empty() ? nullptr : name.c_str());
    if (rc != VW_OK) {
        return (rc == VW_ERR_OUT_OF_MEMORY) ? MvStatus::OutOfMemory : MvStatus::BadArgument;
    }
    impl_->disks[drive].assign(bytes, bytes + size);
    return MvStatus::Ok;
}

MvStatus MiniVmacInProcessCore::detachDisk(unsigned drive)
{
    if (impl_ == nullptr || !impl_->created.load()) {
        return MvStatus::NotPrepared;
    }
    if (drive >= kMaxDrives) {
        return MvStatus::BadArgument;
    }
    const int rc = vw_osglu_attach_disk(drive, nullptr, 0, nullptr, 0, nullptr);
    if (rc != VW_OK) {
        return MvStatus::BadArgument;
    }
    impl_->disks[drive].clear();
    return MvStatus::Ok;
}

MvStatus MiniVmacInProcessCore::start()
{
    if (impl_ == nullptr || !impl_->created.load()) {
        return MvStatus::NotPrepared;
    }
    if (!impl_->rom_loaded.load()) {
        return MvStatus::RomMissing;
    }
    /* Reap a worker that finished on its own before we try to replace it. */
    impl_->reapIfFinished();

    std::unique_lock<std::mutex> lock(impl_->mtx);
    using State = Impl::State;
    if (impl_->state == State::Starting || impl_->state == State::Running) {
        return MvStatus::AlreadyRunning;
    }
    if (impl_->worker_active) {
        return MvStatus::AlreadyRunning;   /* never overwrite a live/reapable run */
    }
    /*
		Enter Starting and mark the run active *while holding the lock*, before
		the thread object exists. This closes the window in which a concurrent
		stop() would observe "no active run" and return without requesting a
		stop -- the defect the independent verifier falsified.
	*/
    impl_->state = State::Starting;
    impl_->worker_active = true;
    impl_->stopping = false;
    vw_core_clear_stop();
    lock.unlock();

    /* 注册每帧钩子。lambda 无捕获，可退化为函数指针；其体的访问权按定义点
	   （成员函数内）判定，因此可以转换回私有嵌套类型。 */
    g_active_impl_raw.store(static_cast<void *>(impl_));
    vw_osglu_set_frame_hook(+[]() {
        void *raw = g_active_impl_raw.load();
        if (raw == nullptr) {
            return;
        }
        static_cast<MiniVmacInProcessCore::Impl *>(raw)->deliverFrame();
    });

    try {
        impl_->worker = std::thread([this]() { impl_->runLoop(); });
    } catch (...) {
        /* Report the failure and leave a consistent terminal state. */
        std::lock_guard<std::mutex> guard(impl_->mtx);
        impl_->worker_active = false;
        impl_->state = State::Exited;
        impl_->cv.notify_all();
        return MvStatus::OutOfMemory;
    }

    {
        std::lock_guard<std::mutex> guard(impl_->mtx);
        if (impl_->state == State::Starting) {
            impl_->state = State::Running;
        }
    }
    return MvStatus::Ok;
}

MvStatus MiniVmacInProcessCore::stop()
{
    if (impl_ == nullptr) {
        return MvStatus::NotPrepared;
    }
    /*
		Repeated and concurrent stop() are both safe: stopAndJoin() lets exactly
		one caller own the thread object and join it, while every other caller
		waits on the same condition and then observes the same result.
	*/
    impl_->stopAndJoin();
    return MvStatus::Ok;
}

bool MiniVmacInProcessCore::isRunning() const
{
    if (impl_ == nullptr) {
        return false;
    }
    std::lock_guard<std::mutex> lock(impl_->mtx);
    return impl_->state == Impl::State::Starting || impl_->state == Impl::State::Running;
}

MvStatus MiniVmacInProcessCore::setKey(uint8_t macKeyCode, bool down)
{
    if (impl_ == nullptr || !impl_->created.load()) {
        return MvStatus::NotPrepared;
    }
    const int rc = vw_core_post_key(macKeyCode, down ? 1 : 0);
    return (rc == VW_OK) ? MvStatus::Ok : MvStatus::Unsupported;
}

MvStatus MiniVmacInProcessCore::setMouse(int32_t x, int32_t y)
{
    if (impl_ == nullptr || !impl_->created.load()) {
        return MvStatus::NotPrepared;
    }
    const int rc = vw_core_post_mouse(x, y);
    return (rc == VW_OK) ? MvStatus::Ok : MvStatus::Unsupported;
}

MvStatus MiniVmacInProcessCore::setMouseButton(bool down)
{
    if (impl_ == nullptr || !impl_->created.load()) {
        return MvStatus::NotPrepared;
    }
    /* Mac 128K/512Ke/Plus 只有一个鼠标按钮。 */
    const int rc = vw_core_post_mouse_button(0, down ? 1 : 0);
    return (rc == VW_OK) ? MvStatus::Ok : MvStatus::BadArgument;
}

MvStatus MiniVmacInProcessCore::setMouseWheel(int32_t dx, int32_t dy)
{
    if (impl_ == nullptr || !impl_->created.load()) {
        return MvStatus::NotPrepared;
    }
    /*
		上游核心没有滚轮概念；Mini vMac 平台层的既有约定是把滚轮方向映射为
		方向键脉冲（见 OSGLUSDL.c 的 SDL_MOUSEWHEEL 分支），这里沿用同一约定。
	*/
    const int rc = vw_core_post_mouse_wheel(dx, dy);
    return (rc == VW_OK) ? MvStatus::Ok : MvStatus::BadArgument;
}

bool MiniVmacInProcessCore::latestFrame(MvFrame *out) const
{
    if (impl_ == nullptr || out == nullptr) {
        return false;
    }
    unsigned rows = 0;
    unsigned char depth = 0;
    unsigned seq = 0;
    const unsigned char *data = vw_osglu_frame_data(&rows, &depth, &seq);
    if (data == nullptr || seq == 0) {
        return false;
    }
    out->height = rows ? rows : kMacScreenHeight;
    out->width = kMacScreenWidth;
    out->depth = depth;
    out->sequence = seq;
    out->valid = true;
    const size_t rowBytes = (depth == 0) ? (out->width / 8) : (size_t)out->width;
    out->pixels.assign(data, data + (rowBytes * out->height));
    return true;
}

size_t MiniVmacInProcessCore::romSize()
{
    return (size_t)vw_core_rom_size();
}

size_t MiniVmacInProcessCore::romSizeForThisBuild() const
{
    return (size_t)vw_core_rom_size();
}

uint64_t MiniVmacInProcessCore::framesDelivered() const
{
    return impl_ ? impl_->frames_delivered.load() : 0;
}

MvStats MiniVmacInProcessCore::stats() const
{
    MvStats s;
    s.frames = (uint64_t)vw_core_frame_count();
    s.ticks = (uint64_t)vw_core_tick_count();
    s.audio_blocks = (uint64_t)vw_osglu_audio_blocks();
    s.unsupported_disk_warnings = (uint64_t)vw_osglu_unsupported_disk_warnings();
    return s;
}

void MiniVmacInProcessCore::setFrameCallback(std::function<void(const MvFrame &)> cb)
{
    if (impl_ == nullptr) {
        return;
    }
    std::lock_guard<std::mutex> lock(impl_->cb_mutex);
    impl_->frame_cb = std::move(cb);
}

} // namespace vw
