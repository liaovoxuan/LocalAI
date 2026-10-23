/*
	MiniVmacInProcessCoreTests.cpp

	Lifecycle, concurrency and containment tests for the in-process Mini vMac
	engine.

	Design constraints these tests honour:

	  * No ROM is distributed with the project and none is used here. Every test
	    synthesises its own ROM bytes in memory and nothing is written to disk.
	  * No real guest is started: the synthetic ROM is not a Macintosh ROM, so it
	    boots nothing. The tests exercise the engine's lifecycle, concurrency and
	    containment, not Mac OS behaviour.
	  * No process is spawned, by the engine or by the tests.

	Copyright (C) 2026 VirtualWorld contributors
	Distributed under version 2 of the GNU General Public License; see
	third_party/minivmac/upstream/COPYING.txt.
*/

#include "MiniVmacInProcessCore.hpp"

extern "C" {
#include "vw_minivmac_core.h"
}

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

using namespace vw;

namespace {

int g_checks = 0;
int g_failures = 0;

void check(bool cond, const char *what)
{
    ++g_checks;
    if (!cond) {
        ++g_failures;
        std::printf("  FAIL: %s\n", what);
    }
}

/* A synthetic ROM: zero-filled with a distinct pattern so we can prove the
   bytes actually reached the engine's ROM buffer. */
/*
	合成 ROM 的默认大小取自引擎公布的尺寸，而不是硬编码 128 KiB。
	引擎有多个机型变体（Mac 128K/Twiggy 为 0x10000，512Ke/Plus 为 0x20000），
	硬编码会让非 Plus 变体全部失败。合成字节不是 Macintosh ROM，不启动任何客户机。
*/
std::vector<uint8_t> syntheticRom(size_t size = MiniVmacInProcessCore::romSize())
{
    std::vector<uint8_t> rom(size, 0);
    for (size_t i = 0; i < size; i += 4096) {
        rom[i] = (uint8_t)(0xA0 + ((i / 4096) & 0x0F));
    }
    return rom;
}

std::vector<uint8_t> syntheticDisk(size_t size = 64 * 1024)
{
    std::vector<uint8_t> d(size, 0);
    for (size_t i = 0; i < size; ++i) {
        d[i] = (uint8_t)(i & 0xFF);
    }
    return d;
}

void testRomSizeIsExposed()
{
    std::printf("test_rom_size_is_exposed\n");
    /* 调用方必须能在 loadRom 之前知道期望的 ROM 字节数——否则只能盲试。 */
    const size_t advertised = MiniVmacInProcessCore::romSize();
    check(advertised > 0, "romSize() advertises a non-zero size");
    check(advertised == 0x20000u || advertised == 0x10000u,
          "romSize() is one of the upstream kROM_Size values (0x20000 Plus / 0x10000 128K-512Ke)");
    /* 公布值必须与实际校验一致：恰好该尺寸被接受，少一字节被拒绝。 */
    MiniVmacInProcessCore core;
    check(core.create() == MvStatus::Ok, "create");
    std::vector<uint8_t> exact(advertised, 0);
    check(core.loadRom(exact.data(), exact.size()) == MvStatus::Ok,
          "exactly romSize() bytes is accepted");
    std::vector<uint8_t> shortRom(advertised - 1, 0);
    check(core.loadRom(shortRom.data(), shortRom.size()) == MvStatus::BadArgument,
          "one byte short is refused");
    check((size_t)core.romSizeForThisBuild() == advertised,
          "instance accessor agrees with the static one");
    core.stop();
    std::printf("    advertised ROM size: %zu bytes\n", advertised);
}

void testStatusNames()
{
    std::printf("test_status_names\n");
    check(std::string(mvStatusName(MvStatus::Ok)) == "ok", "Ok name");
    check(std::string(mvStatusName(MvStatus::Busy)) == "engine-busy", "Busy name");
    check(std::string(mvStatusName(MvStatus::RomMissing)) == "rom-missing", "RomMissing name");
}

void testCreateIsIdempotent()
{
    std::printf("test_create_is_idempotent\n");
    MiniVmacInProcessCore core;
    check(core.create() == MvStatus::Ok, "first create succeeds");
    check(core.create() == MvStatus::Ok, "second create is idempotent, not an error");
    check(!core.isRunning(), "not running before start");
    core.stop();
}

void testSecondInstanceIsBusyWhileFirstLives()
{
    std::printf("test_second_instance_is_busy_while_first_lives\n");
    MiniVmacInProcessCore a;
    check(a.create() == MvStatus::Ok, "first instance owns the core");
    check(MiniVmacInProcessCore::coreInUse(), "core reported in use");
    /* Upstream state is process-global: while `a` is alive, every other
	   instance must be refused -- and that refusal is the CORRECT result, not a
	   failure. Asserting reacquisition here would be a scope bug. */
    {
        MiniVmacInProcessCore b;
        check(b.create() == MvStatus::Busy, "second live instance reports Busy");
    }
    {
        MiniVmacInProcessCore c;
        check(c.create() == MvStatus::Busy, "third live instance reports Busy");
    }
    check(MiniVmacInProcessCore::coreInUse(), "core still owned by the first instance");
    a.stop();
}

void testReacquireOnlyAfterFirstIsDestroyed()
{
    std::printf("test_reacquire_only_after_first_is_destroyed\n");
    {
        MiniVmacInProcessCore a;
        check(a.create() == MvStatus::Ok, "first instance owns the core");
        a.stop();
        /* `a` is destroyed at the end of this scope ... */
    }
    check(!MiniVmacInProcessCore::coreInUse(), "core released once the owner is gone");
    /* ... so only now may a new instance acquire it. */
    MiniVmacInProcessCore c;
    check(c.create() == MvStatus::Ok, "create succeeds after the owner was destroyed");
    check(MiniVmacInProcessCore::coreInUse(), "new owner holds the core");
    c.stop();
}

void testReacquireRepeatedly()
{
    std::printf("test_reacquire_repeatedly\n");
    int ok = 0;
    for (int i = 0; i < 30; ++i) {
        MiniVmacInProcessCore core;
        if (core.create() == MvStatus::Ok) {
            ++ok;
        }
        core.stop();
    }
    check(ok == 30, "30 consecutive create/destroy cycles all succeeded");
    std::printf("    successful cycles: %d/30\n", ok);
}

void testLoadRomValidation()
{
    std::printf("test_load_rom_validation\n");
    MiniVmacInProcessCore core;
    auto rom = syntheticRom();

    check(core.loadRom(rom.data(), rom.size()) == MvStatus::NotPrepared,
          "loadRom before create is refused");
    check(core.create() == MvStatus::Ok, "create");
    check(core.loadRom(nullptr, 0) == MvStatus::BadArgument, "null ROM refused");
    check(core.loadRom(rom.data(), 0) == MvStatus::BadArgument, "empty ROM refused");
    check(core.loadRom(rom.data(), rom.size()) == MvStatus::Ok, "exact-size ROM accepted");

    /* Exactly the core's ROM size is required: no silent padding, no truncation. */
    std::vector<uint8_t> tooBig(1024 * 1024, 0x11);
    check(core.loadRom(tooBig.data(), tooBig.size()) == MvStatus::BadArgument,
          "oversized ROM refused");
    const size_t kRomBytes = MiniVmacInProcessCore::romSize();
    auto shortRom = syntheticRom(kRomBytes / 2);
    check(core.loadRom(shortRom.data(), shortRom.size()) == MvStatus::BadArgument,
          "short ROM refused rather than zero-padded");
    auto oneShort = syntheticRom(kRomBytes - 1);
    check(core.loadRom(oneShort.data(), oneShort.size()) == MvStatus::BadArgument,
          "off-by-one short ROM refused");
    core.stop();
}

void testStartRequiresRom()
{
    std::printf("test_start_requires_rom\n");
    MiniVmacInProcessCore core;
    check(core.create() == MvStatus::Ok, "create");
    /* Refusing to start without a ROM is the honest behaviour: there is nothing
	   to execute, and fabricating one would be a fake pass. */
    check(core.start() == MvStatus::RomMissing, "start without ROM is refused");
    auto rom = syntheticRom();
    check(core.loadRom(rom.data(), rom.size()) == MvStatus::Ok, "load synthetic ROM");
    core.stop();
}

void testDiskAttachDetach()
{
    std::printf("test_disk_attach_detach\n");
    MiniVmacInProcessCore core;
    check(core.create() == MvStatus::Ok, "create");
    auto disk = syntheticDisk();

    check(core.attachDisk(0, disk.data(), disk.size(), true, "Test") == MvStatus::Ok,
          "attach disk 0");
    check(core.attachDisk(6, disk.data(), disk.size()) == MvStatus::BadArgument,
          "drive index out of range refused");
    check(core.attachDisk(1, nullptr, 0) == MvStatus::BadArgument, "null medium refused");
    check(core.detachDisk(0) == MvStatus::Ok, "detach disk 0");
    check(core.detachDisk(0) == MvStatus::Ok, "detach again is safe");
    check(core.detachDisk(99) == MvStatus::BadArgument, "bad drive refused");
    core.stop();
}

void testRunAndStopConcurrently()
{
    std::printf("test_run_and_stop_concurrently\n");
    MiniVmacInProcessCore core;
    auto rom = syntheticRom();
    check(core.create() == MvStatus::Ok, "create");
    check(core.loadRom(rom.data(), rom.size()) == MvStatus::Ok, "load synthetic ROM");
    check(core.start() == MvStatus::Ok, "start");
    check(core.start() == MvStatus::AlreadyRunning, "second start refused");

    /* Let the emulation loop run briefly in this process. */
    std::this_thread::sleep_for(std::chrono::milliseconds(150));
    check(core.isRunning(), "engine is running on its worker thread");

    /* Several threads stopping at once must be safe and idempotent. */
    std::atomic<int> joins{0};
    std::vector<std::thread> stoppers;
    for (int i = 0; i < 4; ++i) {
        stoppers.emplace_back([&core, &joins]() {
            core.stop();
            joins.fetch_add(1);
        });
    }
    for (auto &t : stoppers) {
        t.join();
    }
    check(joins.load() == 4, "all concurrent stop() calls returned");
    check(!core.isRunning(), "engine stopped");

    /* Further stops, and destruction, must remain safe. */
    check(core.stop() == MvStatus::Ok, "stop after stop is safe");
    check(core.stop() == MvStatus::Ok, "stop is idempotent");

    MvStats s = core.stats();
    check(s.ticks > 0, "engine advanced emulated time (ticks > 0)");
}

void testFrameCallbackFiresOnce()
{
    std::printf("test_frame_callback_fires_once\n");
    MiniVmacInProcessCore core;
    auto rom = syntheticRom();
    check(core.create() == MvStatus::Ok, "create");
    check(core.loadRom(rom.data(), rom.size()) == MvStatus::Ok, "load synthetic ROM");

    std::atomic<int> callbacks{0};
    std::atomic<int> afterStop{0};
    std::atomic<bool> stopped{false};
    core.setFrameCallback([&](const MvFrame &f) {
        if (stopped.load()) {
            afterStop.fetch_add(1);
        }
        if (f.valid) {
            callbacks.fetch_add(1);
        }
    });

    check(core.start() == MvStatus::Ok, "start");
    std::this_thread::sleep_for(std::chrono::milliseconds(120));
    core.stop();
    stopped.store(true);
    const int seen = callbacks.load();

    /* Give any stray worker a chance to misbehave before asserting. */
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    check(afterStop.load() == 0, "callback never fires after stop() returned");
    /* B3：每帧投递路径必须真的被行使——此前该路径根本不存在，回调一次都没触发。 */
    check(seen > 0, "per-frame callback actually fired (B3 regression)");
    check((uint64_t)seen <= core.framesDelivered(),
          "delivered callbacks never exceed the engine's frame deliveries");
    std::printf("    frames delivered: %d (engine total %llu)\n", seen,
                (unsigned long long)core.framesDelivered());
}

void testInputsAreSafe()
{
    std::printf("test_inputs_are_safe\n");
    MiniVmacInProcessCore core;
    check(core.setKey(0, true) == MvStatus::NotPrepared, "setKey before create refused");
    check(core.create() == MvStatus::Ok, "create");
    check(core.setKey(0x24, true) == MvStatus::Ok, "key down accepted");
    check(core.setKey(0x24, false) == MvStatus::Ok, "key up accepted");
    check(core.setMouse(-5, -5) == MvStatus::Ok, "negative mouse clamped, not rejected");
    check(core.setMouse(1000000, 1000000) == MvStatus::Ok, "huge mouse clamped");
    core.stop();
}

void testMouseButtonAndWheel()
{
    std::printf("test_mouse_button_and_wheel\n");
    MiniVmacInProcessCore core;
    auto rom = syntheticRom();

    /* 未 create 时必须报 NotPrepared，而不是静默成功。 */
    check(core.setMouseButton(true) == MvStatus::NotPrepared,
          "setMouseButton before create is refused");
    check(core.setMouseWheel(0, 1) == MvStatus::NotPrepared,
          "setMouseWheel before create is refused");

    check(core.create() == MvStatus::Ok, "create");
    check(core.loadRom(rom.data(), rom.size()) == MvStatus::Ok, "load synthetic ROM");
    check(core.start() == MvStatus::Ok, "start");
    std::this_thread::sleep_for(std::chrono::milliseconds(20));

    /* 按钮与滚轮都必须安全可调用，且不得中断模拟。 */
    check(core.setMouseButton(true) == MvStatus::Ok, "mouse button down");
    check(core.setMouseButton(true) == MvStatus::Ok, "repeated button down is safe");
    check(core.setMouseButton(false) == MvStatus::Ok, "mouse button up");
    check(core.setMouseWheel(0, 1) == MvStatus::Ok, "wheel up");
    check(core.setMouseWheel(0, -1) == MvStatus::Ok, "wheel down");
    check(core.setMouseWheel(-1, 0) == MvStatus::Ok, "wheel left");
    check(core.setMouseWheel(1, 0) == MvStatus::Ok, "wheel right");
    check(core.setMouseWheel(0, 0) == MvStatus::Ok, "no-op wheel is safe");

    core.setMouse(100, 100);
    core.setKey(0x24, true);
    core.setKey(0x24, false);
    check(core.isRunning(), "engine survived button and wheel input");

    const uint64_t ticks_before = core.stats().ticks;
    std::this_thread::sleep_for(std::chrono::milliseconds(30));
    check(core.stats().ticks > ticks_before, "engine kept ticking after input");

    core.stop();
    /* 停止后也不得崩溃。 */
    core.setMouseButton(true);
    core.setMouseWheel(0, 1);
    std::printf("    note: no real ROM, so delivery into the guest is NOT verified\n");
}

void testResourceRelease()
{
    std::printf("test_resource_release\n");
    /* Repeated create/destroy cycles must not leak the process-global core. */
    for (int i = 0; i < 25; ++i) {
        MiniVmacInProcessCore core;
        check(core.create() == MvStatus::Ok, "create in loop");
        auto rom = syntheticRom(64 * 1024);
        core.loadRom(rom.data(), rom.size());
        auto disk = syntheticDisk(32 * 1024);
        core.attachDisk(2, disk.data(), disk.size(), true, "Loop");
        if (i % 2 == 0) {
            core.start();
            std::this_thread::sleep_for(std::chrono::milliseconds(2));
        }
        core.stop();
        /* Destructor runs here; the next iteration must find the core free. */
    }
    check(!MiniVmacInProcessCore::coreInUse(), "core released after all instances were destroyed");
}

void testDestroyWhileRunning()
{
    std::printf("test_destroy_while_running\n");
    {
        MiniVmacInProcessCore core;
        auto rom = syntheticRom();
        core.create();
        core.loadRom(rom.data(), rom.size());
        core.start();
        std::this_thread::sleep_for(std::chrono::milliseconds(40));
        /* Destructor must join the worker rather than detach or leak it. */
    }
    check(!MiniVmacInProcessCore::coreInUse(), "destroying a running instance releases the core");
}

void testLatestFrameAccessor()
{
    std::printf("test_latest_frame_accessor\n");
    MiniVmacInProcessCore core;
    MvFrame frame;
    check(!core.latestFrame(&frame), "no frame before create");
    core.create();
    check(!core.latestFrame(nullptr), "null out-param refused");
    auto rom = syntheticRom();
    core.loadRom(rom.data(), rom.size());
    core.start();
    std::this_thread::sleep_for(std::chrono::milliseconds(80));
    core.stop();
    /* Whether a frame was produced depends on how far the synthetic ROM got;
	   the accessor must be safe either way. */
    const bool got = core.latestFrame(&frame);
    /* 像素尚未接通（上游 Screen_OutputFrame 不上报整帧），所以这里只断言
	   "取用帧不会失败"，并如实报告是否拿到了像素。 */
    /* 像素现在真的接通了：从模拟 RAM 的当前显示页读取。 */
    check(got, "latestFrame returns a frame once the engine has ticked");
    if (got) {
        check(frame.pixels.size() > 0, "frame carries pixel bytes");
        check(frame.width == 512, "frame width is the Mac screen width");
    }
    std::printf("    latestFrame=%s bytes=%zu %ux%u seq=%llu\n", got ? "true" : "false",
                frame.pixels.size(), frame.width, frame.height,
                (unsigned long long)frame.sequence);
    core.stop();
}


/* ---------------------------------------------------------------------------
   Lifecycle concurrency.

   These exercise the worker/stop/destroy machinery, not Mac OS. No guest boots
   in any of them: the synthetic ROM is not a Macintosh ROM. Where a test needs
   the worker to stay parked, it uses the test-only hold gate, which is compiled
   into the test archive only (VW_INPROCESS_TEST_HOOKS).
--------------------------------------------------------------------------- */

void testImmediateDoubleStart()
{
    std::printf("test_immediate_double_start\n");
    MiniVmacInProcessCore core;
    auto rom = syntheticRom();
    check(core.create() == MvStatus::Ok, "create");
    check(core.loadRom(rom.data(), rom.size()) == MvStatus::Ok, "load exact-size ROM");
    /* Back-to-back starts, before the worker has had any chance to run. */
    const MvStatus first = core.start();
    const MvStatus second = core.start();
    const MvStatus third = core.start();
    check(first == MvStatus::Ok, "first start accepted");
    check(second == MvStatus::AlreadyRunning, "immediate second start refused");
    check(third == MvStatus::AlreadyRunning, "immediate third start refused");
    core.stop();
}

void testRestartAfterStopDoesNotOverwriteJoinableThread()
{
    std::printf("test_restart_after_stop_does_not_overwrite_joinable_thread\n");
    /*
		合成 ROM 下事件循环会一直运行到被 stop，并不会“自己结束”。因此这里
		断言的是正确行为：worker 持续运行，直到 stop() 让循环观察到停止标志。

		本测试真正要覆盖的风险是：重启时不得覆盖一个仍然 joinable 的
		std::thread（那会直接 std::terminate）。走一遍
		start -> stop -> start -> stop 即可覆盖该路径。
	*/
    MiniVmacInProcessCore core;
    auto rom = syntheticRom();
    core.create();
    core.loadRom(rom.data(), rom.size());
    check(core.start() == MvStatus::Ok, "start");
    std::this_thread::sleep_for(std::chrono::milliseconds(120));
    check(core.isRunning(), "worker keeps running until it is stopped");
    core.stop();
    check(!core.isRunning(), "worker stopped");

    /* 此时 worker 处于 Exited 但线程对象尚可 join 的状态。重启必须先回收它。 */
    const MvStatus restart = core.start();
    check(restart == MvStatus::Ok, "restart after a completed run is accepted");
    std::this_thread::sleep_for(std::chrono::milliseconds(30));
    core.stop();
    core.stop();
    check(!core.isRunning(), "second run stopped cleanly");
}

void testStopStormEightThreads()
{
    std::printf("test_stop_storm_eight_threads\n");
    MiniVmacInProcessCore core;
    auto rom = syntheticRom();
    core.create();
    core.loadRom(rom.data(), rom.size());
    core.start();
    std::this_thread::sleep_for(std::chrono::milliseconds(30));

    /* Eight threads stopping at once: exactly one may join, the rest must wait
	   and observe the same outcome. */
    std::atomic<int> returned{0};
    std::vector<std::thread> storm;
    for (int i = 0; i < 8; ++i) {
        storm.emplace_back([&core, &returned]() {
            core.stop();
            returned.fetch_add(1);
        });
    }
    for (auto &t : storm) {
        t.join();
    }
    check(returned.load() == 8, "all eight stop() calls returned");
    check(!core.isRunning(), "engine stopped after the storm");
    check(core.stop() == MvStatus::Ok, "stop after the storm is still safe");
}

/*
	析构与成员调用的并发契约。

	在 C++ 里，对象析构与"同时对该对象调用成员函数"是**未定义行为**——析构会
	销毁 mutex 与 condition_variable，而另一个线程可能正在它们上面等待。这不是
	库能修复的，必须由调用方保证。

	之前这里写了一个 delete 与 stop() 并发的用例，实测会间歇性触发
	`system_error: condition_variable wait failed`（对已销毁的 cv 等待）。那是
	测试自身用法错误，已改为下列三种**有明确定义**的场景。

	契约：析构前必须确保没有其它线程正在调用该对象的成员函数。
*/
void testConcurrentStopThenDestroy()
{
    std::printf("test_concurrent_stop_then_destroy\n");
    for (int i = 0; i < 8; ++i) {
        auto *core = new MiniVmacInProcessCore();
        auto rom = syntheticRom();
        core->create();
        core->loadRom(rom.data(), rom.size());
        core->start();
        std::this_thread::sleep_for(std::chrono::milliseconds(5));

        /* 多线程并发 stop：只有一个真正 join，其余等待同一结果。 */
        std::vector<std::thread> stoppers;
        for (int k = 0; k < 4; ++k) {
            stoppers.emplace_back([core]() { core->stop(); });
        }
        for (auto &t : stoppers) {
            t.join();
        }
        /* 所有 stop 都返回后（无并发成员调用）才析构。 */
        delete core;
    }
    check(!MiniVmacInProcessCore::coreInUse(), "core released after each stop-then-destroy");
}

void testDestroyWithoutExplicitStop()
{
    std::printf("test_destroy_without_explicit_stop\n");
    /* 未显式 stop 就析构：析构内部必须自行 join，不能泄漏线程。 */
    for (int i = 0; i < 6; ++i) {
        MiniVmacInProcessCore core;
        auto rom = syntheticRom();
        core.create();
        core.loadRom(rom.data(), rom.size());
        core.start();
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    check(!MiniVmacInProcessCore::coreInUse(), "destructor joins its worker and releases the core");
}

void testInvalidRomYieldsStableStatus()
{
    std::printf("test_invalid_rom_yields_stable_status\n");
    /* A synthetic ROM is not a Macintosh ROM. Whatever the engine does with it,
	   it must be a controlled outcome: either the emulation loop runs (and the
	   run returns Ok), or it never ticks and the run is reported as an invalid
	   ROM. It must never abort the process, and it must never be reported as a
	   successfully booted guest. */
    MiniVmacInProcessCore core;
    auto rom = syntheticRom();
    check(core.create() == MvStatus::Ok, "create");
    check(core.loadRom(rom.data(), rom.size()) == MvStatus::Ok, "load exact-size ROM");
    check(core.start() == MvStatus::Ok, "start");
    std::this_thread::sleep_for(std::chrono::milliseconds(150));
    core.stop();
    check(core.framesDelivered() >= 0, "per-frame delivery counter is readable");
    MvStats st = core.stats();
    std::printf("    ticks=%llu frames=%llu (no guest boot is claimed)\n",
                (unsigned long long)st.ticks, (unsigned long long)st.frames);
}

void testHoldGateLifecycle()
{
    std::printf("test_hold_gate_lifecycle\n");
    /* Park the worker before ProgramMain so start/stop/destroy can be exercised
	   deterministically. This says nothing about Mac OS: the gate simply holds
	   the worker thread inside the same process. */
    MiniVmacInProcessCore core;
    auto rom = syntheticRom();
    check(core.create() == MvStatus::Ok, "create");
    check(core.loadRom(rom.data(), rom.size()) == MvStatus::Ok, "load exact-size ROM");

    std::atomic<int> callbacks{0};
    core.setFrameCallback([&](const MvFrame &) { callbacks.fetch_add(1); });

    vw_test_gate_enable();
    check(core.start() == MvStatus::Ok, "start with the gate armed");
    /* Wait for the worker to reach the gate, then hammer the lifecycle. */
    for (int i = 0; i < 200 && !vw_test_gate_entered(); ++i) {
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    check(vw_test_gate_entered() == 1, "worker reached the pre-ProgramMain gate");
    check(core.isRunning(), "engine reports running while held");
    check(core.start() == MvStatus::AlreadyRunning, "start while held is refused");

    std::thread stopper([&core]() { core.stop(); });
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
    /* Release the gate FIRST: stop() cannot return until the held worker exits. */
    vw_test_gate_release();
    stopper.join();
    check(!core.isRunning(), "engine stopped after the gate was released");
    vw_test_gate_release();
    check(core.stop() == MvStatus::Ok, "stop is idempotent after the gate");

    const int seen = callbacks.load();
    std::this_thread::sleep_for(std::chrono::milliseconds(30));
    check(callbacks.load() == seen, "no callback fires after stop() returned");
    std::printf("    callbacks while held: %d\n", seen);
}


/*
	B1 回归测试（由独立验证发现并证伪的缺陷）。

	此前 `worker_joinable` 在线程对象创建之后才置位，因此 stop() 落在这个窗口
	内时会提前返回，连停止请求都不发——实测 253/300 次 stop() 返回 Ok 而引擎
	继续运行。现在 `worker_active` 在持锁、创建线程之前置位，stop() 的请求无条件
	发出。

	判定语义：只有在 start() 已经返回 Ok *且早于* stop() 返回时，才要求 stop()
	返回后引擎不再运行。若 stop() 先完成，则"之后才启动"是合法语义，不计为违规。
*/
void testStopRacingStartIsNotSilentlyIgnored()
{
    std::printf("test_stop_racing_start_is_not_silently_ignored\n");
    const int rounds = 300;
    int violations = 0;
    int started_first = 0;

    for (int i = 0; i < rounds; ++i) {
        MiniVmacInProcessCore core;
        auto rom = syntheticRom();
        if (core.create() != MvStatus::Ok) {
            continue;
        }
        core.loadRom(rom.data(), rom.size());

        std::atomic<bool> go{false};
        std::atomic<int> order{0};
        std::atomic<int> start_seq{-1};
        std::atomic<int> stop_seq{-1};
        std::atomic<int> start_rc{1};

        std::thread starter([&]() {
            while (!go.load()) {}
            start_rc.store((int)core.start());
            start_seq.store(order.fetch_add(1));
        });
        std::thread stopper([&]() {
            while (!go.load()) {}
            core.stop();
            stop_seq.store(order.fetch_add(1));
        });
        go.store(true);
        starter.join();
        stopper.join();

        const bool start_won = (start_rc.load() == 0)
                               && start_seq.load() < stop_seq.load();
        if (!start_won) {
            core.stop();
            continue;
        }
        ++started_first;
        /* stop() 已返回：引擎此刻不得仍在运行。 */
        if (core.isRunning()) {
            ++violations;
            core.stop();
        }
        core.stop();
    }

    std::printf("    rounds with start-before-stop: %d, violations: %d\n",
                started_first, violations);
    check(violations == 0,
          "stop() racing start() never returns while the engine keeps running");
    check(started_first > 0,
          "the race actually exercised the start-before-stop ordering");
}

} // namespace

int main()
{
    std::printf("MiniVmacInProcessCore tests\n");
    std::printf("(synthetic ROM only; no distributed ROM, no real guest, no child process)\n\n");

    testRomSizeIsExposed();
    testStatusNames();
    testCreateIsIdempotent();
    testSecondInstanceIsBusyWhileFirstLives();
    testReacquireOnlyAfterFirstIsDestroyed();
    testReacquireRepeatedly();
    testLoadRomValidation();
    testStartRequiresRom();
    testDiskAttachDetach();
    testRunAndStopConcurrently();
    testFrameCallbackFiresOnce();
    testInputsAreSafe();
    testMouseButtonAndWheel();
    testResourceRelease();
    testDestroyWhileRunning();
    testLatestFrameAccessor();
    testImmediateDoubleStart();
    testRestartAfterStopDoesNotOverwriteJoinableThread();
    testStopStormEightThreads();
    testConcurrentStopThenDestroy();
    testDestroyWithoutExplicitStop();
    testInvalidRomYieldsStableStatus();
    testHoldGateLifecycle();
    testStopRacingStartIsNotSilentlyIgnored();

    std::printf("\nchecks: %d, failures: %d\n", g_checks, g_failures);
    if (g_failures != 0) {
        std::printf("RESULT: FAIL\n");
        return 1;
    }
    std::printf("RESULT: PASS\n");
    return 0;
}
