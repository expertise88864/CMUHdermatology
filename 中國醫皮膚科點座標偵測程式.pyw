# -*- coding: utf-8 -*-
"""中國醫皮膚科點座標偵測程式 — 啟動器（雙擊執行）。實際邏輯在 src/coord_detector.py。"""
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
    """[外審 P1-01] ★在 import 任何 cmuh_common 之前★ 把上一批沒走完的更新收乾淨。

    ★這一支【不擋啟動】，與臨床主程式不同★
    主程式是有人看著的，復原不完整會跳窗問人（見該檔）。這一支是
    非臨床、不碰 HIS 寫入的工具程式，跳一個沒有人會按的視窗只會讓行程卡在那裡、
    連重試的機會都沒有 —— 比帶著混版跑更糟。所以這裡只做「修磁碟 ＋ 留紀錄」。
    """
    try:
        import bootstrap_recovery
        bootstrap_recovery.recover_and_report(_HERE, "點座標偵測程式")
    except Exception:  # noqa: BLE001  復原失敗不可以擋住本程式啟動
        pass


_recover_incomplete_update()

try:
    runpy.run_path(os.path.join(_SRC, "coord_detector.py"), run_name="__main__")
except Exception:  # noqa: BLE001  只攔 Exception；SystemExit（正常退出）照常穿出
    _report_startup_crash("點座標偵測程式")
    raise
