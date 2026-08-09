# -*- coding: utf-8 -*-
"""中國醫皮膚科打卡程式 — 啟動器（雙擊執行）。實際邏輯在 src/autoclock.py。"""
import datetime
import os
import runpy
import sys
import traceback

_PROGRAM = "打卡程式"

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
    但它是排程時間到就自己跑、通常沒有人在螢幕前的，不能跳窗 —— 沒有人會按，行程只會卡死在 MessageBox。
    所以是「無聲退出」：schtasks 下一輪會再試，那時檔案鎖多半已經放掉。

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
        # ★[外審 P1-01] 走固定的 `<app>/src`，不走版本解析後的
        #   sys.path：復原模組本身來自可能正壞掉的那棵樹，
        #   就是「拿壞掉的東西修壞掉的東西」。
        bootstrap_recovery = _load_bootstrap_recovery()
    except Exception:  # noqa: BLE001
        _report_startup_crash("打卡程式（更新復原模組載入失敗）",
                              show_dialog=False)
        return False
    try:
        return bootstrap_recovery.recover_and_report(_HERE, "打卡程式").safe_to_start
    except Exception:  # noqa: BLE001
        _report_startup_crash("打卡程式（更新復原程序本身失敗）",
                              show_dialog=False)
        return False


if not _recover_incomplete_update():
    # ★非零離開★ 「已有一份在跑」用 0（那是正常結束）；這裡是「這一輪沒有做事」，
    #   要讓排程紀錄看得出差別。下一輪排程會再試。
    raise SystemExit(3)

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
