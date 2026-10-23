/*
	MiniVmacInProcessCore.hpp

	Stable C++ API for the VirtualWorld in-process Mini vMac engine.

	The emulator core runs inside the calling process on a worker thread owned by
	this class. There is no child process, no PID, and nothing is exec'd: that is
	what makes the engine usable on HarmonyOS/OpenHarmony, where launching an
	external executable is forbidden.

	Copyright (C) 2026 VirtualWorld contributors
	Distributed under version 2 of the GNU General Public License; see
	third_party/minivmac/upstream/COPYING.txt.
*/

#ifndef MINI_VMAC_IN_PROCESS_CORE_HPP
#define MINI_VMAC_IN_PROCESS_CORE_HPP

#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

namespace vw {

/* Status codes. Stable; they are part of the public contract. */
enum class MvStatus : int32_t {
    Ok = 0,
    BadArgument = -1,
    OutOfMemory = -2,
    NotPrepared = -3,
    AlreadyRunning = -4,
    Unsupported = -5,
    Busy = -6,
    NotRunning = -7,
    RomMissing = -8,
};

const char *mvStatusName(MvStatus s);

/* A captured frame of the emulated Macintosh screen. */
struct MvFrame {
    std::vector<uint8_t> pixels;
    uint32_t width = 0;
    uint32_t height = 0;
    uint8_t depth = 0;
    uint64_t sequence = 0;
    bool valid = false;
};

struct MvAudioBlock {
    std::vector<uint32_t> samples; /* interleaved stereo, engine-native format */
    uint64_t sequence = 0;
    bool valid = false;
};

struct MvStats {
    uint64_t frames = 0;
    uint64_t ticks = 0;
    uint64_t audio_blocks = 0;
    uint64_t unsupported_disk_warnings = 0;
};

/*
	Opaque handle. Callers never see engine internals; the implementation is a
	pImpl so the ABI does not depend on the emulator's globals.
*/
class MiniVmacInProcessCore {
public:
    MiniVmacInProcessCore();
    ~MiniVmacInProcessCore();

    MiniVmacInProcessCore(const MiniVmacInProcessCore &) = delete;
    MiniVmacInProcessCore &operator=(const MiniVmacInProcessCore &) = delete;

    /*
		Prepare the emulated machine. Must be called once before loadRom.

		The upstream core keeps machine state in process globals, so only one
		instance may be active at a time. A second live instance returns
		MvStatus::Busy rather than silently corrupting the first -- an honest,
		stable limitation instead of undefined behaviour.
	*/
    MvStatus create();

    /* Copy a user-supplied ROM image in. The ROM is never distributed with the
	   engine and is never read from disk by this class. */
    MvStatus loadRom(const uint8_t *bytes, size_t size);

    /* Attach an in-memory disk image to a drive (0-based, engine max 6). */
    MvStatus attachDisk(unsigned drive, const uint8_t *bytes, size_t size,
                        bool writable = true, const std::string &name = std::string());

    /* Detach a drive, discarding its medium. Safe when nothing is attached. */
    MvStatus detachDisk(unsigned drive);

    /* Start emulation on a worker thread. Returns immediately. */
    MvStatus start();

    /* Stop and join the worker. Safe to call repeatedly, and safe when never
	   started. Idempotent. */
    MvStatus stop();

    bool isRunning() const;

    /* Commit a key state to the emulated keyboard. */
    MvStatus setKey(uint8_t macKeyCode, bool down);

    /* Move the emulated mouse. */
    MvStatus setMouse(int32_t x, int32_t y);

    /*
		鼠标按钮。Mac 128K/512Ke/Plus 只有一个按钮，因此没有按钮编号参数。
		与上游平台层一致，走同一事件队列，由核心的 Mouse_Update() 消费。
	*/
    MvStatus setMouseButton(bool down);

    /*
		滚轮。上游核心没有滚轮概念，Mini vMac 的约定是映射为方向键脉冲：
		dx<0 左 / dx>0 右 / dy<0 下 / dy>0 上。沿用该约定。
	*/
    MvStatus setMouseWheel(int32_t dx, int32_t dy);

    /* Latest completed frame. Returns false when no frame has been drawn yet. */
    bool latestFrame(MvFrame *out) const;

    MvStats stats() const;

    /* 回调投递次数（工作线程每帧一次）。用于验证"每帧恰好一次"。 */
    uint64_t framesDelivered() const;

    /*
		Callback invoked once per completed frame on the worker thread. Setting a
		callback before start() guarantees it fires exactly once per frame; it is
		never called after stop() has returned.
	*/
    void setFrameCallback(std::function<void(const MvFrame &)> cb);

    /* True when an engine instance currently owns the process-global core. */
    static bool coreInUse();

    /*
		该构建所期望的 ROM 字节数（= 上游的 kROM_Size）。

		这是**编译期**常量：引擎按机型配置构建，例如 Mac Plus 配置为 0x20000
		(128 KiB)，Mac 128K/512Ke 配置为 0x10000 (64 KiB)。调用方必须在 loadRom
		之前用它来确定要提供多大的 ROM —— 引擎不会补零，也不会接受尺寸不符的
		ROM。

		静态版本可用于 create() 之前；实例版本便于已有对象处调用。
	*/
    static size_t romSize();
    size_t romSizeForThisBuild() const;

private:
    struct Impl;
    Impl *impl_;
};

} // namespace vw

#endif /* MINI_VMAC_IN_PROCESS_CORE_HPP */
