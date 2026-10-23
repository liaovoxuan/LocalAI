/*
	vw_minivmac_core.h

	Control surface of the VirtualWorld in-process Mini vMac engine.

	Everything the portable Mini vMac core leaves to the operating system is
	implemented in vw_minivmac_core.c. This header is the boundary the C++ API
	(src/MiniVmacInProcessCore.cpp) uses.

	There is no process management here by design: no fork/exec/system/popen, no
	external executable, no PID. An instance is a set of globals owned by the
	caller, and the caller decides which thread runs vw_core_run().

	Copyright (C) 2026 VirtualWorld contributors
	Distributed under version 2 of the GNU General Public License; see COPYING.txt.
*/

#ifndef VW_MINIVMAC_CORE_H
#define VW_MINIVMAC_CORE_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Stable status codes. Values are part of the public contract. */
#define VW_OK                    0
#define VW_ERR_BAD_ARGUMENT     -1
#define VW_ERR_OUT_OF_MEMORY    -2
#define VW_ERR_NOT_PREPARED     -3
#define VW_ERR_ALREADY_RUNNING  -4
#define VW_ERR_UNSUPPORTED      -5
#define VW_ERR_BUSY             -6
#define VW_ERR_INVALID_ROM      -9
#define VW_ERR_RUN_FAILED      -10

/* Upper bound for a captured frame; the largest supported Mac screen is well
   under this, and the framebuffer is a fixed allocation so the engine never
   grows it at runtime. */
#define VW_SCREEN_MAX_BYTES (1024 * 1024)

/* --- lifecycle ---------------------------------------------------------- */

/* Allocate the emulated RAM and the ROM buffer. Idempotent. */
int vw_core_prepare(void);

/* Pointer/size of the ROM buffer the caller must fill before vw_core_run(). */
unsigned char *vw_core_rom_buffer(void);
unsigned long vw_core_rom_size(void);

/* Run until vw_core_request_stop(). Blocks; caller chooses the thread. */
int vw_core_run(void);

/* Clear the stop flag. Called by start() before the worker exists. */
void vw_core_clear_stop(void);

/* Ask the loop to return. Safe repeatedly and from any thread. */
void vw_core_request_stop(void);

int vw_core_is_running(void);

/* Release the machine arena. Safe repeatedly. */
void vw_core_release(void);
void vw_core_dispose(void);

/* Test-only hold gate. Only present when compiled with VW_INPROCESS_TEST_HOOKS;
   the production archive has no such symbols. */
#ifdef VW_INPROCESS_TEST_HOOKS
void vw_test_gate_enable(void);
void vw_test_gate_release(void);
int  vw_test_gate_entered(void);
int  vw_test_gate_enabled(void);
#endif

/* Reset the emulated machine in place. */
void vw_core_zap(void);

/* --- media (in memory) -------------------------------------------------- */

int vw_osglu_attach_disk(unsigned drive, const void *bytes, unsigned long size,
	unsigned long *outInstalled, int writable, const char *name);
unsigned long vw_osglu_disk_size(unsigned drive);
unsigned char *vw_osglu_disk_bytes(unsigned drive);
void vw_osglu_reset_media(void);

/* --- observation -------------------------------------------------------- */

const unsigned char *vw_osglu_frame_data(unsigned *outRows, unsigned char *outDepth,
	unsigned *outSeq);
void vw_osglu_publish_frame(const void *rows, unsigned rowBytes, unsigned height);

/* 注册每帧回调；由工作线程在每帧绘制完成时调用。 */
void vw_osglu_set_frame_hook(void (*hook)(void));

/* 当前显示页在模拟 RAM 中的地址（复现 SCRNEMDV 的选择逻辑）。 */
unsigned char *vw_osglu_current_screen_buffer(void);

unsigned vw_osglu_audio_frames(void);
const unsigned *vw_osglu_audio_data(void);
unsigned vw_osglu_audio_blocks(void);

unsigned vw_osglu_unsupported_disk_warnings(void);

/* --- input -------------------------------------------------------------- */

int vw_core_post_key(unsigned mac_key_code, int down);
int vw_core_post_mouse(int x, int y);

/* 鼠标按钮：button 必须是 0（Mac 128K/512Ke/Plus 只有一个按钮）。 */
int vw_core_post_mouse_button(int button, int down);

/* 滚轮：按上游约定映射为方向键脉冲。 */
int vw_core_post_mouse_wheel(int dx, int dy);

int vw_core_frame_count(void);
int vw_core_tick_count(void);

/* --- platform hooks used by the OSGLU layer ----------------------------- */

void vw_sched_note_tick(void);
void vw_sched_note_frame(void);
void vw_osglu_reset_clock(void);
unsigned vw_osglu_now_us(void);

#ifdef __cplusplus
}
#endif

#endif /* VW_MINIVMAC_CORE_H */
