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

_PROGRAM = "主程式"

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
        # ★[外審 P1-02] 只有【兩個都不在】才是過渡期的正常狀態★
        #   `current.txt` 在、resolver 不在 = 更新【只送到一半】:
        #   指標說要跑某個版本,而這裡安靜地跑 `<app>\src`。
        #   **實際跑的版本跟指標說的不一樣,而且沒有任何地方講得出來**
        #   —— 人看版本號沒變只會以為更新還沒下來。
        try:
            os.lstat(os.path.join(_HERE, "current.txt"))
        except FileNotFoundError:
            return fallback      # 兩個都不在 —— 過渡期正常,安靜
        except OSError as _e2:
            # 連指標在不在都查不出來 → 一樣要留紀錄(不知道 ≠ 沒事)
            _note_resolver_failure("pointer_check_" + type(_e2).__name__)
            return fallback
        _note_resolver_failure("pointer_without_resolver")
        return fallback
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


def _load_bootstrap_recovery():
    """載入更新復原模組。★一定要從固定的 `<app>/src` 載，不可以走 sys.path★

    ★[外審 2026-08-09 P1-01]★ 舊寫法是 `import bootstrap_recovery`,
    而那時 `sys.path` 開頭已經是【版本解析後】的 `_SRC`。於是:

      * 復原模組本身來自那棵【可能正壞掉的】樹 ——
        它存在的理由正是「上一批更新沒走完、磁碟上新舊混版」;
      * 拿壞掉的東西去修壞掉的東西,是這個專案記過的病灶
        (「復原不可以依賴正在耗盡的資源」)。

    復原處理的是【就地更新】`<app>/src` 的殘局,所以它的家就是那裡。
    ★L2 版本化目錄落地時,`<app>/src/bootstrap_recovery.py` 必須繼續存在★
    —— 找不到就走各支自己的「復原模組不可用」路徑(主程式問人、
    背景程式放行),而不是偷偷改用版本樹裡那一份。

    → 模組物件。★載不進來就【拋例外】★:呼叫端本來就是
    `try: … except Exception:` 的形狀,而 `_report_startup_crash()`
    要靠【當下有活的例外】才寫得出 traceback。回 None 會讓它記成
    `NoneType: None`、對話框變成「啟動失敗：None」——
    診斷資訊比修正前更差。
    """
    import importlib.util
    _p = os.path.join(_HERE, "src", "bootstrap_recovery.py")
    os.lstat(_p)          # 不在 → FileNotFoundError(由呼叫端接)
    _spec = importlib.util.spec_from_file_location(
        "_cmuh_bootstrap_recovery", _p)
    if _spec is None or _spec.loader is None:
        raise ImportError("bootstrap_recovery 的 spec 建不起來:%s" % _p)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    return _mod


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


def _ask_without_the_recovery_module():
    """連 bootstrap_recovery 都載不進來時的詢問視窗（★只用 ctypes★）。

    刻意不共用 `bootstrap_recovery.confirm_start_despite` —— 走到這裡就是因為
    那個模組不能用。這幾行必須自給自足。
    """
    text = ("主程式無法載入「更新復原」模組（bootstrap_recovery.py）。\n\n"
            "這通常代表上一次自動更新沒有完成，程式資料夾裡可能是新舊版本混在\n"
            "一起，行為無法預期。\n\n"
            "建議：關掉所有本套程式後重新啟動一次；仍然不行請找開發者。\n\n"
            "★是否仍要繼續啟動？★（風險自負；按「否」結束）")
    try:
        import ctypes
        # MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 | MB_TOPMOST
        return ctypes.windll.user32.MessageBoxW(
            0, text, "更新未完成", 0x04 | 0x30 | 0x100 | 0x40000) == 6
    except Exception:  # noqa: BLE001  問不到人就不要帶著未知狀態啟動
        return False


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
        # ★[外審 P1-01] 走固定的 `<app>/src`，不走版本解析後的
        #   sys.path：復原模組本身來自可能正壞掉的那棵樹，
        #   就是「拿壞掉的東西修壞掉的東西」。
        bootstrap_recovery = _load_bootstrap_recovery()
    except Exception:  # noqa: BLE001
        # ★[2026-08-02 外審第 2 輪 P1] 這裡原本 `return True` 無條件放行★
        #   但「復原模組自己載不進來」正是它可能被更新換到一半的樣子 —— 那時
        #   磁碟混版的機率最高，卻反而不檢查就啟動。既然無法判斷，就照定案的
        #   政策問人（同樣預設「否」），而不是替使用者決定。
        _report_startup_crash("主程式（更新復原模組載入失敗）")
        return _ask_without_the_recovery_module()
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
