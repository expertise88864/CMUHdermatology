# -*- coding: utf-8 -*-
"""中國醫皮膚科點座標偵測程式 — 啟動器（雙擊執行）。實際邏輯在 src/coord_detector.py。"""
import datetime
import os
import runpy
import sys
import traceback

_PROGRAM = "點座標偵測程式"

_HERE = os.path.dirname(os.path.abspath(__file__))


def _note_resolver_failure(why):
    """[外審 P1-03] ★「resolver 壞了」不可以摺進「沒有指標」那個安靜的正常狀態★

    `version_pointer.py` 對【壞掉的指標】會寫 log,但 stub 這一層以前對
    「resolver 自己載不進來」(語法錯、import 失敗、exec 失敗)只是
    `except Exception: return fallback` —— 一個字都沒留。
    那正是最糟的組合:**實際在跑舊版,而且沒有任何地方說得出來**,
    人看到版本號沒變只會以為更新還沒下來。
    (檔案不存在是另一回事:那是過渡期的正常狀態,見上面的 FileNotFoundError。)

    純標準庫、best-effort;寫不進去也不能擋住開機。
    """
    try:
        import datetime
        _line = ("%s [%s] ★version_pointer 載入失敗(%s)→ 退回舊的 "
                 "<app>\\src★ 這一輪【不是】新版本"
                 % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    _PROGRAM, why))
        with open(os.path.join(_HERE, "version_pointer.log"), "a",
                  encoding="utf-8") as _f:
            _f.write(_line + chr(10))
    except Exception:  # noqa: BLE001  留不下紀錄也不能擋住開機
        pass


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
    _p = os.path.join(_HERE, "version_pointer.py")
    # ★[外審] 只有【這個檔不存在】才是安靜的正常狀態★
    #   ① 用 `except FileNotFoundError` 包住整段是錯的:resolver 存在但
    #      執行時自己去開別的檔失敗,丟的也是 FileNotFoundError ——
    #      一個【壞掉的】resolver 會被當成【還沒送到】而靜默跑舊版。
    #   ② 改用 `isfile()` 也不對(外審第 2 輪):它對【同名目錄】、
    #      【壞掉的連結】一律回 False —— 而那些正是【部署失敗的痕跡】,
    #      卻同樣被當成「還沒送到」。★又把一個看得見的壞變成安靜的壞★。
    #   ③ `os.stat()` 也還不夠(外審第 3 輪):它【會跟隨符號連結】——
    #      一個壞掉的連結照樣丟 FileNotFoundError,於是又被當成
    #      「還沒送到」。而上面②才剛把壞連結歸類成部署失敗的痕跡。
    #   所以用 `os.lstat()`:只有【目錄項目真的不存在】才安靜;
    #   壞掉的連結能被 lstat 到 → 往下走、由載入器失敗、留下紀錄。
    try:
        os.lstat(_p)
    except FileNotFoundError:
        return fallback          # 這個檔還沒送到這台機器 —— 過渡期正常
    except OSError as _e:
        _note_resolver_failure(type(_e).__name__)
        return fallback
    try:
        import importlib.util
        _spec = importlib.util.spec_from_file_location("_cmuh_version_pointer", _p)
        if _spec is None or _spec.loader is None:
            _note_resolver_failure("spec_none")
            return fallback
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        return _mod.resolve_src(_HERE, _PROGRAM).src_dir
    except Exception as _e:  # noqa: BLE001  版本解析失敗絕不可以擋住開機
        _note_resolver_failure(type(_e).__name__)
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
