# -*- mode: python ; coding: utf-8 -*-

import os

TARGET_ARCH = os.environ.get("PYINSTALLER_TARGET_ARCH") or None


a = Analysis(
    ['local_ai.py'],
    pathex=[],
    binaries=[],
    datas=[('config.json', '.'), ('version.json', '.'), ('assets/icons', 'assets/icons'), ('Readme.docx', '.')],
    hiddenimports=['PIL', 'PIL.Image', 'PIL.ImageTk', 'tkinter', 'tkinter.filedialog', 'tkinter.messagebox', 'cpuinfo', 'plugins', 'plugins.qemu_bridge', 'plugins.qemu_bridge.plugin', 'plugins.qemu_bridge.models', 'plugins.qemu_bridge.parser', 'plugins.qemu_bridge.translator', 'plugins.qemu_bridge.validator', 'plugins.qemu_bridge.standalone', 'plugins.qemu_bridge.ai_modify'],
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
    name='LocalAI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=TARGET_ARCH,
    codesign_identity=None,
    icon='assets/icons/localai_dark.ico',
    entitlements_file=None,
)
