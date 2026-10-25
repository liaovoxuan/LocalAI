#!/usr/bin/env bash
# Run inside an Ubuntu 24.04 container with the requested CPU architecture.
set -euo pipefail
arch="${1:?Usage: build_cloudai_linux.sh arm64|riscv64}"
case "$arch:$(uname -m)" in
  arm64:aarch64|riscv64:riscv64) ;;
  *) echo "Target $arch does not match the running Linux architecture $(uname -m)" >&2; exit 1 ;;
esac

apt-get update
# Use distribution-built native extensions, including on RISC-V where PyPI
# wheels are not generally available. Python itself is bundled by PyInstaller.
apt-get install -y --no-install-recommends \
  ca-certificates build-essential zlib1g-dev binutils file \
  python3 python3-dev python3-venv python3-tk python3-pip \
  python3-pil python3-pil.imagetk python3-requests python3-psutil python3-cpuinfo \
  xvfb xauth desktop-file-utils rpm
python3 -m venv --system-site-packages .venv-cloudai-linux
source .venv-cloudai-linux/bin/activate
python -m pip install 'pyinstaller==6.22.3' 'pyinstaller-hooks-contrib==2026.9'
python -m unittest tooling.tests.test_cloudai_linux_packaging -v
python -m unittest discover -s projects/CloudAI/desktop -p test_cloudai_plugins.py -v
python -m PyInstaller --clean --noconfirm projects/CloudAI/packaging/CloudAI_Linux.spec

# Validate the bundled interpreter, Tk, Pillow, plugins, resources and ELF
# headers before publishing anything. Xvfb avoids requiring a physical display.
out="release/cloudai/$arch"
mkdir -p "$out"
file dist/CloudAI/CloudAI
xvfb-run -a timeout 120 dist/CloudAI/CloudAI --smoke-test | tee "$out/smoke-test.json"
python tooling/scripts/package_cloudai_linux.py --arch "$arch" --output "$out"
