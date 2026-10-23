#!/bin/sh
# ============================================================================
# env-ohos.sh — OpenHarmony aarch64 cross-toolchain environment for building
# QEMU's target dependencies (glib-2.0 + libffi + pcre2) and the GLib probe.
#
# DESIGN GOALS (auditable, no host contamination):
#   * Use the OHOS-Native-SDK clang WRAPPERS. Those wrappers already bake in
#       -target aarch64-linux-ohos  --sysroot=<SDK>/sysroot  -D__MUSL__
#     so we never hand-write --sysroot/--target and never point at /usr/include.
#   * Install ALL cross-built artifacts into $OHOS_DEPS_PREFIX only.
#   * PKG_CONFIG_LIBDIR is set to $OHOS_DEPS_PREFIX/lib/pkgconfig and REPLACES
#     the default search path, so pkg-config can never see Homebrew/Darwin .pc
#     files. (PKG_CONFIG_PATH is intentionally emptied.)
#   * Host-only tools (meson/ninja/pkg-config/cmake) come from /opt/homebrew and
#     the managed Python venv on PATH, but they are never linked into the target.
#
# USAGE:  cd <repo-root> && . ci/ohos/env-ohos.sh
# After sourcing, CC/CXX/AR/... and PKG_CONFIG_LIBDIR are exported, and two files
# are written: ci/ohos/ohos-cross.ini (meson) and ci/ohos/ohos-clang.cmake.
# ============================================================================
set -e

# --- OpenHarmony Native SDK (fixed location on this machine) ----------------
export OHOS_NATIVE="${OHOS_NATIVE:-/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/native}"
export OHOS_SYSROOT="$OHOS_NATIVE/sysroot"
export OHOS_LLVM="$OHOS_NATIVE/llvm"

# Wrapper executable name prefix (the binaries are aarch64-unknown-linux-ohos-clang).
# The *effective* clang -target inside the wrapper is aarch64-linux-ohos.
export OHOS_TRIPLE="${OHOS_TRIPLE:-aarch64-linux-ohos}"
export OHOS_WRAPPER_PREFIX="${OHOS_WRAPPER_PREFIX:-aarch64-unknown-linux-ohos}"

# --- Project-private install prefix (never write system dirs) ----------------
export OHOS_DEPS_PREFIX="${OHOS_DEPS_PREFIX:-$PWD/build-qemu-ohos-deps/prefix}"

# --- Cross compilers / binutils from the SDK -------------------------------
export CC="$OHOS_LLVM/bin/${OHOS_WRAPPER_PREFIX}-clang"
export CXX="$OHOS_LLVM/bin/${OHOS_WRAPPER_PREFIX}-clang++"
export AR="$OHOS_LLVM/bin/llvm-ar"
export RANLIB="$OHOS_LLVM/bin/llvm-ranlib"
export STRIP="$OHOS_LLVM/bin/llvm-strip"
export NM="$OHOS_LLVM/bin/llvm-nm"
export OBJCOPY="$OHOS_LLVM/bin/llvm-objcopy"
export LD="$OHOS_LLVM/bin/ld.lld"

# --- Host-only build tools (NOT linked into the target) --------------------
# meson + ninja from the managed Python venv; pkg-config/cmake/ninja also in brew.
export PATH="/opt/homebrew/bin:${HOME}/.workbuddy-ai/binaries/python/envs/default/bin:$PATH"
export PKG_CONFIG="${PKG_CONFIG:-/opt/homebrew/bin/pkg-config}"

# --- CRITICAL: lock pkg-config to the private prefix only -------------------
# PKG_CONFIG_LIBDIR replaces the default search path entirely, so Homebrew and
# /usr/lib/pkgconfig are never consulted for the target. The .pc files installed
# by our libffi/pcre2/glib builds live here and already carry correct absolute
# $prefix paths, so no PKG_CONFIG_SYSROOT_DIR mangling is needed.
export PKG_CONFIG_LIBDIR="$OHOS_DEPS_PREFIX/lib/pkgconfig"
export PKG_CONFIG_PATH=""

# --- Target compiler/linker flags -------------------------------------------
# The wrapper supplies --sysroot + -D__MUSL__; we only add -fPIC. We deliberately
# do NOT add any -I/-L pointing at host (Homebrew/Darwin) directories.
export CFLAGS="-fPIC"
export CXXFLAGS="-fPIC"

# --- Write the meson cross file --------------------------------------------
# system='linux' because OpenHarmony is a Linux-compatible, musl-based userspace.
OHOS_CROSS_INI="$PWD/ci/ohos/ohos-cross.ini"
cat > "$OHOS_CROSS_INI" <<INI
[binaries]
c = '$CC'
cpp = '$CXX'
ar = '$AR'
ranlib = '$RANLIB'
strip = '$STRIP'
nm = '$NM'
objcopy = '$OBJCOPY'
ld = '$LD'
pkg-config = '$PKG_CONFIG'
cmake = '/opt/homebrew/bin/cmake'

[properties]
# pkg-config search dir is locked to our private prefix so glib only sees the
# libffi/pcre2 we cross-built (never Homebrew/Darwin .pc files).
pkg_config_libdir = '$OHOS_DEPS_PREFIX/lib/pkgconfig'
# NOTE: we deliberately DO NOT set sys_root here. The OHOS clang WRAPPER
# already bakes in --sysroot=<SDK>/sysroot, and if meson's sys_root property is
# also set it would relocate the ABSOLUTE -I/-L paths from our .pc files to
# <sysroot>/Users/.../prefix/include (broken). The wrapper-supplied sysroot is
# sufficient; meson must pass our prefix include/lib paths through verbatim.

[host_machine]
system = 'linux'
cpu_family = 'aarch64'
cpu = 'aarch64'
endian = 'little'

[target_machine]
system = 'linux'
cpu_family = 'aarch64'
cpu = 'aarch64'
endian = 'little'
INI

# --- Write the CMake toolchain file -----------------------------------------
OHOS_CMAKE_TC="$PWD/ci/ohos/ohos-clang.cmake"
cat > "$OHOS_CMAKE_TC" <<CMAKE
# CMake toolchain for OpenHarmony aarch64 (musl) using the SDK clang wrappers.
set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR aarch64)

# Use the wrapper scripts; they already provide --target/--sysroot/-D__MUSL__.
set(CMAKE_C_COMPILER   "$CC" CACHE FILEPATH "OHOS clang")
set(CMAKE_CXX_COMPILER "$CXX" CACHE FILEPATH "OHOS clang++")
set(CMAKE_AR   "$AR"   CACHE FILEPATH "llvm-ar")
set(CMAKE_RANLIB "$RANLIB" CACHE FILEPATH "llvm-ranlib")
set(CMAKE_STRIP "$STRIP" CACHE FILEPATH "llvm-strip")
set(CMAKE_NM   "$NM"   CACHE FILEPATH "llvm-nm")
set(CMAKE_SYSROOT "$OHOS_SYSROOT")

# Do NOT let CMake search host (Darwin/Homebrew) paths.
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
set(CMAKE_FIND_ROOT_PATH "$OHOS_SYSROOT" "$OHOS_DEPS_PREFIX")

# pkg-config: only the private prefix (no Homebrew/Darwin contamination).
set(PKG_CONFIG_EXECUTABLE "$PKG_CONFIG" CACHE FILEPATH "host pkg-config")
set(ENV{PKG_CONFIG_LIBDIR} "$OHOS_DEPS_PREFIX/lib/pkgconfig")
set(ENV{PKG_CONFIG_PATH}   "")
CMAKE

echo "[env-ohos] CC       = $CC"
echo "[env-ohos] SYSROOT  = $OHOS_SYSROOT"
echo "[env-ohos] PREFIX   = $OHOS_DEPS_PREFIX"
echo "[env-ohos] PKG_CONFIG_LIBDIR = $PKG_CONFIG_LIBDIR"
echo "[env-ohos] wrote: $OHOS_CROSS_INI , $OHOS_CMAKE_TC"
echo "[env-ohos] NOTE: install only into \$OHOS_DEPS_PREFIX; never into system dirs."
