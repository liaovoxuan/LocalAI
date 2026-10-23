/*
	vw_minivmac_core.c

	VirtualWorld in-process Mini vMac engine -- headless platform layer.

	This file supplies the small set of symbols the portable Mini vMac core
	leaves to the operating system, and exposes a C control surface the C++
	API wraps. It is deliberately self-contained:

	  * no GUI framework (no Cocoa / SDL / GTK / Win32),
	  * no child processes of any kind -- no fork/exec/system/popen/QProcess,
	    and no external executable is ever launched, so the engine is usable on
	    HarmonyOS/OpenHarmony where spawning is forbidden,
	  * no host file access for disks: media are attached in memory,
	  * no PID semantics: instances are opaque handles owned by the caller.

	Emulation genuinely runs: the 68K interpreter in MINEM68K.c executes in this
	process on a worker thread provided by the C++ layer.

	Copyright (C) 2026 VirtualWorld contributors
	Portions derived from Mini vMac, Copyright (C) Philip Cummins, Paul C. Pratt.
	Distributed under version 2 of the GNU General Public License; see COPYING.txt.
*/

#include "CNFUIOSG.h"
#include "OSGCOMUI.h"
#include "OSGCOMUD.h"
#include "COMOSGLU.h"
#include "PROGMAIN.h"
#include "MOUSEMDV.h"
/* Portable, header-only parameter-buffer implementation (in-memory media). */
#include "PBUFSTDC.h"

#include <string.h>
#include <time.h>
#include <stdatomic.h>
#include <pthread.h>

/* --- exported control surface ------------------------------------------- */

#include "vw_minivmac_core.h"

/* --- timing ------------------------------------------------------------- */

static _Atomic ui5b vw_tick_base_us = 0;

/*
	跨线程停止握手。

	stop() 可能从任意线程调用，因此它只写这个原子标志。事件循环真正读取的
	上游全局 ForceMacOff，只在工作线程内部被转写（见 WaitForNextTick），于是
	ForceMacOff 全程只被一个线程访问：既没有数据竞争，也无需改动上游代码。
*/
static _Atomic int vw_stop_requested = 0;

/*
	输入队列。

	所有输入注入都必须由**工作线程**执行：上游的 Keyboard_UpdateKeyMap /
	MyMouseButtonSet / Mouse_Update 会碰触设备状态（VIA、SCC、事件队列），
	而这些状态归模拟线程所有。若调用线程直接调用它们，就会与工作线程竞争
	（ThreadSanitizer 实测到 Mouse_Update 读 SCC_InterruptsEnabled 与工作线程
	EmulatedHardwareZap 写 SCC_Reset 的竞争）。

	因此调用线程只入队，工作线程在 WaitForNextTick 里取出并执行。
*/
enum { VW_IN_KEY = 0, VW_IN_MOUSE, VW_IN_BUTTON, VW_IN_WHEEL };
#define VW_INPUT_CAP 256
typedef struct { int kind; int a; int b; } vw_input_evt;

static vw_input_evt vw_input_buf[VW_INPUT_CAP];
static int vw_input_head = 0;
static int vw_input_tail = 0;
static pthread_mutex_t vw_input_mtx = PTHREAD_MUTEX_INITIALIZER;

static int vw_input_push(int kind, int a, int b)
{
	int rc = VW_OK;
	pthread_mutex_lock(&vw_input_mtx);
	{
		int next = (vw_input_head + 1) % VW_INPUT_CAP;
		if (next == vw_input_tail) {
			rc = VW_ERR_BUSY;		/* 队列已满：调用方需先让引擎跑起来 */
		} else {
			vw_input_buf[vw_input_head].kind = kind;
			vw_input_buf[vw_input_head].a = a;
			vw_input_buf[vw_input_head].b = b;
			vw_input_head = next;
		}
	}
	pthread_mutex_unlock(&vw_input_mtx);
	return rc;
}

/* 仅在工作线程内调用。 */
static void vw_input_drain(void)
{
	for (;;) {
		vw_input_evt e;
		pthread_mutex_lock(&vw_input_mtx);
		if (vw_input_tail == vw_input_head) {
			pthread_mutex_unlock(&vw_input_mtx);
			break;
		}
		e = vw_input_buf[vw_input_tail];
		vw_input_tail = (vw_input_tail + 1) % VW_INPUT_CAP;
		pthread_mutex_unlock(&vw_input_mtx);

		switch (e.kind) {
		case VW_IN_KEY:
			Keyboard_UpdateKeyMap((ui3r)e.a, e.b ? trueblnr : falseblnr);
			break;
		case VW_IN_MOUSE:
			CurMouseH = (ui4b)e.a;
			CurMouseV = (ui4b)e.b;
			Mouse_Update();
			break;
		case VW_IN_BUTTON:
			MyMouseButtonSet(e.a ? trueblnr : falseblnr);
			break;
		case VW_IN_WHEEL:
			/* 沿用上游 SDL 层的约定：滚轮 = 方向键脉冲。 */
			if (e.a < 0) {
				Keyboard_UpdateKeyMap(MKC_Left, trueblnr);
				Keyboard_UpdateKeyMap(MKC_Left, falseblnr);
			} else if (e.a > 0) {
				Keyboard_UpdateKeyMap(MKC_Right, trueblnr);
				Keyboard_UpdateKeyMap(MKC_Right, falseblnr);
			}
			if (e.b < 0) {
				Keyboard_UpdateKeyMap(MKC_Down, trueblnr);
				Keyboard_UpdateKeyMap(MKC_Down, falseblnr);
			} else if (e.b > 0) {
				Keyboard_UpdateKeyMap(MKC_Up, trueblnr);
				Keyboard_UpdateKeyMap(MKC_Up, falseblnr);
			}
			break;
		default:
			break;
		}
	}
}

/* 每帧回调，定义见下文；此处前向声明供 WaitForNextTick 使用。 */
static void (*vw_frame_hook)(void);

static ui5b vw_now_us(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (ui5b)(((ui5b)ts.tv_sec * 1000000u) + (ui5b)(ts.tv_nsec / 1000));
}

/*
	Mini vMac paces itself by asking the platform for the next 60th-of-a-second
	boundary. Headless we simply track monotonic time; there is no window to
	service, so a tick boundary is a pure accounting event.
*/
EXPORTOSGLUPROC WaitForNextTick(void)
{
	/*
		Pacing. The GUI layers block here until the display's next refresh, which
		both paces emulation and gives the stop flag a bounded time to be seen.
		Headless we sleep briefly instead of spinning: an unpaced loop can spend a
		long time inside one catch-up turn, during which ForceMacOff is not
		checked and stop() would appear to hang.
	*/
	struct timespec ts;
	ts.tv_sec = 0;
	ts.tv_nsec = 1000000; /* 1ms */
	nanosleep(&ts, NULL);

	/* 把跨线程的停止请求转写为上游标志。此处运行在工作线程，因此
	   ForceMacOff 始终只有一个线程访问。 */
	if (atomic_load_explicit(&vw_stop_requested, memory_order_relaxed)) {
		ForceMacOff = trueblnr;
	}

	/*
		每个 tick 结束时捕获当前显示页并投递一帧。

		这是"每帧恰好一次"的正确投递点：上游每个 60Hz tick 会经
		Screen_EndTickNotify 更新屏幕，而 DoneWithDrawingForTick 只由 GUI 平台层
		调用、headless 路径上永远不会被触发（这正是此前回调一次都没发出的原因）。
		这里运行在工作线程内，因此回调也天然在工作线程上。
	*/
	vw_input_drain();

	{
		ui3p buf = vw_osglu_current_screen_buffer();
		if (buf != nullpr) {
			vw_osglu_publish_frame(buf, (ui5b)(vMacScreenWidth / 8),
				(ui5b)vMacScreenHeight);
			vw_sched_note_frame();   /* 让 stats().frames 反映真实帧数 */
			if (vw_frame_hook != NULL) {
				vw_frame_hook();
			}
		}
	}

	vw_sched_note_tick();
}

EXPORTOSGLUFUNC blnr ExtraTimeNotOver(void)
{
	/*
		Upstream uses this to decide whether to keep catching up on emulated time.
		Headless there is no vSync, so we always report that the extra-time budget
		is exhausted; the core then returns to its tick pacing instead of spinning.
	*/
	return falseblnr;
}

void vw_osglu_reset_clock(void)
{
	vw_tick_base_us = vw_now_us();
}

ui5b vw_osglu_now_us(void)
{
	return vw_now_us();
}

/* --- memory ------------------------------------------------------------- */

EXPORTOSGLUPROC MyMoveBytes(anyp srcPtr, anyp destPtr, si5b byteCount)
{
	if (byteCount > 0 && srcPtr != nullpr && destPtr != nullpr) {
		memcpy(destPtr, srcPtr, (size_t)byteCount);
	}
}

/* --- sound -------------------------------------------------------------- */

/*
	Headless audio: the core asks for a buffer it can fill with samples, then
	reports how many were written. We hand it a scratch buffer and publish a
	counter plus the most recent block so a host can pull audio if it wants.
	No device is opened and nothing is spawned.
*/
#define VW_SOUND_FRAMES 4096

static ui4b vw_sound_scratch[VW_SOUND_FRAMES * 2];
static ui4b vw_sound_capture[VW_SOUND_FRAMES * 2];
static _Atomic ui4b vw_sound_capture_frames = 0;
static _Atomic ui5b vw_sound_blocks = 0;

EXPORTOSGLUFUNC tpSoundSamp MySound_BeginWrite(ui4r n, ui4r *actL)
{
	ui4r frames = n;
	if (frames > VW_SOUND_FRAMES) {
		frames = VW_SOUND_FRAMES;
	}
	if (actL != nullpr) {
		*actL = frames;
	}
	memset(vw_sound_scratch, 0, sizeof(ui4b) * (size_t)frames * 2);
	return (tpSoundSamp)vw_sound_scratch;
}

EXPORTOSGLUPROC MySound_EndWrite(ui4r actL)
{
	ui4r frames = actL;
	if (frames > VW_SOUND_FRAMES) {
		frames = VW_SOUND_FRAMES;
	}
	memcpy(vw_sound_capture, vw_sound_scratch, sizeof(ui4b) * (size_t)frames * 2);
	vw_sound_capture_frames = frames;
	vw_sound_blocks++;
}

unsigned vw_osglu_audio_frames(void)
{
	return vw_sound_capture_frames;
}

const unsigned *vw_osglu_audio_data(void)
{
	return (const unsigned *)vw_sound_capture;
}

unsigned vw_osglu_audio_blocks(void)
{
	return vw_sound_blocks;
}

/* --- video -------------------------------------------------------------- */

/*
	Screen_OutputFrame (from COMOSGLU.h) hands us the freshly drawn Mac
	framebuffer. We copy it into a stable buffer the host can read at any time,
	and DoneWithDrawingForTick marks the frame as complete. Nothing is drawn to
	a window: the framebuffer is simply data.
*/
static ui3b vw_frame[VW_SCREEN_MAX_BYTES];
static _Atomic ui5b vw_frame_seq = 0;
static _Atomic ui5b vw_frame_rows = 0;
static _Atomic ui3b vw_frame_depth = 0;

/*
	每帧钩子。DoneWithDrawingForTick 在工作线程内、每完成一帧就被调用一次，
	所以这里才是"每帧恰好一次"回调的正确投递点。此前回调只在整轮结束后调用
	一次，等于从未真正每帧投递。
*/
/* vw_frame_hook 已在文件前部前向声明 */

void vw_osglu_set_frame_hook(void (*hook)(void))
{
	vw_frame_hook = hook;
}

EXPORTOSGLUPROC DoneWithDrawingForTick(void)
{
	vw_frame_rows = (ui5b)vMacScreenHeight;
	vw_frame_depth = (ui3b)vMacScreenDepth;
	vw_frame_seq++;
	vw_sched_note_frame();
	if (vw_frame_hook != NULL) {
		vw_frame_hook();
	}
}

void vw_osglu_publish_frame(const void *rows, ui5b rowBytes, ui5b height)
{
	size_t bytes = (size_t)rowBytes * (size_t)height;
	if (rows == NULL || bytes == 0) {
		return;
	}
	if (bytes > sizeof(vw_frame)) {
		bytes = sizeof(vw_frame);
	}
	memcpy(vw_frame, rows, bytes);
	vw_frame_rows = height;
	vw_frame_depth = (ui3b)vMacScreenDepth;
	vw_frame_seq++;
}

const ui3b *vw_osglu_frame_data(ui5b *outRows, ui3b *outDepth, ui5b *outSeq)
{
	if (outRows != NULL) {
		*outRows = vw_frame_rows;
	}
	if (outDepth != NULL) {
		*outDepth = vw_frame_depth;
	}
	if (outSeq != NULL) {
		*outSeq = vw_frame_seq;
	}
	return vw_frame;
}

/* --- user-facing messages ----------------------------------------------- */

static _Atomic ui5b vw_unsupported_disk_warnings = 0;

EXPORTOSGLUPROC WarnMsgUnsupportedDisk(void)
{
	/* Headless: record it rather than putting up a dialog. */
	vw_unsupported_disk_warnings++;
}

ui5b vw_osglu_unsupported_disk_warnings(void)
{
	return vw_unsupported_disk_warnings;
}

/* --- host copy in/out (not provided headless) --------------------------- */

EXPORTOSGLUFUNC tMacErr HTCEexport(tPbuf i)
{
	UnusedParam(i);
	return mnvm_nsDrvErr;
}

EXPORTOSGLUFUNC tMacErr HTCEimport(tPbuf *r)
{
	if (r != NULL) {
		*r = NotAPbuf;
	}
	return mnvm_nsDrvErr;
}

/* --- in-memory disk media ----------------------------------------------- */

/*
	Disks live in this process's heap. The host attaches a byte image to a drive
	and the emulated Macintosh reads and writes it directly; nothing is opened on
	the filesystem and no helper process is involved.

	Raw mode and "new disk" bookkeeping are honoured as flags so the core's
	normal drive logic behaves, but they never touch the host.
*/
typedef struct {
	ui3p data;
	ui5b size;
	blnr attached;
	blnr writable;
	char name[64];
} vw_drive_t;

static vw_drive_t vw_drives[NumDrives];
static ui5b vw_new_disk_size = 0;

/* vSonyRawMode / vSonyNewDisk* / vSony*Mask are provided by COMOSGLU.h. */

static blnr vw_drive_valid(tDrive d)
{
	return (d < (tDrive)NumDrives) ? trueblnr : falseblnr;
}

void vw_osglu_reset_media(void)
{
	int i;
	for (i = 0; i < NumDrives; ++i) {
		if (vw_drives[i].data != NULL) {
			free(vw_drives[i].data);
			vw_drives[i].data = NULL;
		}
		vw_drives[i].size = 0;
		vw_drives[i].attached = falseblnr;
		vw_drives[i].writable = falseblnr;
		vw_drives[i].name[0] = 0;
	}
	vSonyInsertedMask = 0;
	vSonyWritableMask = 0;
	vw_new_disk_size = 0;
}

int vw_osglu_attach_disk(unsigned drive, const void *bytes, unsigned long size,
	unsigned long *outInstalled, int writable, const char *name)
{
	vw_drive_t *d;

	if (drive >= (unsigned)NumDrives || (bytes == NULL && size != 0)) {
		return VW_ERR_BAD_ARGUMENT;
	}
	d = &vw_drives[drive];
	if (d->data != NULL) {
		free(d->data);
		d->data = NULL;
	}
	if (size == 0) {
		d->attached = falseblnr;
		d->size = 0;
		vSonyInsertedMask &= ~(((ui5b)1) << drive);
		vSonyWritableMask &= ~(((ui5b)1) << drive);
		return VW_OK;
	}
	/* Exactly `size` bytes are installed; media are never padded silently. */
	d->data = (ui3p)malloc((size_t)size);
	if (d->data == NULL) {
		d->attached = falseblnr;
		d->size = 0;
		return VW_ERR_OUT_OF_MEMORY;
	}
	memcpy(d->data, bytes, (size_t)size);
	d->size = (ui5b)size;
	d->attached = trueblnr;
	d->writable = writable ? trueblnr : falseblnr;
	d->name[0] = 0;
	if (name != NULL) {
		size_t n = strlen(name);
		if (n > sizeof(d->name) - 1) {
			n = sizeof(d->name) - 1;
		}
		memcpy(d->name, name, n);
		d->name[n] = 0;
	}
	vSonyInsertedMask |= (((ui5b)1) << drive);
	if (d->writable) {
		vSonyWritableMask |= (((ui5b)1) << drive);
	} else {
		vSonyWritableMask &= ~(((ui5b)1) << drive);
	}
	if (outInstalled != NULL) {
		*outInstalled = size;
	}
	return VW_OK;
}

unsigned long vw_osglu_disk_size(unsigned drive)
{
	if (drive >= (unsigned)NumDrives) {
		return 0;
	}
	return vw_drives[drive].attached ? vw_drives[drive].size : 0;
}

unsigned char *vw_osglu_disk_bytes(unsigned drive)
{
	if (drive >= (unsigned)NumDrives || !vw_drives[drive].attached) {
		return NULL;
	}
	return (unsigned char *)vw_drives[drive].data;
}

EXPORTOSGLUFUNC tMacErr vSonyTransfer(blnr IsWrite, ui3p Buffer,
	tDrive Drive_No, ui5r Sony_Start, ui5r Sony_Count, ui5r *Sony_ActCount)
{
	vw_drive_t *d;

	if (Sony_ActCount != NULL) {
		*Sony_ActCount = 0;
	}
	if (!vw_drive_valid(Drive_No)) {
		return mnvm_nsDrvErr;
	}
	d = &vw_drives[Drive_No];
	if (!d->attached) {
		return mnvm_offLinErr;
	}
	if (IsWrite && !d->writable) {
		return mnvm_wrPermErr;
	}
	if (Buffer == NULL) {
		return mnvm_permErr;
	}
	/* Reads and writes are clamped to the medium; short transfers are legal. */
	if (Sony_Start >= d->size) {
		return mnvm_offLinErr;
	}
	if ((ui5r)Sony_Count > (ui5r)(d->size - Sony_Start)) {
		Sony_Count = (ui5r)(d->size - Sony_Start);
	}
	if (IsWrite) {
		memcpy(d->data + Sony_Start, Buffer, (size_t)Sony_Count);
	} else {
		memcpy(Buffer, d->data + Sony_Start, (size_t)Sony_Count);
	}
	if (Sony_ActCount != NULL) {
		*Sony_ActCount = Sony_Count;
	}
	return mnvm_noErr;
}

EXPORTOSGLUFUNC tMacErr vSonyEject(tDrive Drive_No)
{
	if (!vw_drive_valid(Drive_No)) {
		return mnvm_nsDrvErr;
	}
	if (!vw_drives[Drive_No].attached) {
		return mnvm_offLinErr;
	}
	if (vw_drives[Drive_No].data != NULL) {
		free(vw_drives[Drive_No].data);
		vw_drives[Drive_No].data = NULL;
	}
	vw_drives[Drive_No].size = 0;
	vw_drives[Drive_No].attached = falseblnr;
	vSonyInsertedMask &= ~(((ui5b)1) << Drive_No);
	vSonyWritableMask &= ~(((ui5b)1) << Drive_No);
	return mnvm_noErr;
}

#if IncludeSonyNew
EXPORTOSGLUFUNC tMacErr vSonyEjectDelete(tDrive Drive_No)
{
	/* Nothing is stored on the host, so eject already discards the medium. */
	return vSonyEject(Drive_No);
}
#endif

EXPORTOSGLUFUNC tMacErr vSonyGetSize(tDrive Drive_No, ui5r *Sony_Count)
{
	if (Sony_Count == NULL) {
		return mnvm_permErr;
	}
	if (!vw_drive_valid(Drive_No)) {
		return mnvm_nsDrvErr;
	}
	if (!vw_drives[Drive_No].attached) {
		return mnvm_offLinErr;
	}
	*Sony_Count = (ui5r)vw_drives[Drive_No].size;
	return mnvm_noErr;
}

#if IncludeSonyNameNew
EXPORTOSGLUFUNC tMacErr vSonyGetName(tDrive Drive_No, tPbuf *r)
{
	tMacErr err;
	ui5r wrote = 0;
	char nm[64];

	if (r == NULL) {
		return mnvm_permErr;
	}
	if (!vw_drive_valid(Drive_No)) {
		return mnvm_nsDrvErr;
	}
	if (!vw_drives[Drive_No].attached) {
		return mnvm_offLinErr;
	}
	nm[0] = 0;
	if (vw_drives[Drive_No].name[0] != 0) {
		strncpy(nm, vw_drives[Drive_No].name, sizeof(nm) - 1);
		nm[sizeof(nm) - 1] = 0;
	}
	err = PbufNew((ui5b)(strlen(nm) + 1), r);
	if (err != mnvm_noErr) {
		return err;
	}
	PbufTransfer((ui3p)nm, *r, 0, (ui5r)(strlen(nm) + 1), trueblnr);
	(void)wrote;
	return mnvm_noErr;
}
#endif

/* --- engine control surface --------------------------------------------- */

/*
	Test-only hold gate. Compiled out of the production archive. It lets a test
	park the worker immediately before ProgramMain() so that lifecycle
	concurrency (double-start, stop storms, destroy racing stop) can be exercised
	without depending on how far a synthetic ROM happens to get.

	It proves nothing about Mac OS: no guest boots in any of these tests.
*/
#ifdef VW_INPROCESS_TEST_HOOKS
static _Atomic int vw_gate_enabled = 0;
static _Atomic int vw_gate_released = 0;
static _Atomic int vw_gate_entered = 0;

void vw_test_gate_enable(void) { vw_gate_released = 0; vw_gate_entered = 0; vw_gate_enabled = 1; }
void vw_test_gate_release(void) { vw_gate_released = 1; }
int  vw_test_gate_entered(void) { return vw_gate_entered; }
int  vw_test_gate_enabled(void) { return vw_gate_enabled; }
static void vw_gate_wait(void)
{
	if (!vw_gate_enabled) {
		return;
	}
	vw_gate_entered = 1;
	while (!vw_gate_released) {
		struct timespec ts;
		ts.tv_sec = 0;
		ts.tv_nsec = 2000000; /* 2ms */
		nanosleep(&ts, NULL);
	}
}
#else
static void vw_gate_wait(void) { }
#endif

static _Atomic int vw_prepared = 0;
static _Atomic int vw_running = 0;
static _Atomic int vw_frames = 0;
static _Atomic int vw_ticks = 0;

void vw_sched_note_tick(void)
{
	vw_ticks++;
}

void vw_sched_note_frame(void)
{
	vw_frames++;
}

/*
	The portable core allocates machine memory from one pre-reserved arena
	(`ReserveAllocBigBlock`), so that emulated addresses stay inside a single
	allocation the platform owns. The GUI layers build it in two passes: run the
	allocation plan once to measure the size, calloc that much, then run the plan
	again to place the blocks. We do exactly the same, headless.

	Getting this wrong is what makes every later step fail, so the replay is
	checked: if the second pass does not consume exactly the measured size, the
	arena is rejected rather than left half-built.
*/
static ui3p vw_arena = nullpr;

static void vw_reserve_all(void)
{
	ReserveAllocOneBlock(&ROM, kROM_Size, 5, falseblnr);
	ReserveAllocOneBlock(&screencomparebuff, vMacScreenNumBytes, 5, trueblnr);
	EmulationReserveAlloc();
}

int vw_core_prepare(void)
{
	uimr need;

	if (vw_prepared) {
		return VW_OK;
	}
	vw_osglu_reset_media();
	vw_osglu_reset_clock();

	if (vw_arena == nullpr) {
		/* Pass 1: measure the arena. */
		ReserveAllocOffset = 0;
		ReserveAllocBigBlock = nullpr;
		vw_reserve_all();
		need = ReserveAllocOffset;
		if (need == 0) {
			return VW_ERR_OUT_OF_MEMORY;
		}
		vw_arena = (ui3p)calloc(1, (size_t)need);
		if (vw_arena == nullpr) {
			return VW_ERR_OUT_OF_MEMORY;
		}
		/* Pass 2: place the blocks. */
		ReserveAllocBigBlock = vw_arena;
		ReserveAllocOffset = 0;
		vw_reserve_all();
		if (ReserveAllocOffset != need) {
			/* Non-deterministic plan: refuse rather than run on a bad arena. */
			free(vw_arena);
			vw_arena = nullpr;
			ReserveAllocBigBlock = nullpr;
			return VW_ERR_OUT_OF_MEMORY;
		}
	}

	if (ROM == nullpr) {
		return VW_ERR_OUT_OF_MEMORY;
	}
	memset(ROM, 0, (size_t)kROM_Size);
	vw_prepared = 1;
	return VW_OK;
}

/* Release the arena. Safe repeatedly; used by tests to prove no leak. */
void vw_core_release(void)
{
	if (vw_arena != nullpr) {
		free(vw_arena);
		vw_arena = nullpr;
	}
	ReserveAllocBigBlock = nullpr;
	ReserveAllocOffset = 0;
	ROM = nullpr;
	vw_prepared = 0;
}

unsigned char *vw_core_rom_buffer(void)
{
	return (unsigned char *)ROM;
}

unsigned long vw_core_rom_size(void)
{
	return (unsigned long)kROM_Size;
}

/*
	Run the emulator on the calling thread until vw_core_request_stop() is
	called. This is the upstream ProgramMain() entry: it initialises the
	emulated hardware and then loops on the event loop, which returns as soon as
	ForceMacOff is set. No process, thread or PID is created here -- the C++ layer
	decides which thread calls this.
*/
int vw_core_run(void)
{
	if (!vw_prepared) {
		return VW_ERR_NOT_PREPARED;
	}
	if (vw_running) {
		return VW_ERR_ALREADY_RUNNING;
	}
	vw_running = 1;
	/*
		这里刻意不重置 ForceMacOff。stop() 可能在本线程被调度之前就已经发出
		停止请求；若在此处清零，那条请求会被吞掉，事件循环将永远看不到它，
		stop() 便会一直等待。清除动作由 start() 在创建线程之前同步完成。
	*/
	{
		int ticks_before = vw_ticks;
		vw_osglu_reset_clock();
		vw_gate_wait();          /* test-only; no-op in production builds */
		ProgramMain();
		/* If the emulation loop never advanced, the machine failed to come up
		   (an invalid ROM is the expected cause). Report that as a stable code
		   rather than pretending a guest ran. */
		if (vw_ticks == ticks_before) {
			vw_running = 0;
			return VW_ERR_INVALID_ROM;
		}
	}
	vw_running = 0;
	return VW_OK;
}

/*
	Full teardown. The order matters: the emulation loop must already have been
	stopped and joined by the caller, then every piece of host-visible and
	core-global state is returned to its pre-init value so a later instance can
	acquire the core cleanly. Called by the C++ layer's destructor before it
	releases the process-global ownership flag.
*/
void vw_core_dispose(void)
{
	vw_core_request_stop();

	/* host layer */
	vw_osglu_reset_media();
	vw_osglu_reset_clock();

	/* counters and flags */
	vw_frames = 0;
	vw_ticks = 0;
	vw_sound_capture_frames = 0;
	vw_sound_blocks = 0;
	vw_frame_seq = 0;
	vw_frame_rows = 0;
	vw_frame_depth = 0;
	vw_unsupported_disk_warnings = 0;
	memset(vw_frame, 0, sizeof(vw_frame));

	/* core-visible control flags */
	atomic_store_explicit(&vw_stop_requested, 0, memory_order_relaxed);
	ForceMacOff = falseblnr;
	WantMacReset = falseblnr;
	WantMacInterrupt = falseblnr;
	vSonyInsertedMask = 0;
	vSonyWritableMask = 0;

	/* engine state + machine arena (and the core globals it owns) */
	vw_running = 0;
	vw_core_release();
}

/*
	在创建工作线程之前同步清除停止标志。与 start() 的临界区配对，因此不会
	与 stop() 竞争：任一时刻只可能有一个调用者处于“即将启动”或“正在请求
	停止”的状态。
*/
void vw_core_clear_stop(void)
{
	/* 在创建工作线程之前调用（与 start() 的临界区配对），此时没有并发的
	   worker，可以安全地直接写上游标志。 */
	atomic_store_explicit(&vw_stop_requested, 0, memory_order_relaxed);
	ForceMacOff = falseblnr;
}

void vw_core_request_stop(void)
{
	/* 可从任意线程反复调用。只写原子标志；工作线程会在下一个 tick 把它转写
	   为 ForceMacOff，因此循环必定观察到（最迟 1ms）。 */
	atomic_store_explicit(&vw_stop_requested, 1, memory_order_relaxed);
}

int vw_core_is_running(void)
{
	return vw_running;
}

/*
	Reset the emulated machine without tearing the engine down. The core owns the
	reset sequence (EmulatedHardwareZap is file-static), so the supported way to
	ask for one is the WantMacReset flag, which the emulation loop observes.
*/
void vw_core_zap(void)
{
	WantMacReset = trueblnr;
}

/*
	Input. The keyboard helper is defined in COMOSGLU.h, so it is compiled into
	this translation unit and can be driven directly; there is no window system
	to route events through. Mouse position is written to the core's cursor
	globals and advertised with Mouse_Update(), the same call the GUI layers use.
*/
int vw_core_post_key(unsigned mac_key_code, int down)
{
	if (mac_key_code > 127) {
		return VW_ERR_BAD_ARGUMENT;
	}
	/* 只入队；由工作线程执行（见 vw_input_drain）。 */
	return vw_input_push(VW_IN_KEY, (int)mac_key_code, down ? 1 : 0);
}

/*
	鼠标按钮。

	正如同目录的上游平台层（OSGLUCCO.m / OSGLUSDL.c）所做的，按钮走
	MyMouseButtonSet()——它是 COMOSGLU.h 的 LOCALPROC，会写入本编译单元内的
	事件队列。实证：事件队列 MyEvtQA 与 MyEvtQOutP 在本引擎的 .o 中是唯一定义
	（GLOBGLUE.o / MOUSEMDV.o 里都是未定义引用），因此核心的 Mouse_Update() 与
	FindKeyEvent() 读到的就是这里的同一个队列。这也是按键路径一直有效的原因。

	Mac 128K/512Ke/Plus 只有一个鼠标按钮，所以按钮编号参数只接受 0。
*/
int vw_core_post_mouse_button(int button, int down)
{
	if (button != 0) {
		return VW_ERR_BAD_ARGUMENT;
	}
	return vw_input_push(VW_IN_BUTTON, down ? 1 : 0, 0);
}

/*
	滚轮。

	上游核心没有滚轮概念。Mini vMac 的既有约定（见 OSGLUSDL.c 的 SDL_MOUSEWHEEL
	分支）是把滚轮方向映射为一次方向键脉冲：

		dx < 0 -> Left    dx > 0 -> Right
		dy < 0 -> Down    dy > 0 -> Up

	这里沿用同一约定，使宿主滚轮行为与上游平台层一致。
*/
int vw_core_post_mouse_wheel(int dx, int dy)
{
	if (dx == 0 && dy == 0) {
		return VW_OK;
	}
	return vw_input_push(VW_IN_WHEEL, dx, dy);
}

int vw_core_post_mouse(int x, int y)
{
	if (x < 0) { x = 0; }
	if (y < 0) { y = 0; }
	if (x > 0xFFFF) { x = 0xFFFF; }
	if (y > 0xFFFF) { y = 0xFFFF; }
	return vw_input_push(VW_IN_MOUSE, x, y);
}

int vw_core_frame_count(void)
{
	return vw_frames;
}

int vw_core_tick_count(void)
{
	return vw_ticks;
}
