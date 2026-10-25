#!/usr/bin/env bash
# Run in a fresh Ubuntu container, with no build environment mounted outside
# /workspace and no Python installed. Test the installed launcher and relocation.
set -euo pipefail
arch="${1:?Usage: verify_cloudai_linux.sh arm64|riscv64}"
out="$PWD/release/cloudai/$arch"
apt-get update
apt-get install -y --no-install-recommends "$out"/*.deb xvfb xauth
if command -v python3 >/dev/null 2>&1; then
  echo "Runtime verification must run without a system Python installation" >&2
  exit 1
fi
xvfb-run -a timeout 120 cloudai --smoke-test | tee "$out/installed-smoke-test.json"

# A portable archive must work at an arbitrary path, including paths with spaces,
# and must not accidentally use an app previously installed under /opt.
apt-get remove -y cloudai
portable="$(mktemp -d)/portable CloudAI"
mkdir -p "$portable"
tar -xzf "$out"/*.tar.gz -C "$portable"
xvfb-run -a timeout 120 "$portable/CloudAI/CloudAI" --smoke-test | tee "$out/portable-smoke-test.json"
(cd "$out" && sha256sum *.json *.txt *.deb *.rpm *.tar.gz > SHA256SUMS)
