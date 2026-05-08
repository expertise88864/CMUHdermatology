# -*- coding: utf-8 -*-
"""路徑與重啟工具。同時支援 .pyw（Python 直跑）與 .exe（PyInstaller 打包）兩種模式。

關鍵概念：
- get_app_dir()：回傳「使用者看得到的程式目錄」（即 settings/、assets/、log 的父層）。
- restart_self()：雙軌重啟邏輯。

【修正 2026.05.04】get_app_dir 智能化偵測，避免 settings/ 分裂：
  原本若使用 `pythonw src/main.py` 啟動，sys.argv[0] = src/main.py，
  app_dir 會回 src/，settings/ 跑去 src/settings/，與雙擊 root launcher 的
  app_dir = repo root 不一致，造成 settings/ 分裂。

  本版改為：若 sys.argv[0] 落在「含有 cmuh_common 的目錄」內，
  自動往上一層（取 src/ 的父層即 repo root），保證 settings/ 永遠在 repo root。
"""
import os
import sys


def is_frozen() -> bool:
    """是否在 PyInstaller 打包後的 .exe 模式下執行。"""
    return getattr(sys, 'frozen', False)


def _looks_like_src_dir(d: str) -> bool:
    """判斷目錄 d 是否為 src/（即包含 cmuh_common/ 子套件的目錄）。"""
    try:
        return os.path.isdir(os.path.join(d, 'cmuh_common')) and \
               os.path.isfile(os.path.join(d, 'cmuh_common', 'version.py'))
    except OSError:
        return False


def get_app_dir() -> str:
    """回傳程式所在目錄（settings/、assets/、log 的父層）。

    - .exe 模式：sys.executable 所在目錄
    - .pyw 模式：
        * 若 sys.argv[0] 在 src/ 內（直接跑 src/main.py 等）→ 回 src/ 的父層
        * 否則（雙擊 root launcher）→ 回 launcher 所在目錄
    """
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))

    main_script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else __file__
    script_dir = os.path.dirname(main_script)

    # 智能偵測：若 script_dir 看起來是 src/，回上一層 repo root
    if _looks_like_src_dir(script_dir):
        parent = os.path.dirname(script_dir)
        if parent and parent != script_dir:
            return parent
    return script_dir


def get_settings_dir() -> str:
    """設定/快取目錄（自動建立）。對應原主程式 SETTINGS_DIR。"""
    d = os.path.join(get_app_dir(), 'settings')
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def get_conf_path(filename: str) -> str:
    """回傳設定檔完整路徑。"""
    return os.path.join(get_settings_dir(), filename)


def get_assets_dir() -> str:
    """靜態資源目錄。.exe 模式優先回 _MEIPASS/assets，否則回 app_dir/assets。"""
    if is_frozen() and hasattr(sys, '_MEIPASS'):
        bundled = os.path.join(sys._MEIPASS, 'assets')  # type: ignore[attr-defined]
        if os.path.isdir(bundled):
            return bundled
    return os.path.join(get_app_dir(), 'assets')


def get_bundled_asset(relative_path: str) -> str:
    """取得內嵌靜態資源（圖示、音效等）。"""
    return os.path.join(get_assets_dir(), relative_path)


def get_log_path(filename: str = 'app.log') -> str:
    """log 檔路徑，預設放在 app_dir 直接層。"""
    return os.path.join(get_app_dir(), filename)


def restart_self(extra_args=None) -> None:
    """雙軌重啟。

    .pyw 模式：os.execv(python.exe, [python.exe, sys.argv[0], ...])
    .exe 模式：os.execv(sys.executable, [sys.executable, ...])
    """
    args = list(extra_args) if extra_args else []
    if is_frozen():
        os.execv(sys.executable, [sys.executable] + args)
    else:
        os.execv(
            sys.executable,
            [sys.executable, os.path.abspath(sys.argv[0])] + args,
        )
