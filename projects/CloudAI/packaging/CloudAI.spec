# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

SPEC_DIR = Path(SPECPATH).resolve()
PROJECT_DIR = SPEC_DIR.parent
REPO_ROOT = PROJECT_DIR.parent.parent
SHARED_DIR = REPO_ROOT / "shared"
SOURCE = PROJECT_DIR / "desktop" / "cloud_ai.py"
PLUGIN_EXAMPLES_DIR = PROJECT_DIR / "plugin_examples"
PLUGIN_EXAMPLE_DATAS = [
    (str(path), str(Path("plugin_examples") / path.parent.relative_to(PLUGIN_EXAMPLES_DIR)))
    for path in PLUGIN_EXAMPLES_DIR.rglob("*")
    if path.is_file() and "__pycache__" not in path.parts
]

TARGET_ARCH = os.environ.get("PYINSTALLER_TARGET_ARCH") or None


a = Analysis(
    [str(SOURCE)],
    pathex=[str(PROJECT_DIR / "desktop")],
    binaries=[],
    datas=[
        (str(SHARED_DIR / "resources" / "config.json"), "."),
        (str(SHARED_DIR / "resources" / "version.json"), "."),
        (str(SHARED_DIR / "assets" / "icons"), "assets/icons"),
        (str(PROJECT_DIR / "resources" / "CloudAI 隐私政策.docx"), "."),
        (str(PROJECT_DIR / "docs" / "third-party-plugins.md"), "docs"),
        *PLUGIN_EXAMPLE_DATAS,
    ],
    hiddenimports=[
        'PIL', 'PIL.Image', 'PIL.ImageTk', 'tkinter', 'tkinter.filedialog', 'tkinter.messagebox', 'tkinter.ttk', 'cpuinfo',
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
    name='CloudAI',
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
    icon=str(SHARED_DIR / "assets" / "icons" / "localai_dark.ico"),
    entitlements_file=None,
)
