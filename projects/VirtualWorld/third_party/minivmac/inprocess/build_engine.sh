#!/bin/bash
#
# build_engine.sh -- standalone build of the VirtualWorld in-process Mini vMac engine.
#
# Deliberately independent of the project's public CMake: this module is built on
# its own so it can be validated without touching shared build files.
#
# Usage:  bash third_party/minivmac/inprocess/build_engine.sh [build-dir]
#
# Output: <build-dir>/libvwminivmac.a   (portable core + headless platform layer)
#
# Nothing here spawns an external process at *runtime*; the compiler invocations
# below are build-time only, which is why the static scan in the test suite
# checks the produced objects rather than this script.

set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../../.." && pwd)"
build_dir="${1:-$root/build-minivmac-inprocess}"

src_dir="$here/../upstream/src"
# 机型变体：默认使用已验证的 cfg/（Mac Plus 配置，kROM_Size 0x20000）。
# 传 VW_VARIANT=<name> 则使用 variants/<name>/cfg/，产物带变体后缀，
# 以便同一台机器上为不同 ROM 尺寸 / RAM 大小的机型分别构建引擎。
#
# 上游各机型的 kROM_Size 只有两种取值：
#   Mac 128K / Twiggy -> 0x10000 (64 KiB)
#   Mac 512Ke / Plus  -> 0x20000 (128 KiB)
# 但 RAM 大小不同（128K 机型 128 KiB，Plus 1 MiB），这会影响屏幕缓冲地址，
# 所以必须使用对应机型的完整 cfg，而不只是改一个尺寸宏。
variant="${VW_VARIANT:-}"
if [ -n "$variant" ]; then
  cfg_dir="$here/variants/$variant/cfg"
  if [ ! -d "$cfg_dir" ]; then
    echo "unknown variant '$variant': $cfg_dir not found" >&2
    exit 2
  fi
  suffix="-$variant"
else
  cfg_dir="$here/cfg"
  suffix=""
fi

cc="${CC:-clang}"
cflags=(-O2 -g -std=c11 -w
        -I"$cfg_dir" -I"$src_dir" -I"$here"
        -DVW_INPROCESS_ENGINE=1)

# Optional sanitizer / extra flags, e.g. EXTRA_CFLAGS="-fsanitize=address".
if [ -n "${EXTRA_CFLAGS:-}" ]; then
  # shellcheck disable=SC2206
  cflags+=(${EXTRA_CFLAGS})
fi

# The portable emulator core: the 68K interpreter plus the emulated devices.
core_sources=(
  GLOBGLUE.c M68KITAB.c MINEM68K.c VIAEMDEV.c IWMEMDEV.c SCCEMDEV.c
  RTCEMDEV.c ROMEMDEV.c SCSIEMDV.c SONYEMDV.c SCRNEMDV.c MOUSEMDV.c
  KBRDEMDV.c SNDEMDEV.c PROGMAIN.c
)

obj_dir="$build_dir/obj$suffix"
obj_test_dir="$build_dir/obj-test$suffix"
mkdir -p "$obj_dir"
objects=()

for f in "${core_sources[@]}"; do
  obj="$obj_dir/${f%.c}.o"
  "$cc" "${cflags[@]}" -c "$src_dir/$f" -o "$obj"
  objects+=("$obj")
done

# The headless platform layer: every OS service the core needs, in-process.
"$cc" "${cflags[@]}" -c "$here/vw_minivmac_core.c" -o "$obj_dir/vw_minivmac_core.o"
objects+=("$obj_dir/vw_minivmac_core.o")
# 独立 TU：只包含 PICOMMON.h，复现 SCRNEMDV 的显示页选择
"$cc" "${cflags[@]}" -c "$here/vw_screen_probe.c" -o "$obj_dir/vw_screen_probe.o"
objects+=("$obj_dir/vw_screen_probe.o")

# Standard C++ synchronization and timing also support native Windows builds.
cxx="${CXX:-clang++}"
cxxflags=("${cflags[@]/-std=c11/-std=c++17}")
"$cxx" "${cxxflags[@]}" -c "$here/vw_host_platform.cpp" -o "$obj_dir/vw_host_platform.o"
objects+=("$obj_dir/vw_host_platform.o")

rm -f "$build_dir/libvwminivmac$suffix.a"
ar rcs "$build_dir/libvwminivmac$suffix.a" "${objects[@]}"

# --- test archive -------------------------------------------------------
# Identical engine plus the test-only pre-ProgramMain hold gate. The production
# archive above never contains those symbols, so the gate cannot be reached from
# a normal build.
test_objects=()
for f in "${core_sources[@]}"; do
  obj="$obj_test_dir/${f%.c}.o"
  mkdir -p "$obj_test_dir"
  "$cc" "${cflags[@]}" -c "$src_dir/$f" -o "$obj"
  test_objects+=("$obj")
done
"$cc" "${cflags[@]}" -DVW_INPROCESS_TEST_HOOKS -c "$here/vw_minivmac_core.c" \
    -o "$obj_test_dir/vw_minivmac_core.o"
test_objects+=("$obj_test_dir/vw_minivmac_core.o")
"$cc" "${cflags[@]}" -c "$here/vw_screen_probe.c" -o "$obj_test_dir/vw_screen_probe.o"
test_objects+=("$obj_test_dir/vw_screen_probe.o")
test_objects+=("$obj_dir/vw_host_platform.o")

rm -f "$build_dir/libvwminivmac-test$suffix.a"
ar rcs "$build_dir/libvwminivmac-test$suffix.a" "${test_objects[@]}"

echo "built $build_dir/libvwminivmac$suffix.a (production, ${#objects[@]} objects)"
echo "built $build_dir/libvwminivmac-test$suffix.a (with test gate, ${#test_objects[@]} objects)"
