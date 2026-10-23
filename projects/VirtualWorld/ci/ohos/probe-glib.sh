#!/bin/sh
# ============================================================================
# probe-glib.sh — compile + link + PROVE the GLib aarch64-OHOS cross probe.
#
# Demonstrates that build-qemu-ohos-deps/prefix is a usable, contamination-free
# OpenHarmony/aarch64 dependency prefix: it builds probe-glib.c (which includes
# <glib.h> and calls GLib APIs) with the OHOS clang WRAPPER and the cross
# pkg-config, then proves via file/llvm-readelf that the artifact is:
#   * ELF 64-bit LSB, ARM aarch64  (correct architecture)
#   * dynamically linked to the musl loader  (/lib/ld-musl-aarch64.so.1)
#   * NEEDED: libglib-2.0.so.0 (+ libffi / libpcre2-8 / libintl transitively)
#
# Exits non-zero (NO fake-green) if any proof fails.
# Usage:  . ci/ohos/env-ohos.sh && sh ci/ohos/probe-glib.sh
# ============================================================================
set -e

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
. "$ROOT/ci/ohos/env-ohos.sh"

OHOS_READELF="$OHOS_LLVM/bin/llvm-readelf"
OUT="/tmp/probe-glib.elf"
SRC="$ROOT/ci/ohos/probe-glib.c"

echo "[probe] CFLAGS/LIBS from pkg-config (glib-2.0):"
PKG_CFLAGS="$($PKG_CONFIG --cflags glib-2.0)"
PKG_LIBS="$($PKG_CONFIG --libs glib-2.0)"
echo "  CFLAGS: $PKG_CFLAGS"
echo "  LIBS  : $PKG_LIBS"

echo "[probe] compiling + linking with OHOS clang wrapper..."
"$CC" "$SRC" -o "$OUT" $PKG_CFLAGS $PKG_LIBS
echo "[probe] link succeeded: $OUT"

echo "[probe] === file ==="
file "$OUT"

echo "[probe] === llvm-readelf -h (machine / ABI) ==="
"$OHOS_READELF" -h "$OUT" | grep -E "Class|Machine|OS/ABI|Type|Entry"

echo "[probe] === llvm-readelf -d (NEEDED / interpreter) ==="
"$OHOS_READELF" -d "$OUT" | grep -E "NEEDED|INTERP|RUNPATH" || true

echo "[probe] === interpreter check ==="
INTERP="$("$OHOS_READELF" -l "$OUT" | grep -A1 INTERP | grep -oE '/lib/[^]]*' || true)"
echo "  interpreter: $INTERP"

# ---- PROOF assertions (no fake-green) --------------------------------------
fail=0
if file "$OUT" | grep -qE "ELF 64-bit LSB.*ARM aarch64"; then
  echo "[probe] PASS: architecture is ARM aarch64 (ELF64)."
else
  echo "[probe] FAIL: not an aarch64 ELF."; fail=1
fi

if echo "$INTERP" | grep -q "ld-musl-aarch64"; then
  echo "[probe] PASS: musl interpreter (OpenHarmony userspace)."
else
  echo "[probe] FAIL: interpreter is not the musl loader ($INTERP)."; fail=1
fi

if "$OHOS_READELF" -d "$OUT" | grep -q "libglib-2.0.so.0"; then
  echo "[probe] PASS: NEEDED libglib-2.0.so.0 present (linked against GLib)."
else
  echo "[probe] FAIL: libglib-2.0.so.0 not in NEEDED."; fail=1
fi

# Transitive dependencies GLib pulls in from our prefix.
for lib in libffi.so.8 libpcre2-8.so.0 libintl.so.8; do
  if "$OHOS_READELF" -d "$OUT" | grep -q "$lib"; then
    echo "[probe] PASS: NEEDED $lib (transitive from GLib)."
  else
    echo "[probe] NOTE: $lib not directly NEEDED (may be satisfied via libglib)."
  fi
done

if [ "$fail" -ne 0 ]; then
  echo "[probe] FATAL: one or more proof checks failed."; exit 1
fi
echo "[probe] ALL PROOFS PASSED."
