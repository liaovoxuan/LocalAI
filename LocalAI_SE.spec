# -*- mode: python ; coding: utf-8 -*-

import os

TARGET_ARCH = os.environ.get("PYINSTALLER_TARGET_ARCH") or None


a = Analysis(
    ['local_ai_se.py'],
    pathex=[],
    binaries=[],
    datas=[('config.json', '.'), ('version.json', '.'), ('assets/icons', 'assets/icons'), ('Readme.docx', '.')],
    hiddenimports=['PIL', 'PIL.Image', 'PIL.ImageTk', 'tkinter', 'tkinter.filedialog', 'tkinter.messagebox', 'cpuinfo'],
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
    name='LocalAI_SE',
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
    icon='assets/icons/localai_dark.ico',
    entitlements_file=None,
)
