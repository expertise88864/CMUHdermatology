# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for 中國醫皮膚科主程式."""
import os
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None
src_dir = os.path.abspath(os.path.join(SPECPATH, '..', '..', 'src'))
assets_dir = os.path.abspath(os.path.join(SPECPATH, '..', '..', 'assets'))
manifest = os.path.abspath(os.path.join(SPECPATH, '..', '..', 'manifest.json'))

hidden = (
    collect_submodules('pystray')
    + collect_submodules('PIL')
    + ['win32com', 'win32com.client', 'win32gui', 'win32console', 'win32con',
       'pkg_resources', 'pkg_resources.py2_warn',
       '_tkinter', 'PIL._tkinter_finder']
)

a = Analysis(
    [os.path.join(src_dir, 'main.py')],
    pathex=[src_dir],
    binaries=[],
    datas=[(assets_dir, 'assets'), (manifest, '.')],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=['numpy', 'pandas', 'matplotlib', 'IPython', 'pytest', 'jupyter'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='中國醫皮膚科主程式',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(assets_dir, 'cmuh_app.ico'),
    uac_admin=True,
)
coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False, upx=False, upx_exclude=[],
    name='中國醫皮膚科主程式',
)
