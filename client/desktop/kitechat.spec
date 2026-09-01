# -*- mode: python ; coding: utf-8 -*-
# KiteChat Windows client build spec — ONEFILE mode (single exe)
import os

DESK = os.path.dirname(os.path.abspath(SPEC))
WEB = os.path.normpath(os.path.join(DESK, '..', 'web'))

a = Analysis(
    [os.path.join(DESK, 'main.py')],
    pathex=[DESK],
    binaries=[],
    datas=[(WEB, 'web'), (os.path.join(DESK, 'app.ico'), '.')],
    hiddenimports=['webview'],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

# ONEFILE: bundle scripts + binaries + datas into a single executable.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='KiteChat',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    console=False,
    icon=os.path.join(DESK, 'app.ico'),
)
