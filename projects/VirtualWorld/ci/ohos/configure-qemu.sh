#!/bin/sh
# ============================================================================
# configure-qemu.sh — QEMU configure-ONLY probe for OpenHarmony/aarch64.
#
# SCOPE: this script ONLY runs QEMU's ./configure (no make / no guest / no run).
# It proves that the dependency prefix built by build-deps.sh (glib-2.0 + libffi
# + pcre2) is correctly consumed by QEMU's build system, and that pixman can be
# dropped for a headless build (-Dpixman=disabled). It records remaining blockers
# for the (out-of-scope) compile stage.
#
# IMPORTANT: this runs QEMU's configure OUT-OF-TREE (build dir under
# build-qemu-ohos-deps/qemu-config-probe). It does NOT modify qemu-source/**.
#
# Reproduce:
#   . ci/ohos/env-ohos.sh && sh ci/ohos/configure-qemu.sh
# ============================================================================
set -e

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
. "$ROOT/ci/ohos/env-ohos.sh"

QEMU_SRC="$ROOT/qemu-source"
[ -x "$QEMU_SRC/configure" ] || { echo "[qemu] FATAL: $QEMU_SRC/configure missing"; exit 1; }

SDK="$OHOS_LLVM/bin"

# --- Build the cross-prefix tool symlink dir --------------------------------
# QEMU's --cross-prefix=PREFIX makes it look for ${PREFIX}gcc, ${PREFIX}ar, ...
# Our real tools are the clang WRAPPERS + SDK llvm binutils. We symlink them
# under a neutral dir so the PREFIX resolves. (readelf -> llvm-readelf.)
XBIN="/tmp/ohos-xbin"
rm -rf "$XBIN"; mkdir -p "$XBIN"
for t in gcc g++ cpp cc clang clang++; do
  ln -sf "$SDK/aarch64-unknown-linux-ohos-clang" "$XBIN/aarch64-unknown-linux-ohos-$t" 2>/dev/null || true
done
ln -sf "$SDK/aarch64-unknown-linux-ohos-clang++" "$XBIN/aarch64-unknown-linux-ohos-clang++"
ln -sf "$SDK/llvm-ar"      "$XBIN/aarch64-unknown-linux-ohos-ar"
ln -sf "$SDK/llvm-ranlib"  "$XBIN/aarch64-unknown-linux-ohos-ranlib"
ln -sf "$SDK/llvm-strip"   "$XBIN/aarch64-unknown-linux-ohos-strip"
ln -sf "$SDK/llvm-nm"      "$XBIN/aarch64-unknown-linux-ohos-nm"
ln -sf "$SDK/llvm-objcopy" "$XBIN/aarch64-unknown-linux-ohos-objcopy"
ln -sf "$SDK/ld.lld"       "$XBIN/aarch64-unknown-linux-ohos-ld"
ln -sf "$SDK/llvm-readelf" "$XBIN/aarch64-unknown-linux-ohos-readelf"
# NOTE: llvm-dlltool does not ship in the OHOS SDK. dlltool is only used for
# PE/COFF (Windows) import libraries, never for aarch64-linux ELF, so its
# absence is harmless for this target.
export PATH="$XBIN:$PATH"

# --- Out-of-tree configure --------------------------------------------------
QB="$ROOT/build-qemu-ohos-deps/qemu-config-probe"
rm -rf "$QB"; mkdir -p "$QB"
cd "$QB"

echo "[qemu] configure (out-of-tree, cross aarch64-linux-ohos) -> $QB"
"$QEMU_SRC/configure" \
  --cross-prefix=aarch64-unknown-linux-ohos- \
  --host-cc=/usr/bin/cc \
  --prefix="$OHOS_DEPS_PREFIX/qemu" \
  --target-list=aarch64-softmmu,aarch64-linux-user \
  --disable-pixman \
  --disable-docs \
  --disable-guest-agent \
  --disable-capstone \
  --disable-slirp \
  --python="$(command -v python3)" \
  > /tmp/qemu_configure.log 2>&1
rc=$?
echo "CONFIGURE_EXIT=$rc" >> /tmp/qemu_configure.log

if [ "$rc" -ne 0 ]; then
  echo "[qemu] FATAL: configure failed (rc=$rc). See /tmp/qemu_configure.log"
  echo "[qemu] ---- tail ----"; tail -30 /tmp/qemu_configure.log
  exit 1
fi

echo "[qemu] configure SUCCEEDED. Key checks:"
grep -E "Run-time dependency glib-2.0 found|pixman|Found ninja" /tmp/qemu_configure.log | head
echo "[qemu] full log: /tmp/qemu_configure.log"
echo "[qemu] generated meson cross file: $QB/config-meson.cross"
echo "[qemu] REMAINING (out-of-scope) blockers for 'make': see docs/ohos-qemu-dependencies.md"
