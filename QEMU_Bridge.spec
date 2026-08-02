# -*- mode: python ; coding: utf-8 -*-

import os

TARGET_ARCH = os.environ.get("PYINSTALLER_TARGET_ARCH") or None


a = Analysis(
    ['QEMU_Bridge.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'plugins',
        'plugins.qemu_bridge',
        'plugins.qemu_bridge.models',
        'plugins.qemu_bridge.parser',
        'plugins.qemu_bridge.translator',
        'plugins.qemu_bridge.validator',
        'plugins.qemu_bridge.standalone',
        'plugins.qemu_bridge.ai_modify',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=True,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='QEMU Bridge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=TARGET_ARCH,
    codesign_identity=None,
    icon=None,
    entitlements_file=None,
)
