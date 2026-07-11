# -*- coding: utf-8 -*-
"""中國醫皮膚科打卡程式 — 啟動器（雙擊執行）。實際邏輯在 src/autoclock.py。"""
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


# 單例檢查與 cmuh_common import 也放進 try：若 cmuh_common 損壞(Exception)要被兜底寫 log；
# 而「已有一份在跑」時的 raise SystemExit(0) 屬 BaseException，不會被 except Exception 攔，
# 會照常穿出讓本次啟動安靜結束。
try:
    from cmuh_common.single_instance import ensure_single_instance

    if not ensure_single_instance("Local\\CMUH_Skin_AutoClock_SingleInstance_v1"):
        raise SystemExit(0)

    runpy.run_path(os.path.join(_SRC, "autoclock.py"), run_name="__main__")
except Exception:  # noqa: BLE001  只攔 Exception；SystemExit（單例退出）照常穿出
    _report_startup_crash("打卡程式")
    raise
