# -*- coding: utf-8 -*-
"""中國醫皮膚科會診查詢程式 — 啟動器（雙擊執行）。實際邏輯在 src/consult_query.py。"""
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

    ★這一支會對 HIS 送輸入，所以復原沒完成就【不進應用程式】★
    它會 PostMessage 點擊 systemftp 的欄位與表格、輸入帳密（見 consult_query.py
    的 `_click_field` / 登入流程），所以「查詢」不代表唯讀。
    不能跳窗是因為它也會被排程與主程式叫起來、不一定有人在螢幕前。
    所以是「無聲退出」：使用者或主程式下次再開啟時會重試。

    ★[2026-08-02 外審第 2 輪 P1] 第一版這裡是丟棄結果照樣啟動的★
    我當時的理由是「無人看顧就不該擋」。那個理由把兩件事混在一起了：不能【跳窗】
    是對的，不能【退出】則不成立 —— 退出之後排程會再叫它一次，而混版模組一旦
    載進記憶體就再也收不回來，那正是這一批要消滅的失敗模式。
    使用者也不會因此不知情：同一個原因會讓臨床主程式跳窗詢問。

    復原模組本身載不進來時【照常啟動】：那是「查不出來」而不是「查出有問題」，
    而在沒有任何診斷資訊的情況下讓打卡永久停擺，代價比帶著疑慮跑一輪大。
    """
    try:
        import bootstrap_recovery
    except Exception:  # noqa: BLE001
        return True
    try:
        return bootstrap_recovery.recover_and_report(_HERE, "會診查詢程式").safe_to_start
    except Exception:  # noqa: BLE001
        return True


if not _recover_incomplete_update():
    raise SystemExit(0)

try:
    runpy.run_path(os.path.join(_SRC, "consult_query.py"), run_name="__main__")
except Exception:  # noqa: BLE001  只攔 Exception；SystemExit（正常退出）照常穿出
    _report_startup_crash("會診查詢程式")
    raise
