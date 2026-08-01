# -*- coding: utf-8 -*-
"""中國醫皮膚科主程式 — 啟動器（雙擊執行）。

實際邏輯在 src/main.py，本檔僅做：
  1. 把 src/ 加到 sys.path
  2. 用 runpy 跑 src/main.py 並把 __name__ 設為 '__main__'

注意：sys.argv[0] 仍指向本啟動器，cmuh_common.paths.get_app_dir() 會回傳 repo 根目錄
       （settings/ / .deps_cache / log 都放在 repo 根，與線上自動更新解出的檔案位置一致）。
"""
import datetime
import os
import runpy
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _report_startup_crash(program_name):
    """[EH-01] pythonw 沒有主控台：import／啟動階段的例外會靜默死亡、完全沒有 log，
    診間只看到「雙擊沒反應」。這裡只用標準庫把 traceback 寫進 startup_crash.log 並彈
    MessageBox，讓現場至少看得到錯誤。任何一步失敗都吞掉（best-effort），最後由呼叫端 re-raise。
    """
    tb = traceback.format_exc()
    exc = sys.exc_info()[1]
    try:
        with open(os.path.join(_HERE, "startup_crash.log"), "a", encoding="utf-8") as f:
            f.write("\n===== %s %s 啟動失敗 =====\n"
                    % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), program_name))
            f.write(tb)
    except Exception:  # noqa: BLE001  寫 log 失敗不能再擋住彈窗/re-raise
        pass
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            0, "%s 啟動失敗：\n%s\n\n詳見程式資料夾內 startup_crash.log" % (program_name, exc),
            "啟動錯誤", 0x10)
    except Exception:  # noqa: BLE001  無 GUI／非 Windows 也不能擋住 re-raise
        pass


def _recover_incomplete_update():
    """[外審 P1-01] ★在 import 任何東西之前，把上一批沒走完的更新收乾淨★

    原本的復原在 `check_and_update()` 裡，那時 main.py 早就把幾十個 cmuh_common
    模組 import 進記憶體了 —— 半套更新的模組已經在跑，復原換掉磁碟上的檔案也
    來不及；若混到連 import 都失敗，根本走不到那一行。

    bootstrap_recovery 只用標準庫、不 import cmuh_common（那正是可能壞掉的東西）。
    它自己壞掉時回 UNKNOWN 而不是拋例外，所以這裡不需要 try —— 但仍然包起來，
    因為「復原模組不見了」（例如它自己被更新到一半）也必須能啟動並看得到原因。
    """
    try:
        import bootstrap_recovery
    except Exception:  # noqa: BLE001
        _report_startup_crash("主程式（更新復原模組載入失敗）")
        return True    # ★載不到就不擋★ 擋住等於用一個未知狀態換掉診間的可用性
    result = bootstrap_recovery.recover_and_report(_HERE, "主程式")
    if result.safe_to_start:
        return True
    return bootstrap_recovery.confirm_start_despite(result, "主程式")


if _recover_incomplete_update():
    try:
        runpy.run_path(os.path.join(_SRC, "main.py"), run_name="__main__")
    except Exception:  # noqa: BLE001  只攔 Exception；SystemExit（正常退出）照常穿出
        _report_startup_crash("主程式")
        raise
