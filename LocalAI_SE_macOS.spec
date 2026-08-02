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
    [],
    exclude_binaries=True,
    name='LocalAI_SE',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=TARGET_ARCH,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='LocalAI_SE',
)
app = BUNDLE(
    coll,
    name='LocalAI_SE.app',
    icon='assets/icons/localai_dark.icns',
    bundle_identifier='com.localai.se',
)
