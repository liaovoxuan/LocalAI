# -*- mode: python ; coding: utf-8 -*-

import json
import os
import runpy
from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()
PROJECT_DIR = SPEC_DIR.parent
REPO_ROOT = PROJECT_DIR.parent.parent
SHARED_DIR = REPO_ROOT / "shared"
SOURCE = PROJECT_DIR / "desktop" / "local_ai.py"
RUNTIME_DIR = SHARED_DIR / "runtime" / "llama.cpp"

TARGET_ARCH = os.environ.get("PYINSTALLER_TARGET_ARCH") or None
datas = [
    (str(SHARED_DIR / "resources" / "config.json"), "."),
    (str(SHARED_DIR / "resources" / "version.json"), "."),
    (str(SHARED_DIR / "assets" / "icons"), "assets/icons"),
    (str(PROJECT_DIR / "resources" / "Readme.docx"), "."),
]
runtime_datas = runpy.run_path(str(REPO_ROOT / "tooling" / "packaging_runtime.py"))["runtime_datas"]
datas.extend(runtime_datas(RUNTIME_DIR))


a = Analysis(
    [str(SOURCE)],
    pathex=[str(PROJECT_DIR / "desktop")],
    binaries=[],
    datas=datas,
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
    name='LocalAI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
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
    name='LocalAI',
)
app = BUNDLE(
    coll,
    name='LocalAI.app',
    icon=str(SHARED_DIR / "assets" / "icons" / "localai_dark.icns"),
    bundle_identifier='com.localai.standard',
    version=json.loads((SHARED_DIR / "resources" / "version.json").read_text(encoding="utf-8"))["version"],
)
