# -*- coding: utf-8 -*-
"""中國醫皮膚科會診查詢程式 — 啟動器（雙擊執行）。實際邏輯在 src/consult_query.py。"""
import datetime
import os
import runpy
import sys
import traceback

_PROGRAM = "會診查詢程式"

_HERE = os.path.dirname(os.path.abspath(__file__))


def _resolve_src():
    """[批次L L1] 讀 `current.txt` 決定要載入哪一棵 src。

    ★六支 stub 這一段必須逐字相同★（有測試釘住）。邏輯全在
    `<app>/version_pointer.py`,這裡只負責「載不進來也要能開機」:
    stub 與 version_pointer 都是就地更新的檔,一次更新可能只換到一半 ——
    那時仍然要走現行的 `<app>/src`,不可以讓六支程式一起起不來。
    ★用 spec_from_file_location 而不是把 app 根目錄塞進 sys.path★:
    根目錄進了 sys.path 就會永久參與所有 import 解析。
    """
    fallback = os.path.join(_HERE, "src")
    try:
        import importlib.util
        _p = os.path.join(_HERE, "version_pointer.py")
        _spec = importlib.util.spec_from_file_location("_cmuh_version_pointer", _p)
        if _spec is None or _spec.loader is None:
            return fallback
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        return _mod.resolve_src(_HERE, _PROGRAM).src_dir
    except Exception:  # noqa: BLE001  版本解析失敗絕不可以擋住開機
        return fallback


_SRC = _resolve_src()
# ★[批次L L1 外審 P1] 把「固定的根目錄」與「固定的啟動器」釘進環境★
#   `runpy.run_path` 會把 `sys.argv[0]` 換成被執行的那支源碼（版本化之後是
#   `versions/<V>/src/...`）。沒有這兩個值的話:
#     * `get_app_dir()` 會把 `<app>/versions/<V>` 當成根 → settings/log/assets
#       全部跑進版本目錄（切一次版＝所有設定都不見了）;
#     * `restart_self()` 會直接重跑 V1 的源碼 → 永遠不再讀 `current.txt`。
#   子行程會繼承這兩個值（watchdog 啟動的那些也算）。
os.environ["CMUH_APP_DIR"] = _HERE
os.environ["CMUH_LAUNCHER"] = os.path.abspath(__file__)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


def _report_startup_crash(program_name, *, show_dialog=True):
    """[EH-01] pythonw 沒有主控台：import／啟動階段的例外會靜默死亡、完全沒有 log，
    診間只看到「雙擊沒反應」。這裡只用標準庫把 traceback 寫進 startup_crash.log 並彈
    MessageBox，讓現場至少看得到錯誤。任何一步失敗都吞掉（best-effort），最後由呼叫端 re-raise。

    ★[2026-08-02 外審第 2 輪 P1] `show_dialog=False` 是給【無人看顧】的路徑用的★
    `MessageBoxW` 是同步的：這支程式是 ONLOGON 排程（而且 MultipleInstances=
    IgnoreNew），沒有人會去按那個「確定」—— 行程就永遠停在那裡，走不到
    `SystemExit(3)`，之後每一次排程又因為 IgnoreNew 被忽略。結果是打卡／會診
    ★無限期停擺★，而排程紀錄上連一個非零離開碼都看不到。
    我上一輪為了「留下痕跡」在復原失敗路徑呼叫這一支，等於把自己文件裡
    寫明禁止的東西（無人看顧不可跳窗）加了回去。
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
    if not show_dialog:
        return
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

    ★[2026-08-02 外審第 3 輪 P1] 復原模組載不進來時也【不可以】照常啟動★
    我原本寫「那是查不出來而不是查出有問題，讓打卡永久停擺代價更大」。
    那個理由有一個洞：**退出不等於永久停擺** —— schtasks 每隔幾分鐘就會再叫
    一次，狀況好轉（防毒放手、更新收乾淨）就會自己接回來。而「復原模組自己
    載不進來」正是它被更新換到一半時的樣子，也就是磁碟混版機率最高的時刻。
    在那一刻放行，等於把「不知道安不安全」當成「安全」。
    使用者也不會因此不知情：同一個原因會讓臨床主程式跳窗詢問。
    """
    try:
        import bootstrap_recovery
    except Exception:  # noqa: BLE001
        _report_startup_crash("會診查詢程式（更新復原模組載入失敗）",
                              show_dialog=False)
        return False
    try:
        return bootstrap_recovery.recover_and_report(_HERE, "會診查詢程式").safe_to_start
    except Exception:  # noqa: BLE001
        _report_startup_crash("會診查詢程式（更新復原程序本身失敗）",
                              show_dialog=False)
        return False


if not _recover_incomplete_update():
    # ★非零離開★ 「已有一份在跑」用 0（那是正常結束）；這裡是「這一輪沒有做事」，
    #   要讓排程紀錄看得出差別。下一輪排程會再試。
    raise SystemExit(3)

try:
    runpy.run_path(os.path.join(_SRC, "consult_query.py"), run_name="__main__")
except Exception:  # noqa: BLE001  只攔 Exception；SystemExit（正常退出）照常穿出
    _report_startup_crash("會診查詢程式")
    raise
