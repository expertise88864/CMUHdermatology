# -*- coding: utf-8 -*-
"""路徑與重啟工具。同時支援 .pyw（Python 直跑）與 .exe（PyInstaller 打包）兩種模式。

關鍵概念：
- get_app_dir()：回傳「使用者看得到的程式目錄」（即 .exe 或主 .py 所在目錄），
  不是 PyInstaller 解壓後的 _MEIPASS 暫存目錄。
- restart_self()：雙軌重啟邏輯，取代原主程式 line 4161 的 os.execv 用法。
"""
import os
import sys


def is_frozen() -> bool:
    """是否在 PyInstaller 打包後的 .exe 模式下執行。"""
    return getattr(sys, 'frozen', False)


def get_app_dir() -> str:
    """回傳程式所在目錄（settings/、assets/、線上更新檔的父層）。

    - .pyw 模式：sys.argv[0] 所在目錄（即 main.py / scheduler.py / autoclock.py / coord_detector.py）
    - .exe 模式：sys.executable 所在目錄
    """
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    main_script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else __file__
    return os.path.dirname(main_script)


def get_settings_dir() -> str:
    """設定/快取目錄（自動建立）。對應原主程式 SETTINGS_DIR (line 386)。"""
    d = os.path.join(get_app_dir(), 'settings')
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def get_conf_path(filename: str) -> str:
    """回傳設定檔完整路徑。對應原主程式 get_conf_path (line 395)。"""
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
    """log 檔路徑，預設放在 app_dir 直接層（與原主程式 LOG_FILE 一致）。"""
    return os.path.join(get_app_dir(), filename)


def restart_self(extra_args=None) -> None:
    """雙軌重啟。

    .pyw 模式：os.execv(python.exe, [python.exe, sys.argv[0], ...])
    .exe 模式：os.execv(sys.executable, [sys.executable, ...])
              （sys.executable 即 .exe 自己，不可再傳 main.py 路徑）
    搬自原主程式 line 4161，加入雙軌相容。
    """
    args = list(extra_args) if extra_args else []
    if is_frozen():
        os.execv(sys.executable, [sys.executable] + args)
    else:
        os.execv(
            sys.executable,
            [sys.executable, os.path.abspath(sys.argv[0])] + args,
        )
