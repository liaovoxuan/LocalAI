/*
	vw_screen_probe.c -- 读取模拟 RAM 中当前显示页的地址。

	为什么单独成一个编译单元：

	Mini vMac 的配置头（PICOMMON.h / DFCNFCMP.h / ENDIANAC.h / GLOBGLUE.h）用一个
	`#error "header already included"` 约定来保证"只按特定顺序包含一次"，而
	COMOSGLU.h 那条链（OSGCOMUI.h -> OSGCOMUD.h -> COMOSGLU.h）并不包含完整链。
	因此把需要 PICOMMON.h 的代码放进独立 TU，只包含 PICOMMON.h，就与上游设备
	文件（SCRNEMDV.c 等）的包含风格完全一致，也避免与主引擎 TU 的头顺序冲突。

	功能：复现 SCRNEMDV.c 对屏幕缓冲的选择
	    SCRNvPage2 == 1 ? get_ram_address(kMain_Buffer) : get_ram_address(kAlternate_Buffer)
	其中 kMain_Buffer = kRAM_Size - 0x5900、kAlternate_Buffer = kRAM_Size - 0xD900，
	SCRNvPage2 由 cfg/CNFUDPIC.h 定义为一个 VIA 位（Wires[Wire_VIA1_iA6_SCRNvPage2]）。

	只读取，不修改任何模拟状态；上游源码未做任何改动。

	Copyright (C) 2026 VirtualWorld contributors
	Distributed under version 2 of the GNU General Public License; see COPYING.txt.
*/

#include "PICOMMON.h"

/* 与 SCRNEMDV.c 中的局部宏保持一致（那两个宏是文件局部的，这里按同值复现）。 */
#define VW_KMAIN_OFFSET      0x5900
#define VW_KALTERNATE_OFFSET 0xD900

unsigned char *vw_osglu_current_screen_buffer(void)
{
	if (RAM == nullpr) {
		return nullpr;
	}
	if (SCRNvPage2 == 1) {
		return (unsigned char *)get_ram_address(kRAM_Size - VW_KMAIN_OFFSET);
	}
	return (unsigned char *)get_ram_address(kRAM_Size - VW_KALTERNATE_OFFSET);
}
