# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for 中國醫皮膚科點座標偵測程式."""
import os

block_cipher = None
src_dir = os.path.abspath(os.path.join(SPECPATH, '..', '..', 'src'))
assets_dir = os.path.abspath(os.path.join(SPECPATH, '..', '..', 'assets'))
manifest = os.path.abspath(os.path.join(SPECPATH, '..', '..', 'manifest.json'))

a = Analysis(
    [os.path.join(src_dir, 'coord_detector.py')],
    pathex=[src_dir],
    datas=[(assets_dir, 'assets'), (manifest, '.')],
    hiddenimports=['_tkinter', 'PIL._tkinter_finder'],
    excludes=['numpy', 'pandas', 'matplotlib', 'IPython', 'pytest'],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='中國醫皮膚科點座標偵測程式',
    console=False, upx=False,
    icon=os.path.join(assets_dir, 'cmuh_app.ico'),
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas,
               name='中國醫皮膚科點座標偵測程式', upx=False)
