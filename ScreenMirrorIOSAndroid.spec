# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

# Use relative path for portability across environments
# SPECPATH points to the directory containing this spec file
SPEC_DIR = Path(SPECPATH) if SPECPATH else Path('.')
TOOLS_UXPLAY = SPEC_DIR / 'tools' / 'uxplay'

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[(str(TOOLS_UXPLAY), 'tools/uxplay')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ScreenMirrorIOSAndroid',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['ScreenMirrorIOSAndroid.ico'],
)
