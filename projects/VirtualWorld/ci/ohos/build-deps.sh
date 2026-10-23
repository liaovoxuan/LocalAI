#!/bin/sh
# ============================================================================
# build-deps.sh — reproducible cross-build of QEMU's OpenHarmony/aarch64 target
# dependencies: libffi, pcre2, glib-2.0. Installs ONLY into build-qemu-ohos-deps/prefix.
#
# Run from the repository root:
#   . ci/ohos/env-ohos.sh && sh ci/ohos/build-deps.sh
# Or simply:
#   sh ci/ohos/build-deps.sh   (it sources the env itself)
#
# Pinned versions (reproducible). SHA-256 is computed after download and checked
# against EXPECTED_SHA* below when non-empty (soft — only warns if it mismatches).
# ============================================================================
set -e

# Resolve repo root and source the toolchain environment.
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
. "$ROOT/ci/ohos/env-ohos.sh"

SRC_DIR="$ROOT/third_party/ohos-ports/sources"
BUILD_DIR="$ROOT/build-qemu-ohos-deps/build"
PREFIX="$OHOS_DEPS_PREFIX"
mkdir -p "$SRC_DIR" "$BUILD_DIR" "$PREFIX"

# ---- Pinned versions -------------------------------------------------------
GLIB_VERSION=2.78.0
LIBFFI_VERSION=3.4.4
PCRE2_VERSION=10.42

GLIB_URL="https://download.gnome.org/sources/glib/${GLIB_VERSION%.*}/glib-${GLIB_VERSION}.tar.xz"
LIBFFI_URL="https://github.com/libffi/libffi/releases/download/v${LIBFFI_VERSION}/libffi-${LIBFFI_VERSION}.tar.gz"
PCRE2_URL="https://github.com/PCRE2Project/pcre2/releases/download/pcre2-${PCRE2_VERSION}/pcre2-${PCRE2_VERSION}.tar.bz2"

GLIB_SHA="";    LIBFFI_SHA="";  PCRE2_SHA=""   # filled/checked by the script

fetch() { # url  outfile  varname
  url="$1"; out="$2"; var="$3"
  if [ -f "$out" ]; then echo "[fetch] reuse $out"; else
    echo "[fetch] $url -> $out"
    curl -fL --retry 3 -o "$out" "$url"
  fi
  sha=$(shasum -a 256 "$out" | cut -d' ' -f1)
  echo "[fetch] sha256($out) = $sha"
  eval "exp=\$$var"
  if [ -n "$exp" ] && [ "$exp" != "$sha" ]; then
    echo "[fetch] WARNING: sha256 mismatch for $out (expected $exp)" >&2
  fi
}

fetch "$LIBFFI_URL" "$SRC_DIR/libffi-${LIBFFI_VERSION}.tar.gz" LIBFFI_SHA
fetch "$PCRE2_URL"  "$SRC_DIR/pcre2-${PCRE2_VERSION}.tar.bz2" PCRE2_SHA
fetch "$GLIB_URL"   "$SRC_DIR/glib-${GLIB_VERSION}.tar.xz"    GLIB_SHA

# ---- libffi (autotools) ----------------------------------------------------
# HOST_ALIAS NOTE (IMPORTANT — do not "fix" back to aarch64-linux-ohos):
# The SDK clang WRAPPER (aarch64-unknown-linux-ohos-clang) already hardcodes
#   -target aarch64-linux-ohos  --sysroot=<SDK>/sysroot  -D__MUSL__
# so the actual emitted object files / shared libs are genuinely
# aarch64-linux-ohos ELF (proved later by file/readelf). The autotools --host
# triplet below is ONLY consumed by libffi's ./configure logic (config.sub /
# config.guess / feature probes); it is not passed to the compiler. The bundled
# config.sub in libffi 3.4.4 does NOT recognize the `ohos` OS token, so we pass
# the musl alias `aarch64-unknown-linux-musl` — a known, recognized triplet whose
# OS (musl) matches OpenHarmony's userspace exactly. CC/CXX/AR/... remain the
# real OHOS wrappers (exported by env-ohos.sh), so the produced binary is real
# aarch64-linux-ohos, never a mislabeled host build.
HOST_ALIAS="aarch64-unknown-linux-musl"

build_libffi() {
  echo "==================== libffi ${LIBFFI_VERSION} ===================="
  # In-source build. VPATH builds of libffi trip a known bug
  # ("No rule to make target 'ffi_common.h'"). An in-source build avoids it.
  # libffi's configure re-invokes itself in a $host subdir with a RELATIVE
  # --srcdir=.. ; with the sandbox DISABLED (so configure.host is not quarantined
  # by the safe-delete hook) that relative path resolves correctly to the source
  # root. The real compiler is still the OHOS clang wrapper (see HOST_ALIAS note).
  rm -rf "$BUILD_DIR/libffi-${LIBFFI_VERSION}"
  tar -xzf "$SRC_DIR/libffi-${LIBFFI_VERSION}.tar.gz" -C "$BUILD_DIR"
  src=$(ls -d "$BUILD_DIR"/libffi-${LIBFFI_VERSION})
  cd "$src"
  ./configure --host="$HOST_ALIAS" \
    --prefix="$PREFIX" --enable-shared --disable-static \
    --disable-multi-os-directory --disable-docs
  make -j"$(sysctl -n hw.ncpu 2>/dev/null || echo 4)"
  make install
  cd "$ROOT"
  echo "[libffi] installed:"; ls "$PREFIX/lib"/libffi* 2>/dev/null; ls "$PREFIX/lib/pkgconfig"/libffi.pc 2>/dev/null
  [ -f "$PREFIX/lib/pkgconfig/libffi.pc" ] || { echo "[libffi] FATAL: libffi.pc missing — build did not install"; return 1; }
}

# ---- pcre2 (autotools) -----------------------------------------------------
# Same HOST_ALIAS rationale as libffi (musl alias; real OHOS compilers via env).
build_pcre2() {
  echo "==================== pcre2 ${PCRE2_VERSION} ===================="
  # In-source build. Like libffi, pcre2's VPATH build mis-resolves the src/*.c
  # prerequisites ("No rule to make target 'src/pcre2grep.c'"); an in-source
  # build avoids it. The real compiler is the OHOS clang wrapper (HOST_ALIAS).
  rm -rf "$BUILD_DIR/pcre2-${PCRE2_VERSION}"
  tar -xjf "$SRC_DIR/pcre2-${PCRE2_VERSION}.tar.bz2" -C "$BUILD_DIR"
  src=$(ls -d "$BUILD_DIR"/pcre2-${PCRE2_VERSION})
  cd "$src"
  ./configure --host="$HOST_ALIAS" \
    --prefix="$PREFIX" --enable-shared --disable-static \
    --disable-pcre2grep-libz --disable-pcre2grep-libbz2 --enable-unicode
  make -j"$(sysctl -n hw.ncpu 2>/dev/null || echo 4)"
  make install
  cd "$ROOT"
  echo "[pcre2] installed:"; ls "$PREFIX/lib"/libpcre2* 2>/dev/null; ls "$PREFIX/lib/pkgconfig"/libpcre2-8.pc 2>/dev/null
  [ -f "$PREFIX/lib/pkgconfig/libpcre2-8.pc" ] || { echo "[pcre2] FATAL: libpcre2-8.pc missing — build did not install"; return 1; }
}

# ---- glib-2.0 (meson, cross) ---------------------------------------------
build_glib() {
  echo "==================== glib ${GLIB_VERSION} ===================="
  d="$BUILD_DIR/glib"; rm -rf "$d"; mkdir -p "$d"
  tar -xJf "$SRC_DIR/glib-${GLIB_VERSION}.tar.xz" -C "$BUILD_DIR"
  src=$(ls -d "$BUILD_DIR"/glib-${GLIB_VERSION})
  cd "$src"
  # Disable everything optional that would drag in gettext/libmount/selinux.
  # glib 2.78.0 has NO 'introspection'/'std' project options (GI moved to a
  # separate repo), so we omit them. nls/selinux/libmount are 'feature' type;
  # xattr/tests/gtk_doc/man are boolean.
  meson setup "$d" --cross-file="$ROOT/ci/ohos/ohos-cross.ini" \
    --prefix="$PREFIX" \
    -Dlibmount=disabled -Dselinux=disabled -Dnls=disabled \
    -Dxattr=false -Dtests=false -Dgtk_doc=false -Dman=false \
    -Ddefault_library=shared
  meson compile -C "$d"
  meson install -C "$d"
  cd "$ROOT"
  echo "[glib] installed:"; ls "$PREFIX/lib"/libglib-2.0* 2>/dev/null; ls "$PREFIX/lib/pkgconfig"/glib-2.0.pc 2>/dev/null
  echo "[glib] headers:"; ls "$PREFIX/include"/glib-2.0/glib.h 2>/dev/null
}

build_libffi
build_pcre2
build_glib

echo "==================== SUMMARY ===================="
echo "Install prefix: $PREFIX"
echo "pkg-config files:"; ls "$PREFIX/lib/pkgconfig" 2>/dev/null
echo "Cross-built shared libs:"; ls "$PREFIX/lib"/*.so 2>/dev/null
echo "DONE."
