# -*- coding: utf-8 -*-
"""依賴檢查與安裝（runtime 入口層）。搬自原主程式 line 132-200，泛化以支援多入口。

每個 entry script 啟動最前面呼叫 ensure_dependencies(REQUIRED_LIBS_FOR_THIS_APP)。

【強化版指紋】指紋包含 (pkg:imp 對 + Python 主版本)。Python 升級後快取自動失效。
"""
import gc
import importlib
import logging
import os
import sys
from typing import Iterable

from cmuh_common.paths import get_app_dir


def _build_fingerprint(required_libs: Iterable[tuple]) -> str:
    py_ver = f"py{sys.version_info[0]}.{sys.version_info[1]}"
    libs = "|".join(f"{a}:{b}" for a, b in required_libs)
    return f"{py_ver}|{libs}"


def ensure_dependencies(
    required_libs: list,
    deps_cache_filename: str = '.deps_cache',
) -> None:
    """檢查並（必要時）安裝 required_libs。

    required_libs: [(pip_name, import_name), ...]
    """
    fingerprint = _build_fingerprint(required_libs)
    deps_cache_file = os.path.join(get_app_dir(), deps_cache_filename)

    main_script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else __file__

    # 快速路徑：快取與指紋一致 + 比腳本新 → 跳過
    try:
        if os.path.exists(deps_cache_file):
            with open(deps_cache_file, 'r', encoding='utf-8') as cf:
                cache_lines = cf.read().splitlines()
            if cache_lines and cache_lines[0].strip() == fingerprint:
                cache_mtime = os.path.getmtime(deps_cache_file)
                try:
                    script_mtime = os.path.getmtime(main_script)
                except OSError:
                    script_mtime = 0
                if cache_mtime > script_mtime:
                    return
    except Exception:
        logging.debug("讀依賴快取失敗", exc_info=True)

    # 完整檢查：哪些缺
    missing_libs = []
    for pkg, imp in required_libs:
        try:
            importlib.import_module(imp)
        except ImportError:
            missing_libs.append((pkg, imp))

    # 缺的話跳 UI
    if missing_libs:
        from cmuh_common.deps_installer import DependencyInstaller
        app = DependencyInstaller(required_libs, missing_libs)
        app.mainloop()
        is_finished = app.is_finished
        app.destroy()
        del app
        gc.collect()  # [核心修正] 清除 Tkinter 變數，避免背景執行緒 Variable.__del__ 崩潰
        if not is_finished:
            sys.exit(0)

    # 寫快取
    try:
        with open(deps_cache_file, 'w', encoding='utf-8') as f:
            f.write(fingerprint + "\n")
    except Exception:
        logging.debug("寫依賴快取失敗", exc_info=True)
