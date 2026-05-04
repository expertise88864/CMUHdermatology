# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for 中國醫皮膚科打卡程式."""
import os
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None
src_dir = os.path.abspath(os.path.join(SPECPATH, '..', '..', 'src'))
assets_dir = os.path.abspath(os.path.join(SPECPATH, '..', '..', 'assets'))
manifest = os.path.abspath(os.path.join(SPECPATH, '..', '..', 'manifest.json'))

hidden = (
    collect_submodules('pystray') + collect_submodules('PIL')
    + collect_submodules('selenium') + collect_submodules('webdriver_manager')
    + ['winotify', 'win32com', 'win32gui', 'win32console', 'win32con',
       '_tkinter', 'PIL._tkinter_finder']
)

a = Analysis(
    [os.path.join(src_dir, 'autoclock.py')],
    pathex=[src_dir],
    datas=[(assets_dir, 'assets'), (manifest, '.')],
    hiddenimports=hidden,
    excludes=['numpy', 'pandas', 'matplotlib', 'IPython', 'pytest'],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='中國醫皮膚科打卡程式',
    console=False, upx=False,
    icon=os.path.join(assets_dir, 'cmuh_app.ico'),
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas,
               name='中國醫皮膚科打卡程式', upx=False)
